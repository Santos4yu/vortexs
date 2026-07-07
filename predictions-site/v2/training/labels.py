"""
Training-label construction for VORTEX V2.

Reuses backend/grade_results.py's `_grade()` -- the exact same pure
over/under/push decision function V1's live grader uses -- so V2's labels
are defined identically to how V1 defines "hit," keeping any hit-rate
comparison between the two fair.

Deliberately does NOT reuse `_mlb_game_stats()` (V1's per-date boxscore
fetcher): every field these stat_types need (hits, total bases, RBIs, runs,
doubles/triples/HR/walks/HBP/steals for fantasy_score, pitching
strikeouts/hits/earned-runs/innings-pitched) is already present in the
gameLog entry fetch_gamelogs.py fetches, so a second boxscore call per
game/date would be redundant network cost.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "backend"))
from grade_results import _grade  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from v2.common.stat_types import (  # noqa: E402
    BATTER_RAW_FIELDS, PITCHER_RAW_FIELDS, STANDARD_LINES, DEFAULT_SIDE,
)


def compute_batter_actual(stat: dict, stat_type: str) -> float:
    if stat_type in BATTER_RAW_FIELDS:
        return float(stat.get(BATTER_RAW_FIELDS[stat_type], 0) or 0)

    if stat_type == "hits_runs_rbis":
        return (float(stat.get("hits", 0) or 0)
                + float(stat.get("runs", 0) or 0)
                + float(stat.get("rbi", 0) or 0))

    if stat_type == "fantasy_score":
        # Same PrizePicks-style scoring backend/grade_results.py's
        # _mlb_game_stats() uses, just computed from gameLog fields (which
        # carry the same doubles/triples/HR/runs/RBI/BB/HBP/SB values)
        # instead of a boxscore fetch.
        h = float(stat.get("hits", 0) or 0)
        d = float(stat.get("doubles", 0) or 0)
        t = float(stat.get("triples", 0) or 0)
        hr = float(stat.get("homeRuns", 0) or 0)
        r = float(stat.get("runs", 0) or 0)
        rbi = float(stat.get("rbi", 0) or 0)
        bb = float(stat.get("baseOnBalls", 0) or 0)
        hbp = float(stat.get("hitByPitch", 0) or 0)
        sb = float(stat.get("stolenBases", 0) or 0)
        singles = max(0.0, h - d - t - hr)
        return singles * 3 + d * 5 + t * 8 + hr * 10 + r * 2 + rbi * 2 + bb * 2 + hbp * 2 + sb * 5

    raise ValueError(f"Unknown batter stat_type: {stat_type}")


def compute_pitcher_actual(stat: dict, stat_type: str) -> float:
    if stat_type in PITCHER_RAW_FIELDS:
        return float(stat.get(PITCHER_RAW_FIELDS[stat_type], 0) or 0)
    raise ValueError(f"Unknown pitcher stat_type: {stat_type}")


def label_for_game(game: dict, stat_type: str, is_pitcher: bool = False) -> str:
    """`game` is one entry from fetch_gamelogs.py's per-player game list."""
    actual = (compute_pitcher_actual(game["stat"], stat_type) if is_pitcher
              else compute_batter_actual(game["stat"], stat_type))
    line = STANDARD_LINES[stat_type]
    return _grade(actual, line, DEFAULT_SIDE)
