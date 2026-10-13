"""Валюты и способы оплаты — единый источник правды для бота и Mini App."""

from __future__ import annotations

# Поддерживаемые активы. Порядок определяет вывод в профиле.
# icon/color используются в интерфейсе профиля.
CURRENCIES: dict[str, dict] = {
    "TON": {"title": "TON", "flag": "🔷", "symbol": "TON", "icon": "◆", "color": "#2b7cff"},
    "USDT": {"title": "USDT", "flag": "🟢", "symbol": "₮", "icon": "₮", "color": "#26a17b"},
    "STARS": {"title": "Stars", "flag": "⭐", "symbol": "★", "icon": "★", "color": "#f5a623"},
    "RUB": {"title": "Российский рубль", "flag": "🇷🇺", "symbol": "₽", "icon": "₽", "color": "#2f6bd8"},
    "USD": {"title": "Доллар США", "flag": "🇺🇸", "symbol": "$", "icon": "$", "color": "#3aa76d"},
    "EUR": {"title": "Евро", "flag": "🇪🇺", "symbol": "€", "icon": "€", "color": "#2b6cb0"},
    "KZT": {"title": "Казахстанский тенге", "flag": "🇰🇿", "symbol": "₸", "icon": "₸", "color": "#38b6ff"},
    "BYN": {"title": "Белорусский рубль", "flag": "🇧🇾", "symbol": "Br", "icon": "Br", "color": "#c0392b"},
    "UAH": {"title": "Украинская гривна", "flag": "🇺🇦", "symbol": "₴", "icon": "₴", "color": "#005bbb"},
}

# Способы оплаты. currencies: список валют, для которых способ доступен.
# ANY — способ принимает любую валюту (крипта / внутренние способы Telegram).
ANY = "__any__"

PAY_METHODS: dict[str, dict] = {
    "card_ru": {"title": "Карта РФ / СБП", "currencies": ["RUB"]},
    "card_ua": {"title": "Карта УКР", "currencies": ["UAH"]},
    "kaspi": {"title": "Kaspi.kz", "currencies": ["KZT"]},
    "card_kz": {"title": "Карта Казахстана", "currencies": ["KZT"]},
    "card_by": {"title": "Карта Беларуси (БПС-Скарт)", "currencies": ["BYN"]},
    "ton": {"title": "TON-кошелёк", "currencies": [ANY]},
    "usdt_trc20": {"title": "USDT (TRC-20)", "currencies": [ANY]},
    "stars": {"title": "Telegram Stars", "currencies": [ANY]},
}

# Способы, по которым нужны реквизиты (выводятся до создания сделки)
NEEDS_REQUISITES = {"card_ru", "card_ua", "kaspi", "card_kz", "card_by", "ton"}


def methods_for_currency(currency: str) -> list[dict]:
    """Способы оплаты, доступные для валюты."""
    out = []
    for code, meta in PAY_METHODS.items():
        if currency in meta["currencies"] or ANY in meta["currencies"]:
            out.append({"code": code, "title": meta["title"]})
    return out


def default_method_for(currency: str) -> str | None:
    methods = methods_for_currency(currency)
    return methods[0]["code"] if methods else None


def is_valid_method(code: str, currency: str) -> bool:
    meta = PAY_METHODS.get(code)
    if not meta:
        return False
    return currency in meta["currencies"] or ANY in meta["currencies"]


def money(amount: float, currency: str) -> str:
    """Форматирует сумму как в интерфейсе: 321312.00 RUB."""
    return f"{amount:,.2f} {currency}".replace(",", " ")