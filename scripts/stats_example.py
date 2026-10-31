# axbot — Telegram anti-spam captcha bot
# Copyright (C) 2026 Axawys
# SPDX-License-Identifier: GPL-3.0-or-later
"""Render an example statistics chart from made-up data.

    python scripts/stats_example.py [output.png] [days]
"""

import datetime as dt
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from axbot.stats import aggregate, render_chart, summary_text  # noqa: E402


def fake_events(start: dt.date, days: int, tz: dt.tzinfo) -> list[tuple[str, int]]:
    rng = random.Random(42)
    events = []
    for i in range(days):
        day = start + dt.timedelta(days=i)
        spam_wave = 3.0 if 9 <= i <= 12 else 1.0  # a spam raid in the middle
        humans = rng.randint(2, 7)
        bots = int(rng.randint(1, 5) * spam_wave)
        for _ in range(humans):
            events.append(("captcha", day))
            events.append(("pass" if rng.random() > 0.08 else "fail_wrong", day))
        for _ in range(bots):
            events.append(("captcha", day))
            events.append(("fail_timeout" if rng.random() > 0.25 else "fail_wrong", day))
        if rng.random() < 0.3:
            events.append(("bot_kicked", day))
        if rng.random() < 0.2:
            events.append(("captcha", day))
            events.append(("left", day))
    out = []
    for kind, day in events:
        moment = dt.datetime.combine(day, dt.time(rng.randint(0, 23), rng.randint(0, 59)), tz)
        out.append((kind, int(moment.timestamp())))
    return out


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "stats-example.png")
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    tz = dt.UTC
    end = dt.date(2026, 10, 31)
    start = end - dt.timedelta(days=days - 1)
    stats = aggregate(fake_events(start, days, tz), "Linux Chat", start, end, tz)
    out.write_bytes(render_chart(stats))
    print(summary_text(stats))
    print(f"\nsaved to {out}")


if __name__ == "__main__":
    main()
