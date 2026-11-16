"""Механизм 1 (режим charging=priority): правила приоритезации на станции и частичная зарядка.

Курьер (Lab3CourierCharging): при прибытии на станцию отправляет REQUEST/charge с заявкой
{заряд, ёмкость, потребность в энергии, ближайший срок, срочность, остаток маршрута};
по INFORM{event: quota} заканчивает зарядку досрочно (честный) или игнорирует (эгоист);
по INFORM{event: evicted} уходит.

Станция (PriorityStationPolicy): очередь упорядочивается по критичности (common.criticality),
свободное место получает самый критичный; если кто-то ждёт, заряжающемуся, набравшему квоту
(common.quota_target), отправляется INFORM{event: quota} — частичная зарядка.
Лимит мощности сети в этом режиме не координируется (станции не знают друг о друге).
"""
from __future__ import annotations

from lab1.mas.agents.courier import CHARGING, IDLE, TO_STATION, WAITING, WORK_STATES, CourierAgent
from lab1.mas.agents.station import StationAgent
from lab1.mas.charging import FIFOStationPolicy, NaiveCourierCharging
from lab1.mas.messages import Message, Perf

from .common import P, criticality, make_claims, quota_target


class Lab3CourierCharging(NaiveCourierCharging):
    """Стратегия зарядки курьера с заявкой о критичности. selfish — эгоистичный курьер."""

    name = "lab3-priority"

    def __init__(self, selfish: bool = False):
        self.selfish = selfish

    def full_target(self, c: CourierAgent) -> float:
        if self.selfish and P(c.sim, "selfish_overstay"):
            return c.capacity            # эгоист заряжается до 100 %
        return P(c.sim, "full_level") * c.capacity

    def update(self, c: CourierAgent, tick: int) -> None:
        if c.state in WORK_STATES and self.need_charge(c):
            sid = self.choose_station(c)
            if sid:
                c.go_charge(sid)
        elif c.state in (WAITING, CHARGING) and c.battery >= self.full_target(c) - 1e-9:
            self.finish(c, tick)

    def on_arrival(self, c: CourierAgent, tick: int) -> None:
        c.state = WAITING
        c.wait_started = tick
        c.send(c.station_target, Perf.REQUEST, "charge", make_claims(c, self.selfish))

    def on_charge_message(self, c: CourierAgent, msg: Message) -> None:
        tick = c.sim.tick
        if msg.perf == Perf.AGREE:
            if c.state != WAITING:
                return
            wait = tick - (c.wait_started if c.wait_started is not None else tick)
            c.sim.metrics.on_charge_wait(c.id, wait)
            c.sim.lab3.on_wait(c.id, wait)
            c.state = CHARGING
            c.log("charge_start", station=msg.sender, battery=round(c.battery, 1))
        elif msg.perf == Perf.REFUSE:
            if c.state not in (WAITING, TO_STATION):
                return
            c.known_offline[msg.sender] = tick
            c.log("charge_refused", station=msg.sender, reason=msg.content.get("reason"))
            self.on_refused(c, msg)
        elif msg.perf == Perf.INFORM:
            event = msg.content.get("event")
            if event == "quota" and c.state == CHARGING:
                if self.selfish and P(c.sim, "selfish_overstay"):
                    c.log("quota_ignored", station=msg.sender, battery=round(c.battery, 1))
                else:
                    c.sim.lab3.inc("l3_quota_stops")
                    c.log("quota_accepted", station=msg.sender, battery=round(c.battery, 1))
                    self.finish(c, tick)
            elif event == "evicted" and c.state in (WAITING, CHARGING):
                c.log("evicted", station=msg.sender, battery=round(c.battery, 1))
                self.finish(c, tick)

    def on_refused(self, c: CourierAgent, msg: Message) -> None:
        sid = self.choose_station(c)
        if sid and sid != msg.sender:
            c.go_charge(sid)
        else:
            c.state = IDLE
            c.station_target, c.wait_started = None, None

    def on_message(self, c: CourierAgent, msg: Message) -> bool:
        if msg.protocol != "charge":
            return False
        if msg.sender == c.station_target:
            self.on_charge_message(c, msg)
        return True


