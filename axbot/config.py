# axbot — Telegram anti-spam captcha bot
# Copyright (C) 2026 Axawys
# SPDX-License-Identifier: GPL-3.0-or-later

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    bot_token: str
    db_path: str
    superadmins: frozenset[int]
    log_level: str
    timezone: str = "UTC"


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines, existing env vars win."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def load_config() -> Config:
    _load_dotenv(Path(os.environ.get("AXBOT_ENV_FILE", ".env")))

    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("BOT_TOKEN is not set (see .env.example)")

    superadmins = frozenset(
        int(x) for x in os.environ.get("SUPERADMINS", "").replace(",", " ").split() if x.strip()
    )
    return Config(
        bot_token=token,
        db_path=os.environ.get("DB_PATH", "axbot.sqlite3"),
        superadmins=superadmins,
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        timezone=os.environ.get("TIMEZONE", "UTC").strip() or "UTC",
    )
