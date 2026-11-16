"""Рисунки и базовая таблица показателей ЛР1 (воспроизводимо).

    python lab1/docs/make_figures.py

Строит lab1/figures/*.png и lab1/results/baseline.csv и печатает числа, которые используются в отчёте.
Плагины ЛР2–ЛР4 намеренно не загружаются: таблица полномочий, диаграммы и прогоны отражают только
ядро ЛР1 (allocation=central, charging=naive, scenario=none, defense=none).
Все рисунки рассчитаны на ширину ~8.4 дюйма (в отчёте DOCX вставляются шириной 16 см).
"""
from __future__ import annotations

import sys
import textwrap
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import patheffects  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch, Patch  # noqa: E402

from lab1.mas import SimConfig, Simulation, roles  # noqa: E402
from lab1.mas.agents.courier import STATES  # noqa: E402
from lab1.mas.experiment import aggregate, run_batch, save_csv  # noqa: E402
from lab1.mas.metrics import LABELS  # noqa: E402

FIG = ROOT / "lab1" / "figures"
RES = ROOT / "lab1" / "results"
SEEDS = list(range(1, 31))
DEMO_SEED = 1          # прогон для наблюдаемого графа, карты, диаграммы заказа и занятости флота
OFFLINE_TICKS = 60     # длительность отключения курьера в проверочном прогоне сторожевого таймера
FIG_W = 8.4            # ширина рисунков, дюймы

# --- оформление (эталонная палитра, светлая тема) ---
INK, INK2, MUTED, GRIDC, AXIS = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"
BLUE, ORANGE, AQUA, YELLOW, MAGENTA, GREEN, VIOLET, RED = (
    "#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948")
CRIT = "#d03b3b"
NODE = "#f3f2ee"
PROTO_ORDER = ["order", "assign", "order-status", "telemetry", "charge"]
PROTO_COLORS = dict(zip(PROTO_ORDER, [BLUE, ORANGE, AQUA, YELLOW, MAGENTA]))
ROLE_RU = {"client": "Клиент", "dispatcher": "Диспетчер", "courier": "Курьер", "station": "Станция"}

plt.rcParams.update({
    "font.family": "DejaVu Sans", "font.size": 9, "axes.edgecolor": AXIS, "axes.labelcolor": INK2,
    "xtick.color": INK2, "ytick.color": INK2, "axes.titlecolor": INK, "figure.facecolor": "white",
    "savefig.facecolor": "white", "axes.spines.top": False, "axes.spines.right": False,
})


def tint(color: str, a: float) -> tuple:
    """Смешать цвет с белым: a=0 — белый, a=1 — исходный цвет."""
    c = color.lstrip("#")
    r, g, b = (int(c[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return (1 - a + a * r, 1 - a + a * g, 1 - a + a * b)


def canvas(w: float, h: float, width_in: float = FIG_W):
    """Холст в условных единицах w×h; итоговая ширина рисунка — width_in дюймов."""
    s = width_in / w
    fig = plt.figure(figsize=(w * s, h * s))
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, w)
    ax.set_ylim(0, h)
    ax.set_aspect("equal")
    ax.axis("off")
    return fig, ax


def save(fig, name: str) -> None:
    FIG.mkdir(parents=True, exist_ok=True)
    fig.savefig(FIG / name, dpi=200, bbox_inches="tight", pad_inches=0.08)
    plt.close(fig)
    print(f"  рисунок: lab1/figures/{name}")


def rbox(ax, cx, cy, w, h, text="", fc="white", ec=INK2, lw=1.1, fs=9, weight="normal", r=0.12,
         z=2, ls="-", color=INK):
    p = FancyBboxPatch((cx - w / 2, cy - h / 2), w, h, boxstyle=f"round,pad=0,rounding_size={r}",
                       fc=fc, ec=ec, lw=lw, zorder=z, ls=ls)
    ax.add_patch(p)
    if text:
        ax.text(cx, cy, text, ha="center", va="center", fontsize=fs, color=color, weight=weight, zorder=z + 1,
                linespacing=1.2)
    return p


def label(ax, x, y, text, fs=8, color=INK, ha="center", va="center", bg="white", ec="none", z=6,
          weight="normal", pad=0.18, ls=1.15):
    bbox = dict(boxstyle=f"round,pad={pad}", fc=bg, ec=ec, lw=1.0) if bg else None
    return ax.text(x, y, text, fontsize=fs, color=color, ha=ha, va=va, zorder=z, weight=weight,
                   bbox=bbox, linespacing=ls)


def arrow(ax, p1, p2, color=INK2, lw=1.3, bend=0.0, style="-|>", ls="-", ms=11, z=4,
          patchA=None, patchB=None, shrinkA=1, shrinkB=1):
    """Стрелка p1 -> p2; bend — смещение середины дуги влево от направления движения (в единицах осей).
    Возвращает точку середины дуги (для подписи)."""
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    length = (dx * dx + dy * dy) ** 0.5 or 1.0
    rad = -2 * bend / length
    a = FancyArrowPatch(p1, p2, arrowstyle=style, mutation_scale=ms, color=color, lw=lw, ls=ls,
                        connectionstyle=f"arc3,rad={rad}", zorder=z, patchA=patchA, patchB=patchB,
                        shrinkA=shrinkA, shrinkB=shrinkB)
    ax.add_patch(a)
    nx, ny = -dy / length, dx / length
    return ((p1[0] + p2[0]) / 2 + nx * bend, (p1[1] + p2[1]) / 2 + ny * bend)


def poly(ax, pts, color=INK2, lw=1.3, ls="-", z=4):
    """Ломаная со стрелкой на конце."""
    xs, ys = zip(*pts)
    ax.plot(xs[:-1], ys[:-1], color=color, lw=lw, ls=ls, zorder=z, solid_capstyle="butt")
    ax.annotate("", xy=pts[-1], xytext=pts[-2], zorder=z,
                arrowprops=dict(arrowstyle="-|>", color=color, lw=lw, ls=ls, mutation_scale=11,
                                shrinkA=0, shrinkB=0))


# ============================================================================ прогоны
def baseline() -> list[dict]:
    rows = run_batch({"baseline": {}}, SEEDS)
    RES.mkdir(parents=True, exist_ok=True)
    save_csv(rows, RES / "baseline.csv")
    print("  таблица: lab1/results/baseline.csv")
    return rows


def demo_run(seed=DEMO_SEED, frames=False) -> Simulation:
    sim = Simulation(SimConfig(seed=seed, record_frames=frames, trace_messages=True))
    sim.run()
    return sim


def msgs(sim) -> list[dict]:
    return [e for e in sim.trace.events if e["kind"] == "msg"]


# ============================================================================ карта среды
def fig_env_map(sim) -> None:
    cfg, world = sim.cfg, sim.world
    fig, ax = plt.subplots(figsize=(6.4, 6.9))
    ax.set_xlim(-0.6, cfg.width - 0.4)
    ax.set_ylim(-0.6, cfg.height - 0.4)
    ax.set_aspect("equal")
    for i in range(cfg.width + 1):
        ax.axvline(i - 0.5, color=GRIDC, lw=0.6, zorder=0)
    for j in range(cfg.height + 1):
        ax.axhline(j - 0.5, color=GRIDC, lw=0.6, zorder=0)
    for o in sim.orders:
        urgent, heavy = o.priority == "urgent", o.weight > 5
        ax.scatter(*o.dropoff, s=60 if heavy else 24, marker="o", zorder=3,
                   facecolor=RED if urgent else "white", edgecolor=RED if urgent else INK2, lw=1.0)
    couriers = sim.couriers
    for k, h in enumerate(world.hubs):
        ax.scatter(*h, s=230, marker="s", color=BLUE, zorder=4)
        start = [c.id.split("-")[1] for i, c in enumerate(couriers) if world.hubs[i % len(world.hubs)] == h]
        ax.annotate(f"Т{k + 1}\nкурьеры {', '.join(start)}", h, xytext=(-11, 0), textcoords="offset points",
                    ha="right", va="center", fontsize=7.8, color=INK, zorder=6, linespacing=1.1,
                    path_effects=[patheffects.withStroke(linewidth=3, foreground="white")])
    for sid, p in world.station_pos.items():
        ax.scatter(*p, s=260, marker="^", color=AQUA, edgecolor=INK2, lw=0.8, zorder=4)
        ax.annotate(f"{sid}\n{cfg.station_slots} места", p, xytext=(12, 0), textcoords="offset points",
                    ha="left", va="center", fontsize=7.8, color=INK, zorder=6, linespacing=1.1,
                    path_effects=[patheffects.withStroke(linewidth=3, foreground="white")])
    ax.set_xticks(range(0, cfg.width, 2))
    ax.set_yticks(range(0, cfg.height, 2))
    ax.tick_params(length=0, labelsize=8)
    for s in ax.spines.values():
        s.set_visible(False)
    handles = [
        Line2D([], [], marker="s", ls="", color=BLUE, ms=9, label="точка выдачи (склад, ресторан)"),
        Line2D([], [], marker="^", ls="", color=AQUA, mec=INK2, ms=10, label="зарядная станция"),
        Line2D([], [], marker="o", ls="", mfc="white", mec=INK2, ms=5.5, label="место доставки: обычный заказ"),
        Line2D([], [], marker="o", ls="", mfc=RED, mec=RED, ms=5.5, label="место доставки: срочный заказ"),
        Line2D([], [], marker="o", ls="", mfc="white", mec=INK2, ms=8.5, label="крупный маркер: груз > 5 кг"),
    ]
    ax.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, -0.04), ncol=2, frameon=False, fontsize=8)
    ax.set_title(f"Сетка {cfg.width}×{cfg.height}: точки выдачи, станции и {len(sim.orders)} заказов (seed {cfg.seed})",
                 fontsize=9.5, loc="left")
    save(fig, "env_map.png")


