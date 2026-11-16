"""Реестр режимов работы системы.

Четыре независимых измерения режима: allocation (ЛР2), charging (ЛР3), scenario и defense (ЛР4).
Режим задаётся функцией install(sim), которая после построения агентов подключает плагины,
промежуточные обработчики шины, заменяет агентов и т.п. Необязательная configure(cfg) правит
параметры до построения среды. Лабораторные 2–4 регистрируют свои режимы в labN/plugin.py.
"""
from __future__ import annotations

import importlib
import traceback
from dataclasses import dataclass
from typing import Callable

CATEGORIES = ("scenario", "allocation", "charging", "defense")   # порядок установки


@dataclass
class Mode:
    category: str
    key: str
    title: str
    install: Callable
    description: str = ""
    configure: Callable | None = None
    lab: str = "lab1"


MODES: dict[str, dict[str, Mode]] = {c: {} for c in CATEGORIES}
PLUGIN_ERRORS: dict[str, str] = {}
_loaded = False


def register(category: str, key: str, title: str, install: Callable, description: str = "",
             configure: Callable | None = None, lab: str = "lab1") -> Mode:
    if category not in MODES:
        raise ValueError(f"неизвестная категория режима: {category}")
    mode = Mode(category, key, title, install, description, configure, lab)
    MODES[category][key] = mode
    return mode


def get(category: str, key: str) -> Mode:
    try:
        return MODES[category][key]
    except KeyError:
        raise KeyError(f"режим {category}={key!r} не зарегистрирован; есть: {sorted(MODES[category])}") from None


def configure_all(cfg) -> None:
    for cat in CATEGORIES:
        mode = get(cat, getattr(cfg, cat))
        if mode.configure:
            mode.configure(cfg)


def install_all(sim) -> None:
    for cat in CATEGORIES:
        get(cat, getattr(sim.cfg, cat)).install(sim)


def load_lab_plugins(labs=("lab2", "lab3", "lab4")) -> dict[str, str]:
    """Импортирует labN/plugin.py (если есть) и вызывает его register(). Ошибки не роняют систему."""
    global _loaded
    if _loaded:
        return PLUGIN_ERRORS
    _loaded = True
    for lab in labs:
        try:
            mod = importlib.import_module(f"{lab}.plugin")
        except ModuleNotFoundError as e:
            if e.name in (lab, f"{lab}.plugin"):
                continue
            PLUGIN_ERRORS[lab] = traceback.format_exc()
            continue
        except Exception:
            PLUGIN_ERRORS[lab] = traceback.format_exc()
            continue
        try:
            if hasattr(mod, "register"):
                mod.register()
        except Exception:
            PLUGIN_ERRORS[lab] = traceback.format_exc()
    return PLUGIN_ERRORS


def listing() -> dict:
    return {cat: [{"key": m.key, "title": m.title, "description": m.description, "lab": m.lab}
                  for m in MODES[cat].values()] for cat in CATEGORIES}


# --- базовые режимы ЛР1 ---
def _install_central(sim) -> None:
    from .allocation import CentralizedAllocator
    sim.dispatcher.set_allocator(CentralizedAllocator())


def _install_naive_charging(sim) -> None:
    from .charging import FIFOStationPolicy, NaiveCourierCharging
    for c in sim.couriers:
        c.set_charging(NaiveCourierCharging())
    for s in sim.stations:
        s.set_policy(FIFOStationPolicy())


register("allocation", "central", "Централизованная диспетчеризация", _install_central,
         "Диспетчер выбирает курьера с минимальным ожидаемым временем завершения по телеметрии")
register("charging", "naive", "Без переговоров (ближайшая станция, FIFO)", _install_naive_charging,
         "Курьер едет на ближайшую станцию, станция обслуживает в порядке прибытия")
register("scenario", "none", "Штатная работа", lambda sim: None, "Без сбоев и злоумышленников")
register("defense", "none", "Без защитных механизмов", lambda sim: None)
