"""Арбитр мощности — новая роль «Энергокоординатор сети» (agent id "grid", роль "grid").

Общесистемное ограничение: одновременно заряжаться могут не более grid_power_limit курьеров.
Станция не начинает зарядку без разрешения (токена мощности) арбитра. Протокол "power":

  station -> grid  REQUEST {req, prio}        запрос токена для ожидающего курьера с критичностью prio
  grid -> station  AGREE   {req}              токен выдан (одна единица мощности)
  station -> grid  CANCEL  {req}              запрос больше не нужен (ожидающий ушёл)
  station -> grid  INFORM  {event: release}   токен возвращён (зарядка закончилась)
  grid -> station  INFORM  {share}            справедливая доля лимита (для оценок ожидания станцией)
  grid -> station  INFORM  {contention}       есть неудовлетворённые запросы: включить частичную зарядку

Конфликт (запросов больше, чем свободных токенов) арбитр решает по правилу:
токен получает запрос с максимальной оценкой prio + w_wait * (такт - такт запроса) / aging_ticks
(старение не даёт станции с низкоприоритетными курьерами ждать бесконечно).
"""
from __future__ import annotations

import math

from lab1.mas.agents.base import Agent
from lab1.mas.messages import Message, Perf

from .common import P


class GridArbiterAgent(Agent):
    role = "grid"

    def __init__(self, aid: str, sim):
        super().__init__(aid, sim)
        self.limit = sim.cfg.grid_power_limit
        self.in_use: dict[str, int] = {}               # станция -> выданных токенов
        self.pending: list[dict] = []                  # {station, req, prio, t}
        self.contention_sent: dict[str, bool] = {}
        self.started = False

    def total_in_use(self) -> int:
        return sum(self.in_use.values())

    def handle(self, msg: Message) -> None:
        if msg.protocol != "power":
            return super().handle(msg)
        sid, c = msg.sender, msg.content
        if msg.perf == Perf.REQUEST:
            self.pending.append({"station": sid, "req": c.get("req"), "prio": float(c.get("prio", 0.0)),
                                 "t": self.sim.tick})
        elif msg.perf == Perf.CANCEL:
            self.pending = [p for p in self.pending if not (p["station"] == sid and p["req"] == c.get("req"))]
        elif msg.perf == Perf.INFORM and c.get("event") == "release":
            self.in_use[sid] = max(0, self.in_use.get(sid, 0) - 1)

    def score(self, p: dict, tick: int) -> float:
        return p["prio"] + P(self.sim, "w_wait") * (tick - p["t"]) / P(self.sim, "aging_ticks")

    def act(self, tick: int) -> None:
        stations = self.sim.stations
        if not self.started:
            self.started = True
            total_slots = sum(s.slots for s in stations) or 1
            for s in stations:
                share = s.slots if self.limit is None else max(1, min(s.slots, math.floor(
                    self.limit * s.slots / total_slots + 0.5)))
                self.send(s.id, Perf.INFORM, "power", {"share": share, "limit": self.limit})
        with self.sim.metrics.timer("grid_arbiter"):
            free = math.inf if self.limit is None else self.limit - self.total_in_use()
            while free > 0 and self.pending:
                best = max(self.pending, key=lambda p: (self.score(p, tick), -p["t"]))
                self.pending.remove(best)
                sid = best["station"]
                self.in_use[sid] = self.in_use.get(sid, 0) + 1
                free -= 1
                self.sim.lab3.grid_waits.append(tick - best["t"])
                self.send(sid, Perf.AGREE, "power", {"req": best["req"]})
                self.log("power_granted", station=sid, req=best["req"], prio=round(best["prio"], 2),
                         in_use=self.total_in_use())
            busy = bool(self.pending)
            for s in stations:
                want = busy and self.in_use.get(s.id, 0) > 0
                if self.contention_sent.get(s.id, False) != want:
                    self.contention_sent[s.id] = want
                    self.send(s.id, Perf.INFORM, "power", {"contention": want})
