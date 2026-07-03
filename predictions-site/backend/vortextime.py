"""
VORTEX betting-day clock — single source of truth.
=================================================

Every part of the system (board logging, grading, /record, /grade) must agree
on what "today" means, or picks get logged under one date and looked up under
another. This module is that agreement.

The betting day rolls over at **4 AM Mountain Time** — after every game, including
late West Coast extra-inning games, is final. It is implemented as a fixed UTC
offset (UTC-10) rather than a named zone so it is:
  • immune to missing tzdata on the host (the old ZoneInfo bug),
  • robust to minor server-clock drift (the date only flips at 10:00 UTC, far
    from the evening hours when the owner is actually checking),
  • the same whether computed from the bot or a backend script.

4 AM Mountain  =  6 AM Eastern  =  3 AM Pacific  =  10:00 UTC (during DST).
"""

from datetime import datetime, timezone, timedelta

# UTC-10: subtract 10h from UTC, then take the date → day flips at 4 AM Mountain.
_DAY_FRAME = timezone(timedelta(hours=-10))


def vortex_now() -> datetime:
    """Current instant expressed in the day-frame (UTC-10)."""
    return datetime.now(_DAY_FRAME)


def vortex_day() -> str:
    """Today's betting day as 'YYYY-MM-DD'."""
    return vortex_now().date().isoformat()


def vortex_day_offset(days: int) -> str:
    """Betting day shifted by `days` (negative = past), as 'YYYY-MM-DD'."""
    return (vortex_now().date() + timedelta(days=days)).isoformat()


def vortex_board_day() -> str:
    """
    Board date: auto-advances to tomorrow when it's late evening Mountain time.

    After ~8 PM Mountain, most games are final or in late innings.
    The board should prep for tomorrow's slate instead of re-hashing finished games.
    Uses the UTC-10 betting-day frame: hour >= 20 = 8 PM Mountain or later.
    """
    now = vortex_now()  # datetime in UTC-10 frame
    if now.hour >= 20:  # 8 PM Mountain or later → advance to tomorrow
        return vortex_day_offset(1)
    return vortex_day()
