"""Механизм 3 (режим charging=reputation): переговоры + арбитр мощности + репутация и санкции.

Новая роль «Реестр репутации» (agent id "reputation", роль "reputation"). Репутация r ∈ [0, 1], старт 1.

Наблюдения станций (протокол "reputation", station -> reputation INFORM):
  {event: session, courier, claimed, measured_battery} — при подключении курьера станция измеряет заряд
      батареи своим датчиком и сообщает его вместе с заявкой курьера;
  {event: overstay, courier} — курьер не ушёл в течение overstay_grace тактов после уведомления о квоте
      (станция отключает зарядку — выселяет — и шлёт курьеру INFORM{event: evicted});
  {event: noshow, courier} — бронь истекла, курьер не приехал и не отменил её.
Проверка заявленной срочности (протокол "audit"): reputation -> dispatcher QUERY {courier};
  dispatcher -> reputation INFORM {orders: [{id, priority, deadline}]} — по книге заказов диспетчера.
Решение реестра: ложь (заряд занижен больше допуска, заявлен срочный заказ, которого нет, или срок
  раньше фактического) — r -= rep_lie; пересиживание — r -= rep_overstay; неявка — r -= rep_noshow;
  честный сеанс — r += rep_honest. Изменение r рассылается станциям (reputation -> station INFORM {courier, r}).
Санкции на станции: доверие к заявке = r (при r < rep_ban — 0, приоритет только по старению);
  при r < rep_ban отказ в брони (REFUSE{reason: reputation}); выселение при пересиживании.
"""
from __future__ import annotations

from lab1.mas.agents.base import Agent, Plugin
from lab1.mas.agents.dispatcher import ASSIGNED, ASSIGNING, PICKED
from lab1.mas.agents.station import StationAgent
from lab1.mas.messages import Message, Perf

from .common import P
from .negotiation import NegotiatingStationPolicy


class ReputationAgent(Agent):
    role = "reputation"

    def __init__(self, aid: str, sim, dispatcher_id: str = "dispatcher"):
        super().__init__(aid, sim)
        self.dispatcher_id = dispatcher_id
        self.r: dict[str, float] = {}
        self.audits: dict[str, dict] = {}
        self.seq = 0

    def handle(self, msg: Message) -> None:
        c = msg.content
        if msg.protocol == "reputation" and msg.perf == Perf.INFORM:
            with self.sim.metrics.timer("reputation"):
                self.on_observation(msg.sender, c)
        elif msg.protocol == "audit" and msg.perf == Perf.INFORM:
            with self.sim.metrics.timer("reputation"):
                self.on_audit(msg.conv, c)
        else:
            super().handle(msg)

    def on_observation(self, station: str, c: dict) -> None:
        cid, ev = c.get("courier"), c.get("event")
        if ev == "session":
            claimed = c.get("claimed", {})
            lies = []
            if float(c.get("measured_battery", 0)) - float(claimed.get("battery", 0)) > P(self.sim, "lie_tol_battery"):
                lies.append("battery")
            if claimed.get("deadline") is not None or claimed.get("urgent"):
                self.seq += 1
                conv = f"audit{self.seq}"
                self.audits[conv] = {"courier": cid, "claimed": claimed, "lies": lies}
                self.send(self.dispatcher_id, Perf.QUERY, "audit", {"courier": cid}, conv=conv)
            else:
                self.conclude(cid, lies)
        elif ev == "overstay":
            self.sanction(cid, P(self.sim, "rep_overstay"), "overstay")
        elif ev == "noshow":
            self.sanction(cid, P(self.sim, "rep_noshow"), "noshow")

    def on_audit(self, conv: str | None, c: dict) -> None:
        a = self.audits.pop(conv, None)
        if a is None:
            return
        claimed, lies = a["claimed"], list(a["lies"])
        orders = c.get("orders", [])
        true_dl = min((o["deadline"] for o in orders), default=None)
        if claimed.get("urgent") and not any(o["priority"] == "urgent" for o in orders):
            lies.append("urgent")
        dl = claimed.get("deadline")
        if dl is not None and (true_dl is None or dl < true_dl - P(self.sim, "lie_tol_deadline")):
            lies.append("deadline")
        self.conclude(a["courier"], lies)

    def conclude(self, cid: str, lies: list[str]) -> None:
        if lies:
            self.sanction(cid, P(self.sim, "rep_lie"), "lie:" + ",".join(lies))
        else:
            self.set_r(cid, min(1.0, self.r.get(cid, 1.0) + P(self.sim, "rep_honest")), "honest")

    def sanction(self, cid: str, delta: float, reason: str) -> None:
        self.sim.lab3.on_sanction(cid, reason)
        self.set_r(cid, max(0.0, self.r.get(cid, 1.0) - delta), reason)

    def set_r(self, cid: str, value: float, reason: str) -> None:
        value = round(value, 4)
        if value == self.r.get(cid, 1.0):
            return
        self.r[cid] = value
        self.sim.lab3.reputation[cid] = value
        self.log("reputation", courier=cid, r=value, reason=reason)
        for s in self.sim.stations:
            self.send(s.id, Perf.INFORM, "reputation", {"courier": cid, "r": value, "reason": reason})


