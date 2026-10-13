"""Сквозная проверка API Mini App без Telegram.

Запуск:  python -m tests.smoke
Работает в DEV_MODE: подпись initData не проверяется.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import api, db as D  # noqa: E402
from bot.funpay import build_provider  # noqa: E402
from bot.services import Service  # noqa: E402

FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    print(("  ok  " if condition else FAIL) + f"  {label}")
    if not condition:
        FAILURES.append(label)


FAIL = " FAIL"


async def main() -> int:
    tmp = Path(tempfile.mkdtemp()) / "smoke.db"
    database = D.Database(str(tmp))
    await database.connect()
    service = Service(database, build_provider("mock"))

    api.DEV_MODE = True
    app = api.create_app(database, service)

    async with TestClient(TestServer(app)) as client:
        async def get(path: str):
            return await client.get(path)

        async def post(path: str, body: dict | None = None):
            return await client.post(path, json=body or {})

        print("\n[1] Конфигурация интерфейса")
        r = await get("/api/config")
        data = await r.json()
        check(r.status == 200, "GET /api/config отвечает 200")
        codes = [c["code"] for c in data["currencies"]]
        check("KZT" in codes, "валюта KZT (тенге) доступна")
        check("BYN" in codes, "валюта BYN (белорусский рубль) доступна")
        check(
            data["manager_link"] is not None
            and data["manager_link"].endswith("PlayerokOfficialManager"),
            "менеджер доступен по ссылке",
        )

        kzt_methods = [
            m["code"] for m in data["pay_methods"]
            if "KZT" in m["currencies"] or "__any__" in m["currencies"]
        ]
        check("kaspi" in kzt_methods, "для KZT доступен Kaspi.kz")
        byn_methods = [
            m["code"] for m in data["pay_methods"]
            if "BYN" in m["currencies"] or "__any__" in m["currencies"]
        ]
        check("card_by" in byn_methods, "для BYN доступна карта Беларуси")

        print("\n[2] Регистрация пользователя")
        r = await post("/api/register")
        check((await r.json())["user_id"] == 1, "пользователь зарегистрирован")

        print("\n[3] Валидация сделки")
        r = await post("/api/deals", {
            "side": "buy", "title": "ab", "amount": 10,
            "currency": "RUB", "pay_method": "card_ru",
        })
        check(r.status == 400, "слишком короткое описание отклонено")

        r = await post("/api/deals", {
            "side": "buy", "title": "Подарок", "amount": 100,
            "currency": "KZT", "pay_method": "card_ru",
        })
        check(r.status == 400, "способ оплаты чужой валюты отклонён")

        r = await post("/api/deals", {
            "side": "buy", "title": "Подарок", "amount": 100,
            "currency": "RUB", "pay_method": "card_ru",
        })
        check(r.status == 400, "отсутствие реквизитов для карты РФ отклонено")

        print("\n[4] Сделка в тенге (покупка)")
        r = await post("/api/deals", {
            "side": "buy", "title": "https://t.me/nft/PlusPepe-1",
            "amount": 25000, "currency": "KZT", "pay_method": "kaspi",
            "requisites": "+77015550123",
        })
        data = await r.json()
        check(r.status == 200, "сделка в KZT создана")
        deal = data["deal"]
        deal_id = deal["id"]
        check(deal["currency"] == "KZT", "валюта сделки KZT")
        check(deal["user_role"] == "buyer", "роль пользователя — покупатель")
        check(deal["stage"] == 0, "этап 0 — создана")
        check(deal["status_title"] == "Создана", "статус создан")

        print("\n[5] Сделка в белорусских рублях (продажа)")
        r = await post("/api/deals", {
            "side": "sell", "title": "2 Crystals и 1 Butterfly",
            "amount": 800, "currency": "BYN", "pay_method": "card_by",
            "requisites": "4111 1111 1111 1111",
        })
        data = await r.json()
        check(r.status == 200, "сделка в BYN создана")
        check(data["deal"]["user_role"] == "seller", "роль пользователя — продавец")

        print("\n[6] Полный жизненный цикл сделки в KZT")
        # Стартового баланса нет: перед оплатой кошелёк пополняем явно
        r = await get("/api/wallets")
        check(
            (await r.json())["balances"]["KZT"] == 0.0,
            "стартовый баланс равен нулю",
        )
        r = await post("/api/wallets/topup", {"currency": "KZT", "amount": 50000})
        check(r.status == 200, "кошелёк пополнен явно")

        await service.counterparty_join(deal_id, "Aliclownsi")
        r = await get(f"/api/deals/{deal_id}")
        deal = (await r.json())["deal"]
        check(deal["status"] == D.COUNTERPARTY_JOINED, "продавец присоединился")
        check(deal["counterparty"] == "Aliclownsi", "имя продавца сохранено")
        check("pay" in deal["actions"], "покупателю доступна оплата")

        r = await post(f"/api/deals/{deal_id}/deliver")
        check(r.status == 400, "продавец не может передать товар до оплаты")

        r = await post(f"/api/deals/{deal_id}/pay")
        check(r.status == 200, "оплата прошла")
        deal = (await r.json())["deal"]
        check(deal["status"] == D.PAID, "статус — оплачена")
        check(deal["stage"] == 2, "этап 2 — оплачена")

        await service.counterparty_join(1, "hacker")
        r = await post(f"/api/deals/{deal_id}/confirm")
        check(r.status == 400, "подтвердить получение до передачи нельзя")

        await service._demo_deliver(deal_id)
        r = await get(f"/api/deals/{deal_id}")
        deal = (await r.json())["deal"]
        check(deal["status"] == D.DELIVERED, "продавец передал подарок")
        check(deal["stage"] == 3, "этап 3 — встреча")

        r = await post(f"/api/deals/{deal_id}/confirm")
        check(r.status == 200, "покупатель подтвердил получение")
        deal = (await r.json())["deal"]
        check(deal["status"] == D.COMPLETED, "сделка завершена")
        check(deal["stage"] == 4, "этап 4 — получена")

        r = await post(f"/api/deals/{deal_id}/review", {"text": "Всё прошло отлично"})
        check(r.status == 403, f"отзывы внутри бота выключены (HTTP {r.status})")

        print("\n[7] Списки, кошельки, лидеры, отзывы")
        r = await get("/api/deals?scope=history")
        history = (await r.json())["deals"]
        check(len(history) == 1, f"в истории 1 завершённая сделка (получено {len(history)})")

        r = await get("/api/deals?scope=active")
        active = (await r.json())["deals"]
        check(len(active) == 1, f"в активных 1 сделка в BYN (получено {len(active)})")

        r = await get("/api/wallets")
        balances = (await r.json())["balances"]
        expected = {"TON", "USDT", "STARS", "RUB", "USD", "EUR", "KZT", "BYN", "UAH"}
        check(set(balances) == expected, f"кошельки по всем валютам ({len(balances)})")

        r = await post("/api/wallets/topup", {"currency": "BYN", "amount": 500})
        check(r.status == 200, "пополнение BYN прошло")
        check((await r.json())["balances"]["BYN"] >= 500, "баланс BYN вырос")

        r = await get("/api/leaders")
        check(len((await r.json())["leaders"]) >= 1, "таблица лидеров заполнена")

        r = await get("/api/reviews")
        review_payload = await r.json()
        check(len(review_payload["reviews"]) == 0, "список отзывов пуст")
        rating = review_payload["rating"]
        check(rating["count"] == 0, "рейтинг не накручен")
        check(deal["code"].startswith("deal_") is False, f"у сделки есть код {deal['code']}")

        print("\n[8] Защита от чужих сделок")
        r = await get("/api/deals/9999")
        check(r.status == 400, "несуществующая сделка отклонена")

        print("\n[9] Статика Mini App")
        for path, label in [
            ("/", "index.html"),
            ("/static/styles.css", "styles.css"),
            ("/static/app.js", "app.js"),
        ]:
            r = await get(path)
            check(r.status == 200, f"{label} отдаётся")

    await service.shutdown()
    await database.close()

    print()
    if FAILURES:
        print(f"ПРОВАЛЕНО проверок: {len(FAILURES)}")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("Все проверки пройдены")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))