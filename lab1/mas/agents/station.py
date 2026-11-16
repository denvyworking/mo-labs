"""Зарядная станция: ограниченное число мест, очередь, политика допуска (подключаемая)."""
from __future__ import annotations

from ..world import Pos
from .base import Agent, Plugin


class StationAgent(Agent):
    role = "station"

    def __init__(self, aid, sim, pos: Pos, slots: int):
        super().__init__(aid, sim)
        self.pos = pos
        self.slots = slots
        self.rate = sim.cfg.charge_rate
        self.occupied: dict[str, int] = {}   # курьер -> такт начала зарядки
        self.queue: list[str] = []
        self.online = True                   # внешнее средство доступно
        self.policy: Plugin | None = None

    def set_policy(self, policy: Plugin) -> None:
        if self.policy is not None:
            self.remove_plugin(self.policy)
        self.policy = policy
        self.add_plugin(policy, first=True)

    def free_slots(self) -> int:
        return self.slots - len(self.occupied)

    def courier_here(self, cid: str) -> bool:
        c = self.sim.agents.get(cid)
        return c is not None and getattr(c, "pos", None) == self.pos

    def act(self, tick: int) -> None:
        # Физика: заряжаем тех, кто стоит на местах; ушедшие освобождают место
        for cid in list(self.occupied):
            c = self.sim.agents.get(cid)
            if c is None or c.pos != self.pos:
                self.occupied.pop(cid)
                self.log("slot_freed", courier=cid, reason="left")
                continue
            if self.online:
                c.battery = min(c.capacity, c.battery + self.rate)
