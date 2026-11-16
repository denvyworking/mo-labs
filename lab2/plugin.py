"""ЛР2: механизмы распределения задач. Регистрирует режимы категории allocation и новые протоколы.

Режимы: cnp (контрактная сеть), cnp-commit (с предварительными обязательствами), auction (последовательный аукцион SSI, цена Викри),
auction-par (параллельный одноразовый аукцион), delegation (распределённое делегирование).
Базовый режим central (централизованная диспетчеризация) есть в ядре.
"""
from __future__ import annotations

from lab1.mas import registry, roles


def register() -> None:
    from .auction import install_auction, install_auction_parallel
    from .cnp import install_cnp, install_cnp_commit
    from .delegation import install_delegation

    roles.allow("dispatcher", "courier", "cnp", {"cfp", "accept-proposal", "reject-proposal"},
                "Контрактная сеть: CFP -> PROPOSE/REFUSE -> ACCEPT/REJECT -> INFORM/FAILURE")
    roles.allow("courier", "dispatcher", "cnp", {"propose", "refuse", "inform", "failure"})
    roles.allow("dispatcher", "courier", "auction", {"cfp", "accept-proposal", "reject-proposal"},
                "Аукцион по пакету заказов: CFP(лоты) -> PROPOSE(ставки) -> ACCEPT(лот, цена) -> INFORM + новые ставки")
    roles.allow("courier", "dispatcher", "auction", {"propose", "refuse", "inform", "failure"})
    roles.allow("courier", "courier", "delegate", {"request", "agree", "refuse", "cancel"},
                "Одноранговое делегирование заказа: REQUEST -> AGREE/REFUSE, уведомление диспетчера INFORM")
    roles.allow("courier", "dispatcher", "delegate", {"inform"})

    registry.register(
        "allocation", "cnp", "Контрактная сеть (CNP)", install_cnp, lab="lab2",
        description="Диспетчер объявляет CFP по каждому заказу, курьеры отвечают ставкой (ожидаемое время "
                    "завершения с учётом очереди и заряда), лучший получает контракт и перепроверяет выполнимость")
    registry.register(
        "allocation", "cnp-commit", "Контрактная сеть с предварительными обязательствами", install_cnp_commit,
        lab="lab2",
        description="Как cnp, но каждое ещё не решённое предложение курьер учитывает в следующих ставках, "
                    "будто уже выиграл его (средство от одновременных CFP)")
    registry.register(
        "allocation", "auction", "Последовательный аукцион (SSI, Викри)", install_auction, lab="lab2",
        description="Периодические аукционы по пакету заказов: в каждом раунде один лот уходит наименьшей "
                    "маргинальной ставке, победитель пересчитывает ставки; плата — по второй цене")
    registry.register(
        "allocation", "auction-par", "Параллельный одноразовый аукцион", install_auction_parallel, lab="lab2",
        description="Один раунд по пакету заказов: каждый лот — наименьшей ставке, ставки по лотам независимы")
    registry.register(
        "allocation", "delegation", "Распределённое делегирование", install_delegation, lab="lab2",
        description="Диспетчер отдаёт заказ ближайшему курьеру, перегруженный курьер сам передаёт заказ соседу "
                    "(REQUEST/AGREE, не более 2 передач)")
