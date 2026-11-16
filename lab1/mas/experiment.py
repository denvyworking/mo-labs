"""Серийные эксперименты: прогоны по набору seed, агрегирование, сохранение CSV."""
from __future__ import annotations

import csv
import math
import statistics
from pathlib import Path

from .config import SimConfig
from .sim import Simulation


def run_batch(variants: dict[str, dict], seeds, base: SimConfig | None = None,
              trace_messages: bool = False) -> list[dict]:
    """variants: имя варианта -> переопределения SimConfig. Возвращает строки «вариант × seed»."""
    base = base or SimConfig()
    rows = []
    for name, overrides in variants.items():
        for seed in seeds:
            sim = Simulation(base, seed=seed, trace_messages=trace_messages, **overrides)
            m = sim.run()
            rows.append({"variant": name, "seed": seed, **flatten(m)})
    return rows


def flatten(m: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in m.items():
        if isinstance(v, dict):
            out.update(flatten(v, f"{prefix}{k}."))
        else:
            out[f"{prefix}{k}"] = v
    return out


def aggregate(rows: list[dict], keys: list[str], by: str = "variant") -> dict[str, dict[str, tuple]]:
    """Среднее, стандартное отклонение и полуширина 95% доверительного интервала по каждому ключу."""
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r[by], []).append(r)
    out = {}
    for g, rs in groups.items():
        out[g] = {}
        for k in keys:
            vals = [float(r.get(k, 0) or 0) for r in rs]
            mean = statistics.fmean(vals) if vals else 0.0
            sd = statistics.stdev(vals) if len(vals) > 1 else 0.0
            ci = 1.96 * sd / math.sqrt(len(vals)) if len(vals) > 1 else 0.0
            out[g][k] = (round(mean, 4), round(sd, 4), round(ci, 4))
    return out


def save_csv(rows: list[dict], path: str | Path) -> None:
    keys: list[str] = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)
