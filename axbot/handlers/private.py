# axbot — Telegram anti-spam captcha bot
# Copyright (C) 2026 Axawys
# SPDX-License-Identifier: GPL-3.0-or-later
"""Settings panel in the private chat with the bot."""

from __future__ import annotations

from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, Message, MessageOriginUser
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .. import service, stats
from ..common import (
    ATTEMPTS_MAX,
    ATTEMPTS_MIN,
    GREETING_TTL_PRESETS,
    TIMEOUT_PRESETS,
    Panel,
    Try,
    can_manage,
    fmt_duration,
    full_name,
    is_owner,
    mention,
    settings_summary,
    yes_no,
)
from ..config import Config
from ..db import ChatSettings, Database
from ..questions import MODES, QUESTIONS, THEMES, get_theme, pick_question

router = Router(name="private")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")


class Input(StatesGroup):
    greeting = State()
    manager = State()
    stats_range = State()


START_TEXT = """👋 Привет! Я <b>axbot</b>, защищаю Telegram-чаты от спам-ботов.

Каждый новичок в чате отвечает на простой вопрос на логику. Ответил — добро пожаловать. Не ответил вовремя — до свидания, спам-бот.

Тематики вопросов: {themes}. Выбрать можно в настройках чата.

<b>Как подключить:</b>
1. Добавь меня в группу (кнопка ниже).
2. Сделай администратором с правами «Блокировка пользователей» и «Удаление сообщений».
3. Готово! Кто меня добавил — тот и управляет мной: здесь, в личке, или командами в чате (/help в группе).

Исходный код: GPLv3."""


def _add_to_group_url(username: str) -> str:
    return f"https://t.me/{username}?startgroup=axbot&admin=restrict_members+delete_messages"


# --------------------------------------------------------------- keyboards


def panel_keyboard(chat: ChatSettings) -> InlineKeyboardMarkup:
    cid = chat.chat_id
    kb = InlineKeyboardBuilder()
    kb.button(
        text=f"Капча: {'✅ вкл' if chat.enabled else '❌ выкл'}",
        callback_data=Panel(action="toggle", chat_id=cid, value="enabled"),
    )
    kb.button(text=f"⏳ Время: {fmt_duration(chat.timeout)}", callback_data=Panel(action="timeout", chat_id=cid))
    kb.button(text=f"🔁 Попыток: {chat.attempts}", callback_data=Panel(action="cycle", chat_id=cid, value="attempts"))
    kb.button(
        text=f"При провале: {'👢 кик' if chat.fail_action == 'kick' else '🔨 бан'}",
        callback_data=Panel(action="toggle", chat_id=cid, value="fail_action"),
    )
    kb.button(
        text=f"Тематика: {get_theme(chat.theme).title}",
        callback_data=Panel(action="cycle", chat_id=cid, value="theme"),
    )
    kb.button(text=f"Вопросы: {MODES.get(chat.mode, chat.mode)}", callback_data=Panel(action="cycle", chat_id=cid, value="mode"))
    kb.button(
        text=f"🧹 Чистить «вступил/вышел»: {yes_no(chat.clean_service)}",
        callback_data=Panel(action="toggle", chat_id=cid, value="clean_service"),
    )
    kb.button(text="💬 Приветствие", callback_data=Panel(action="greet", chat_id=cid))
    kb.button(text="👥 Менеджеры", callback_data=Panel(action="mgr", chat_id=cid))
    kb.button(text="📊 Статистика", callback_data=Panel(action="stats", chat_id=cid))
    kb.button(text="🧪 Попробовать капчу", callback_data=Panel(action="try", chat_id=cid, value=f"{chat.theme},{chat.mode}"))
    kb.button(text="« Мои чаты", callback_data=Panel(action="list"))
    kb.adjust(1, 2, 1, 2, 1, 2, 2, 1)
    return kb.as_markup()


def back_button(kb: InlineKeyboardBuilder, chat_id: int) -> None:
    kb.button(text="« Назад", callback_data=Panel(action="open", chat_id=chat_id))


# ----------------------------------------------------------------- helpers


