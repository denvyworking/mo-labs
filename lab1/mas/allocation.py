"""Механизмы распределения заказов. В ядре — базовый: централизованная диспетчеризация.

Интерфейс механизма — плагин диспетчера (Plugin): step() вызывается каждый такт до act() диспетчера,
on_message() получает сообщения своего протокола. Для назначения используются методы диспетчера
assign() / confirm_assignment() / release(). Участие курьеров (ставки, предложения) реализуется
плагинами курьеров (см. ЛР2).
"""
from __future__ import annotations

from .agents.base import Plugin
from .agents.dispatcher import DispatcherAgent
from .world import manhattan

RETRY_AFTER = 10   # через сколько тактов снова предлагать заказ отказавшимся курьерам


def belief_eta(d: DispatcherAgent, b: dict, order, tick: int) -> float:
    """Оценка диспетчером времени завершения заказа курьером по его (возможно устаревшим) данным."""
    start = max(tick, b["tail_eta"])
    tail = tuple(b["tail_pos"])
    d1, trip = manhattan(tail, order.pickup), order.trip()
    eta = start + d1 + trip
    energy = d1 * b["cons_empty"] + trip * b["cons_loaded"]
    reserve = d.sim.cfg.battery_reserve * b["capacity"]
    if b["tail_battery"] - energy < reserve:
        eta += (b["capacity"] - max(0.0, b["tail_battery"])) / d.sim.cfg.charge_rate + 6
    return eta


class CentralizedAllocator(Plugin):
    """Диспетчер сам выбирает исполнителя по глобальной картине (телеметрия курьеров):
    заказы в порядке срочности, исполнитель — с минимальным ожидаемым временем завершения."""

    name = "central"

    def step(self, d: DispatcherAgent, tick: int) -> None:
        with d.sim.metrics.timer("allocation"):
            for rec in d.pending():
                if rec.refused_by and tick - rec.last_refuse > RETRY_AFTER:
                    rec.refused_by.clear()
                best = None
                for cid in d.couriers_available():
                    b = d.beliefs[cid]
                    if cid in rec.refused_by or rec.order.weight > b["payload"]:
                        continue
                    eta = belief_eta(d, b, rec.order, tick)
                    if best is None or eta < best[0]:
                        best = (eta, cid)
                if best is None:
                    continue
                eta, cid = best
                d.assign(rec, cid)
                self._extend_belief(d.beliefs[cid], rec.order, eta)

    @staticmethod
    def _extend_belief(b: dict, order, eta: float) -> None:
        """Диспетчер сразу учитывает своё назначение, не дожидаясь следующей телеметрии."""
        d1 = manhattan(tuple(b["tail_pos"]), order.pickup)
        b["tail_battery"] -= d1 * b["cons_empty"] + order.trip() * b["cons_loaded"]
        b["tail_pos"] = list(order.dropoff)
        b["tail_eta"] = eta
        b["tasks"] = list(b.get("tasks", [])) + [order.id]
