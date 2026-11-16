"""Общие вспомогательные функции механизмов распределения ЛР2.

Здесь нет координации — только локальные вычисления агента над собственным состоянием:
оценка «что будет, если в моей очереди окажутся ещё и эти заказы» (предварительные обязательства
в контрактной сети, выигранные лоты в аукционе) и прогноз времени завершения заказов очереди
(признак перегрузки при делегировании).
"""
from __future__ import annotations

from lab1.mas.metrics import jain


def param(sim, key: str, default):
    """Параметр механизма из SimConfig.params (общий для всех режимов словарь)."""
    return sim.cfg.params.get(key, default)


def bump(sim, key: str, n: float = 1) -> None:
    """Счётчик механизма в итоговых показателях (metrics.custom)."""
    sim.metrics.custom[key] = sim.metrics.custom.get(key, 0) + n


def init_counters(sim, keys) -> None:
    for k in keys:
        sim.metrics.custom.setdefault(k, 0)


def estimate_with(c, order, assumed=()) -> dict:
    """Оценка курьером добавления order в конец очереди, как если бы в ней уже стояли заказы assumed.

    Курьер рассуждает о гипотетическом состоянии своей очереди: временно дополняет её и вызывает
    собственную оценку estimate() (путь, заряд, возможный заезд на зарядку), затем восстанавливает.
    """
    if not assumed:
        return c.estimate(order)
    saved = c.queue
    c.queue = list(saved) + list(assumed)
    try:
        return c.estimate(order)
    finally:
        c.queue = saved


def queue_etas(c) -> list[tuple]:
    """Прогноз времени завершения каждого заказа из очереди (без текущего) при исполнении по порядку."""
    saved = c.queue
    out = []
    try:
        for i, o in enumerate(saved):
            c.queue = saved[:i]
            out.append((o, c.estimate(o)["eta"]))
    finally:
        c.queue = saved
    return out


def add_income(sim, book: dict, courier_id: str, amount: float, key: str) -> None:
    """Доход курьеров в механизме (плата по контракту, книга book: курьер -> сумма) и индекс Джайна по нему."""
    book[courier_id] = book.get(courier_id, 0.0) + amount
    sim.metrics.custom[key + "_total"] = round(sum(book.values()), 2)
    sim.metrics.custom["jain_" + key] = jain(list(book.values()))
