"""Общие части механизмов ЛР3: параметры, заявки курьера, формула критичности, квота частичной зарядки,
эгоистичные курьеры и показатели «честные / эгоистичные».

Все параметры механизмов берутся из SimConfig.params (значения по умолчанию — DEFAULTS).
"""
from __future__ import annotations

import statistics
from collections import defaultdict

from lab1.mas.world import manhattan

DEFAULTS: dict = {
    # --- формула критичности (приоритет на станции и у арбитра) ---
    "w_battery": 1.0,        # вес «опустошённости» батареи (1 - заряд/ёмкость)
    "w_deadline": 2.0,       # вес близости срока самого срочного заказа курьера
    "w_urgent": 0.5,         # надбавка за срочный (urgent) заказ на борту / в очереди
    "w_wait": 1.0,           # вес старения: +w_wait за каждые aging_ticks тактов ожидания
    "aging_ticks": 20,
    "slack_scale": 40,       # запас времени до срока, при котором надбавка за срок обнуляется
    "w_booking": 1.0,        # надбавка курьеру, прибывшему по действующей брони
    # --- частичная зарядка ---
    "partial_charge": True,
    "quota_margin": 0.15,    # запас сверх заявленной потребности (доля ёмкости)
    "quota_floor": 0.5,      # не меньше этой доли ёмкости
    "full_level": 0.95,      # «полная» зарядка, как в режиме naive
    # --- переговоры о слоте ---
    "cfp_timeout": 3,        # сколько тактов курьер ждёт предложений станций
    "booking_grace": 4,      # окно брони: ETA + booking_grace
    # --- эгоистичные курьеры ---
    "selfish_share": 0.0,    # доля эгоистичных курьеров
    "selfish_lie": True,     # завышают срочность и занижают заряд в заявке
    "selfish_overstay": True,  # игнорируют квоту, заряжаются до 100 %
    "selfish_noshow": True,  # бронируют все предложенные станции, приезжают на одну
    # --- репутация и санкции ---
    "rep_lie": 0.3,          # штраф за ложную заявку
    "rep_overstay": 0.2,     # штраф за пересиживание квоты
    "rep_noshow": 0.15,      # штраф за неявку по брони
    "rep_honest": 0.05,      # восстановление за честный сеанс
    "rep_ban": 0.5,          # ниже порога: заявки не учитываются, брони запрещены
    "overstay_grace": 3,     # сколько тактов станция ждёт ухода после уведомления о квоте
    "lie_tol_battery": 2.0,  # допуск расхождения заявленного и измеренного заряда
    "lie_tol_deadline": 5,   # допуск расхождения заявленного и фактического срока
}


def P(sim, key: str):
    """Параметр механизма: SimConfig.params или значение по умолчанию."""
    return sim.cfg.params.get(key, DEFAULTS[key])


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x


# --- эгоистичные курьеры ---
def selfish_ids(sim) -> set[str]:
    """Эгоистичные курьеры: round(share * N) лёгких курьеров, равномерно по списку (детерминированно,
    одинаково для всех режимов). Только лёгкие — чтобы сравнение групп не искажалось типом курьера."""
    light = [c for c in sim.couriers if c.ctype == "light"]
    share = float(P(sim, "selfish_share"))
    k = max(0, min(len(light), round(share * len(sim.couriers))))
    if k == 0:
        return set()
    return {light[int((j + 0.5) * len(light) / k)].id for j in range(k)}


# --- заявка курьера ---
def true_claims(c) -> dict:
    """Истинное состояние курьера: заряд, потребность в энергии на принятые заказы, ближайший срок."""
    tasks = c.tasks()
    dist, energy, tail = c.route_remaining()
    back = c.cons_empty * manhattan(tail, c.stations()[c.nearest_station(tail)])
    need = min(c.capacity, energy + back + c.reserve)
    deadline = min((o.deadline for o in tasks), default=None)
    return {"battery": round(c.battery, 2), "capacity": c.capacity, "need": round(need, 2),
            "deadline": deadline, "urgent": any(o.priority == "urgent" for o in tasks), "route": dist}


def make_claims(c, selfish: bool) -> dict:
    """Заявка, которую курьер отправляет станции. Эгоист (selfish_lie) занижает заряд и завышает срочность."""
    cl = true_claims(c)
    if selfish and P(c.sim, "selfish_lie"):
        cl.update(battery=round(0.3 * c.battery, 2), need=c.capacity, deadline=c.sim.tick + 1, urgent=True, route=0)
    return cl


