"""
Offline, per-game opposing-starting-pitcher resolution for VORTEX V2 training.

A batter's own gamelog only carries `opponent_id` (the opposing TEAM), never
the identity of the pitcher actually faced that day -- but Research's
richest matchup signal (backend/analyze.py's _v2_matchup, keyed on the
opposing pitcher's era/fip) needs exactly that. fetch_gamelogs.py already
captures each row's `game_pk` for free (it's in the same gameLog API
response), so resolving the starter only costs one extra call PER DISTINCT
GAME (not per batter-row, not per team-season) -- shared across every batter
and pitcher who played in that game.

`/game/{gamePk}/boxscore` lists each side's pitchers in appearance order and
tags the actual starter with stat.pitching.gamesStarted == 1 (confirmed
against real data -- searching for gamesStarted==1 rather than trusting
"first in the pitchers list" guards against any odd depth-chart ordering).

Offline/training only -- never imported by any predictions-site/api/*.py
endpoint. Cached forever per game_pk: a finished game's boxscore never
changes.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
import stats_mlb  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent / "data" / "raw"
DATA_DIR.mkdir(parents=True, exist_ok=True)


def fetch_game_starters(game_pk: int) -> dict:
    """Return {team_id: starting_pitcher_id} for both sides of one game."""
    cache_file = DATA_DIR / f"boxscore_starters_{game_pk}.json"
    if cache_file.exists():
        return {int(k): v for k, v in json.loads(cache_file.read_text(encoding="utf-8")).items()}

    data = stats_mlb._get(f"/game/{game_pk}/boxscore", {}, cache_key=None)
    if data is None:
        return {}  # request failed -- do NOT cache, retry next call
    teams = data.get("teams", {})
    starters = {}
    for side in ("home", "away"):
        t = teams.get(side, {})
        team_id = (t.get("team") or {}).get("id")
        if not team_id:
            continue
        players = t.get("players", {})
        for pid in t.get("pitchers", []):
            p = players.get(f"ID{pid}", {})
            gs = (p.get("stats", {}) or {}).get("pitching", {}).get("gamesStarted")
            if gs == 1:
                starters[team_id] = pid
                break

    cache_file.write_text(json.dumps(starters), encoding="utf-8")
    return starters


def resolve_starters_for_games(game_pks: set) -> dict:
    """Batch version: {game_pk: {team_id: starting_pitcher_id}} for every
    game_pk in `game_pks`. Slow on a cold cache (one boxscore call per
    distinct game); instant on every subsequent run."""
    out = {}
    total = len(game_pks)
    for i, game_pk in enumerate(sorted(game_pks)):
        if not game_pk:
            continue
        out[game_pk] = fetch_game_starters(game_pk)
        if (i + 1) % 200 == 0:
            print(f"  resolved starters for {i + 1}/{total} games")
    return out
