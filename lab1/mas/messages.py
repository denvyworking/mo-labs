"""Сообщения (подмножество FIPA-ACL) и шина — единственный канал связи между агентами."""
from __future__ import annotations

import heapq
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .sim import Simulation


class Perf(str, Enum):
    REQUEST = "request"
    AGREE = "agree"
    REFUSE = "refuse"
    INFORM = "inform"
    FAILURE = "failure"
    CANCEL = "cancel"
    CFP = "cfp"
    PROPOSE = "propose"
    ACCEPT = "accept-proposal"
    REJECT = "reject-proposal"
    QUERY = "query"


@dataclass
class Message:
    sender: str             # заявленный отправитель (может быть подделан)
    receiver: str
    perf: Perf
    protocol: str
    content: dict = field(default_factory=dict)
    conv: str | None = None
    reply_to: int | None = None
    id: int = 0
    t: int = 0
    origin: str = ""        # фактический отправитель; знает только шина (для аудита и аутентификации)
    delay: int = 0          # дополнительная задержка, вносимая сбоями канала

    def size(self) -> int:
        return len(json.dumps(self.content, ensure_ascii=False, default=str)) + 32

    def to_dict(self) -> dict:
        return {
            "id": self.id, "from": self.sender, "to": self.receiver, "perf": self.perf.value,
            "proto": self.protocol, "conv": self.conv, "reply_to": self.reply_to,
            "origin": self.origin, "content": self.content,
        }

    def short(self) -> str:
        return f"#{self.id} {self.sender}->{self.receiver} {self.perf.value}/{self.protocol}"


# Промежуточный обработчик шины: вернуть сообщение (возможно изменённое), список сообщений или None (заблокировать).
Middleware = Callable[[Message, "Simulation"], "Message | list[Message] | None"]


class Bus:
    def __init__(self, sim: "Simulation", latency: int = 1):
        self.sim = sim
        self.latency = max(1, latency)
        self.middlewares: list[Middleware] = []
        self._heap: list[tuple[int, int, Message]] = []
        self._seq = 0

    def _next_id(self) -> int:
        self._seq += 1
        return self._seq

    def send(self, msg: Message, origin: str) -> Message:
        msg.id = self._next_id()
        msg.t = self.sim.tick
        msg.origin = origin
        out = [msg]
        for mw in self.middlewares:
            nxt: list[Message] = []
            for m in out:
                r = mw(m, self.sim)
                if r is None:
                    self.sim.metrics.on_message_blocked(m)
                    self.sim.trace.event("msg_blocked", **m.to_dict())
                    continue
                nxt.extend(r if isinstance(r, list) else [r])
            out = nxt
        for m in out:
            if m is not msg and (m.id == 0 or m.id == msg.id):
                m.id = self._next_id()
                m.t = self.sim.tick
                m.origin = m.origin or "bus"
            self.sim.metrics.on_message(m)
            if self.sim.trace.messages:
                self.sim.trace.event("msg", **m.to_dict())
            heapq.heappush(self._heap, (self.sim.tick + self.latency + m.delay, m.id, m))
        return msg

    def deliver_due(self, tick: int) -> None:
        while self._heap and self._heap[0][0] <= tick:
            _, _, m = heapq.heappop(self._heap)
            agent = self.sim.agents.get(m.receiver)
            if agent is None or not agent.reachable:
                self.sim.metrics.on_message_dropped(m)
                self.sim.trace.event("msg_dropped", id=m.id, to=m.receiver, proto=m.protocol)
                continue
            agent.deliver(m)

    def pending(self) -> int:
        return len(self._heap)
