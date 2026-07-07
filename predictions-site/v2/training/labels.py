"""
Training-label construction for VORTEX V2.

Reuses backend/grade_results.py's `_grade()` -- the exact same pure
over/under/push decision function V1's live grader uses -- so V2's labels
are defined identically to how V1 defines "hit," keeping any hit-rate
comparison between the two fair.

Deliberately does NOT reuse `_mlb_game_stats()` (V1's per-date boxscore
fetcher): for the batter stat types V2 currently trains on (hits,
total_bases, home_runs), the batting gameLog entry already fetched by
fetch_gamelogs.py carries the exact same MLB Stats API numbers for that
game, so a second boxscore call per game/date would be redundant network
cost. `_mlb_game_stats` is the right tool to reach for if V2 later adds
pitcher props or fantasy_score, which combine fields not all present in a
batter's own gameLog stat block.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "backend"))
from grade_results import _grade  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from v2.common.stat_types import GAMELOG_FIELD, STANDARD_LINES, DEFAULT_SIDE  # noqa: E402


def label_for_game(game: dict, stat_type: str) -> str:
    """`game` is one entry from fetch_gamelogs.py's per-player game list.
    Returns "hit", "miss", or "push"."""
    field = GAMELOG_FIELD[stat_type]
    actual = float(game["stat"].get(field, 0) or 0)
    line = STANDARD_LINES[stat_type]
    return _grade(actual, line, DEFAULT_SIDE)
