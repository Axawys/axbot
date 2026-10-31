# axbot — Telegram anti-spam captcha bot
# Copyright (C) 2026 Axawys
# SPDX-License-Identifier: GPL-3.0-or-later
"""Building blocks for captcha questions and themes.

Rule for every question: everything needed to answer must be in the question
itself — a bit of basic logic, counting or careful reading. No trivia.

`answer` is a tuple of indices into `options`:
  * one index  -> "choice" question (press the right button);
  * several    -> "order" question (press the buttons in this exact order),
                  other options are decoys.
Question ids must be globally unique and stable: they are stored in the database.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Question:
    id: str
    text: str  # HTML
    options: tuple[str, ...]
    answer: tuple[int, ...]

    @property
    def is_order(self) -> bool:
        return len(self.answer) > 1


@dataclass(frozen=True)
class Theme:
    key: str
    title: str
    intro: str  # line shown above the question (HTML)
    greeting: str  # default greeting; {mention}, {name}, {chat} placeholders
    fail_phrases: tuple[str, ...]  # "<name> ..." when someone fails
    choice: tuple[Question, ...]
    order: tuple[Question, ...]


def choice(qid: str, text: str, correct: str, *wrong: str) -> Question:
    return Question(qid, text, (correct, *wrong), (0,))


def order(qid: str, text: str, sequence: list[str], decoys: list[str] = ()) -> Question:
    return Question(qid, text, (*sequence, *decoys), tuple(range(len(sequence))))