async def _show(target: Message | CallbackQuery, text: str, markup: InlineKeyboardMarkup | None) -> None:
    """Edit the panel message when coming from a button, otherwise send a new one."""
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                await target.message.answer(text, reply_markup=markup, disable_web_page_preview=True)
    else:
        await target.answer(text, reply_markup=markup, disable_web_page_preview=True)


async def show_chat_list(target: Message | CallbackQuery, bot: Bot, db: Database, config: Config) -> None:
    user_id = target.from_user.id
    chats = await db.chats_for_user(user_id, all_chats=user_id in config.superadmins)
    me = await bot.me()
    kb = InlineKeyboardBuilder()
    for chat in chats:
        kb.button(text=f"💬 {chat.title or chat.chat_id}", callback_data=Panel(action="open", chat_id=chat.chat_id))
    kb.button(text="➕ Добавить в группу", url=_add_to_group_url(me.username))
    kb.adjust(1)
    if chats:
        text = "Выбери чат для настройки:"
    else:
        text = (
            "У тебя пока нет чатов со мной.\n\n"
            "Добавь меня в группу кнопкой ниже — тот, кто добавил, становится моим владельцем в этом чате. "
            "Если чат уже есть, но его нет в списке — набери /settings прямо в группе."
        )
    await _show(target, text, kb.as_markup())


async def show_panel(target: Message | CallbackQuery, bot: Bot, db: Database, config: Config, chat_id: int) -> None:
    chat = await db.get_chat(chat_id)
    if chat is None:
        await _show(target, "Не знаю такой чат. Добавь меня туда заново.", None)
        return
    text = settings_summary(chat)
    if not chat.active:
        text += "\n\n⚠️ Меня нет в этом чате — настройки сохранятся до возвращения."
    await _show(target, text, panel_keyboard(chat))


async def _check(callback: CallbackQuery, bot: Bot, db: Database, config: Config, chat_id: int, owner_only=False) -> bool:
    check = is_owner if owner_only else can_manage
    if await check(bot, db, config, chat_id, callback.from_user.id):
        return True
    await callback.answer("⛔ Нет доступа к этому чату.", show_alert=True)
    return False


# ---------------------------------------------------------------- commands


@router.message(CommandStart(deep_link=True))
async def start_deeplink(message: Message, command: CommandObject, state: FSMContext, bot: Bot, db: Database, config: Config) -> None:
    await state.clear()
    payload = command.args or ""
    if payload.startswith("chat_"):
        try:
            chat_id = int(payload.removeprefix("chat_"))
        except ValueError:
            chat_id = 0
        if chat_id and await can_manage(bot, db, config, chat_id, message.from_user.id):
            await show_panel(message, bot, db, config, chat_id)
            return
        await message.answer("⛔ Управлять ботом в этом чате может только тот, кто его добавил, или назначенные им менеджеры.")
        return
    await cmd_start(message, state, bot, db, config)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, bot: Bot, db: Database, config: Config) -> None:
    await state.clear()
    me = await bot.me()
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить в группу", url=_add_to_group_url(me.username))
    kb.button(text="⚙️ Мои чаты", callback_data=Panel(action="list"))
    for key, theme in THEMES.items():
        kb.button(text=f"🧪 Попробовать: {theme.title}", callback_data=Panel(action="try", value=f"{key},mixed"))
    kb.adjust(1)
    themes = ", ".join(t.title for t in THEMES.values())
    await message.answer(START_TEXT.format(themes=themes), reply_markup=kb.as_markup())


@router.message(Command("chats", "settings"))
async def cmd_chats(message: Message, state: FSMContext, bot: Bot, db: Database, config: Config) -> None:
    await state.clear()
    await show_chat_list(message, bot, db, config)


@router.message(Command("help"))
async def cmd_help(message: Message, state: FSMContext, bot: Bot, db: Database, config: Config) -> None:
    await cmd_start(message, state, bot, db, config)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.")


# ------------------------------------------------------------ panel buttons


@router.callback_query(Panel.filter(F.action == "list"))
async def cb_list(callback: CallbackQuery, state: FSMContext, bot: Bot, db: Database, config: Config) -> None:
    await state.clear()
    await show_chat_list(callback, bot, db, config)
    await callback.answer()


