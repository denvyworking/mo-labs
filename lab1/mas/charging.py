"""Зарядка. В ядре — базовый режим без переговоров (это «до» для ЛР3):
курьер едет на ближайшую станцию, когда энергии не хватает на текущий заказ, станция обслуживает FIFO,
курьер заряжается почти до полного.

Стратегия курьера — плагин с методами update() (решение «пора ли заряжаться», окончание зарядки)
и on_arrival() (прибыл на станцию). Политика станции — плагин станции, обрабатывает протокол "charge".
"""
from __future__ import annotations

from .agents.base import Plugin
from .agents.courier import CHARGING, IDLE, TO_STATION, WAITING, WORK_STATES, CourierAgent
from .agents.station import StationAgent
from .messages import Message, Perf
from .world import manhattan

OFFLINE_MEMORY = 50   # сколько тактов курьер помнит, что станция недоступна


class NaiveCourierCharging(Plugin):
    name = "naive"
    target_level = 0.95     # до какого уровня заряжаться
    idle_threshold = 0.4    # свободный курьер заряжается, если заряд ниже этой доли

    def energy_needed(self, c: CourierAgent) -> float:
        """Энергия на текущий (или следующий) заказ плюс путь от точки доставки до ближайшей станции."""
        o = c.current or (c.queue[0] if c.queue else None)
        if o is None:
            return 0.0
        e, pos = 0.0, c.pos
        if not (o is c.current and c.picked):
            e += c.cons_empty * manhattan(pos, o.pickup)
            pos = o.pickup
        e += c.cons_loaded * manhattan(pos, o.dropoff)
        e += c.cons_empty * manhattan(o.dropoff, c.stations()[c.nearest_station(o.dropoff)])
        return e

    def need_charge(self, c: CourierAgent) -> bool:
        if c.battery >= self.target_level * c.capacity:
            return False
        if c.current is None and not c.queue:
            return c.battery < self.idle_threshold * c.capacity
        return c.battery - self.energy_needed(c) < c.reserve

    def choose_station(self, c: CourierAgent) -> str | None:
        tick = c.sim.tick
        bad = {s for s, t in c.known_offline.items() if tick - t < OFFLINE_MEMORY}
        return c.nearest_station(exclude=bad) or c.nearest_station()

    def update(self, c: CourierAgent, tick: int) -> None:
        if c.state in WORK_STATES and self.need_charge(c):
            sid = self.choose_station(c)
            if sid:
                c.go_charge(sid)
        elif c.state in (WAITING, CHARGING) and c.battery >= self.target_level * c.capacity:
            self.finish(c, tick)

    def on_arrival(self, c: CourierAgent, tick: int) -> None:
        c.state = WAITING
        c.wait_started = tick
        c.send(c.station_target, Perf.REQUEST, "charge",
               {"battery": round(c.battery, 2), "capacity": c.capacity})

    def finish(self, c: CourierAgent, tick: int) -> None:
        c.send(c.station_target, Perf.INFORM, "charge", {"event": "done", "battery": round(c.battery, 2)})
        c.log("charge_done", station=c.station_target, battery=round(c.battery, 1))
        c.station_target, c.wait_started, c.state = None, None, IDLE

    def on_message(self, c: CourierAgent, msg: Message) -> bool:
        if msg.protocol != "charge":
            return False
        if msg.sender != c.station_target or c.state not in (WAITING, TO_STATION):
            return True
        if msg.perf == Perf.AGREE:
            c.sim.metrics.on_charge_wait(c.id, c.sim.tick - (c.wait_started or c.sim.tick))
            c.state = CHARGING
            c.log("charge_start", station=msg.sender, battery=round(c.battery, 1))
        elif msg.perf == Perf.REFUSE:
            c.known_offline[msg.sender] = c.sim.tick
            c.log("charge_refused", station=msg.sender, reason=msg.content.get("reason"))
            sid = self.choose_station(c)
            if sid and sid != msg.sender:
                c.go_charge(sid)
            else:
                c.state = IDLE
        return True


class FIFOStationPolicy(Plugin):
    """Станция без приоритетов: свободное место — сразу, иначе очередь в порядке прибытия."""

    name = "fifo"

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
            elif st.free_slots() > 0:
                self.admit(st, cid, msg)
            else:
                st.queue.append(cid)
                st.reply(msg, Perf.INFORM, {"queued": len(st.queue)})
                st.log("queued", courier=cid, position=len(st.queue))
        elif msg.perf in (Perf.INFORM, Perf.CANCEL):
            if msg.perf == Perf.CANCEL or msg.content.get("event") == "done":
                st.occupied.pop(cid, None)
                if cid in st.queue:
                    st.queue.remove(cid)
        return True

    def admit(self, st: StationAgent, cid: str, msg: Message | None = None) -> None:
        st.occupied[cid] = st.sim.tick
        content = {"granted": True}
        if msg is not None:
            st.reply(msg, Perf.AGREE, content)
        else:
            st.send(cid, Perf.AGREE, "charge", content)
        st.log("slot_granted", courier=cid)

    def step(self, st: StationAgent, tick: int) -> None:
        st.queue = [cid for cid in st.queue if st.courier_here(cid)]
        while st.online and st.queue and st.free_slots() > 0:
            self.admit(st, st.queue.pop(0))