class PriorityStationPolicy(FIFOStationPolicy):
    """Станция с приоритетной очередью по критичности и частичной зарядкой при наличии ожидающих."""

    name = "lab3-priority"

    def __init__(self):
        self.info: dict[str, dict] = {}       # курьер -> {claims, arrived, booked}
        self.quota_sent: dict[str, int] = {}  # курьер -> такт уведомления о квоте

    # --- доверие к заявке (переопределяется в режиме reputation) ---
    def trust(self, cid: str) -> float:
        return 1.0

    def priority(self, st: StationAgent, cid: str, now: int) -> float:
        inf = self.info.get(cid)
        if inf is None:
            return 0.0
        return criticality(st.sim, inf["claims"], inf["arrived"], now, self.trust(cid), inf.get("booked", False))

    def contended(self, st: StationAgent) -> bool:
        return bool(st.queue)

    def quota(self, st: StationAgent, cid: str) -> float:
        """Квота частичной зарядки; заявленная потребность учитывается с доверием trust(cid)."""
        claims = self.info[cid]["claims"]
        t = self.trust(cid)
        if t < 1.0:
            claims = {**claims, "need": t * float(claims.get("need", 0.0))}
        return quota_target(st.sim, claims)

    # --- протокол charge ---
    def on_message(self, st: StationAgent, msg: Message) -> bool:
        if msg.protocol != "charge":
            return False
        cid = msg.sender
        if msg.perf == Perf.REQUEST:
            if not st.online:
                st.reply(msg, Perf.REFUSE, {"reason": "offline"})
            elif not st.courier_here(cid):
                st.reply(msg, Perf.REFUSE, {"reason": "not-at-station"})
            elif cid in st.occupied or cid in st.queue:
                pass
            else:
                self.on_request(st, cid, msg)
        elif msg.perf == Perf.CANCEL or (msg.perf == Perf.INFORM and msg.content.get("event") == "done"):
            self.end_session(st, cid, reason="done")
        return True

    def on_request(self, st: StationAgent, cid: str, msg: Message) -> None:
        self.info[cid] = {"claims": dict(msg.content), "arrived": st.sim.tick, "booked": False}
        st.queue.append(cid)
        st.reply(msg, Perf.INFORM, {"queued": len(st.queue)})
        st.log("queued", courier=cid, position=len(st.queue),
               priority=round(self.priority(st, cid, st.sim.tick), 2))

    def end_session(self, st: StationAgent, cid: str, reason: str) -> None:
        st.occupied.pop(cid, None)
        if cid in st.queue:
            st.queue.remove(cid)
        self.info.pop(cid, None)
        self.quota_sent.pop(cid, None)

    def admit(self, st: StationAgent, cid: str, msg: Message | None = None) -> None:
        if cid in st.queue:
            st.queue.remove(cid)
        st.occupied[cid] = st.sim.tick
        st.send(cid, Perf.AGREE, "charge", {"granted": True})
        st.log("slot_granted", courier=cid, priority=round(self.priority(st, cid, st.sim.tick), 2))
        self.on_admitted(st, cid)

    def on_admitted(self, st: StationAgent, cid: str) -> None:
        pass

    # --- такт станции ---
    def purge(self, st: StationAgent) -> None:
        for cid in [c for c in st.queue if not st.courier_here(c)]:
            st.queue.remove(cid)
            self.info.pop(cid, None)
        for cid in [c for c in self.info if c not in st.queue and c not in st.occupied]:
            self.info.pop(cid, None)
            self.quota_sent.pop(cid, None)

    def order_queue(self, st: StationAgent, tick: int) -> None:
        st.queue.sort(key=lambda cid: -self.priority(st, cid, tick))

    def admit_ready(self, st: StationAgent, tick: int) -> None:
        while st.online and st.queue and st.free_slots() > 0:
            self.admit(st, st.queue[0])

    def enforce_quota(self, st: StationAgent, tick: int) -> None:
        if not self.contended(st):
            return
        for cid in list(st.occupied):
            inf = self.info.get(cid)
            c = st.sim.agents.get(cid)
            if inf is None or c is None or cid in self.quota_sent:
                continue
            # заряд подключённой батареи станция измеряет сама (датчик зарядного места)
            if c.battery >= self.quota(st, cid) - 1e-9:
                self.quota_sent[cid] = tick
                st.send(cid, Perf.INFORM, "charge", {"event": "quota", "battery": round(c.battery, 1)})
                st.log("quota_reached", courier=cid, battery=round(c.battery, 1))
        self.after_quota(st, tick)

    def after_quota(self, st: StationAgent, tick: int) -> None:
        pass

    def step(self, st: StationAgent, tick: int) -> None:
        self.purge(st)
        self.order_queue(st, tick)
        self.admit_ready(st, tick)
        self.enforce_quota(st, tick)
