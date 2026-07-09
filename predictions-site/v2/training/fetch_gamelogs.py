"""
Offline, season-parametrized MLB batter gamelog fetcher for VORTEX V2
training.

Reuses stats_mlb's HTTP/session/base-URL machinery (`_get`, the
Cloudflare-proxy-with-fallback + polite rate-limiting logic already built
for the live site) instead of reimplementing request handling, but keeps its
own on-disk cache under v2/training/data/raw -- separate from stats_mlb's
serverless cache, because a finished past season's gamelog is immutable and
should never expire, whereas stats_mlb's cache is tuned for a live site's
rolling 14h/48h TTLs.

Offline/training only -- never imported by any predictions-site/api/*.py
endpoint.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
import stats_mlb  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent / "data" / "raw"
DATA_DIR.mkdir(parents=True, exist_ok=True)


def fetch_season_batters(season: int, limit: int | None = None) -> list[dict]:
    """
    Return [{"id":, "fullName":}] for every non-pitcher on an active/40-man
    roster in `season`. Uses the same /sports/1/players endpoint stats_mlb.py's
    _fetch_active_players() calls for the current season, parametrized here
    to accept any past season.
    """
    cache_file = DATA_DIR / f"season_batters_{season}.json"
    if cache_file.exists():
        batters = json.loads(cache_file.read_text(encoding="utf-8"))
        return batters[:limit] if limit else batters

    data = stats_mlb._get(
        "/sports/1/players",
        {"season": season, "hydrate": "currentTeam"},
        cache_key=None,  # cached ourselves below, indefinitely -- a past season's roster never changes
    )
    if data is None:
        return []  # request failed (e.g. transient timeout) -- do NOT cache, retry next call
    people = data.get("people", [])
    batters = [
        {"id": p["id"], "fullName": p["fullName"]}
        for p in people
        if p.get("id") and p.get("primaryPosition", {}).get("abbreviation") != "P"
    ]

    cache_file.write_text(json.dumps(batters), encoding="utf-8")
    return batters[:limit] if limit else batters


def fetch_batter_gamelog(player_id: int, season: int) -> list[dict]:
    """
    Return this player's full `season` batting gamelog as a list of
    {date, is_home, opponent_id, stat} dicts, sorted ascending by date.
    A finished past season's gamelog is immutable, so this cache never
    expires.
    """
    cache_file = DATA_DIR / f"gamelog_{player_id}_{season}.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text(encoding="utf-8"))

    data = stats_mlb._get(
        f"/people/{player_id}/stats",
        {"stats": "gameLog", "group": "hitting", "season": season, "sportId": 1},
        cache_key=None,
    )
    if data is None:
        return []  # request failed -- do NOT cache, retry next call
    raw_splits = (data.get("stats") or [{}])[0].get("splits", [])

    games = [
        {
            "date": s.get("date"),
            "is_home": bool(s.get("isHome")),
            "opponent_id": (s.get("opponent") or {}).get("id"),
            # team_id is the player's OWN team for this specific game (not a
            # season-long roster lookup) -- correctly follows a mid-season
            # trade. game_pk feeds resolve_starters.py's opposing-starter
            # lookup (one boxscore fetch per distinct game, not per row).
            "team_id": (s.get("team") or {}).get("id"),
            "game_pk": (s.get("game") or {}).get("gamePk"),
            "stat": s.get("stat") or {},
        }
        for s in raw_splits
        if s.get("date")
    ]
    games.sort(key=lambda g: g["date"])

    cache_file.write_text(json.dumps(games), encoding="utf-8")
    return games


def fetch_all_gamelogs(season: int, limit: int | None = None, progress: bool = True) -> dict:
    """
    Fetch every batter's gamelog for `season`.
    Returns {player_id: {"fullName":, "games": [...]}}.
    Slow on a cold cache (one MLB Stats API call per player); every
    subsequent run for the same season is instant (all cache hits).
    """
    batters = fetch_season_batters(season, limit=limit)
    out = {}
    for i, b in enumerate(batters):
        games = fetch_batter_gamelog(b["id"], season)
        if games:
            out[b["id"]] = {"fullName": b["fullName"], "games": games}
        if progress and (i + 1) % 25 == 0:
            print(f"  [{season}] fetched {i + 1}/{len(batters)} batters "
                  f"({len(out)} with a game log so far)")
    return out


# ── Pitchers (separate cache namespace -- doesn't touch the batter cache above) ──

def fetch_season_pitchers(season: int, limit: int | None = None) -> list[dict]:
    """Same idea as fetch_season_batters, but the mirror-image position filter."""
    cache_file = DATA_DIR / f"season_pitchers_{season}.json"
    if cache_file.exists():
        pitchers = json.loads(cache_file.read_text(encoding="utf-8"))
        return pitchers[:limit] if limit else pitchers

    data = stats_mlb._get(
        "/sports/1/players",
        {"season": season, "hydrate": "currentTeam"},
        cache_key=None,
    )
    if data is None:
        return []  # request failed -- do NOT cache, retry next call
    people = data.get("people", [])
    pitchers = [
        {"id": p["id"], "fullName": p["fullName"]}
        for p in people
        if p.get("id") and p.get("primaryPosition", {}).get("abbreviation") == "P"
    ]

    cache_file.write_text(json.dumps(pitchers), encoding="utf-8")
    return pitchers[:limit] if limit else pitchers


def fetch_pitcher_gamelog(player_id: int, season: int) -> list[dict]:
    """Same shape as fetch_batter_gamelog, but group="pitching" -- a
    pitcher's gameLog `stat` block carries strikeOuts/hits/earnedRuns/
    inningsPitched instead of batting fields."""
    cache_file = DATA_DIR / f"gamelog_pitching_{player_id}_{season}.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text(encoding="utf-8"))

    data = stats_mlb._get(
        f"/people/{player_id}/stats",
        {"stats": "gameLog", "group": "pitching", "season": season, "sportId": 1},
        cache_key=None,
    )
    if data is None:
        return []  # request failed -- do NOT cache, retry next call
    raw_splits = (data.get("stats") or [{}])[0].get("splits", [])

    games = [
        {
            "date": s.get("date"),
            "is_home": bool(s.get("isHome")),
            "opponent_id": (s.get("opponent") or {}).get("id"),
            "team_id": (s.get("team") or {}).get("id"),
            "game_pk": (s.get("game") or {}).get("gamePk"),
            "stat": s.get("stat") or {},
        }
        for s in raw_splits
        if s.get("date")
    ]
    games.sort(key=lambda g: g["date"])

    cache_file.write_text(json.dumps(games), encoding="utf-8")
    return games


def fetch_all_pitcher_gamelogs(season: int, limit: int | None = None, progress: bool = True) -> dict:
    pitchers = fetch_season_pitchers(season, limit=limit)
    out = {}
    for i, p in enumerate(pitchers):
        games = fetch_pitcher_gamelog(p["id"], season)
        if games:
            out[p["id"]] = {"fullName": p["fullName"], "games": games}
        if progress and (i + 1) % 25 == 0:
            print(f"  [{season}] fetched {i + 1}/{len(pitchers)} pitchers "
                  f"({len(out)} with a game log so far)")
    return out
