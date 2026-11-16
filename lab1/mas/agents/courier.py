"""Курьер: автономный робот-доставщик.

Знает своё положение, заряд, очередь принятых заказов и карту станций. Сам решает, как ехать,
когда заряжаться (через подключаемую стратегию зарядки) и может ли взять заказ.
"""
from __future__ import annotations

from ..config import COURIER_TYPES
from ..messages import Message, Perf
from ..world import Order, Pos, manhattan, step_towards
from .base import Agent, Plugin

IDLE, TO_PICKUP, TO_DROPOFF = "idle", "to_pickup", "to_dropoff"
TO_STATION, WAITING, CHARGING, STRANDED = "to_station", "waiting_charge", "charging", "stranded"
WORK_STATES = (IDLE, TO_PICKUP, TO_DROPOFF)
STATES = (IDLE, TO_PICKUP, TO_DROPOFF, TO_STATION, WAITING, CHARGING, STRANDED)


class CourierAgent(Agent):
    role = "courier"

    def __init__(self, aid, sim, ctype: str, pos: Pos, dispatcher_id: str = "dispatcher"):
        super().__init__(aid, sim)
        spec = COURIER_TYPES[ctype]
        self.ctype = ctype
        self.payload = spec["payload"]
        self.capacity = spec["battery"]
        self.cons_empty = spec["cons_empty"]
        self.cons_loaded = spec["cons_loaded"]
        self.pos: Pos = pos
        self.battery: float = self.capacity
        self.state = IDLE
        self.queue: list[Order] = []          # принятые, ещё не начатые заказы
        self.current: Order | None = None     # заказ в работе
        self.picked = False                   # посылка текущего заказа на борту
        self.dispatcher_id = dispatcher_id
        self.station_target: str | None = None
        self.wait_started: int | None = None
        self.known_offline: dict[str, int] = {}   # станция -> такт, когда узнали о недоступности
        self.charging: Plugin | None = None
        self.tow_left = 0
        self.last_telemetry = -10**9

    # --- знания и оценки ---
    @property
    def reachable(self) -> bool:
        return self.active and self.state != STRANDED

    @property
    def reserve(self) -> float:
        return self.sim.cfg.battery_reserve * self.capacity

    def stations(self) -> dict[str, Pos]:
        return self.sim.world.station_pos

    def nearest_station(self, pos: Pos | None = None, exclude=()) -> str | None:
        pos = self.pos if pos is None else pos
        cands = [(manhattan(pos, p), sid) for sid, p in self.stations().items() if sid not in exclude]
        return min(cands)[1] if cands else None

    def tasks(self) -> list[Order]:
        return ([self.current] if self.current else []) + list(self.queue)

    def route_remaining(self) -> tuple[int, float, Pos]:
        """Длина и энергия оставшегося маршрута по всем принятым заказам; где курьер окажется в конце."""
        pos, dist, energy = self.pos, 0, 0.0
        for i, o in enumerate(self.tasks()):
            if not (i == 0 and self.picked):
                d = manhattan(pos, o.pickup)
                dist, energy, pos = dist + d, energy + d * self.cons_empty, o.pickup
            d = manhattan(pos, o.dropoff)
            dist, energy, pos = dist + d, energy + d * self.cons_loaded, o.dropoff
        return dist, energy, pos

    def can_take(self, order: Order) -> tuple[bool, str]:
        if order.weight > self.payload:
            return False, "payload"
        # Плечо «станция -> забор -> доставка -> станция» должно укладываться в батарею
        s1 = self.stations()[self.nearest_station(order.pickup)]
        s2 = self.stations()[self.nearest_station(order.dropoff)]
        need = (self.cons_empty * manhattan(s1, order.pickup) + self.cons_loaded * order.trip()
                + self.cons_empty * manhattan(order.dropoff, s2))
        if need > self.capacity - self.reserve:
            return False, "range"
        return True, "ok"

    def estimate(self, order: Order) -> dict:
        """Оценка добавления заказа в конец очереди: доп. путь, ожидаемое время завершения, энергия."""
        dist, energy, tail = self.route_remaining()
        d1, trip = manhattan(tail, order.pickup), order.trip()
        e_new = d1 * self.cons_empty + trip * self.cons_loaded
        tail_batt = self.battery - energy
        charge_penalty = 0.0
        back = self.cons_empty * manhattan(order.dropoff, self.stations()[self.nearest_station(order.dropoff)])
        if tail_batt - e_new - back < self.reserve:
            sp = self.stations()[self.nearest_station(tail)]
            detour = manhattan(tail, sp) + manhattan(sp, order.pickup) - d1
            charge_penalty = detour + (self.capacity - max(0.0, tail_batt)) / self.sim.cfg.charge_rate
        busy = 0
        if self.state in (TO_STATION, WAITING, CHARGING):
            busy = (self.capacity - self.battery) / self.sim.cfg.charge_rate
        extra = d1 + trip + charge_penalty
        return {"extra": round(extra, 2), "eta": round(self.sim.tick + busy + dist + extra, 2),
                "energy": round(e_new, 2), "tail_battery": round(tail_batt, 2), "queue": len(self.tasks())}

    # --- приём и снятие заказов ---
    def accept_task(self, order: Order) -> None:
        if any(o.id == order.id for o in self.tasks()):
            return
        self.queue.append(order)
        self.log("task_accepted", order=order.id)

    def drop_task(self, oid: str) -> bool:
        """Снять ещё не забранный заказ. Возвращает False, если посылка уже на борту."""
        if self.current and self.current.id == oid:
            if self.picked:
                return False
            self.current = None
            if self.state in (TO_PICKUP, TO_DROPOFF):
                self.state = IDLE
            return True
        before = len(self.queue)
        self.queue = [o for o in self.queue if o.id != oid]
        return len(self.queue) != before

    def handle(self, msg: Message) -> None:
        if msg.protocol == "assign" and msg.perf == Perf.REQUEST:
            order = Order.from_msg(msg.content)
            ok, reason = self.can_take(order)
            if ok:
                self.accept_task(order)
                self.reply(msg, Perf.AGREE, {"order_id": order.id})
            else:
                self.reply(msg, Perf.REFUSE, {"order_id": order.id, "reason": reason})
        elif msg.protocol == "assign" and msg.perf == Perf.CANCEL:
            oid = msg.content.get("order_id")
            if self.drop_task(oid):
                self.log("task_cancelled", order=oid, by=msg.sender)
        else:
            super().handle(msg)

    # --- зарядка ---
    def set_charging(self, strategy: Plugin) -> None:
        if self.charging is not None:
            self.remove_plugin(self.charging)
        self.charging = strategy
        self.add_plugin(strategy)

    def go_charge(self, sid: str) -> None:
        self.station_target = sid
        self.state = TO_STATION
        self.log("go_charge", station=sid, battery=round(self.battery, 1))

    # --- движение и работа ---
    def move_to(self, target: Pos) -> bool:
        """Шаг к цели. True — курьер на месте."""
        if self.pos == target:
            return True
        cost = self.cons_loaded if self.picked else self.cons_empty
        if self.battery + 1e-9 < cost:
            self.strand()
            return False
        self.pos = step_towards(self.pos, target)
        self.battery -= cost
        self.sim.metrics.on_move(self.id, cost)
        return self.pos == target

    def strand(self) -> None:
        self.state = STRANDED
        self.tow_left = self.sim.cfg.tow_delay
        self.sim.metrics.stranded += 1
        self.log("stranded", pos=list(self.pos), battery=round(self.battery, 2))

    def recover(self) -> None:
        """Эвакуация на ближайшую станцию с половинным зарядом; незабранные заказы возвращаются диспетчеру."""
        self.pos = self.stations()[self.nearest_station()]
        self.battery = 0.5 * self.capacity
        self.state = IDLE
        released = [o for o in self.queue] + ([self.current] if self.current and not self.picked else [])
        self.queue = []
        if self.current and not self.picked:
            self.current = None
        for o in released:
            self.send(self.dispatcher_id, Perf.FAILURE, "assign", {"order_id": o.id, "reason": "released"}, conv=o.id)
        self.log("recovered", pos=list(self.pos), released=[o.id for o in released])
        self.send_telemetry()

    def notify(self, perf: Perf, protocol: str, content: dict) -> None:
        self.send(self.dispatcher_id, perf, protocol, content, conv=content.get("order_id"))

    def work(self) -> None:
        if self.current is None:
            if not self.queue:
                self.state = IDLE
                return
            self.current, self.picked = self.queue.pop(0), False
        o = self.current
        if not self.picked:
            self.state = TO_PICKUP
            if self.move_to(o.pickup):
                ok, reason = self.sim.world.try_pickup(o.id, self.id, self.pos)
                if ok:
                    self.picked, self.state = True, TO_DROPOFF
                    self.log("picked", order=o.id)
                    self.notify(Perf.INFORM, "order-status", {"order_id": o.id, "status": "picked"})
                else:
                    self.sim.metrics.failed_pickups += 1
                    self.log("pickup_failed", order=o.id, reason=reason)
                    self.notify(Perf.FAILURE, "assign", {"order_id": o.id, "reason": reason})
                    self.current, self.state = None, IDLE
        else:
            self.state = TO_DROPOFF
            if self.move_to(o.dropoff):
                ok, reason = self.sim.world.try_deliver(o.id, self.id, self.pos)
                self.log("delivered" if ok else "delivery_failed", order=o.id, reason=reason)
                if ok:
                    self.notify(Perf.INFORM, "order-status", {"order_id": o.id, "status": "delivered"})
                self.current, self.picked, self.state = None, False, IDLE

    def telemetry(self) -> dict:
        dist, energy, tail = self.route_remaining()
        return {"pos": list(self.pos), "battery": round(self.battery, 2), "state": self.state,
                "tasks": [o.id for o in self.tasks()], "tail_pos": list(tail),
                "tail_eta": self.sim.tick + dist, "tail_battery": round(self.battery - energy, 2),
                "carrying": self.current.id if self.current and self.picked else None}

    def send_telemetry(self) -> None:
        self.last_telemetry = self.sim.tick
        self.send(self.dispatcher_id, Perf.INFORM, "telemetry", self.telemetry())

    def act(self, tick: int) -> None:
        if self.state == STRANDED:
            self.tow_left -= 1
            if self.tow_left <= 0:
                self.recover()
            return
        if self.charging is not None:
            self.charging.update(self, tick)
        if self.state == TO_STATION:
            if self.move_to(self.stations()[self.station_target]) and self.state == TO_STATION:
                self.charging.on_arrival(self, tick)
        elif self.state in WORK_STATES:
            self.work()
        if self.state != STRANDED and tick - self.last_telemetry >= self.sim.cfg.telemetry_period:
            self.send_telemetry()
