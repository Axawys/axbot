# axbot — Telegram anti-spam captcha bot
# Copyright (C) 2026 Axawys
# SPDX-License-Identifier: GPL-3.0-or-later
"""Captcha questions.

The goal is NOT to test Linux knowledge: everything needed to answer is in
the question itself — a bit of basic logic, counting or reading carefully.
The Linux flavour is just for fun.

`answer` is a tuple of indices into `options`:
  * one index  -> "choice" question (press the right button);
  * several    -> "order" question (press the buttons in this exact order),
                  other options are decoys.
Question ids must stay stable: they are stored in the database.
"""

from __future__ import annotations

import random
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


def _choice(qid: str, text: str, correct: str, *wrong: str) -> Question:
    return Question(qid, text, (correct, *wrong), (0,))


def _order(qid: str, text: str, sequence: list[str], decoys: list[str] = ()) -> Question:
    return Question(qid, text, (*sequence, *decoys), tuple(range(len(sequence))))


CHOICE_QUESTIONS: list[Question] = [
    # --- simple, but fun ---
    _choice(
        "vim",
        "Ты открыл vim и застрял в нём навсегда. Спасайся — нажми <code>:q!</code>",
        ":q!", "Alt+F4", "Выдернуть шнур", "Переустановить ОС",
    ),
    _choice("bsod", "Какого цвета Blue Screen of Death? Подсказка в названии 😉", "🟦", "🟥", "🟩", "🟨"),
    _choice("math", "Сколько будет 2 + 2? (Даже в Linux.)", "4", "5", "22", "42"),
    _choice(
        "vacation",
        "Пингвины любят холод. Где Tux проведёт отпуск?",
        "🧊 Антарктида", "🏜 Сахара", "🌋 В вулкане", "🏝 Тропики",
    ),
    _choice(
        "sudo",
        "Чтобы пингвин тебя послушался, нужно волшебное слово. Нажми <code>sudo</code>:",
        "sudo", "please", "abracadabra", "pls",
    ),
    # --- basic logic ---
    _choice(
        "fish",
        "У Tux было 5 рыбок 🐟. Две он съел, а потом поймал ещё три. Сколько рыбок у Tux теперь?",
        "6", "8", "4", "10",
    ),
    _choice(
        "compile",
        "Ядро компилируется 40 минут. Сборку запустили в 13:30. Во сколько она закончится?",
        "14:10", "14:30", "13:70", "14:00",
    ),
    _choice(
        "reboot",
        "Сервер перезагружается раз в 3 дня. Последний раз это было в понедельник. "
        "В какой день будет следующая перезагрузка?",
        "Четверг", "Среда", "Пятница", "Вторник",
    ),
    _choice(
        "friday",
        "Если вчера была пятница (а по пятницам не деплоят 😉), какой день будет послезавтра?",
        "Понедельник", "Воскресенье", "Вторник", "Суббота",
    ),
    _choice("uptime", "Аптайм сервера — 2 суток и 3 часа. Сколько это часов?", "51", "48", "23", "53"),
    _choice(
        "ram",
        "Оперативки в компах становится всё больше: 1, 2, 4, 8, 16 ГБ… Что дальше по этой логике?",
        "32 ГБ", "24 ГБ", "18 ГБ", "20 ГБ",
    ),
    _choice(
        "swim",
        "Все пингвины умеют плавать. Tux — пингвин. Что можно сказать точно?",
        "Tux умеет плавать", "Tux умеет летать", "Tux — рыба", "Ничего нельзя сказать",
    ),
    _choice(
        "stability",
        "По условию задачи: Debian стабильнее Ubuntu, а Ubuntu стабильнее Arch. "
        "Кто из троих наименее стабилен?",
        "Arch", "Debian", "Ubuntu", "Все одинаковые",
    ),
    _choice("ubuntu", "Сколько раз буква «u» встречается в слове «ubuntu»?", "3", "2", "1", "4"),
    _choice(
        "disks",
        "У админа 3 сервера, в каждом по 4 диска. Один диск сгорел 🔥. Сколько дисков работает?",
        "11", "12", "7", "9",
    ),
    _choice("reverse", "Прочитай задом наперёд: «xuniL»", "Linux", "Lunix", "Linus", "Unix"),
    _choice(
        "queue",
        "Tux стоит в очереди за рыбой: перед ним 3 пингвина, а за ним 2. Сколько всего пингвинов в очереди?",
        "6", "5", "3", "7",
    ),
    _choice(
        "download",
        "Скорость загрузки — 10 МБ в секунду. За сколько секунд скачается файл на 50 МБ?",
        "5", "500", "40", "60",
    ),
    _choice(
        "battery",
        "Ноутбук с Linux заряжен на 80% и теряет 20% каждый час. Через сколько часов он сядет?",
        "4", "5", "3", "2",
    ),
]

ORDER_QUESTIONS: list[Question] = [
    _order(
        "ilovelinux",
        "Нажми кнопки так, чтобы получилось «I ❤️ Linux»:",
        ["I", "❤️", "Linux"], ["Windows"],
    ),
    _order("numbers", "Нажми числа от меньшего к большему:", ["1️⃣", "2️⃣", "3️⃣", "4️⃣"]),
    _order(
        "vimexit",
        "Выйди из vim правильно: <code>Esc</code>, потом <code>:q!</code>, потом <code>Enter</code>",
        ["Esc", ":q!", "Enter"], ["Ctrl+Z"],
    ),
    _order("even", "Нажми только чётные числа — по возрастанию:", ["2", "4", "8"], ["3", "7"]),
    _order(
        "alphabet",
        "Расставь дистрибутивы по алфавиту:",
        ["Arch", "Debian", "Fedora", "Mint"],
    ),
    _order(
        "sizes",
        "Мышь 🐭 меньше пингвина 🐧, а жираф 🦒 больше пингвина. "
        "Нажми от самого маленького к самому большому:",
        ["🐭", "🐧", "🦒"],
    ),
    _order(
        "scheduler",
        "Планировщик сначала запускает самые короткие задачи. Нажми задачи в порядке запуска:",
        ["1 мин", "5 мин", "30 мин", "2 ч"],
    ),
    _order(
        "countdown",
        "Обратный отсчёт до релиза: нажми числа от большего к меньшему, а в конце — ракету:",
        ["3️⃣", "2️⃣", "1️⃣", "🚀"],
    ),
]

QUESTIONS: dict[str, Question] = {q.id: q for q in CHOICE_QUESTIONS + ORDER_QUESTIONS}
assert len(QUESTIONS) == len(CHOICE_QUESTIONS) + len(ORDER_QUESTIONS), "duplicate question id"

MODES = {
    "mixed": "🎲 вперемешку",
    "choice": "🔘 выбор ответа",
    "order": "🔢 кнопки по порядку",
}


def pick_question(mode: str = "mixed", exclude: str | None = None) -> Question:
    if mode == "choice":
        pool = CHOICE_QUESTIONS
    elif mode == "order":
        pool = ORDER_QUESTIONS
    else:
        pool = CHOICE_QUESTIONS + ORDER_QUESTIONS
    candidates = [q for q in pool if q.id != exclude] or pool
    return random.choice(candidates)
