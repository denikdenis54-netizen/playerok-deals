"""Обработчики команд Telegram-бота."""

from __future__ import annotations

import html
import logging
from pathlib import Path

import aiohttp
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    User,
    WebAppInfo,
)

from .config import config

log = logging.getLogger(__name__)

router = Router()


def escape(text: str) -> str:
    """Экранирует текст для HTML-режима Telegram."""
    return html.escape(str(text), quote=False)

DESCRIPTION = (
    "💎 <b>PLAYEROK · маркетплейс игр</b>\n\n"
    "Маркетплейс игровых товаров · оплата под защитой сервиса.\n\n"
    "Жми «Открыть Playerok» под сообщением или кнопку слева от поля ввода."
)

MANAGER_NAME = config.manager_username or "PlayerokOfficialManager"
MANAGER_URL = f"https://t.me/{MANAGER_NAME}"

BUTTON_TEXT = "Открыть Playerok"

_WELCOME_TEMPLATE = (
    "💎 <b>PLAYEROK · маркетплейс игр</b>\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "💫 Привет, {name}! Сделки — прямо в Telegram.\n"
    "\n"
    "🛡 Гарант — деньги у сервиса, пока сделка не закрыта\n"
    "💳 Кошелёк — пополнение и вывод в пару тапов\n"
    "💬 Сделки и чат — во встроенном приложении\n"
    "⚡️ Автовыплата — сразу после подтверждения\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "👇 Жми «Открыть Playerok» — под сообщением или слева от поля ввода\n"
    "\n"
    '👤 Менеджер: <a href="{url}">@{name2}</a>'
)


def welcome_text(user: User | None = None) -> str:
    """Приветствие /start. Имя берётся из профиля Telegram."""
    raw = ""
    if user is not None:
        raw = (user.first_name or "").strip()
    return _WELCOME_TEMPLATE.format(
        name=escape(raw[:32]) if raw else "друг",
        url=MANAGER_URL,
        name2=escape(MANAGER_NAME),
    )

MEDIA_DIR = Path(__file__).resolve().parent / "media"
WELCOME_VIDEO = "welcome_video.mp4"
WELCOME_PHOTO = "welcome_photo.jpg"

SAFETY_TEXT = (
    "🛡 <b>Безопасность сделки</b>\n\n"
    "• Средства перечисляются на счёт Playerok и замораживаются в эскроу.\n"
    "• Продавец получает деньги только после твоего подтверждения получения.\n"
    "• Никогда не отправляй товар до появления сделки в этом приложении.\n"
    "• Все действия фиксируются в истории сделки."
)


def app_keyboard() -> InlineKeyboardMarkup:
    """Кнопка приложения. Никогда не бросает исключение.

    Раньше здесь был RuntimeError при пустом WEBAPP_URL, из-за чего
    /start падал и пользователь не получал ответа вообще.
    """
    url = config.webapp_url
    if url:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=BUTTON_TEXT, web_app=WebAppInfo(url=url)
                    )
                ]
            ]
        )

    log.warning("WEBAPP_URL не задан — отправляю ссылку на бота вместо Mini App")
    if config.bot_link:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Открыть бота", url=config.bot_link)]
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=[])


def manager_url() -> str | None:
    return config.manager_link


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject) -> None:
    # /start <КОД> — открытие сделки по ссылке из «Поделиться сделкой»
    payload = (command.args or "").strip().upper()
    if payload:
        reply = await deal_card_text(payload)
        if reply:
            await message.answer(
                reply, parse_mode="HTML", reply_markup=deal_keyboard(payload)
            )
            return

    welcome = welcome_text(message.from_user)
    if not await send_welcome_media(message, welcome):
        # медиа не отправилось — приветствие отдельным сообщением
        await message.answer(welcome, parse_mode="HTML", reply_markup=app_keyboard())


async def send_welcome_media(message: Message, welcome: str) -> bool:
    """Приветственная картинка с текстом в подписи и кнопкой приложения.

    Одно сообщение вместо двух: текст идёт caption, кнопка крепится к медиа.
    Сначала пробуем картинку, при неудаче — видео, затем текст отдельным
    сообщением.
    """
    photo = MEDIA_DIR / WELCOME_PHOTO
    if photo.exists():
        # Лимит подписи к фото в Bot API — 1024 символа
        if len(welcome) <= 1024:
            try:
                await message.answer_photo(
                    FSInputFile(photo),
                    caption=welcome,
                    parse_mode="HTML",
                    reply_markup=app_keyboard(),
                )
                return True
            except TelegramBadRequest as exc:
                log.error("Не удалось отправить приветственную картинку: %s", exc)
        else:
            await message.answer_photo(FSInputFile(photo), reply_markup=app_keyboard())
            await message.answer(welcome, parse_mode="HTML", reply_markup=app_keyboard())
            return True
    else:
        log.warning("Приветственная картинка не найдена: %s", photo)

    video = MEDIA_DIR / WELCOME_VIDEO
    if not video.exists():
        log.warning("Приветственное видео не найдено: %s", video)
        return False

    # Лимит подписи к видео в Bot API — 1024 символа
    if len(welcome) > 1024:
        await message.answer_video(FSInputFile(video), reply_markup=app_keyboard())
        await message.answer(welcome, parse_mode="HTML", reply_markup=app_keyboard())
        return True

    try:
        await message.answer_video(
            FSInputFile(video),
            caption=welcome,
            parse_mode="HTML",
            reply_markup=app_keyboard(),
        )
        return True
    except TelegramBadRequest as exc:
        # Чаще всего — файл больше лимита Bot API (50 МБ)
        log.error("Не удалось отправить видео: %s", exc)
        return False


