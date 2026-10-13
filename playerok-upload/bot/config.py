from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


@dataclass
class Config:
    bot_token: str = field(default_factory=lambda: _env("BOT_TOKEN"))
    # Заполняется автоматически при старте через getMe
    bot_username: str = field(default_factory=lambda: _env("BOT_USERNAME"))
    webapp_url: str = field(default_factory=lambda: _env("WEBAPP_URL").rstrip("/"))
    port: int = field(default_factory=lambda: int(_env("PORT", "8080")))
    manager_username: str = field(
        default_factory=lambda: _env("MANAGER_USERNAME").lstrip("@")
    )
    db_path: Path = field(
        default_factory=lambda: BASE_DIR / _env("DB_PATH", "data/deals.db")
    )
    escrow_provider: str = field(
        default_factory=lambda: _env("ESCROW_PROVIDER", "mock") or "mock"
    )
    funpay_account_id: str = field(default_factory=lambda: _env("FUNPAY_ACCOUNT_ID"))
    funpay_api_key: str = field(default_factory=lambda: _env("FUNPAY_API_KEY"))
    funpay_api_url: str = field(default_factory=lambda: _env("FUNPAY_API_URL"))

    @property
    def manager_link(self) -> str | None:
        """Ссылка на менеджера. None — если аккаунт ещё не указан."""
        if not self.manager_username:
            return None
        return f"https://t.me/{self.manager_username}"

    @property
    def bot_link(self) -> str | None:
        """Ссылка на бота. None — если username ещё неизвестен."""
        if not self.bot_username:
            return None
        return f"https://t.me/{self.bot_username}"

    def validate(self) -> None:
        if not self.bot_token or ":" not in self.bot_token:
            raise SystemExit(
                "BOT_TOKEN не задан. Скопируй .env.example в .env и вставь токен от @BotFather"
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)


config = Config()