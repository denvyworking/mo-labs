"""Среда: городская сетка, точки выдачи, зарядные станции, физическое состояние посылок."""
from __future__ import annotations

import random
from dataclasses import asdict, dataclass

Pos = tuple[int, int]

HUB_LAYOUT = [(0.2, 0.2), (0.8, 0.2), (0.2, 0.8), (0.8, 0.8), (0.5, 0.5), (0.5, 0.2), (0.2, 0.5), (0.8, 0.5)]
STATION_LAYOUT = [(0.5, 0.1), (0.5, 0.9), (0.1, 0.5), (0.9, 0.5), (0.35, 0.35), (0.65, 0.65)]


def manhattan(a: Pos, b: Pos) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def step_towards(a: Pos, b: Pos) -> Pos:
    """Один шаг по сетке к цели: сначала по оси с большим остатком пути."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    if dx == 0 and dy == 0:
        return a
    if abs(dx) >= abs(dy):
        return (a[0] + (1 if dx > 0 else -1), a[1])
    return (a[0], a[1] + (1 if dy > 0 else -1))


def _place(frac: tuple[float, float], w: int, h: int) -> Pos:
    return (min(w - 1, round(frac[0] * (w - 1))), min(h - 1, round(frac[1] * (h - 1))))


@dataclass
class Order:
    id: str
    pickup: Pos
    dropoff: Pos
    weight: float
    priority: str          # "normal" | "urgent"
    created: int
    deadline: int
    reward: float
    note: str = ""         # свободный текст клиента (данные, не команды)

    def trip(self) -> int:
        return manhattan(self.pickup, self.dropoff)

    def to_msg(self) -> dict:
        d = asdict(self)
        d["pickup"], d["dropoff"] = list(self.pickup), list(self.dropoff)
        return d

    @staticmethod
    def from_msg(d: dict) -> "Order":
        return Order(
            id=str(d["id"]), pickup=tuple(d["pickup"]), dropoff=tuple(d["dropoff"]),
            weight=float(d["weight"]), priority=str(d.get("priority", "normal")),
            created=int(d["created"]), deadline=int(d["deadline"]), reward=float(d["reward"]),
            note=str(d.get("note", "")),
        )


class World:
    """Физическая истина: где находятся посылки. Агенты знают её только через сообщения и свои датчики."""

    def __init__(self, cfg, sim):
        self.sim = sim
        self.width, self.height = cfg.width, cfg.height
        self.hubs: list[Pos] = [_place(HUB_LAYOUT[i % len(HUB_LAYOUT)], cfg.width, cfg.height) for i in range(cfg.n_hubs)]
        self.station_pos: dict[str, Pos] = {
            f"station-{i + 1}": _place(STATION_LAYOUT[i % len(STATION_LAYOUT)], cfg.width, cfg.height)
            for i in range(cfg.n_stations)
        }
        self.orders: dict[str, Order] = {}
        self.parcels: dict[str, str] = {}   # order_id -> "waiting" | "carried:<courier>" | "delivered"

    def publish(self, order: Order) -> None:
        self.orders[order.id] = order
        self.parcels[order.id] = "waiting"

    def try_pickup(self, oid: str, cid: str, pos: Pos) -> tuple[bool, str]:
        o = self.orders.get(oid)
        if o is None:
            return False, "unknown-order"
        if pos != o.pickup:
            return False, "wrong-place"
        if self.parcels[oid] != "waiting":
            return False, "already-taken"
        self.parcels[oid] = f"carried:{cid}"
        self.sim.metrics.on_pickup(oid, cid, self.sim.tick)
        return True, "ok"

    def try_deliver(self, oid: str, cid: str, pos: Pos) -> tuple[bool, str]:
        o = self.orders.get(oid)
        if o is None:
            return False, "unknown-order"
        if self.parcels.get(oid) != f"carried:{cid}":
            return False, "not-carried"
        if pos != o.dropoff:
            return False, "wrong-place"
        self.parcels[oid] = "delivered"
        self.sim.metrics.on_delivered(oid, cid, self.sim.tick)
        return True, "ok"


def generate_orders(cfg, world: World, rng: random.Random) -> list[Order]:
    """Пуассоновский поток заказов. Зависит только от seed, поэтому одинаков для всех режимов."""
    orders: list[Order] = []
    t = 0.0
    for i in range(cfg.n_orders):
        t += rng.expovariate(cfg.order_rate)
        created = int(t)
        pickup = rng.choice(world.hubs)
        while True:
            drop = (rng.randrange(cfg.width), rng.randrange(cfg.height))
            if manhattan(drop, pickup) >= 3:
                break
        heavy = rng.random() < cfg.heavy_share
        weight = round(rng.uniform(6, 18) if heavy else rng.uniform(0.5, 4.5), 1)
        urgent = rng.random() < cfg.urgent_share
        trip = manhattan(pickup, drop)
        deadline = created + int(25 + 1.5 * trip if urgent else 60 + 2.5 * trip)
        reward = round((10 + 0.5 * trip) * (1.5 if urgent else 1.0) * (1.3 if heavy else 1.0), 1)
        orders.append(Order(
            id=f"o{i + 1:03d}", pickup=pickup, dropoff=drop, weight=weight,
            priority="urgent" if urgent else "normal", created=created, deadline=deadline, reward=reward,
        ))
    return orders