# ============================================================================ диаграмма ролей
def fig_roles(sim) -> None:
    mult = {"client": "1 агент", "dispatcher": "1 агент", "courier": f"N = {sim.cfg.n_couriers} агентов",
            "station": f"M = {sim.cfg.n_stations} агента"}
    tone = {"client": BLUE, "dispatcher": ORANGE, "courier": AQUA, "station": VIOLET}
    texts = {}
    for role, spec in roles.ROLES.items():
        body = ["Цели:"] + [f"• {g}" for g in spec["goals"]]
        body += ["Обязанности (действия):"] + [f"• {a}" for a in spec["actions"]]
        body += ["Полномочия:"] + [f"• {a}" for a in spec["authority"]]
        texts[role] = [ln for s in body for ln in
                       (textwrap.wrap(s, 44, subsequent_indent="   ") if s.startswith("•") else [s])]
    bw, gap_x, gap_y, margin = 6.0, 4.0, 2.4, 0.1
    W = 2 * margin + 2 * bw + gap_x
    s = FIG_W / W
    fs = 8
    line_h = 1.32 * fs / 72 / s
    head_h, pad = 2.6 * 9 / 72 / s, 0.2
    hgt = {r: head_h + pad * 2 + line_h * len(t) for r, t in texts.items()}
    top_h = max(hgt["client"], hgt["dispatcher"])
    bot_h = max(hgt["courier"], hgt["station"])
    H = 2 * margin + top_h + bot_h + gap_y
    fig, ax = canvas(W, H)
    xl, xr = margin + bw / 2, margin + bw + gap_x + bw / 2
    yt, yb = H - margin - top_h / 2, margin + bot_h / 2
    card = {"client": (xl, yt, top_h), "dispatcher": (xr, yt, top_h), "courier": (xr, yb, bot_h),
            "station": (xl, yb, bot_h)}
    for role, (cx, cy, bh) in card.items():
        rbox(ax, cx, cy, bw, bh, fc="white", ec=tone[role], lw=1.4, r=0.15)
        ax.add_patch(FancyBboxPatch((cx - bw / 2, cy + bh / 2 - head_h), bw, head_h,
                                    boxstyle="round,pad=0,rounding_size=0.15", fc=tint(tone[role], 0.18),
                                    ec="none", zorder=2.5))
        ax.text(cx - bw / 2 + 0.2, cy + bh / 2 - head_h * 0.36, roles.ROLES[role]["title"], fontsize=9.5,
                weight="bold", color=INK, va="center", zorder=4)
        ax.text(cx - bw / 2 + 0.2, cy + bh / 2 - head_h * 0.76, f"роль {role} · {mult[role]}", fontsize=7.8,
                color=INK2, va="center", zorder=4)
        y = cy + bh / 2 - head_h - pad
        for ln in texts[role]:
            head = ln.endswith(":") and not ln.startswith(("•", " "))
            ax.text(cx - bw / 2 + 0.2, y, ln, fontsize=fs, color=INK, va="top", ha="left", zorder=4,
                    weight="bold" if head else "normal")
            y -= line_h
    gx0, gx1 = margin + bw, margin + bw + gap_x
    gm = (gx0 + gx1) / 2
    off = 0.32

    def hlink(y, txt_lr, txt_rl, txt_k):
        arrow(ax, (gx0, y + off), (gx1, y + off), color=INK2, lw=1.4)
        arrow(ax, (gx1, y - off), (gx0, y - off), color=INK2, lw=1.4)
        label(ax, gm, y + off + 0.15, txt_lr, va="bottom")
        label(ax, gm, y - off - 0.15, txt_rl, va="top")
        label(ax, gm, y - off - 2.0, txt_k, color=INK2, bg=None, fs=7.8)

    hlink(yt, "заказывает\n(протокол order)", "подтверждает приём,\nуведомляет\nо доставке", "кратность 1 : 1")
    hlink(yb, "обслуживает:\nвыделяет место,\nставит в очередь,\nзаряжает",
          "запрашивает место,\nсообщает об\nокончании зарядки", "кратность N : M")
    y0, y1 = yt - top_h / 2, yb + bot_h / 2
    arrow(ax, (xr - off, y0), (xr - off, y1), color=INK2, lw=1.4)
    arrow(ax, (xr + off, y1), (xr + off, y0), color=INK2, lw=1.4)
    label(ax, xr - off - 0.15, (y0 + y1) / 2,
          "назначает и отзывает заказы,\nконтролирует исполнение\n(сторожевой таймер); 1 : N", ha="right")
    label(ax, xr + off + 0.15, (y0 + y1) / 2, "соглашается / отказывает,\nсообщает статус\nи телеметрию", ha="left")
    save(fig, "roles.png")


# ============================================================================ проектный граф коммуникаций
def fig_comm_design(sim) -> None:
    W, H = 16.0, 8.1
    fig, ax = canvas(W, H)
    yc = 5.35
    pos = {"client": (1.25, yc), "dispatcher": (5.6, yc), "courier": (10.4, yc), "station": (14.75, yc)}
    mult = {"client": "×1", "dispatcher": "×1", "courier": f"×N ({sim.cfg.n_couriers})",
            "station": f"×M ({sim.cfg.n_stations})"}
    nodes = {}
    for r, (x, y) in pos.items():
        nodes[r] = rbox(ax, x, y, 2.3, 1.15, f"{ROLE_RU[r]}\n{mult[r]}", fc=NODE, ec=INK2, lw=1.2, fs=9.5,
                        weight="bold", r=0.14, z=5)
    groups = defaultdict(list)
    for (snd, rcv, proto), perfs in sorted(roles.PERMISSIONS.items(),
                                           key=lambda kv: PROTO_ORDER.index(kv[0][2])
                                           if kv[0][2] in PROTO_ORDER else 99):
        groups[(snd, rcv)].append((proto, sorted(perfs)))
    for (snd, rcv), items in groups.items():
        for k, (proto, perfs) in enumerate(items):
            bend = 1.05 + 0.85 * k
            color = PROTO_COLORS.get(proto, INK2)
            mid = arrow(ax, pos[snd], pos[rcv], color=color, lw=1.7, bend=bend, patchA=nodes[snd], patchB=nodes[rcv])
            label(ax, mid[0], mid[1], f"{proto}:\n{', '.join(p.upper() for p in perfs)}", fs=7.8, ec=color,
                  pad=0.22)
    ax.text(0.1, 2.05, "Протоколы (roles.PROTOCOLS):", fontsize=8.5, weight="bold", color=INK)
    for i, proto in enumerate([p for p in PROTO_ORDER if p in roles.PROTOCOLS]):
        y = 1.6 - 0.38 * i
        ax.plot([0.15, 0.6], [y, y], color=PROTO_COLORS[proto], lw=3, solid_capstyle="round")
        ax.text(0.75, y, f"{proto} — {roles.PROTOCOLS[proto]}", fontsize=8, va="center", color=INK)
    ax.text(0.1, H - 0.05, "Стрелка — разрешённое направление «роль-отправитель → роль-получатель» в протоколе;\n"
            "подпись — допустимые перформативы (roles.PERMISSIONS, только ядро ЛР1)", fontsize=8, color=INK2,
            va="top", linespacing=1.3)
    save(fig, "comm_design.png")


