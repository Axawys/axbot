# axbot — Telegram anti-spam captcha bot
# Copyright (C) 2026 Axawys
# SPDX-License-Identifier: 0BSD
"""Shared helpers: access checks, callback data, formatting."""

from __future__ import annotations

import logging
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.filters.callback_data import CallbackData

from .config import Config
from .db import ChatSettings, Database
from .questions import MODES, get_theme

log = logging.getLogger(__name__)

GROUP_TYPES = {"group", "supergroup"}

# Telegram's "anonymous group admin" helper account.
ANONYMOUS_ADMIN_ID = 1087968824


# ------------------------------------------------------------- callback data


class Cap(CallbackData, prefix="c"):
    """Captcha button in a group. idx=-1 means 'already pressed'."""

    user_id: int
    idx: int


class Try(CallbackData, prefix="t"):
    """Test captcha in private chat (stateless)."""

    qid: str
    idx: int
    progress: int


class Panel(CallbackData, prefix="p"):
    """Settings panel in private chat."""

    action: str
    chat_id: int = 0
    value: str = ""


# ------------------------------------------------------------- access checks


async def is_chat_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except TelegramAPIError:
        return False
    return member.status in ("creator", "administrator")


async def is_chat_creator(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except TelegramAPIError:
        return False
    return member.status == "creator"


async def is_owner(bot: Bot, db: Database, config: Config, chat_id: int, user_id: int) -> bool:
    """Owner = whoever added the bot, the chat creator, or a bot superadmin."""
    if user_id in config.superadmins:
        return True
    chat = await db.get_chat(chat_id)
    if chat and chat.owner_id == user_id:
        return True
    return await is_chat_creator(bot, chat_id, user_id)


async def can_manage(bot: Bot, db: Database, config: Config, chat_id: int, user_id: int) -> bool:
    """Managers = owner (see above) + people the owner explicitly allowed."""
    if await db.is_manager(chat_id, user_id):
        return True
    return await is_owner(bot, db, config, chat_id, user_id)


# --------------------------------------------------------------- formatting


def fmt_duration(seconds: int) -> str:
    if seconds <= 0:
        return "никогда"
    if seconds < 60:
        return f"{seconds} сек"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} мин" + (f" {sec} сек" if sec else "")
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ч" + (f" {minutes} мин" if minutes else "")


def mention(user_id: int, name: str) -> str:
    return f'<a href="tg://user?id={user_id}">{escape(name or str(user_id))}</a>'


def full_name(user) -> str:
    return user.full_name or (user.username and f"@{user.username}") or str(user.id)


FAIL_ACTIONS = {
    "kick": "👢 кик (может зайти снова)",
    "ban": "🔨 бан навсегда",
}

def yes_no(value: bool) -> str:
    return "✅" if value else "❌"


def settings_summary(chat: ChatSettings) -> str:
    greeting = "своё" if chat.greeting else "стандартное"
    return (
        f"⚙️ Настройки чата <b>{escape(chat.title or str(chat.chat_id))}</b>\n\n"
        f"Капча: {'✅ включена' if chat.enabled else '❌ выключена'}\n"
        f"Время на ответ: {fmt_duration(chat.timeout)}\n"
        f"Попыток: {chat.attempts}\n"
        f"При провале: {FAIL_ACTIONS.get(chat.fail_action, chat.fail_action)}\n"
        f"Тематика: {get_theme(chat.theme).title}\n"
        f"Вопросы: {MODES.get(chat.mode, chat.mode)}\n"
        f"Удалять «вступил / вышел»: {yes_no(chat.clean_service)}\n"
        f"Приветствие: {greeting}, автоудаление — {fmt_duration(chat.greeting_ttl)}"
    )


# Limits shared by the panel and group commands.
TIMEOUT_MIN, TIMEOUT_MAX = 30, 3600
ATTEMPTS_MIN, ATTEMPTS_MAX = 1, 5
TIMEOUT_PRESETS = [60, 90, 120, 180, 300, 600]
GREETING_TTL_PRESETS = [0, 30, 60, 300, 900]
