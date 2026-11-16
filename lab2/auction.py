"""Аукцион по пакету накопившихся заказов, протокол "auction".

Аукционист — диспетчер, участники — курьеры. Раз в auction_period тактов (если предыдущий аукцион
завершён и есть заказы в очереди) аукционист выставляет пакет из не более чем auction_max_lots заказов.

Последовательный аукцион по одному лоту (SSI, kind="ssi"):

    аукционист -> все курьеры : CFP {auction, lots=[заказы], deadline}            (лоты: ANNOUNCED)
    курьер     -> аукционист  : PROPOSE {auction, bids={лот: ставка}}  или  REFUSE (ни один лот не по силам)
    -- раунд: аукционист выбирает пару (лот, курьер) с наименьшей ставкой среди всех текущих ставок --
    аукционист -> победитель  : ACCEPT-PROPOSAL {auction, order_id, price, remaining, rebid} (лот: ASSIGNING)
    победитель -> аукционист  : INFORM {order_id, status=committed}  или  FAILURE  (лот: ASSIGNED / PENDING)
    победитель -> аукционист  : PROPOSE {auction, bids} — новые ставки на оставшиеся лоты (если rebid)
    ... раунды повторяются, пока есть лоты со ставками ...
    аукционист -> участники   : REJECT-PROPOSAL {auction} — остальные предложения отклонены (аукцион закрыт)

Ставка курьера — маргинальная стоимость лота: ожидаемое время его завершения (такт), если поставить его
в конец своей очереди, где уже стоят выигранные лоты. Для критерия «сумма времён завершения заказов»
маргинальная стоимость добавления заказа в конец маршрута равна именно времени его завершения.
После раунда меняется только очередь победителя, поэтому новые ставки подаёт только он; ставки
остальных остаются в силе. Если у победителя нет ставок на оставшиеся лоты (например, не поднимет вес),
пересчёт не нужен (rebid=False).

Ускорение раундов без изменения исхода: после выигрыша ставки победителя на другие лоты могут только
вырасти (заказ добавлен в конец маршрута; неравенство треугольника), поэтому его прежние ставки — нижние
оценки новых. Если лучшая ставка остальных участников не больше этих оценок, исход следующего раунда
уже известен, и лот отдаётся сразу, не дожидаясь пересчёта.

Цена — по второй цене (Викри): победитель получает плату, равную наименьшей известной ставке остальных
участников на этот лот (отсчёт от начала аукциона); если других ставок нет — свою ставку.
Плата копится как доход курьера (показатели auction_income_total, jain_auction_income).

Параллельный одноразовый аукцион (kind="parallel"): один раунд, каждый лот независимо отдаётся
наименьшей ставке, ставки на разные лоты считаются без учёта друг друга (меньше раундов,
но курьер может выиграть несколько лотов, каждый из которых оценивал «в одиночку»).

Параметры (SimConfig.params): auction_period (5), auction_max_lots (6), auction_slack (1), auction_retry (10).
"""
from __future__ import annotations

from lab1.mas.agents.base import Plugin
from lab1.mas.agents.dispatcher import ANNOUNCED, ASSIGNING, DispatcherAgent
from lab1.mas.messages import Message, Perf
from lab1.mas.world import Order

from .common import add_income, bump, init_counters, param

PROTOCOL = "auction"
COUNTERS = ("auctions", "auction_lots", "auction_rebids", "auction_unsold", "auction_failures",
            "auction_income_total", "jain_auction_income")