# ============================================================================ наблюдаемый граф коммуникаций
def fig_comm_observed(sim) -> dict:
    events = msgs(sim)
    pair = Counter()
    by_role_proto = defaultdict(Counter)
    for e in events:
        a, b = sorted((e["from"], e["to"]))
        pair[(a, b)] += 1
        by_role_proto[(sim.role_of(e["from"]), sim.role_of(e["to"]))][e["proto"]] += 1
    fig = plt.figure(figsize=(FIG_W, 9.0))
    gs = fig.add_gridspec(2, 1, height_ratios=[1.55, 1], hspace=0.22)
    ax = fig.add_subplot(gs[0])
    ax.set_xlim(-0.2, 14.2)
    ax.set_ylim(-4.6, 4.6)
    ax.set_aspect("equal")
    ax.set_anchor("W")
    ax.axis("off")
    couriers = [c.id for c in sim.couriers]
    stations = [s.id for s in sim.stations]
    p = {"client": (0.9, 0.0), "dispatcher": (4.4, 0.0)}
    for i, cid in enumerate(couriers):
        p[cid] = (8.6, 4.1 - 8.2 * i / (len(couriers) - 1))
    for j, sid in enumerate(stations):
        p[sid] = (13.1, 2.3 - 4.6 * j / max(1, len(stations) - 1))
    mx = max(pair.values())
    st_total = Counter()
    for (a, b), cnt in pair.items():
        (x1, y1), (x2, y2) = p[a], p[b]
        ax.plot([x1, x2], [y1, y2], color=INK2, lw=0.6 + 6.5 * cnt / mx, alpha=0.5, solid_capstyle="round", zorder=1)
        rset = frozenset({sim.role_of(a), sim.role_of(b)})
        if rset == {"courier", "station"}:   # подписи для них — суммой у станции
            st_total[a if sim.role_of(a) == "station" else b] += cnt
            continue
        t = 0.7 if rset == {"dispatcher", "courier"} else 0.5
        if x1 > x2:
            t = 1 - t
        ax.text(x1 + (x2 - x1) * t, y1 + (y2 - y1) * t, str(cnt), fontsize=7.5, ha="center", va="center", color=INK,
                zorder=4, bbox=dict(boxstyle="round,pad=0.12", fc="white", ec="none"))
    for sid, cnt in st_total.items():
        x, y = p[sid]
        up = y > 0
        ax.text(x, y + (0.42 if up else -0.42), f"всего {cnt} сообщ.\n(по 3 на сеанс)", fontsize=7.3, ha="center",
                va="bottom" if up else "top", color=INK2, linespacing=1.1)
    for node, (x, y) in p.items():
        role = sim.role_of(node)
        if role == "courier":
            txt, w, h = f"{node}\n({sim.agents[node].ctype})", 1.75, 0.78
        elif role == "station":
            txt, w, h = node, 1.75, 0.55
        else:
            txt, w, h = ROLE_RU[node], 1.85, 0.55
        ax.add_patch(FancyBboxPatch((x - w / 2, y - h / 2), w, h, boxstyle="round,pad=0,rounding_size=0.12",
                                    fc=NODE, ec=INK2, lw=1.0, zorder=3))
        ax.text(x, y, txt, ha="center", va="center", fontsize=7.8, color=INK, zorder=4, linespacing=1.1)
    ax.set_title(f"а) Сообщения между агентами за прогон (seed {sim.cfg.seed}); толщина линии ∝ числу сообщений "
                 "в обе стороны", fontsize=9, loc="left")
    ax2 = fig.add_subplot(gs[1])
    keys = sorted(by_role_proto, key=lambda k: -sum(by_role_proto[k].values()))
    left = [0] * len(keys)
    for proto in PROTO_ORDER:
        vals = [by_role_proto[k].get(proto, 0) for k in keys]
        if any(vals):
            ax2.barh(range(len(keys)), vals, left=left, color=PROTO_COLORS[proto], height=0.62, label=proto,
                     edgecolor="white", linewidth=1.5)
            left = [l + v for l, v in zip(left, vals)]
    for i, tot in enumerate(left):
        ax2.text(tot + 8, i, str(tot), va="center", fontsize=8.5, color=INK)
    ax2.set_yticks(range(len(keys)))
    ax2.set_yticklabels([f"{ROLE_RU[a]} → {ROLE_RU[b]}" for a, b in keys], fontsize=8.5)
    ax2.invert_yaxis()
    ax2.set_xlim(0, max(left) * 1.1)
    ax2.xaxis.grid(True, color=GRIDC, lw=0.8)
    ax2.set_axisbelow(True)
    ax2.spines["left"].set_visible(False)
    ax2.tick_params(axis="y", length=0)
    ax2.set_xlabel("число сообщений за прогон")
    ax2.legend(loc="lower right", frameon=False, fontsize=8.5, title="протокол", title_fontsize=8.5)
    ax2.set_title("б) Разбивка трафика по парам ролей и протоколам (тот же прогон)", fontsize=9, loc="left")
    save(fig, "comm_observed.png")
    return {"pairs": pair, "by_role_proto": by_role_proto}


