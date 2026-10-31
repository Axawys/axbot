# axbot — Telegram anti-spam captcha bot
# Copyright (C) 2026 Axawys
# SPDX-License-Identifier: GPL-3.0-or-later

from aiogram import Router

from . import events, group_admin, private


def build_router() -> Router:
    root = Router(name="axbot")
    # Order matters: events first (joins, captcha buttons, muting pending users),
    # then admin commands in groups, then the private settings panel.
    root.include_routers(events.router, group_admin.router, private.router)
    return root