# --- правила приоритезации ---
def criticality(sim, claims: dict, arrived: int, now: int, trust: float = 1.0, booked: bool = False) -> float:
    """Критичность курьера (чем больше, тем раньше обслуживается).

    P = trust * (w_battery * D + w_deadline * U + w_urgent * [urgent]) + w_wait * (now - arrived) / aging_ticks
        + w_booking * [прибыл по брони]
    D = 1 - заряд / ёмкость; U = clamp(1 - (срок - now - остаток маршрута) / slack_scale).
    trust — доверие к заявке (1 без репутации; в режиме reputation зависит от репутации курьера).
    """
    cap = float(claims.get("capacity") or 1.0)
    d = clamp(1.0 - float(claims.get("battery", 0.0)) / cap)
    dl = claims.get("deadline")
    u = 0.0 if dl is None else clamp(1.0 - (dl - now - claims.get("route", 0)) / P(sim, "slack_scale"))
    claim_part = P(sim, "w_battery") * d + P(sim, "w_deadline") * u + P(sim, "w_urgent") * bool(claims.get("urgent"))
    aging = P(sim, "w_wait") * (now - arrived) / P(sim, "aging_ticks")
    return trust * claim_part + aging + (P(sim, "w_booking") if booked else 0.0)


def quota_target(sim, claims: dict) -> float:
    """До какого заряда станция заряжает курьера, если ресурс в дефиците (частичная зарядка)."""
    cap = float(claims.get("capacity") or 1.0)
    full = P(sim, "full_level") * cap
    if not P(sim, "partial_charge"):
        return full
    want = float(claims.get("need", cap)) + P(sim, "quota_margin") * cap
    return clamp(want, P(sim, "quota_floor") * cap, full)


# --- состояние механизма и показатели ---
class Lab3State:
    """Служебное состояние ЛР3, привязанное к симуляции (sim.lab3): кто эгоист, счётчики для показателей."""

    COUNTERS = ("l3_quota_stops", "l3_bookings", "l3_noshows", "l3_rebookings", "l3_fallbacks",
                "l3_power_requests", "l3_evictions", "l3_lies_detected", "l3_false_accusations",
                "l3_overstays", "l3_booking_refusals")

    def __init__(self, sim, with_counters: tuple = COUNTERS):
        self.sim = sim
        self.selfish = selfish_ids(sim)
        self.waits: dict[str, list[int]] = defaultdict(list)
        self.grid_waits: list[int] = []
        self.reputation: dict[str, float] = {}
        for k in with_counters:
            sim.metrics.custom[k] = 0
        sim.metrics.custom["l3_selfish_n"] = len(self.selfish)
        sim.hooks_post.append(self._finalize_hook)

    def inc(self, key: str, n: int = 1) -> None:
        self.sim.metrics.custom[key] = self.sim.metrics.custom.get(key, 0) + n

    def on_wait(self, cid: str, wait: int) -> None:
        self.waits[cid].append(wait)

    def on_sanction(self, cid: str, reason: str) -> None:
        """Учёт санкций наблюдателем эксперимента (знает, кто на самом деле эгоист)."""
        if reason.startswith("lie"):
            self.inc("l3_lies_detected")
        elif reason == "overstay":
            self.inc("l3_overstays")
        if cid not in self.selfish:
            self.inc("l3_false_accusations")

    # Показатели «честные / эгоистичные» считаются один раз — на последнем такте прогона.
    def _finalize_hook(self, sim, tick: int) -> None:
        if tick >= sim.cfg.max_ticks - 1 or sum(sim.metrics.tasks_done.values()) >= len(sim.orders):
            self.finalize()

    def finalize(self) -> None:
        sim, m = self.sim, self.sim.metrics
        cids = [c.id for c in sim.couriers]
        # группы сравниваются по лёгким курьерам (эгоисты выбираются только среди них)
        light = [c.id for c in sim.couriers if c.ctype == "light"]
        honest = [c for c in light if c not in self.selfish]
        selfish = [c for c in light if c in self.selfish]

        def mean_wait(group):
            vals = [w for c in group for w in self.waits.get(c, [])]
            return round(statistics.fmean(vals), 3) if vals else 0.0

        def mean_earn(group):
            return round(statistics.fmean([m.earnings[c] for c in group]), 2) if group else 0.0

        per_courier_wait = [statistics.fmean(v) for v in self.waits.values() if v]
        m.custom.update({
            "l3_wait_honest": mean_wait(honest),
            "l3_wait_selfish": mean_wait(selfish),
            "l3_earn_honest": mean_earn(honest),
            "l3_earn_selfish": mean_earn(selfish),
            "l3_gini_wait": gini(per_courier_wait),
            "l3_gini_earn": gini([m.earnings[c] for c in cids]),
            "l3_grid_wait_avg": round(statistics.fmean(self.grid_waits), 3) if self.grid_waits else 0.0,
        })
        if self.reputation:
            rh = [self.reputation.get(c, 1.0) for c in honest]
            rs = [self.reputation.get(c, 1.0) for c in selfish]
            m.custom["l3_rep_honest"] = round(statistics.fmean(rh), 3) if rh else 1.0
            m.custom["l3_rep_selfish"] = round(statistics.fmean(rs), 3) if rs else 1.0


def gini(values: list[float]) -> float:
    """Коэффициент Джини: 0 — полное равенство, ближе к 1 — всё у одного."""
    vals = sorted(v for v in values)
    n = len(vals)
    s = sum(vals)
    if n == 0 or s == 0:
        return 0.0
    cum = sum((i + 1) * v for i, v in enumerate(vals))
    return round((2 * cum) / (n * s) - (n + 1) / n, 4)
