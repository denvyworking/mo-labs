"""Клиент (шлюз заказов): публикует заказы в момент их появления и получает подтверждения доставки."""
from __future__ import annotations

from ..messages import Message, Perf
from ..world import Order
from .base import Agent


class ClientAgent(Agent):
    role = "client"

    def __init__(self, aid, sim, orders: list[Order], dispatcher_id: str = "dispatcher"):
        super().__init__(aid, sim)
        self.schedule = sorted(orders, key=lambda o: (o.created, o.id))
        self.next_idx = 0
        self.dispatcher_id = dispatcher_id
        self.confirmed: dict[str, int] = {}

    @property
    def all_submitted(self) -> bool:
        return self.next_idx >= len(self.schedule)

    def act(self, tick: int) -> None:
        while not self.all_submitted and self.schedule[self.next_idx].created <= tick:
            o = self.schedule[self.next_idx]
            self.next_idx += 1
            self.submit(o)

    def submit(self, o: Order) -> None:
        self.sim.world.publish(o)
        self.sim.metrics.on_order_created(o)
        self.log("order_created", order=o.id, priority=o.priority, weight=o.weight)
        self.send(self.dispatcher_id, Perf.REQUEST, "order", o.to_msg(), conv=o.id)

    def handle(self, msg: Message) -> None:
        if msg.protocol == "order-status" and msg.content.get("status") == "delivered":
            self.confirmed[msg.content["order_id"]] = self.sim.tick
        elif msg.protocol == "order":
            pass   # подтверждение приёма заказа диспетчером
        else:
            super().handle(msg)
