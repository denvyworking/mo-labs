"""Показатели работы системы: эффективность, стоимость координации, справедливость, нарушения."""
from __future__ import annotations

import statistics
import time
from collections import Counter, defaultdict
from contextlib import contextmanager


def jain(values: list[float]) -> float:
    """Индекс справедливости Джайна: 1 — полностью равное распределение, 1/n — всё у одного."""
    vals = [v for v in values]
    if not vals:
        return 1.0
    s, s2 = sum(vals), sum(v * v for v in vals)
    return 1.0 if s2 == 0 else round(s * s / (len(vals) * s2), 4)


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    vals = sorted(values)
    k = (len(vals) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def _mean(values) -> float:
    return round(statistics.fmean(values), 2) if values else 0.0


class Metrics:
    def __init__(self, sim):
        self.sim = sim
        self.msgs = Counter()           # по перформативам
        self.msgs_by_proto = Counter()
        self.msgs_by_edge = Counter()   # (роль отправителя, роль получателя)
        self.bytes = 0
        self.dropped = 0
        self.blocked = 0
        self.orders: dict[str, dict] = {}
        self.stranded = 0
        self.reassignments = 0
        self.failed_pickups = 0
        self.charge_waits: list[tuple[str, int]] = []
        self.cpu = defaultdict(float)   # секунды по компонентам координации
        self.violations = Counter()
        self.distance = Counter()
        self.energy = defaultdict(float)
        self.busy_ticks = Counter()
        self.tasks_done = Counter()
        self.earnings = defaultdict(float)
        self.power_overload_ticks = 0
        self.max_power = 0
        self.custom: dict = {}          # счётчики механизмов из ЛР2–ЛР4

    # --- сообщения ---
    def on_message(self, m) -> None:
        self.msgs[m.perf.value] += 1
        self.msgs_by_proto[m.protocol] += 1
        self.msgs_by_edge[(self.sim.role_of(m.sender), self.sim.role_of(m.receiver))] += 1
        self.bytes += m.size()

    def on_message_dropped(self, m) -> None:
        self.dropped += 1

    def on_message_blocked(self, m) -> None:
        self.blocked += 1

    @contextmanager
    def timer(self, key: str):
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.cpu[key] += time.perf_counter() - t0

    # --- заказы (физическая истина) ---
    def on_order_created(self, o) -> None:
        self.orders[o.id] = {"created": o.created, "deadline": o.deadline, "priority": o.priority,
                             "reward": o.reward, "assigned": None, "picked": None, "delivered": None,
                             "courier": None}

    def on_assigned(self, oid: str, cid: str, tick: int) -> None:
        rec = self.orders.get(oid)
        if rec is not None and rec["assigned"] is None:
            rec["assigned"] = tick

    def on_pickup(self, oid: str, cid: str, tick: int) -> None:
        self.orders[oid]["picked"] = tick

    def on_delivered(self, oid: str, cid: str, tick: int) -> None:
        rec = self.orders[oid]
        rec["delivered"], rec["courier"] = tick, cid
        self.tasks_done[cid] += 1
        self.earnings[cid] += rec["reward"]

    # --- курьеры ---
    def on_move(self, cid: str, energy: float) -> None:
        self.distance[cid] += 1
        self.energy[cid] += energy

    def on_charge_wait(self, cid: str, wait: int) -> None:
        self.charge_waits.append((cid, wait))

    def on_tick(self, tick: int) -> None:
        power = sum(len(s.occupied) for s in self.sim.stations)
        self.max_power = max(self.max_power, power)
        limit = self.sim.cfg.grid_power_limit
        if limit is not None and power > limit:
            self.power_overload_ticks += 1
            self.violations["power_limit"] += 1
        for s in self.sim.stations:
            if len(s.occupied) > s.slots:
                self.violations["station_slots"] += 1
        for c in self.sim.couriers:
            if c.state != "idle":
                self.busy_ticks[c.id] += 1

    # --- итог ---
    def summary(self) -> dict:
        sim = self.sim
        ords = list(self.orders.values())
        n = len(ords) or 1
        delivered = [o for o in ords if o["delivered"] is not None]
        on_time = [o for o in delivered if o["delivered"] <= o["deadline"]]
        urgent = [o for o in ords if o["priority"] == "urgent"]
        urgent_on_time = [o for o in urgent if o["delivered"] is not None and o["delivered"] <= o["deadline"]]
        dt = [o["delivered"] - o["created"] for o in delivered]
        wa = [o["assigned"] - o["created"] for o in ords if o["assigned"] is not None]
        cids = [c.id for c in sim.couriers]
        waits = [w for _, w in self.charge_waits]
        wait_by_c = defaultdict(list)
        for cid, w in self.charge_waits:
            wait_by_c[cid].append(w)
        total_msgs = sum(self.msgs.values())
        ticks = sim.tick + 1
        return {
            "orders_total": len(ords),
            "delivered": len(delivered),
            "completion_rate": round(len(delivered) / n, 4),
            "on_time_rate": round(len(on_time) / n, 4),
            "urgent_on_time_rate": round(len(urgent_on_time) / len(urgent), 4) if urgent else 1.0,
            "late": len(delivered) - len(on_time),
            "avg_delivery_time": _mean(dt),
            "p95_delivery_time": round(percentile(dt, 95), 2),
            "avg_assign_latency": _mean(wa),
            "makespan": max((o["delivered"] for o in delivered), default=0),
            "ticks": ticks,
            "distance_total": sum(self.distance.values()),
            "energy_total": round(sum(self.energy.values()), 1),
            "revenue": round(sum(o["reward"] for o in delivered), 1),
            "stranded": self.stranded,
            "reassignments": self.reassignments,
            "failed_pickups": self.failed_pickups,
            "msgs_total": total_msgs,
            "msgs_per_order": round(total_msgs / n, 2),
            "bytes_total": self.bytes,
            "msgs_dropped": self.dropped,
            "msgs_blocked": self.blocked,
            "msgs_by_perf": dict(self.msgs),
            "msgs_by_protocol": dict(self.msgs_by_proto),
            "coord_cpu_ms": round(sum(self.cpu.values()) * 1000, 3),
            "cpu_ms": {k: round(v * 1000, 3) for k, v in self.cpu.items()},
            "charge_sessions": len(waits),
            "charge_wait_avg": _mean(waits),
            "charge_wait_max": max(waits, default=0),
            "jain_charge_wait": jain([statistics.fmean(v) for v in wait_by_c.values()]),
            "jain_tasks": jain([self.tasks_done[c] for c in cids]),
            "jain_earnings": jain([self.earnings[c] for c in cids]),
            "utilization": round(statistics.fmean([self.busy_ticks[c] / ticks for c in cids]), 4) if cids else 0.0,
            "power_overload_ticks": self.power_overload_ticks,
            "max_power": self.max_power,
            "violations": dict(self.violations),
            **self.custom,
        }


# Русские подписи основных показателей (для таблиц и веб-интерфейса).
LABELS = {
    "orders_total": "Заказов всего",
    "delivered": "Доставлено",
    "completion_rate": "Доля доставленных",
    "on_time_rate": "Доля доставленных в срок",
    "urgent_on_time_rate": "Срочные в срок",
    "late": "Опозданий",
    "avg_delivery_time": "Ср. время доставки, такт",
    "p95_delivery_time": "95-й перцентиль доставки",
    "avg_assign_latency": "Ср. задержка назначения",
    "makespan": "Время выполнения всех заказов",
    "distance_total": "Суммарный пробег",
    "energy_total": "Израсходовано энергии",
    "revenue": "Выручка",
    "stranded": "Разрядки в пути",
    "reassignments": "Переназначений",
    "failed_pickups": "Неудачных заборов",
    "msgs_total": "Сообщений всего",
    "msgs_per_order": "Сообщений на заказ",
    "bytes_total": "Объём сообщений, байт",
    "msgs_dropped": "Потеряно сообщений",
    "msgs_blocked": "Заблокировано сообщений",
    "coord_cpu_ms": "CPU координации, мс",
    "charge_sessions": "Сеансов зарядки",
    "charge_wait_avg": "Ср. ожидание зарядки",
    "charge_wait_max": "Макс. ожидание зарядки",
    "jain_charge_wait": "Джайн: ожидание зарядки",
    "jain_tasks": "Джайн: заказы на курьера",
    "jain_earnings": "Джайн: доход курьеров",
    "utilization": "Загрузка курьеров",
    "power_overload_ticks": "Тактов с превышением мощности",
    "max_power": "Пиковая мощность (мест)",
}
