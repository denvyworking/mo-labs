"""Контрактная сеть (FIPA Contract Net Protocol), протокол "cnp".

Роли: диспетчер — менеджер, курьеры — участники (подрядчики). Для каждого заказа отдельные торги:

    менеджер  -> все курьеры : CFP {cfp_id, order, deadline}                      (заказ: ANNOUNCED)
    участник  -> менеджер    : PROPOSE {cfp_id, order_id, cost=eta}  или  REFUSE {reason}
    менеджер  -> лучший      : ACCEPT-PROPOSAL {cfp_id, order_id}               (заказ: ASSIGNING)
    менеджер  -> остальные   : REJECT-PROPOSAL {cfp_id, order_id}
    победитель-> менеджер    : INFORM {order_id, status=committed}              (заказ: ASSIGNED)
                         или : FAILURE {order_id, reason}  -> заказ снова PENDING, повторное объявление

Торги закрываются, когда ответили все приглашённые или наступил срок deadline = t + 2·latency + slack.
Ставка участника — ожидаемое время завершения заказа (такт), если поставить его в конец своей очереди;
она учитывает очередь, заряд и возможный заезд на зарядку (CourierAgent.estimate).

Проблема одновременных CFP: курьер отвечает на несколько объявлений сразу, и каждая ставка, посчитанная
«в одиночку», занижена, если он выиграет несколько контрактов. Два средства:
  1) перепроверка победителя (в обоих режимах): получив ACCEPT, курьер пересчитывает оценку с учётом уже
     выигранного; если заказ будет выполнен позже и своей ставки (с поправкой на прошедшее время),
     и срока заказа — отвечает FAILURE, и менеджер объявляет заказ заново;
  2) предварительные обязательства (режим cnp-commit, параметр cnp_commit): каждое выставленное и ещё
     не решённое предложение курьер учитывает в следующих ставках так, будто этот заказ уже стоит в его
     очереди (оценка пессимистичная). Обязательство снимается по ACCEPT/REJECT или по истечении срока.

Параметры (SimConfig.params): cnp_slack (1), cnp_retry (10), cnp_commit (False для cnp, True для cnp-commit).
"""
from __future__ import annotations

from lab1.mas.agents.base import Plugin
from lab1.mas.agents.dispatcher import ANNOUNCED, ASSIGNING, DispatcherAgent
from lab1.mas.messages import Message, Perf
from lab1.mas.world import Order

from .common import bump, estimate_with, init_counters, param

PROTOCOL = "cnp"
COUNTERS = ("cnp_calls", "cnp_no_bids", "cnp_failures", "cnp_late_proposals")


class ContractNetManager(Plugin):
    """Менеджер контрактной сети (плагин диспетчера)."""

    name = "cnp-manager"

    def __init__(self, slack: int = 1, retry: int = 10):
        self.slack = slack
        self.retry = retry
        self.calls: dict[int, dict] = {}       # cfp_id -> открытые торги
        self.retry_at: dict[str, int] = {}     # заказ без предложений -> когда объявить снова
        self.seq = 0

    # --- сообщения участников ---
    def on_message(self, d: DispatcherAgent, msg: Message) -> bool:
        if msg.protocol != PROTOCOL:
            return False
        with d.sim.metrics.timer("allocation"):
            c = msg.content
            if msg.perf in (Perf.PROPOSE, Perf.REFUSE):
                call = self.calls.get(c.get("cfp_id"))
                if call is None or msg.sender not in call["invited"]:
                    if msg.perf == Perf.PROPOSE:   # торги уже закрыты: отклоняем, чтобы снять обязательство
                        bump(d.sim, "cnp_late_proposals")
                        d.send(msg.sender, Perf.REJECT, PROTOCOL,
                               {"cfp_id": c.get("cfp_id"), "order_id": c.get("order_id"), "reason": "closed"},
                               conv=msg.conv)
                    return True
                call["replies"][msg.sender] = float(c["cost"]) if msg.perf == Perf.PROPOSE else None
            elif msg.perf in (Perf.INFORM, Perf.FAILURE):
                rec = d.records.get(c.get("order_id"))
                if rec is None or rec.status != ASSIGNING or rec.courier != msg.sender:
                    return True   # устаревший ответ: назначение уже отозвано сторожевым таймером
                if msg.perf == Perf.INFORM:
                    d.confirm_assignment(rec, msg.sender)
                else:
                    bump(d.sim, "cnp_failures")
                    d.release(rec, "cnp-failure:" + str(c.get("reason")))
        return True

    # --- такт менеджера: закрыть созревшие торги, объявить новые ---
    def step(self, d: DispatcherAgent, tick: int) -> None:
        with d.sim.metrics.timer("allocation"):
            for cfp_id, call in list(self.calls.items()):
                if len(call["replies"]) >= len(call["invited"]) or tick >= call["deadline"]:
                    del self.calls[cfp_id]
                    self.award(d, call, tick)
            for rec in d.pending():
                if self.retry_at.get(rec.order.id, -1) > tick:
                    continue
                self.announce(d, rec, tick)

    def announce(self, d: DispatcherAgent, rec, tick: int) -> None:
        invited = d.couriers_available()
        if not invited:
            return
        self.seq += 1
        deadline = tick + 2 * d.sim.cfg.msg_latency + self.slack
        self.calls[self.seq] = {"order_id": rec.order.id, "invited": set(invited), "replies": {},
                                "deadline": deadline}
        d.set_status(rec, ANNOUNCED, None)
        bump(d.sim, "cnp_calls")
        order = rec.order.to_msg()
        for cid in invited:
            d.send(cid, Perf.CFP, PROTOCOL, {"cfp_id": self.seq, "order": dict(order), "deadline": deadline},
                   conv=rec.order.id)

    def award(self, d: DispatcherAgent, call: dict, tick: int) -> None:
        rec = d.records[call["order_id"]]
        if rec.status != ANNOUNCED:
            return
        bids = sorted((cost, cid) for cid, cost in call["replies"].items() if cost is not None)
        if not bids:
            bump(d.sim, "cnp_no_bids")
            self.retry_at[rec.order.id] = tick + self.retry
            d.release(rec, "cnp-no-bids")
            return
        cost, winner = bids[0]
        d.set_status(rec, ASSIGNING, winner)
        base = {"order_id": rec.order.id}
        d.send(winner, Perf.ACCEPT, PROTOCOL, {**base, "cost": cost}, conv=rec.order.id)
        for _, cid in bids[1:]:
            d.send(cid, Perf.REJECT, PROTOCOL, {**base, "winner_cost": cost}, conv=rec.order.id)


