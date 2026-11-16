"""Формальная спецификация ролей и полномочий (машиночитаемая часть ЛР1).

Используется для: построения диаграммы ролей и графа коммуникаций (ЛР1),
проверки полномочий защитными механизмами (ЛР4). Механизмы ЛР2–ЛР3 дополняют таблицу через allow().
"""
from __future__ import annotations

ROLES: dict[str, dict] = {
    "client": {
        "title": "Клиент (шлюз заказов)",
        "goals": ["Получить доставку каждого заказа к сроку"],
        "knowledge": ["Свои заказы: место забора, место доставки, вес, срок, вознаграждение"],
        "actions": ["Опубликовать заказ", "Получить подтверждение доставки"],
        "authority": ["Создавать заказы от своего имени"],
        "means": ["Канал связи с диспетчером"],
    },
    "dispatcher": {
        "title": "Диспетчер",
        "goals": ["Доставить все заказы", "Максимизировать долю доставок в срок",
                  "Минимизировать простои и затраты на координацию"],
        "knowledge": ["Книга заказов и их статусы", "Состав флота и характеристики курьеров",
                      "Последняя телеметрия курьеров (убеждения, могут устаревать)"],
        "actions": ["Принять заказ", "Распределить заказ (механизм подключается)", "Отозвать назначение",
                    "Обнаружить потерю связи с курьером и переназначить заказы", "Уведомить клиента"],
        "authority": ["Назначать и отзывать заказы у курьеров", "Подтверждать доставку клиенту"],
        "means": ["Канал связи со всеми курьерами и клиентом", "Сторожевой таймер"],
    },
    "courier": {
        "title": "Курьер (автономный робот)",
        "goals": ["Выполнить принятые заказы", "Не разрядиться в пути", "Максимизировать свой доход (ЛР2–ЛР3)"],
        "knowledge": ["Своё положение, заряд, грузоподъёмность", "Очередь принятых заказов",
                      "Карта точек выдачи и станций"],
        "actions": ["Принять / отклонить заказ", "Перемещаться", "Забрать и доставить посылку",
                    "Запросить зарядку", "Сообщать телеметрию", "Вернуть заказ при невозможности исполнения"],
        "authority": ["Решать, принимать ли заказ", "Выбирать станцию и момент зарядки"],
        "means": ["Двигатель и батарея", "Грузовой отсек", "Канал связи с диспетчером и станциями"],
    },
    "station": {
        "title": "Зарядная станция",
        "goals": ["Обслужить курьеров без превышения числа мест", "Соблюдать лимит мощности сети (ЛР3)"],
        "knowledge": ["Занятые места и очередь", "Своё состояние (доступна / нет)"],
        "actions": ["Выделить место", "Поставить в очередь", "Отказать", "Заряжать"],
        "authority": ["Распоряжаться своими местами зарядки"],
        "means": ["Места зарядки (ограниченный ресурс)", "Канал связи с курьерами"],
    },
}

PROTOCOLS: dict[str, str] = {
    "order": "Публикация заказа клиентом и подтверждение приёма",
    "assign": "Назначение заказа курьеру (REQUEST -> AGREE/REFUSE), отзыв (CANCEL), возврат (FAILURE)",
    "order-status": "Уведомления о заборе и доставке",
    "telemetry": "Периодическая телеметрия курьера диспетчеру",
    "charge": "Запрос места зарядки, очередь, окончание зарядки",
}

# (роль отправителя, роль получателя, протокол) -> допустимые перформативы
PERMISSIONS: dict[tuple[str, str, str], set[str]] = {}


def allow(sender_role: str, receiver_role: str, protocol: str, perfs, description: str | None = None) -> None:
    PERMISSIONS.setdefault((sender_role, receiver_role, protocol), set()).update(perfs)
    if description and protocol not in PROTOCOLS:
        PROTOCOLS[protocol] = description


def is_allowed(sender_role: str, receiver_role: str, protocol: str, perf: str) -> bool:
    return perf in PERMISSIONS.get((sender_role, receiver_role, protocol), ())


allow("client", "dispatcher", "order", {"request"})
allow("dispatcher", "client", "order", {"agree", "refuse"})
allow("dispatcher", "client", "order-status", {"inform"})
allow("dispatcher", "courier", "assign", {"request", "cancel"})
allow("courier", "dispatcher", "assign", {"agree", "refuse", "failure"})
allow("courier", "dispatcher", "telemetry", {"inform"})
allow("courier", "dispatcher", "order-status", {"inform"})
allow("courier", "station", "charge", {"request", "inform", "cancel"})
allow("station", "courier", "charge", {"agree", "refuse", "inform"})