# ============================================================================ состояния курьера
def fig_courier_states(sim) -> None:
    cfg = sim.cfg
    W, H = 17.6, 11.4
    fig, ax = canvas(W, H)
    nm = {"idle": "idle\nсвободен", "to_pickup": "to_pickup\nедет к забору", "to_dropoff": "to_dropoff\nвезёт посылку",
          "to_station": "to_station\nедет на станцию", "waiting_charge": "waiting_charge\nждёт места",
          "charging": "charging\nзаряжается", "stranded": "stranded\nразрядился в пути\n(заряд < расхода на шаг)"}
    yt, yb = 8.0, 2.6
    P = {"idle": (2.3, yt), "to_pickup": (7.4, yt), "to_dropoff": (12.5, yt),
         "charging": (2.3, yb), "waiting_charge": (7.4, yb), "to_station": (12.5, yb), "stranded": (15.95, 5.3)}
    assert set(P) == set(STATES)
    ax.add_patch(FancyBboxPatch((0.95, 5.75), 13.05, 4.75, boxstyle="round,pad=0,rounding_size=0.2",
                                fc=tint(BLUE, 0.05), ec=tint(BLUE, 0.5), lw=1.0, ls="--", zorder=0))
    ax.text(13.85, 10.35, "Рабочие состояния (WORK_STATES)", fontsize=8, color=INK2, va="top", ha="right")
    ax.add_patch(FancyBboxPatch((0.95, 0.9), 13.05, 3.1, boxstyle="round,pad=0,rounding_size=0.2",
                                fc=tint(YELLOW, 0.07), ec=tint(YELLOW, 0.7), lw=1.0, ls="--", zorder=0))
    ax.text(1.15, 3.85, "Цикл зарядки (стратегия — плагин курьера; базовая naive)", fontsize=8, color=INK2,
            va="top")
    nodes = {}
    for st, (x, y) in P.items():
        if st == "stranded":
            nodes[st] = rbox(ax, x, y, 3.15, 1.45, nm[st], fc="white", ec=CRIT, lw=1.4, fs=8.0, r=0.3, z=5)
        else:
            nodes[st] = rbox(ax, x, y, 2.6, 1.05, nm[st], fc="white", ec=INK2, lw=1.3, fs=8.5, r=0.3, z=5)
    # старт
    ax.plot([1.25], [9.95], "o", color=INK, ms=7, zorder=6)
    arrow(ax, (1.3, 9.85), (1.75, 8.55), color=INK, lw=1.2, shrinkA=3)
    ax.text(1.5, 10.0, "старт", fontsize=7.8, color=INK2, va="center")

    def straight(a, b, dy, text, va):
        (xa, ya), (xb, yb_) = P[a], P[b]
        sgn = 1 if xb > xa else -1
        x0, x1 = xa + sgn * 1.3, xb - sgn * 1.3
        arrow(ax, (x0, ya + dy), (x1, yb_ + dy), color=INK2, lw=1.3)
        if text:
            label(ax, (x0 + x1) / 2, ya + dy + (0.12 if va == "bottom" else -0.12), text, va=va)

    straight("idle", "to_pickup", 0.2, "очередь не пуста /\nвзять заказ", "bottom")
    straight("to_pickup", "idle", -0.2, "забор не удался\n/ FAILURE assign;\nCANCEL заказа", "top")
    straight("to_pickup", "to_dropoff", 0.0, "в точке забора,\ntry_pickup ok /\nINFORM {picked}", "bottom")
    mid = arrow(ax, P["to_dropoff"], P["idle"], color=INK2, lw=1.3, bend=-1.7, patchA=nodes["to_dropoff"],
                patchB=nodes["idle"])
    label(ax, mid[0], mid[1], "в точке доставки, try_deliver / INFORM order-status {delivered}")
    straight("to_station", "waiting_charge", 0.2, "прибыл на станцию\n/ REQUEST charge", "bottom")
    straight("waiting_charge", "to_station", -0.2, "REFUSE, есть\nдругая станция", "top")
    straight("waiting_charge", "charging", 0.0, "AGREE\n(место выделено)", "bottom")
    x, y = P["waiting_charge"]
    ax.add_patch(FancyArrowPatch((x - 0.4, y - 0.53), (x + 0.4, y - 0.53), arrowstyle="-|>", mutation_scale=10,
                                 color=INK2, lw=1.2, connectionstyle="arc3,rad=1.3", zorder=4))
    label(ax, x, y - 1.3, "INFORM {queued}")
    # рабочие -> зарядка и обратно
    arrow(ax, (12.5, 5.75), (12.5, 3.15), color=INK2, lw=1.3)
    label(ax, 12.3, 4.9, "need_charge (в рабочем состоянии):\nзаряд − энергия на заказ и путь\n"
          f"до станции < резерва {cfg.battery_reserve:.0%};\nбез заказов: заряд < 40 %", ha="right")
    arrow(ax, (2.3, 4.0), (2.3, 7.45), color=INK2, lw=1.3)
    label(ax, 2.5, 4.9, "заряд ≥ 95 % / INFORM\ncharge {done} (из waiting_\ncharge и charging); REFUSE\n"
          "и нет другой станции", ha="left")
    # разрядка и эвакуация
    arrow(ax, (13.8, 7.6), (15.3, 6.05), color=CRIT, lw=1.3)
    arrow(ax, (13.8, 3.0), (15.3, 4.55), color=CRIT, lw=1.3)
    arrow(ax, (8.3, 7.45), (14.5, 5.7), color=CRIT, lw=1.3, bend=-0.25)
    poly(ax, [(15.95, 4.57), (15.95, 0.45), (0.5, 0.45), (0.5, 7.7), (1.0, 7.7)], color=INK2, lw=1.3)
    label(ax, 8.2, 0.45, f"через tow_delay = {cfg.tow_delay} тактов: эвакуация на ближайшую станцию, "
          "заряд 50 %,\nFAILURE assign по незабранным заказам → idle")
    ax.text(0.1, H - 0.05, "Переход: «условие / действие (сообщение)». Телеметрия INFORM telemetry уходит каждые "
            f"{cfg.telemetry_period} тактов во всех состояниях, кроме stranded.", fontsize=8, color=INK2, va="top")
    save(fig, "courier_states.png")


# ============================================================================ жизненный цикл заказа
def fig_order_lifecycle(sim) -> None:
    cfg = sim.cfg
    W, H = 17.2, 9.0
    fig, ax = canvas(W, H)
    y0 = 4.9
    P = {"pending": (2.05, y0), "assigning": (5.6, y0), "assigned": (9.15, y0), "picked": (12.6, y0),
         "delivered": (16.05, y0), "announced": (7.4, 7.75)}
    sub = {"pending": "ждёт\nраспределения", "announced": "выставлен на торги\n(механизмы ЛР2)",
           "assigning": "ждёт ответа\nкурьера", "assigned": "назначен,\nкурьер согласен", "picked": "посылка\nу курьера",
           "delivered": "доставлен,\nклиент уведомлён"}
    nodes = {}
    for st, (x, y) in P.items():
        w = {"announced": 2.9, "delivered": 2.3, "assigned": 2.2}.get(st, 2.1)
        nodes[st] = rbox(ax, x, y, w, 1.3, f"{st.upper()}\n{sub[st]}", fc="white", ec=INK2, lw=1.3, fs=7.6,
                         r=0.3, z=5, ls="--" if st == "announced" else "-")
    ax.plot([0.2], [y0], "o", color=INK, ms=7, zorder=6)
    arrow(ax, (0.28, y0), (1.0, y0), color=INK, lw=1.2, shrinkA=2, shrinkB=0)
    label(ax, 0.02, y0 - 0.95, "REQUEST\norder /\nAGREE", ha="left", fs=7.4, bg=None)

    def main(a, b, text):
        (xa, ya), (xb, _) = P[a], P[b]
        x0, x1 = xa + nodes[a].get_width() / 2, xb - nodes[b].get_width() / 2
        arrow(ax, (x0, ya), (x1, ya), color=INK2, lw=1.3, shrinkB=0)
        label(ax, (x0 + x1) / 2, ya + 0.1, text, va="bottom", fs=7.6, bg=None)

    main("pending", "assigning", "REQUEST\nassign")
    main("assigning", "assigned", "AGREE\nкурьера")
    main("assigned", "picked", "INFORM\n{picked}")
    main("picked", "delivered", "INFORM\n{delivered}")
    timeout = cfg.assign_timeout + 2 * cfg.msg_latency
    poly(ax, [(5.6, y0 - 0.65), (5.6, 3.25), (2.45, 3.25), (2.45, y0 - 0.65)])
    label(ax, 4.0, 3.25, f"REFUSE; нет ответа > {timeout} тактов;\nкурьер потерян → release()", fs=7.6)
    poly(ax, [(9.15, y0 - 0.65), (9.15, 1.75), (1.65, 1.75), (1.65, y0 - 0.65)])
    label(ax, 5.4, 1.75, "FAILURE от исполнителя; курьер потерян → release()\n(переназначение: hops + 1)", fs=7.6)
    m1 = arrow(ax, P["pending"], P["announced"], color=INK2, lw=1.2, ls="--", bend=0.9,
               patchA=nodes["pending"], patchB=nodes["announced"])
    label(ax, m1[0] - 0.1, m1[1] + 0.15, "механизм ЛР2\nобъявил заказ", ha="right", fs=7.6)
    arrow(ax, (5.95, 7.45), (2.75, y0 + 0.65), color=INK2, lw=1.2, ls="--", shrinkA=0, shrinkB=0)
    label(ax, 4.45, 6.35, "release()", fs=7.6)
    arrow(ax, (8.85, 7.5), (9.15, y0 + 0.65), color=INK2, lw=1.2, ls="--", bend=-0.35, shrinkA=0, shrinkB=0)
    label(ax, 9.05, 7.75, "confirm_assignment()\n(победитель)", ha="left", fs=7.6)
    ax.text(0.1, 0.85, f"Тайм-аут ответа (assign_timeout + 2·msg_latency = {timeout} тактов) и сторожевой таймер "
            f"(молчание > {cfg.watchdog_timeout} тактов) действуют только\nна ASSIGNING и ASSIGNED. PICKED и DELIVERED "
            "выставляются по уведомлению курьера, диспетчер доверяет сообщению.\nПунктир — переходы, которыми "
            "пользуются механизмы ЛР2; в базовом режиме central они не возникают.",
            fontsize=7.8, color=INK2, va="top", linespacing=1.35)
    save(fig, "order_lifecycle.png")


