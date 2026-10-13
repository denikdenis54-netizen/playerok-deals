"""Абстракция escrow-провайдера FunPay.

Бот работает с эскроу через этот интерфейс, поэтому замена симуляции
на реального провайдера не затрагивает ни логику сделок, ни интерфейс.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass


class EscrowError(Exception):
    """Ошибка операции эскроу."""


class ProviderNotImplemented(EscrowError):
    """Реальная интеграция не настроена."""


@dataclass
class OrderInfo:
    ref: str              # идентификатор заказа у провайдера
    status: str           # pending | held | released | refunded
    amount: float
    currency: str
    title: str = ""
    pay_method: str = ""


class EscrowProvider(abc.ABC):
    name: str = "base"

    @abc.abstractmethod
    async def create_order(
        self, deal_id: int, amount: float, currency: str, title: str, pay_method: str
    ) -> OrderInfo:
        """Резервирует средства в эскроу под сделку."""

    @abc.abstractmethod
    async def get_order(self, ref: str) -> OrderInfo:
        """Актуальное состояние заказа."""

    @abc.abstractmethod
    async def release(self, ref: str) -> OrderInfo:
        """Выпускает средства продавцу (сделка успешна)."""

    @abc.abstractmethod
    async def refund(self, ref: str) -> OrderInfo:
        """Возвращает средства покупателю (сделка отменена)."""