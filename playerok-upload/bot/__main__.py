"""Точка входа: Telegram-бот (aiogram, long polling) + веб-сервер Mini App."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from . import api, handlers
from .config import config
from .db import db
from .funpay import build_provider
from .services import Service

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("funpay-deal")


async def main() -> None:
    config.validate()
    api.DEV_MODE = os.getenv("DEV_MODE", "").strip() in ("1", "true", "yes")

    await db.connect()
    provider = build_provider(
        config.escrow_provider,
        **(
            {}
            if config.escrow_provider == "mock"
            else {
                "account_id": config.funpay_account_id,
                "api_key": config.funpay_api_key,
                "api_url": config.funpay_api_url,
            }
        ),
    )
    service = Service(db, provider)

    if not config.webapp_url:
        log.warning(
            "WEBAPP_URL пуст — Telegram не откроет Mini App. "
            "Укажи публичный HTTPS-адрес в .env (см. README)"
        )
    if not config.manager_link:
        log.warning("MANAGER_USERNAME пуст — кнопка «Написать менеджеру» пока заглушка")
    if config.escrow_provider != "mock":
        log.error(
            "ESCROW_PROVIDER=%s: реальная интеграция с FunPay не реализована "
            "(нет публичного API). Работай в ESCROW_PROVIDER=mock",
            config.escrow_provider,
        )

    app = api.create_app(db, service)
    runner = aiohttp.web.AppRunner(app, access_log=None)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "0.0.0.0", config.port)
    await site.start()
    log.info("Mini App: http://0.0.0.0:%s", config.port)

    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = Dispatcher()
    dispatcher.include_router(handlers.router)

    # Уведомления о ходе сделки приходят в чат бота. Пользователь должен
    # хотя бы раз нажать /start — иначе Telegram не разрешает писать ему.
    async def notify(user_id: int, text: str) -> None:
        await bot.send_message(user_id, text, parse_mode="HTML")

    service.set_notifier(notify)

    try:
        me = await bot.get_me()
        config.bot_username = me.username
        log.info("Бот: @%s (%s)", me.username, me.full_name)
        await handlers.setup_commands(bot)
        await handlers.check_webapp_url()
    except Exception as exc:  # noqa: BLE001
        log.error("Не удалось подключиться к Telegram API: %s", exc)
        await runner.cleanup()
        await service.shutdown()
        await db.close()
        await bot.session.close()
        sys.exit(1)

    try:
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        await service.shutdown()
        await runner.cleanup()
        await db.close()
        await bot.session.close()


def run() -> None:
    """Точка входа для консольной команды `funpay-deal`."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Остановлено пользователем")


if __name__ == "__main__":
    run()