# ============================================================================ диаграммы последовательности
def draw_sequence(lifelines: list[tuple[str, str]], rows: list[dict], name: str, title: str,
                  col_w: float = 3.8, row_h: float = 0.5) -> None:
    """rows: {"t": такт|None, "kind": "msg"|"note"|"lost"|"phys", "a": id, "b": id, "text": str, "proto": str, "h": n}."""
    n = len(lifelines)
    W = 1.0 + col_w * n
    nrows = sum(r.get("h", 1) for r in rows)
    H = 2.2 + row_h * (nrows + 1)
    fig, ax = canvas(W, H)
    xs = {lid: 1.0 + col_w * (i + 0.5) for i, (lid, _) in enumerate(lifelines)}
    top = H - 0.95
    for lid, ttl in lifelines:
        x = xs[lid]
        rbox(ax, x, top, col_w * 0.86, 0.7, ttl, fc=NODE, ec=INK2, lw=1.1, fs=8.3, weight="bold", r=0.1, z=5)
        ax.plot([x, x], [top - 0.35, 0.95], color=AXIS, lw=1.1, ls=(0, (4, 3)), zorder=1)
    ax.text(0.45, top, "такт", ha="center", va="center", fontsize=8, color=INK2, weight="bold")
    y = top - 0.45
    last_t = None
    for r in rows:
        h = r.get("h", 1)
        y -= row_h * h
        yc = y + row_h * (h - 1) / 2
        if r.get("t") is not None and r["t"] != last_t:
            ax.text(0.45, yc, str(r["t"]), ha="center", va="center", fontsize=8, color=INK)
            ax.plot([0.05, W - 0.05], [yc + row_h * h / 2] * 2, color=GRIDC, lw=0.5, zorder=0)
            last_t = r["t"]
        kind = r["kind"]
        if kind in ("msg", "lost", "phys"):
            xa, xb = xs[r["a"]], xs[r["b"]]
            color = PROTO_COLORS.get(r.get("proto"), INK2) if kind == "msg" else (CRIT if kind == "lost" else MUTED)
            sgn = 1 if xb > xa else -1
            ya = yc - row_h * 0.22
            if kind == "lost":
                xe = xb - sgn * col_w * 0.3
                ax.plot([xa, xe], [ya, ya], color=color, lw=1.3, ls="--", zorder=3)
                ax.plot([xe], [ya], marker="x", color=color, ms=7, mew=1.8, zorder=4)
            else:
                ax.annotate("", xy=(xb - sgn * 0.05, ya), xytext=(xa, ya), zorder=3,
                            arrowprops=dict(arrowstyle="-|>", color=color, lw=1.5 if kind == "msg" else 1.1,
                                            ls="-" if kind == "msg" else ":", mutation_scale=10,
                                            shrinkA=0, shrinkB=0))
            ax.text((xa + xb) / 2, ya + 0.05, r["text"], ha="center", va="bottom", fontsize=7.6, color=INK,
                    zorder=5, bbox=dict(boxstyle="round,pad=0.08", fc="white", ec="none", alpha=0.9))
        elif kind == "note":
            x = xs[r["a"]]
            w = r.get("w", col_w * 0.97)
            ax.add_patch(FancyBboxPatch((x - w / 2, yc - row_h * h / 2 + 0.05), w, row_h * h - 0.1,
                                        boxstyle="round,pad=0,rounding_size=0.06",
                                        fc=r.get("fc", "#fff8e6"), ec=r.get("ec", tint(YELLOW, 0.8)), lw=0.9,
                                        zorder=4))
            ax.text(x, yc, r["text"], ha="center", va="center", fontsize=7.2, color=INK, zorder=5, linespacing=1.1)
    used = []
    for r in rows:
        if r["kind"] == "msg" and r.get("proto") not in used:
            used.append(r["proto"])
    handles = [Line2D([], [], color=PROTO_COLORS[p], lw=2, label=f"сообщение: {p}") for p in PROTO_ORDER if p in used]
    if any(r["kind"] == "lost" for r in rows):
        handles.append(Line2D([], [], color=CRIT, lw=1.3, ls="--", marker="x", label="сообщение потеряно"))
    if any(r["kind"] == "phys" for r in rows):
        handles.append(Line2D([], [], color=MUTED, lw=1.1, ls=":", label="физическое действие"))
    handles.append(Patch(fc="#fff8e6", ec=tint(YELLOW, 0.8), label="решение / событие"))
    ax.legend(handles=handles, loc="lower center", bbox_to_anchor=(0.5, 0.0), ncol=3,
              frameon=False, fontsize=7.8, handlelength=2.2, columnspacing=1.2)
    ax.text(0.05, H - 0.05, title, fontsize=8.6, color=INK, va="top", weight="bold")
    save(fig, name)


def seq_order(sim) -> dict:
    """Срочный заказ из базового прогона, доставленный в срок курьером без очереди: от публикации до уведомления."""
    ev = sim.trace.events
    picked = {e["order"]: e["t"] for e in ev if e["kind"] == "picked"}
    delivered = {e["order"]: e["t"] for e in ev if e["kind"] == "delivered"}
    assigned = {e["order"]: (e["t"], e["courier"]) for e in ev if e["kind"] == "assigned"}
    o = None
    for cand in sim.orders:
        if cand.priority != "urgent" or cand.id not in assigned:
            continue
        ta, cid = assigned[cand.id]
        if 3 <= picked[cand.id] - ta <= 10 and delivered[cand.id] - picked[cand.id] >= 6 \
                and delivered[cand.id] <= cand.deadline:
            o = cand
            break
    ta, cid = assigned[o.id]
    tp, td = picked[o.id], delivered[o.id]
    m = [e for e in msgs(sim) if e.get("conv") == o.id or e["content"].get("order_id") == o.id]
    tel = [e for e in msgs(sim) if e["proto"] == "telemetry" and e["from"] == cid and ta < e["t"] < tp]

    def mrow(e, text):
        return {"t": e["t"], "kind": "msg", "a": e["from"], "b": e["to"], "proto": e["proto"], "text": text}

    def find(**kw):
        return next(e for e in m if all((e.get(k) if k in e else e["content"].get(k)) == v for k, v in kw.items()))

    rows = [{"t": o.created, "kind": "phys", "a": "client", "b": "world", "text": "publish: посылка «waiting»"}]
    rows.append(mrow(find(proto="order", perf="request"), f"REQUEST order {{{o.id}, срочный}}"))
    agr = find(proto="order", perf="agree")
    rows.append(mrow(agr, "AGREE order {order_id}"))
    rows.append({"t": agr["t"], "kind": "note", "a": "dispatcher", "h": 1.6,
                 "text": "CentralizedAllocator: курьер с\nмин. оценкой belief_eta"})
    ar = find(proto="assign", perf="request")
    rows.append(mrow(ar, f"REQUEST assign {{заказ {o.id}}}"))
    rows.append({"t": ar["t"] + 1, "kind": "note", "a": cid, "h": 1.6,
                 "text": "can_take: вес ≤ грузоподъёмности,\nзапас хода → заказ в очередь"})
    ag = find(proto="assign", perf="agree")
    rows.append(mrow(ag, "AGREE assign {order_id}"))
    rows.append({"t": ag["t"] + 1, "kind": "note", "a": "dispatcher", "text": "статус ASSIGNED"})
    if tel:
        rows.append(mrow(tel[0], "INFORM telemetry {pos, battery…}"))
    rows.append({"t": None, "kind": "note", "a": cid, "text": f"едет к точке забора ({tp - ag['t']} такт.)"})
    rows.append({"t": tp, "kind": "phys", "a": cid, "b": "world", "text": "try_pickup → ok"})
    rows.append(mrow(find(proto="order-status", status="picked"), "INFORM order-status {picked}"))
    rows.append({"t": None, "kind": "note", "a": cid, "text": f"едет к точке доставки ({td - tp} такт.)"})
    rows.append({"t": td, "kind": "phys", "a": cid, "b": "world", "text": "try_deliver → ok"})
    rows.append(mrow(next(e for e in m if e["from"] == cid and e["content"].get("status") == "delivered"),
                     "INFORM order-status {delivered}"))
    cl = next(e for e in m if e["proto"] == "order-status" and e["to"] == "client")
    rows.append(mrow(cl, "INFORM order-status {delivered}"))
    lifelines = [("client", "Клиент"), ("dispatcher", "Диспетчер"), (cid, f"Курьер {cid}"), ("world", "Среда (World)")]
    draw_sequence(lifelines, rows, "seq_order.png",
                  f"Заказ {o.id} (seed {sim.cfg.seed}, срок {o.deadline}): публикация → назначение → забор → "
                  "доставка → уведомление")
    return {"order": o.id, "courier": cid, "created": o.created, "deadline": o.deadline, "assigned": ta,
            "picked": tp, "delivered": td, "client_informed": cl["t"] + 1, "weight": o.weight}


