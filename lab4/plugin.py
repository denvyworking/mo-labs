"""Регистрация режимов ЛР4: неблагоприятные сценарии (scenario) и защитные механизмы (defense)."""
from __future__ import annotations

from lab1.mas import registry


def register() -> None:
    from .defenses import DEFENSES, make_install
    from .scenarios import SCENARIOS

    for key, title, install, description in SCENARIOS:
        registry.register("scenario", key, title, install, description=description, lab="lab4")
    for key, title, layers, description in DEFENSES:
        registry.register("defense", key, title, make_install(layers), description=description, lab="lab4")
