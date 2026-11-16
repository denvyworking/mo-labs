"""Механизм 2 (режим charging=negotiation): переговоры о слоте зарядки + арбитр мощности сети.

Протокол "slot" (курьер <-> станции), запускается, когда курьеру пора заряжаться:
  1. courier -> все станции  CFP {заявка, pos}
  2. station -> courier      PROPOSE {eta, wait, start} — оценка ожидания по своему расписанию
                             (занятые места, очередь, действующие брони, доля лимита мощности)
                             или REFUSE {reason}
  3. курьер ждёт ответы (не дольше cfp_timeout), выбирает станцию с минимальной стоимостью
     «путь до станции + ожидаемое ожидание + путь от станции к следующей точке маршрута»
  4. courier -> выбранная    ACCEPT {eta};  courier -> остальные  REJECT
  5. station -> courier      INFORM {booked, until} — бронь с окном [eta, eta + booking_grace]
  6. прибытие: REQUEST/charge (как в режиме priority); курьер по действующей брони получает надбавку
     к критичности. Не прибыл до конца окна — станция снимает бронь и шлёт INFORM {event: expired};
     курьер, ещё едущий к станции, переговаривается заново (перезапрос). Отказ от брони — CANCEL.
Зарядка начинается только после получения станцией токена мощности от арбитра (см. grid.py).
"""
from __future__ import annotations

import heapq
import itertools

from lab1.mas.agents.courier import CHARGING, TO_STATION, WAITING, WORK_STATES, CourierAgent
from lab1.mas.agents.station import StationAgent
from lab1.mas.charging import OFFLINE_MEMORY
from lab1.mas.messages import Message, Perf
from lab1.mas.world import manhattan

from .common import P, criticality, make_claims, quota_target
from .priority import Lab3CourierCharging, PriorityStationPolicy

_req_ids = itertools.count(1)


