"""Хранилище: пользователи, сделки, события, отзывы."""

from __future__ import annotations

import json
import time
from typing import Any, Iterable

import logging

import aiosqlite

from .config import config

log = logging.getLogger(__name__)

# --- Статусы сделки -------------------------------------------------------
CREATED = "created"                # Создана
AWAITING_COUNTERPARTY = "awaiting_counterparty"  # Ждём вторую сторону
COUNTERPARTY_JOINED = "counterparty_joined"      # Вторая сторона в сделке
PAID = "paid"                      # Оплачена (escrow)
DELIVERED = "delivered"            # Встреча: товар передан
COMPLETED = "completed"            # Получена: сделка завершена
CANCELLED = "cancelled"

ACTIVE_STATUSES = (CREATED, AWAITING_COUNTERPARTY, COUNTERPARTY_JOINED, PAID, DELIVERED)

# Этапы прогресс-бара, как в оригинальном интерфейсе
STAGES = ("Создана", "Продавец", "Оплачена", "Встреча", "Получена")

STAGE_BY_STATUS = {
    CREATED: 0,
    AWAITING_COUNTERPARTY: 0,
    COUNTERPARTY_JOINED: 1,
    PAID: 2,
    DELIVERED: 3,
    COMPLETED: 4,
}

STATUS_TITLES = {
    CREATED: "Создана",
    AWAITING_COUNTERPARTY: "Ожидание второй стороны",
    COUNTERPARTY_JOINED: "Ожидание оплаты",
    PAID: "Ожидает передачи",
    DELIVERED: "Ожидает получения",
    COMPLETED: "Завершена",
    CANCELLED: "Отменена",
}

# Кто из сторон исполняет действие на каждом шаге.
# side: "sell" — пользователь покупает (продавец — контент-мейкер)
#       "buy"  — пользователь продаёт (покупатель — контент-мейкер)
def awaiting_side(side: str) -> str:
    """Чья сторона должна сделать следующий шаг."""
    return "buyer" if side == "sell" else "seller"


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id    INTEGER PRIMARY KEY,
    username   TEXT,
    first_name TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS deals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    code          TEXT,
    user_id       INTEGER NOT NULL,
    counterparty_id INTEGER,
    side          TEXT    NOT NULL,
    title         TEXT    NOT NULL,
    amount        REAL    NOT NULL,
    currency      TEXT    NOT NULL,
    pay_method    TEXT    NOT NULL,
    status        TEXT    NOT NULL,
    counterparty  TEXT,
    requisites    TEXT,
    provider_ref  TEXT,
    created_at    REAL    NOT NULL,
    updated_at    REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_deals_user  ON deals(user_id, status);
CREATE INDEX IF NOT EXISTS idx_deals_state ON deals(status, updated_at);

CREATE TABLE IF NOT EXISTS deal_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id     INTEGER NOT NULL,
    actor       TEXT,
    from_status TEXT,
    to_status   TEXT,
    created_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_deal ON deal_events(deal_id);

