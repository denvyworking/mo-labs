"""Запуск одной симуляции из командной строки.

    python lab1/run.py --list
    python lab1/run.py --allocation central --charging naive --seed 3 --couriers 8 --orders 60
    python lab1/run.py --trace trace.jsonl --json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lab1.mas import SimConfig, Simulation, registry  # noqa: E402
from lab1.mas.metrics import LABELS  # noqa: E402


def main() -> None:
    errors = registry.load_lab_plugins()
    ap = argparse.ArgumentParser(description="МАС городской доставки: одиночный прогон")
    ap.add_argument("--list", action="store_true", help="показать доступные режимы")
    ap.add_argument("--allocation", default="central")
    ap.add_argument("--charging", default="naive")
    ap.add_argument("--scenario", default="none")
    ap.add_argument("--defense", default="none")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--couriers", type=int, default=SimConfig.n_couriers)
    ap.add_argument("--orders", type=int, default=SimConfig.n_orders)
    ap.add_argument("--stations", type=int, default=SimConfig.n_stations)
    ap.add_argument("--slots", type=int, default=SimConfig.station_slots)
    ap.add_argument("--rate", type=float, default=SimConfig.order_rate, help="интенсивность заказов за такт")
    ap.add_argument("--trace", help="сохранить трассу в JSONL")
    ap.add_argument("--json", action="store_true", help="вывести все показатели в JSON")
    a = ap.parse_args()

    if a.list:
        for cat, modes in registry.listing().items():
            print(f"[{cat}]")
            for m in modes:
                print(f"  {m['key']:<22} {m['title']}  ({m['lab']})")
        for lab, err in errors.items():
            print(f"\nОшибка загрузки {lab}:\n{err}")
        return

    cfg = SimConfig(seed=a.seed, n_couriers=a.couriers, n_orders=a.orders, n_stations=a.stations,
                    station_slots=a.slots, order_rate=a.rate, allocation=a.allocation,
                    charging=a.charging, scenario=a.scenario, defense=a.defense)
    sim = Simulation(cfg)
    m = sim.run()
    if a.trace:
        sim.trace.save_jsonl(a.trace)
    if a.json:
        print(json.dumps(m, ensure_ascii=False, indent=2))
        return
    print(f"Режим: allocation={a.allocation}, charging={a.charging}, scenario={a.scenario}, defense={a.defense}")
    for k, label in LABELS.items():
        if k in m:
            print(f"  {label:<34} {m[k]}")
    print(f"  {'Сообщения по протоколам':<34} {m['msgs_by_protocol']}")
    if m["violations"]:
        print(f"  {'Нарушения':<34} {m['violations']}")


if __name__ == "__main__":
    main()