@router.callback_query(Panel.filter(F.action == "open"))
async def cb_open(callback: CallbackQuery, callback_data: Panel, state: FSMContext, bot: Bot, db: Database, config: Config) -> None:
    if not await _check(callback, bot, db, config, callback_data.chat_id):
        return
    await state.clear()
    await show_panel(callback, bot, db, config, callback_data.chat_id)
    await callback.answer()


@router.callback_query(Panel.filter(F.action == "toggle"))
async def cb_toggle(callback: CallbackQuery, callback_data: Panel, bot: Bot, db: Database, config: Config) -> None:
    cid = callback_data.chat_id
    if not await _check(callback, bot, db, config, cid):
        return
    chat = await db.get_chat(cid)
    key = callback_data.value
    if key == "enabled":
        await db.update_setting(cid, key, not chat.enabled)
    elif key == "clean_service":
        await db.update_setting(cid, key, not chat.clean_service)
    elif key == "fail_action":
        await db.update_setting(cid, key, "ban" if chat.fail_action == "kick" else "kick")
    await show_panel(callback, bot, db, config, cid)
    await callback.answer()


@router.callback_query(Panel.filter(F.action == "cycle"))
async def cb_cycle(callback: CallbackQuery, callback_data: Panel, bot: Bot, db: Database, config: Config) -> None:
    cid = callback_data.chat_id
    if not await _check(callback, bot, db, config, cid):
        return
    chat = await db.get_chat(cid)
    if callback_data.value == "attempts":
        nxt = chat.attempts + 1 if chat.attempts < ATTEMPTS_MAX else ATTEMPTS_MIN
        await db.update_setting(cid, "attempts", nxt)
    elif callback_data.value == "theme":
        keys = list(THEMES)
        nxt = keys[(keys.index(chat.theme) + 1) % len(keys)] if chat.theme in keys else keys[0]
        await db.update_setting(cid, "theme", nxt)
    elif callback_data.value == "mode":
        modes = list(MODES)
        nxt = modes[(modes.index(chat.mode) + 1) % len(modes)] if chat.mode in modes else modes[0]
        await db.update_setting(cid, "mode", nxt)
    await show_panel(callback, bot, db, config, cid)
    await callback.answer()


@router.callback_query(Panel.filter(F.action == "timeout"))
async def cb_timeout(callback: CallbackQuery, callback_data: Panel, bot: Bot, db: Database, config: Config) -> None:
    cid = callback_data.chat_id
    if not await _check(callback, bot, db, config, cid):
        return
    if callback_data.value:
        await db.update_setting(cid, "timeout", int(callback_data.value))
        await show_panel(callback, bot, db, config, cid)
        await callback.answer("Сохранено")
        return
    chat = await db.get_chat(cid)
    kb = InlineKeyboardBuilder()
    for secs in TIMEOUT_PRESETS:
        mark = "• " if secs == chat.timeout else ""
        kb.button(text=f"{mark}{fmt_duration(secs)}", callback_data=Panel(action="timeout", chat_id=cid, value=str(secs)))
    back_button(kb, cid)
    kb.adjust(3, 3, 1)
    await _show(
        callback,
        "⏳ Сколько времени давать новичку на ответ?\n\nЛюбое значение от 30 до 3600 сек можно задать "
        "командой <code>/timeout N</code> в чате.",
        kb.as_markup(),
    )
    await callback.answer()


# ---------------------------------------------------------------- greeting


async def show_greeting_menu(target, db: Database, cid: int) -> None:
    chat = await db.get_chat(cid)
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Изменить", callback_data=Panel(action="greet_edit", chat_id=cid))
    kb.button(text="↩️ Стандартное", callback_data=Panel(action="greet_reset", chat_id=cid))
    kb.button(
        text=f"🧹 Автоудаление: {fmt_duration(chat.greeting_ttl)}",
        callback_data=Panel(action="greet_ttl", chat_id=cid),
    )
    back_button(kb, cid)
    kb.adjust(2, 1, 1)
    me = target.from_user
    preview = service.render_greeting(chat, chat.greeting, me.id, full_name(me))
    await _show(
        target,
        f"💬 <b>Приветствие</b> ({'своё' if chat.greeting else 'стандартное'})\n\n"
        f"Так оно выглядит:\n\n{preview}",
        kb.as_markup(),
    )


