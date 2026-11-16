"""Эксперименты ЛР4: устойчивость МАС к неблагоприятным сценариям до и после защиты.

    python lab4/experiments.py            # все эксперименты (около 3–5 минут)
    python lab4/experiments.py --seeds 5  # быстрый прогон

E1. Матрица «сценарий × защита» (9 × 8) на одних и тех же seed (метод общих случайных чисел):
    без защиты, каждый слой отдельно и все слои вместе. Сюда же входит проверка ложных срабатываний:
    штатный режим (scenario=none) с каждой защитой.
E2. Показательные трассы: каждый сценарий без защиты и со всеми слоями (seed=2), JSONL с сообщениями
    и снимками истинного состояния -> lab4/traces/; анализ трасс (analyze.py) -> results/trace_*.
Результаты: results/*.csv, графики: figures/*.png.
"""
from __future__ import annotations

import argparse
import csv
import json
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
import numpy as np  # noqa: E402

from lab1.mas import SimConfig, Simulation  # noqa: E402
from lab1.mas.experiment import flatten, save_csv  # noqa: E402
from lab4 import analyze, plugin  # noqa: E402
from lab4.common import ensure_integrity, install_truth_probe, integrity_metrics  # noqa: E402

plugin.register()

HERE = Path(__file__).resolve().parent
RES, FIG, TRACES = HERE / "results", HERE / "figures", HERE / "traces"
TRACE_SEED = 2
TRACE_LIMIT = 5 * 1024 * 1024

SCENARIOS = ["none", "false_telemetry", "rogue_courier", "station_outage", "forged_authority", "task_cycling",
             "contradictory", "injection", "replay_flood"]
DEFENSES = ["none", "auth", "authz", "validate", "fresh", "monitor", "fallback", "full"]
SC_TITLE = {"none": "Штатный режим", "false_telemetry": "Недостоверные сообщения",
            "rogue_courier": "Подменённая цель агента", "station_outage": "Отказ станции и потери",
            "forged_authority": "Подмена полномочий", "task_cycling": "Циклическая передача",
            "contradictory": "Противоречивые указания", "injection": "Внедрение инструкций",
            "replay_flood": "Повтор и лавина сообщений"}
DF_TITLE = {"none": "без защиты", "auth": "аутентифи-\nкация", "authz": "авториза-\nция", "validate": "проверка\nданных",
            "fresh": "свежесть,\nэпохи", "monitor": "контроль\nисполнения", "fallback": "запасной\nпуть",
            "full": "все слои"}
KEYS = ["completion_rate", "on_time_rate", "urgent_on_time_rate", "avg_delivery_time", "p95_delivery_time",
        "makespan", "ticks", "stranded", "failed_pickups", "reassignments", "distance_total", "msgs_total",
        "bytes_total", "msgs_blocked", "coord_cpu_ms", "cpu_ms.defense", "false_confirm", "undelivered",
        "atk_msgs", "atk_instr", "atk_instr_exec", "atk_lost_msgs", "def_alarms", "def_blocked", "def_tp", "def_fp",
        "def_neutralized", "def_gate_denied", "def_reconciled", "def_exec_timeouts", "def_ttl_escalations",
        "def_parked", "def_quarantined", "def_quarantined_honest", "def_untrusted", "def_station_fallbacks",
        "def_client_resends"]

# Оформление (эталонная палитра навыка dataviz: слот 1 — синий, слот 2 — оранжевый)
C_NONE, C_FULL, C_BASE = "#eb6834", "#2a78d6", "#898781"
INK, INK2, GRID, AXIS, SURFACE = "#0b0b0b", "#52514e", "#e1e0d9", "#c3c2b7", "#fcfcfb"
SEQ = ["#cde2fb", "#b7d3f6", "#9ec5f4", "#86b6ef", "#6da7ec", "#5598e7", "#3987e5", "#2a78d6", "#256abf",
       "#1c5cab", "#184f95", "#104281", "#0d366b"]
plt.rcParams.update({
    "font.family": "DejaVu Sans", "font.size": 9, "axes.edgecolor": AXIS, "axes.labelcolor": INK2,
    "axes.titlecolor": INK, "axes.titlesize": 10, "xtick.color": INK2, "ytick.color": INK2, "axes.facecolor": SURFACE,
    "figure.facecolor": SURFACE, "savefig.facecolor": SURFACE, "axes.grid": False, "legend.frameon": False,
    "axes.spines.top": False, "axes.spines.right": False,
})


