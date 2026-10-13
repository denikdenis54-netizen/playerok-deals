"""HTTP-сервер Mini App: статика + JSON API.

Авторизация — по подписи Telegram initData (HMAC-SHA256 от BOT_TOKEN).
Никакие данные не принимаются без валидной подписи, кроме режима DEV_MODE=1,
который явно предназначен для локальной отладки интерфейса.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

from aiohttp import web

from . import db as D
from .config import config
from .currencies import CURRENCIES, PAY_METHODS, methods_for_currency
from .db import Database
from .services import DealError, Service

log = logging.getLogger(__name__)

WEBAPP_DIR = Path(__file__).resolve().parent / "webapp"
MAX_AUTH_AGE = 60 * 60 * 24  # сутки
DEV_MODE = False  # проставляется в __main__


class Unauthorized(Exception):
    pass


# --- Проверка initData ---------------------------------------------------
def _check_string(init_data: str) -> dict[str, str]:
    try:
        return dict(parse_qsl(init_data, keep_blank_values=True))
    except ValueError as exc:
        raise Unauthorized("Некорректные данные авторизации") from exc


def verify_init_data(init_data: str, token: str) -> dict[str, Any]:
    parsed = _check_string(init_data)
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        raise Unauthorized("Отсутствует подпись initData")

    data_check_string = "\n".join(f"{k}={parsed[k]}" for k in sorted(parsed))
    secret_key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    expected = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected, received_hash):
        raise Unauthorized("Подпись initData не совпадает")

    try:
        auth_date = int(parsed.get("auth_date", "0"))
    except ValueError:
        auth_date = 0
    if auth_date and time.time() - auth_date > MAX_AUTH_AGE:
        raise Unauthorized("Данные авторизации устарели, открой приложение заново")

    try:
        user = json.loads(parsed.get("user") or "{}")
    except json.JSONDecodeError:
        user = {}

    if not user.get("id"):
        raise Unauthorized("В данных авторизации нет пользователя")
    return user


# --- Приложение ----------------------------------------------------------
def create_app(database: Database, service: Service) -> web.Application:
    app = web.Application()
    app["db"] = database
    app["service"] = service

    @web.middleware
    async def no_store_static(request: web.Request, handler):
        """Не кэшировать Mini App: иначе правка JS/CSS не видна до перезагрузки."""
        response = await handler(request)
        if request.path.startswith("/static/") or request.path in ("/", "/index.html"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
            response.headers["Pragma"] = "no-cache"
        return response

    app.middlewares.append(no_store_static)

    @web.middleware
    async def middleware(request: web.Request, handler):
        try:
            return await handler(request)
        except Unauthorized as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=401)
        except DealError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        except Exception as exc:  # noqa: BLE001
            log.exception("Необработанная ошибка API")
            return web.json_response(
                {"ok": False, "error": f"Внутренняя ошибка: {exc}"}, status=500
            )

    app.middlewares.append(middleware)

    def user_id(request: web.Request) -> int:
        if DEV_MODE and not request.headers.get("Authorization"):
            return int(request.query.get("user_id", "1"))
        header = request.headers.get("Authorization", "")
        token = header[4:] if header.startswith("tma ") else ""
        if not token:
            token = request.query.get("initData", "")
        if not token:
            raise Unauthorized("Нет данных авторизации")
        user = verify_init_data(token, config.bot_token)
        return int(user["id"])

    async def register(request: web.Request) -> web.Response:
        uid = user_id(request)
        header = request.headers.get("Authorization", "")
        token = header[4:] if header.startswith("tma ") else ""
        username = first_name = None
        if token:
            try:
                user = verify_init_data(token, config.bot_token)
            except Unauthorized:
                user = {}
            username = user.get("username")
            first_name = user.get("first_name")
        await database.upsert_user(uid, username, first_name)
        return web.json_response({"ok": True, "user_id": uid})

    # --- Статика ---
    async def index(request: web.Request) -> web.StreamResponse:
        return web.FileResponse(WEBAPP_DIR / "index.html")

    async def favicon(request: web.Request) -> web.StreamResponse:
        # Браузер и Telegram запрашивают иконку сами. Отдаём пустой ответ,
        # иначе в логах сыплется 500 на каждой загрузке.
        return web.Response(status=204)

    async def health(request: web.Request) -> web.Response:
        return web.json_response(
            {
                "ok": True,
                "escrow_provider": config.escrow_provider,
                "demo": service.demo,
                "manager_link": config.manager_link,
            }
        )

    # --- Конфиг интерфейса ---
    async def get_config(request: web.Request) -> web.Response:
        total_deals = await database.count_deals()
        return web.json_response(
            {
                "ok": True,
                "currencies": [
                    {"code": code, **meta} for code, meta in CURRENCIES.items()
                ],
                "pay_methods": [
                    {"code": code, "title": meta["title"], "currencies": meta["currencies"]}
                    for code, meta in PAY_METHODS.items()
                ],
                "manager_link": config.manager_link,
                "bot_link": config.bot_link,
                "site": "Playerok.com",
                "demo": service.demo,
                # Рейтинг площадки. Отзывы внутри бота выключены, поэтому
                # оценки не накапливаются: это одна фиксированная цифра.
                # Сколько сделок она охватывает — считается по базе.
                "archived_rating": {
                    "rating": 4.5,
                    "deals": total_deals,
                    "label": "Общий рейтинг",
                },
            }
        )

    # --- Сделки ---
    async def deals_list(request: web.Request) -> web.Response:
        uid = user_id(request)
        scope = request.query.get("scope", "active")
        if scope == "active":
            rows = await database.list_deals(uid, statuses=D.ACTIVE_STATUSES)
        elif scope == "history":
            rows = await database.list_deals(uid, statuses=(D.COMPLETED, D.CANCELLED))
        else:
            rows = await database.list_deals(uid)
        deals = [service.decorate(D.row_to_deal(r)) for r in rows]
        return web.json_response({"ok": True, "deals": deals})

    def share_url(deal: dict) -> str:
        """Ссылка для «Поделиться сделкой».

        Используем ?start=, а не ?startapp=: ссылка всегда открывает чат
        с ботом и показывает карточку сделки, даже если Mini App
        недоступна или грузится долго. Само открытие сделки делает
        кнопка «Открыть Playerok» с адресом ?deal=<КОД>.
        """
        if not config.bot_link:
            return ""
        return f"{config.bot_link}?start={deal['code']}"

    async def deal_get(request: web.Request) -> web.Response:
        uid = user_id(request)
        deal_id = int(request.match_info["deal_id"])
        deal = await service.get_deal(deal_id, uid)
        balance = await database.get_balance(uid, deal["currency"])
        return web.json_response(
            {
                "ok": True,
                "deal": deal,
                "balance": balance,
                "share_url": share_url(deal),
            }
        )

    async def deal_by_code(request: web.Request) -> web.Response:
        """Открытие сделки по коду из deep link."""
        uid = user_id(request)
        code = request.match_info["code"]
        deal = await service.get_by_code(code, uid)
        balance = await database.get_balance(uid, deal["currency"])
        return web.json_response(
            {"ok": True, "deal": deal, "balance": balance, "share_url": share_url(deal)}
        )

    async def deal_join(request: web.Request) -> web.Response:
        uid = user_id(request)
        deal_id = int(request.match_info["deal_id"])
        body = await request.json()
        deal = await service.join(deal_id, uid, body.get("username"))
        return web.json_response(
            {"ok": True, "deal": deal, "share_url": share_url(deal)}
        )

    async def deal_create(request: web.Request) -> web.Response:
        uid = user_id(request)
        body = await request.json()
        currency = body.get("currency", "RUB")
        deal = await service.create_deal(
            user_id=uid,
            side=body.get("side", "buy"),
            title=body.get("title", ""),
            amount=float(body.get("amount") or 0),
            currency=currency,
            pay_method=body.get("pay_method", ""),
            requisites=body.get("requisites"),
        )
        return web.json_response(
            {"ok": True, "deal": deal, "share_url": share_url(deal)}
        )

    def _action(name: str):
        async def handler(request: web.Request) -> web.Response:
            uid = user_id(request)
            deal_id = int(request.match_info["deal_id"])
            deal = await getattr(service, name)(deal_id, uid)
            return web.json_response(
                {"ok": True, "deal": deal, "share_url": share_url(deal)}
            )

        return handler

    async def deal_review(request: web.Request) -> web.Response:
        # Отзывы внутри бота выключены: сделок в проекте нет, оценивать нечего.
        # Рейтинг площадки показывается отдельно и не набирается здесь.
        return web.json_response(
            {"ok": False, "error": "Отзывы отключены"}, status=403
        )

    # --- Кошельки / лидеры / отзывы ---
    async def wallets(request: web.Request) -> web.Response:
        uid = user_id(request)
        balances = await service.balances(uid)
        frozen = await database.balances_with_frozen(uid)
        return web.json_response(
            {
                "ok": True,
                "balances": balances,
                "frozen": {c: v["frozen"] for c, v in frozen.items()},
                "currencies": [{"code": c, **m} for c, m in CURRENCIES.items()],
                "methods": {
                    c: methods_for_currency(c) for c in CURRENCIES
                },
            }
        )

    async def wallet_topup(request: web.Request) -> web.Response:
        uid = user_id(request)
        body = await request.json()
        balances = await service.top_up(
            uid, body.get("currency", "RUB"), float(body.get("amount") or 0)
        )
        return web.json_response({"ok": True, "balances": balances})

    async def leaders(request: web.Request) -> web.Response:
        rows = await database.top_users(limit=20)
        items = [
            {
                "user_id": r["user_id"],
                "username": r["username"],
                "name": r["first_name"] or r["username"] or str(r["user_id"]),
                "deals": r["deals"],
                "volume": r["volume"],
            }
            for r in rows
        ]
        return web.json_response({"ok": True, "leaders": items})

    async def reviews(request: web.Request) -> web.Response:
        rows = await database.list_reviews(limit=30)
        items = [
            {
                "id": r["id"],
                "deal_id": r["deal_id"],
                "text": r["text"],
                "score": r["score"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]
        rating = await database.rating()
        return web.json_response({"ok": True, "reviews": items, "rating": rating})

    app.router.add_get("/", index)
    app.router.add_get("/index.html", index)
    app.router.add_get("/favicon.ico", favicon)
    app.router.add_get("/health", health)
    app.router.add_get("/api/config", get_config)
    app.router.add_post("/api/register", register)
    app.router.add_get("/api/deals", deals_list)
    app.router.add_post("/api/deals", deal_create)
    app.router.add_get("/api/deals/{deal_id}", deal_get)
    app.router.add_post("/api/deals/{deal_id}/pay", _action("pay"))
    app.router.add_post("/api/deals/{deal_id}/deliver", _action("deliver"))
    app.router.add_post("/api/deals/{deal_id}/confirm", _action("confirm"))
    app.router.add_post("/api/deals/{deal_id}/cancel", _action("cancel"))
    app.router.add_post("/api/deals/{deal_id}/close", _action("close"))
    app.router.add_post("/api/deals/{deal_id}/review", deal_review)
    app.router.add_get("/api/deals/code/{code}", deal_by_code)
    app.router.add_post("/api/deals/{deal_id}/join", deal_join)
    app.router.add_get("/api/wallets", wallets)
    app.router.add_post("/api/wallets/topup", wallet_topup)
    app.router.add_get("/api/leaders", leaders)
    app.router.add_get("/api/reviews", reviews)

    app.router.add_static("/static/", WEBAPP_DIR / "static", name="static")
    return app