class NegotiatingCourierCharging(Lab3CourierCharging):
    name = "lab3-negotiation"

    def __init__(self, selfish: bool = False):
        super().__init__(selfish)
        self.phase: str | None = None      # None | "cfp" | "booked" | "walkin"
        self.conv: str | None = None
        self.cfp_t = 0
        self.asked: set[str] = set()
        self.proposals: dict[str, dict | None] = {}
        self.seq = 0

    # --- запуск переговоров ---
    def critical(self, c: CourierAgent) -> bool:
        """Заряда едва хватает до ближайшей станции — переговоры некогда вести."""
        sid = c.nearest_station()
        cons = c.cons_loaded if c.picked else c.cons_empty
        return c.battery - cons * manhattan(c.pos, c.stations()[sid]) < 0.5 * c.reserve

    def start_cfp(self, c: CourierAgent, tick: int) -> None:
        bad = {s for s, t in c.known_offline.items() if tick - t < OFFLINE_MEMORY}
        sids = [s for s in c.stations() if s not in bad] or list(c.stations())
        self.seq += 1
        self.conv = f"{c.id}:slot{self.seq}"
        self.phase, self.cfp_t, self.asked, self.proposals = "cfp", tick, set(sids), {}
        content = {**make_claims(c, self.selfish), "pos": list(c.pos)}
        for sid in sids:
            c.send(sid, Perf.CFP, "slot", dict(content), conv=self.conv)
        c.log("slot_cfp", stations=sids, battery=round(c.battery, 1))
        if c.state in WORK_STATES and not c.picked:
            # на время переговоров курьер без посылки направляется к ближайшей станции, а не к месту забора:
            # иначе он может забрать посылку и повезти её на зарядку (курьер с посылкой продолжает доставку)
            c.go_charge(self.choose_station(c))

    def walk_in(self, c: CourierAgent, reason: str) -> None:
        sid = self.choose_station(c)
        self.phase = "walkin"
        c.sim.lab3.inc("l3_fallbacks")
        c.log("slot_fallback", station=sid, reason=reason)
        if sid:
            c.go_charge(sid)

    def next_point(self, c: CourierAgent):
        o = c.current or (c.queue[0] if c.queue else None)
        if o is None:
            return None
        return o.dropoff if (o is c.current and c.picked) else o.pickup

    def decide(self, c: CourierAgent, tick: int) -> None:
        cons = c.cons_loaded if c.picked else c.cons_empty
        nxt = self.next_point(c)
        cands = []
        for sid, prop in self.proposals.items():
            if prop is None:
                continue
            spos = c.stations()[sid]
            d = manhattan(c.pos, spos)
            if c.battery - cons * d < 0.5 * c.reserve:
                continue                                  # не доедет
            after = manhattan(spos, nxt) if nxt is not None else 0
            cands.append((d + float(prop.get("wait", 0)) + after, d, sid))
        if not cands:
            self.walk_in(c, "no-proposals")
            return
        cands.sort()
        _, d_best, best = cands[0]
        noshow = self.selfish and P(c.sim, "selfish_noshow")
        for cost, d, sid in cands:
            if sid == best or noshow:   # эгоист бронирует все предложенные станции
                c.send(sid, Perf.ACCEPT, "slot", {"eta": tick + d}, conv=self.conv)
            else:
                c.send(sid, Perf.REJECT, "slot", {}, conv=self.conv)
        for sid in self.asked - {s for _, _, s in cands}:
            if self.proposals.get(sid) is not None:
                c.send(sid, Perf.REJECT, "slot", {}, conv=self.conv)
        self.phase = "booked"
        c.go_charge(best)
        c.log("slot_chosen", station=best, eta=tick + d_best, options=len(cands))

    # --- цикл ---
    def update(self, c: CourierAgent, tick: int) -> None:
        if self.phase == "cfp":
            if c.state in WORK_STATES and self.critical(c):
                self.walk_in(c, "critical")
            elif len(self.proposals) >= len(self.asked) or tick - self.cfp_t >= P(c.sim, "cfp_timeout"):
                self.decide(c, tick)
            return
        if c.state in WORK_STATES and self.need_charge(c):
            if self.critical(c):
                self.walk_in(c, "critical")
            else:
                self.start_cfp(c, tick)
        elif c.state in (WAITING, CHARGING) and c.battery >= self.full_target(c) - 1e-9:
            self.finish(c, tick)

    def on_arrival(self, c: CourierAgent, tick: int) -> None:
        if self.phase == "cfp":          # доехал до станции раньше итога переговоров — встаёт в очередь без брони
            self.phase = "walkin"
        super().on_arrival(c, tick)

    def finish(self, c: CourierAgent, tick: int) -> None:
        super().finish(c, tick)
        self.phase = None

    def on_refused(self, c: CourierAgent, msg: Message) -> None:
        self.phase = None
        super().on_refused(c, msg)

    def on_message(self, c: CourierAgent, msg: Message) -> bool:
        if msg.protocol == "slot":
            self.on_slot_message(c, msg)
            return True
        return super().on_message(c, msg)

    def on_slot_message(self, c: CourierAgent, msg: Message) -> None:
        if msg.perf in (Perf.PROPOSE, Perf.REFUSE):
            if self.phase == "cfp" and msg.conv == self.conv and msg.sender in self.asked:
                self.proposals[msg.sender] = dict(msg.content) if msg.perf == Perf.PROPOSE else None
        elif msg.perf == Perf.INFORM and msg.content.get("event") == "expired":
            if c.state == TO_STATION and c.station_target == msg.sender and self.phase == "booked":
                c.sim.lab3.inc("l3_rebookings")
                c.log("slot_expired", station=msg.sender)
                self.start_cfp(c, c.sim.tick)


