# axbot — Telegram anti-spam captcha bot
# Copyright (C) 2026 Axawys
# SPDX-License-Identifier: GPL-3.0-or-later
"""Management commands sent right in the group by the bot's owner/managers."""

from __future__ import annotations

import time
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.deep_linking import create_start_link

from .. import service, stats
from ..common import (
    ANONYMOUS_ADMIN_ID,
    ATTEMPTS_MAX,
    ATTEMPTS_MIN,
    GROUP_TYPES,
    TIMEOUT_MAX,
    TIMEOUT_MIN,
    can_manage,
    fmt_duration,
    full_name,
    is_owner,
    mention,
    settings_summary,
)
from ..config import Config
from ..db import Database
from ..questions import MODES, THEMES, get_theme

router = Router(name="group_admin")
router.message.filter(F.chat.type.in_(GROUP_TYPES))

REPLY_TTL = 60  # command + bot reply are removed after this many seconds
STATS_TTL = 600  # statistics stay a bit longer


GROUP_HELP = """🐧 <b>Команды axbot в чате</b> (для владельца и менеджеров бота)

/settings — текущие настройки и ссылка на панель
/captcha on|off — включить / выключить капчу
/timeout 120 — время на ответ, сек ({tmin}–{tmax})
/attempts 2 — число попыток ({amin}–{amax})
/action kick|ban — что делать с не прошедшими
/theme universal|linux — тематика вопросов
/mode mixed|choice|order — тип вопросов
/greeting текст — своё приветствие (или ответом на сообщение)
/greeting reset — вернуть стандартное, /greeting — показать текущее
/greetttl 300 — удалять приветствие через N сек (0 — не удалять)
/clean on|off — удалять сообщения «вступил / вышел»
/pass — пропустить новичка без капчи (ответом на его капчу или сообщение, либо ID)
/stats — график неудачных попыток входа за 7 дней
/stats 30 или /stats 01.10.2026 31.10.2026 — за выбранный период
/managers, /addmanager, /delmanager — менеджеры (ответом на сообщение или ID; только владелец)

В приветствии работают подстановки <code>{{mention}}</code>, <code>{{name}}</code>, <code>{{chat}}</code>."""


async def _reply(message: Message, db: Database, text: str, **kwargs) -> None:
    """Reply, then clean up both messages after REPLY_TTL."""
    try:
        sent = await message.reply(text, **kwargs)
    except TelegramAPIError:
        return
    delete_at = int(time.time()) + REPLY_TTL
    await db.schedule_delete(message.chat.id, message.message_id, delete_at)
    await db.schedule_delete(sent.chat.id, sent.message_id, delete_at)


async def _authorized(message: Message, bot: Bot, db: Database, config: Config, owner_only: bool = False) -> bool:
    if message.sender_chat is not None or (message.from_user and message.from_user.id == ANONYMOUS_ADMIN_ID):
        await _reply(message, db, "🕶 Не вижу, кто ты: отключи анонимность администратора, чтобы управлять ботом.")
        return False
    user_id = message.from_user.id
    check = is_owner if owner_only else can_manage
    if await check(bot, db, config, message.chat.id, user_id):
        await db.get_or_create_chat(message.chat.id, message.chat.title or "")
        return True
    # Strangers poking admin commands: silently remove the command.
    try:
        await message.delete()
    except TelegramAPIError:
        pass
    return False


def _parse_bool(arg: str | None) -> bool | None:
    if arg is None:
        return None
    arg = arg.strip().lower()
    if arg in ("on", "1", "yes", "true", "вкл", "да"):
        return True
    if arg in ("off", "0", "no", "false", "выкл", "нет"):
        return False
    return None


def _parse_int(arg: str | None, lo: int, hi: int) -> int | None:
    try:
        value = int((arg or "").strip())
    except ValueError:
        return None
    return value if lo <= value <= hi else None


def _target_user(message: Message, command: CommandObject) -> tuple[int, str] | None:
    """User from a reply or a numeric argument."""
    reply = message.reply_to_message
    if reply is not None and reply.from_user is not None:
        if reply.new_chat_members:
            u = reply.new_chat_members[0]
            return u.id, full_name(u)
        return reply.from_user.id, full_name(reply.from_user)
    if command.args and command.args.strip().lstrip("-").isdigit():
        uid = int(command.args.strip())
        return uid, str(uid)
    return None


# ------------------------------------------------------------------ commands


@router.message(Command("settings", "axbot"))
async def cmd_settings(message: Message, bot: Bot, db: Database, config: Config) -> None:
    if not await _authorized(message, bot, db, config):
        return
    chat = await db.get_chat(message.chat.id)
    link = await create_start_link(bot, f"chat_{message.chat.id}")
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⚙️ Открыть панель", url=link)]])
    await _reply(message, db, settings_summary(chat) + "\n\nСправка: /help", reply_markup=kb)


@router.message(Command("help"))
async def cmd_help(message: Message, bot: Bot, db: Database, config: Config) -> None:
    if not await _authorized(message, bot, db, config):
        return
    await _reply(
        message, db,
        GROUP_HELP.format(tmin=TIMEOUT_MIN, tmax=TIMEOUT_MAX, amin=ATTEMPTS_MIN, amax=ATTEMPTS_MAX),
    )


