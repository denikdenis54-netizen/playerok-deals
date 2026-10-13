"""Реальная интеграция с FunPay — НЕ РЕАЛИЗОВАНА.

Почему: у FunPay нет публичного документированного API для программного
управления сделками и эскроу. Есть только внутренние эндпоинты сайта,
которые не являются публичным контрактом: они могут измениться в любой
момент, и их использование против правил сервиса.

Чтобы не выдумывать несуществующие адреса эндпоинтов, этот файл
намеренно бросает ProviderNotImplemented. Бот запускается только
с ESCROW_PROVIDER=mock, пока менеджер FunPay не предоставит официальный
способ интеграции.

Что нужно для реализации (любой из вариантов):
  1. Официальный API / партнёрский доступ от FunPay.
  2. Ручной режим: бот только собирает заявку и показывает реквизиты,
     а выпуск средств подтверждает менеджер (ESCROW_PROVIDER=manual).
  3. Собственный посредник с собственным эскроу (ЮKassa, Тинькофф
     безопасные сделки и т.п.) — тогда провайдер пишется здесь.
"""

from __future__ import annotations

from .base import EscrowProvider, OrderInfo, ProviderNotImplemented


class LiveProvider(EscrowProvider):
    name = "live"

    _MESSAGE = (
        "Реальная интеграция с FunPay не настроена.\n\n"
        "У FunPay нет публичного API для управления сделками. "
        "Чтобы бот работал с настоящими сделками, нужен официальный доступ "
        "или собственный платёжный провайдер.\n\n"
        "Пока работает демо-режим: ESCROW_PROVIDER=mock в .env"
    )

    def __init__(self, account_id: str = "", api_key: str = "", api_url: str = "") -> None:
        self.account_id = account_id
        self.api_key = api_key
        self.api_url = api_url

    async def create_order(
        self, deal_id: int, amount: float, currency: str, title: str, pay_method: str
    ) -> OrderInfo:
        raise ProviderNotImplemented(self._MESSAGE)

    async def get_order(self, ref: str) -> OrderInfo:
        raise ProviderNotImplemented(self._MESSAGE)

    async def release(self, ref: str) -> OrderInfo:
        raise ProviderNotImplemented(self._MESSAGE)

    async def refund(self, ref: str) -> OrderInfo:
        raise ProviderNotImplemented(self._MESSAGE)