# axbot — Telegram anti-spam captcha bot
# Copyright (C) 2026 Axawys
# SPDX-License-Identifier: GPL-3.0-or-later
"""Captcha question themes.

To add a theme: create a module next to this one with a `THEME` object
(see linux.py / universal.py) and register it in THEMES below.
"""

from __future__ import annotations

import random

from . import linux, universal
from .base import Question, Theme

THEMES: dict[str, Theme] = {t.key: t for t in (universal.THEME, linux.THEME)}
DEFAULT_THEME = "universal"

QUESTIONS: dict[str, Question] = {}
for _theme in THEMES.values():
    for _q in (*_theme.choice, *_theme.order):
        assert _q.id not in QUESTIONS, f"duplicate question id: {_q.id}"
        QUESTIONS[_q.id] = _q

MODES = {
    "mixed": "🎲 вперемешку",
    "choice": "🔘 выбор ответа",
    "order": "🔢 кнопки по порядку",
}


def get_theme(key: str | None) -> Theme:
    return THEMES.get(key or "", THEMES[DEFAULT_THEME])


def pick_question(theme: str | None = None, mode: str = "mixed", exclude: str | None = None) -> Question:
    t = get_theme(theme)
    if mode == "choice":
        pool = list(t.choice)
    elif mode == "order":
        pool = list(t.order)
    else:
        pool = [*t.choice, *t.order]
    candidates = [q for q in pool if q.id != exclude] or pool
    return random.choice(candidates)


__all__ = ["DEFAULT_THEME", "MODES", "QUESTIONS", "THEMES", "Question", "Theme", "get_theme", "pick_question"]