@router.message(Command("captcha"))
async def cmd_captcha(message: Message, command: CommandObject, bot: Bot, db: Database, config: Config) -> None:
    if not await _authorized(message, bot, db, config):
        return
    value = _parse_bool(command.args)
    if value is None:
        await _reply(message, db, "Использование: <code>/captcha on</code> или <code>/captcha off</code>")
        return
    await db.update_setting(message.chat.id, "enabled", value)
    await _reply(message, db, "✅ Капча включена." if value else "❌ Капча выключена. Двери открыты всем 😬")


@router.message(Command("timeout"))
async def cmd_timeout(message: Message, command: CommandObject, bot: Bot, db: Database, config: Config) -> None:
    if not await _authorized(message, bot, db, config):
        return
    value = _parse_int(command.args, TIMEOUT_MIN, TIMEOUT_MAX)
    if value is None:
        await _reply(message, db, f"Использование: <code>/timeout 120</code> (от {TIMEOUT_MIN} до {TIMEOUT_MAX} сек)")
        return
    await db.update_setting(message.chat.id, "timeout", value)
    await _reply(message, db, f"⏳ Время на ответ: {fmt_duration(value)}.")


@router.message(Command("attempts"))
async def cmd_attempts(message: Message, command: CommandObject, bot: Bot, db: Database, config: Config) -> None:
    if not await _authorized(message, bot, db, config):
        return
    value = _parse_int(command.args, ATTEMPTS_MIN, ATTEMPTS_MAX)
    if value is None:
        await _reply(message, db, f"Использование: <code>/attempts 2</code> (от {ATTEMPTS_MIN} до {ATTEMPTS_MAX})")
        return
    await db.update_setting(message.chat.id, "attempts", value)
    await _reply(message, db, f"🔁 Попыток: {value}.")


@router.message(Command("action"))
async def cmd_action(message: Message, command: CommandObject, bot: Bot, db: Database, config: Config) -> None:
    if not await _authorized(message, bot, db, config):
        return
    value = (command.args or "").strip().lower()
    if value not in ("kick", "ban"):
        await _reply(message, db, "Использование: <code>/action kick</code> или <code>/action ban</code>")
        return
    await db.update_setting(message.chat.id, "fail_action", value)
    await _reply(message, db, "👢 Не прошедших — кикаю." if value == "kick" else "🔨 Не прошедших — баню навсегда.")


@router.message(Command("mode"))
async def cmd_mode(message: Message, command: CommandObject, bot: Bot, db: Database, config: Config) -> None:
    if not await _authorized(message, bot, db, config):
        return
    value = (command.args or "").strip().lower()
    if value not in MODES:
        await _reply(message, db, "Использование: <code>/mode mixed</code>, <code>/mode choice</code> или <code>/mode order</code>")
        return
    await db.update_setting(message.chat.id, "mode", value)
    await _reply(message, db, f"🎲 Тип вопросов: {MODES[value]}.")


@router.message(Command("theme"))
async def cmd_theme(message: Message, command: CommandObject, bot: Bot, db: Database, config: Config) -> None:
    if not await _authorized(message, bot, db, config):
        return
    value = (command.args or "").strip().lower()
    if value not in THEMES:
        options = "\n".join(f"<code>/theme {key}</code> — {t.title}" for key, t in THEMES.items())
        chat = await db.get_chat(message.chat.id)
        await _reply(message, db, f"Сейчас: {get_theme(chat.theme).title}\n\nДоступные тематики:\n{options}")
        return
    await db.update_setting(message.chat.id, "theme", value)
    await _reply(message, db, f"🎨 Тематика вопросов: {THEMES[value].title}.")


@router.message(Command("greeting"))
async def cmd_greeting(message: Message, command: CommandObject, bot: Bot, db: Database, config: Config) -> None:
    if not await _authorized(message, bot, db, config):
        return
    chat_id = message.chat.id
    args = (command.args or "").strip()

    if args.lower() == "reset":
        await db.update_setting(chat_id, "greeting", None)
        await _reply(message, db, "↩️ Вернул стандартное приветствие.")
        return

    new_text: str | None = None
    if args:
        # Keep formatting: take the HTML of the message and cut the command off.
        html = message.html_text
        new_text = html.split(maxsplit=1)[1] if len(html.split(maxsplit=1)) > 1 else args
    elif message.reply_to_message and (message.reply_to_message.text or message.reply_to_message.caption):
        new_text = message.reply_to_message.html_text

    if new_text is None:
        chat = await db.get_chat(chat_id)
        current = chat.greeting or get_theme(chat.theme).greeting
        await _reply(
            message, db,
            "💬 Текущее приветствие (исходник):\n\n"
            f"<pre>{escape(current)}</pre>\n\n"
            "Изменить: <code>/greeting текст</code> или ответом на сообщение. "
            "Подстановки: <code>{mention}</code>, <code>{name}</code>, <code>{chat}</code>.",
        )
        return

    if len(new_text) > 3000:
        await _reply(message, db, "Слишком длинно — максимум 3000 символов.")
        return
    await db.update_setting(chat_id, "greeting", new_text)
    chat = await db.get_chat(chat_id)
    preview = service.render_greeting(chat, new_text, message.from_user.id, full_name(message.from_user))
    await _reply(message, db, "✅ Приветствие сохранено. Так оно будет выглядеть:\n\n" + preview)


