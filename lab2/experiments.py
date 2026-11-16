"""Эксперименты ЛР2: сравнение механизмов распределения задач.

    python lab2/experiments.py                 # все эксперименты (E1–E5), 30 seed, CSV + графики
    python lab2/experiments.py --seeds 5       # быстрый прогон
    python lab2/experiments.py --jobs 8        # параллельно (быстрее, но замеры CPU менее точны)
    python lab2/experiments.py --only E1 E4    # выборочно

E1 — базовая конфигурация; E2 — масштабируемость (число курьеров, поток пропорционально);
E3 — интенсивность заказов; E4 — задержка связи; E5 — период телеметрии;
D1 — диагностика: назначения заряжающимся курьерам при 24 курьерах.
Для всех механизмов используются одни и те же seed (общие случайные числа): поток заказов одинаков.
Результаты: lab2/results/*.csv, графики: lab2/figures/*.png.
"""
from __future__ import annotations

import argparse
import math
import statistics
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lab1.mas import SimConfig, Simulation  # noqa: E402
from lab1.mas.experiment import aggregate, flatten, save_csv  # noqa: E402
from lab2 import plugin as lab2_plugin  # noqa: E402

lab2_plugin.register()

HERE = Path(__file__).resolve().parent
RESULTS, FIGURES = HERE / "results", HERE / "figures"

# Механизмы (ключ режима allocation -> подпись). Цвет закреплён за механизмом во всех графиках.
MECHS = {
    "central": "Централизованная",
    "cnp": "Контрактная сеть",
    "cnp-commit": "CNP + обязательства",
    "auction": "Аукцион SSI",
    "auction-par": "Параллельный аукцион",
    "delegation": "Делегирование",
}
COLORS = {"central": "#2a78d6", "cnp": "#eb6834", "cnp-commit": "#1baf7a", "auction": "#eda100",
          "auction-par": "#e87ba4", "delegation": "#008300"}
MARKERS = {"central": "o", "cnp": "s", "cnp-commit": "D", "auction": "^", "auction-par": "v", "delegation": "P"}
ALLOC_PROTOCOLS = ("assign", "cnp", "auction", "delegate")
# смещения подписей точек на диаграмме компромисса (чтобы близкие точки не сливались)
LABEL_OFFSETS = {"central": (8, 6, "left"), "cnp": (9, -14, "left"), "cnp-commit": (9, 8, "left"),
                 "auction": (9, -14, "left"), "auction-par": (-9, 8, "right"), "delegation": (9, 6, "left")}

KEYS = ["completion_rate", "on_time_rate", "urgent_on_time_rate", "avg_delivery_time", "p95_delivery_time",
        "avg_assign_latency", "makespan", "msgs_total", "msgs_per_order", "alloc_msgs", "alloc_msgs_per_order",
        "telemetry_per_order", "other_msgs_per_order", "bytes_total", "coord_cpu_ms", "cpu_per_order_ms",
        "cpu_ms.allocation",
        "cpu_ms.bidding", "distance_total", "energy_total", "jain_tasks", "jain_earnings", "utilization",
        "reassignments", "stranded"]

LABELS = {
    "on_time_rate": "Доля доставок в срок", "urgent_on_time_rate": "Срочные в срок",
    "avg_delivery_time": "Ср. время доставки, такт", "p95_delivery_time": "95-й перцентиль доставки, такт",
    "avg_assign_latency": "Задержка назначения, такт", "makespan": "Время выполнения всех заказов, такт",
    "msgs_per_order": "Сообщений на заказ (всего)", "alloc_msgs_per_order": "Сообщений распределения на заказ",
    "telemetry_per_order": "Сообщений телеметрии на заказ", "coord_cpu_ms": "CPU координации, мс",
    "cpu_per_order_ms": "CPU координации на заказ, мс", "distance_total": "Суммарный пробег, клеток",
    "jain_tasks": "Индекс Джайна (заказы)", "utilization": "Загрузка курьеров",
}

