"""Симуляция эскроу для локальной разработки и демонстрации.

Никаких реальных платежей: все средства существуют только в памяти
процесса. Вторая сторона сделки имитируется автоматически (по флажку
demo), чтобы полный цикл можно было прокликать без живого контрагента.
"""

from __future__ import annotations

import asyncio
import time
from typing import Callable

from .base import EscrowError, EscrowProvider, OrderInfo

PENDING = "pending"
HELD = "held"
RELEASED = "released"
REFUNDED = "refunded"


class MockProvider(EscrowProvider):
    name = "mock"

    def __init__(self, demo: bool = True, step_delay: float = 1.5) -> None:
        self._orders: dict[str, OrderInfo] = {}
        self._demo = demo
        self._step_delay = step_delay
        self._seq = 0
        # callback(deal_ref) — вызывается при смене статуса заказа
        self.on_change: Callable[[str], None] | None = None

    def _new_ref(self) -> str:
        self._seq += 1
        return f"MOCK-{int(time.time())}-{self._seq}"

    def _get(self, ref: str) -> OrderInfo:
        order = self._orders.get(ref)
        if order is None:
            raise EscrowError(f"Заказ {ref} не найден у провайдера")
        return order

    async def create_order(
        self, deal_id: int, amount: float, currency: str, title: str, pay_method: str
    ) -> OrderInfo:
        ref = self._new_ref()
        order = OrderInfo(
            ref=ref,
            status=PENDING,
            amount=amount,
            currency=currency,
            title=title,
            pay_method=pay_method,
        )
        self._orders[ref] = order
        return order

    async def get_order(self, ref: str) -> OrderInfo:
        return self._get(ref)

    async def hold(self, ref: str) -> OrderInfo:
        """Заказчик перечислил деньги — средства заморожены в эскроу."""
        order = self._get(ref)
        order.status = HELD
        if self.on_change:
            self.on_change(ref)
        return order

    async def release(self, ref: str) -> OrderInfo:
        order = self._get(ref)
        order.status = RELEASED
        if self.on_change:
            self.on_change(ref)
        return order

    async def refund(self, ref: str) -> OrderInfo:
        order = self._get(ref)
        order.status = REFUNDED
        if self.on_change:
            self.on_change(ref)
        return order

    # --- Имитация действий второй стороны в демо-режиме ---------------------
    async def simulate(self, action: str, ref: str) -> None:
        """Прогоняет шаг контрагента с задержкой (для демо-навигации)."""
        if not self._demo:
            return
        await asyncio.sleep(self._step_delay)
        if action == "counterparty_joins":
            return
        if action == "counterparty_pays":
            await self.hold(ref)
        elif action == "counterparty_delivers":
            pass
        elif action == "counterparty_receives":
            await self.release(ref)