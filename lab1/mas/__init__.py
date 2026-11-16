"""Ядро МАС «Городская доставка автономными курьерами» (ЛР1). Механизмы ЛР2–ЛР4 подключаются через registry."""
from . import registry, roles
from .config import COURIER_TYPES, SimConfig
from .sim import Simulation, run_simulation

__all__ = ["COURIER_TYPES", "SimConfig", "Simulation", "registry", "roles", "run_simulation"]
