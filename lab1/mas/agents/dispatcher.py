"""Диспетчер: ведёт книгу заказов, хранит убеждения о курьерах, контролирует исполнение.

Сам способ распределения заказов вынесен в подключаемый механизм (allocator), см. mas/allocation.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..config import COURIER_TYPES
from ..messages import Message, Perf
from ..world import Order
from .base import Agent, Plugin

# Состояния заказа в книге диспетчера. ANNOUNCED — заказ выставлен механизмом распределения
# (CFP, аукцион) и ждёт его решения; сторожевой таймер ядра такие заказы не трогает.
PENDING, ANNOUNCED, ASSIGNING, ASSIGNED, PICKED, DELIVERED = (
    "pending", "announced", "assigning", "assigned", "picked", "delivered")


@dataclass
class OrderRecord:
    order: Order
    status: str = PENDING
    courier: str | None = None
    since: int = 0                       # такт последней смены статуса
    refused_by: set = field(default_factory=set)
    last_refuse: int = -1
    hops: int = 0                        # сколько раз заказ передавался между исполнителями


class DispatcherAgent(Agent):
    role = "dispatcher"

    def __init__(self, aid, sim, client_id: str = "client"):
        super().__init__(aid, sim)
        self.client_id = client_id
        self.records: dict[str, OrderRecord] = {}
        self.beliefs: dict[str, dict] = {}
        self.allocator: Plugin | None = None

    def init_beliefs(self, couriers) -> None:
        """Априорное знание: состав флота и стартовое состояние курьеров."""
        for c in couriers:
            spec = COURIER_TYPES[c.ctype]
            self.beliefs[c.id] = {
                "ctype": c.ctype, "payload": spec["payload"], "capacity": spec["battery"],
                "cons_empty": spec["cons_empty"], "cons_loaded": spec["cons_loaded"],
                "pos": c.pos, "battery": c.battery, "state": "idle", "tasks": [],
                "tail_pos": c.pos, "tail_eta": 0, "tail_battery": c.battery,
                "last_seen": 0, "lost": False,
            }

    def set_allocator(self, allocator: Plugin) -> None:
        if self.allocator is not None:
            self.remove_plugin(self.allocator)
        self.allocator = allocator
        self.add_plugin(allocator, first=True)

    # --- запросы к книге заказов ---
    def pending(self) -> list[OrderRecord]:
        recs = [r for r in self.records.values() if r.status == PENDING]
        return sorted(recs, key=lambda r: (r.order.priority != "urgent", r.order.deadline, r.order.created))

    def couriers_available(self) -> list[str]:
        return [cid for cid, b in self.beliefs.items() if not b.get("lost")]

    # --- действия над заказами (используются механизмами распределения) ---
    def assign(self, rec: OrderRecord, courier_id: str, protocol: str = "assign") -> None:
        """Прямое назначение (REQUEST) — курьер отвечает AGREE/REFUSE."""
        self.set_status(rec, ASSIGNING, courier_id)
        self.send(courier_id, Perf.REQUEST, protocol, rec.order.to_msg(), conv=rec.order.id)

    def confirm_assignment(self, rec: OrderRecord, courier_id: str) -> None:
        """Назначение подтверждено исполнителем (AGREE на REQUEST или принятое предложение)."""
        self.set_status(rec, ASSIGNED, courier_id)
        self.sim.metrics.on_assigned(rec.order.id, courier_id, self.sim.tick)
        self.log("assigned", order=rec.order.id, courier=courier_id)

    def release(self, rec: OrderRecord, reason: str) -> None:
        """Вернуть заказ в очередь на распределение."""
        if rec.status in (ASSIGNED, PICKED) and rec.courier is not None:
            rec.hops += 1
            self.sim.metrics.reassignments += 1
        self.log("released", order=rec.order.id, courier=rec.courier, reason=reason)
        self.set_status(rec, PENDING, None)

    def set_status(self, rec: OrderRecord, status: str, courier: str | None) -> None:
        rec.status, rec.courier, rec.since = status, courier, self.sim.tick

    # --- обработка сообщений ---
    def step(self, tick: int) -> None:
        for m in self.inbox:   # любое сообщение курьера — признак того, что он на связи
            if m.sender in self.beliefs:
                self.beliefs[m.sender]["last_seen"] = tick
                self.beliefs[m.sender]["lost"] = False
        super().step(tick)

    def handle(self, msg: Message) -> None:
        p, perf, c = msg.protocol, msg.perf, msg.content
        if p == "order" and perf == Perf.REQUEST:
            self.on_order(msg)
        elif p == "telemetry" and perf == Perf.INFORM:
            if msg.sender in self.beliefs:
                self.beliefs[msg.sender].update(c)
        elif p == "assign":
            self.on_assign_reply(msg)
        elif p == "order-status" and perf == Perf.INFORM:
            self.on_status(msg)
        else:
            super().handle(msg)

    def on_order(self, msg: Message) -> None:
        order = Order.from_msg(msg.content)
        if order.id in self.records:
            return
        self.records[order.id] = OrderRecord(order=order, since=self.sim.tick)
        self.reply(msg, Perf.AGREE, {"order_id": order.id})

    def on_assign_reply(self, msg: Message) -> None:
        rec = self.records.get(msg.content.get("order_id", msg.conv))
        if rec is None:
            return
        mine = rec.courier == msg.sender
        if msg.perf == Perf.AGREE:
            if mine and rec.status == ASSIGNING:
                self.confirm_assignment(rec, msg.sender)
            elif not mine:
                # опоздавшее согласие: заказ уже снят или отдан другому — отзываем
                self.send(msg.sender, Perf.CANCEL, "assign", {"order_id": rec.order.id}, conv=rec.order.id)
        elif msg.perf == Perf.REFUSE and mine and rec.status == ASSIGNING:
            rec.refused_by.add(msg.sender)
            rec.last_refuse = self.sim.tick
            self.release(rec, "refused:" + str(msg.content.get("reason")))
        elif msg.perf == Perf.FAILURE and mine and rec.status in (ASSIGNING, ASSIGNED):
            self.release(rec, "failure:" + str(msg.content.get("reason")))

    def on_status(self, msg: Message) -> None:
        rec = self.records.get(msg.content.get("order_id"))
        if rec is None:
            return
        status = msg.content.get("status")
        if status == "picked":
            self.set_status(rec, PICKED, msg.sender)
        elif status == "delivered":
            self.set_status(rec, DELIVERED, msg.sender)
            self.send(self.client_id, Perf.INFORM, "order-status",
                      {"order_id": rec.order.id, "status": "delivered", "courier": msg.sender}, conv=rec.order.id)

    # --- контроль исполнения ---
    def act(self, tick: int) -> None:
        cfg = self.sim.cfg
        for cid, b in self.beliefs.items():
            if not b.get("lost") and tick - b["last_seen"] > cfg.watchdog_timeout:
                b["lost"] = True
                self.log("courier_lost", courier=cid)
                for rec in self.records.values():
                    if rec.courier == cid and rec.status in (ASSIGNING, ASSIGNED):
                        self.send(cid, Perf.CANCEL, "assign", {"order_id": rec.order.id}, conv=rec.order.id)
                        self.release(rec, "courier-lost")
        timeout = cfg.assign_timeout + 2 * cfg.msg_latency
        for rec in self.records.values():
            if rec.status == ASSIGNING and tick - rec.since > timeout and rec.courier is not None:
                self.send(rec.courier, Perf.CANCEL, "assign", {"order_id": rec.order.id}, conv=rec.order.id)
                self.release(rec, "no-reply")