def seq_charge() -> dict:
    """Первый эпизод очереди на станции в базовых прогонах (seed по возрастанию)."""
    for seed in SEEDS:
        sim = demo_run(seed)
        q = [e for e in sim.trace.events if e["kind"] == "queued"]
        if q:
            break
    qe = q[0]
    st, qc, tq = qe["agent"], qe["courier"], qe["t"]
    ev = sim.trace.events
    sess = defaultdict(list)
    for e in ev:
        if e["kind"] == "msg" and e["proto"] == "charge" and st in (e["from"], e["to"]):
            sess[e["from"] if e["to"] == st else e["to"]].append(e)
    done = defaultdict(list)
    for e in ev:
        if e["kind"] == "charge_done" and e.get("station") == st:
            done[e["agent"]].append(e["t"])
    occupants = []
    for g in reversed([e for e in ev if e["kind"] == "slot_granted" and e["agent"] == st and e["t"] <= tq]):
        c = g["courier"]
        if c != qc and c not in occupants and any(t >= tq - 1 for t in done.get(c, [])):
            occupants.append(c)
        if len(occupants) >= sim.cfg.station_slots:
            break
    cs = occupants[::-1] + [qc]
    t_end = min(t for t in done[qc] if t >= tq)
    t_start = min(e["t"] for c in cs for e in sess[c] if e["perf"] == "request" and e["t"] <= tq)
    rows = []
    for e in ev:
        if not (t_start <= e["t"] <= t_end):
            continue
        if e["kind"] == "msg" and e["proto"] == "charge" and st in (e["from"], e["to"]):
            if (e["from"] if e["to"] == st else e["to"]) not in cs:
                continue
            c = e["content"]
            if e["perf"] == "request":
                txt = f"REQUEST charge {{battery {c['battery']:g}}}"
            elif e["perf"] == "agree":
                txt = "AGREE charge {granted}"
            elif "queued" in c:
                txt = f"INFORM charge {{queued: {c['queued']}}}"
            else:
                txt = f"INFORM charge {{done, {c['battery']:g}}}"
            rows.append({"t": e["t"], "kind": "msg", "a": e["from"], "b": e["to"], "proto": "charge", "text": txt})
        elif e["kind"] == "slot_granted" and e["agent"] == st and e["courier"] in cs:
            rows.append({"t": e["t"], "kind": "note", "a": st,
                         "text": f"место → {e['courier']}, заряд +{sim.cfg.charge_rate:g}/такт"})
        elif e["kind"] == "queued" and e["agent"] == st:
            rows.append({"t": e["t"], "kind": "note", "a": st, "text": f"мест нет: {e['courier']} в очередь"})
        elif e["kind"] == "charge_start" and e["agent"] in cs:
            rows.append({"t": e["t"], "kind": "note", "a": e["agent"], "text": "состояние charging"})
        elif e["kind"] == "charge_done" and e["agent"] in cs:
            rows.append({"t": e["t"], "kind": "note", "a": e["agent"], "text": "заряд ≥ 95 %: уезжает"})
    lifelines = ([(c, f"Курьер {c}\n({sim.agents[c].ctype})") for c in cs[:-1]]
                 + [(st, f"Станция {st}\n({sim.cfg.station_slots} места)")]
                 + [(qc, f"Курьер {qc}\n({sim.agents[qc].ctype})")])
    draw_sequence(lifelines, rows, "seq_charge.png",
                  f"Зарядка с очередью (seed {sim.cfg.seed}, {st}): FIFO-политика станции, стратегия naive",
                  row_h=0.48)
    req_q = next(e for e in sess[qc] if e["perf"] == "request" and e["t"] <= tq)
    agr_q = next(e for e in sess[qc] if e["perf"] == "agree" and e["t"] >= tq)
    return {"seed": sim.cfg.seed, "station": st, "queued_courier": qc, "occupants": occupants[::-1],
            "request": req_q["t"], "queued": tq, "agree": agr_q["t"], "wait": agr_q["t"] + 1 - req_q["t"],
            "done": t_end, "t_start": t_start}


def watchdog_run(seed=DEMO_SEED):
    """Проверочный прогон: курьер становится недоступен (hooks_pre) — срабатывает сторожевой таймер."""
    probe = Simulation(SimConfig(seed=seed))
    target, t_off = None, None
    for t in range(probe.cfg.max_ticks):
        probe.tick = t
        probe.step()
        if t > 30:
            for c in probe.couriers:
                if c.current is not None and not c.picked and c.queue:
                    target, t_off = c.id, t + 1
                    break
        if target:
            break
    t_on = t_off + OFFLINE_TICKS

    def hook(sim, t):
        if t == t_off:
            sim.agents[target].active = False
        if t == t_on:
            sim.agents[target].active = True

    sim = Simulation(SimConfig(seed=seed))
    sim.hooks_pre.append(hook)
    m = sim.run()
    return sim, m, target, t_off, t_on


def seq_watchdog() -> dict:
    sim, m, x, t_off, t_on = watchdog_run()
    ev = sim.trace.events
    allm = msgs(sim)
    tl = next(e for e in ev if e["kind"] == "courier_lost" and e["courier"] == x)["t"]
    released = [e for e in ev if e["kind"] == "released" and e["t"] == tl and e["courier"] == x]
    first = released[0]["order"]
    y = next(e for e in ev if e["kind"] == "assigned" and e["order"] == first and e["t"] > tl)["courier"]
    last_from_x = max(e["t"] for e in allm if e["from"] == x and e["t"] < t_off)
    last_tel = max(e["t"] for e in allm if e["from"] == x and e["proto"] == "telemetry" and e["t"] < t_off)
    dropped = {e["id"] for e in ev if e["kind"] == "msg_dropped"}
    rows = []
    tel = next(e for e in allm if e["from"] == x and e["proto"] == "telemetry" and e["t"] == last_tel)
    rows.append({"t": last_tel, "kind": "msg", "a": x, "b": "dispatcher", "proto": "telemetry",
                 "text": f"INFORM telemetry {{tasks: {', '.join(tel['content']['tasks'])}}}"})
    if last_from_x != last_tel:
        e = next(e for e in allm if e["from"] == x and e["t"] == last_from_x)
        rows.append({"t": last_from_x, "kind": "msg", "a": x, "b": "dispatcher", "proto": e["proto"],
                     "text": f"{e['perf'].upper()} {e['proto']} {{{e['content'].get('order_id', '')}}}"})
    rows.append({"t": t_off, "kind": "note", "a": x, "fc": tint(CRIT, 0.1), "ec": CRIT,
                 "text": "связь потеряна (агент недоступен)"})
    for e in allm:
        if t_off <= e["t"] < tl and e["to"] == x and e["id"] in dropped:
            rows.append({"t": e["t"], "kind": "lost", "a": "dispatcher", "b": x,
                         "text": f"{e['perf'].upper()} {e['proto']} {{{e['content'].get('id', '')}}}"})
    silent = tl - (last_from_x + sim.cfg.msg_latency)
    rows.append({"t": tl, "kind": "note", "a": "dispatcher", "h": 1.6,
                 "text": f"тишина {silent} > {sim.cfg.watchdog_timeout} тактов:\n{x} помечен lost"})
    cancels = [e for e in allm if e["t"] == tl and e["to"] == x and e["perf"] == "cancel"]
    rows.append({"t": tl, "kind": "lost", "a": "dispatcher", "b": x,
                 "text": f"CANCEL assign × {len(cancels)}"})
    rows.append({"t": tl, "kind": "note", "a": "dispatcher", "h": 1.6,
                 "text": f"release(): {', '.join(r['order'] for r in released)}\n→ PENDING, снова распределяются"})
    for e in allm:
        if e["t"] <= tl or not (e.get("conv") == first or e["content"].get("order_id") == first):
            continue
        if not (e["from"] == y or e["to"] == y or e["to"] == "client"):
            continue
        if e["proto"] == "assign":
            txt = f"{e['perf'].upper()} assign {{{first}}}"
        else:
            txt = f"INFORM order-status {{{e['content'].get('status')}}}"
        rows.append({"t": e["t"], "kind": "msg", "a": e["from"], "b": e["to"], "proto": e["proto"], "text": txt})
    rows.append({"t": t_on, "kind": "note", "a": x, "fc": tint(GREEN, 0.1), "ec": GREEN, "text": "связь восстановлена"})
    back = [e for e in allm if e["from"] == x and e["t"] >= t_on]
    tel2 = next(e for e in back if e["proto"] == "telemetry")
    rows.append({"t": tel2["t"], "kind": "msg", "a": x, "b": "dispatcher", "proto": "telemetry",
                 "text": f"INFORM telemetry {{tasks: {', '.join(tel2['content']['tasks'])}}}"})
    fail = next((e for e in back if e["perf"] == "failure" and e["content"].get("order_id") == first), None)
    if fail:
        rows.append({"t": fail["t"], "kind": "note", "a": x, "h": 1.6, "fc": tint(CRIT, 0.1), "ec": CRIT,
                     "text": f"исполняет устаревшую очередь:\nзабор {first} → already-taken"})
        rows.append({"t": fail["t"], "kind": "msg", "a": x, "b": "dispatcher", "proto": "assign",
                     "text": f"FAILURE assign {{{first}, already-taken}}"})
        rows.append({"t": fail["t"] + 1, "kind": "note", "a": "dispatcher", "h": 1.6,
                     "text": f"{x} не исполнитель {first}\n→ сообщение игнорируется"})
    lifelines = [(x, f"Курьер {x}"), ("dispatcher", "Диспетчер"), (y, f"Курьер {y}"), ("client", "Клиент")]
    draw_sequence(lifelines, rows, "seq_watchdog.png",
                  f"Сторожевой таймер (seed {sim.cfg.seed}; {x} недоступен на тактах {t_off}–{t_on - 1})")
    others = [e for e in ev if e["kind"] == "pickup_failed"]
    return {"courier": x, "t_off": t_off, "t_on": t_on, "last_msg": last_from_x, "lost_at": tl, "silence": silent,
            "released": [r["order"] for r in released], "new_courier": y, "first": first,
            "pickup_failed": [(e["t"], e["agent"], e["order"], e["reason"]) for e in others],
            "metrics": {k: m[k] for k in ("delivered", "completion_rate", "on_time_rate", "reassignments",
                                          "failed_pickups", "msgs_dropped", "makespan")}}