class ContractNetParticipant(Plugin):
    """Участник контрактной сети (плагин курьера): ставки с предварительными обязательствами."""

    name = "cnp-participant"

    def __init__(self, commitments: bool = False):
        self.commitments = commitments
        self.proposals: dict[str, dict] = {}   # заказ -> {order, cost, expires}

    def on_message(self, c, msg: Message) -> bool:
        if msg.protocol != PROTOCOL:
            return False
        with c.sim.metrics.timer("bidding"):
            m = msg.content
            if msg.perf == Perf.CFP:
                self.on_cfp(c, msg)
            elif msg.perf == Perf.ACCEPT:
                self.on_accept(c, msg)
            elif msg.perf == Perf.REJECT:
                self.proposals.pop(m.get("order_id"), None)
        return True

    def on_cfp(self, c, msg: Message) -> None:
        m = msg.content
        order = Order.from_msg(m["order"])
        base = {"cfp_id": m["cfp_id"], "order_id": order.id}
        ok, reason = c.can_take(order)
        if not ok:
            c.reply(msg, Perf.REFUSE, {**base, "reason": reason})
            return
        assumed = [p["order"] for oid, p in self.proposals.items() if oid != order.id] if self.commitments else []
        est = estimate_with(c, order, assumed)
        expires = m["deadline"] + 2 * c.sim.cfg.msg_latency + 2
        self.proposals[order.id] = {"order": order, "cost": est["eta"], "t": c.sim.tick, "expires": expires}
        c.reply(msg, Perf.PROPOSE, {**base, "cost": est["eta"], "extra": est["extra"]})

    def on_accept(self, c, msg: Message) -> None:
        oid = msg.content.get("order_id")
        prop = self.proposals.pop(oid, None)
        if prop is None:
            c.reply(msg, Perf.FAILURE, {"order_id": oid, "reason": "expired"})
            return
        order = prop["order"]
        # Перепроверка выполнимости с учётом уже выигранных за это время контрактов
        ok, reason = c.can_take(order)
        if ok:
            eta = c.estimate(order)["eta"]
            drift = c.sim.tick - prop["t"]   # простой курьера сдвигает оценку на прошедшее время
            if eta > prop["cost"] + drift + 1e-9 and eta > order.deadline:
                ok, reason = False, "overcommitted"
        if not ok:
            c.reply(msg, Perf.FAILURE, {"order_id": oid, "reason": reason})
            return
        c.accept_task(order)
        c.reply(msg, Perf.INFORM, {"order_id": oid, "status": "committed"})

    def step(self, c, tick: int) -> None:
        for oid in [k for k, p in self.proposals.items() if p["expires"] < tick]:
            del self.proposals[oid]


def _install(sim, commit: bool) -> None:
    init_counters(sim, COUNTERS)
    sim.dispatcher.set_allocator(ContractNetManager(slack=int(param(sim, "cnp_slack", 1)),
                                                    retry=int(param(sim, "cnp_retry", 10))))
    commit = bool(param(sim, "cnp_commit", commit))
    for c in sim.couriers:
        c.add_plugin(ContractNetParticipant(commitments=commit))


def install_cnp(sim) -> None:
    """Контрактная сеть: ставки «в одиночку» + перепроверка победителя."""
    _install(sim, commit=False)


def install_cnp_commit(sim) -> None:
    """Контрактная сеть с предварительными обязательствами в ставках."""
    _install(sim, commit=True)
