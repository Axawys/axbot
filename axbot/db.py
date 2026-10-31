# axbot — Telegram anti-spam captcha bot
# Copyright (C) 2026 Axawys
# SPDX-License-Identifier: 0BSD

from __future__ import annotations

import time
from dataclasses import dataclass, fields

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    chat_id       INTEGER PRIMARY KEY,
    title         TEXT    NOT NULL DEFAULT '',
    owner_id      INTEGER,
    active        INTEGER NOT NULL DEFAULT 1,
    enabled       INTEGER NOT NULL DEFAULT 1,
    timeout       INTEGER NOT NULL DEFAULT 120,
    attempts      INTEGER NOT NULL DEFAULT 2,
    fail_action   TEXT    NOT NULL DEFAULT 'kick',
    mode          TEXT    NOT NULL DEFAULT 'mixed',
    theme         TEXT    NOT NULL DEFAULT 'universal',
    greeting      TEXT,
    greeting_ttl  INTEGER NOT NULL DEFAULT 0,
    clean_service INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS managers (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);

CREATE TABLE IF NOT EXISTS pending (
    chat_id       INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    name          TEXT    NOT NULL DEFAULT '',
    message_id    INTEGER NOT NULL,
    question_id   TEXT    NOT NULL,
    progress      INTEGER NOT NULL DEFAULT 0,
    attempts_left INTEGER NOT NULL,
    deadline      INTEGER NOT NULL,
    restore_perms TEXT,
    PRIMARY KEY (chat_id, user_id)
);

