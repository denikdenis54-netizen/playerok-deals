"""Бизнес-логика сделок: переходы статусов, стороны, демо-симуляция."""

from __future__ import annotations

import asyncio
import html
import logging
import os
import random
import secrets
import time
from typing import Any, Awaitable, Callable, Optional

from . import db as D
from .currencies import (
    CURRENCIES,
    NEEDS_REQUISITES,
    PAY_METHODS,
    is_valid_method,
    methods_for_currency,
)
from aiogram.exceptions import TelegramBadRequest

from .config import config
from .db import Database, row_to_deal
from .funpay.base import EscrowProvider, OrderInfo, ProviderNotImplemented
from .funpay.mock import MockProvider

log = logging.getLogger(__name__)


def _esc(text: Any) -> str:
    """Экранирование для HTML-режима Telegram."""
    return html.escape(str(text), quote=False)

MAX_AMOUNT = 100_000_000
MAX_TITLE = 512

DEMO_COUNTERPARTIES = [
    "demo_buyer",
    "demo_seller",
    "demo_user",
]

DEMO_STEP_DELAY = 45.0  # секунд между демо-шагами второй стороны
# По умолчанию выключено: вторая сторона приходит по ссылке, а не выдумывается.
# Включается переменной DEMO_AUTOJOIN=1 — только для показа интерфейса без второго человека.
DEMO_AUTOJOIN = os.getenv("DEMO_AUTOJOIN", "0").strip() in ("1", "true", "yes")
DEMO_TOPUP = 0.0  # стартовый баланс не начисляется: кошелёк пополняет пользователь


class DealError(Exception):
    """Ошибка бизнес-логики, показывается пользователю."""


