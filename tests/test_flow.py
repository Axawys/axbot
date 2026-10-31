# axbot — Telegram anti-spam captcha bot
# Copyright (C) 2026 Axawys
# SPDX-License-Identifier: GPL-3.0-or-later
"""End-to-end flow tests against a fake Telegram API (no network)."""

from __future__ import annotations

import datetime as dt
import itertools

import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.methods import (
    BanChatMember,
    DeleteMessage,
    EditMessageText,
    GetChatMember,
    GetMe,
    RestrictChatMember,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import (
    CallbackQuery,
    Chat,
    ChatMemberAdministrator,
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberOwner,
    ChatMemberUpdated,
    Message,
    Update,
    User,
)

from axbot import service
from axbot.common import Cap
from axbot.config import Config
from axbot.db import Database
from axbot.handlers import build_router, events, group_admin, private
from axbot.questions import QUESTIONS

BOT_ID = 4242
OWNER = User(id=1, is_bot=False, first_name="Owner")
NEWBIE = User(id=2, is_bot=False, first_name="Newbie")
STRANGER = User(id=3, is_bot=False, first_name="Stranger")
CHAT = Chat(id=-100500, type="supergroup", title="Linux Chat")
NOW = dt.datetime.now(dt.UTC)


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod] = []
        self.admins: set[int] = {OWNER.id}
        self._ids = itertools.count(1000)

    def of(self, kind):
        return [c for c in self.calls if isinstance(c, kind)]

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, GetMe):
            return User(id=BOT_ID, is_bot=True, first_name="axbot", username="axbot_test")
        if isinstance(method, SendMessage):
            return Message(
                message_id=next(self._ids), date=NOW,
                chat=Chat(id=method.chat_id, type="supergroup" if method.chat_id < 0 else "private"),
                text=method.text, reply_markup=method.reply_markup,
            )
        if isinstance(method, EditMessageText):
            return True
        if isinstance(method, GetChatMember):
            user = User(id=method.user_id, is_bot=False, first_name=f"u{method.user_id}")
            if method.user_id == OWNER.id:
                return ChatMemberOwner(user=user, is_anonymous=False)
            return ChatMemberMember(user=user)
        return True

    async def close(self):
        pass

    async def stream_content(self, *a, **kw):  # pragma: no cover
        yield b""