def run_one(scenario: str, defense: str, seed: int, trace: bool = False):
    sim = Simulation(SimConfig(seed=seed, scenario=scenario, defense=defense, trace_messages=trace))
    ensure_integrity(sim)
    if trace:
        install_truth_probe(sim)
    m = sim.run()
    m.update(integrity_metrics(sim))
    return m, sim


def stats(vals: list[float]) -> tuple[float, float, float]:
    mean = statistics.fmean(vals)
    sd = statistics.stdev(vals) if len(vals) > 1 else 0.0
    return mean, sd, 1.96 * sd / math.sqrt(len(vals)) if len(vals) > 1 else 0.0


# ---------------------------------------------------------------------------------------------
def e1_matrix(seeds) -> dict:
    rows = []
    t0 = time.time()
    for sc in SCENARIOS:
        for df in DEFENSES:
            for s in seeds:
                m, _ = run_one(sc, df, s)
                rows.append({"scenario": sc, "defense": df, "seed": s, **flatten(m)})
        print(f"  {sc:<17} готово, {time.time() - t0:.0f} с")
    save_csv(rows, RES / "runs.csv")
    agg: dict = {}
    for sc in SCENARIOS:
        for df in DEFENSES:
            rs = [r for r in rows if r["scenario"] == sc and r["defense"] == df]
            agg[(sc, df)] = {k: stats([float(r.get(k, 0) or 0) for r in rs]) for k in KEYS}
            det = [r["def_detect_tick"] for r in rs if r.get("def_detect_tick") not in (None, "")]
            agg[(sc, df)]["detect_tick"] = stats(det) if det else (float("nan"),) * 3
            agg[(sc, df)]["detected_runs"] = (len(det), 0, 0)
            agg[(sc, df)]["_rows"] = rs
    with open(RES / "summary.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["scenario", "defense", "n"] + [f"{k}_{s}" for k in KEYS for s in ("mean", "ci95")])
        for (sc, df), a in agg.items():
            w.writerow([sc, df, len(a["_rows"])] + [round(v, 4) for k in KEYS for v in (a[k][0], a[k][2])])
    return agg


def tables(agg: dict, seeds) -> None:
    # Повторная оценка устойчивости: без защиты / со всеми слоями
    with open(RES / "resilience.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["scenario", "defense", "completion", "completion_ci", "on_time", "on_time_ci", "avg_delivery",
                    "undelivered", "false_confirm", "stranded", "failed_pickups", "reassignments", "msgs_total",
                    "R_completion", "R_on_time"])
        for sc in SCENARIOS:
            for df in ("none", "full"):
                a, base = agg[(sc, df)], agg[("none", df)]
                w.writerow([sc, df, round(a["completion_rate"][0], 4), round(a["completion_rate"][2], 4),
                            round(a["on_time_rate"][0], 4), round(a["on_time_rate"][2], 4),
                            round(a["avg_delivery_time"][0], 2), round(a["undelivered"][0], 2),
                            round(a["false_confirm"][0], 2), round(a["stranded"][0], 2),
                            round(a["failed_pickups"][0], 2), round(a["reassignments"][0], 2),
                            round(a["msgs_total"][0], 1),
                            round(a["completion_rate"][0] / base["completion_rate"][0], 3),
                            round(a["on_time_rate"][0] / base["on_time_rate"][0], 3)])
    # Вклад слоёв: доля доставленных в срок и доля доставленных
    for key, name in (("on_time_rate", "layers_on_time.csv"), ("completion_rate", "layers_completion.csv")):
        with open(RES / name, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["scenario"] + DEFENSES)
            for sc in SCENARIOS:
                w.writerow([sc] + [round(agg[(sc, df)][key][0], 4) for df in DEFENSES])
    # Ложные срабатывания и накладные расходы в штатном режиме
    base_rows = {r["seed"]: r for r in agg[("none", "none")]["_rows"]}
    with open(RES / "false_positives.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["defense", "completion", "on_time", "avg_delivery", "def_alarms", "def_fp_msgs",
                    "quarantined_honest", "seeds_with_changed_on_time", "msgs_total", "msgs_overhead_pct",
                    "bytes_total", "bytes_overhead_pct", "coord_cpu_ms", "cpu_defense_ms"])
        b = agg[("none", "none")]
        for df in DEFENSES:
            a = agg[("none", df)]
            changed = sum(1 for r in a["_rows"] if r["on_time_rate"] != base_rows[r["seed"]]["on_time_rate"])
            w.writerow([df, round(a["completion_rate"][0], 4), round(a["on_time_rate"][0], 4),
                        round(a["avg_delivery_time"][0], 2), round(a["def_alarms"][0], 3), round(a["def_fp"][0], 3),
                        round(a["def_quarantined_honest"][0], 3), changed, round(a["msgs_total"][0], 1),
                        round(100 * (a["msgs_total"][0] / b["msgs_total"][0] - 1), 2), round(a["bytes_total"][0]),
                        round(100 * (a["bytes_total"][0] / b["bytes_total"][0] - 1), 2),
                        round(a["coord_cpu_ms"][0], 2), round(a["cpu_ms.defense"][0], 2)])
    # Обнаружение: какой слой сколько вредоносных воздействий отсёк, ложные срабатывания под атакой
    with open(RES / "detection.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["scenario", "defense", "atk_msgs", "def_tp_msgs", "def_fp_msgs", "msg_detection_rate",
                    "instr", "instr_executed", "detected_runs", "runs", "detect_tick", "quarantined",
                    "quarantined_honest", "station_fallbacks", "client_resends", "def_alarms"])
        for sc in SCENARIOS[1:]:
            for df in DEFENSES[1:]:
                a = agg[(sc, df)]
                atk = a["atk_msgs"][0]
                w.writerow([sc, df, round(atk, 1), round(a["def_tp"][0], 1), round(a["def_fp"][0], 2),
                            round(a["def_tp"][0] / atk, 3) if atk else "", round(a["atk_instr"][0], 1),
                            round(a["atk_instr_exec"][0], 2), a["detected_runs"][0], len(seeds),
                            "" if math.isnan(a["detect_tick"][0]) else round(a["detect_tick"][0], 1),
                            round(a["def_quarantined"][0], 2), round(a["def_quarantined_honest"][0], 2),
                            round(a["def_station_fallbacks"][0], 2), round(a["def_client_resends"][0], 2),
                            round(a["def_alarms"][0], 1)])