# ============================================================================ архитектура
def fig_architecture() -> None:
    W, H = 16.0, 13.2
    fig, ax = canvas(W, H)
    C_IF, C_REG, C_AG, C_BUS, C_ENV, C_SPEC = BLUE, VIOLET, AQUA, ORANGE, GREEN, MAGENTA
    fs = 7.8
    # интерфейсы
    for x, t in ((2.75, "CLI: run.py\nодиночный прогон, список режимов,\nтрасса JSONL, показатели JSON"),
                 (8.0, "Веб-демонстрация: web/server.py\nвыбор режимов, анимация города,\nжурнал сообщений, история"),
                 (13.25, "Эксперименты: experiment.py\nrun_batch по seed, aggregate\n(среднее, СКО, 95 % ДИ) → CSV")):
        rbox(ax, x, 12.35, 5.1, 1.35, t, fc=tint(C_IF, 0.08), ec=C_IF, fs=fs, r=0.12)
    # симуляция
    rbox(ax, 8.0, 10.55, 15.6, 1.25,
         "Simulation (sim.py) — тактовый цикл:  1) hooks_pre;  2) шина доставляет созревшие сообщения;\n"
         "3) step() агентов в порядке client, dispatcher, stations, couriers;  4) Metrics.on_tick;  5) hooks_post;  "
         "6) кадр для веба", fc=NODE, ec=INK2, fs=fs, r=0.12)
    for x in (2.75, 8.0, 13.25):
        arrow(ax, (x, 11.67), (x, 11.18), color=INK2, lw=1.1, shrinkA=0, shrinkB=0)
    # реестр режимов
    rbox(ax, 1.8, 6.6, 3.4, 6.2, "", fc=tint(C_REG, 0.07), ec=C_REG, r=0.12)
    ax.text(0.25, 9.5, "Реестр режимов\n(registry.py)", fontsize=8.6, weight="bold", va="top", color=INK)
    ax.text(0.25, 8.65, "Порядок установки:\n1. scenario: none\n2. allocation: central\n3. charging: naive\n"
            "4. defense: none\n\nconfigure(cfg) — до\nпостроения среды;\ninstall(sim) — после\nпостроения агентов:\n"
            "плагины, middlewares\nшины, hooks, замена\nагентов\n\nЛР2–ЛР4: labN/plugin.py,\nregister() добавляет\n"
            "режимы без правки ядра", fontsize=7.4, va="top", color=INK, linespacing=1.22)
    # спецификация и наблюдение
    rbox(ax, 14.12, 8.65, 3.55, 2.1, "", fc=tint(C_SPEC, 0.08), ec=C_SPEC, r=0.12)
    ax.text(12.5, 9.5, "Спецификация (roles.py)", fontsize=8.4, weight="bold", va="top", color=INK)
    ax.text(12.5, 9.0, "ROLES: цели, знания,\nдействия, полномочия;\nPROTOCOLS, PERMISSIONS",
            fontsize=fs, va="top", color=INK, linespacing=1.25)
    rbox(ax, 14.12, 5.4, 3.55, 3.8, "", fc=NODE, ec=INK2, r=0.12)
    ax.text(12.5, 7.1, "Наблюдение", fontsize=8.4, weight="bold", va="top", color=INK)
    ax.text(12.5, 6.6, "Metrics: доставки, сроки,\nсообщения, байты, CPU\nкоординации, зарядка,\n"
            "справедливость,\nнарушения ограничений\n\nTrace: события и\nсообщения (JSONL)",
            fontsize=fs, va="top", color=INK, linespacing=1.25)
    # агенты
    ax.text(3.9, 9.72, "Агенты (agents/). Такт агента: сообщения → плагины → handle() →\nплагины step() → act(). "
            "Пунктир — подключаемые плагины.", fontsize=7.4, color=INK2, va="top", linespacing=1.25)
    agents = [(4.9, "Клиент", "client", "публикует\nзаказы по\nрасписанию"),
              (6.95, "Диспетчер", "dispatcher", "книга заказов,\nубеждения,\nсторожевой\nтаймер"),
              (9.0, "Станции ×M", "station-*", "места,\nочередь,\nфизика\nзарядки"),
              (11.05, "Курьеры ×N", "courier-*", "движение,\nбатарея,\nзабор,\nдоставка")]
    plug = {6.95: "механизм\nраспределения", 9.0: "политика\nдопуска", 11.05: "стратегия\nзарядки, ставки"}
    for x, t, k, d in agents:
        rbox(ax, x, 7.2, 1.95, 3.0, "", fc=tint(C_AG, 0.08), ec=C_AG, r=0.12)
        ax.text(x, 8.25, f"{t}\n{k}", ha="center", va="center", fontsize=7.8, weight="bold", color=INK,
                linespacing=1.15)
        ax.text(x, 7.25, d, ha="center", va="center", fontsize=7.2, color=INK, linespacing=1.12)
        if x in plug:
            rbox(ax, x, 6.15, 1.8, 0.65, plug[x], fc="white", ec=C_REG, ls="--", fs=6.8, r=0.08, z=4)
    arrow(ax, (3.5, 7.2), (3.93, 7.2), color=C_REG, lw=1.1, ls="--", shrinkA=0, shrinkB=0)
    # шина
    rbox(ax, 8.0, 4.15, 4.8, 1.5,
         "Шина сообщений (messages.Bus):\nзадержка msg_latency;\nmiddlewares: сбои, атаки, защиты;\n"
         "истинный origin; учёт и трасса", fc=tint(C_BUS, 0.08), ec=C_BUS, fs=7.4, r=0.12)
    links = {4.9: 5.95, 6.95: 6.95, 9.0: 9.0, 11.05: 10.05}
    for xa, xb in links.items():
        arrow(ax, (xa - 0.22, 5.7), (xb - 0.22, 4.9), color=C_BUS, lw=1.2, shrinkA=0, shrinkB=0)
        arrow(ax, (xb + 0.22, 4.9), (xa + 0.22, 5.7), color=C_BUS, lw=1.2, shrinkA=0, shrinkB=0)
    arrow(ax, (3.5, 4.15), (5.6, 4.15), color=C_REG, lw=1.1, ls="--", shrinkA=0, shrinkB=0)
    # среда
    rbox(ax, 8.0, 1.2, 15.6, 2.0, "", fc=tint(C_ENV, 0.07), ec=C_ENV, r=0.12)
    ax.text(0.45, 1.75, "Среда World (world.py) — физическая истина", fontsize=8.4, weight="bold", color=INK,
            va="center")
    ax.text(0.45, 0.9, "сетка W×H, манхэттенские шаги; точки выдачи и станции; поток заказов зависит только от seed;\n"
            "посылки: waiting → carried:<курьер> → delivered; try_pickup / try_deliver проверяют место и владельца",
            fontsize=fs, color=INK, va="center", linespacing=1.35)
    for x, txt in ((4.45, "публикация\nзаказа"), (11.5, "движение,\nзабор,\nдоставка")):
        ax.annotate("", xy=(x, 2.22), xytext=(x, 5.7), zorder=3,
                    arrowprops=dict(arrowstyle="-|>", color=C_ENV, lw=1.2, ls=":"))
    label(ax, 4.35, 3.0, "публикация\nзаказа", fs=7.0, ha="right", bg=None)
    label(ax, 11.6, 3.0, "движение, забор,\nдоставка (физика,\nне через шину)", fs=7.0, ha="left", bg=None)
    save(fig, "architecture.png")


