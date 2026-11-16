"""Распределённое делегирование, протокол "delegate".

Диспетчер выполняет только роль маршрутизатора: каждый заказ сразу отдаёт одному курьеру — ближайшему
к точке выдачи по последней известной позиции (телеметрия), с подходящей грузоподъёмностью, НЕ глядя
на очереди и заряд (обычное назначение ядра: assign REQUEST -> AGREE/REFUSE). Дальше решают курьеры.

Перегруженный курьер сам передаёт заказ соседу по одноранговому протоколу:

    курьер A -> курьер B : REQUEST {order, eta (прогноз A для этого заказа), hops}
    курьер B -> курьер A : AGREE {order_id, eta}  — B ставит заказ в свою очередь
                       или REFUSE {order_id, reason}  — A спрашивает следующего соседа
    курьер A -> диспетчер: INFORM {order_id, to=B, hops}  — передача обязательства (книга заказов)
    курьер A -> курьер B : CANCEL {order_id} — только если AGREE пришёл после тайм-аута, а заказ уже не у A

Правила курьера A (локальное знание: своя очередь, заряд, положение):
  * перегрузка — в очереди есть заказ, который по прогнозу будет выполнен позже срока, или курьер держит
    больше deleg_queue_limit заказов; кандидат на передачу — первый опаздывающий заказ, иначе последний;
  * передаются только ещё не начатые заказы (посылка не на борту); на время переговоров заказ откладывается
    в сторону, чтобы курьер не начал его выполнять;
  * соседи опрашиваются по одному в случайном порядке, не более deleg_attempts за попытку (не тот,
    от кого заказ получен); тайм-аут ответа 2·latency+1; если все отказали — заказ возвращается на место
    в очереди и не предлагается deleg_cooldown тактов;
  * один заказ передаётся не более deleg_max_hops раз (защита от «пинг-понга»).
Правила курьера B: согласен, если может взять заказ (грузоподъёмность, запас хода), после этого сам не
будет перегружен (заказов не больше deleg_queue_limit) и выполнит заказ раньше A хотя бы на deleg_gain тактов.

Параметры (SimConfig.params): deleg_queue_limit (2), deleg_attempts (3), deleg_max_hops (2),
deleg_gain (2), deleg_cooldown (15).
"""
from __future__ import annotations

import random

from lab1.mas.agents.base import Plugin
from lab1.mas.agents.courier import STRANDED
from lab1.mas.agents.dispatcher import ASSIGNED, DispatcherAgent
from lab1.mas.allocation import RETRY_AFTER
from lab1.mas.messages import Message, Perf
from lab1.mas.world import Order, manhattan

from .common import bump, init_counters, param, queue_etas

PROTOCOL = "delegate"
COUNTERS = ("delegations", "delegation_requests", "delegation_refusals", "delegation_giveups")


class DelegationRouter(Plugin):
    """Диспетчер-маршрутизатор: первичное назначение ближайшему курьеру и учёт передач между курьерами."""

    name = "delegation-router"

    def on_message(self, d: DispatcherAgent, msg: Message) -> bool:
        if msg.protocol != PROTOCOL:
            return False
        with d.sim.metrics.timer("allocation"):
            c = msg.content
            rec = d.records.get(c.get("order_id"))
            # Передача учитывается, только если заказ ещё числится за отправителем и не забран
            if msg.perf == Perf.INFORM and rec is not None and rec.status == ASSIGNED and rec.courier == msg.sender:
                rec.hops += 1
                d.set_status(rec, ASSIGNED, str(c.get("to")))
                d.log("delegated", order=rec.order.id, frm=msg.sender, to=c.get("to"))
        return True

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
                    dist = manhattan(tuple(b["pos"]), rec.order.pickup)
                    if best is None or (dist, cid) < best:
                        best = (dist, cid)
                if best is not None:
                    d.assign(rec, best[1])


