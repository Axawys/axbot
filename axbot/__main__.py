# axbot — Telegram anti-spam captcha bot
# Copyright (C) 2026 Axawys
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import asyncio
import contextlib
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeAllChatAdministrators, BotCommandScopeAllPrivateChats

from .config import load_config
from .db import Database
from .handlers import build_router
from .service import watchdog

log = logging.getLogger("axbot")

PRIVATE_COMMANDS = [
    BotCommand(command="start", description="Что я умею"),
    BotCommand(command="chats", description="Мои чаты и настройки"),
    BotCommand(command="cancel", description="Отменить ввод"),
]

ADMIN_COMMANDS = [
    BotCommand(command="settings", description="Настройки капчи"),
    BotCommand(command="help", description="Список команд бота"),
    BotCommand(command="captcha", description="on|off — включить/выключить капчу"),
    BotCommand(command="timeout", description="Время на ответ, сек"),
    BotCommand(command="attempts", description="Число попыток"),
    BotCommand(command="action", description="kick|ban — что делать с провалившими"),
    BotCommand(command="mode", description="mixed|choice|order — тип вопросов"),
    BotCommand(command="greeting", description="Приветствие (текст | reset)"),
    BotCommand(command="greetttl", description="Автоудаление приветствия, сек"),
    BotCommand(command="clean", description="on|off — удалять «вступил/вышел»"),
    BotCommand(command="pass", description="Пропустить новичка без капчи"),
    BotCommand(command="managers", description="Кто управляет ботом"),
    BotCommand(command="addmanager", description="Добавить менеджера"),
    BotCommand(command="delmanager", description="Убрать менеджера"),
]


async def main() -> None:
    config = load_config()
    logging.basicConfig(
        level=config.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    db = Database(config.db_path)
    await db.connect()

    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(db=db, config=config)
    dp.include_router(build_router())

    await bot.set_my_commands(PRIVATE_COMMANDS, scope=BotCommandScopeAllPrivateChats())
    await bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeAllChatAdministrators())

    me = await bot.me()
    log.info("starting @%s", me.username)

    wd = asyncio.create_task(watchdog(bot, db))
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        wd.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await wd
        await db.close()
        await bot.session.close()


def run() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())


if __name__ == "__main__":
    run()
