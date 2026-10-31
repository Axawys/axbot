# axbot — Telegram anti-spam captcha bot
# Copyright (C) 2026 Axawys
# SPDX-License-Identifier: GPL-3.0-or-later
"""Captcha statistics: date range parsing, aggregation and the PNG chart."""

from __future__ import annotations

import asyncio
import datetime as dt
import io
import re
from collections import Counter
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .db import Database

MAX_DAYS = 366
DEFAULT_DAYS = 7
WEEKLY_AFTER_DAYS = 92  # longer ranges are grouped by week

# Failure reasons shown on the chart, in stacking order (bottom → top).
# Colors: categorical slots 1–3 of the validated reference palette (light mode).
FAIL_SERIES = [
    ("fail_timeout", "Не ответили вовремя", "#2a78d6"),
    ("fail_wrong", "Ошиблись в ответах", "#eb6834"),
    ("bot_kicked", "Боты, добавленные участниками", "#1baf7a"),
]

SURFACE = "#fcfcfb"
TEXT_PRIMARY = "#0b0b0b"
TEXT_SECONDARY = "#52514e"
GRID = "#e4e3df"

_DATE_RE = re.compile(r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}\.\d{1,2}(?:\.\d{2,4})?")


class RangeError(ValueError):
    pass


def get_tz(name: str) -> dt.tzinfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return dt.UTC


def _parse_date(text: str, today: dt.date) -> dt.date:
    try:
        if "-" in text:
            return dt.date.fromisoformat("-".join(p.zfill(2) for p in text.split("-")))
        parts = text.split(".")
        day, month = int(parts[0]), int(parts[1])
        if len(parts) == 2:
            return dt.date(today.year, month, day)
        year = int(parts[2])
        return dt.date(year + 2000 if year < 100 else year, month, day)
    except ValueError as e:
        raise RangeError(f"Не понимаю дату «{text}».") from e


def parse_range(text: str | None, today: dt.date) -> tuple[dt.date, dt.date]:
    """Inclusive (start, end).

    Accepts: empty (last 7 days), "30" (last 30 days), one date (that day),
    or two dates in DD.MM.YYYY / DD.MM / YYYY-MM-DD with any separator.
    """
    text = (text or "").strip()
    if not text:
        days = DEFAULT_DAYS
    elif text.isdigit():
        days = int(text)
        if not 1 <= days <= MAX_DAYS:
            raise RangeError(f"Число дней — от 1 до {MAX_DAYS}.")
    else:
        found = _DATE_RE.findall(text)
        if not found or len(found) > 2:
            raise RangeError("Укажи одну или две даты, например <code>01.10.2026 31.10.2026</code>.")
        dates = [_parse_date(d, today) for d in found]
        start, end = min(dates), max(dates)
        if (end - start).days + 1 > MAX_DAYS:
            raise RangeError(f"Слишком длинный период — максимум {MAX_DAYS} дней.")
        return start, end
    return today - dt.timedelta(days=days - 1), today


# ------------------------------------------------------------- aggregation


@dataclass
class Stats:
    chat_title: str
    start: dt.date
    end: dt.date
    weekly: bool
    buckets: list[dt.date]  # first day of every bucket
    per_bucket: dict[str, list[int]]  # kind -> count per bucket
    totals: Counter = field(default_factory=Counter)

    @property
    def failed(self) -> int:
        return sum(self.totals[k] for k, _, _ in FAIL_SERIES)


async def collect(db: Database, chat_id: int, chat_title: str, start: dt.date, end: dt.date, tz: dt.tzinfo) -> Stats:
    start_ts = int(dt.datetime.combine(start, dt.time(), tz).timestamp())
    end_ts = int(dt.datetime.combine(end + dt.timedelta(days=1), dt.time(), tz).timestamp())
    events = await db.events_between(chat_id, start_ts, end_ts)
    return aggregate(events, chat_title, start, end, tz)


def aggregate(events: list[tuple[str, int]], chat_title: str, start: dt.date, end: dt.date, tz: dt.tzinfo) -> Stats:
    days = (end - start).days + 1
    weekly = days > WEEKLY_AFTER_DAYS
    step = 7 if weekly else 1
    first = start - dt.timedelta(days=start.weekday()) if weekly else start
    buckets = []
    d = first
    while d <= end:
        buckets.append(d)
        d += dt.timedelta(days=step)

    per_bucket: dict[str, list[int]] = {}
    totals: Counter = Counter()
    for kind, ts in events:
        day = dt.datetime.fromtimestamp(ts, tz).date()
        if not start <= day <= end:
            continue
        idx = (day - first).days // step
        per_bucket.setdefault(kind, [0] * len(buckets))[idx] += 1
        totals[kind] += 1
    return Stats(chat_title, start, end, weekly, buckets, per_bucket, totals)


# ------------------------------------------------------------------- text


def _pct(part: int, whole: int) -> str:
    return f"{round(100 * part / whole)}%" if whole else "—"


