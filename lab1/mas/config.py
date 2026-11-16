"""Параметры моделирования МАС «Городская доставка автономными курьерами»."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

# Типы курьеров: грузоподъёмность (кг), ёмкость батареи (усл. ед.),
# расход энергии на шаг без груза и с грузом.
COURIER_TYPES: dict[str, dict[str, float]] = {
    "light": {"payload": 5.0, "battery": 100.0, "cons_empty": 1.0, "cons_loaded": 1.3},
    "heavy": {"payload": 20.0, "battery": 160.0, "cons_empty": 1.4, "cons_loaded": 1.8},
}


@dataclass
class SimConfig:
    seed: int = 1
    # Среда
    width: int = 20
    height: int = 20
    n_hubs: int = 4                 # точки выдачи заказов (склады / рестораны)
    n_stations: int = 2             # зарядные станции
    station_slots: int = 2          # число одновременных мест зарядки на станции
    charge_rate: float = 10.0       # прирост заряда за такт на месте зарядки
    grid_power_limit: int | None = None  # общесистемный лимит одновременно заряжающихся (None — без лимита)
    # Флот
    n_couriers: int = 8
    heavy_couriers: int = 2         # первые heavy_couriers курьеров — тяжёлые
    battery_reserve: float = 0.1    # доля ёмкости, которую курьер держит в резерве
    tow_delay: int = 40             # тактов до эвакуации разрядившегося курьера
    # Поток заказов
    n_orders: int = 60
    order_rate: float = 0.2         # средняя интенсивность (заказов за такт), пуассоновский поток
    heavy_share: float = 0.2
    urgent_share: float = 0.2
    # Связь и контроль
    msg_latency: int = 1            # задержка доставки сообщения, тактов
    telemetry_period: int = 5       # период телеметрии курьер -> диспетчер
    watchdog_timeout: int = 25      # молчание курьера, после которого диспетчер снимает с него заказы
    assign_timeout: int = 6         # ожидание ответа курьера на назначение
    max_ticks: int = 2000
    # Режимы (ключи из mas.registry)
    allocation: str = "central"
    charging: str = "naive"
    scenario: str = "none"
    defense: str = "none"
    # Наблюдение
    record_frames: bool = False     # кадры для веб-демонстрации
    trace_messages: bool = True     # писать каждое сообщение в трассу
    # Свободные параметры для механизмов из ЛР2–ЛР4
    params: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    def replace(self, **overrides) -> "SimConfig":
        data = self.to_dict()
        params = dict(data.pop("params"))
        params.update(overrides.pop("params", {}) or {})
        data.update(overrides)
        return SimConfig(**data, params=params)