BASE = SimConfig()


def experiments(seeds: list[int]) -> dict[str, list[tuple]]:
    """Задания: эксперимент -> [(эксперимент, значение фактора, механизм, seed, переопределения SimConfig)]."""
    plan: dict[str, list[tuple]] = {}

    def add(exp, x, overrides):
        for mech in MECHS:
            for s in seeds:
                plan.setdefault(exp, []).append((exp, x, mech, s, overrides))

    add("E1", "base", {})
    for n in (4, 8, 12, 16, 24):
        add("E2", n, {"n_couriers": n, "heavy_couriers": max(1, n // 4), "order_rate": round(0.025 * n, 3),
                      "n_orders": int(7.5 * n)})
    for rate in (0.1, 0.2, 0.4, 0.8):
        add("E3", rate, {"order_rate": rate})
    for lat in (1, 2, 4):
        add("E4", lat, {"msg_latency": lat})
    for period in (5, 10, 20):
        add("E5", period, {"telemetry_period": period})
    return plan


def run_one(task: tuple) -> dict:
    exp, x, mech, seed, overrides = task
    sim = Simulation(BASE, seed=seed, allocation=mech, trace_messages=False, **overrides)
    m = sim.run()
    row = {"exp": exp, "x": x, "variant": mech, "seed": seed, **flatten(m)}
    n = max(1, m["orders_total"])
    by_proto = m["msgs_by_protocol"]
    row["alloc_msgs"] = sum(by_proto.get(p, 0) for p in ALLOC_PROTOCOLS)
    row["alloc_msgs_per_order"] = round(row["alloc_msgs"] / n, 3)
    row["telemetry_per_order"] = round(by_proto.get("telemetry", 0) / n, 3)
    row["other_msgs_per_order"] = round((m["msgs_total"] - row["alloc_msgs"] - by_proto.get("telemetry", 0)) / n, 3)
    row["cpu_per_order_ms"] = round(m["coord_cpu_ms"] / n, 4)
    row.setdefault("cpu_ms.allocation", 0.0)
    row.setdefault("cpu_ms.bidding", 0.0)
    return row


# --- диагностика: назначения заряжающимся курьерам (крупный флот E2) ---
CHARGING_STATES = {"to_station", "waiting_charge", "charging"}


def diag_charging(task: tuple) -> dict:
    """Доля назначений курьерам, которые в момент назначения едут на зарядку или заряжаются,
    и средний интервал «назначение -> забор» для таких и для остальных назначений."""
    mech, seed, overrides = task
    sim = Simulation(BASE, seed=seed, allocation=mech, trace_messages=False, **overrides)
    seen: dict[str, tuple[int, bool]] = {}

    def hook(s, t):
        for oid, r in s.dispatcher.records.items():
            if oid not in seen and r.courier and r.status in ("assigning", "assigned"):
                seen[oid] = (t, s.agents[r.courier].state in CHARGING_STATES)

    sim.hooks_post.append(hook)
    sim.run()
    lag = {True: [], False: []}
    for oid, (t, charging) in seen.items():
        picked = sim.metrics.orders[oid]["picked"]
        if picked is not None:
            lag[charging].append(picked - t)
    n = max(1, len(seen))
    return {"variant": mech, "seed": seed, "assignments": len(seen),
            "share_to_charging": round(sum(c for _, c in seen.values()) / n, 4),
            "pickup_lag_charging": round(statistics.fmean(lag[True]), 2) if lag[True] else "",
            "pickup_lag_other": round(statistics.fmean(lag[False]), 2) if lag[False] else ""}


# --- статистика ---
def summarize(rows: list[dict], keys: list[str]) -> list[dict]:
    """Среднее, СКО и полуширина 95% ДИ по (механизм, значение фактора)."""
    out = []
    xs = sorted({r["x"] for r in rows}, key=lambda v: (isinstance(v, str), v))
    for x in xs:
        agg = aggregate([r for r in rows if r["x"] == x], keys)
        for mech in MECHS:
            if mech not in agg:
                continue
            rec = {"x": x, "variant": mech}
            for k, (mean, sd, ci) in agg[mech].items():
                rec[k], rec[k + "_sd"], rec[k + "_ci95"] = mean, sd, ci
            out.append(rec)
    return out


def paired(rows: list[dict], keys: list[str], ref: str = "central") -> list[dict]:
    """Парные разности с эталоном по одинаковым seed (общие случайные числа): среднее и 95% ДИ."""
    base = {r["seed"]: r for r in rows if r["variant"] == ref}
    out = []
    for mech in MECHS:
        if mech == ref:
            continue
        rs = [r for r in rows if r["variant"] == mech and r["seed"] in base]
        rec = {"variant": mech, "ref": ref, "n": len(rs)}
        for k in keys:
            d = [float(r.get(k, 0) or 0) - float(base[r["seed"]].get(k, 0) or 0) for r in rs]
            mean = statistics.fmean(d)
            ci = 1.96 * statistics.stdev(d) / math.sqrt(len(d)) if len(d) > 1 else 0.0
            rec[k + "_diff"], rec[k + "_ci95"] = round(mean, 4), round(ci, 4)
        out.append(rec)
    return out


# --- графики ---
INK, INK2, MUTED, GRID, AXIS, SURFACE = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7", "#fcfcfb"


def _plt():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({
        "font.family": "DejaVu Sans", "font.size": 10, "axes.edgecolor": AXIS, "axes.labelcolor": INK2,
        "axes.titlesize": 11, "axes.titlecolor": INK, "xtick.color": INK2, "ytick.color": INK2,
        "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8, "grid.linestyle": "-",
        "axes.spines.top": False, "axes.spines.right": False, "figure.facecolor": "white",
        "axes.facecolor": SURFACE, "legend.frameon": False, "axes.axisbelow": True,
    })
    return plt


def fig_dotplot(summary: list[dict], metrics: list[str], path: Path, title: str) -> None:
    """E1: среднее ± 95% ДИ по каждому механизму (точка с «усами»), по панели на показатель."""
    plt = _plt()
    cols = 3
    rows_n = math.ceil(len(metrics) / cols)
    fig, axes = plt.subplots(rows_n, cols, figsize=(13, 3.1 * rows_n + 0.8))
    mechs = [m for m in MECHS if any(r["variant"] == m for r in summary)]
    for n, (ax, k) in enumerate(zip(axes.flat, metrics)):
        for i, mech in enumerate(mechs):
            r = next(r for r in summary if r["variant"] == mech)
            y = len(mechs) - 1 - i
            ax.errorbar(r[k], y, xerr=r[k + "_ci95"], fmt=MARKERS[mech], color=COLORS[mech], ms=8,
                        mec="white", mew=1.2, elinewidth=1.6, capsize=3)
            ax.annotate(_fmt(r[k]), (r[k] + r[k + "_ci95"], y), xytext=(6, 0), textcoords="offset points",
                        va="center", fontsize=8.5, color=INK2)
        ax.set_yticks(range(len(mechs)))
        ax.set_yticklabels([MECHS[m] for m in reversed(mechs)] if n % cols == 0 else [])
        ax.set_title(LABELS.get(k, k), loc="left")
        ax.grid(axis="y", visible=False)
        lo, hi = ax.get_xlim()
        ax.set_xlim(lo, hi + (hi - lo) * 0.22)
    for ax in list(axes.flat)[len(metrics):]:
        ax.set_visible(False)
    fig.suptitle(title, x=0.01, ha="left", fontsize=12, color=INK)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def fig_messages(summary: list[dict], path: Path, n_seeds: int) -> None:
    """E1: состав трафика на заказ — распределение выделено цветом, остальное — оттенки серого."""
    plt = _plt()
    parts = [("alloc_msgs_per_order", "Распределение заказов", "#2a78d6"),
             ("telemetry_per_order", "Телеметрия (мониторинг)", "#898781"),
             ("other_msgs_per_order", "Заказы, статусы, зарядка", "#c3c2b7")]
    mechs = [m for m in MECHS if any(r["variant"] == m for r in summary)]
    fig, ax = plt.subplots(figsize=(10, 4.2))
    for i, mech in enumerate(mechs):
        r = next(r for r in summary if r["variant"] == mech)
        y, left = len(mechs) - 1 - i, 0.0
        for k, label, color in parts:
            v = r[k]
            ax.barh(y, v, left=left, height=0.55, color=color, edgecolor="white", linewidth=2,
                    label=label if i == 0 else None)
            if k == "alloc_msgs_per_order" and v > 3:
                ax.text(left + v / 2, y, f"{v:.1f}", ha="center", va="center", fontsize=9, color="white")
            left += v
        ax.text(left + 0.4, y, f"всего {left:.1f}", va="center", fontsize=9, color=INK2)
    ax.set_yticks(range(len(mechs)))
    ax.set_yticklabels([MECHS[m] for m in reversed(mechs)])
    ax.set_xlabel(f"Сообщений на один заказ (среднее по {n_seeds} seed)")
    ax.grid(axis="y", visible=False)
    ax.set_xlim(0, ax.get_xlim()[1] * 1.08)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.17), ncol=3)
    ax.set_title("Состав трафика на заказ, базовая конфигурация", loc="left")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def fig_tradeoff(summary: list[dict], path: Path, xk: str, yk: str, title: str) -> None:
    """Компромисс «стоимость координации — качество»: точка на механизм, 95% ДИ по обеим осям."""
    plt = _plt()
    fig, ax = plt.subplots(figsize=(8.5, 5.5))
    for r in summary:
        mech = r["variant"]
        ax.errorbar(r[xk], r[yk], xerr=r[xk + "_ci95"], yerr=r[yk + "_ci95"], fmt=MARKERS[mech],
                    color=COLORS[mech], ms=9, mec="white", mew=1.2, elinewidth=1.4, capsize=3, label=MECHS[mech])
        dx, dy, ha = LABEL_OFFSETS.get(mech, (8, 6, "left"))
        ax.annotate(MECHS[mech], (r[xk], r[yk]), xytext=(dx, dy), textcoords="offset points", fontsize=9,
                    color=INK, ha=ha)
    ax.set_xlabel(LABELS.get(xk, xk))
    ax.set_ylabel(LABELS.get(yk, yk))
    ax.set_title(title, loc="left")
    ax.legend(loc="best", fontsize=8.5)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def fig_lines(summary: list[dict], metrics: list[str], xlabel: str, path: Path, title: str,
              numeric: bool = False) -> None:
    """E2–E5: показатель от фактора, по линии на механизм, 95% ДИ — «усы» (со смещением, чтобы не слипались)."""
    plt = _plt()
    xs = sorted({r["x"] for r in summary})
    mechs = [m for m in MECHS if any(r["variant"] == m for r in summary)]
    cols = 2
    rows_n = math.ceil(len(metrics) / cols)
    fig, axes = plt.subplots(rows_n, cols, figsize=(12, 3.9 * rows_n + 0.9))
    for ax, k in zip(axes.flat, metrics):
        for j, mech in enumerate(mechs):
            pts = [next((r for r in summary if r["variant"] == mech and r["x"] == x), None) for x in xs]
            off = (j - (len(mechs) - 1) / 2) * (0.012 * (xs[-1] - xs[0]) if numeric else 0.06)
            px = [(x if numeric else i) + off for i, (x, p) in enumerate(zip(xs, pts)) if p]
            py = [p[k] for p in pts if p]
            pe = [p[k + "_ci95"] for p in pts if p]
            ax.errorbar(px, py, yerr=pe, color=COLORS[mech], marker=MARKERS[mech], ms=6.5, mec="white", mew=1,
                        lw=2, elinewidth=1.1, capsize=2.5, label=MECHS[mech], solid_capstyle="round")
        ax.set_xticks(xs if numeric else range(len(xs)))
        ax.set_xticklabels([str(x) for x in xs])
        ax.set_xlabel(xlabel)
        ax.set_title(LABELS.get(k, k), loc="left")
        ax.grid(axis="x", visible=False)
    for ax in list(axes.flat)[len(metrics):]:
        ax.set_visible(False)
    handles, labels = axes.flat[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncol=len(mechs), bbox_to_anchor=(0.5, 0.965), fontsize=9)
    fig.suptitle(title, x=0.01, ha="left", fontsize=12, color=INK)
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    fig.savefig(path, dpi=150)
    plt.close(fig)


