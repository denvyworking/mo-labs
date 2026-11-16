# ЛР3 — механизм переговоров и разрешения конфликтов (зарядка курьеров)

Конфликт: курьеры конкурируют за места на зарядных станциях и за общий лимит мощности сети
(`grid_power_limit`). Режим «до» — `charging=naive` из ядра (ближайшая станция, FIFO, лимит никто не
соблюдает). Режимы «после» регистрируются из `lab3/plugin.py` в категории `charging` и видны в CLI и веб-демо.

## Запуск (из корня репозитория)

```
python lab3/experiments.py            # E1–E3, 30 seed, ~3 мин -> lab3/results/*.csv, lab3/figures/*.png
python lab3/experiments.py --quick    # 8 seed, проверка (~1 мин)
python lab3/experiments.py --only e1  # один эксперимент: e1 | e2 | e3 | timeline
python lab1/run.py --list                                  # среди режимов charging — режимы lab3
python lab1/run.py --charging negotiation --couriers 14 --seed 2
python lab1/web/server.py --port 8000                      # веб-демо, выбрать режим зарядки в списке
python lab1/tools/md2docx.py lab3/report.md lab3/report.docx
```

В веб-демо для заметного конфликта задайте 14 курьеров, лимит сети 2–3 и 150 заказов
(скорость зарядки в веб-форме не настраивается, в экспериментах она снижена до 5).

## Режимы (`charging=...`)

| Ключ | Что добавляет |
|---|---|
| `priority` | Очередь на станции по критичности (заряд, близость срока заказа, срочность, старение); частичная зарядка до квоты, если кто-то ждёт. Лимит сети не координируется. |
| `negotiation` | Переговоры о слоте (CFP → PROPOSE/REFUSE → ACCEPT/REJECT → бронь с окном, перезапрос при истечении брони) + новый агент `grid` (энергокоординатор): станция начинает зарядку только по токену мощности; конфликт токенов решается по критичности со старением; предзапрос токена по брони. |
| `reputation` | Как `negotiation` + новый агент `reputation` (реестр репутации): станции сообщают измеренный заряд и заявку, реестр сверяет срочность с книгой диспетчера (протокол `audit`), снижает репутацию за ложь, неявку по брони и пересиживание квоты; санкции — снижение доверия к заявке, запрет брони, выселение. |
| `negotiation-selfish`, `reputation-selfish` | Пресеты для веб-демо и CLI, где нельзя задать `params`: то же, что `negotiation` / `reputation`, с `selfish_share=0.3`. |

Новые роли (`roles.ROLES`): `grid` — «Энергокоординатор сети (арбитр мощности)», `reputation` — «Реестр
репутации (арбитр санкций)». Новые протоколы (`roles.allow`): `slot` (courier↔station), `power`
(station↔grid), `reputation` (station↔reputation), `audit` (reputation↔dispatcher). Протокол ядра `charge`
используется с дополнительными полями заявки и событиями `quota` / `evicted`.

## Параметры (`SimConfig.params`, значения по умолчанию — `lab3/common.py: DEFAULTS`)

| Параметр | По умолчанию | Смысл |
|---|---|---|
| `selfish_share` | 0.0 | доля эгоистичных курьеров (выбираются среди лёгких, равномерно по списку) |
| `selfish_lie`, `selfish_overstay`, `selfish_noshow` | True | виды нечестности: ложная заявка, игнорирование квоты, бронирование всех станций |
| `w_battery`, `w_deadline`, `w_urgent`, `w_wait`, `aging_ticks`, `slack_scale`, `w_booking` | 1, 2, 0.5, 1, 20, 40, 1 | веса формулы критичности |
| `partial_charge`, `quota_margin`, `quota_floor`, `full_level` | True, 0.15, 0.5, 0.95 | частичная зарядка: квота = потребность + запас, в пределах [floor, full] × ёмкость |
| `cfp_timeout`, `booking_grace` | 3, 4 | ожидание предложений; окно брони после ETA |
| `rep_lie`, `rep_overstay`, `rep_noshow`, `rep_honest`, `rep_ban` | 0.3, 0.2, 0.15, 0.05, 0.5 | изменения репутации и порог санкций |
| `overstay_grace`, `lie_tol_battery`, `lie_tol_deadline` | 3, 2.0, 5 | допуски проверок |

Пример: `Simulation(cfg, charging="reputation", params={"selfish_share": 0.25})`.

## Файлы

- `plugin.py` — `register()`: режимы, роли, протоколы.
- `common.py` — параметры, заявка курьера, формула критичности, квота, эгоисты, показатели `l3_*`.
- `priority.py` — стратегия курьера с заявкой; станция с приоритетной очередью и квотой.
- `negotiation.py` — переговоры о слоте (курьер и станция), брони, токены мощности на станции.
- `grid.py` — агент-энергокоординатор (арбитр мощности).
- `reputation.py` — реестр репутации, плагин аудита для диспетчера, станция с санкциями.
- `modes.py` — установка режимов.
- `experiments.py` — эксперименты E1–E3 и графики.
- `report.md` / `report.docx` — отчёт.

Дополнительные показатели в итогах прогона (`metrics.custom`): `l3_wait_honest` / `l3_wait_selfish`,
`l3_earn_honest` / `l3_earn_selfish`, `l3_gini_wait`, `l3_gini_earn`, `l3_quota_stops`, `l3_bookings`,
`l3_noshows`, `l3_rebookings`, `l3_fallbacks`, `l3_power_requests`, `l3_grid_wait_avg`, `l3_lies_detected`,
`l3_false_accusations`, `l3_overstays`, `l3_evictions`, `l3_booking_refusals`, `l3_rep_honest`, `l3_rep_selfish`.