@router.callback_query(Panel.filter(F.action == "greet"))
async def cb_greet(callback: CallbackQuery, callback_data: Panel, state: FSMContext, bot: Bot, db: Database, config: Config) -> None:
    if not await _check(callback, bot, db, config, callback_data.chat_id):
        return
    await state.clear()
    await show_greeting_menu(callback, db, callback_data.chat_id)
    await callback.answer()


@router.callback_query(Panel.filter(F.action == "greet_reset"))
async def cb_greet_reset(callback: CallbackQuery, callback_data: Panel, bot: Bot, db: Database, config: Config) -> None:
    if not await _check(callback, bot, db, config, callback_data.chat_id):
        return
    await db.update_setting(callback_data.chat_id, "greeting", None)
    await show_greeting_menu(callback, db, callback_data.chat_id)
    await callback.answer("Вернул стандартное")


@router.callback_query(Panel.filter(F.action == "greet_ttl"))
async def cb_greet_ttl(callback: CallbackQuery, callback_data: Panel, bot: Bot, db: Database, config: Config) -> None:
    cid = callback_data.chat_id
    if not await _check(callback, bot, db, config, cid):
        return
    chat = await db.get_chat(cid)
    presets = GREETING_TTL_PRESETS
    idx = presets.index(chat.greeting_ttl) if chat.greeting_ttl in presets else -1
    await db.update_setting(cid, "greeting_ttl", presets[(idx + 1) % len(presets)])
    await show_greeting_menu(callback, db, cid)
    await callback.answer()


@router.callback_query(Panel.filter(F.action == "greet_edit"))
async def cb_greet_edit(callback: CallbackQuery, callback_data: Panel, state: FSMContext, bot: Bot, db: Database, config: Config) -> None:
    if not await _check(callback, bot, db, config, callback_data.chat_id):
        return
    chat = await db.get_chat(callback_data.chat_id)
    await state.set_state(Input.greeting)
    await state.update_data(chat_id=callback_data.chat_id)
    kb = InlineKeyboardBuilder()
    kb.button(text="Отмена", callback_data=Panel(action="greet", chat_id=callback_data.chat_id))
    await _show(
        callback,
        "✏️ Пришли новый текст приветствия. Форматирование (жирный, курсив, ссылки) сохранится.\n\n"
        "Подстановки:\n<code>{mention}</code> — ссылка на новичка\n<code>{name}</code> — его имя\n"
        "<code>{chat}</code> — название чата\n\n"
        f"Стандартное для примера:\n<pre>{escape(get_theme(chat.theme).greeting)}</pre>",
        kb.as_markup(),
    )
    await callback.answer()


@router.message(StateFilter(Input.greeting), F.text)
async def input_greeting(message: Message, state: FSMContext, bot: Bot, db: Database, config: Config) -> None:
    cid = (await state.get_data()).get("chat_id")
    if not cid or not await can_manage(bot, db, config, cid, message.from_user.id):
        await state.clear()
        return
    if len(message.html_text) > 3000:
        await message.answer("Слишком длинно — максимум 3000 символов. Попробуй короче или /cancel.")
        return
    await db.update_setting(cid, "greeting", message.html_text)
    await state.clear()
    await message.answer("✅ Приветствие сохранено.")
    await show_greeting_menu(message, db, cid)


# ---------------------------------------------------------------- managers


async def show_managers(target, bot: Bot, db: Database, config: Config, cid: int) -> None:
    chat = await db.get_chat(cid)
    owner = await is_owner(bot, db, config, cid, target.from_user.id)
    managers = await db.list_managers(cid)
    lines = ["👥 <b>Кто управляет ботом</b>\n"]
    if chat.owner_id:
        lines.append(f"Владелец (добавил бота): {mention(chat.owner_id, await _name(bot, cid, chat.owner_id))}")
    lines.append("Создатель чата — всегда.")
    lines.append("")
    lines.append("Менеджеры:" if managers else "Менеджеров пока нет.")
    kb = InlineKeyboardBuilder()
    for uid in managers:
        name = await _name(bot, cid, uid)
        lines.append(f"• {mention(uid, name)}")
        if owner:
            kb.button(text=f"🗑 {name}", callback_data=Panel(action="mgr_del", chat_id=cid, value=str(uid)))
    if owner:
        kb.button(text="➕ Добавить менеджера", callback_data=Panel(action="mgr_add", chat_id=cid))
    else:
        lines.append("\nДобавлять и убирать менеджеров может только владелец.")
    back_button(kb, cid)
    kb.adjust(1)
    await _show(target, "\n".join(lines), kb.as_markup())


