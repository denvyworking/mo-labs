"""Эксперименты ЛР3: сравнение МАС до и после введения механизма переговоров и разрешения конфликтов.

    python lab3/experiments.py            # все эксперименты (~2–4 минуты)
    python lab3/experiments.py --quick    # 8 seed вместо 30 (проверка)

E1 — до/после в сценарии дефицита: naive, priority, negotiation, reputation.
E2 — степень дефицита: лимит мощности сети и размер флота (с пропорциональным потоком заказов).
E3 — эгоистичные курьеры (0 %, 25 %, 50 %): выигрыш нечестных без репутации и с ней.
Результаты: lab3/results/*.csv, графики: lab3/figures/*.png. Для всех вариантов одни и те же seed.
"""
from __future__ import annotations

import argparse
import math
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from lab1.mas import SimConfig, Simulation  # noqa: E402
from lab1.mas.experiment import flatten, save_csv  # noqa: E402

import lab3.plugin  # noqa: E402
from lab3.common import gini  # noqa: E402

lab3.plugin.register()

HERE = Path(__file__).resolve().parent
RESULTS, FIGURES = HERE / "results", HERE / "figures"
N_SEEDS = 30   # переопределяется ключом --quick

# Сценарий дефицита (обоснование — в отчёте): 14 курьеров (2 тяжёлых), 2 станции по 2 места,
# медленная зарядка (5 ед./такт), лимит сети — 3 одновременно заряжающихся при 4 физических местах.
SCENARIO = dict(n_couriers=14, heavy_couriers=2, n_orders=150, order_rate=0.35, n_stations=2, station_slots=2,
                charge_rate=5.0, grid_power_limit=3, trace_messages=False)
BASE = SimConfig(**SCENARIO)

VARIANTS = {
    "naive": {"charging": "naive"},
    "priority": {"charging": "priority"},
    "negotiation": {"charging": "negotiation"},
    "reputation": {"charging": "reputation"},
}
ABLATION = {"negotiation-nopartial": {"charging": "negotiation", "params": {"partial_charge": False}}}

TITLES = {
    "naive": "naive (до)",
    "priority": "priority",
    "negotiation": "negotiation",
    "reputation": "reputation",
    "negotiation-nopartial": "negotiation без частичной",
}
# Эталонная категориальная палитра (dataviz), фиксированный порядок слотов
COLORS = {"naive": "#2a78d6", "priority": "#eb6834", "negotiation": "#1baf7a", "reputation": "#eda100",
          "negotiation-nopartial": "#e87ba4"}
GROUP_COLORS = {"honest": "#2a78d6", "selfish": "#eb6834"}
SURFACE, GRID, AXIS, INK, INK2 = "#fcfcfb", "#e1e0d9", "#c3c2b7", "#0b0b0b", "#52514e"
CHARGE_PROTOCOLS = ("charge", "slot", "power", "reputation", "audit")


# --- наблюдатель (одинаковый для всех режимов, в том числе naive) ---
class Observer:
    """Каждый такт фиксирует физическую картину: мощность (сумма занятых мест), ожидающих курьеров,
    сколько из них везут посылку или имеют срочный заказ. Агенты эти данные не видят."""

    def __init__(self, sim: Simulation, timeline: bool = False):
        self.sim = sim
        self.timeline = [] if timeline else None
        self.power_sum = 0
        self.excess = 0
        self.at_limit = self.with_queue = 0
        self.wait_ticks = self.wait_urgent = self.wait_parcel = 0
        self.ticks = 0
        sim.hooks_post.append(self.hook)

    def hook(self, sim, tick: int) -> None:
        power = sum(len(s.occupied) for s in sim.stations)
        limit = sim.cfg.grid_power_limit
        self.ticks += 1
        self.power_sum += power
        if limit is not None:
            self.excess += max(0, power - limit)
            self.at_limit += power >= limit
        waiting = [c for c in sim.couriers if c.state == "waiting_charge"]
        self.with_queue += bool(waiting)
        self.wait_ticks += len(waiting)
        self.wait_urgent += sum(1 for c in waiting if any(o.priority == "urgent" for o in c.tasks()))
        self.wait_parcel += sum(1 for c in waiting if c.picked)
        if self.timeline is not None:
            self.timeline.append((tick, power, len(waiting)))

    def summary(self) -> dict:
        n = max(1, self.ticks)
        return {"obs_mean_power": round(self.power_sum / n, 3),
                "obs_share_ticks_at_limit": round(self.at_limit / n, 4),
                "obs_share_ticks_with_waiting": round(self.with_queue / n, 4),
                "obs_excess_slot_ticks": self.excess,
                "obs_wait_courier_ticks": self.wait_ticks,
                "obs_wait_urgent_ticks": self.wait_urgent,
                "obs_wait_parcel_ticks": self.wait_parcel}