# ---------------------------------------------------------------------------------------------
def save_trace(sim, path: Path) -> str:
    """Сохранить трассу; если файл больше ~5 МБ, убрать из него сообщения телеметрии (снимки истины остаются)."""
    sim.trace.save_jsonl(path)
    if path.stat().st_size <= TRACE_LIMIT:
        return "полная"
    keep = [e for e in sim.trace.events if not (e["kind"] == "msg" and e.get("proto") == "telemetry")]
    keep.insert(1, {"t": 0, "kind": "trace_note", "note": "сообщения телеметрии удалены из-за размера трассы"})
    with open(path, "w", encoding="utf-8") as f:
        for e in keep:
            f.write(json.dumps(e, ensure_ascii=False, default=str) + "\n")
    return "без телеметрии"


def e2_traces() -> dict:
    TRACES.mkdir(parents=True, exist_ok=True)
    out, curves = {}, {}
    for sc in SCENARIOS:
        for df in ("none", "full"):
            m, sim = run_one(sc, df, TRACE_SEED, trace=True)
            path = TRACES / f"{sc}__{df}.jsonl"
            mode = save_trace(sim, path)
            r = analyze.analyze(analyze.load(path), max_examples=4)
            r.update(file=path.name, size_kb=round(path.stat().st_size / 1024), trace_mode=mode,
                     completion=m["completion_rate"], on_time=m["on_time_rate"])
            out[path.name] = r
            curves[(sc, df)] = sorted(o["delivered"] for o in sim.metrics.orders.values() if o["delivered"] is not None)
            print(f"  {path.name:<34} {r['size_kb']:>5} КБ ({mode}), отказов: "
                  + ", ".join(f"{k}={v}" for k, v in r["failures"].items() if v))
    with open(RES / "trace_failures.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["trace", "scenario", "defense", "size_kb", "completion", "on_time", *analyze.TAXONOMY,
                    *[f"cause:{c}" for c in analyze.CAUSES], "suspicious_notes"])
        for name, r in out.items():
            w.writerow([name, r["scenario"], r["defense"], r["size_kb"], r["completion"], r["on_time"],
                        *[r["failures"][k] for k in analyze.TAXONOMY],
                        *[r["causes"].get(c, 0) for c in analyze.CAUSES], r["suspicious_notes"]])
    (RES / "trace_examples.json").write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                                             encoding="utf-8")
    return curves