@pytest_asyncio.fixture
async def env(tmp_path):
    session = FakeSession()
    bot = Bot(f"{BOT_ID}:TEST", session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    db = Database(str(tmp_path / "test.sqlite3"))
    await db.connect()
    config = Config(bot_token="x", db_path="", superadmins=frozenset(), log_level="INFO")
    dp = Dispatcher(db=db, config=config)
    dp.include_router(build_router())
    ids = itertools.count(1)

    async def feed(**kw):
        await dp.feed_update(bot, Update(update_id=next(ids), **kw))

    yield bot, dp, db, session, feed
    await db.close()
    # Routers are module-level singletons: detach them for the next test.
    for r in (events.router, group_admin.router, private.router):
        r._parent_router = None


def member_update(user: User, actor: User, old, new) -> ChatMemberUpdated:
    return ChatMemberUpdated(chat=CHAT, from_user=actor, date=NOW, old_chat_member=old, new_chat_member=new)


async def add_bot(feed):
    bot_user = User(id=BOT_ID, is_bot=True, first_name="axbot")
    admin = ChatMemberAdministrator(
        user=bot_user, can_be_edited=False, is_anonymous=False, can_manage_chat=True,
        can_delete_messages=True, can_manage_video_chats=False, can_restrict_members=True,
        can_promote_members=False, can_change_info=False, can_invite_users=True,
        can_post_stories=False, can_edit_stories=False, can_delete_stories=False,
        can_send_welcome_messages=False,
    )
    await feed(my_chat_member=member_update(bot_user, OWNER, ChatMemberLeft(user=bot_user), admin))


async def join(feed, user=NEWBIE, actor=None):
    await feed(chat_member=member_update(user, actor or user, ChatMemberLeft(user=user), ChatMemberMember(user=user)))


def click(feed, session, msg_id: int, user: User, idx: int):
    msg = Message(message_id=msg_id, date=NOW, chat=CHAT, text="captcha",
                  reply_markup=next(c.reply_markup for c in session.of(SendMessage) if c.reply_markup))
    data = Cap(user_id=NEWBIE.id, idx=idx).pack()
    return feed(callback_query=CallbackQuery(id="cb", from_user=user, chat_instance="ci", message=msg, data=data))


def group_msg(text: str, user: User, mid: int = 77) -> Message:
    return Message(message_id=mid, date=NOW, chat=CHAT, from_user=user, text=text)


# ------------------------------------------------------------------- tests


@pytest.mark.asyncio
async def test_bot_added_sets_owner(env):
    bot, dp, db, session, feed = env
    await add_bot(feed)
    chat = await db.get_chat(CHAT.id)
    assert chat.owner_id == OWNER.id and chat.active
    assert any("пингвин-вышибала" in m.text for m in session.of(SendMessage))


@pytest.mark.asyncio
async def test_correct_answer_passes(env):
    bot, dp, db, session, feed = env
    await add_bot(feed)
    await join(feed)

    p = await db.get_pending(CHAT.id, NEWBIE.id)
    assert p is not None
    restrict = session.of(RestrictChatMember)[-1]
    assert restrict.permissions.can_send_messages is False

    q = QUESTIONS[p.question_id]
    for step, idx in enumerate(q.answer):
        await click(feed, session, p.message_id, NEWBIE, idx)
        if step < len(q.answer) - 1:
            assert (await db.get_pending(CHAT.id, NEWBIE.id)).progress == step + 1

    assert await db.get_pending(CHAT.id, NEWBIE.id) is None
    assert session.of(RestrictChatMember)[-1].permissions.can_send_messages is True
    assert "Проверка пройдена" in session.of(SendMessage)[-1].text
    assert not session.of(BanChatMember)


@pytest.mark.asyncio
async def test_wrong_answers_kick(env):
    bot, dp, db, session, feed = env
    await add_bot(feed)
    await join(feed)
    for _ in range(2):  # default attempts = 2
        p = await db.get_pending(CHAT.id, NEWBIE.id)
        q = QUESTIONS[p.question_id]
        wrong = next(i for i in range(len(q.options)) if i != q.answer[0])
        await click(feed, session, p.message_id, NEWBIE, wrong)
    assert await db.get_pending(CHAT.id, NEWBIE.id) is None
    assert len(session.of(BanChatMember)) == 1
    assert any(type(c).__name__ == "UnbanChatMember" for c in session.calls)


@pytest.mark.asyncio
async def test_someone_else_cannot_answer(env):
    bot, dp, db, session, feed = env
    await add_bot(feed)
    await join(feed)
    p = await db.get_pending(CHAT.id, NEWBIE.id)
    await click(feed, session, p.message_id, STRANGER, QUESTIONS[p.question_id].answer[0])
    assert await db.get_pending(CHAT.id, NEWBIE.id) is not None


@pytest.mark.asyncio
async def test_timeout_kicks(env):
    bot, dp, db, session, feed = env
    await add_bot(feed)
    await join(feed)
    await db.update_pending(CHAT.id, NEWBIE.id, deadline=0)
    for p in await db.expired_pending(10**10):
        await service.expire(bot, db, p.chat_id, p.user_id)
    assert await db.get_pending(CHAT.id, NEWBIE.id) is None
    assert len(session.of(BanChatMember)) == 1


@pytest.mark.asyncio
async def test_ban_mode(env):
    bot, dp, db, session, feed = env
    await add_bot(feed)
    await db.update_setting(CHAT.id, "fail_action", "ban")
    await join(feed)
    await db.update_pending(CHAT.id, NEWBIE.id, deadline=0)
    await service.expire(bot, db, CHAT.id, NEWBIE.id)
    assert session.of(BanChatMember)[-1].revoke_messages is True
    assert not any(type(c).__name__ == "UnbanChatMember" for c in session.calls)


@pytest.mark.asyncio
async def test_added_by_admin_skips_captcha(env):
    bot, dp, db, session, feed = env
    await add_bot(feed)
    await join(feed, actor=OWNER)
    assert await db.get_pending(CHAT.id, NEWBIE.id) is None


@pytest.mark.asyncio
async def test_bot_added_by_member_is_kicked(env):
    bot, dp, db, session, feed = env
    await add_bot(feed)
    spam_bot = User(id=999, is_bot=True, first_name="SpamBot")
    await join(feed, user=spam_bot, actor=STRANGER)
    assert session.of(BanChatMember)[-1].user_id == 999


@pytest.mark.asyncio
async def test_leave_cancels(env):
    bot, dp, db, session, feed = env
    await add_bot(feed)
    await join(feed)
    p = await db.get_pending(CHAT.id, NEWBIE.id)
    await feed(chat_member=member_update(NEWBIE, NEWBIE, ChatMemberMember(user=NEWBIE), ChatMemberLeft(user=NEWBIE)))
    assert await db.get_pending(CHAT.id, NEWBIE.id) is None
    assert any(isinstance(c, DeleteMessage) and c.message_id == p.message_id for c in session.calls)


@pytest.mark.asyncio
async def test_group_commands_owner_vs_stranger(env):
    bot, dp, db, session, feed = env
    await add_bot(feed)

    await feed(message=group_msg("/timeout 300", STRANGER, mid=10))
    assert (await db.get_chat(CHAT.id)).timeout == 120
    assert any(isinstance(c, DeleteMessage) and c.message_id == 10 for c in session.calls)

    await feed(message=group_msg("/timeout 300", OWNER, mid=11))
    await feed(message=group_msg("/action ban", OWNER, mid=12))
    await feed(message=group_msg("/captcha off", OWNER, mid=13))
    chat = await db.get_chat(CHAT.id)
    assert (chat.timeout, chat.fail_action, chat.enabled) == (300, "ban", False)

    await join(feed)  # captcha disabled → nothing happens
    assert await db.get_pending(CHAT.id, NEWBIE.id) is None


@pytest.mark.asyncio
async def test_manager_can_manage(env):
    bot, dp, db, session, feed = env
    await add_bot(feed)
    reply_to = group_msg("hi", STRANGER, mid=20)
    msg = Message(message_id=21, date=NOW, chat=CHAT, from_user=OWNER, text="/addmanager", reply_to_message=reply_to)
    await feed(message=msg)
    assert await db.is_manager(CHAT.id, STRANGER.id)
    await feed(message=group_msg("/mode order", STRANGER, mid=22))
    assert (await db.get_chat(CHAT.id)).mode == "order"


@pytest.mark.asyncio
async def test_greeting_command_and_pass(env):
    bot, dp, db, session, feed = env
    await add_bot(feed)
    await feed(message=group_msg("/greeting Привет, {name}! Это {chat}", OWNER, mid=30))
    assert (await db.get_chat(CHAT.id)).greeting == "Привет, {name}! Это {chat}"
    await join(feed)
    p = await db.get_pending(CHAT.id, NEWBIE.id)
    await feed(message=group_msg(f"/pass {NEWBIE.id}", OWNER, mid=31))
    assert await db.get_pending(CHAT.id, NEWBIE.id) is None
    assert any(isinstance(c, SendMessage) and c.text == "Привет, Newbie! Это Linux Chat" for c in session.calls)
    assert p is not None


@pytest.mark.asyncio
async def test_private_panel_access(env):
    bot, dp, db, session, feed = env
    await add_bot(feed)
    pm = Chat(id=OWNER.id, type="private")
    await feed(message=Message(message_id=1, date=NOW, chat=pm, from_user=OWNER, text=f"/start chat_{CHAT.id}"))
    assert "Настройки чата" in session.of(SendMessage)[-1].text

    pm2 = Chat(id=STRANGER.id, type="private")
    await feed(message=Message(message_id=2, date=NOW, chat=pm2, from_user=STRANGER, text=f"/start chat_{CHAT.id}"))
    assert "⛔" in session.of(SendMessage)[-1].text


def test_questions_are_sane():
    for q in QUESTIONS.values():
        assert len(set(q.options)) == len(q.options), q.id
        assert all(0 <= i < len(q.options) for i in q.answer), q.id
        assert len(Cap(user_id=10**12, idx=len(q.options)).pack()) <= 64