def run_one(overrides: dict, seed: int, timeline: bool = False) -> tuple[dict, Observer]:
    sim = Simulation(BASE, seed=seed, **overrides)
    obs = Observer(sim, timeline)
    m = sim.run()
    row = flatten(m)
    row.update(obs.summary())
    row["energy_per_tick"] = round(m["energy_total"] / max(1, m["ticks"]), 3)
    # Джини по среднему ожиданию курьеров и по доходу — по показателям ядра, одинаково для всех режимов
    waits: dict[str, list[int]] = {}
    for cid, w in sim.metrics.charge_waits:
        waits.setdefault(cid, []).append(w)
    row["gini_wait"] = gini([statistics.fmean(v) for v in waits.values()])
    row["gini_earn"] = gini([sim.metrics.earnings[c.id] for c in sim.couriers])
    row["charge_msgs"] = sum(int(m["msgs_by_protocol"].get(p, 0)) for p in CHARGE_PROTOCOLS)
    row["charge_msgs_per_session"] = round(row["charge_msgs"] / max(1, m["charge_sessions"]), 2)
    return row, obs


def run_grid(variants: dict, seeds) -> list[dict]:
    rows = []
    for vname, ov in variants.items():
        for seed in seeds:
            row, _ = run_one(ov, seed)
            rows.append({"variant": vname, "seed": seed, **row})
    return rows


# --- статистика ---
def mean_ci(vals: list[float]) -> tuple[float, float]:
    vals = [float(v) for v in vals]
    if not vals:
        return 0.0, 0.0
    m = statistics.fmean(vals)
    ci = 1.96 * statistics.stdev(vals) / math.sqrt(len(vals)) if len(vals) > 1 else 0.0
    return m, ci


def summarize(rows: list[dict], keys: list[str], by: tuple[str, ...]) -> list[dict]:
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault(tuple(r.get(b) for b in by), []).append(r)
    out = []
    for g, rs in groups.items():
        rec = dict(zip(by, g))
        rec["n"] = len(rs)
        for k in keys:
            m, ci = mean_ci([r.get(k, 0) or 0 for r in rs])
            rec[k] = round(m, 4)
            rec[k + "_ci95"] = round(ci, 4)
        out.append(rec)
    return out


def paired_vs(rows: list[dict], base: str, keys: list[str]) -> list[dict]:
    """Парные разности «вариант − base» по одинаковым seed (метод общих случайных чисел)."""
    by = {(r["variant"], r["seed"]): r for r in rows}
    variants = sorted({r["variant"] for r in rows}, key=list(VARIANTS).index)
    out = []
    for v in variants:
        if v == base:
            continue
        rec = {"variant": v, "vs": base}
        seeds = sorted(s for (vv, s) in by if vv == v and (base, s) in by)
        for k in keys:
            m, ci = mean_ci([float(by[(v, s)].get(k, 0) or 0) - float(by[(base, s)].get(k, 0) or 0) for s in seeds])
            rec[k] = round(m, 4)
            rec[k + "_ci95"] = round(ci, 4)
        out.append(rec)
    return out


