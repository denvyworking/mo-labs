"""Симуляция с дискретным временем: построение агентов, тактовый цикл, кадры для демонстрации."""
from __future__ import annotations

import random
from typing import Callable

from . import registry
from .agents.base import Agent
from .agents.client import ClientAgent
from .agents.courier import STATES, CourierAgent
from .agents.dispatcher import DispatcherAgent
from .agents.station import StationAgent
from .config import SimConfig
from .messages import Bus
from .metrics import Metrics
from .trace import Trace
from .world import World, generate_orders

Hook = Callable[["Simulation", int], None]


class Simulation:
    def __init__(self, cfg: SimConfig | None = None, **overrides):
        cfg = (cfg or SimConfig()).replace(**overrides)   # копия: режимы могут править параметры
        registry.configure_all(cfg)
        self.cfg = cfg
        self.tick = 0
        self.rng = random.Random(cfg.seed * 7919 + 17)    # для стохастических решений агентов и сбоев
        self.trace = Trace(self, messages=cfg.trace_messages)
        self.metrics = Metrics(self)
        self.world = World(cfg, self)
        self.bus = Bus(self, latency=cfg.msg_latency)
        self.agents: dict[str, Agent] = {}
        self.hooks_pre: list[Hook] = []    # вызываются в начале такта (до доставки сообщений)
        self.hooks_post: list[Hook] = []   # вызываются в конце такта
        self.frames: list[dict] = []
        self.orders = generate_orders(cfg, self.world, random.Random(cfg.seed))
        self._build()
        registry.install_all(self)

    # --- построение ---
    def _build(self) -> None:
        cfg = self.cfg
        self.add_agent(ClientAgent("client", self, self.orders))
        self.add_agent(DispatcherAgent("dispatcher", self))
        for sid, pos in self.world.station_pos.items():
            self.add_agent(StationAgent(sid, self, pos, cfg.station_slots))
        hubs = self.world.hubs
        for i in range(cfg.n_couriers):
            ctype = "heavy" if i < cfg.heavy_couriers else "light"
            self.add_agent(CourierAgent(f"courier-{i + 1}", self, ctype, hubs[i % len(hubs)]))
        self.dispatcher.init_beliefs(self.couriers)

    def add_agent(self, agent: Agent) -> Agent:
        self.agents[agent.id] = agent
        return agent

    def replace_agent(self, agent: Agent) -> Agent:
        """Заменить агента с тем же id (например, на злонамеренную версию); порядок обхода сохраняется."""
        if agent.id not in self.agents:
            raise KeyError(agent.id)
        self.agents[agent.id] = agent
        return agent

    # --- доступ ---
    def agents_by_role(self, role: str) -> list[Agent]:
        return [a for a in self.agents.values() if a.role == role]

    def role_of(self, aid: str) -> str:
        a = self.agents.get(aid)
        return a.role if a is not None else "unknown"

    @property
    def dispatcher(self) -> DispatcherAgent:
        return self.agents["dispatcher"]

    @property
    def client(self) -> ClientAgent:
        return self.agents["client"]

    @property
    def couriers(self) -> list[CourierAgent]:
        return self.agents_by_role("courier")

    @property
    def stations(self) -> list[StationAgent]:
        return self.agents_by_role("station")

    # --- цикл ---
    def done(self) -> bool:
        return self.client.all_submitted and all(s == "delivered" for s in self.world.parcels.values())

    def step(self) -> None:
        t = self.tick
        for h in self.hooks_pre:
            h(self, t)
        self.bus.deliver_due(t)
        for a in list(self.agents.values()):
            if a.active:
                a.step(t)
        self.metrics.on_tick(t)
        for h in self.hooks_post:
            h(self, t)
        if self.cfg.record_frames:
            self.frames.append(self.frame())

    def run(self) -> dict:
        self.trace.event("start", config=self.cfg.to_dict())
        for t in range(self.cfg.max_ticks):
            self.tick = t
            self.step()
            if self.done():
                break
        self.trace.event("end")
        return self.metrics.summary()

    # --- кадры для веб-демонстрации ---
    def frame(self) -> dict:
        state_code = {s: i for i, s in enumerate(STATES)}
        cidx = {c.id: i for i, c in enumerate(self.couriers)}
        parcels = []
        for oid, st in self.world.parcels.items():
            if st == "delivered":
                continue
            o = self.world.orders[oid]
            carrier = cidx.get(st.split(":", 1)[1], -1) if st.startswith("carried:") else -1
            parcels.append([oid, *o.pickup, *o.dropoff, carrier, 1 if o.priority == "urgent" else 0])
        return {
            "t": self.tick,
            "c": [[*c.pos, state_code.get(c.state, 0), round(100 * c.battery / c.capacity), len(c.tasks())]
                  for c in self.couriers],
            "s": [[len(s.occupied), len(s.queue), 1 if s.online else 0] for s in self.stations],
            "p": parcels,
        }

    def static_info(self) -> dict:
        return {
            "width": self.world.width, "height": self.world.height, "hubs": self.world.hubs,
            "stations": [{"id": s.id, "pos": s.pos, "slots": s.slots} for s in self.stations],
            "couriers": [{"id": c.id, "type": c.ctype} for c in self.couriers],
            "states": list(STATES),
        }


def run_simulation(cfg: SimConfig | None = None, **overrides) -> tuple[dict, Simulation]:
    sim = Simulation(cfg, **overrides)
    return sim.run(), sim
