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
    Calculates the Mountain clock from UTC so the 8 PM rollover stays correct
    through daylight-saving time without requiring a timezone database.
    """
    utc_now = datetime.now(timezone.utc)
    year = utc_now.year
    march_1 = datetime(year, 3, 1, tzinfo=timezone.utc)
    dst_start_day = 8 + (6 - march_1.weekday()) % 7
    nov_1 = datetime(year, 11, 1, tzinfo=timezone.utc)
    dst_end_day = 1 + (6 - nov_1.weekday()) % 7
    dst_start = datetime(year, 3, dst_start_day, 9, tzinfo=timezone.utc)
    dst_end = datetime(year, 11, dst_end_day, 8, tzinfo=timezone.utc)
    mountain_offset = 6 if dst_start <= utc_now < dst_end else 7
    mountain_hour = (utc_now - timedelta(hours=mountain_offset)).hour
    now = utc_now - timedelta(hours=mountain_offset)
    # From 8 PM through the 4 AM betting-day reset, research should stay on
    # the next calendar slate.  This also covers the midnight-to-4-AM bridge
    # where `vortex_day()` deliberately still represents the prior bet day.
    if mountain_hour >= 20 or mountain_hour < 4:
        return vortex_day_offset(1)
    if now.hour >= 20:  # 8 PM Mountain or later → advance to tomorrow
        return vortex_day_offset(1)
    return vortex_day()
