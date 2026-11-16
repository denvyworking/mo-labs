"""Регистрация механизмов ЛР3 в ядре: режимы зарядки, новые роли и протоколы."""
from __future__ import annotations

from lab1.mas import registry, roles

ROLE_GRID = {
    "title": "Энергокоординатор сети (арбитр мощности)",
    "goals": ["Не допускать превышения общего лимита мощности сети",
              "Распределять мощность между станциями по критичности курьеров без голодания"],
    "knowledge": ["Лимит мощности сети", "Сколько токенов выдано каждой станции", "Очередь запросов с приоритетами"],
    "actions": ["Выдать токен мощности", "Поставить запрос в очередь", "Принять возврат токена",
                "Сообщить станциям их долю лимита и наличие дефицита"],
    "authority": ["Разрешать или откладывать начало зарядки на любой станции"],
    "means": ["Канал связи со станциями"],
}
ROLE_REPUTATION = {
    "title": "Реестр репутации (арбитр санкций)",
    "goals": ["Сделать невыгодными ложные заявки, неявку по брони и пересиживание квоты"],
    "knowledge": ["Репутация каждого курьера", "Наблюдения станций", "Заказы курьера по книге диспетчера"],
    "actions": ["Сверить заявку с измерением станции и книгой диспетчера", "Понизить / повысить репутацию",
                "Разослать станциям новую репутацию"],
    "authority": ["Назначать санкции (понижение доверия к заявкам, запрет брони)"],
    "means": ["Канал связи со станциями и диспетчером"],
}


def register() -> None:
    from .modes import demo_selfish, install_negotiation, install_priority, install_reputation

    roles.ROLES["grid"] = ROLE_GRID
    roles.ROLES["reputation"] = ROLE_REPUTATION

    roles.allow("courier", "station", "slot", {"cfp", "accept-proposal", "reject-proposal", "cancel"},
                "Переговоры о слоте зарядки: CFP -> PROPOSE/REFUSE -> ACCEPT/REJECT -> бронь (ЛР3)")
    roles.allow("station", "courier", "slot", {"propose", "refuse", "inform"})
    roles.allow("station", "grid", "power", {"request", "cancel", "inform"},
                "Токены мощности сети: запрос, выдача, возврат, доля лимита, сигнал дефицита (ЛР3)")
    roles.allow("grid", "station", "power", {"agree", "inform"})
    roles.allow("station", "reputation", "reputation", {"inform"},
                "Наблюдения станций и рассылка репутации курьеров (ЛР3)")
    roles.allow("reputation", "station", "reputation", {"inform"})
    roles.allow("reputation", "dispatcher", "audit", {"query"},
                "Сверка заявленной срочности с книгой заказов диспетчера (ЛР3)")
    roles.allow("dispatcher", "reputation", "audit", {"inform"})

    registry.register(
        "charging", "priority", "Приоритеты на станции + частичная зарядка", install_priority,
        "Очередь по критичности (заряд, срок заказа, старение); при очереди — зарядка до квоты", lab="lab3")
    registry.register(
        "charging", "negotiation", "Переговоры о слоте + арбитр мощности", install_negotiation,
        "CFP станциям, предложения с оценкой ожидания, бронь; зарядка только по токену энергокоординатора",
        lab="lab3")
    registry.register(
        "charging", "reputation", "Переговоры + репутация и санкции", install_reputation,
        "Как negotiation; станции сверяют заявки с измерениями и книгой диспетчера, реестр снижает "
        "доверие к лжецам, лишает брони за неявку, выселяет за пересиживание", lab="lab3")
    registry.register(
        "charging", "negotiation-selfish", "Переговоры, 30 % эгоистов (демо)", install_negotiation,
        "Пресет для демонстрации: negotiation с params.selfish_share=0.3", configure=demo_selfish, lab="lab3")
    registry.register(
        "charging", "reputation-selfish", "Репутация, 30 % эгоистов (демо)", install_reputation,
        "Пресет для демонстрации: reputation с params.selfish_share=0.3", configure=demo_selfish, lab="lab3")