async def _name(bot: Bot, chat_id: int, user_id: int) -> str:
    try:
        return full_name((await bot.get_chat_member(chat_id, user_id)).user)
    except TelegramAPIError:
        return str(user_id)


@router.callback_query(Panel.filter(F.action == "mgr"))
async def cb_mgr(callback: CallbackQuery, callback_data: Panel, state: FSMContext, bot: Bot, db: Database, config: Config) -> None:
    if not await _check(callback, bot, db, config, callback_data.chat_id):
        return
    await state.clear()
    await show_managers(callback, bot, db, config, callback_data.chat_id)
    await callback.answer()


@router.callback_query(Panel.filter(F.action == "mgr_del"))
async def cb_mgr_del(callback: CallbackQuery, callback_data: Panel, bot: Bot, db: Database, config: Config) -> None:
    cid = callback_data.chat_id
    if not await _check(callback, bot, db, config, cid, owner_only=True):
        return
    await db.remove_manager(cid, int(callback_data.value))
    await show_managers(callback, bot, db, config, cid)
    await callback.answer("Убрал")


@router.callback_query(Panel.filter(F.action == "mgr_add"))
async def cb_mgr_add(callback: CallbackQuery, callback_data: Panel, state: FSMContext, bot: Bot, db: Database, config: Config) -> None:
    cid = callback_data.chat_id
    if not await _check(callback, bot, db, config, cid, owner_only=True):
        return
    await state.set_state(Input.manager)
    await state.update_data(chat_id=cid)
    kb = InlineKeyboardBuilder()
    kb.button(text="Отмена", callback_data=Panel(action="mgr", chat_id=cid))
    await _show(
        callback,
        "➕ Перешли сюда любое сообщение будущего менеджера или пришли его числовой ID.\n\n"
        "Если у человека скрыт аккаунт при пересылке — проще ответить ему в чате командой /addmanager.",
        kb.as_markup(),
    )
    await callback.answer()


@router.message(StateFilter(Input.manager))
async def input_manager(message: Message, state: FSMContext, bot: Bot, db: Database, config: Config) -> None:
    cid = (await state.get_data()).get("chat_id")
    if not cid or not await is_owner(bot, db, config, cid, message.from_user.id):
        await state.clear()
        return
    uid: int | None = None
    if isinstance(message.forward_origin, MessageOriginUser):
        uid = message.forward_origin.sender_user.id
    elif message.text and message.text.strip().isdigit():
        uid = int(message.text.strip())
    if uid is None:
        await message.answer("Не вижу ID: аккаунт скрыт при пересылке. Пришли числовой ID или /cancel.")
        return
    await db.add_manager(cid, uid)
    await state.clear()
    await message.answer(f"✅ {mention(uid, await _name(bot, cid, uid))} теперь менеджер.")
    await show_managers(message, bot, db, config, cid)


# --------------------------------------------------------------- statistics

STATS_PRESETS = [("7", "7 дней"), ("30", "30 дней"), ("90", "90 дней"), ("365", "Год")]


