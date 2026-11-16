# МАС «Городская доставка автономными курьерами» — ядро (ЛР1)

Python 3.11+, зависимости: только стандартная библиотека; для графиков `matplotlib`, для отчётов `python-docx`.
Все команды запускаются **из корня репозитория** (папка, где лежат `lab1 … lab4`).

```
python lab1/run.py --list                       # все режимы (вкл. ЛР2–ЛР4, если есть labN/plugin.py)
python lab1/run.py --seed 3 --couriers 8        # один прогон, таблица показателей
python lab1/run.py --trace trace.jsonl --json   # трасса + все показатели в JSON
python lab1/web/server.py --port 8000           # веб-демонстрация: http://localhost:8000
python lab1/tools/md2docx.py labN/report.md labN/report.docx   # отчёт в DOCX
```

## Предметная область

Сетка `width × height` (по умолчанию 20×20), манхэттенское движение, дискретное время (такт).
Заказы появляются пуассоновским потоком в точках выдачи (hubs) и доставляются в случайные клетки.
У заказа есть вес, срок (deadline), приоритет (normal/urgent), вознаграждение, свободный текст `note`.
Курьеры двух типов: `light` (до 5 кг, батарея 100) и `heavy` (до 20 кг, батарея 160); расход энергии
на шаг зависит от наличия груза. Зарядные станции имеют ограниченное число мест (`station_slots`);
опционально задаётся общесистемный лимит одновременно заряжающихся `grid_power_limit`.
Разрядившийся в пути курьер выбывает на `tow_delay` тактов (эвакуация).

Поток заказов зависит только от `seed` — при одинаковом seed все режимы получают одинаковые заказы
(метод общих случайных чисел для честного сравнения).

## Агенты и протоколы ядра

| Агент (роль) | id | Что делает |
|---|---|---|
| Клиент `client` | `client` | публикует заказы (`order`), получает уведомления о доставке |
| Диспетчер `dispatcher` | `dispatcher` | книга заказов, убеждения о курьерах (телеметрия), распределение (подключаемо), сторожевой таймер |
| Курьер `courier` | `courier-1..N` | принимает/отклоняет заказы, едет, забирает, доставляет, заряжается (стратегия подключаема), шлёт телеметрию |
| Станция `station` | `station-1..M` | места зарядки, очередь, политика допуска (подключаема) |

Протоколы (`msg.protocol`) и перформативы — `lab1/mas/roles.py` (`PROTOCOLS`, `PERMISSIONS`):
`order` (client→dispatcher REQUEST, ответ AGREE), `assign` (dispatcher→courier REQUEST/CANCEL;
courier→dispatcher AGREE/REFUSE/FAILURE), `order-status` (courier→dispatcher INFORM picked/delivered;
dispatcher→client INFORM delivered), `telemetry` (courier→dispatcher INFORM каждые `telemetry_period`),
`charge` (courier→station REQUEST / INFORM{event: done} / CANCEL; station→courier AGREE / REFUSE / INFORM{queued}).

Сообщение доставляется через `msg_latency` тактов. Физическая истина (где посылка) — в `World`
(`world.parcels`), агенты знают её только через сообщения и свои датчики.

## Структура кода

```
lab1/mas/config.py       SimConfig, COURIER_TYPES
lab1/mas/world.py        World, Order, manhattan, step_towards, generate_orders
lab1/mas/messages.py     Perf, Message, Bus (middlewares, задержка, учёт)
lab1/mas/agents/         base.py (Agent, Plugin), client.py, dispatcher.py, courier.py, station.py
lab1/mas/allocation.py   CentralizedAllocator (базовый механизм распределения), belief_eta()
lab1/mas/charging.py     NaiveCourierCharging, FIFOStationPolicy (базовая зарядка без переговоров)
lab1/mas/roles.py        ROLES, PROTOCOLS, PERMISSIONS, allow(), is_allowed()
lab1/mas/registry.py     реестр режимов, загрузка labN/plugin.py
lab1/mas/sim.py          Simulation (цикл, кадры), run_simulation()
lab1/mas/metrics.py      Metrics, LABELS, jain()
lab1/mas/experiment.py   run_batch(), aggregate(), save_csv()
lab1/mas/trace.py        Trace (события + сообщения, JSONL)
```

## Цикл такта

1. `sim.hooks_pre` (сбои, расписания), 2. шина доставляет созревшие сообщения,
3. каждый агент: сообщения → плагины (`on_message`, первый вернувший `True` поглощает) → `handle()` →
`plugin.step()` → `act()`; порядок агентов: client, dispatcher, stations, couriers (+ добавленные),
4. `metrics.on_tick`, 5. `sim.hooks_post`, 6. кадр для веба (если `record_frames`).

## Как расширять (ЛР2–ЛР4)