class Service:
    def __init__(
        self,
        database: Database,
        provider: EscrowProvider,
        notify: Optional[Callable[[int, str], Awaitable[None]]] = None,
    ) -> None:
        self.db = database
        self.provider = provider
        self.demo = isinstance(provider, MockProvider) and provider._demo
        self._tasks: set[asyncio.Task] = set()
        # Кто отправляет сообщения в Telegram. Ставится через set_notifier()
        # из __main__, чтобы сервис не зависел от aiogram.
        self._notify: Optional[Callable[[int, str], Awaitable[None]]] = None

    def set_notifier(
        self, notify: Optional[Callable[[int, str], Awaitable[None]]]
    ) -> None:
        self._notify = notify

    async def notify_user(self, user_id: int | None, text: str) -> None:
        """Отправляет сообщение в Telegram. Молчит, если отправка не вышла.

        Пользователь мог ни разу не нажать /start — тогда Telegram не даст
        написать ему первым, и это нормально, а не ошибка.
        """
        if self._notify is None or not user_id:
            return
        try:
            await self._notify(user_id, text)
        except TelegramBadRequest as exc:
            # Telegram не даёт писать первым тому, кто не запускал бота.
            # Это не сбой: сообщение просто дойдёт после /start.
            log.warning(
                "Уведомление не доставлено пользователю %s: %s", user_id, exc
            )
        except Exception:  # noqa: BLE001 — уведомление не должно ломать сделку
            log.warning("Не удалось отправить уведомление пользователю %s", user_id)

    # --- Роли сторон -------------------------------------------------------
    @staticmethod
    def user_role(deal: D.aiosqlite.Row | dict) -> str:
        """Роль создателя сделки: buyer или seller."""
        side = deal["side"]
        return "buyer" if side == "buy" else "seller"

    @staticmethod
    def counterparty_role(deal: D.aiosqlite.Row | dict) -> str:
        return "seller" if Service.user_role(deal) == "buyer" else "buyer"

    @staticmethod
    def role_title(role: str) -> str:
        return "Продавец" if role == "seller" else "Покупатель"

    # --- Валидация ---------------------------------------------------------
    @staticmethod
    def validate_create(
        side: str, title: str, amount: float, currency: str, pay_method: str
    ) -> tuple[str, str]:
        if side not in ("buy", "sell"):
            raise DealError("Выбери тип сделки: купить или продать")

        title = (title or "").strip()
        if len(title) < 3:
            raise DealError("Опиши товар чуть подробнее — минимум 3 символа")
        if len(title) > MAX_TITLE:
            raise DealError(f"Описание слишком длинное, максимум {MAX_TITLE} символов")

        if currency not in CURRENCIES:
            raise DealError("Неизвестная валюта")
        if amount is None or amount <= 0:
            raise DealError("Укажи сумму больше нуля")
        if amount > MAX_AMOUNT:
            raise DealError("Сумма слишком большая")

        if not is_valid_method(pay_method, currency):
            allowed = ", ".join(m["title"] for m in methods_for_currency(currency))
            raise DealError(f"Этот способ не подходит. Доступно: {allowed}")

        return title, pay_method

    # --- Код сделки -------------------------------------------------------
    CODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

    async def generate_code(self) -> str:
        """Короткий публичный код вида MV0211K5 (без неоднозначных символов)."""
        for _ in range(12):
            code = "".join(secrets.choice(self.CODE_ALPHABET) for _ in range(8))
            if await self.db.get_deal_by_code(code) is None:
                return code
        return f"{int(time.time()) % 10**8:08d}"

    # --- Создание ----------------------------------------------------------
    async def create_deal(
        self,
        user_id: int,
        side: str,
        title: str,
        amount: float,
        currency: str,
        pay_method: str,
        requisites: str | None = None,
    ) -> dict[str, Any]:
        title, pay_method = self.validate_create(side, title, amount, currency, pay_method)

        if pay_method in NEEDS_REQUISITES:
            requisites = (requisites or "").strip()
            if len(requisites) < 4:
                raise DealError(
                    f"Для способа «{PAY_METHODS[pay_method]['title']}» нужны реквизиты — "
                    "укажи их перед созданием сделки"
                )

        try:
            order = await self.provider.create_order(
                deal_id=0, amount=amount, currency=currency, title=title, pay_method=pay_method
            )
        except ProviderNotImplemented as exc:
            raise DealError(str(exc)) from exc

        deal_id = await self.db.create_deal(
            user_id=user_id,
            code=await self.generate_code(),
            side=side,
            title=title,
            amount=float(amount),
            currency=currency,
            pay_method=pay_method,
            status=D.CREATED,
            requisites=requisites or None,
            provider_ref=order.ref,
        )

        if self.demo and DEMO_AUTOJOIN:
            self._spawn(self._demo_join(deal_id))
        return await self.get_deal(deal_id, user_id)

    # --- Получение ---------------------------------------------------------
    async def get_deal(self, deal_id: int, user_id: int | None = None) -> dict[str, Any]:
        row = await self.db.get_deal(deal_id)
        if row is None:
            raise DealError("Сделка не найдена")
        if user_id is not None and not self.is_participant(row, user_id):
            raise DealError("Нет доступа к этой сделке")
        is_owner = row["user_id"] == user_id
        is_part = self.is_participant(row, user_id) if user_id is not None else False
        deal = self.decorate(
            row_to_deal(row), viewer_is_owner=is_owner if is_part else None
        )
        deal["is_owner"] = is_owner
        deal["is_participant"] = is_part
        deal.update(await self._parties(row))
        return deal

    async def _parties(self, row) -> dict[str, Any]:
        """Роли и имена сторон с точки зрения создателя сделки.

        Карточка описывает саму сделку (кто продавец, кто покупатель),
        поэтому подписи не зависят от того, кто смотрит экран.
        """
        owner = await self.db.get_user(row["user_id"])
        # Ник не у всех: у Telegram-аккаунтов бывает, но не обязательно
        owner_name = None
        if owner:
            owner_name = owner["username"] or owner["first_name"] or None
        owner_role = self.user_role(row)
        cp = row["counterparty"]
        return {
            "owner_role": owner_role,
            "owner_name": owner_name,
            "seller_name": owner_name if owner_role == "seller" else cp,
            "buyer_name": cp if owner_role == "seller" else owner_name,
        }

    def decorate(
        self, deal: dict[str, Any], viewer_is_owner: bool | None = None
    ) -> dict[str, Any]:
        """Добавляет производные поля для интерфейса.

        viewer_is_owner: True — смотрит создатель, False — вторая сторона,
        None — посторонний (только карточка и приглашение).
        Роли всегда считаются от точки зрения смотрящего: иначе покупатель,
        пришедший по ссылке, увидел бы экран продавца без кнопки оплаты.
        """
        owner_role = self.user_role(deal)
        owner_cp = self.counterparty_role(deal)
        # Роль, которую получит новый участник
        deal["invite_role"] = self.role_title(owner_cp)

        if viewer_is_owner is None:
            role, cp = owner_role, owner_cp
        elif viewer_is_owner:
            role, cp = owner_role, owner_cp
        else:
            role, cp = owner_cp, owner_role

        deal["user_role"] = role
        deal["counterparty_role"] = cp
        deal["pay_method_title"] = PAY_METHODS.get(deal["pay_method"], {}).get("title", deal["pay_method"])
        deal["currency_title"] = CURRENCIES.get(deal["currency"], {}).get("title", deal["currency"])
        deal.update(self.progress(deal["status"], role))
        return deal

    def progress(self, status: str, role: str) -> dict[str, Any]:
        """Заголовок «СЕЙЧАС», подпись статуса и доступные действия."""
        cp_role = "seller" if role == "buyer" else "buyer"
        cp_name = self.role_title(cp_role)

        if status == D.CREATED or status == D.AWAITING_COUNTERPARTY:
            headline = f"{cp_name} ещё не присоединился"
            detail = "Как только вторая сторона присоединится, сделка перейдёт к оплате."
            actions = []
        elif status == D.COUNTERPARTY_JOINED:
            headline = f"{cp_name} в сделке"
            if role == "buyer":
                detail = "Оплата в эскроу Playerok. Средства заморозятся до передачи подарка."
                actions = ["pay"]
            else:
                detail = "Покупатель оплачивает сделку. Ожидай оплату в эскроу."
                actions = []
        elif status == D.PAID:
            headline = "Оплата в эскроу Playerok"
            if role == "seller":
                detail = "Средства заморожены. Передай подарок и подтверди отправку."
                actions = ["deliver"]
            else:
                detail = "Продавец передаёт подарок. Средства заморожены в эскроу."
                actions = []
        elif status == D.DELIVERED:
            headline = "Подарок передан"
            if role == "buyer":
                detail = "Продавец передал подарок. Подтверди получение, чтобы завершить сделку."
                actions = ["confirm"]
            else:
                # Завершает сделку только покупатель: продавец не может
                # закрыть её сам и получить деньги без подтверждения.
                detail = (
                    "Подарок передан. Сделку завершит покупатель — "
                    "подожди подтверждения получения."
                )
                actions = []
        elif status == D.COMPLETED:
            headline = "Сделка завершена"
            detail = "Средства переведены продавцу. Спасибо за сделку!"
            # Отзывы внутри бота выключены — оценивать нечего.
            actions = []
        else:
            headline = "Сделка отменена"
            detail = "Средства возвращены покупателю."
            actions = []

        return {"headline": headline, "detail": detail, "actions": actions}

    # --- Действия ----------------------------------------------------------
    async def _load(self, deal_id: int, user_id: int) -> D.aiosqlite.Row:
        row = await self.db.get_deal(deal_id)
        if row is None:
            raise DealError("Сделка не найдена")
        if not self.is_participant(row, user_id):
            raise DealError("Нет доступа к этой сделке")
        return row

    @staticmethod
    def is_participant(row, user_id: int) -> bool:
        """Доступ есть у создателя и у второй стороны."""
        return user_id in (row["user_id"], row["counterparty_id"])

    def role_for(self, row, user_id: int) -> str:
        """Роль участника в сделке с точки зрения этого пользователя.

        Создатель — своя роль, вторая сторона — противоположная.
        """
        owner_role = self.user_role(row)
        if row["user_id"] == user_id:
            return owner_role
        return self.counterparty_role(row)

    def buyer_id(self, row) -> int | None:
        """Telegram ID покупателя — кому возвращать деньги при отмене."""
        if self.user_role(row) == "buyer":
            return row["user_id"]
        return row["counterparty_id"]

    def seller_id(self, row) -> int | None:
        """Telegram ID продавца.

        Роль создателя зависит от side: у «buy» он покупатель, у «sell» —
        продавец. Поэтому брать counterparty_id напрямую нельзя: когда
        сделку создаёт продавец, обе стороны указались бы на второго
        участника и уведомления ушли бы одному человеку дважды.
        """
        if self.user_role(row) == "seller":
            return row["user_id"]
        return row["counterparty_id"]

    async def get_by_code(self, code: str, user_id: int) -> dict[str, Any]:
        """Сделка по коду из ссылки — для перехода второй стороны."""
        row = await self.db.get_deal_by_code((code or "").strip().upper())
        if row is None:
            raise DealError("Сделка не найдена. Проверь ссылку.")
        is_owner = row["user_id"] == user_id
        is_part = self.is_participant(row, user_id)
        deal = self.decorate(row_to_deal(row), viewer_is_owner=is_owner if is_part else None)
        deal["is_owner"] = is_owner
        deal["is_participant"] = is_part
        deal.update(await self._parties(row))
        return deal

    async def join(self, deal_id: int, user_id: int, username: str | None) -> dict[str, Any]:
        """Вторая сторона присоединяется по ссылке."""
        row = await self.db.get_deal(deal_id)
        if row is None:
            raise DealError("Сделка не найдена")
        if row["user_id"] == user_id:
            raise DealError("Ты создатель этой сделки")
        if row["counterparty_id"] is not None and row["counterparty_id"] != user_id:
            raise DealError("В эту сделку уже вошли")
        if row["status"] not in (D.CREATED, D.AWAITING_COUNTERPARTY):
            raise DealError("Присоединиться уже нельзя")

        name = (username or "").lstrip("@")[:32] or f"user{user_id}"
        await self.db.set_counterparty(deal_id, name, user_id)
        await self.db.set_status(deal_id, D.COUNTERPARTY_JOINED)
        return await self.get_deal(deal_id, user_id)

    async def pay(self, deal_id: int, user_id: int) -> dict[str, Any]:
        row = await self._load(deal_id, user_id)
        if row["status"] != D.COUNTERPARTY_JOINED:
            raise DealError("Сейчас сделка не ждёт оплаты")
        if self.role_for(row, user_id) != "buyer":
            raise DealError("Оплату вносит покупатель")

        amount = float(row["amount"])
        currency = row["currency"]
        try:
            await self.db.withdraw(user_id, currency, amount)
        except ValueError as exc:
            raise DealError(f"{exc}. Пополни кошелёк на вкладке «Кошельки»") from exc

        ref = row["provider_ref"]
        await self._provider_hold(ref)
        await self.db.set_status(deal_id, D.PAID)

        # Деньги сразу видны у продавца, но помечены замороженными:
        # потратить их можно только после подтверждения получения.
        seller = self.seller_id(row)
        if seller is not None and seller != user_id:
            await self.db.credit_frozen(seller, currency, amount)

        await self._notify_paid(row)

        if self.demo:
            self._spawn(self._demo_deliver(deal_id))
        return await self.get_deal(deal_id, user_id)

    async def _notify_paid(self, row) -> None:
        """Уведомления обеим сторонам сразу после оплаты.

        Формулировки описывают, что происходит с деньгами на самом деле:
        сумма списана с кошелька покупателя и держится в заморозке до
        подтверждения получения. На баланс продавца она попадёт только
        после подтверждения — поэтому «зачислено» здесь и не написано.
        """
        buyer_id = self.buyer_id(row)
        seller_id = self.seller_id(row)
        if buyer_id is None or seller_id is None:
            return
        if buyer_id == seller_id:
            log.warning(
                "Сделка %s: стороны совпали (id %s), уведомление о оплате "
                "не отправлено", row["code"], buyer_id,
            )
            return

        parties = await self._parties(row)
        amount = f"{float(row['amount']):.2f} {row['currency']}"
        code = _esc(row["code"])
        seller_label = (
            f"@{parties['seller_name']}" if parties["seller_name"] else "Продавец"
        )
        buyer_label = (
            f"@{parties['buyer_name']}" if parties["buyer_name"] else "Покупатель"
        )

        # Подарок продавец передаёт менеджеру, а не покупателю напрямую.
        manager = config.manager_username
        manager_part = (
            f'<a href="https://t.me/{_esc(manager)}">@{_esc(manager)}</a>'
            if manager
            else "менеджеру"
        )

        await self.notify_user(
            seller_id,
            "💰 <b>Оплата получена</b>\n"
            f"Сделка <code>{code}</code>\n\n"
            f"{buyer_label} оплатил <b>{amount}</b>\n\n"
            "Сумма заморожена до окончания сделки. Зачисление на твой кошелёк "
            "произойдёт после подтверждения получения от покупателя.\n\n"
            f"Передай подарок менеджеру {manager_part} и отметь отправку.",
        )
        await self.notify_user(
            buyer_id,
            "✅ <b>Оплата внесена</b>\n"
            f"Сделка <code>{code}</code>\n\n"
            f"<b>{amount}</b> заморожено до окончания сделки.\n\n"
            f"Продавец — {seller_label}. Передаст подарок через менеджера. "
            "Менеджер передаст его тебе — после этого подтверди получение, "
            "тогда сумма уйдёт продавцу. Не подтверждай, пока товар не пришёл.",
        )

    async def deliver(self, deal_id: int, user_id: int) -> dict[str, Any]:
        row = await self._load(deal_id, user_id)
        if row["status"] != D.PAID:
            raise DealError("Сейчас сделка не ждёт передачу подарка")
        if self.role_for(row, user_id) != "seller":
            raise DealError("Передачу подарка подтверждает продавец")

        await self.db.set_status(deal_id, D.DELIVERED)
        if self.demo:
            self._spawn(self._demo_receive(deal_id))
        return await self.get_deal(deal_id, user_id)

    async def confirm(self, deal_id: int, user_id: int) -> dict[str, Any]:
        row = await self._load(deal_id, user_id)
        if row["status"] != D.DELIVERED:
            raise DealError("Сейчас сделка не ждёт подтверждение получения")
        if self.role_for(row, user_id) != "buyer":
            raise DealError("Получение подтверждает покупатель")

        ref = row["provider_ref"]
        if ref:
            await self._provider_release(ref)
        # Покупатель подтвердил получение — заморозка снимается,
        # сумма остаётся в кошельке продавца доступной.
        seller = self.seller_id(row)
        if seller is not None:
            await self.db.unfreeze(seller, row["currency"], float(row["amount"]))
        await self.db.set_status(deal_id, D.COMPLETED)
        return await self.get_deal(deal_id, user_id)

    async def close(self, deal_id: int, user_id: int) -> dict[str, Any]:
        """Завершение сделки. Разрешено только покупателю.

        Продавец закрыть сделку сам не может: иначе он получил бы деньги
        без подтверждения, что товар действительно дошёл.
        """
        row = await self._load(deal_id, user_id)
        if row["status"] != D.DELIVERED:
            raise DealError("Завершить сделку можно только после передачи подарка")
        if self.role_for(row, user_id) != "buyer":
            raise DealError("Завершить сделку может только покупатель")

        if row["provider_ref"]:
            await self._provider_release(row["provider_ref"])
        await self.db.set_status(deal_id, D.COMPLETED)
        return await self.get_deal(deal_id, user_id)

    async def cancel(self, deal_id: int, user_id: int) -> dict[str, Any]:
        row = await self._load(deal_id, user_id)
        if row["status"] in (D.COMPLETED, D.CANCELLED):
            raise DealError("Сделку уже нельзя отменить")
        amount = float(row["amount"])
        if row["status"] in (D.PAID, D.DELIVERED):
            # Возврат покупателю и снятие зачисления у продавца:
            # сумма была заморожена в его кошельке, при отмене её убираем.
            buyer = self.buyer_id(row)
            seller = self.seller_id(row)
            if buyer is not None:
                await self.db.top_up(buyer, row["currency"], amount)
            if seller is not None and seller != buyer:
                await self.db.drop_frozen(seller, row["currency"], amount)
            if row["provider_ref"]:
                await self._provider_refund(row["provider_ref"])
        await self.db.set_status(deal_id, D.CANCELLED)
        return await self.get_deal(deal_id, user_id)

    async def add_review(self, deal_id: int, user_id: int, text: str, score: int = 5) -> None:
        text = (text or "").strip()
        if len(text) < 3:
            raise DealError("Напиши пару слов об отзыве")
        row = await self._load(deal_id, user_id)
        if row["status"] != D.COMPLETED:
            raise DealError("Отзыв можно оставить только после завершения сделки")
        await self.db.add_review(deal_id, user_id, text[:1000], score)

    # --- Вторая сторона (в реальном режиме — по сигналу от другого бота) --
    async def counterparty_join(self, deal_id: int, username: str) -> dict[str, Any]:
        row = await self.db.get_deal(deal_id)
        if row is None:
            raise DealError("Сделка не найдена")
        if row["status"] not in (D.CREATED, D.AWAITING_COUNTERPARTY):
            return self.decorate(row_to_deal(row))
        await self.db.set_counterparty(deal_id, username)
        await self.db.set_status(deal_id, D.COUNTERPARTY_JOINED)
        return self.decorate(row_to_deal(await self.db.get_deal(deal_id)))

    # --- Провайдер ---------------------------------------------------------
    async def _provider_hold(self, ref: str) -> None:
        if isinstance(self.provider, MockProvider):
            await self.provider.hold(ref)

    async def _provider_release(self, ref: str) -> None:
        try:
            await self.provider.release(ref)
        except ProviderNotImplemented as exc:
            log.warning("release(%s): %s", ref, exc)

    async def _provider_refund(self, ref: str) -> None:
        try:
            await self.provider.refund(ref)
        except ProviderNotImplemented as exc:
            log.warning("refund(%s): %s", ref, exc)

    # --- Кошельки ----------------------------------------------------------
    async def balances(self, user_id: int) -> dict[str, float]:
        balances = await self.db.list_balances(user_id)
        return {code: balances.get(code, 0.0) for code in CURRENCIES}

    async def top_up(self, user_id: int, currency: str, amount: float) -> dict[str, float]:
        if currency not in CURRENCIES:
            raise DealError("Неизвестная валюта")
        if amount <= 0:
            raise DealError("Укажи сумму больше нуля")
        if amount > MAX_AMOUNT:
            raise DealError("Сумма слишком большая")
        await self.db.top_up(user_id, currency, float(amount))
        return await self.balances(user_id)

    # --- Демо-шаги второй стороны -----------------------------------------
    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _demo_join(self, deal_id: int) -> None:
        await asyncio.sleep(DEMO_STEP_DELAY)
        row = await self.db.get_deal(deal_id)
        if row is None or row["status"] != D.CREATED:
            return
        await self.counterparty_join(deal_id, random.choice(DEMO_COUNTERPARTIES))

    async def _demo_deliver(self, deal_id: int) -> None:
        await asyncio.sleep(DEMO_STEP_DELAY)
        row = await self.db.get_deal(deal_id)
        if row is None or row["status"] != D.PAID:
            return
        if self.user_role(row) == "buyer":
            await self.db.set_status(deal_id, D.DELIVERED)

    async def _demo_receive(self, deal_id: int) -> None:
        await asyncio.sleep(DEMO_STEP_DELAY)
        row = await self.db.get_deal(deal_id)
        if row is None or row["status"] != D.DELIVERED:
            return
        ref = row["provider_ref"]
        if ref:
            await self._provider_release(ref)
        await self.db.set_status(deal_id, D.COMPLETED)

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()