class NegotiatingStationPolicy(PriorityStationPolicy):
    name = "lab3-negotiation"

    def __init__(self, grid_id: str = "grid"):
        super().__init__()
        self.grid_id = grid_id
        self.bookings: dict[str, dict] = {}     # курьер -> {eta, until, claims}
        self.cfp_seen: dict[str, dict] = {}     # курьер -> заявка из последнего CFP
        self.share: int | None = None           # доля лимита мощности (сообщает арбитр)
        self.grid_contention = False
        self.requests: dict[int, int] = {}      # открытые запросы мощности: req -> такт
        self.tokens_free = 0                    # полученные, ещё не отданные курьерам токены
        self.powered: set[str] = set()          # курьеры, заряжающиеся по токену

    def contended(self, st: StationAgent) -> bool:
        return bool(st.queue) or self.grid_contention

    # --- оценка ожидания (расписание станции) ---
    def duration(self, st: StationAgent, claims: dict, contended: bool) -> float:
        target = quota_target(st.sim, claims) if contended else P(st.sim, "full_level") * claims.get("capacity", 100)
        return max(0.0, target - float(claims.get("battery", 0))) / st.rate

    def estimate(self, st: StationAgent, eta: int, claims: dict) -> tuple[float, float]:
        """Когда освободится место для курьера, прибывающего в такт eta: список «освобождений» мест
        (с учётом доли лимита мощности), обслуживание ожидающих и забронировавших раньше eta."""
        now = st.sim.tick
        k = max(1, min(st.slots, self.share or st.slots))
        busy = True   # в оценке длительности сеансов считаем, что действует квота (частичная зарядка)
        free = []
        for cid in st.occupied:
            c, inf = st.sim.agents.get(cid), self.info.get(cid)
            if c is None or inf is None:
                continue
            target = quota_target(st.sim, inf["claims"]) if busy else P(st.sim, "full_level") * c.capacity
            free.append(now + max(0.0, target - c.battery) / st.rate)
        free.sort()
        free = free[:k] + [now] * max(0, k - len(free))
        heapq.heapify(free)
        ahead = [(now, self.duration(st, self.info[cid]["claims"], busy)) for cid in st.queue if cid in self.info]
        ahead += [(b["eta"], self.duration(st, b["claims"], busy)) for cid, b in self.bookings.items()
                  if b["eta"] <= eta]
        for arr, dur in sorted(ahead):
            t0 = max(heapq.heappop(free), arr)
            heapq.heappush(free, t0 + dur)
        start = max(heapq.heappop(free), eta)
        return start - eta, start

    # --- переговоры ---
    def refuse_booking(self, st: StationAgent, cid: str) -> str | None:
        return None

    def on_message(self, st: StationAgent, msg: Message) -> bool:
        if msg.protocol == "slot":
            self.on_slot(st, msg)
            return True
        if msg.protocol == "power":
            self.on_power(st, msg)
            return True
        return super().on_message(st, msg)

    def on_slot(self, st: StationAgent, msg: Message) -> None:
        cid, c = msg.sender, msg.content
        tick = st.sim.tick
        if msg.perf == Perf.CFP:
            reason = "offline" if not st.online else self.refuse_booking(st, cid)
            if reason:
                st.reply(msg, Perf.REFUSE, {"reason": reason})
                if reason == "reputation":
                    st.sim.lab3.inc("l3_booking_refusals")
                return
            self.cfp_seen[cid] = dict(c)
            eta = tick + manhattan(tuple(c.get("pos", st.pos)), st.pos) + st.sim.cfg.msg_latency
            wait, start = self.estimate(st, eta, c)
            st.reply(msg, Perf.PROPOSE, {"eta": eta, "wait": round(wait, 1), "start": round(start, 1)})
        elif msg.perf == Perf.ACCEPT:
            eta = int(c.get("eta", tick))
            self.bookings[cid] = {"eta": eta, "until": eta + P(st.sim, "booking_grace"),
                                  "claims": self.cfp_seen.pop(cid, {}), "conv": msg.conv}
            st.sim.lab3.inc("l3_bookings")
            st.reply(msg, Perf.INFORM, {"booked": True, "until": self.bookings[cid]["until"]})
            st.log("slot_booked", courier=cid, eta=eta)
        elif msg.perf == Perf.REJECT:
            self.cfp_seen.pop(cid, None)
        elif msg.perf == Perf.CANCEL:
            self.bookings.pop(cid, None)

    def on_power(self, st: StationAgent, msg: Message) -> None:
        c = msg.content
        if msg.perf == Perf.AGREE:
            self.requests.pop(c.get("req"), None)
            self.tokens_free += 1
        elif msg.perf == Perf.INFORM:
            if "share" in c:
                self.share = int(c["share"])
            if "contention" in c:
                self.grid_contention = bool(c["contention"])

    # --- брони и прибытие ---
    def on_request(self, st: StationAgent, cid: str, msg: Message) -> None:
        super().on_request(st, cid, msg)
        b = self.bookings.pop(cid, None)
        if b is not None and st.sim.tick <= b["until"]:
            self.info[cid]["booked"] = True

    def expire_bookings(self, st: StationAgent, tick: int) -> None:
        for cid, b in list(self.bookings.items()):
            if tick > b["until"] and not st.courier_here(cid):
                del self.bookings[cid]
                st.sim.lab3.inc("l3_noshows")
                st.send(cid, Perf.INFORM, "slot", {"event": "expired"}, conv=b.get("conv"))
                st.log("slot_noshow", courier=cid, eta=b["eta"])
                self.on_noshow(st, cid)

    def on_noshow(self, st: StationAgent, cid: str) -> None:
        pass

    # --- токены мощности ---
    def release_token(self, st: StationAgent, cid: str | None = None) -> None:
        if cid is not None:
            self.powered.discard(cid)
        st.send(self.grid_id, Perf.INFORM, "power", {"event": "release"})

    def end_session(self, st: StationAgent, cid: str, reason: str) -> None:
        super().end_session(st, cid, reason)
        if cid in self.powered:
            self.release_token(st, cid)

    def expected_soon(self, st: StationAgent, tick: int) -> list[tuple[float, str]]:
        """Забронировавшие, которые прибудут в пределах времени запроса токена (предзапрос мощности по брони).
        Возвращает [(критичность, курьер)]."""
        lead = 2 * st.sim.cfg.msg_latency
        out = []
        for cid, b in sorted(self.bookings.items(), key=lambda kv: kv[1]["eta"]):
            if b["eta"] - lead <= tick and cid not in st.queue and cid not in st.occupied:
                prio = criticality(st.sim, b["claims"], b["eta"], tick, self.trust(cid), True) if b["claims"] else 0.0
                out.append((prio, cid))
        return out

    def admit_ready(self, st: StationAgent, tick: int) -> None:
        # ушедшие с места (физика станции убирает их из occupied) возвращают токен
        for cid in [c for c in self.powered if c not in st.occupied]:
            self.release_token(st, cid)
        while self.tokens_free > 0 and st.online and st.queue and st.free_slots() > 0:
            cid = st.queue[0]
            self.tokens_free -= 1
            self.powered.add(cid)
            self.admit(st, cid)
        # сколько токенов нужно: ожидающим на месте + подъезжающим по брони, но не больше свободных мест
        cands = [(self.priority(st, cid, tick), cid) for cid in st.queue] + self.expected_soon(st, tick)
        wanted = min(len(cands), st.free_slots()) if st.online else 0
        while self.tokens_free > wanted:     # лишний токен — вернуть арбитру
            self.tokens_free -= 1
            self.release_token(st)
        have = self.tokens_free + len(self.requests)
        if wanted > have:
            for prio, cid in cands[have:wanted]:
                req = next(_req_ids)
                self.requests[req] = tick
                st.sim.lab3.inc("l3_power_requests")
                st.send(self.grid_id, Perf.REQUEST, "power", {"req": req, "prio": round(prio, 3)})
        elif wanted < have:
            for req in sorted(self.requests)[max(0, wanted - self.tokens_free):]:
                del self.requests[req]
                st.send(self.grid_id, Perf.CANCEL, "power", {"req": req})

    def step(self, st: StationAgent, tick: int) -> None:
        self.expire_bookings(st, tick)
        super().step(st, tick)