class AuctionManager(Plugin):
    """Аукционист (плагин диспетчера)."""

    name = "auctioneer"

    def __init__(self, kind: str = "ssi", period: int = 5, max_lots: int = 6, slack: int = 1, retry: int = 10):
        self.kind = kind
        self.period = period
        self.max_lots = max_lots
        self.slack = slack
        self.retry = retry
        self.cur: dict | None = None            # текущий аукцион
        self.seq = 0
        self.last_start = -10**9
        self.retry_at: dict[str, int] = {}
        self.income: dict[str, float] = {}

    # --- сообщения участников ---
    def on_message(self, d: DispatcherAgent, msg: Message) -> bool:
        if msg.protocol != PROTOCOL:
            return False
        with d.sim.metrics.timer("allocation"):
            c, a = msg.content, self.cur
            if msg.perf in (Perf.PROPOSE, Perf.REFUSE):
                if a is None or c.get("auction") != a["id"] or msg.sender not in a["stale"]:
                    if msg.perf == Perf.PROPOSE:   # аукцион закрыт или ответ опоздал
                        d.send(msg.sender, Perf.REJECT, PROTOCOL, {"auction": c.get("auction"), "reason": "closed"})
                    return True
                del a["stale"][msg.sender]
                bids = c.get("bids", {}) if msg.perf == Perf.PROPOSE else {}
                a["bids"][msg.sender] = {oid: float(v) for oid, v in bids.items() if oid in a["open"]}
                if a["bids"][msg.sender]:
                    a["unresolved"].add(msg.sender)
            elif msg.perf in (Perf.INFORM, Perf.FAILURE):
                rec = d.records.get(c.get("order_id"))
                if rec is None or rec.status != ASSIGNING or rec.courier != msg.sender:
                    return True
                if msg.perf == Perf.INFORM:
                    d.confirm_assignment(rec, msg.sender)
                else:
                    bump(d.sim, "auction_failures")
                    d.release(rec, "auction-failure:" + str(c.get("reason")))
        return True

    # --- такт аукциониста ---
    def step(self, d: DispatcherAgent, tick: int) -> None:
        with d.sim.metrics.timer("allocation"):
            a = self.cur
            if a is not None:
                for cid, (_, deadline) in list(a["stale"].items()):
                    if tick >= deadline:          # не ответил вовремя — дальше участвует без ставок
                        del a["stale"][cid]
                        a["bids"][cid] = {}
                if self.kind == "parallel":
                    if not a["stale"]:
                        self.award_parallel(d, a, tick)
                else:
                    self.award_ssi(d, a, tick)
            if self.cur is None and tick - self.last_start >= self.period:
                self.start(d, tick)

    def start(self, d: DispatcherAgent, tick: int) -> None:
        lots = [r for r in d.pending() if self.retry_at.get(r.order.id, -1) <= tick][: self.max_lots]
        invited = d.couriers_available()
        if not lots or not invited:
            return
        self.seq += 1
        self.last_start = tick
        for r in lots:
            d.set_status(r, ANNOUNCED, None)
        deadline = tick + 2 * d.sim.cfg.msg_latency + self.slack
        # stale: участники, чьих (новых) ставок ждём: курьер -> (нижние оценки ставок или None, срок ответа)
        self.cur = a = {"id": self.seq, "t0": tick, "open": [r.order.id for r in lots], "bids": {},
                        "stale": {cid: (None, deadline) for cid in invited}, "unresolved": set()}
        bump(d.sim, "auctions")
        bump(d.sim, "auction_lots", len(lots))
        payload = [r.order.to_msg() for r in lots]
        for cid in invited:
            d.send(cid, Perf.CFP, PROTOCOL, {"auction": a["id"], "kind": self.kind,
                                            "lots": [dict(o) for o in payload], "deadline": deadline})

    def price(self, a: dict, oid: str, winner: str, bid: float) -> float:
        """Вторая цена (Викри): наименьшая известная ставка остальных на этот лот
        (для ждущих пересчёта — их прежняя ставка, нижняя оценка новой); отсчёт от начала аукциона."""
        others = [b[oid] for cid, b in a["bids"].items() if cid != winner and oid in b]
        others += [lb[oid] for cid, (lb, _) in a["stale"].items() if lb and oid in lb]
        return round((min(others) if others else bid) - a["t0"], 2)

    def assign_lot(self, d: DispatcherAgent, a: dict, oid: str, winner: str, bid: float, rebid: bool,
                   remaining: list[str]) -> None:
        rec = d.records[oid]
        price = self.price(a, oid, winner, bid)
        d.set_status(rec, ASSIGNING, winner)
        add_income(d.sim, self.income, winner, price, "auction_income")
        d.send(winner, Perf.ACCEPT, PROTOCOL, {"auction": a["id"], "order_id": oid, "bid": bid, "price": price,
                                              "remaining": remaining, "rebid": rebid}, conv=oid)

    def award_ssi(self, d: DispatcherAgent, a: dict, tick: int) -> None:
        """Раунды SSI: каждый раз лот с наименьшей ставкой среди всех участников (см. описание модуля)."""
        while a["open"]:
            best = None
            for cid, bids in a["bids"].items():
                for oid in a["open"]:
                    if oid in bids and (best is None or (bids[oid], oid, cid) < best):
                        best = (bids[oid], oid, cid)
            bound = float("inf")                  # нижняя оценка ставок тех, кто пересчитывает
            for lb, _ in a["stale"].values():
                if lb is None:                    # первые ставки ещё не пришли — оценки нет
                    bound = float("-inf")
                    break
                bound = min([bound] + [v for o, v in lb.items() if o in a["open"]])
            if best is None or best[0] > bound:
                if a["stale"]:
                    return                        # исход раунда ещё не известен — ждём ставок
                break
            bid, oid, winner = best
            a["open"].remove(oid)
            a["unresolved"].discard(winner)
            old = {o: v for o, v in a["bids"].pop(winner).items() if o in a["open"]}
            self.assign_lot(d, a, oid, winner, bid, bool(old), list(a["open"]))
            if old:                               # победитель пересчитает ставки на оставшиеся лоты
                a["stale"][winner] = (old, tick + 2 * d.sim.cfg.msg_latency + self.slack)
                bump(d.sim, "auction_rebids")
            else:
                a["bids"][winner] = {}
        self.close(d, a, tick)

    def award_parallel(self, d: DispatcherAgent, a: dict, tick: int) -> None:
        """Одноразовый параллельный аукцион: каждый лот — наименьшей ставке, независимо от других лотов."""
        for oid in list(a["open"]):
            cands = sorted((b[oid], cid) for cid, b in a["bids"].items() if oid in b)
            if not cands:
                continue
            bid, winner = cands[0]
            a["open"].remove(oid)
            self.assign_lot(d, a, oid, winner, bid, False, [])
        # курьер, выигравший все лоты, на которые ставил, — его предложения закрыты; остальным — отказ
        for cid, bids in a["bids"].items():
            if all(d.records[o].courier == cid and d.records[o].status == ASSIGNING for o in bids):
                a["unresolved"].discard(cid)
        self.close(d, a, tick)

    def close(self, d: DispatcherAgent, a: dict, tick: int) -> None:
        for oid in a["open"]:               # лоты без ставок — обратно в очередь, повтор позже
            rec = d.records[oid]
            if rec.status == ANNOUNCED:
                self.retry_at[oid] = tick + self.retry
                bump(d.sim, "auction_unsold")
                d.release(rec, "auction-unsold")
        for cid in sorted(a["unresolved"]):
            d.send(cid, Perf.REJECT, PROTOCOL, {"auction": a["id"], "reason": "closed"})
        self.cur = None