-- Captcha history for statistics. kind: see EVENT_KINDS.
CREATE TABLE IF NOT EXISTS events (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    kind    TEXT    NOT NULL,
    ts      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS events_chat_ts ON events (chat_id, ts);

CREATE TABLE IF NOT EXISTS scheduled_deletes (
    chat_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    delete_at  INTEGER NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);
"""

EVENT_KINDS = {
    "captcha",       # captcha shown to a newcomer
    "pass",          # answered correctly
    "pass_manual",   # let in by a manager with /pass
    "fail_timeout",  # did not answer in time
    "fail_wrong",    # ran out of attempts
    "bot_kicked",    # a bot added by a regular member
    "left",          # left the chat before answering
}

# Columns of `chats` that may be changed through settings.
SETTING_FIELDS = {
    "enabled",
    "timeout",
    "attempts",
    "fail_action",
    "mode",
    "theme",
    "greeting",
    "greeting_ttl",
    "clean_service",
}


@dataclass
class ChatSettings:
    chat_id: int
    title: str = ""
    owner_id: int | None = None
    active: bool = True
    enabled: bool = True
    timeout: int = 120
    attempts: int = 2
    fail_action: str = "kick"
    mode: str = "mixed"
    theme: str = "universal"
    greeting: str | None = None
    greeting_ttl: int = 0
    clean_service: bool = True


@dataclass
class Pending:
    chat_id: int
    user_id: int
    name: str
    message_id: int
    question_id: str
    progress: int
    attempts_left: int
    deadline: int
    restore_perms: str | None = None


def _from_row(cls, row: aiosqlite.Row):
    names = {f.name for f in fields(cls)}
    data = {k: row[k] for k in row.keys() if k in names}
    for f in fields(cls):
        if f.type in ("bool",) and f.name in data:
            data[f.name] = bool(data[f.name])
    return cls(**data)


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database is not connected"
        return self._conn

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.executescript(SCHEMA)
        await self._migrate()
        await self._conn.commit()

    async def _migrate(self) -> None:
        """Add columns that appeared after the first release."""
        async with self.conn.execute("PRAGMA table_info(chats)") as cur:
            columns = {row["name"] for row in await cur.fetchall()}
        if "theme" not in columns:
            await self.conn.execute("ALTER TABLE chats ADD COLUMN theme TEXT NOT NULL DEFAULT 'universal'")
            # Chats created before themes existed were Linux chats.
            await self.conn.execute("UPDATE chats SET theme = 'linux'")

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # ---------------------------------------------------------------- chats

    async def get_chat(self, chat_id: int) -> ChatSettings | None:
        async with self.conn.execute("SELECT * FROM chats WHERE chat_id = ?", (chat_id,)) as cur:
            row = await cur.fetchone()
        return _from_row(ChatSettings, row) if row else None

    async def get_or_create_chat(self, chat_id: int, title: str = "") -> ChatSettings:
        await self.conn.execute(
            "INSERT OR IGNORE INTO chats (chat_id, title) VALUES (?, ?)", (chat_id, title)
        )
        if title:
            await self.conn.execute(
                "UPDATE chats SET title = ? WHERE chat_id = ?", (title, chat_id)
            )
        await self.conn.commit()
        chat = await self.get_chat(chat_id)
        assert chat is not None
        return chat

    async def set_chat_active(self, chat_id: int, active: bool) -> None:
        await self.conn.execute(
            "UPDATE chats SET active = ? WHERE chat_id = ?", (int(active), chat_id)
        )
        await self.conn.commit()

    async def set_owner(self, chat_id: int, owner_id: int) -> None:
        await self.conn.execute(
            "UPDATE chats SET owner_id = ? WHERE chat_id = ?", (owner_id, chat_id)
        )
        await self.conn.commit()

    async def update_setting(self, chat_id: int, key: str, value) -> None:
        if key not in SETTING_FIELDS:
            raise ValueError(f"unknown setting: {key}")
        if isinstance(value, bool):
            value = int(value)
        await self.conn.execute(f"UPDATE chats SET {key} = ? WHERE chat_id = ?", (value, chat_id))
        await self.conn.commit()

    async def chats_for_user(self, user_id: int, all_chats: bool = False) -> list[ChatSettings]:
        if all_chats:
            query = "SELECT * FROM chats WHERE active = 1 ORDER BY title"
            params: tuple = ()
        else:
            query = """
                SELECT * FROM chats
                WHERE active = 1 AND (
                    owner_id = ?
                    OR chat_id IN (SELECT chat_id FROM managers WHERE user_id = ?)
                )
                ORDER BY title
            """
            params = (user_id, user_id)
        async with self.conn.execute(query, params) as cur:
            rows = await cur.fetchall()
        return [_from_row(ChatSettings, r) for r in rows]

    async def migrate_chat(self, old_id: int, new_id: int) -> None:
        for table in ("chats", "managers", "pending", "scheduled_deletes", "events"):
            await self.conn.execute(
                f"UPDATE OR IGNORE {table} SET chat_id = ? WHERE chat_id = ?", (new_id, old_id)
            )
        await self.conn.commit()

    # ------------------------------------------------------------- managers

    async def is_manager(self, chat_id: int, user_id: int) -> bool:
        async with self.conn.execute(
            "SELECT 1 FROM managers WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)
        ) as cur:
            return await cur.fetchone() is not None

    async def add_manager(self, chat_id: int, user_id: int) -> None:
        await self.conn.execute(
            "INSERT OR IGNORE INTO managers (chat_id, user_id) VALUES (?, ?)", (chat_id, user_id)
        )
        await self.conn.commit()

    async def remove_manager(self, chat_id: int, user_id: int) -> bool:
        cur = await self.conn.execute(
            "DELETE FROM managers WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def list_managers(self, chat_id: int) -> list[int]:
        async with self.conn.execute(
            "SELECT user_id FROM managers WHERE chat_id = ? ORDER BY user_id", (chat_id,)
        ) as cur:
            return [r["user_id"] for r in await cur.fetchall()]

    # -------------------------------------------------------------- pending

    async def add_pending(self, p: Pending) -> None:
        await self.conn.execute(
            """INSERT OR REPLACE INTO pending
               (chat_id, user_id, name, message_id, question_id, progress,
                attempts_left, deadline, restore_perms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                p.chat_id,
                p.user_id,
                p.name,
                p.message_id,
                p.question_id,
                p.progress,
                p.attempts_left,
                p.deadline,
                p.restore_perms,
            ),
        )
        await self.conn.commit()

    async def get_pending(self, chat_id: int, user_id: int) -> Pending | None:
        async with self.conn.execute(
            "SELECT * FROM pending WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)
        ) as cur:
            row = await cur.fetchone()
        return _from_row(Pending, row) if row else None

    async def get_pending_by_message(self, chat_id: int, message_id: int) -> Pending | None:
        async with self.conn.execute(
            "SELECT * FROM pending WHERE chat_id = ? AND message_id = ?", (chat_id, message_id)
        ) as cur:
            row = await cur.fetchone()
        return _from_row(Pending, row) if row else None

    async def update_pending(self, chat_id: int, user_id: int, **values) -> None:
        allowed = {"question_id", "progress", "attempts_left", "message_id", "deadline"}
        if not values or not set(values) <= allowed:
            raise ValueError(f"bad pending fields: {values}")
        cols = ", ".join(f"{k} = ?" for k in values)
        await self.conn.execute(
            f"UPDATE pending SET {cols} WHERE chat_id = ? AND user_id = ?",
            (*values.values(), chat_id, user_id),
        )
        await self.conn.commit()

    async def remove_pending(self, chat_id: int, user_id: int) -> None:
        await self.conn.execute(
            "DELETE FROM pending WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)
        )
        await self.conn.commit()

    async def expired_pending(self, now: int) -> list[Pending]:
        async with self.conn.execute(
            "SELECT * FROM pending WHERE deadline <= ?", (now,)
        ) as cur:
            return [_from_row(Pending, r) for r in await cur.fetchall()]

    async def clear_pending_for_chat(self, chat_id: int) -> None:
        await self.conn.execute("DELETE FROM pending WHERE chat_id = ?", (chat_id,))
        await self.conn.commit()

    # --------------------------------------------------------------- events

    async def log_event(self, chat_id: int, user_id: int, kind: str, ts: int | None = None) -> None:
        if kind not in EVENT_KINDS:
            raise ValueError(f"unknown event kind: {kind}")
        await self.conn.execute(
            "INSERT INTO events (chat_id, user_id, kind, ts) VALUES (?, ?, ?, ?)",
            (chat_id, user_id, kind, int(time.time()) if ts is None else ts),
        )
        await self.conn.commit()

    async def events_between(self, chat_id: int, start_ts: int, end_ts: int) -> list[tuple[str, int]]:
        """(kind, ts) for start_ts <= ts < end_ts."""
        async with self.conn.execute(
            "SELECT kind, ts FROM events WHERE chat_id = ? AND ts >= ? AND ts < ? ORDER BY ts",
            (chat_id, start_ts, end_ts),
        ) as cur:
            return [(r["kind"], r["ts"]) for r in await cur.fetchall()]

    # ----------------------------------------------------- scheduled deletes

    async def schedule_delete(self, chat_id: int, message_id: int, delete_at: int) -> None:
        await self.conn.execute(
            "INSERT OR REPLACE INTO scheduled_deletes (chat_id, message_id, delete_at) VALUES (?, ?, ?)",
            (chat_id, message_id, delete_at),
        )
        await self.conn.commit()

    async def pop_due_deletes(self, now: int) -> list[tuple[int, int]]:
        async with self.conn.execute(
            "SELECT chat_id, message_id FROM scheduled_deletes WHERE delete_at <= ?", (now,)
        ) as cur:
            rows = [(r["chat_id"], r["message_id"]) for r in await cur.fetchall()]
        if rows:
            await self.conn.execute("DELETE FROM scheduled_deletes WHERE delete_at <= ?", (now,))
            await self.conn.commit()
        return rows