class DelegationPeer(Plugin):
    """Курьер-участник делегирования: сам решает, когда отдать заказ и брать ли чужой."""

    name = "delegation-peer"

    def __init__(self, rng: random.Random, queue_limit=2, attempts=3, max_hops=2, gain=2, cooldown=15):
        self.rng = rng
        self.queue_limit, self.attempts, self.max_hops = queue_limit, attempts, max_hops
        self.gain, self.cooldown = gain, cooldown
        self.offer: dict | None = None          # заказ, который сейчас предлагается соседу
        self.hops: dict[str, int] = {}          # заказ -> сколько раз его уже передавали
        self.came_from: dict[str, str] = {}     # заказ -> от кого получен
        self.blocked_until: dict[str, int] = {}

    # --- инициатива: передать заказ ---
    def step(self, c, tick: int) -> None:
        with c.sim.metrics.timer("bidding"):
            if self.offer and tick - self.offer["sent"] > 2 * c.sim.cfg.msg_latency + 1:
                self.next_peer(c, tick)
            if self.offer is None and c.state != STRANDED and c.queue:
                self.maybe_delegate(c, tick)

    def eligible(self, o: Order, tick: int) -> bool:
        return self.hops.get(o.id, 0) < self.max_hops and self.blocked_until.get(o.id, -1) <= tick

    def maybe_delegate(self, c, tick: int) -> None:
        etas = queue_etas(c)
        cand = next(((i, o, eta) for i, (o, eta) in enumerate(etas) if eta > o.deadline and self.eligible(o, tick)),
                    None)
        if cand is None and len(c.tasks()) > self.queue_limit:
            cand = next(((i, o, eta) for i, (o, eta) in reversed(list(enumerate(etas))) if self.eligible(o, tick)),
                        None)
        if cand is None:
            return
        idx, o, eta = cand
        peers = [a.id for a in c.sim.agents_by_role("courier") if a.id not in (c.id, self.came_from.get(o.id))]
        self.rng.shuffle(peers)
        if not peers:
            return
        c.queue = [q for q in c.queue if q.id != o.id]       # отложить на время переговоров
        self.offer = {"order": o, "index": idx, "eta": eta, "peers": peers[: self.attempts], "peer": None, "sent": tick}
        self.next_peer(c, tick)

    def next_peer(self, c, tick: int) -> None:
        off = self.offer
        if not off["peers"]:                 # все отказали — вернуть заказ на место
            o = off["order"]
            if not any(t.id == o.id for t in c.tasks()):
                c.queue.insert(min(off["index"], len(c.queue)), o)
            self.blocked_until[o.id] = tick + self.cooldown
            self.offer = None
            bump(c.sim, "delegation_giveups")
            return
        off["peer"], off["sent"] = off["peers"].pop(0), tick
        o = off["order"]
        bump(c.sim, "delegation_requests")
        c.send(off["peer"], Perf.REQUEST, PROTOCOL,
               {"order": o.to_msg(), "eta": off["eta"], "hops": self.hops.get(o.id, 0)}, conv=o.id)

    # --- сообщения ---
    def on_message(self, c, msg: Message) -> bool:
        if msg.protocol != PROTOCOL:
            return False
        with c.sim.metrics.timer("bidding"):
            if msg.perf == Perf.REQUEST:
                self.on_request(c, msg)
            elif msg.perf == Perf.AGREE:
                self.on_agree(c, msg)
            elif msg.perf == Perf.REFUSE:
                off = self.offer
                if off and off["order"].id == msg.content.get("order_id") and off["peer"] == msg.sender:
                    bump(c.sim, "delegation_refusals")
                    self.next_peer(c, c.sim.tick)
            elif msg.perf == Perf.CANCEL:
                oid = msg.content.get("order_id")
                if c.drop_task(oid):
                    self.hops.pop(oid, None)
                    c.log("delegation_cancelled", order=oid, by=msg.sender)
        return True

    def on_request(self, c, msg: Message) -> None:
        m = msg.content
        order = Order.from_msg(m["order"])
        ok, reason = c.can_take(order)
        if ok and any(t.id == order.id for t in c.tasks()):
            ok, reason = False, "duplicate"
        if ok and len(c.tasks()) + 1 > self.queue_limit:
            ok, reason = False, "busy"
        eta = None
        if ok:
            eta = c.estimate(order)["eta"]
            if eta + self.gain > float(m["eta"]):
                ok, reason = False, "no-gain"
        if not ok:
            c.reply(msg, Perf.REFUSE, {"order_id": order.id, "reason": reason})
            return
        c.accept_task(order)
        self.hops[order.id] = int(m.get("hops", 0)) + 1
        self.came_from[order.id] = msg.sender
        c.reply(msg, Perf.AGREE, {"order_id": order.id, "eta": eta})

    def on_agree(self, c, msg: Message) -> None:
        oid = msg.content.get("order_id")
        off = self.offer
        if off and off["order"].id == oid and off["peer"] == msg.sender:
            self.offer = None
        elif not c.drop_task(oid):
            # согласие опоздало (после тайм-аута), а заказ уже не у нас: предлагается другому соседу,
            # передан или посылка на борту — отзываем согласие соседа
            c.send(msg.sender, Perf.CANCEL, PROTOCOL, {"order_id": oid}, conv=oid)
            return
        hops = self.hops.pop(oid, 0) + 1
        bump(c.sim, "delegations")
        c.log("delegated", order=oid, to=msg.sender)
        c.send(c.dispatcher_id, Perf.INFORM, PROTOCOL, {"order_id": oid, "to": msg.sender, "hops": hops}, conv=oid)


def install_delegation(sim) -> None:
    init_counters(sim, COUNTERS)
    sim.dispatcher.set_allocator(DelegationRouter())
    p = sim.cfg.params
    for i, c in enumerate(sim.couriers):
        c.add_plugin(DelegationPeer(
            random.Random(sim.cfg.seed * 1009 + i),
            queue_limit=int(p.get("deleg_queue_limit", 2)), attempts=int(p.get("deleg_attempts", 3)),
            max_hops=int(p.get("deleg_max_hops", 2)), gain=float(p.get("deleg_gain", 2)),
            cooldown=int(p.get("deleg_cooldown", 15))))
