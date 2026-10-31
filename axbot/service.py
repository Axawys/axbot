# axbot — Telegram anti-spam captcha bot
# Copyright (C) 2026 Axawys
# SPDX-License-Identifier: GPL-3.0-or-later
"""Captcha lifecycle: start → (answer)* → pass | fail, plus the watchdog."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import weakref
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    ChatMemberRestricted,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    User,
)

from .common import Cap, fmt_duration, full_name, mention
from .db import ChatSettings, Database, Pending
from .questions import QUESTIONS, Question, get_theme, pick_question

log = logging.getLogger(__name__)

_PERM_FIELDS = list(ChatPermissions.model_fields)
MUTED = ChatPermissions(**{f: False for f in _PERM_FIELDS})
UNMUTED = ChatPermissions(**{f: True for f in _PERM_FIELDS})

NOTICE_TTL = 60  # seconds a "user failed" notice stays in the chat

# One lock per (chat, user) so button spam / watchdog can't race each other.
_locks: "weakref.WeakValueDictionary[tuple[int, int], asyncio.Lock]" = weakref.WeakValueDictionary()


def user_lock(chat_id: int, user_id: int) -> asyncio.Lock:
    key = (chat_id, user_id)
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


# ------------------------------------------------------------------ render


def captcha_text(
    chat: ChatSettings, user_id: int, name: str, q: Question, progress: int, attempts: int, seconds_left: int
) -> str:
    lines = [
        f"👋 {mention(user_id, name)}, добро пожаловать в <b>{escape(chat.title)}</b>!",
        get_theme(chat.theme).intro,
        "",
        f"❓ {q.text}",
    ]
    if q.is_order:
        lines.append("<i>Нажимай кнопки по порядку.</i>")
        if progress:
            typed = " ".join(f"<code>{escape(q.options[i])}</code>" for i in q.answer[:progress])
            lines.append(f"Набрано: {typed}")
    lines += ["", f"⏳ Осталось: {fmt_duration(max(seconds_left, 1))} · попыток: {attempts}"]
    return "\n".join(lines)


def _per_row(options: list[str]) -> int:
    longest = max(len(o) for o in options)
    if longest > 16:
        return 1
    if longest > 6:
        return 2
    return min(len(options), 4)


def build_keyboard(q: Question, make_data) -> InlineKeyboardMarkup:
    """Shuffled keyboard; make_data(idx) returns callback_data for option idx."""
    order = list(range(len(q.options)))
    random.shuffle(order)
    per_row = _per_row(list(q.options))
    buttons = [InlineKeyboardButton(text=q.options[i], callback_data=make_data(i)) for i in order]
    rows = [buttons[i : i + per_row] for i in range(0, len(buttons), per_row)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def captcha_keyboard(q: Question, user_id: int) -> InlineKeyboardMarkup:
    return build_keyboard(q, lambda i: Cap(user_id=user_id, idx=i).pack())


def mark_pressed(markup: InlineKeyboardMarkup, pressed_data: str, new_data: str) -> InlineKeyboardMarkup:
    rows = []
    for row in markup.inline_keyboard:
        new_row = []
        for b in row:
            if b.callback_data == pressed_data:
                b = InlineKeyboardButton(text=f"✅ {b.text}", callback_data=new_data)
            new_row.append(b)
        rows.append(new_row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


# --------------------------------------------------------------- utilities


async def safe_delete(bot: Bot, chat_id: int, message_id: int) -> None:
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramAPIError:
        pass


async def send_temp(bot: Bot, db: Database, chat_id: int, text: str, ttl: int, **kwargs) -> None:
    try:
        msg = await bot.send_message(chat_id, text, **kwargs)
    except TelegramAPIError as e:
        log.warning("send to %s failed: %s", chat_id, e)
        return
    if ttl > 0:
        await db.schedule_delete(chat_id, msg.message_id, int(time.time()) + ttl)


def render_greeting(chat: ChatSettings, template: str | None, user_id: int, name: str) -> str:
    """template=None renders the theme's default greeting."""
    chat_title = chat.title
    text = template or get_theme(chat.theme).greeting
    return (
        text.replace("{mention}", mention(user_id, name))
        .replace("{name}", escape(name))
        .replace("{chat}", escape(chat_title))
    )


# ------------------------------------------------------------------- start


async def start_captcha(
    bot: Bot, db: Database, chat: ChatSettings, user: User, old_restriction: ChatMemberRestricted | None
) -> None:
    name = full_name(user)
    async with user_lock(chat.chat_id, user.id):
        restore = None
        if old_restriction is not None:
            restore = json.dumps(
                {f: bool(getattr(old_restriction, f, False)) for f in _PERM_FIELDS}
            )

        try:
            await bot.restrict_chat_member(chat.chat_id, user.id, permissions=MUTED)
        except TelegramAPIError as e:
            log.warning("cannot restrict %s in %s: %s", user.id, chat.chat_id, e)
            await send_temp(
                bot, db, chat.chat_id,
                "⚠️ Не могу ограничить нового участника — дайте мне права администратора "
                "«Блокировка пользователей» и «Удаление сообщений».",
                ttl=120,
            )

        q = pick_question(chat.theme, chat.mode)
        try:
            msg = await bot.send_message(
                chat.chat_id,
                captcha_text(chat, user.id, name, q, 0, chat.attempts, chat.timeout),
                reply_markup=captcha_keyboard(q, user.id),
            )
        except TelegramAPIError as e:
            log.error("cannot send captcha to %s: %s", chat.chat_id, e)
            await _lift(bot, chat.chat_id, user.id, restore)
            return

        await db.add_pending(
            Pending(
                chat_id=chat.chat_id,
                user_id=user.id,
                name=name,
                message_id=msg.message_id,
                question_id=q.id,
                progress=0,
                attempts_left=chat.attempts,
                deadline=int(time.time()) + chat.timeout,
                restore_perms=restore,
            )
        )
        log.info("captcha %s for user %s in chat %s", q.id, user.id, chat.chat_id)


# ------------------------------------------------------------------ answer


async def handle_answer(bot: Bot, db: Database, callback, data: Cap) -> None:
    message = callback.message
    chat_id = message.chat.id
    user = callback.from_user

    if data.user_id != user.id:
        await callback.answer("🐧 Это не твоя капча. Руки прочь от чужого терминала!", show_alert=True)
        return

    async with user_lock(chat_id, user.id):
        p = await db.get_pending(chat_id, user.id)
        if p is None or p.message_id != message.message_id:
            await callback.answer("Эта проверка уже неактуальна.")
            return
        if data.idx < 0:
            await callback.answer("Уже нажато 👍")
            return

        chat = await db.get_or_create_chat(chat_id, message.chat.title or "")
        q = QUESTIONS.get(p.question_id)
        seconds_left = p.deadline - int(time.time())

        if q is not None and data.idx == q.answer[p.progress]:
            progress = p.progress + 1
            if progress >= len(q.answer):
                await callback.answer("✅ Добро пожаловать!")
                await _pass(bot, db, chat, p)
                return
            await db.update_pending(chat_id, user.id, progress=progress)
            await callback.answer("👍")
            try:
                await message.edit_text(
                    captcha_text(chat, user.id, p.name, q, progress, p.attempts_left, seconds_left),
                    reply_markup=mark_pressed(
                        message.reply_markup, callback.data, Cap(user_id=user.id, idx=-1).pack()
                    ),
                )
            except TelegramAPIError as e:
                log.debug("edit failed: %s", e)
            return

        attempts = p.attempts_left - 1
        if attempts <= 0:
            await callback.answer("❌ Неверно. Попытки закончились.", show_alert=True)
            await _fail(bot, db, chat, p)
            return

        new_q = pick_question(chat.theme, chat.mode, exclude=p.question_id)
        await db.update_pending(chat_id, user.id, question_id=new_q.id, progress=0, attempts_left=attempts)
        await callback.answer(f"❌ Неверно! Вот другой вопрос. Осталось попыток: {attempts}", show_alert=True)
        try:
            await message.edit_text(
                captcha_text(chat, user.id, p.name, new_q, 0, attempts, seconds_left),
                reply_markup=captcha_keyboard(new_q, user.id),
            )
        except TelegramAPIError as e:
            log.debug("edit failed: %s", e)


# ------------------------------------------------------------ pass / fail


async def _lift(bot: Bot, chat_id: int, user_id: int, restore: str | None) -> None:
    perms = ChatPermissions(**json.loads(restore)) if restore else UNMUTED
    try:
        await bot.restrict_chat_member(chat_id, user_id, permissions=perms)
    except TelegramAPIError as e:
        log.warning("cannot unrestrict %s in %s: %s", user_id, chat_id, e)


async def _pass(bot: Bot, db: Database, chat: ChatSettings, p: Pending) -> None:
    await db.remove_pending(p.chat_id, p.user_id)
    await safe_delete(bot, p.chat_id, p.message_id)
    await _lift(bot, p.chat_id, p.user_id, p.restore_perms)
    msg = None
    for template in dict.fromkeys([chat.greeting, None]):  # custom first, default as fallback
        try:
            msg = await bot.send_message(
                p.chat_id, render_greeting(chat, template, p.user_id, p.name)
            )
            break
        except TelegramAPIError as e:
            log.warning("greeting failed in %s: %s", p.chat_id, e)
    if msg is not None and chat.greeting_ttl > 0:
        await db.schedule_delete(p.chat_id, msg.message_id, int(time.time()) + chat.greeting_ttl)
    log.info("user %s passed in chat %s", p.user_id, p.chat_id)


async def _fail(bot: Bot, db: Database, chat: ChatSettings, p: Pending) -> None:
    await db.remove_pending(p.chat_id, p.user_id)
    await safe_delete(bot, p.chat_id, p.message_id)
    try:
        if chat.fail_action == "ban":
            await bot.ban_chat_member(p.chat_id, p.user_id, revoke_messages=True)
        else:
            await bot.ban_chat_member(p.chat_id, p.user_id)
            await bot.unban_chat_member(p.chat_id, p.user_id, only_if_banned=True)
    except TelegramAPIError as e:
        log.warning("cannot remove %s from %s: %s", p.user_id, p.chat_id, e)
        return
    await send_temp(
        bot, db, p.chat_id, f"{escape(p.name)} {random.choice(get_theme(chat.theme).fail_phrases)}", ttl=NOTICE_TTL
    )
    log.info("user %s failed in chat %s (%s)", p.user_id, p.chat_id, chat.fail_action)


async def approve(bot: Bot, db: Database, chat_id: int, user_id: int) -> bool:
    """Manual pass by a manager. Returns False if the user had no pending captcha."""
    async with user_lock(chat_id, user_id):
        p = await db.get_pending(chat_id, user_id)
        if p is None:
            return False
        chat = await db.get_or_create_chat(chat_id)
        await _pass(bot, db, chat, p)
        return True


async def cancel(bot: Bot, db: Database, chat_id: int, user_id: int) -> None:
    """User left the chat while the captcha was pending."""
    async with user_lock(chat_id, user_id):
        p = await db.get_pending(chat_id, user_id)
        if p is None:
            return
        await db.remove_pending(chat_id, user_id)
        await safe_delete(bot, chat_id, p.message_id)


async def expire(bot: Bot, db: Database, chat_id: int, user_id: int) -> None:
    async with user_lock(chat_id, user_id):
        p = await db.get_pending(chat_id, user_id)
        if p is None or p.deadline > int(time.time()):
            return
        chat = await db.get_or_create_chat(chat_id)
        await _fail(bot, db, chat, p)


# ---------------------------------------------------------------- watchdog


async def watchdog(bot: Bot, db: Database, interval: float = 3.0) -> None:
    """Fails expired captchas and removes scheduled messages. Survives restarts
    because all state lives in the database."""
    while True:
        try:
            now = int(time.time())
            for p in await db.expired_pending(now):
                await expire(bot, db, p.chat_id, p.user_id)
            for chat_id, message_id in await db.pop_due_deletes(now):
                await safe_delete(bot, chat_id, message_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("watchdog iteration failed")
        await asyncio.sleep(interval)
