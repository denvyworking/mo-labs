"""Установка режимов ЛР3 (категория charging): какие стратегии, политики и агенты подключаются."""
from __future__ import annotations

from .common import Lab3State
from .grid import GridArbiterAgent
from .negotiation import NegotiatingCourierCharging, NegotiatingStationPolicy
from .priority import Lab3CourierCharging, PriorityStationPolicy
from .reputation import AuditResponder, ReputationAgent, ReputationStationPolicy

GRID_ID, REPUTATION_ID = "grid", "reputation"


def _setup(sim) -> set[str]:
    sim.lab3 = Lab3State(sim)
    return sim.lab3.selfish


def install_priority(sim) -> None:
    selfish = _setup(sim)
    for c in sim.couriers:
        c.set_charging(Lab3CourierCharging(selfish=c.id in selfish))
    for s in sim.stations:
        s.set_policy(PriorityStationPolicy())


def install_negotiation(sim) -> None:
    selfish = _setup(sim)
    sim.add_agent(GridArbiterAgent(GRID_ID, sim))
    for c in sim.couriers:
        c.set_charging(NegotiatingCourierCharging(selfish=c.id in selfish))
    for s in sim.stations:
        s.set_policy(NegotiatingStationPolicy(GRID_ID))


def install_reputation(sim) -> None:
    selfish = _setup(sim)
    sim.add_agent(GridArbiterAgent(GRID_ID, sim))
    sim.add_agent(ReputationAgent(REPUTATION_ID, sim))
    sim.dispatcher.add_plugin(AuditResponder(), first=True)
    for c in sim.couriers:
        c.set_charging(NegotiatingCourierCharging(selfish=c.id in selfish))
    for s in sim.stations:
        s.set_policy(ReputationStationPolicy(GRID_ID, REPUTATION_ID))


def demo_selfish(cfg) -> None:
    """Пресет для веб-демо и CLI (там нельзя задать params): 30 % эгоистичных курьеров."""
    cfg.params.setdefault("selfish_share", 0.3)
