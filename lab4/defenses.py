"""Защитные механизмы ЛР4 (категория режима `defense`), слоями.

Слои (ключ режима = один слой; `full` = все слои):
  auth      — аутентификация отправителя: шина сверяет заявленного отправителя с истинным (модель подписи);
  authz     — авторизация по таблице полномочий roles.PERMISSIONS; единственный источник команд
              (команды роли «диспетчер» принимаются только от настроенного у получателя диспетчера);
              ограничение полномочий компонентов, читающих недоверенный текст (действие, извлечённое
              из заказа, имеет полномочия автора заказа);
  validate  — схема и диапазоны полей, нейтрализация инструкций в свободном тексте, правдоподобие
              телеметрии и статусов (скорость, расход энергии, минимальное время доставки);
  fresh     — свежесть и идемпотентность (одноразовый номер и метка времени), эпохи назначений;
  monitor   — контроль исполнения: сверка книги заказов с телеметрией, тайм-аут исполнения, лимит
              передач (TTL) с исключением вернувших, репутация и карантин;
  fallback  — тайм-ауты и запасной путь: станция не отвечает или не заряжает -> другая станция;
              клиент повторяет заказ без подтверждения.

Всё реализовано промежуточными обработчиками шины и плагинами-фильтрами агентов; ядро не меняется.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from lab1.mas import roles
from lab1.mas.agents.base import Plugin
from lab1.mas.agents.courier import CHARGING, IDLE, WAITING
from lab1.mas.agents.dispatcher import ASSIGNED, ASSIGNING, PENDING, PICKED
from lab1.mas.messages import Message, Perf
from lab1.mas.world import manhattan

from .common import attack_kind, bump, ensure_integrity, init_counters, malicious_agents

LAYERS = ("auth", "authz", "validate", "fresh", "monitor", "fallback")

COUNTERS = ["def_alarms", "def_blocked", "def_tp", "def_fp", "def_blk_auth", "def_blk_authz", "def_blk_schema",
            "def_blk_fresh", "def_blk_plaus", "def_neutralized", "def_gate_denied", "def_reconciled",
            "def_exec_timeouts", "def_ttl_escalations", "def_parked", "def_quarantined", "def_quarantined_honest",
            "def_untrusted", "def_station_fallbacks", "def_client_resends", "def_strikes"]

# Параметры (переопределяются через SimConfig.params)
DEFAULTS = {
    "fresh_slack": 3,          # допуск к задержке канала при проверке свежести, тактов
    "motion_slack": 1,         # допуск при проверке скорости (клеток)
    "energy_tol": 1.0,         # допуск при проверке расхода энергии
    "untrusted_after": 3,      # неправдоподобных сообщений до отказа в доверии телеметрии
    "miss_limit": 3,           # отчётов подряд без принятого заказа до сверки (дольше переговоров ЛР2)
    "exec_max": 150,           # тактов на забор после подтверждения назначения
    "ttl": 3,                  # передач заказа до эскалации
    "q_min": 3,                # нарушений до карантина
    "q_rep": 0.25,             # и бета-репутация (доставки+1)/(доставки+нарушения+2) ниже порога
    "reply_timeout": 4,        # ожидание ответа станции, тактов
    "queue_timeout": 40,       # ожидание в очереди станции, тактов
    "flat_limit": 5,           # тактов «зарядки» без роста заряда
    "offline_memory": 50,      # сколько помнить неотвечающую станцию
    "resend_timeout": 5,       # клиент: ожидание подтверждения заказа
    "resend_max": 3,
    "note_max_len": 300,
    "reward_max": 1000.0,
}

# Сигнатуры распоряжений в свободном тексте (заметка — только данные)
INSTRUCTION_SIGNATURES = [r"игнорир", r"систем\w*\s*:", r"команд", r"отмен", r"назнач", r"отмет\w*.*доставлен",
                          r"все\s+заказ", r"диспетчер\w*\s*:", r"\bo\d{3}\b", r"courier-\d+", r"ignore", r"system",
                          r"cancel", r"assign", r"override", r"instruction"]
SIGNATURE_RE = re.compile("|".join(INSTRUCTION_SIGNATURES), re.IGNORECASE)

STATIC_BELIEF_KEYS = ("ctype", "payload", "capacity", "cons_empty", "cons_loaded", "lost", "last_seen")
ORDER_KEYS = ("id", "pickup", "dropoff", "weight")

# Действие, извлечённое из текста заказа, -> эквивалентное сообщение (получатель, протокол, перформатив)
ACTION_EQUIV = {"cancel": ("courier", "assign", "cancel"), "mark_delivered": ("client", "order-status", "inform"),
                "reassign_all": ("courier", "assign", "request"), "set_priority": ("dispatcher", "order", "request")}


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


class Defense:
    """Состояние защиты в одном прогоне: включённые слои, репутация курьеров, учёт срабатываний."""

    def __init__(self, sim, layers):
        self.sim = sim
        self.layers = set(layers)
        self.p = {k: sim.cfg.params.get(f"def_{k}", v) for k, v in DEFAULTS.items()}
        self.strikes: Counter = Counter()
        self.delivered: Counter = Counter()
        self.quarantined: set = set()
        self.untrusted: set = set()
        self.epochs: dict = {}
        self.epoch_seq = 0
        self.known_protocols: set = set()

    # --- учёт ---
    def alarm(self, key: str, n: int = 1) -> None:
        bump(self.sim, key, n)
        bump(self.sim, "def_alarms", n)

    def _tp_fp(self, msg: Message | None, malicious: bool | None = None) -> None:
        bad = attack_kind(msg) is not None if malicious is None else malicious
        bump(self.sim, "def_tp" if bad else "def_fp")

    def block_at_bus(self, msg: Message, layer: str, reason: str):
        self.alarm(f"def_blk_{layer}")
        bump(self.sim, "def_blocked")
        self._tp_fp(msg)
        self.sim.trace.event("def_block", layer=layer, reason=reason, id=msg.id, frm=msg.sender, to=msg.receiver,
                             origin=msg.origin, proto=msg.protocol, perf=msg.perf.value)
        return None   # шина сама учтёт msgs_blocked и запишет msg_blocked

    def reject(self, agent, msg: Message, layer: str, reason: str) -> bool:
        """Отбросить сообщение на стороне получателя (плагин-фильтр)."""
        self.alarm(f"def_blk_{layer}")
        bump(self.sim, "def_blocked")
        self._tp_fp(msg)
        self.sim.metrics.on_message_blocked(msg)
        self.sim.trace.event("def_block", layer=layer, reason=reason, id=msg.id, frm=msg.sender, to=msg.receiver,
                             origin=msg.origin, proto=msg.protocol, perf=msg.perf.value, at=agent.id)
        return True

    def strike(self, cid: str, reason: str) -> None:
        self.strikes[cid] += 1
        bump(self.sim, "def_strikes")
        self.sim.trace.event("def_strike", courier=cid, reason=reason, strikes=self.strikes[cid])

    def reputation(self, cid: str) -> float:
        s, f = self.delivered[cid], self.strikes[cid]
        return (s + 1) / (s + f + 2)

    def detected(self, aid: str) -> None:
        if aid in malicious_agents(self.sim) and "def_detect_tick" not in self.sim.metrics.custom:
            self.sim.metrics.custom["def_detect_tick"] = self.sim.tick

    # --- установка ---
    def install(self) -> None:
        sim = self.sim
        ensure_integrity(sim)
        init_counters(sim, COUNTERS)
        self.known_protocols = {k[2] for k in roles.PERMISSIONS}
        if "fresh" in self.layers:
            sim.bus.middlewares.insert(0, self.stamp)          # «подпись» на стороне отправителя
        if self.layers & {"auth", "authz", "validate"}:
            sim.bus.middlewares.append(self.gateway)           # шлюз на стороне получателя
        if "authz" in self.layers:
            for a in sim.agents.values():
                for p in a.plugins:
                    if getattr(p, "processes_untrusted_text", False):
                        p.gate = self.gate
        d = sim.dispatcher
        if "monitor" in self.layers:
            d.add_plugin(MonitorGuard(self), first=True)
            orig = d.couriers_available     # карантин: курьер не предлагается распределителю
            d.couriers_available = lambda: [c for c in orig() if c not in self.quarantined]
        if "validate" in self.layers:
            d.add_plugin(PlausibilityGuard(self, d), first=True)
        for a in list(sim.agents.values()):
            if "fallback" in self.layers:
                if a.role == "courier":
                    a.add_plugin(StationFallback(self), first=True)
                elif a.role == "client":
                    a.add_plugin(ClientResend(self), first=True)
            if "fresh" in self.layers:
                a.add_plugin(FreshGuard(self), first=True)

    # --- слой fresh: отправитель ставит одноразовый номер, метку времени и эпоху назначения ---
    def stamp(self, msg: Message, sim):
        with sim.metrics.timer("defense"):
            sig = {"n": f"{msg.origin}#{msg.id}", "ts": sim.tick}
            if msg.protocol == "assign" and msg.origin == msg.sender and sim.role_of(msg.origin) == "dispatcher":
                oid = msg.content.get("id") if msg.perf == Perf.REQUEST else msg.content.get("order_id")
                key = (msg.origin, msg.receiver, oid)
                if msg.perf == Perf.REQUEST:
                    self.epoch_seq += 1
                    self.epochs[key] = self.epoch_seq
                if key in self.epochs:
                    sig["ep"] = self.epochs[key]
            msg.content = {**msg.content, "_sig": sig}
        return msg

    # --- шлюз: auth, authz, схема ---
    def gateway(self, msg: Message, sim):
        with sim.metrics.timer("defense"):
            if "auth" in self.layers and msg.origin and msg.origin != msg.sender:
                return self.block_at_bus(msg, "auth", "origin!=sender")
            if "authz" in self.layers:
                r = self.check_authz(msg)
                if r:
                    return self.block_at_bus(msg, "authz", r)
            if "validate" in self.layers:
                r = self.check_schema(msg)
                if r:
                    return self.block_at_bus(msg, "schema", r)
        return msg

    def check_authz(self, msg: Message) -> str | None:
        sim = self.sim
        sr, rr = sim.role_of(msg.sender), sim.role_of(msg.receiver)
        if msg.protocol in self.known_protocols and not roles.is_allowed(sr, rr, msg.protocol, msg.perf.value):
            return f"not-permitted:{sr}->{rr}:{msg.protocol}/{msg.perf.value}"
        primary = getattr(sim.agents.get(msg.receiver), "dispatcher_id", None)
        if sr == "dispatcher" and primary is not None and msg.sender != primary:
            return "not-primary-dispatcher"
        return None

    def gate(self, d, act: dict, provenance: str, own: str) -> bool:
        """Действие из текста заказа исполняется только в пределах полномочий автора заказа."""
        role = self.sim.role_of(provenance)
        rr, proto, perf = ACTION_EQUIV.get(act["type"], (None, None, None))
        ok = rr is not None and roles.is_allowed(role, rr, proto, perf)
        if act["type"] == "set_priority":
            ok = ok and act.get("targets") == [own]
        if not ok:
            self.alarm("def_gate_denied")
            self._tp_fp(None, malicious=True)
            self.sim.trace.event("def_gate", order=own, action=act["type"], provenance=provenance, role=role)
        return ok

    def check_schema(self, msg: Message) -> str | None:
        c = msg.content
        if not isinstance(c, dict):
            return "content-not-dict"
        if all(k in c for k in ORDER_KEYS):
            return self.check_order(msg)
        if msg.protocol == "telemetry":
            return self.check_telemetry(msg)
        return None

    def _in_grid(self, p) -> bool:
        w = self.sim.world
        return (isinstance(p, (list, tuple)) and len(p) == 2 and all(isinstance(v, int) for v in p)
                and 0 <= p[0] < w.width and 0 <= p[1] < w.height)

    def check_order(self, msg: Message) -> str | None:
        c = msg.content
        if not (self._in_grid(c.get("pickup")) and self._in_grid(c.get("dropoff"))):
            return "coords"
        max_payload = max((a.payload for a in self.sim.couriers), default=0)
        if not _num(c.get("weight")) or not 0 < c["weight"] <= max_payload:
            return "weight"
        if not (_num(c.get("created")) and _num(c.get("deadline")) and c["deadline"] > c["created"]):
            return "deadline"
        if not _num(c.get("reward")) or not 0 <= c["reward"] <= self.p["reward_max"]:
            return "reward"
        fixes = {}
        if c.get("priority", "normal") not in ("normal", "urgent"):
            fixes["priority"] = "normal"
        note = c.get("note", "")
        if not isinstance(note, str):
            fixes["note"] = ""
        else:
            clean = note[: self.p["note_max_len"]]
            if SIGNATURE_RE.search(clean):
                clean = ""
            if clean != note:
                fixes["note"] = clean
        if fixes:
            msg.content = {**c, **fixes}
            self.alarm("def_neutralized")
            self._tp_fp(msg)
            self.sim.trace.event("def_neutralize", id=msg.id, order=c.get("id"), fields=sorted(fixes))
        return None

    def check_telemetry(self, msg: Message) -> str | None:
        c = msg.content
        if "pos" in c and not self._in_grid(c["pos"]):
            return "pos"
        a = self.sim.agents.get(msg.sender)
        cap = getattr(a, "capacity", None)
        if "battery" in c and (not _num(c["battery"]) or c["battery"] < 0 or (cap is not None and c["battery"] > cap + 1e-6)):
            return "battery"
        if "tasks" in c and not (isinstance(c["tasks"], list) and all(isinstance(t, str) for t in c["tasks"])):
            return "tasks"
        static = [k for k in STATIC_BELIEF_KEYS if k in c]
        if static:   # телеметрия не может менять априорные сведения о курьере
            msg.content = {k: v for k, v in c.items() if k not in static}
            self.alarm("def_neutralized")
            self._tp_fp(msg)
        return None


# ---------------------------------------------------------------------------------------------
# Получатель: свежесть, идемпотентность, эпохи (все агенты)
# ---------------------------------------------------------------------------------------------
class FreshGuard(Plugin):
    name = "lab4-fresh"

    def __init__(self, D: Defense):
        self.D = D
        self.seen: set = set()
        self.epochs: dict = {}    # (order_id, издатель) -> эпоха назначения (у курьера)

    def on_message(self, agent, msg: Message) -> bool:
        sim = agent.sim
        with sim.metrics.timer("defense"):
            sig = msg.content.get("_sig") if isinstance(msg.content, dict) else None
            if not isinstance(sig, dict):
                return False
            if sig.get("n") in self.seen:
                return self.D.reject(agent, msg, "fresh", "duplicate")
            age = sim.tick - sig.get("ts", sim.tick)
            if age > sim.cfg.msg_latency + self.D.p["fresh_slack"]:
                return self.D.reject(agent, msg, "fresh", f"stale:{age}")
            self.seen.add(sig.get("n"))
            if agent.role == "courier" and msg.protocol == "assign":
                if msg.perf == Perf.REQUEST and "ep" in sig:
                    self.epochs[(msg.content.get("id"), msg.sender)] = sig["ep"]
                elif msg.perf == Perf.CANCEL:
                    oid = msg.content.get("order_id")
                    if any(k[0] == oid for k in self.epochs):
                        ep = sig.get("ep")
                        if ep is None or self.epochs.get((oid, msg.sender)) != ep:
                            return self.D.reject(agent, msg, "fresh", "epoch")
        return False


# ---------------------------------------------------------------------------------------------
# Диспетчер: правдоподобие телеметрии и статусов (слой validate)
# ---------------------------------------------------------------------------------------------
class PlausibilityGuard(Plugin):
    name = "lab4-plausibility"

    def __init__(self, D: Defense, d):
        self.D = D
        # априорное знание: стартовое состояние флота (как в init_beliefs)
        self.last = {cid: (0, tuple(b["pos"]), float(b["battery"])) for cid, b in d.beliefs.items()}
        self.anchors: dict = defaultdict(list)     # cid -> [(t, pos)] достоверные точки из статусов
        self.picked_at: dict = {}
        self.violations: Counter = Counter()
        self.stations = list(d.sim.world.station_pos.values())

    def on_message(self, d, msg: Message) -> bool:
        with d.sim.metrics.timer("defense"):
            if msg.protocol == "telemetry" and msg.perf == Perf.INFORM and msg.sender in d.beliefs:
                return self.check_telemetry(d, msg)
            if msg.protocol == "order-status" and msg.perf == Perf.INFORM:
                return self.check_status(d, msg)
        return False

    def _violation(self, d, msg, cid: str, reason: str) -> bool:
        self.violations[cid] += 1
        self.D.strike(cid, reason)
        if self.violations[cid] >= self.D.p["untrusted_after"] and cid not in self.D.untrusted:
            self.D.untrusted.add(cid)
            self.D.alarm("def_untrusted")
            self.D.detected(cid)
            d.log("def_untrusted", courier=cid)
        return self.D.reject(d, msg, "plaus", reason)

    def check_telemetry(self, d, msg: Message) -> bool:
        cid, c = msg.sender, msg.content
        if cid in self.D.untrusted:
            return self.D.reject(d, msg, "plaus", "untrusted-source")
        if "pos" not in c or "battery" not in c:
            return False
        t, pos, bat = msg.t, tuple(c["pos"]), float(c["battery"])
        t0, p0, b0 = self.last.get(cid, (t, pos, bat))
        chain = [(t0, p0)] + [a for a in self.anchors[cid] if a[0] >= t0] + [(t, pos)]
        slack = self.D.p["motion_slack"]
        dist, station_possible = 0, False
        for (ta, pa), (tb, pb) in zip(chain, chain[1:]):
            step = manhattan(pa, pb)
            if step > (tb - ta) + slack:
                return self._violation(d, msg, cid, "motion")
            dist += step
            if any(manhattan(pa, s) + manhattan(s, pb) <= (tb - ta) + slack for s in self.stations):
                station_possible = True
        cons = d.beliefs[cid]["cons_empty"]
        if not station_possible and bat > b0 - dist * cons + self.D.p["energy_tol"]:
            return self._violation(d, msg, cid, "energy")
        self.last[cid] = (t, pos, bat)
        self.anchors[cid] = []
        return False

    def check_status(self, d, msg: Message) -> bool:
        rec = d.records.get(msg.content.get("order_id"))
        if rec is None:
            return False
        cid, status = msg.sender, msg.content.get("status")
        if status == "picked":
            self.picked_at[(cid, rec.order.id)] = msg.t
            self.anchors[cid].append((msg.t, tuple(rec.order.pickup)))
        elif status == "delivered":
            trip = rec.order.trip()
            tp = self.picked_at.get((cid, rec.order.id))
            if tp is not None:
                ok = msg.t - tp >= trip - 1
            else:
                ok = rec.courier == cid and msg.t - rec.since >= trip - 1
            if not ok:
                return self._violation(d, msg, cid, "delivery-too-fast")
            self.anchors[cid].append((msg.t, tuple(rec.order.dropoff)))
        return False


# ---------------------------------------------------------------------------------------------
# Диспетчер: контроль исполнения, TTL, репутация и карантин (слой monitor)
# ---------------------------------------------------------------------------------------------
LOST_TRACK = "lost-track"   # заказ «припаркован»: посылки нет на месте забора, ждём статуса


class MonitorGuard(Plugin):
    name = "lab4-monitor"

    def __init__(self, D: Defense):
        self.D = D
        self.excluded: dict = defaultdict(set)
        self.miss: Counter = Counter()
        self.reported: set = set()
        self.to_reconcile: list = []
        self.to_park: list = []
        self.escalated: set = set()
        self.hidden: set = set()

    def on_message(self, d, msg: Message) -> bool:
        with d.sim.metrics.timer("defense"):
            p, perf, c = msg.protocol, msg.perf, msg.content
            if p == "telemetry" and perf == Perf.INFORM and msg.sender in d.beliefs:
                cid = msg.sender
                tasks = set(c.get("tasks") or [])
                if c.get("carrying"):
                    tasks.add(c["carrying"])
                for oid in tasks:
                    self.reported.add((cid, oid))
                    rec = d.records.get(oid)
                    if rec is not None and rec.status == LOST_TRACK:
                        d.set_status(rec, PICKED if oid == c.get("carrying") else ASSIGNED, cid)
                for rec in d.records.values():
                    if rec.courier != cid or rec.status not in (ASSIGNED, PICKED) or msg.t <= rec.since:
                        continue
                    oid = rec.order.id
                    if oid in tasks:
                        self.miss.pop(oid, None)
                        continue
                    self.miss[oid] += 1
                    if self.miss[oid] >= self.D.p["miss_limit"]:
                        self.to_reconcile.append((oid, cid))
            elif p == "order-status" and perf == Perf.INFORM and c.get("status") == "delivered":
                if msg.sender in d.beliefs:
                    self.D.delivered[msg.sender] += 1
            elif p == "assign" and perf == Perf.FAILURE:
                rec = d.records.get(c.get("order_id"))
                if rec is not None and rec.courier == msg.sender and rec.status in (ASSIGNING, ASSIGNED):
                    reason = str(c.get("reason"))
                    self.excluded[rec.order.id].add(msg.sender)
                    if reason == "already-taken":
                        self.to_park.append(rec.order.id)
                    else:
                        self.D.strike(msg.sender, "returned:" + reason)
        return False

    def _withdraw(self, d, rec, reason: str) -> None:
        cid = rec.courier
        d.send(cid, Perf.CANCEL, "assign", {"order_id": rec.order.id}, conv=rec.order.id)
        d.release(rec, reason)
        self.excluded[rec.order.id].add(cid)

    def step(self, d, tick: int) -> None:
        with d.sim.metrics.timer("defense"):
            D = self.D
            # 1) сверка книги заказов с телеметрией
            for oid, cid in self.to_reconcile:
                rec = d.records.get(oid)
                self.miss.pop(oid, None)
                if rec is None or rec.courier != cid:
                    continue
                if rec.status == ASSIGNED:
                    self._withdraw(d, rec, "reconcile")
                    D.alarm("def_reconciled")
                    if (cid, oid) not in self.reported:
                        D.strike(cid, "broken-promise")
                elif rec.status == PICKED and oid not in self.hidden:
                    self.hidden.add(oid)
                    D.strike(cid, "hidden-parcel")
            self.to_reconcile = []
            # 2) посылки нет на месте забора: не переназначать по кругу, ждать статуса или телеметрии
            for oid in self.to_park:
                rec = d.records.get(oid)
                if rec is not None and rec.status == PENDING:
                    d.set_status(rec, LOST_TRACK, None)
                    D.alarm("def_parked")
                    d.log("def_parked", order=oid)
            self.to_park = []
            # 3) тайм-аут исполнения: принял, но не забирает
            for rec in list(d.records.values()):
                if rec.status == ASSIGNED and tick - rec.since > D.p["exec_max"]:
                    cid = rec.courier
                    self._withdraw(d, rec, "exec-timeout")
                    D.alarm("def_exec_timeouts")
                    D.strike(cid, "exec-timeout")
            # 4) лимит передач: эскалация, исключение прежних исполнителей
            for rec in d.records.values():
                if rec.status == PENDING and rec.hops >= D.p["ttl"] and rec.order.id not in self.escalated:
                    self.escalated.add(rec.order.id)
                    D.alarm("def_ttl_escalations")
                    d.log("def_ttl", order=rec.order.id, hops=rec.hops, excluded=sorted(self.excluded[rec.order.id]))
            # 5) репутация и карантин
            for cid, s in list(D.strikes.items()):
                if cid in D.quarantined or cid not in d.beliefs:
                    continue
                if s >= D.p["q_min"] and D.reputation(cid) < D.p["q_rep"]:
                    self.quarantine(d, cid)
            # 6) исключения для переназначения (через refused_by, которое учитывает распределитель)
            avail = set(d.couriers_available())
            for rec in d.records.values():
                ex = self.excluded.get(rec.order.id)
                if rec.status != PENDING or not ex:
                    continue
                eligible = {c for c in avail if rec.order.weight <= d.beliefs[c]["payload"]}
                live = ex & eligible
                if eligible and live >= eligible:
                    ex.clear()          # исключили всех, кто может везти, — даём ещё один круг
                    continue
                if live:
                    rec.refused_by |= live
                    rec.last_refuse = tick

    def quarantine(self, d, cid: str) -> None:
        D = self.D
        D.quarantined.add(cid)
        D.alarm("def_quarantined")
        if cid not in malicious_agents(d.sim):
            bump(d.sim, "def_quarantined_honest")
        D.detected(cid)
        d.log("def_quarantine", courier=cid, strikes=D.strikes[cid], delivered=D.delivered[cid])
        for rec in list(d.records.values()):
            if rec.courier == cid and rec.status in (ASSIGNING, ASSIGNED):
                self._withdraw(d, rec, "quarantine")


# ---------------------------------------------------------------------------------------------
# Курьер и клиент: тайм-ауты и запасной путь (слой fallback)
# ---------------------------------------------------------------------------------------------
class StationFallback(Plugin):
    name = "lab4-station-fallback"

    def __init__(self, D: Defense):
        self.D = D
        self.reset()

    def reset(self) -> None:
        self.target = None
        self.since = None
        self.replied = False
        self.queued = False
        self.resent = False
        self.last_bat = None
        self.flat = 0

    def on_message(self, c, msg: Message) -> bool:
        if c.station_target is not None and msg.sender == c.station_target:
            self.replied = True
            if msg.perf == Perf.INFORM and "queued" in msg.content:
                self.queued = True
        return False

    def step(self, c, tick: int) -> None:
        with c.sim.metrics.timer("defense"):
            if c.state not in (WAITING, CHARGING) or c.station_target is None:
                self.reset()
                return
            if self.target != c.station_target or self.since is None:
                self.reset()
                self.target, self.since, self.last_bat = c.station_target, tick, c.battery
                return
            rising = c.battery > self.last_bat + 1e-9
            self.last_bat = c.battery
            p = self.D.p
            if c.state == CHARGING:
                self.flat = 0 if rising else self.flat + 1
                if self.flat >= p["flat_limit"]:
                    self.leave(c, tick, "no-charge")
                return
            if rising:                       # фактически заряжается, хотя ответ мог потеряться
                self.since = tick
                return
            if self.queued or self.replied:
                if tick - self.since > p["queue_timeout"]:
                    self.leave(c, tick, "queue-timeout")
                return
            if tick - self.since > p["reply_timeout"]:
                if not self.resent:
                    self.resent, self.since = True, tick
                    c.send(c.station_target, Perf.REQUEST, "charge",
                           {"battery": round(c.battery, 2), "capacity": c.capacity})
                else:
                    self.leave(c, tick, "no-reply")

    def leave(self, c, tick: int, reason: str) -> None:
        sid = c.station_target
        c.known_offline[sid] = tick
        c.send(sid, Perf.CANCEL, "charge", {"reason": reason})
        bad = {s for s, t in c.known_offline.items() if tick - t < self.D.p["offline_memory"]}
        other = c.nearest_station(exclude=bad)
        self.D.alarm("def_station_fallbacks")
        c.log("def_station_fallback", station=sid, reason=reason, to=other, battery=round(c.battery, 1))
        self.reset()
        if other is not None:
            c.go_charge(other)
        else:
            c.station_target, c.state = None, IDLE


class ClientResend(Plugin):
    name = "lab4-client-resend"

    def __init__(self, D: Defense):
        self.D = D
        self.acked: set = set()
        self.sent: dict = {}
        self.tries: Counter = Counter()
        self.seen_idx = 0

    def on_message(self, cl, msg: Message) -> bool:
        if msg.protocol == "order" and msg.perf == Perf.AGREE:
            self.acked.add(msg.content.get("order_id"))
        return False

    def step(self, cl, tick: int) -> None:
        for o in cl.schedule[self.seen_idx:cl.next_idx]:
            self.sent[o.id] = tick
        self.seen_idx = cl.next_idx
        for oid, t0 in list(self.sent.items()):
            if oid in self.acked or self.tries[oid] >= self.D.p["resend_max"]:
                continue
            if tick - t0 > self.D.p["resend_timeout"]:
                o = next(x for x in cl.schedule if x.id == oid)
                self.tries[oid] += 1
                self.sent[oid] = tick
                self.D.alarm("def_client_resends")
                cl.send(cl.dispatcher_id, Perf.REQUEST, "order", o.to_msg(), conv=oid)


def make_install(layers):
    def install(sim) -> None:
        Defense(sim, layers).install()
    return install


DEFENSES = [
    ("auth", "Аутентификация отправителя", ("auth",),
     "Шина сверяет заявленного отправителя с истинным (модель цифровой подписи)"),
    ("authz", "Авторизация и единственный источник команд", ("authz",),
     "Таблица полномочий roles.PERMISSIONS; команды диспетчера только от своего диспетчера; действия из текста заказа — с полномочиями автора заказа"),
    ("validate", "Схема, диапазоны и правдоподобие", ("validate",),
     "Проверка полей и координат, нейтрализация инструкций в заметках, правдоподобие телеметрии и статусов"),
    ("fresh", "Свежесть, идемпотентность, эпохи", ("fresh",),
     "Одноразовые номера и метки времени сообщений, отбрасывание повторов и устаревших, эпохи назначений"),
    ("monitor", "Контроль исполнения, TTL, карантин", ("monitor",),
     "Сверка книги заказов с телеметрией, тайм-аут исполнения, лимит передач, репутация и карантин курьеров"),
    ("fallback", "Тайм-ауты и запасной путь", ("fallback",),
     "Станция не отвечает или не заряжает — другая станция; клиент повторяет неподтверждённый заказ"),
    ("full", "Все защитные слои", LAYERS, "Все слои вместе"),
]
