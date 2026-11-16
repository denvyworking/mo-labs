"""Базовый агент и подключаемое поведение (плагин).

Цикл агента за такт: получить сообщения -> предложить каждое плагинам (первый, вернувший True,
поглощает сообщение) -> базовая обработка handle() -> step() плагинов -> собственное действие act().
Механизмы из ЛР2–ЛР4 подключаются как плагины, не меняя ядра.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..messages import Message, Perf

if TYPE_CHECKING:
    from ..sim import Simulation


class Plugin:
    name = "plugin"

    def attach(self, agent: "Agent") -> None:
        self.agent = agent

    def on_message(self, agent: "Agent", msg: Message) -> bool:
        return False

    def step(self, agent: "Agent", tick: int) -> None:
        pass


class Agent:
    role = "agent"

    def __init__(self, aid: str, sim: "Simulation"):
        self.id = aid
        self.sim = sim
        self.inbox: list[Message] = []
        self.plugins: list[Plugin] = []
        self.active = True

    # --- плагины ---
    def add_plugin(self, plugin: Plugin, first: bool = False) -> Plugin:
        if first:
            self.plugins.insert(0, plugin)
        else:
            self.plugins.append(plugin)
        plugin.attach(self)
        return plugin

    def remove_plugin(self, plugin: Plugin) -> None:
        if plugin in self.plugins:
            self.plugins.remove(plugin)

    # --- связь ---
    @property
    def reachable(self) -> bool:
        return self.active

    def deliver(self, msg: Message) -> None:
        self.inbox.append(msg)

    def send(self, to: str, perf: Perf, protocol: str, content: dict | None = None,
             conv: str | None = None, reply_to: int | None = None) -> Message:
        msg = Message(sender=self.id, receiver=to, perf=perf, protocol=protocol,
                      content=content or {}, conv=conv, reply_to=reply_to)
        return self.sim.bus.send(msg, origin=self.id)

    def reply(self, msg: Message, perf: Perf, content: dict | None = None) -> Message:
        return self.send(msg.sender, perf, msg.protocol, content, conv=msg.conv, reply_to=msg.id)

    def broadcast(self, role: str, perf: Perf, protocol: str, content: dict | None = None,
                  conv: str | None = None) -> list[Message]:
        return [self.send(a.id, perf, protocol, dict(content or {}), conv=conv)
                for a in self.sim.agents_by_role(role) if a.id != self.id]

    # --- цикл ---
    def step(self, tick: int) -> None:
        msgs, self.inbox = self.inbox, []
        for m in msgs:
            if not any(p.on_message(self, m) for p in list(self.plugins)):
                self.handle(m)
        for p in list(self.plugins):
            p.step(self, tick)
        self.act(tick)

    def handle(self, msg: Message) -> None:
        self.log("unhandled", msg=msg.short())

    def act(self, tick: int) -> None:
        pass

    def log(self, kind: str, **data) -> None:
        self.sim.trace.event(kind, agent=self.id, **data)