def _fmt(v: float) -> str:
    return f"{v:.3f}" if abs(v) < 1.5 else (f"{v:.1f}" if abs(v) < 1000 else f"{v:.0f}")


# --- вывод таблиц в консоль ---
def print_table(summary: list[dict], keys: list[str], title: str) -> None:
    print(f"\n{title}")
    print("x".ljust(6) + "механизм".ljust(22) + "".join(k[:16].rjust(18) for k in keys))
    for r in summary:
        cells = "".join(f"{_fmt(r[k])}±{_fmt(r[k + '_ci95'])}".rjust(18) for k in keys)
        print(str(r["x"]).ljust(6) + MECHS[r["variant"]].ljust(22) + cells)


def main() -> None:
    ap = argparse.ArgumentParser(description="Эксперименты ЛР2")
    ap.add_argument("--seeds", type=int, default=30)
    ap.add_argument("--jobs", type=int, default=1,
                    help="параллельных процессов (по умолчанию 1: замеры CPU координации не искажаются конкуренцией)")
    ap.add_argument("--only", nargs="*", default=None, help="например: E1 E3 D1")
    a = ap.parse_args()
    seeds = list(range(1, a.seeds + 1))
    plan = experiments(seeds)
    if a.only:
        plan = {k: v for k, v in plan.items() if k in a.only}
    tasks = [t for ts in plan.values() for t in ts]
    print(f"Прогонов: {len(tasks)}, seed: 1..{a.seeds}, процессов: {a.jobs}")
    t0 = time.perf_counter()
    if a.jobs > 1:
        with ProcessPoolExecutor(max_workers=a.jobs) as ex:
            rows = list(ex.map(run_one, tasks, chunksize=8))
    else:
        rows = [run_one(t) for t in tasks]
    print(f"Моделирование: {time.perf_counter() - t0:.1f} с")

    RESULTS.mkdir(parents=True, exist_ok=True)
    FIGURES.mkdir(parents=True, exist_ok=True)
    names = {"E1": "e1_base", "E2": "e2_scale", "E3": "e3_load", "E4": "e4_latency", "E5": "e5_telemetry"}
    xlabels = {"E2": "Число курьеров (поток заказов пропорционален)", "E3": "Интенсивность заказов, заказов/такт",
               "E4": "Задержка доставки сообщения, тактов", "E5": "Период телеметрии, тактов"}
    for exp, name in names.items():
        rs = [r for r in rows if r["exp"] == exp]
        if not rs:
            continue
        save_csv(rs, RESULTS / f"{name}.csv")
        summ = summarize(rs, KEYS)
        save_csv(summ, RESULTS / f"{name}_summary.csv")
        if exp == "E1":
            pr = paired(rs, ["on_time_rate", "urgent_on_time_rate", "avg_delivery_time", "p95_delivery_time",
                             "avg_assign_latency", "alloc_msgs_per_order", "msgs_per_order", "coord_cpu_ms",
                             "distance_total", "jain_tasks"])
            save_csv(pr, RESULTS / "e1_paired_vs_central.csv")
            fig_dotplot(summ, ["on_time_rate", "avg_delivery_time", "p95_delivery_time", "avg_assign_latency",
                               "alloc_msgs_per_order", "coord_cpu_ms", "distance_total", "urgent_on_time_rate",
                               "jain_tasks"],
                        FIGURES / "e1_overview.png",
                        f"E1. Базовая конфигурация: 8 курьеров, 60 заказов, 0,2 заказа/такт (среднее ± 95% ДИ, {a.seeds} seed)")
            fig_messages(summ, FIGURES / "e1_messages.png", a.seeds)
            fig_tradeoff(summ, FIGURES / "e1_tradeoff.png", "alloc_msgs_per_order", "avg_delivery_time",
                         "Стоимость координации и качество (E1, среднее ± 95% ДИ)")
            print_table(summ, ["on_time_rate", "urgent_on_time_rate", "avg_delivery_time", "p95_delivery_time",
                               "avg_assign_latency", "makespan"], "E1 качество")
            print_table(summ, ["msgs_per_order", "alloc_msgs_per_order", "telemetry_per_order", "bytes_total",
                               "coord_cpu_ms", "cpu_ms.allocation", "cpu_ms.bidding"], "E1 стоимость координации")
            print_table(summ, ["distance_total", "energy_total", "jain_tasks", "jain_earnings", "utilization",
                               "reassignments"], "E1 ресурсы и справедливость")
            print("\nE1 парные разности с central (среднее ± 95% ДИ):")
            for p in pr:
                print("  " + MECHS[p["variant"]].ljust(22) + "  ".join(
                    f"{k[:-5]}={p[k]:+.3f}±{p[k[:-5] + '_ci95']:.3f}" for k in p if k.endswith("_diff")))
            continue
        metrics = {
            "E2": ["on_time_rate", "avg_delivery_time", "alloc_msgs_per_order", "cpu_per_order_ms"],
            "E3": ["on_time_rate", "avg_delivery_time", "avg_assign_latency", "alloc_msgs_per_order"],
            "E4": ["on_time_rate", "avg_delivery_time", "avg_assign_latency", "alloc_msgs_per_order"],
            "E5": ["on_time_rate", "avg_delivery_time", "telemetry_per_order", "msgs_per_order"],
        }[exp]
        titles = {"E2": "E2. Масштабируемость", "E3": "E3. Нагрузка", "E4": "E4. Задержка связи",
                  "E5": "E5. Период телеметрии"}
        fig_lines(summ, metrics, xlabels[exp], FIGURES / f"{name}.png",
                  f"{titles[exp]} (среднее ± 95% ДИ, {a.seeds} seed)", numeric=(exp == "E2"))
        print_table(summ, ["on_time_rate", "urgent_on_time_rate", "avg_delivery_time", "avg_assign_latency",
                           "alloc_msgs_per_order", "msgs_per_order", "cpu_per_order_ms", "distance_total"],
                    f"{exp} ({xlabels[exp]})")
    if not a.only or "D1" in a.only:
        big = {"n_couriers": 24, "heavy_couriers": 6, "order_rate": 0.6, "n_orders": 180}
        drows = [diag_charging((m, s, big)) for m in ("central", "cnp", "auction") for s in seeds]
        save_csv(drows, RESULTS / "d1_charging_assignments.csv")
        print("\nD1 (24 курьера): назначения курьерам, едущим на зарядку или заряжающимся")
        for m in ("central", "cnp", "auction"):
            rs = [r for r in drows if r["variant"] == m]
            share = statistics.fmean(r["share_to_charging"] for r in rs)
            lc = statistics.fmean(float(r["pickup_lag_charging"]) for r in rs if r["pickup_lag_charging"] != "")
            lo = statistics.fmean(float(r["pickup_lag_other"]) for r in rs if r["pickup_lag_other"] != "")
            print(f"  {MECHS[m]:<22} доля={share:.3f}  назначение->забор: заряжающиеся {lc:.1f}, остальные {lo:.1f}")
    print(f"\nГотово за {time.perf_counter() - t0:.1f} с. CSV: {RESULTS}, графики: {FIGURES}")


if __name__ == "__main__":
    main()