class AuctionBidder(Plugin):
    """Участник аукциона (плагин курьера): ставки — маргинальная стоимость с учётом выигранных лотов."""

    name = "auction-bidder"

    def __init__(self):
        self.auction: int | None = None
        self.lots: dict[str, Order] = {}
        self.bids: dict[str, tuple[float, int]] = {}   # лот -> (ставка, такт подачи)

    def on_message(self, c, msg: Message) -> bool:
        if msg.protocol != PROTOCOL:
            return False
        with c.sim.metrics.timer("bidding"):
            m = msg.content
            if msg.perf == Perf.CFP:
                self.auction = m["auction"]
                self.lots = {o["id"]: Order.from_msg(o) for o in m["lots"]}
                self.bids = {}
                self.send_bids(c, msg, list(self.lots))
            elif msg.perf == Perf.ACCEPT and m.get("auction") == self.auction:
                self.on_accept(c, msg)
            elif msg.perf == Perf.REJECT and m.get("auction") == self.auction:
                self.auction, self.lots, self.bids = None, {}, {}
        return True

    def send_bids(self, c, msg: Message, lot_ids: list[str]) -> None:
        bids = {}
        for oid in lot_ids:
            order = self.lots.get(oid)
            if order is None or not c.can_take(order)[0]:
                continue
            bids[oid] = c.estimate(order)["eta"]     # очередь уже содержит выигранные лоты
            self.bids[oid] = (bids[oid], c.sim.tick)
        if bids:
            c.reply(msg, Perf.PROPOSE, {"auction": self.auction, "bids": bids})
        else:
            c.reply(msg, Perf.REFUSE, {"auction": self.auction, "reason": "no-feasible-lots"})

    def on_accept(self, c, msg: Message) -> None:
        m = msg.content
        oid = m["order_id"]
        order = self.lots.pop(oid, None)
        ok, reason = (False, "unknown-lot") if order is None else c.can_take(order)
        if ok:   # перепроверка: в параллельном варианте ставка не учитывала другие выигранные лоты
            bid, t = self.bids.get(oid, (m.get("bid", 0.0), c.sim.tick))
            eta = c.estimate(order)["eta"]
            if eta > bid + (c.sim.tick - t) + 1e-9 and eta > order.deadline:
                ok, reason = False, "overcommitted"
        if ok:
            c.accept_task(order)
            c.reply(msg, Perf.INFORM, {"order_id": oid, "status": "committed", "price": m.get("price")})
        else:
            c.reply(msg, Perf.FAILURE, {"order_id": oid, "reason": reason})
        if m.get("rebid"):
            self.send_bids(c, msg, [o for o in m.get("remaining", []) if o in self.lots])


def _install(sim, kind: str) -> None:
    init_counters(sim, COUNTERS)
    sim.dispatcher.set_allocator(AuctionManager(
        kind=kind, period=int(param(sim, "auction_period", 5)), max_lots=int(param(sim, "auction_max_lots", 6)),
        slack=int(param(sim, "auction_slack", 1)), retry=int(param(sim, "auction_retry", 10))))
    for c in sim.couriers:
        c.add_plugin(AuctionBidder())


def install_auction(sim) -> None:
    _install(sim, "ssi")


def install_auction_parallel(sim) -> None:
    _install(sim, "parallel")