# --- оформление графиков ---
def style(ax, title: str, ylabel: str = "") -> None:
    ax.set_facecolor(SURFACE)
    ax.set_title(title, fontsize=10, color=INK, loc="left")
    if ylabel:
        ax.set_ylabel(ylabel, fontsize=9, color=INK2)
    ax.grid(axis="y", color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(AXIS)
        ax.spines[side].set_linewidth(0.8)
    ax.tick_params(colors=INK2, labelsize=8, length=3, color=AXIS)


def new_fig(nrows, ncols, w, h):
    fig, axes = plt.subplots(nrows, ncols, figsize=(w, h), squeeze=False)
    fig.patch.set_facecolor(SURFACE)
    return fig, axes


def save(fig, name: str) -> None:
    FIGURES.mkdir(parents=True, exist_ok=True)
    fig.savefig(FIGURES / name, dpi=150, facecolor=SURFACE, bbox_inches="tight")
    plt.close(fig)


def bars_panel(ax, summ: list[dict], key: str, title: str, ylabel: str = "", points: bool = False) -> None:
    """Столбцы от нуля; для долей с узким диапазоном (points=True) — точка и 95% ДИ без обрезанных столбцов."""
    order = [s for s in summ]
    xs = range(len(order))
    for i, s in enumerate(order):
        if points:
            ax.errorbar(i, s[key], yerr=s[key + "_ci95"], color=COLORS[s["variant"]], capsize=4, linewidth=2,
                        marker="o", markersize=8, markeredgecolor=SURFACE, markeredgewidth=1.5)
        else:
            ax.bar(i, s[key], width=0.62, color=COLORS[s["variant"]], edgecolor=SURFACE, linewidth=1.5)
            ax.errorbar(i, s[key], yerr=s[key + "_ci95"], color=INK2, capsize=3, linewidth=1)
    if points:
        ax.set_xlim(-0.6, len(order) - 0.4)
    ax.set_xticks(list(xs))
    ax.set_xticklabels([s["variant"] for s in order], rotation=20, ha="right")
    style(ax, title, ylabel)


# --- эксперименты ---
E1_KEYS = ["on_time_rate", "urgent_on_time_rate", "completion_rate", "avg_delivery_time", "p95_delivery_time",
           "makespan", "charge_sessions", "charge_wait_avg", "charge_wait_max", "jain_charge_wait",
           "jain_earnings", "gini_wait", "gini_earn", "power_overload_ticks", "obs_excess_slot_ticks",
           "max_power", "obs_mean_power", "obs_share_ticks_at_limit", "obs_share_ticks_with_waiting",
           "energy_per_tick", "stranded", "violations.station_slots", "obs_wait_courier_ticks",
           "obs_wait_urgent_ticks", "obs_wait_parcel_ticks", "energy_total", "distance_total", "msgs_total",
           "charge_msgs", "charge_msgs_per_session", "coord_cpu_ms", "l3_quota_stops", "l3_bookings",
           "l3_grid_wait_avg", "l3_power_requests", "l3_fallbacks", "l3_false_accusations"]


def experiment_e1(seeds) -> list[dict]:
    rows = run_grid(VARIANTS, seeds)
    save_csv(rows, RESULTS / "e1_runs.csv")
    summ = summarize(rows, E1_KEYS, ("variant",))
    save_csv(summ, RESULTS / "e1_summary.csv")
    save_csv(paired_vs(rows, "naive", ["on_time_rate", "urgent_on_time_rate", "avg_delivery_time",
                                       "charge_wait_avg", "charge_wait_max", "jain_charge_wait",
                                       "power_overload_ticks", "obs_wait_urgent_ticks", "obs_wait_parcel_ticks"]),
             RESULTS / "e1_paired_vs_naive.csv")

    fig, axes = new_fig(2, 4, 13, 6.2)
    panels = [("on_time_rate", "Доля доставок в срок"), ("urgent_on_time_rate", "Срочные в срок"),
              ("avg_delivery_time", "Ср. время доставки, такт"), ("charge_wait_avg", "Ср. ожидание зарядки, такт"),
              ("charge_wait_max", "Макс. ожидание зарядки, такт"), ("jain_charge_wait", "Индекс Джайна по ожиданию"),
              ("obs_wait_urgent_ticks", "Ожидание курьеров со срочным\nзаказом, курьеро-такт"),
              ("power_overload_ticks", "Тактов с превышением\nлимита мощности")]
    for ax, (k, t) in zip(axes.flat, panels):
        bars_panel(ax, summ, k, t, points=k in ("on_time_rate", "urgent_on_time_rate", "jain_charge_wait"))
    fig.suptitle(f"E1. До и после: сценарий дефицита (14 курьеров, 2×2 места, лимит сети 3), {N_SEEDS} seed, 95% ДИ",
                 fontsize=11, color=INK, x=0.01, ha="left")
    fig.tight_layout()
    save(fig, "e1_before_after.png")

    # стоимость механизма: сообщения протоколов зарядки по видам
    fig, axes = new_fig(1, 2, 11, 3.6)
    ax = axes[0][0]
    bottoms = [0.0] * len(VARIANTS)
    proto_colors = {"charge": "#2a78d6", "slot": "#eb6834", "power": "#1baf7a", "reputation": "#eda100",
                    "audit": "#e87ba4"}
    by_v = {v: [r for r in rows if r["variant"] == v] for v in VARIANTS}
    for p in CHARGE_PROTOCOLS:
        vals = [statistics.fmean([float(r.get(f"msgs_by_protocol.{p}", 0) or 0) for r in by_v[v]]) for v in VARIANTS]
        ax.bar(range(len(VARIANTS)), vals, bottom=bottoms, width=0.62, color=proto_colors[p], label=p,
               edgecolor=SURFACE, linewidth=1.5)
        bottoms = [b + v for b, v in zip(bottoms, vals)]
    ax.set_xticks(range(len(VARIANTS)))
    ax.set_xticklabels(list(VARIANTS))
    style(ax, "Сообщения протоколов зарядки за прогон", "сообщений")
    ax.legend(fontsize=8, frameon=False, ncol=1, loc="upper left")
    bars_panel(axes[0][1], summ, "coord_cpu_ms", "Время работы механизмов координации, мс")
    fig.tight_layout()
    save(fig, "e1_cost.png")
    return summ


def experiment_timeline(seed: int = 2) -> None:
    fig, axes = new_fig(2, 1, 11, 5.2)
    rows_out = []
    for ax, v in zip(axes.flat, ["naive", "negotiation"]):
        row, obs = run_one(VARIANTS[v], seed, timeline=True)
        ts = [t for t, _, _ in obs.timeline]
        power = [p for _, p, _ in obs.timeline]
        wait = [w for _, _, w in obs.timeline]
        ax.step(ts, power, where="post", color=COLORS[v], linewidth=1.2, label="заряжаются (мощность)")
        ax.step(ts, wait, where="post", color=INK2, linewidth=0.8, alpha=0.7, label="ждут на станциях")
        over = [t for t, p in zip(ts, power) if p > BASE.grid_power_limit]
        if over:
            ax.scatter(over, [BASE.grid_power_limit + 1] * len(over), s=8, color="#e34948",
                       label="превышение лимита", zorder=3)
        ax.axhline(BASE.grid_power_limit, color="#e34948", linewidth=1, label=f"лимит сети = {BASE.grid_power_limit}")
        style(ax, f"{TITLES[v]}: превышений {row['power_overload_ticks']} тактов, "
                  f"ср. ожидание {row['charge_wait_avg']}, в срок {row['on_time_rate']:.0%}", "курьеров")
        ax.set_ylim(-0.2, 5.6)
        ax.legend(fontsize=8, frameon=False, ncol=4, loc="upper right")
        for t, p, w in obs.timeline:
            rows_out.append({"variant": v, "seed": seed, "tick": t, "power": p, "waiting": w})
    axes[1][0].set_xlabel("такт", fontsize=9, color=INK2)
    fig.tight_layout()
    save(fig, "power_timeline.png")
    save_csv(rows_out, RESULTS / "power_timeline.csv")


E2_KEYS = ["on_time_rate", "urgent_on_time_rate", "avg_delivery_time", "charge_wait_avg", "charge_wait_max",
           "jain_charge_wait", "power_overload_ticks", "obs_excess_slot_ticks", "charge_sessions", "stranded",
           "obs_wait_urgent_ticks"]
E2_VARIANTS = {**{k: VARIANTS[k] for k in ("naive", "priority", "negotiation")}, **ABLATION}


def line_panels(rows_summ: list[dict], xkey: str, panels, title: str, fname: str, xlabel: str) -> None:
    fig, axes = new_fig(1, len(panels), 4.0 * len(panels), 3.8)
    for ax, (k, t) in zip(axes.flat, panels):
        for v in E2_VARIANTS:
            pts = sorted([s for s in rows_summ if s["variant"] == v], key=lambda s: s[xkey])
            xs = [s[xkey] for s in pts]
            ys = [s[k] for s in pts]
            ci = [s[k + "_ci95"] for s in pts]
            ax.plot(xs, ys, color=COLORS[v], linewidth=2, marker="o", markersize=5, label=TITLES[v])
            ax.fill_between(xs, [y - c for y, c in zip(ys, ci)], [y + c for y, c in zip(ys, ci)],
                            color=COLORS[v], alpha=0.15, linewidth=0)
        ax.set_xticks(sorted({s[xkey] for s in rows_summ}))
        style(ax, t)
        ax.set_xlabel(xlabel, fontsize=9, color=INK2)
    axes[0][0].legend(fontsize=8, frameon=False)
    fig.suptitle(title, fontsize=11, color=INK, x=0.01, ha="left")
    fig.tight_layout()
    save(fig, fname)


def experiment_e2(seeds) -> None:
    rows = []
    for limit in (2, 3, 4):
        for vname, ov in E2_VARIANTS.items():
            for seed in seeds:
                row, _ = run_one({**ov, "grid_power_limit": limit}, seed)
                rows.append({"variant": vname, "seed": seed, "limit": limit, **row})
    save_csv(rows, RESULTS / "e2_limit_runs.csv")
    summ = summarize(rows, E2_KEYS, ("variant", "limit"))
    save_csv(summ, RESULTS / "e2_limit_summary.csv")
    line_panels(summ, "limit", [("on_time_rate", "Доля доставок в срок"),
                                ("charge_wait_avg", "Ср. ожидание зарядки, такт"),
                                ("power_overload_ticks", "Тактов с превышением лимита")],
                f"E2а. Лимит мощности сети (при 4 физических местах), {N_SEEDS} seed, 95% ДИ", "e2_limit.png",
                "лимит сети, одновременно заряжающихся")

    rows = []
    for n in (8, 11, 14, 17, 20):
        extra = {"n_couriers": n, "order_rate": round(0.025 * n, 4), "n_orders": round(150 * n / 14)}
        for vname, ov in E2_VARIANTS.items():
            for seed in seeds:
                row, _ = run_one({**ov, **extra}, seed)
                rows.append({"variant": vname, "seed": seed, "fleet": n, **row})
    save_csv(rows, RESULTS / "e2_fleet_runs.csv")
    summ = summarize(rows, E2_KEYS, ("variant", "fleet"))
    save_csv(summ, RESULTS / "e2_fleet_summary.csv")
    line_panels(summ, "fleet", [("on_time_rate", "Доля доставок в срок"),
                                ("charge_wait_avg", "Ср. ожидание зарядки, такт"),
                                ("power_overload_ticks", "Тактов с превышением лимита")],
                f"E2б. Размер флота (поток заказов растёт пропорционально), лимит 3, {N_SEEDS} seed, 95% ДИ",
                "e2_fleet.png", "курьеров")


E3_KEYS = ["on_time_rate", "urgent_on_time_rate", "avg_delivery_time", "charge_wait_avg", "jain_charge_wait",
           "l3_wait_honest", "l3_wait_selfish", "l3_earn_honest", "l3_earn_selfish", "l3_lies_detected",
           "l3_false_accusations", "l3_evictions", "l3_overstays", "l3_noshows", "l3_booking_refusals",
           "l3_rep_honest", "l3_rep_selfish", "power_overload_ticks", "charge_msgs", "stranded"]
E3_MODES = ("priority", "negotiation", "reputation")


def experiment_e3(seeds) -> None:
    rows = []
    for limit in (3, 2):
        for share in (0.0, 0.25, 0.5):
            for vname in E3_MODES:
                for seed in seeds:
                    row, _ = run_one({**VARIANTS[vname], "grid_power_limit": limit,
                                      "params": {"selfish_share": share}}, seed)
                    rows.append({"variant": vname, "seed": seed, "limit": limit, "selfish_share": share, **row})
    save_csv(rows, RESULTS / "e3_selfish_runs.csv")
    summ = summarize(rows, E3_KEYS, ("variant", "limit", "selfish_share"))
    # выигрыш эгоиста по ожиданию: парная разность «честные − эгоисты» в одном прогоне
    for s in summ:
        rs = [r for r in rows if r["variant"] == s["variant"] and r["limit"] == s["limit"]
              and r["selfish_share"] == s["selfish_share"]]
        if s["selfish_share"] > 0:
            m, ci = mean_ci([r["l3_wait_honest"] - r["l3_wait_selfish"] for r in rs])
            s["wait_gain_selfish"], s["wait_gain_selfish_ci95"] = round(m, 4), round(ci, 4)
            m, ci = mean_ci([r["l3_earn_selfish"] - r["l3_earn_honest"] for r in rs])
            s["earn_gain_selfish"], s["earn_gain_selfish_ci95"] = round(m, 4), round(ci, 4)
    save_csv(summ, RESULTS / "e3_selfish_summary.csv")

    fig, axes = new_fig(2, 2, 12, 7.4)
    for row_i, limit in enumerate((3, 2)):
        for col_i, (hk, sk, title, ylab) in enumerate([
                ("l3_wait_honest", "l3_wait_selfish", "Ср. ожидание зарядки", "такт"),
                ("l3_earn_honest", "l3_earn_selfish", "Ср. доход лёгкого курьера", "усл. ед.")]):
            ax = axes[row_i][col_i]
            labels, x = [], 0
            for vname in E3_MODES:
                for share in (0.25, 0.5):
                    s = next(z for z in summ if z["variant"] == vname and z["limit"] == limit
                             and z["selfish_share"] == share)
                    for off, key, grp in ((-0.19, hk, "honest"), (0.19, sk, "selfish")):
                        ax.bar(x + off, s[key], width=0.36, color=GROUP_COLORS[grp], edgecolor=SURFACE, linewidth=1.5,
                               label={"honest": "честные", "selfish": "эгоистичные"}[grp] if x == 0 else None)
                        ax.errorbar(x + off, s[key], yerr=s[key + "_ci95"], color=INK2, capsize=2, linewidth=0.9)
                    labels.append(f"{vname}\n{int(share * 100)}% эгоистов")
                    x += 1
            ax.set_xticks(range(len(labels)))
            ax.set_xticklabels(labels, fontsize=7.5)
            style(ax, f"{title}: лимит сети {limit}", ylab)
            if row_i == 0 and col_i == 0:
                ax.legend(fontsize=8, frameon=False)
    fig.suptitle(f"E3. Честные и эгоистичные курьеры (лёгкие), {N_SEEDS} seed, 95% ДИ", fontsize=11, color=INK,
                 x=0.01, ha="left")
    fig.tight_layout()
    save(fig, "e3_selfish.png")


def main() -> None:
    ap = argparse.ArgumentParser(description="Эксперименты ЛР3")
    ap.add_argument("--quick", action="store_true", help="8 seed вместо 30")
    ap.add_argument("--only", choices=["e1", "e2", "e3", "timeline"], help="один эксперимент")
    a = ap.parse_args()
    global N_SEEDS
    N_SEEDS = 8 if a.quick else 30
    seeds = range(1, N_SEEDS + 1)
    t0 = time.time()
    if a.only in (None, "e1"):
        summ = experiment_e1(seeds)
        print(f"E1 готов ({time.time() - t0:.0f} с)")
        for s in summ:
            print(f"  {s['variant']:<12} в срок {s['on_time_rate']:.3f}±{s['on_time_rate_ci95']:.3f}  "
                  f"срочные {s['urgent_on_time_rate']:.3f}  ожидание {s['charge_wait_avg']:.2f}  "
                  f"макс {s['charge_wait_max']:.1f}  Джайн {s['jain_charge_wait']:.3f}  "
                  f"превышений {s['power_overload_ticks']:.1f}")
    if a.only in (None, "timeline"):
        experiment_timeline()
        print(f"Временной ряд мощности готов ({time.time() - t0:.0f} с)")
    if a.only in (None, "e2"):
        experiment_e2(seeds)
        print(f"E2 готов ({time.time() - t0:.0f} с)")
    if a.only in (None, "e3"):
        experiment_e3(seeds)
        print(f"E3 готов ({time.time() - t0:.0f} с)")
    print(f"Всего {time.time() - t0:.0f} с. CSV: {RESULTS}, графики: {FIGURES}")


if __name__ == "__main__":
    main()