Каждая лабораторная — пакет `labN/` (с `__init__.py`) и файл `labN/plugin.py` с функцией `register()`,
которая регистрирует режимы. Ядро и веб-интерфейс подхватывают их автоматически.

```python
# labN/plugin.py
from lab1.mas import registry, roles

def register():
    from .my_mechanism import install_my_alloc
    roles.allow("dispatcher", "courier", "cnp", {"cfp", "accept-proposal", "reject-proposal"}, "Контрактная сеть")
    registry.register("allocation", "cnp", "Контрактная сеть", install_my_alloc,
                      description="...", lab="lab2")
```

Категории режимов и порядок установки: `scenario` → `allocation` → `charging` → `defense`.
`install(sim)` вызывается после построения агентов; `configure(cfg)` (необязательно) — до построения среды.

Точки расширения:

* **Распределение** — плагин диспетчера: `sim.dispatcher.set_allocator(MyAllocator())`.
  `step(d, tick)` вызывается каждый такт; `on_message(d, msg)` получает сообщения своего протокола.
  API диспетчера: `d.pending()` (заказы к распределению, отсортированы по срочности), `d.records`,
  `d.beliefs[cid]` (последняя телеметрия: pos, battery, tail_pos, tail_eta, tail_battery, payload, lost…),
  `d.couriers_available()`, `d.assign(rec, cid)` (прямое назначение REQUEST/AGREE),
  `d.set_status(rec, ANNOUNCED, None)` (заказ выставлен на торги — сторожевой таймер его не трогает),
  `d.confirm_assignment(rec, cid)` (назначение состоялось), `d.release(rec, reason)` (вернуть в очередь).
  Статусы: `PENDING, ANNOUNCED, ASSIGNING, ASSIGNED, PICKED, DELIVERED` в `mas/agents/dispatcher.py`.
* **Участие курьеров** — плагин курьера: `c.add_plugin(MyBidder())`. API курьера:
  `c.can_take(order) -> (ok, reason)`, `c.estimate(order) -> {extra, eta, energy, tail_battery, queue}`,
  `c.accept_task(order)`, `c.drop_task(oid)`, `c.tasks()`, `c.route_remaining()`, `c.pos`, `c.battery`,
  `c.capacity`, `c.state`, `c.queue`, `c.current`, `c.picked`. `Order.from_msg(d)` / `order.to_msg()`.
* **Зарядка** — стратегия курьера `c.set_charging(strategy)` (методы `update(c, tick)`, `on_arrival(c, tick)`,
  `on_message`) и политика станции `st.set_policy(policy)` (`on_message`, `step`). Удобно наследоваться от
  `NaiveCourierCharging` / `FIFOStationPolicy`. Физика зарядки в `StationAgent.act`: заряжаются те, кто
  в `st.occupied` и стоит на станции. Метрика `power_overload_ticks` считает такты, когда сумма
  `len(st.occupied)` по станциям больше `grid_power_limit`.
* **Новые агенты** — `sim.add_agent(MyAgent("arbiter", sim))` в `install`; роль описать в `roles.ROLES`.
* **Сбои и злоумышленники** — `sim.bus.middlewares.append(fn)` (fn(msg, sim) → msg | [msgs] | None),
  `sim.hooks_pre.append(fn)`, замена агента `sim.replace_agent(EvilCourier("courier-3", sim, "light", pos))`,
  поддельный отправитель: `sim.bus.send(Message(sender="dispatcher", ...), origin=self.id)` — шина
  запоминает истинный `origin`, который защитные механизмы могут проверять (модель подписи сообщений).
* **Защиты** — те же middlewares или плагин-фильтр агента `agent.add_plugin(guard, first=True)`
  (вернуть `True` из `on_message`, чтобы отбросить сообщение). Таблица полномочий: `roles.is_allowed()`.
* **Метрики** — `sim.metrics.custom["my_counter"] = ...` попадёт в итог; время работы механизма —
  `with sim.metrics.timer("allocation"):` (суммируется в `coord_cpu_ms`).
* **Параметры механизмов** — `SimConfig.params` (словарь), напр. `Simulation(cfg, params={"cfp_window": 2})`.

## Эксперименты

```python
from lab1.mas import SimConfig
from lab1.mas.experiment import run_batch, aggregate, save_csv
rows = run_batch({"central": {"allocation": "central"}, "cnp": {"allocation": "cnp"}}, seeds=range(1, 31))
agg = aggregate(rows, ["on_time_rate", "msgs_total"])   # {variant: {key: (mean, sd, ci95)}}
```

Скрипты экспериментов в `labN/` должны начинаться с добавления корня репозитория в `sys.path`
(как `lab1/run.py`) и вызывать `registry.load_lab_plugins()` или импортировать свой `labN.plugin` и вызывать `register()`.