# ---------------------------------------------------------------------------------------------
def _style(ax, grid_axis="x"):
    ax.grid(axis=grid_axis, color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    ax.tick_params(length=0)


def fig_before_after(agg: dict) -> None:
    scs = SCENARIOS[1:]
    y = np.arange(len(scs))
    fig, axes = plt.subplots(1, 2, figsize=(10, 4.9), sharey=True)
    for ax, key, title in ((axes[0], "completion_rate", "Доля доставленных заказов"),
                           (axes[1], "on_time_rate", "Доля доставленных в срок")):
        h = 0.36
        for off, df, color, label in ((-h / 2 - 0.02, "none", C_NONE, "атака без защиты"),
                                      (h / 2 + 0.02, "full", C_FULL, "атака, все слои защиты")):
            means = [agg[(sc, df)][key][0] for sc in scs]
            cis = [agg[(sc, df)][key][2] for sc in scs]
            ax.barh(y + off, means, height=h, color=color, label=label, zorder=2)
            ax.errorbar(means, y + off, xerr=cis, fmt="none", ecolor=INK2, elinewidth=0.8, capsize=2, zorder=3)
        base = agg[("none", "none")][key][0]
        ax.axvline(base, color=C_BASE, linewidth=1.2, zorder=4, label="штатный режим")
        ax.text(base - 0.005, -0.75, f"штатный режим {base:.2f}", color=INK2, fontsize=8, ha="right", va="center")
        ax.set_xlim(0.5, 1.02)
        ax.set_ylim(len(scs) - 0.45, -1.1)
        ax.set_title(title, loc="left")
        _style(ax)
    axes[0].set_yticks(y, [SC_TITLE[s] for s in scs])
    handles, labels = axes[1].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncol=3, fontsize=8.5, bbox_to_anchor=(0.6, 1.0))
    fig.text(0.01, 0.01, "Среднее по 25 seed, отрезки — 95% доверительный интервал. Ось начинается с 0,5.",
             color=INK2, fontsize=7.5)
    fig.tight_layout(rect=(0, 0.03, 1, 0.94))
    fig.savefig(FIG / "before_after.png", dpi=150)
    plt.close(fig)


def fig_heatmap(agg: dict) -> None:
    from matplotlib.colors import LinearSegmentedColormap
    cmap = LinearSegmentedColormap.from_list("seq", SEQ)
    data = np.array([[agg[(sc, df)]["on_time_rate"][0] for df in DEFENSES] for sc in SCENARIOS])
    fig, ax = plt.subplots(figsize=(9.5, 5.2))
    vmin = 0.5
    im = ax.imshow(data, cmap=cmap, vmin=vmin, vmax=1.0, aspect="auto")
    for i in range(data.shape[0]):
        for j in range(data.shape[1]):
            v = data[i, j]
            ax.text(j, i, f"{v:.2f}", ha="center", va="center", fontsize=8,
                    color="#ffffff" if v > 0.82 else INK)
    ax.set_xticks(range(len(DEFENSES)), [DF_TITLE[d] for d in DEFENSES], fontsize=8)
    ax.set_yticks(range(len(SCENARIOS)), [SC_TITLE[s] for s in SCENARIOS])
    ax.tick_params(length=0)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.set_xticks(np.arange(-0.5, len(DEFENSES)), minor=True)
    ax.set_yticks(np.arange(-0.5, len(SCENARIOS)), minor=True)
    ax.grid(which="minor", color=SURFACE, linewidth=2)
    ax.tick_params(which="minor", length=0)
    ax.set_title("Доля заказов, доставленных в срок: сценарий × защитный слой (среднее по 25 seed)", loc="left")
    cb = fig.colorbar(im, ax=ax, fraction=0.03, pad=0.02)
    cb.outline.set_visible(False)
    cb.ax.tick_params(length=0, labelsize=8)
    fig.tight_layout()
    fig.savefig(FIG / "layers_heatmap.png", dpi=150)
    plt.close(fig)