CREATE TABLE IF NOT EXISTS reviews (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id    INTEGER NOT NULL,
    author_id  INTEGER NOT NULL,
    text       TEXT    NOT NULL,
    score      INTEGER NOT NULL DEFAULT 5,
    created_at REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reviews_deal ON reviews(deal_id);

CREATE TABLE IF NOT EXISTS wallets (
    user_id  INTEGER NOT NULL,
    currency TEXT    NOT NULL,
    balance  REAL    NOT NULL DEFAULT 0,
    -- Часть balance, которая пришла по сделке и ждёт подтверждения
    -- получения. В кошельке показывается со снежинкой, тратить нельзя.
    frozen   REAL    NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, currency)
);
"""


class Database:
    def __init__(self, path: str | None = None) -> None:
        self._path = str(path or config.db_path)
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._migrate()
        await self._db.commit()

    async def _migrate(self) -> None:
        """Добавляет колонки, появившиеся в новых версиях схемы."""
        wanted = {
            "deals": [("code", "TEXT"), ("counterparty_id", "INTEGER")],
            "reviews": [("score", "INTEGER NOT NULL DEFAULT 5")],
            "wallets": [("frozen", "REAL NOT NULL DEFAULT 0")],
        }
        for table, columns in wanted.items():
            cur = await self._db.execute(f"PRAGMA table_info({table})")
            existing = {r["name"] for r in await cur.fetchall()}
            for name, decl in columns:
                if name not in existing:
                    await self._db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                    log.info("Миграция: %s.%s добавлена", table, name)

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Database.connect() не вызван")
        return self._db

    # --- Пользователи ------------------------------------------------------
    async def upsert_user(self, user_id: int, username: str | None, first_name: str | None) -> None:
        await self.db.execute(
            """INSERT INTO users (user_id, username, first_name, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   username   = excluded.username,
                   first_name = excluded.first_name""",
            (user_id, username, first_name, time.time()),
        )
        await self.db.commit()

    async def get_user(self, user_id: int) -> aiosqlite.Row | None:
        cur = await self.db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return await cur.fetchone()

    # --- Сделки ------------------------------------------------------------
    async def create_deal(self, **fields: Any) -> int:
        now = time.time()
        cur = await self.db.execute(
            """INSERT INTO deals
               (code, user_id, side, title, amount, currency, pay_method, status,
                counterparty, requisites, provider_ref, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                fields.get("code"),
                fields["user_id"],
                fields["side"],
                fields["title"],
                fields["amount"],
                fields["currency"],
                fields["pay_method"],
                fields.get("status", CREATED),
                fields.get("counterparty"),
                fields.get("requisites"),
                fields.get("provider_ref"),
                now,
                now,
            ),
        )
        await self.db.commit()
        deal_id = int(cur.lastrowid or 0)
        await self.add_event(deal_id, actor="user", to_status=fields.get("status", CREATED))
        return deal_id

    async def get_deal(self, deal_id: int) -> aiosqlite.Row | None:
        cur = await self.db.execute("SELECT * FROM deals WHERE id = ?", (deal_id,))
        return await cur.fetchone()

    async def get_deal_by_code(self, code: str) -> aiosqlite.Row | None:
        cur = await self.db.execute("SELECT * FROM deals WHERE code = ?", (code,))
        return await cur.fetchone()

    async def rating(self) -> dict:
        """Средний рейтинг и число отзывов (для экрана «Отзывы»)."""
        cur = await self.db.execute(
            "SELECT AVG(score) AS avg_score, COUNT(*) AS total FROM reviews"
        )
        row = await cur.fetchone()
        avg = float(row["avg_score"] or 0.0)
        return {"rating": round(avg, 1), "stars": int(round(avg)), "count": int(row["total"] or 0)}

    async def set_status(self, deal_id: int, status: str, provider_ref: str | None = None) -> None:
        row = await self.get_deal(deal_id)
        if row is None:
            return
        await self.db.execute(
            """UPDATE deals
               SET status = ?, updated_at = ?,
                   provider_ref = COALESCE(?, provider_ref)
               WHERE id = ?""",
            (status, time.time(), provider_ref, deal_id),
        )
        await self.db.commit()
        await self.add_event(deal_id, actor="system", from_status=row["status"], to_status=status)

    async def set_counterparty(
        self, deal_id: int, username: str | None, counterparty_id: int | None = None
    ) -> None:
        await self.db.execute(
            """UPDATE deals
               SET counterparty = ?, counterparty_id = COALESCE(?, counterparty_id),
                   updated_at = ?
               WHERE id = ?""",
            (username, counterparty_id, time.time(), deal_id),
        )
        await self.db.commit()

    async def list_deals(
        self,
        user_id: int | None = None,
        statuses: Iterable[str] | None = None,
        limit: int = 50,
    ) -> list[aiosqlite.Row]:
        sql = "SELECT * FROM deals"
        params: list[Any] = []
        where = []
        if user_id is not None:
            where.append("user_id = ?")
            params.append(user_id)
        if statuses is not None:
            statuses = tuple(statuses)
            if not statuses:
                return []
            where.append(f"status IN ({','.join('?' * len(statuses))})")
            params.extend(statuses)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        cur = await self.db.execute(sql, params)
        return list(await cur.fetchall())

    async def delete_deal(self, deal_id: int) -> None:
        await self.db.execute("DELETE FROM deals WHERE id = ?", (deal_id,))
        await self.db.execute("DELETE FROM deal_events WHERE deal_id = ?", (deal_id,))
        await self.db.commit()

    # --- События -----------------------------------------------------------
    async def add_event(
        self,
        deal_id: int,
        actor: str | None = None,
        from_status: str | None = None,
        to_status: str | None = None,
    ) -> None:
        await self.db.execute(
            """INSERT INTO deal_events (deal_id, actor, from_status, to_status, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (deal_id, actor, from_status, to_status, time.time()),
        )
        await self.db.commit()

    async def list_events(self, deal_id: int) -> list[aiosqlite.Row]:
        cur = await self.db.execute(
            "SELECT * FROM deal_events WHERE deal_id = ? ORDER BY id", (deal_id,)
        )
        return list(await cur.fetchall())

    # --- Отзывы ------------------------------------------------------------
    async def add_review(
        self, deal_id: int, author_id: int, text: str, score: int = 5
    ) -> int:
        score = max(1, min(5, int(score)))
        cur = await self.db.execute(
            """INSERT INTO reviews (deal_id, author_id, text, score, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (deal_id, author_id, text, score, time.time()),
        )
        await self.db.commit()
        return int(cur.lastrowid or 0)

    async def list_reviews(self, deal_id: int | None = None, limit: int = 20) -> list[aiosqlite.Row]:
        if deal_id is None:
            cur = await self.db.execute(
                "SELECT * FROM reviews ORDER BY id DESC LIMIT ?", (limit,)
            )
        else:
            cur = await self.db.execute(
                "SELECT * FROM reviews WHERE deal_id = ? ORDER BY id DESC LIMIT ?",
                (deal_id, limit),
            )
        return list(await cur.fetchall())

    # --- Кошельки ----------------------------------------------------------
    async def get_balance(self, user_id: int, currency: str) -> float:
        cur = await self.db.execute(
            "SELECT balance FROM wallets WHERE user_id = ? AND currency = ?",
            (user_id, currency),
        )
        row = await cur.fetchone()
        return float(row["balance"]) if row else 0.0

    async def list_balances(self, user_id: int) -> dict[str, float]:
        cur = await self.db.execute(
            "SELECT currency, balance FROM wallets WHERE user_id = ?", (user_id,)
        )
        return {r["currency"]: float(r["balance"]) for r in await cur.fetchall()}

    async def top_up(self, user_id: int, currency: str, amount: float) -> float:
        await self.db.execute(
            """INSERT INTO wallets (user_id, currency, balance) VALUES (?, ?, ?)
               ON CONFLICT(user_id, currency) DO UPDATE SET
                   balance = wallets.balance + excluded.balance""",
            (user_id, currency, amount),
        )
        await self.db.commit()
        return await self.get_balance(user_id, currency)

    async def withdraw(self, user_id: int, currency: str, amount: float) -> float:
        """Списывает средства. Бросает ValueError при нехватке баланса."""
        current = await self.get_balance(user_id, currency)
        if current + 1e-9 < amount:
            raise ValueError(f"Недостаточно средств: доступно {current:.2f} {currency}")
        await self.db.execute(
            """UPDATE wallets SET balance = balance - ?
               WHERE user_id = ? AND currency = ?""",
            (amount, user_id, currency),
        )
        await self.db.commit()
        return await self.get_balance(user_id, currency)

    async def frozen_balance(self, user_id: int, currency: str) -> float:
        cur = await self.db.execute(
            "SELECT frozen FROM wallets WHERE user_id = ? AND currency = ?",
            (user_id, currency),
        )
        row = await cur.fetchone()
        return float(row["frozen"]) if row else 0.0

    async def balances_with_frozen(
        self, user_id: int
    ) -> dict[str, dict[str, float]]:
        """Балансы вместе с замороженной частью — для кошелька в интерфейсе."""
        cur = await self.db.execute(
            "SELECT currency, balance, frozen FROM wallets WHERE user_id = ?",
            (user_id,),
        )
        return {
            r["currency"]: {
                "balance": float(r["balance"]),
                "frozen": float(r["frozen"]),
            }
            for r in await cur.fetchall()
        }

    async def credit_frozen(
        self, user_id: int, currency: str, amount: float
    ) -> None:
        """Зачисляет средства как замороженные: видны в кошельке, но не тратятся."""
        await self.db.execute(
            """INSERT INTO wallets (user_id, currency, balance, frozen)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id, currency) DO UPDATE SET
                   balance = wallets.balance + excluded.balance,
                   frozen  = wallets.frozen  + excluded.frozen""",
            (user_id, currency, amount, amount),
        )
        await self.db.commit()

    async def unfreeze(self, user_id: int, currency: str, amount: float) -> None:
        """Снимает заморозку после подтверждения — сумма остаётся на балансе."""
        await self.db.execute(
            """UPDATE wallets SET frozen = MAX(0, frozen - ?)
               WHERE user_id = ? AND currency = ?""",
            (amount, user_id, currency),
        )
        await self.db.commit()

    async def drop_frozen(self, user_id: int, currency: str, amount: float) -> None:
        """Сделка отменена: убирает зачисленную сумму вместе с заморозкой."""
        await self.db.execute(
            """UPDATE wallets
               SET balance = MAX(0, balance - ?), frozen = MAX(0, frozen - ?)
               WHERE user_id = ? AND currency = ?""",
            (amount, amount, user_id, currency),
        )
        await self.db.commit()

    # --- Лидеры ------------------------------------------------------------
    async def top_users(self, limit: int = 10) -> list[aiosqlite.Row]:
        """Пользователи по числу завершённых сделок."""
        cur = await self.db.execute(
            """SELECT u.user_id, u.username, u.first_name,
                      COUNT(d.id) AS deals,
                      COALESCE(SUM(d.amount), 0) AS volume
               FROM users u
               JOIN deals d ON d.user_id = u.user_id
               WHERE d.status = ?
               GROUP BY u.user_id
               ORDER BY deals DESC, volume DESC
               LIMIT ?""",
            (COMPLETED, limit),
        )
        return list(await cur.fetchall())


db = Database()


def row_to_deal(row: aiosqlite.Row) -> dict[str, Any]:
    """Сериализует сделку в словарь для API Mini App."""
    requisites: dict | None = None
    if row["requisites"]:
        try:
            requisites = json.loads(row["requisites"])
        except json.JSONDecodeError:
            requisites = None
    return {
        "id": row["id"],
        "code": row["code"] or f"DEAL{row['id']}",
        "user_id": row["user_id"],
        "counterparty_id": row["counterparty_id"],
        "side": row["side"],
        "title": row["title"],
        "amount": row["amount"],
        "currency": row["currency"],
        "pay_method": row["pay_method"],
        "status": row["status"],
        "status_title": STATUS_TITLES.get(row["status"], row["status"]),
        "stage": STAGE_BY_STATUS.get(row["status"], 0),
        "counterparty": row["counterparty"],
        "requisites": requisites,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }