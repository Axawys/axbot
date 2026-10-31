# axbot — Telegram anti-spam captcha bot
# Copyright (C) 2026 Axawys
# SPDX-License-Identifier: 0BSD
"""Chat events: bot added/removed, users joining/leaving, captcha buttons."""

from __future__ import annotations

import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import JOIN_TRANSITION, LEAVE_TRANSITION, ChatMemberUpdatedFilter
from aiogram.types import (
    CallbackQuery,
    ChatMemberAdministrator,
    ChatMemberRestricted,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.deep_linking import create_start_link

from .. import service
from ..common import GROUP_TYPES, Cap, full_name, is_chat_admin
from ..config import Config
from ..db import Database
from ..questions import get_theme

log = logging.getLogger(__name__)
router = Router(name="events")

REQUIRED_RIGHTS = "«Блокировка пользователей» и «Удаление сообщений»"


def _missing_rights(member) -> list[str]:
    if not isinstance(member, ChatMemberAdministrator):
        return ["права администратора"]
    missing = []
    if not member.can_restrict_members:
        missing.append("«Блокировка пользователей»")
    if not member.can_delete_messages:
        missing.append("«Удаление сообщений»")
    return missing


# ------------------------------------------------------------ bot membership


@router.my_chat_member(F.chat.type.in_(GROUP_TYPES), ChatMemberUpdatedFilter(JOIN_TRANSITION))
async def bot_added(event: ChatMemberUpdated, bot: Bot, db: Database, config: Config) -> None:
    chat = event.chat
    adder = event.from_user
    settings = await db.get_or_create_chat(chat.id, chat.title or "")
    await db.set_chat_active(chat.id, True)

    # Whoever added the bot becomes its owner in this chat. To avoid hijacking
    # by a random member re-adding the bot, an existing owner is only replaced
    # by a chat admin.
    if settings.owner_id is None or settings.owner_id == adder.id or await is_chat_admin(bot, chat.id, adder.id):
        await db.set_owner(chat.id, adder.id)
        owner_note = f"Управляет мной {escape(full_name(adder))} — тот, кто меня добавил."
    else:
        owner_note = "Владелец настроек остался прежним."

    panel_link = await create_start_link(bot, f"chat_{chat.id}")
    missing = _missing_rights(event.new_chat_member)
    rights_note = (
        f"\n\n⚠️ Чтобы я работал, сделайте меня администратором с правами {REQUIRED_RIGHTS}."
        if missing
        else "\n\n✅ Права администратора есть — капча уже работает."
    )
    text = (
        "👋 Привет! Я <b>axbot</b>, защищаю чат от спам-ботов.\n"
        "Каждый новичок будет отвечать на простой вопрос на логику, а боты останутся за дверью.\n\n"
        f"Тематика вопросов: {get_theme(settings.theme).title}. Её можно сменить в настройках.\n\n"
        f"{owner_note}{rights_note}\n\n"
        "Настройки — в личке со мной или командой /settings в этом чате."
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⚙️ Открыть настройки", url=panel_link)]]
    )
    try:
        await bot.send_message(chat.id, text, reply_markup=kb)
    except TelegramAPIError as e:
        log.warning("cannot greet chat %s: %s", chat.id, e)

    try:
        await bot.send_message(
            adder.id,
            f"Ты добавил меня в <b>{escape(chat.title or '')}</b>. Здесь можно меня настроить 👇",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⚙️ Настройки", url=panel_link)]]
            ),
        )
    except TelegramAPIError:
        pass  # the adder never started the bot in private — that's fine


@router.my_chat_member(F.chat.type.in_(GROUP_TYPES), ChatMemberUpdatedFilter(LEAVE_TRANSITION))
async def bot_removed(event: ChatMemberUpdated, db: Database) -> None:
    await db.set_chat_active(event.chat.id, False)
    await db.clear_pending_for_chat(event.chat.id)
    log.info("removed from chat %s", event.chat.id)


@router.my_chat_member(F.chat.type.in_(GROUP_TYPES))
async def bot_rights_changed(event: ChatMemberUpdated, bot: Bot, db: Database) -> None:
    """Bot promoted / demoted while already in the chat."""
    await db.get_or_create_chat(event.chat.id, event.chat.title or "")
    was_ok = not _missing_rights(event.old_chat_member)
    missing = _missing_rights(event.new_chat_member)
    if missing:
        text = "⚠️ Мне не хватает прав: " + ", ".join(missing) + ". Капча не сможет работать."
    elif not was_ok:
        text = "✅ Права получены — теперь каждый новичок пройдёт проверку."
    else:
        return
    try:
        await bot.send_message(event.chat.id, text)
    except TelegramAPIError:
        pass


@router.message(F.migrate_to_chat_id)
async def chat_migrated(message: Message, db: Database) -> None:
    await db.migrate_chat(message.chat.id, message.migrate_to_chat_id)
    log.info("chat %s migrated to %s", message.chat.id, message.migrate_to_chat_id)


# ----------------------------------------------------------- users joining


@router.chat_member(F.chat.type.in_(GROUP_TYPES), ChatMemberUpdatedFilter(JOIN_TRANSITION))
async def user_joined(event: ChatMemberUpdated, bot: Bot, db: Database) -> None:
    chat = await db.get_or_create_chat(event.chat.id, event.chat.title or "")
    if not chat.enabled or not chat.active:
        return

    new = event.new_chat_member
    user = new.user
    if user.id == bot.id or new.status in ("creator", "administrator"):
        return

    # Added by an admin (not via invite link by themselves) → trust them.
    inviter = event.from_user
    added_by_admin = inviter.id != user.id and await is_chat_admin(bot, chat.chat_id, inviter.id)

    if user.is_bot:
        if not added_by_admin:
            log.info("kicking bot %s added by %s in %s", user.id, inviter.id, chat.chat_id)
            try:
                await bot.ban_chat_member(chat.chat_id, user.id)
            except TelegramAPIError as e:
                log.warning("cannot kick bot %s: %s", user.id, e)
            else:
                await db.log_event(chat.chat_id, user.id, "bot_kicked")
        return

    if added_by_admin:
        return

    old_restriction = new if isinstance(new, ChatMemberRestricted) else None
    await service.start_captcha(bot, db, chat, user, old_restriction)


@router.chat_member(F.chat.type.in_(GROUP_TYPES), ChatMemberUpdatedFilter(LEAVE_TRANSITION))
async def user_left(event: ChatMemberUpdated, bot: Bot, db: Database) -> None:
    await service.cancel(bot, db, event.chat.id, event.new_chat_member.user.id)


# -------------------------------------------------------- service messages


@router.message(F.chat.type.in_(GROUP_TYPES), F.new_chat_members | F.left_chat_member)
async def service_message(message: Message, db: Database) -> None:
    chat = await db.get_chat(message.chat.id)
    if chat is None or chat.clean_service:
        try:
            await message.delete()
        except TelegramAPIError:
            pass


async def _is_pending(message: Message, db: Database) -> bool:
    if message.from_user is None:
        return False
    return await db.get_pending(message.chat.id, message.from_user.id) is not None


@router.message(F.chat.type.in_(GROUP_TYPES), _is_pending)
async def pending_user_message(message: Message) -> None:
    """Fallback for chats where the bot couldn't mute the newcomer."""
    try:
        await message.delete()
    except TelegramAPIError:
        pass


# ----------------------------------------------------------------- buttons


@router.callback_query(Cap.filter(), F.message.chat.type.in_(GROUP_TYPES))
async def captcha_button(callback: CallbackQuery, callback_data: Cap, bot: Bot, db: Database) -> None:
    await service.handle_answer(bot, db, callback, callback_data)