def deal_keyboard(code: str) -> InlineKeyboardMarkup:
    """Кнопка, открывающая Mini App сразу на этой сделке."""
    if not config.webapp_url:
        return app_keyboard()
    url = f"{config.webapp_url.rstrip('/')}/?deal={code}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=BUTTON_TEXT, web_app=WebAppInfo(url=url))]
        ]
    )


async def deal_card_text(code: str) -> str | None:
    """Карточка сделки для перехода по ссылке.

    Коротко, как в исходном интерфейсе:
        Сделка Playerok #MV03P7QB

    Кнопку «Открыть Playerok» добавляет deal_keyboard — она ведёт
    сразу на эту сделку, поэтому подробности в тексте не нужны.
    """
    from . import db as D
    from .db import db as database

    row = await database.get_deal_by_code(code)
    if row is None:
        return None

    deal = D.row_to_deal(row)

    if config.webapp_url:
        # #КОД кликабельный: ведёт на сделку в приложении
        deep = f"{config.webapp_url.rstrip('/')}/?deal={deal['code']}"
        return f'Сделка Playerok #<a href="{deep}">{escape(deal["code"])}</a>'
    return f"Сделка Playerok #{escape(deal['code'])}"


@router.message(Command("app", "sdelki", "сделки"))
async def cmd_app(message: Message) -> None:
    await message.answer("Открывай приложение:", reply_markup=app_keyboard())


@router.message(Command("help", "помощь"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "📖 <b>Команды</b>\n\n"
        "/start — описание сервиса\n"
        "/app — открыть приложение\n"
        "/manager — написать менеджеру\n"
        "/safety — правила безопасной сделки\n"
        "/luxenaction — служебная команда",
        parse_mode="HTML",
    )


@router.message(Command("manager", "менеджер"))
async def cmd_manager(message: Message) -> None:
    url = manager_url()
    if not url:
        await message.answer("Менеджер пока не назначен. Аккаунт будет указан позже.")
        return
    await message.answer(f"💬 Написать менеджеру: {url}")


@router.message(Command("safety", "безопасность"))
async def cmd_safety(message: Message) -> None:
    await message.answer(SAFETY_TEXT, parse_mode="HTML")


@router.message(Command("luxenaction"))
async def cmd_luxenaction(message: Message, command: CommandObject) -> None:
    """Служебная команда. Логика будет добавлена по ТЗ владельца."""
    args = (command.args or "").strip()
    log.info("luxenaction от %s, args=%r", message.from_user.id, args)
    await message.answer(
        "Команда /luxenaction принята. Функционал в разработке — "
        "опиши, что она должна делать, и я добавлю."
    )


@router.message(F.text)
async def fallback(message: Message) -> None:
    await message.answer(
        "Открой приложение, чтобы создать сделку:", reply_markup=app_keyboard()
    )


async def check_webapp_url() -> None:
    """Проверяет, что адрес Mini App отдаёт приложение.

    Без проверки кнопка меню и ссылки на сделку выглядят рабочими,
    но ведут в заглушку туннеля или на мёртвый адрес.
    """
    url = config.webapp_url
    if not url:
        log.error("WEBAPP_URL пуст: кнопка меню и ссылки на сделку работать не будут")
        return
    if "example.com" in url:
        log.error("WEBAPP_URL — заглушка (%s). Нужен реальный HTTPS-адрес", url)
        return

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                status = resp.status
                body = await resp.text()
    except Exception as exc:  # noqa: BLE001
        log.error("WEBAPP_URL недоступен (%s): %s", url, exc)
        return

    if status != 200 or "Playerok Deals" not in body:
        log.error(
            "По адресу %s отдаётся не приложение (HTTP %s) — "
            "похоже на страницу-заглушку туннеля",
            url,
            status,
        )
    else:
        log.info("Mini App по адресу %s отдаётся корректно", url)


async def setup_commands(bot: Bot) -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Описание сервиса"),
            BotCommand(command="app", description="Открыть Playerok"),
            BotCommand(command="manager", description="Написать менеджеру"),
            BotCommand(command="safety", description="Безопасность сделки"),
            BotCommand(command="luxenaction", description="Служебная команда"),
        ]
    )
    if config.webapp_url:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text="Playerok Deals", web_app=WebAppInfo(url=config.webapp_url)
            )
        )