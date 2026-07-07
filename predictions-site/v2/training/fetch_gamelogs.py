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
    people = (data or {}).get("people", [])
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
    raw_splits = ((data or {}).get("stats") or [{}])[0].get("splits", [])

    games = [
        {
            "date": s.get("date"),
            "is_home": bool(s.get("isHome")),
            "opponent_id": (s.get("opponent") or {}).get("id"),
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