class AuditResponder(Plugin):
    """Плагин диспетчера: отвечает реестру репутации, какие заказы закреплены за курьером (по книге заказов)."""

    name = "lab3-audit"

    def on_message(self, d, msg: Message) -> bool:
        if msg.protocol != "audit":
            return False
        if msg.perf == Perf.QUERY:
            cid = msg.content.get("courier")
            orders = [{"id": r.order.id, "priority": r.order.priority, "deadline": r.order.deadline}
                      for r in d.records.values() if r.courier == cid and r.status in (ASSIGNING, ASSIGNED, PICKED)]
            d.reply(msg, Perf.INFORM, {"courier": cid, "orders": orders})
        return True


class ReputationStationPolicy(NegotiatingStationPolicy):
    name = "lab3-reputation"

    def __init__(self, grid_id: str = "grid", registry_id: str = "reputation"):
        super().__init__(grid_id)
        self.registry_id = registry_id
        self.rep: dict[str, float] = {}     # локальная копия репутации (рассылается реестром)

    def trust(self, cid: str) -> float:
        r = self.rep.get(cid, 1.0)
        return r if r >= P(self.agent.sim, "rep_ban") else 0.0

    def refuse_booking(self, st: StationAgent, cid: str) -> str | None:
        return "reputation" if self.rep.get(cid, 1.0) < P(st.sim, "rep_ban") else None

    def on_message(self, st: StationAgent, msg: Message) -> bool:
        if msg.protocol == "reputation":
            if msg.perf == Perf.INFORM and msg.sender == self.registry_id:
                self.rep[msg.content.get("courier")] = float(msg.content.get("r", 1.0))
            return True
        return super().on_message(st, msg)

    def report(self, st: StationAgent, content: dict) -> None:
        st.send(self.registry_id, Perf.INFORM, "reputation", content)

    def on_admitted(self, st: StationAgent, cid: str) -> None:
        inf, c = self.info.get(cid), st.sim.agents.get(cid)
        if inf is None or c is None:
            return
        claimed = {k: inf["claims"].get(k) for k in ("battery", "deadline", "urgent", "need")}
        # заряд подключённой батареи измеряет датчик зарядного места
        self.report(st, {"event": "session", "courier": cid, "claimed": claimed,
                         "measured_battery": round(c.battery, 2)})

    def after_quota(self, st: StationAgent, tick: int) -> None:
        grace = P(st.sim, "overstay_grace")
        for cid, tq in list(self.quota_sent.items()):
            if cid in st.occupied and tick - tq >= grace:
                st.occupied.pop(cid, None)          # отключить зарядку: место и мощность освобождаются
                self.quota_sent.pop(cid, None)
                self.info.pop(cid, None)
                if cid in self.powered:
                    self.release_token(st, cid)
                st.sim.lab3.inc("l3_evictions")
                st.send(cid, Perf.INFORM, "charge", {"event": "evicted"})
                st.log("evicted", courier=cid)
                self.report(st, {"event": "overstay", "courier": cid})

    def on_noshow(self, st: StationAgent, cid: str) -> None:
        self.report(st, {"event": "noshow", "courier": cid})