def summary_text(s: Stats) -> str:
    t = s.totals
    shown = t["captcha"]
    passed = t["pass"] + t["pass_manual"]
    period = f"{s.start:%d.%m.%Y} – {s.end:%d.%m.%Y}"
    lines = [
        f"📊 <b>Статистика входа</b>, {period}",
        "",
        f"Капчу показали: {shown}",
        f"Прошли проверку: {passed} ({_pct(passed, shown)})"
        + (f", из них пропущены вручную: {t['pass_manual']}" if t["pass_manual"] else ""),
        f"Не пустили: <b>{s.failed}</b>",
        f"  • не ответили вовремя: {t['fail_timeout']}",
        f"  • ошиблись в ответах: {t['fail_wrong']}",
        f"  • боты, добавленные участниками: {t['bot_kicked']}",
        f"Ушли сами, не ответив: {t['left']}",
    ]
    if s.weekly:
        lines.append("\nНа графике данные сгруппированы по неделям.")
    return "\n".join(lines)


# ------------------------------------------------------------------ chart


def render_chart(s: Stats) -> bytes:
    """Stacked columns of failed attempts per day (or week). Returns PNG bytes."""
    # Imported lazily: matplotlib is heavy and only needed here.
    from matplotlib.figure import Figure
    from matplotlib.ticker import MaxNLocator

    n = len(s.buckets)
    fig = Figure(figsize=(10, 5.4), dpi=160, facecolor=SURFACE)
    ax = fig.add_axes((0.07, 0.1, 0.9, 0.69), facecolor=SURFACE)

    x = list(range(n))
    # Thin columns: at most ~44 px wide (≈24 px on screen), never filling the slot.
    slot_px = 0.9 * 10 * 160 / max(n, 1)
    width = min(0.72, 44 / slot_px)
    bottom = [0] * n
    for kind, label, color in FAIL_SERIES:
        values = s.per_bucket.get(kind, [0] * n)
        # Surface-colored edge = the 2px gap between stacked segments.
        ax.bar(x, values, width=width, bottom=bottom, color=color, label=label,
               edgecolor=SURFACE, linewidth=1.0, zorder=3)
        bottom = [b + v for b, v in zip(bottom, values, strict=True)]

    # Selective direct label: only the worst bucket gets its total.
    peak = max(bottom) if bottom else 0
    if peak:
        i = bottom.index(peak)
        ax.annotate(str(peak), (i, peak), xytext=(0, 4), textcoords="offset points",
                    ha="center", va="bottom", fontsize=10, color=TEXT_PRIMARY, fontweight="bold")
    else:
        ax.text(0.5, 0.5, "Неудачных попыток не было", transform=ax.transAxes,
                ha="center", va="center", fontsize=13, color=TEXT_SECONDARY)

    # Axes: recessive grid, no box.
    ax.set_ylim(0, max(peak * 1.15, 5))
    ax.yaxis.set_major_locator(MaxNLocator(integer=True, nbins=6))
    ax.grid(axis="y", color=GRID, linewidth=0.8, zorder=0)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(axis="both", colors=TEXT_SECONDARY, labelsize=9, length=0, pad=6)
    ax.set_xlim(-0.6, n - 0.4)

    every = max(1, round(n / 12))
    ticks = list(range(0, n, every))
    fmt = "%d.%m" if (s.end - s.start).days < 300 or s.start.year == s.end.year else "%d.%m.%y"
    # A partial first week is labeled with the range start, not the Monday before it.
    ax.set_xticks(ticks, [max(s.buckets[i], s.start).strftime(fmt) for i in ticks])

    # Title block, left-aligned with the plot.
    unit = "по неделям" if s.weekly else "по дням"
    fig.text(0.07, 0.94, "Неудачные попытки входа", fontsize=16, fontweight="bold", color=TEXT_PRIMARY)
    subtitle = f"{s.chat_title} · {s.start:%d.%m.%Y} – {s.end:%d.%m.%Y} · {unit} · всего {s.failed}"
    fig.text(0.07, 0.885, subtitle, fontsize=10.5, color=TEXT_SECONDARY)
    if s.failed:
        fig.legend(loc="upper left", bbox_to_anchor=(0.062, 0.865), ncol=3, frameon=False,
                   fontsize=9.5, labelcolor=TEXT_PRIMARY, handlelength=1.0, handleheight=1.0,
                   columnspacing=1.6, borderaxespad=0)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=SURFACE)
    return buf.getvalue()


# ----------------------------------------------------------------- report


async def build_report(
    db: Database, chat_id: int, chat_title: str, range_text: str | None, tz_name: str
) -> tuple[bytes, str]:
    """PNG chart + caption for the chat. Raises RangeError on bad input."""
    tz = get_tz(tz_name)
    today = dt.datetime.now(tz).date()
    start, end = parse_range(range_text, today)
    stats = await collect(db, chat_id, chat_title or str(chat_id), start, end, tz)
    png = await asyncio.to_thread(render_chart, stats)
    return png, summary_text(stats)


RANGE_HELP = (
    "Период можно указать так:\n"
    "<code>30</code> — последние 30 дней\n"
    "<code>01.10.2026 31.10.2026</code> — с даты по дату\n"
    "<code>2026-10-01 2026-10-31</code> — то же в формате ISO\n"
    "<code>15.10</code> — один день текущего года"
)