# ============================================================================ занятость флота
def fig_fleet_timeline(sim) -> None:
    colors = {"idle": "#d6d5ce", "to_pickup": BLUE, "to_dropoff": AQUA, "to_station": ORANGE,
              "waiting_charge": MAGENTA, "charging": YELLOW, "stranded": INK2}
    ru = {"idle": "свободен", "to_pickup": "едет к забору", "to_dropoff": "везёт посылку",
          "to_station": "едет на станцию", "waiting_charge": "ждёт места", "charging": "заряжается",
          "stranded": "разрядился"}
    couriers = sim.couriers
    T = len(sim.frames)
    fig, ax = plt.subplots(figsize=(FIG_W, 3.7))
    used = set()
    for i, c in enumerate(couriers):
        codes = [f["c"][i][2] for f in sim.frames]
        start = 0
        for t in range(1, T + 1):
            if t == T or codes[t] != codes[start]:
                st = STATES[codes[start]]
                used.add(st)
                ax.broken_barh([(start, t - start)], (i - 0.36, 0.72), facecolor=colors[st], edgecolor="none")
                start = t
    ax.set_yticks(range(len(couriers)))
    ax.set_yticklabels([f"{c.id} ({c.ctype})" for c in couriers], fontsize=8)
    ax.invert_yaxis()
    ax.set_xlim(0, T)
    ax.set_xlabel("такт", fontsize=8.5)
    ax.tick_params(axis="x", labelsize=8)
    ax.tick_params(axis="y", length=0)
    ax.spines["left"].set_visible(False)
    handles = [Patch(fc=colors[s], label=ru[s]) for s in STATES if s in used]
    ax.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, -0.2), ncol=len(handles), frameon=False,
              fontsize=8, handlelength=1.4, columnspacing=1.0)
    ax.set_title(f"Состояния курьеров по тактам, базовый режим, seed {sim.cfg.seed}", fontsize=9, loc="left")
    save(fig, "fleet_timeline.png")


# ============================================================================ таблица и проверки
REPORT_KEYS = ["completion_rate", "on_time_rate", "urgent_on_time_rate", "late", "avg_delivery_time",
               "p95_delivery_time", "avg_assign_latency", "makespan", "distance_total", "energy_total", "revenue",
               "stranded", "reassignments", "failed_pickups", "msgs_total", "msgs_per_order", "bytes_total",
               "msgs_by_protocol.order", "msgs_by_protocol.assign", "msgs_by_protocol.order-status",
               "msgs_by_protocol.telemetry", "msgs_by_protocol.charge", "coord_cpu_ms", "charge_sessions",
               "charge_wait_avg", "charge_wait_max", "jain_tasks", "jain_earnings", "utilization", "max_power",
               "power_overload_ticks"]


def message_estimate(rows) -> list[int]:
    """Отклонение фактического числа сообщений от оценки M = 7|O| + N·⌈T/τ⌉ + 3·S + Q (базовый режим)."""
    cfg = SimConfig()
    dev = []
    for r in rows:
        sim = Simulation(SimConfig(seed=r["seed"]))
        sim.run()
        queued = sum(1 for e in sim.trace.events if e["kind"] == "queued")
        tel = cfg.n_couriers * -(-r["ticks"] // cfg.telemetry_period)
        dev.append(r["msgs_total"] - (7 * r["orders_total"] + tel + 3 * r["charge_sessions"] + queued))
    return dev


def fleet_load() -> None:
    """Нагрузка по курьерам в базовом режиме (средние по SEEDS) — для наблюдений в отчёте."""
    tasks, energy = defaultdict(list), defaultdict(list)
    light_on_heavy = heavy_total = runs_with_queue = 0
    for seed in SEEDS:
        sim = Simulation(SimConfig(seed=seed, trace_messages=False))
        sim.run()
        runs_with_queue += any(e["kind"] == "queued" for e in sim.trace.events)
        heavy = {c.id for c in sim.couriers if c.ctype == "heavy"}
        for c in sim.couriers:
            tasks[c.id].append(sim.metrics.tasks_done[c.id])
            energy[c.id].append(sim.metrics.energy[c.id])
        for oid, rec in sim.metrics.orders.items():
            if rec["courier"] in heavy:
                heavy_total += 1
                light_on_heavy += sim.world.orders[oid].weight <= 5
    print("нагрузка по курьерам (доставок / энергия, среднее за прогон):",
          {c: (round(sum(v) / len(v), 2), round(sum(energy[c]) / len(energy[c]), 1)) for c, v in tasks.items()})
    print(f"тяжёлые курьеры: {heavy_total} заказов за все прогоны, из них лёгких (≤ 5 кг) {light_on_heavy}")
    print(f"прогонов с постановкой в очередь на станции: {runs_with_queue}")


def main() -> None:
    print("Базовый прогон, seed", SEEDS[0], "…", SEEDS[-1])
    t0 = time.perf_counter()
    rows = baseline()
    print(f"  {len(rows)} прогонов за {time.perf_counter() - t0:.2f} с (вместе с записью CSV)")
    agg = aggregate(rows, REPORT_KEYS)["baseline"]
    print("\n| Показатель | Среднее | СКО | ±95 % ДИ | мин | макс |")
    for k in REPORT_KEYS:
        vals = [float(r.get(k, 0) or 0) for r in rows]
        mean, sd, ci = agg[k]
        print(f"| {LABELS.get(k, k)} [{k}] | {mean:.4g} | {sd:.3g} | {ci:.3g} | {min(vals):g} | {max(vals):g} |")
    tel_share = [r["msgs_by_protocol.telemetry"] / r["msgs_total"] for r in rows]
    print(f"\nдоля телеметрии: {sum(tel_share) / len(tel_share):.3f} ({min(tel_share):.3f}…{max(tel_share):.3f})")
    dev = message_estimate(rows)
    print(f"отклонение числа сообщений от оценки: {Counter(dev)}")
    print(f"seed без опозданий: {sum(1 for r in rows if r['late'] == 0)}; "
          f"прогонов с очередью на станции: {sum(1 for r in rows if r['charge_wait_max'] > 2)}")
    fleet_load()

    print("\nРисунки:")
    demo = demo_run(DEMO_SEED, frames=True)
    print(f"  seed {DEMO_SEED}: точки выдачи {demo.world.hubs}, станции {demo.world.station_pos}")
    print(f"  заказы: срочных {sum(o.priority == 'urgent' for o in demo.orders)}, "
          f"тяжёлых {sum(o.weight > 5 for o in demo.orders)}, последний создан на такте {demo.orders[-1].created}")
    fig_env_map(demo)
    fig_roles(demo)
    fig_comm_design(demo)
    obs = fig_comm_observed(demo)
    print("  пары ролей:", {f"{a}->{b}": dict(v) for (a, b), v in obs["by_role_proto"].items()})
    fig_courier_states(demo)
    fig_order_lifecycle(demo)
    print("  seq_order:", seq_order(demo))
    print("  seq_charge:", seq_charge())
    print("  seq_watchdog:", seq_watchdog())
    fig_architecture()
    fig_fleet_timeline(demo)


if __name__ == "__main__":
    main()