def fig_timeline(curves: dict) -> None:
    pick = ["rogue_courier", "station_outage", "forged_authority", "injection"]
    fig, axes = plt.subplots(2, 2, figsize=(10, 6.2), sharex=True, sharey=True)
    base = curves[("none", "none")]
    horizon = 700
    for ax, sc in zip(axes.flat, pick):
        if sc == "station_outage":
            ax.axvspan(50, 350, color=GRID, alpha=0.6, linewidth=0, zorder=0)
            ax.text(200, 2, "station-1 недоступна", color=INK2, fontsize=8, ha="center")
        for data, color, label, lw in ((base, C_BASE, "штатный режим", 1.4),
                                       (curves[(sc, "none")], C_NONE, "атака без защиты", 2.0),
                                       (curves[(sc, "full")], C_FULL, "атака, все слои защиты", 2.0)):
            xs = [0] + [t for t in data if t <= horizon] + [horizon]
            ys = [0] + list(range(1, len(xs) - 1)) + [len(xs) - 2]
            ax.step(xs, ys, where="post", color=color, linewidth=lw, label=label)
        n_none = sum(1 for t in curves[(sc, "none")] if t <= horizon)
        ax.text(horizon, n_none, f" {n_none}", color=INK2, fontsize=8, va="center")
        ax.set_title(SC_TITLE[sc], loc="left")
        ax.set_xlim(0, horizon + 40)
        _style(ax, "y")
    for ax in axes[1]:
        ax.set_xlabel("такт")
    fig.supylabel("доставлено заказов (нарастающим итогом)", color=INK2, fontsize=9)
    handles, labels = axes[0, 0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncol=3, fontsize=8.5)
    same = [f"«{SC_TITLE[sc]}»" for sc in pick if curves[(sc, "full")] == base]
    note = f"Один прогон, seed={TRACE_SEED}; всего 60 заказов."
    if same:
        note += " Линия «все слои защиты» совпадает со штатной: " + ", ".join(same) + "."
    fig.text(0.01, 0.005, note, color=INK2, fontsize=7.5)
    fig.tight_layout(rect=(0, 0.02, 1, 0.95))
    fig.savefig(FIG / "timeline.png", dpi=150)
    plt.close(fig)


def fig_overhead(agg: dict) -> None:
    dfs = DEFENSES
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.6), sharey=True)
    y = np.arange(len(dfs))
    for ax, key, scale, title, unit in ((axes[0], "bytes_total", 1 / 1024, "Объём сообщений за прогон, КБ", "КБ"),
                                        (axes[1], "coord_cpu_ms", 1.0, "Время CPU на координацию и защиту за прогон, мс", "мс")):
        vals = [agg[("none", df)][key][0] * scale for df in dfs]
        cis = [agg[("none", df)][key][2] * scale for df in dfs]
        colors = [C_BASE if df == "none" else C_FULL for df in dfs]
        ax.barh(y, vals, height=0.55, color=colors, zorder=2)
        ax.errorbar(vals, y, xerr=cis, fmt="none", ecolor=INK2, elinewidth=0.8, capsize=2, zorder=3)
        for yi, v, ci in zip(y, vals, cis):
            ax.text(v + ci, yi, f"  {v:.1f} {unit}", va="center", fontsize=7.5, color=INK2)
        ax.set_title(title, loc="left", fontsize=9)
        ax.set_xlim(0, max(vals) * 1.3)
        _style(ax)
    axes[0].set_yticks(y, [DF_TITLE[d].replace("-" + chr(10), "").replace(chr(10), " ") for d in dfs])
    axes[0].invert_yaxis()
    b = agg[("none", "none")]
    base_on_time = {r["seed"]: r["on_time_rate"] for r in b["_rows"]}
    changed = sum(1 for df in dfs[1:] for r in agg[("none", df)]["_rows"] if r["on_time_rate"] != base_on_time[r["seed"]])
    same_msgs = all(agg[("none", df)]["msgs_total"][0] == b["msgs_total"][0] for df in dfs)
    note = ("доля доставленных в срок не изменилась ни в одном прогоне" if changed == 0
            else f"доля доставленных в срок изменилась в {changed} прогонах из {(len(dfs) - 1) * len(b['_rows'])}")
    if same_msgs:
        note += "; число сообщений не изменилось"
    fig.text(0.01, 0.01, f"Штатный режим (без атак), {len(b['_rows'])} seed, отрезки — 95% ДИ; {note}.",
             color=INK2, fontsize=7.5)
    fig.tight_layout(rect=(0, 0.05, 1, 1))
    fig.savefig(FIG / "overhead.png", dpi=150)
    plt.close(fig)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=25)
    a = ap.parse_args()
    seeds = list(range(1, a.seeds + 1))
    for d in (RES, FIG, TRACES):
        d.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    print(f"E1: матрица {len(SCENARIOS)} сценариев × {len(DEFENSES)} защит × {len(seeds)} seed")
    agg = e1_matrix(seeds)
    tables(agg, seeds)
    print("E2: показательные трассы и их анализ")
    curves = e2_traces()
    print("Графики")
    fig_before_after(agg)
    fig_heatmap(agg)
    fig_timeline(curves)
    fig_overhead(agg)
    print(f"Готово за {time.time() - t0:.0f} с. Результаты: {RES}, графики: {FIG}, трассы: {TRACES}")


if __name__ == "__main__":
    main()