def stats_keyboard(cid: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for value, label in STATS_PRESETS:
        kb.button(text=label, callback_data=Panel(action="stats", chat_id=cid, value=value))
    kb.button(text="📅 Свой период", callback_data=Panel(action="stats_custom", chat_id=cid))
    back_button(kb, cid)
    kb.adjust(4, 1, 1)
    return kb.as_markup()


async def send_stats(target: Message, db: Database, config: Config, cid: int, range_text: str) -> bool:
    chat = await db.get_chat(cid)
    try:
        png, caption = await stats.build_report(db, cid, chat.title, range_text, config.timezone)
    except stats.RangeError as e:
        await target.answer(f"{e}\n\n{stats.RANGE_HELP}\n\nИли /cancel.")
        return False
    await target.answer_photo(BufferedInputFile(png, "stats.png"), caption=caption, reply_markup=stats_keyboard(cid))
    return True


@router.callback_query(Panel.filter(F.action == "stats"))
async def cb_stats(callback: CallbackQuery, callback_data: Panel, state: FSMContext, bot: Bot, db: Database, config: Config) -> None:
    cid = callback_data.chat_id
    if not await _check(callback, bot, db, config, cid):
        return
    await state.clear()
    if not callback_data.value:
        await _show(callback, "📊 За какой период показать неудачные попытки входа?", stats_keyboard(cid))
        await callback.answer()
        return
    await callback.answer("Строю график…")
    await send_stats(callback.message, db, config, cid, callback_data.value)


@router.callback_query(Panel.filter(F.action == "stats_custom"))
async def cb_stats_custom(callback: CallbackQuery, callback_data: Panel, state: FSMContext, bot: Bot, db: Database, config: Config) -> None:
    cid = callback_data.chat_id
    if not await _check(callback, bot, db, config, cid):
        return
    await state.set_state(Input.stats_range)
    await state.update_data(chat_id=cid)
    await callback.message.answer("📅 Пришли период.\n\n" + stats.RANGE_HELP + "\n\nОтмена: /cancel")
    await callback.answer()


@router.message(StateFilter(Input.stats_range), F.text)
async def input_stats_range(message: Message, state: FSMContext, bot: Bot, db: Database, config: Config) -> None:
    cid = (await state.get_data()).get("chat_id")
    if not cid or not await can_manage(bot, db, config, cid, message.from_user.id):
        await state.clear()
        return
    if await send_stats(message, db, config, cid, message.text):
        await state.clear()


# ------------------------------------------------------------ test captcha


def _try_text(q, progress: int) -> str:
    text = f"🧪 <b>Тестовая капча</b> — так её увидят новички.\n\n❓ {q.text}"
    if q.is_order:
        text += "\n<i>Нажимай кнопки по порядку.</i>"
        if progress:
            text += "\nНабрано: " + " ".join(f"<code>{escape(q.options[i])}</code>" for i in q.answer[:progress])
    return text


@router.callback_query(Panel.filter(F.action == "try"))
async def cb_try(callback: CallbackQuery, callback_data: Panel) -> None:
    theme, _, mode = (callback_data.value or "").partition(",")
    q = pick_question(theme, mode or "mixed")
    kb = service.build_keyboard(q, lambda i: Try(qid=q.id, idx=i, progress=0).pack())
    await callback.message.answer(_try_text(q, 0), reply_markup=kb)
    await callback.answer()


@router.callback_query(Try.filter())
async def cb_try_answer(callback: CallbackQuery, callback_data: Try) -> None:
    q = QUESTIONS.get(callback_data.qid)
    if q is None or callback_data.idx < 0:
        await callback.answer()
        return
    progress = callback_data.progress
    if callback_data.idx != q.answer[progress]:
        await callback.message.edit_text(_try_text(q, progress) + "\n\n❌ Неверно! В чате это стоило бы попытки.")
        await callback.answer()
        return
    progress += 1
    if progress >= len(q.answer):
        await callback.message.edit_text(_try_text(q, progress) + "\n\n✅ Верно! Проверка пройдена.")
        await callback.answer("✅")
        return
    # Rebuild keyboard: mark the pressed button, bump progress in the rest.
    rows = []
    for row in callback.message.reply_markup.inline_keyboard:
        new_row = []
        for b in row:
            d = Try.unpack(b.callback_data)
            if b.callback_data == callback.data:
                new_row.append(b.model_copy(update={"text": f"✅ {b.text}", "callback_data": Try(qid=q.id, idx=-1, progress=0).pack()}))
            elif d.idx < 0:
                new_row.append(b)
            else:
                new_row.append(b.model_copy(update={"callback_data": Try(qid=q.id, idx=d.idx, progress=progress).pack()}))
        rows.append(new_row)
    await callback.message.edit_text(_try_text(q, progress), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer("👍")