@router.message(Command("greetttl"))
async def cmd_greetttl(message: Message, command: CommandObject, bot: Bot, db: Database, config: Config) -> None:
    if not await _authorized(message, bot, db, config):
        return
    value = _parse_int(command.args, 0, 86400)
    if value is None:
        await _reply(message, db, "Использование: <code>/greetttl 300</code> (0 — не удалять)")
        return
    await db.update_setting(message.chat.id, "greeting_ttl", value)
    await _reply(message, db, f"🧹 Автоудаление приветствия: {fmt_duration(value)}.")


@router.message(Command("clean"))
async def cmd_clean(message: Message, command: CommandObject, bot: Bot, db: Database, config: Config) -> None:
    if not await _authorized(message, bot, db, config):
        return
    value = _parse_bool(command.args)
    if value is None:
        await _reply(message, db, "Использование: <code>/clean on</code> или <code>/clean off</code>")
        return
    await db.update_setting(message.chat.id, "clean_service", value)
    await _reply(message, db, f"🧹 Удаление «вступил / вышел»: {'включено' if value else 'выключено'}.")


@router.message(Command("pass"))
async def cmd_pass(message: Message, command: CommandObject, bot: Bot, db: Database, config: Config) -> None:
    if not await _authorized(message, bot, db, config):
        return
    user_id: int | None = None
    reply = message.reply_to_message
    if reply is not None and reply.from_user is not None and reply.from_user.id == bot.id:
        p = await db.get_pending_by_message(message.chat.id, reply.message_id)
        user_id = p.user_id if p else None
    else:
        target = _target_user(message, command)
        user_id = target[0] if target else None

    if user_id is None:
        await _reply(message, db, "Ответь этой командой на капчу новичка или укажи его ID.")
        return
    if await service.approve(bot, db, message.chat.id, user_id):
        await _reply(message, db, "✅ Пропустил без проверки.")
    else:
        await _reply(message, db, "У этого пользователя нет активной проверки.")


@router.message(Command("stats"))
async def cmd_stats(message: Message, command: CommandObject, bot: Bot, db: Database, config: Config) -> None:
    if not await _authorized(message, bot, db, config):
        return
    try:
        png, caption = await stats.build_report(
            db, message.chat.id, message.chat.title or "", command.args, config.timezone
        )
    except stats.RangeError as e:
        await _reply(message, db, f"{e}\n\n{stats.RANGE_HELP}")
        return
    try:
        sent = await message.reply_photo(BufferedInputFile(png, "stats.png"), caption=caption)
    except TelegramAPIError:
        return
    delete_at = int(time.time()) + STATS_TTL
    await db.schedule_delete(message.chat.id, message.message_id, delete_at)
    await db.schedule_delete(sent.chat.id, sent.message_id, delete_at)


@router.message(Command("managers"))
async def cmd_managers(message: Message, bot: Bot, db: Database, config: Config) -> None:
    if not await _authorized(message, bot, db, config):
        return
    chat = await db.get_chat(message.chat.id)
    lines = ["👥 <b>Кто управляет ботом</b>"]
    if chat.owner_id:
        lines.append(f"Владелец: {mention(chat.owner_id, await _name(bot, message.chat.id, chat.owner_id))}")
    managers = await db.list_managers(message.chat.id)
    if managers:
        for uid in managers:
            lines.append(f"• {mention(uid, await _name(bot, message.chat.id, uid))}")
    else:
        lines.append("Менеджеров нет.")
    lines.append("\nСоздатель чата всегда может управлять ботом.")
    await _reply(message, db, "\n".join(lines))


@router.message(Command("addmanager"))
async def cmd_addmanager(message: Message, command: CommandObject, bot: Bot, db: Database, config: Config) -> None:
    if not await _authorized(message, bot, db, config, owner_only=True):
        return
    target = _target_user(message, command)
    if target is None:
        await _reply(message, db, "Ответь командой на сообщение человека или укажи его ID.")
        return
    await db.add_manager(message.chat.id, target[0])
    await _reply(message, db, f"✅ {mention(*target)} теперь может управлять ботом.")


@router.message(Command("delmanager"))
async def cmd_delmanager(message: Message, command: CommandObject, bot: Bot, db: Database, config: Config) -> None:
    if not await _authorized(message, bot, db, config, owner_only=True):
        return
    target = _target_user(message, command)
    if target is None:
        await _reply(message, db, "Ответь командой на сообщение человека или укажи его ID.")
        return
    removed = await db.remove_manager(message.chat.id, target[0])
    await _reply(message, db, f"🗑 {mention(*target)} больше не менеджер." if removed else "Он и не был менеджером.")


async def _name(bot: Bot, chat_id: int, user_id: int) -> str:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return full_name(member.user)
    except TelegramAPIError:
        return str(user_id)
