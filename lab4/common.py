"""Общие средства ЛР4: разметка «истинных» атак, счётчики, наблюдатель целостности, зонд истинного состояния.

Разметка атак (mark / attack_kind) — это знание экспериментатора, а не агентов: её читает только учёт
(audit) при подсчёте верных и ложных срабатываний. Решения защитных механизмов на неё не опираются.
"""
from __future__ import annotations

from lab1.mas.agents.base import Plugin
from lab1.mas.messages import Message, Perf

ATTACK_ATTR = "_lab4_attack"


# --- истинная разметка (только для оценки) ---
def mark(msg: Message, kind: str) -> Message:
    setattr(msg, ATTACK_ATTR, kind)
    return msg


def attack_kind(msg: Message) -> str | None:
    return getattr(msg, ATTACK_ATTR, None)


def malicious_agents(sim) -> set:
    """Идентификаторы злонамеренных/неисправных агентов, заданных сценарием (для учёта ложных карантинов)."""
    if not hasattr(sim, "_lab4_malicious"):
        sim._lab4_malicious = set()
    return sim._lab4_malicious


# --- счётчики в итоговых показателях ---
def bump(sim, key: str, n: int = 1) -> None:
    c = sim.metrics.custom
    c[key] = c.get(key, 0) + n


def init_counters(sim, keys) -> None:
    for k in keys:
        sim.metrics.custom.setdefault(k, 0)


def send_marked(agent, to: str, perf: Perf, protocol: str, content: dict, kind: str,
                sender: str | None = None, conv: str | None = None, reply_to: int | None = None) -> Message:
    """Отправка сообщения злонамеренным агентом. sender можно подделать: шина всё равно запомнит
    истинный origin. Сообщение помечается как атакующее до прохождения шины (для учёта)."""
    msg = Message(sender=sender or agent.id, receiver=to, perf=perf, protocol=protocol,
                  content=content, conv=conv, reply_to=reply_to)
    mark(msg, kind)
    bump(agent.sim, "atk_msgs")
    return agent.sim.bus.send(msg, origin=agent.id)


# --- наблюдатель целостности: когда клиент впервые получил подтверждение доставки ---
class ConfirmObserver(Plugin):
    """Ставится последним плагином клиента: видит только сообщения, прошедшие защиту."""

    name = "lab4-confirm-observer"

    def __init__(self):
        self.first: dict[str, int] = {}

    def on_message(self, agent, msg: Message) -> bool:
        if msg.protocol == "order-status" and msg.content.get("status") == "delivered":
            self.first.setdefault(msg.content.get("order_id"), agent.sim.tick)
        return False


def integrity_metrics(sim) -> dict:
    """false_confirm — заказы, о доставке которых клиенту сообщили раньше факта или вообще без доставки;
    undelivered — заказы, физически не доставленные к концу прогона."""
    obs = getattr(sim, "_lab4_confirm", None)
    first = obs.first if obs is not None else {}
    orders = sim.metrics.orders
    false_confirm = 0
    for oid, t in first.items():
        rec = orders.get(oid)
        if rec is None or rec["delivered"] is None or rec["delivered"] > t:
            false_confirm += 1
    undelivered = sum(1 for o in orders.values() if o["delivered"] is None)
    return {"false_confirm": false_confirm, "undelivered": undelivered}


def ensure_integrity(sim) -> None:
    """Подключить наблюдатель и обновление показателей целостности (идемпотентно)."""
    if getattr(sim, "_lab4_confirm", None) is not None:
        return
    obs = ConfirmObserver()
    sim.client.add_plugin(obs)
    sim._lab4_confirm = obs

    def hook(s, tick):
        s.metrics.custom.update(integrity_metrics(s))

    sim.hooks_post.append(hook)


# --- зонд истинного состояния для трасс (наблюдатель-экспериментатор, на агентов не влияет) ---
def install_truth_probe(sim, period: int = 5) -> None:
    def probe(s, tick):
        if tick % period:
            return
        s.trace.event("truth", c=[[c.id, c.pos[0], c.pos[1], c.state, round(c.battery, 1), len(c.tasks()),
                                   c.current.id if c.current and c.picked else None] for c in s.couriers],
                      s=[[st.id, int(st.active and st.online), len(st.occupied), len(st.queue)] for st in s.stations])

    sim.hooks_post.append(probe)
