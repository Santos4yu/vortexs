"""
Vortex — WNBA Stats Engine
===========================
Pulls player & team data from ESPN's public hidden API.

  • No API key required.
  • No proxy required — ESPN's endpoints are not IP/fingerprint blocked the way
    stats.nba.com is, so WNBA works where the NBA engine is currently disabled.

Public functions
----------------
  get_player_id(player_name)                    -> dict | None   {id, team_id, abbr}
  get_player_gamelog(athlete_id)                -> list[dict]     recent games, newest first
  get_historical_splits(athlete_id, line, prop) -> dict           L5/L10/L20 hit-rate engine
  get_todays_schedule()                         -> list[dict]     tonight's games + status
  get_team_stats(team_id)                       -> dict           pace / defensive profile
  get_opponent_lookup(schedule)                 -> dict[abbr, dict]

Supported prop_type values
--------------------------
  "points"       PTS
  "rebounds"     REB
  "assists"      AST
  "threes"       3PM
  "pts_reb_ast"  PTS + REB + AST
  "steals"       STL
  "blocks"       BLK
"""

import io
import sys
import json
import time
import logging
import unicodedata
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

import requests

if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

log = logging.getLogger("vortex.stats_wnba")

# ── Constants ────────────────────────────────────────────────────────────────
SITE   = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba"
COMMON = "https://site.api.espn.com/apis/common/v3/sports/basketball/wnba"
SEASON = datetime.now(timezone.utc).year

CACHE_DIR = Path(__file__).parent / "cache" / "wnba_stats"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

TIMEOUT       = 15
REQUEST_DELAY = 0.25   # ESPN is tolerant, but stay polite

_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
})

# ESPN labels the WNBA season by its starting year. The active season is detected
# at runtime (clock can drift from the data), then the season that actually
# returns gamelog data is memoized so we don't probe on every call.
_SEASON_CACHE: Optional[int] = None

# ESPN gamelog stat-array index map (from `labels`):
# ['MIN','PTS','REB','AST','STL','BLK','TO','FG','FG%','3PT','3P%','FT','FT%','PF']
_IDX = {"min": 0, "pts": 1, "reb": 2, "ast": 3, "stl": 4, "blk": 5, "tpt": 9}

# prop_type → callable(stats_array) → numeric game value
PROP_VALUE = {
    "points":      lambda s: _num(s[_IDX["pts"]]),
    "rebounds":    lambda s: _num(s[_IDX["reb"]]),
    "assists":     lambda s: _num(s[_IDX["ast"]]),
    "steals":      lambda s: _num(s[_IDX["stl"]]),
    "blocks":      lambda s: _num(s[_IDX["blk"]]),
    "threes":      lambda s: _made(s[_IDX["tpt"]]),
    "pts_reb_ast": lambda s: _num(s[_IDX["pts"]]) + _num(s[_IDX["reb"]]) + _num(s[_IDX["ast"]]),
    "pts_reb":     lambda s: _num(s[_IDX["pts"]]) + _num(s[_IDX["reb"]]),
    "pts_ast":     lambda s: _num(s[_IDX["pts"]]) + _num(s[_IDX["ast"]]),
    "reb_ast":     lambda s: _num(s[_IDX["reb"]]) + _num(s[_IDX["ast"]]),
}

PROP_LABEL = {
    "points": "Points", "rebounds": "Rebounds", "assists": "Assists",
    "threes": "3-Pointers Made", "pts_reb_ast": "Pts + Reb + Ast",
    "pts_reb": "Pts + Reb", "pts_ast": "Pts + Ast", "reb_ast": "Reb + Ast",
    "steals": "Steals", "blocks": "Blocks",
}

# Odds API market key → prop_type
MARKET_TO_PROP_TYPE = {
    "player_points":                  "points",
    "player_rebounds":                "rebounds",
    "player_assists":                 "assists",
    "player_points_rebounds_assists": "pts_reb_ast",
    "player_points_rebounds":         "pts_reb",
    "player_points_assists":          "pts_ast",
    "player_rebounds_assists":        "reb_ast",
    "player_threes":                  "threes",
}

# ── Helpers ──────────────────────────────────────────────────────────────────

def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0

def _made(v) -> float:
    """ESPN stores '3PT' as 'made-attempted' (e.g. '2-5'). Return made."""
    try:
        return float(str(v).split("-")[0])
    except (TypeError, ValueError, IndexError):
        return 0.0

def _norm(name: str) -> str:
    """Lowercase, strip accents/punctuation for fuzzy name matching."""
    name = unicodedata.normalize("NFKD", name or "")
    name = "".join(c for c in name if not unicodedata.combining(c))
    return "".join(c for c in name.lower() if c.isalnum() or c == " ").strip()

def _get_cached(key: str, ttl_min: int = 360):
    """Read a computed (non-HTTP) object from the file cache, or None if stale."""
    cf = CACHE_DIR / f"{key}.json"
    if cf.exists() and (time.time() - cf.stat().st_mtime) / 60 < ttl_min:
        try:
            return json.loads(cf.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def _set_cached(key: str, obj) -> None:
    try:
        (CACHE_DIR / f"{key}.json").write_text(json.dumps(obj), encoding="utf-8")
    except Exception:
        pass


def _get(url: str, cache_key: str = None, ttl_min: int = 360) -> Optional[dict]:
    """GET an ESPN endpoint with a simple file cache (TTL in minutes)."""
    if cache_key:
        cf = CACHE_DIR / f"{cache_key}.json"
        if cf.exists():
            age_min = (time.time() - cf.stat().st_mtime) / 60
            if age_min < ttl_min:
                try:
                    return json.loads(cf.read_text(encoding="utf-8"))
                except Exception:
                    pass
    try:
        time.sleep(REQUEST_DELAY)
        r = _SESSION.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        if cache_key:
            (CACHE_DIR / f"{cache_key}.json").write_text(
                json.dumps(data), encoding="utf-8")
        return data
    except requests.RequestException as exc:
        log.warning("ESPN WNBA request failed: %s (%s)", url, exc)
        return None

# ── Roster index (player name → athlete id) ──────────────────────────────────

def _build_roster_index() -> dict:
    """
    Build {normalized_name: {id, name, team_id, abbr}} across all WNBA teams.
    Cached 12h — rosters change rarely intra-day.
    """
    idx = {}
    teams_data = _get(f"{SITE}/teams", cache_key="teams", ttl_min=720)
    if not teams_data:
        return idx
    try:
        teams = teams_data["sports"][0]["leagues"][0]["teams"]
    except (KeyError, IndexError):
        return idx
    for t in teams:
        team = t.get("team", {})
        tid  = team.get("id")
        abbr = team.get("abbreviation", "")
        roster = _get(f"{SITE}/teams/{tid}/roster",
                      cache_key=f"roster_{tid}", ttl_min=720)
        if not roster:
            continue
        for a in roster.get("athletes", []):
            nm = a.get("displayName", "")
            if not nm:
                continue
            idx[_norm(nm)] = {"id": a.get("id"), "name": nm,
                              "team_id": tid, "abbr": abbr}
    return idx

def get_player_id(player_name: str) -> Optional[dict]:
    """Resolve an Odds-API player name to {id, name, team_id, abbr} via fuzzy match."""
    idx = _build_roster_index()
    if not idx:
        return None
    target = _norm(player_name)
    if target in idx:
        return idx[target]
    # fuzzy fallback — best ratio above 0.82
    best, best_score = None, 0.0
    for key, val in idx.items():
        sc = SequenceMatcher(None, target, key).ratio()
        if sc > best_score:
            best, best_score = val, sc
    return best if best_score >= 0.82 else None

# ── Player game log ──────────────────────────────────────────────────────────

def _season_candidates() -> list[int]:
    """Season-year guesses, best first. Detected season then the prior year."""
    global _SEASON_CACHE
    if _SEASON_CACHE:
        return [_SEASON_CACHE]
    detected = SEASON
    sb = _get(f"{SITE}/scoreboard", cache_key="scoreboard", ttl_min=15)
    try:
        detected = int(sb["leagues"][0]["season"]["year"])
    except (KeyError, IndexError, TypeError, ValueError):
        pass
    return [detected, detected - 1]


def _fetch_gamelog_raw(athlete_id) -> Optional[dict]:
    """Fetch the gamelog, trying season candidates until one returns events."""
    global _SEASON_CACHE
    for season in _season_candidates():
        data = _get(f"{COMMON}/athletes/{athlete_id}/gamelog?season={season}",
                    cache_key=f"gamelog_{athlete_id}_{season}", ttl_min=180)
        if data and any(c.get("events")
                        for st in data.get("seasonTypes", [])
                        for c in st.get("categories", [])):
            _SEASON_CACHE = season   # lock in the working season
            return data
    return None


def get_player_gamelog(athlete_id) -> list[dict]:
    """
    Return per-game stat dicts, newest first:
      {event_id, date, min, pts, reb, ast, stl, blk, threes}
    """
    data = _fetch_gamelog_raw(athlete_id)
    if not data:
        return []
    meta = data.get("events", {})  # event_id → {date, ...}
    games = []
    for st in data.get("seasonTypes", []):
        # Regular season only (skip preseason/postseason aggregates as needed)
        for cat in st.get("categories", []):
            for ev in cat.get("events", []):
                eid  = ev.get("eventId")
                arr  = ev.get("stats", [])
                if not eid or len(arr) < 10:
                    continue
                _m   = meta.get(eid, {}) or {}
                date = _m.get("gameDate") or _m.get("date") or ""
                # atVs: "vs" = home game, "@" = road game
                _atvs = str(_m.get("atVs", "")).strip().lower()
                is_home = True if _atvs == "vs" else (False if _atvs == "@" else None)
                games.append({
                    "event_id": eid,
                    "date":     date,
                    "is_home":  is_home,
                    "opponent": _m.get("opponent", {}).get("abbreviation", "") if isinstance(_m.get("opponent"), dict) else "",
                    "min":      _num(arr[_IDX["min"]]),
                    "pts":      _num(arr[_IDX["pts"]]),
                    "reb":      _num(arr[_IDX["reb"]]),
                    "ast":      _num(arr[_IDX["ast"]]),
                    "stl":      _num(arr[_IDX["stl"]]),
                    "blk":      _num(arr[_IDX["blk"]]),
                    "threes":   _made(arr[_IDX["tpt"]]),
                    "_stats":   arr,
                })
    # De-dup by event, newest first (ESPN dates are ISO; fall back to insertion)
    seen, out = set(), []
    for g in sorted(games, key=lambda x: x["date"], reverse=True):
        if g["event_id"] in seen:
            continue
        seen.add(g["event_id"])
        out.append(g)
    return out

# ── Historical splits — the core hit-rate engine ─────────────────────────────

def _window(values: list[float], line: float) -> dict:
    """Over hit-rate + average over a list of game values vs a prop line."""
    g = len(values)
    if g == 0:
        return {"hits": 0, "games": 0, "rate": 0, "avg": 0}
    hits = sum(1 for v in values if v > line)
    return {
        "hits":  hits,
        "games": g,
        "rate":  round(hits / g * 100, 1),
        "avg":   round(sum(values) / g, 2),
    }

def get_historical_splits(athlete_id, line: float, prop_type: str) -> dict:
    """
    L5 / L10 / L20 Over hit-rates + averages for a prop, plus minutes context.
    Shape mirrors stats_mlb.get_historical_splits so it flows into the scorer/embed.
    """
    valfn = PROP_VALUE.get(prop_type)
    if valfn is None:
        return {}
    log_games = get_player_gamelog(athlete_id)
    if not log_games:
        return {}

    vals = [valfn(g["_stats"]) for g in log_games]   # newest first
    mins = [g["min"] for g in log_games]

    recent = [{"value": v, "date": g["date"], "min": g["min"]}
              for v, g in zip(vals, log_games)][:20]

    # Home/away splits (last 20) — venue context like the MLB card
    _paired   = list(zip(vals, log_games))[:20]
    home_vals = [v for v, g in _paired if g.get("is_home") is True]
    away_vals = [v for v, g in _paired if g.get("is_home") is False]

    def _avg(xs):
        return round(sum(xs) / len(xs), 1) if xs else None

    def _rate(xs):
        return round(sum(1 for v in xs if v > line) / len(xs) * 100, 1) if xs else None

    season_avg = round(sum(vals) / len(vals), 2) if vals else 0
    return {
        "l5":            _window(vals[:5],  line),
        "l10":           _window(vals[:10], line),
        "l20":           _window(vals[:20], line),
        "season_avg":    season_avg,
        "games_played":  len(vals),
        "recent_games":  recent,
        # WNBA-specific: minutes floor is the single biggest props gate
        "min_l5":        round(sum(mins[:5]) / min(5, len(mins)), 1) if mins else 0,
        "min_l10":       round(sum(mins[:10]) / min(10, len(mins)), 1) if mins else 0,
        # Venue splits
        "home_avg":      _avg(home_vals),
        "home_games":    len(home_vals),
        "home_rate":     _rate(home_vals),
        "away_avg":      _avg(away_vals),
        "away_games":    len(away_vals),
        "away_rate":     _rate(away_vals),
    }

# ── Schedule ─────────────────────────────────────────────────────────────────

def get_todays_schedule() -> list[dict]:
    """
    Upcoming WNBA games: home/away abbreviations, team ids, status, start time.
    status state ∈ {"pre", "in", "post"}.

    Fetches TODAY + TOMORROW (US/Eastern) and merges them. ESPN's default
    scoreboard only returns the current calendar day, so late at night — once
    today's games are final but tomorrow's haven't "started" — the board would
    otherwise have zero upcoming games to match the Odds API's next-day props.
    Looking one day ahead keeps WNBA plays flowing across the day boundary.
    """
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td

    # US/Eastern calendar days (UTC-4 EDT / UTC-5 EST — fixed -4 is fine here:
    # the only effect is which date label we request, and we request a 2-day
    # window so a 1-hour DST skew can't drop a game).
    _et_now = _dt.now(_tz(_td(hours=-4)))
    days = [
        ("scoreboard",        None),                                   # ESPN default = today
        (f"scoreboard_{(_et_now + _td(days=1)).strftime('%Y%m%d')}",
         (_et_now + _td(days=1)).strftime("%Y%m%d")),                  # tomorrow
    ]

    out: list[dict] = []
    seen: set = set()
    for cache_key, datestr in days:
        url = f"{SITE}/scoreboard"
        if datestr:
            url += f"?dates={datestr}"
        data = _get(url, cache_key=cache_key, ttl_min=15)
        if not data:
            continue
        for ev in data.get("events", []):
            try:
                ev_id = ev.get("id")
                if ev_id in seen:
                    continue
                comp = ev["competitions"][0]
                state = ev["status"]["type"]["state"]
                home = away = None
                for c in comp["competitors"]:
                    side = {"id": c["team"]["id"], "abbr": c["team"].get("abbreviation", ""),
                            "name": c["team"].get("displayName", "")}
                    if c["homeAway"] == "home":
                        home = side
                    else:
                        away = side
                if home and away:
                    seen.add(ev_id)
                    out.append({
                        "event_id":      ev_id,
                        "commence_time": ev.get("date", ""),
                        "state":         state,
                        "home":          home,
                        "away":          away,
                    })
            except (KeyError, IndexError):
                continue
    return out

def get_opponent_lookup(schedule: list[dict]) -> dict:
    """Map team abbr → {opp_abbr, opp_id, is_home, commence_time, state}."""
    lookup = {}
    for g in schedule:
        lookup[g["home"]["abbr"]] = {
            "opp_abbr": g["away"]["abbr"], "opp_id": g["away"]["id"],
            "is_home": True, "commence_time": g["commence_time"], "state": g["state"]}
        lookup[g["away"]["abbr"]] = {
            "opp_abbr": g["home"]["abbr"], "opp_id": g["home"]["id"],
            "is_home": False, "commence_time": g["commence_time"], "state": g["state"]}
    return lookup

# ── Team stats (opponent defense / pace) ─────────────────────────────────────

def get_team_stats(team_id) -> dict:
    """
    Opponent-context stats for matchup filtering: points allowed, pace proxy.
    Returns {} when unavailable so the scorer treats it as neutral.
    """
    data = _get(f"{SITE}/teams/{team_id}/statistics",
                cache_key=f"teamstats_{team_id}", ttl_min=360)
    if not data:
        return {}
    out = {}
    try:
        cats = data["results"]["stats"]["categories"]
        for cat in cats:
            for s in cat.get("stats", []):
                nm = s.get("name")
                if nm:
                    out[nm] = s.get("value")
    except (KeyError, TypeError):
        return {}
    return out


def get_team_pace(team_id) -> Optional[float]:
    """
    Estimated possessions per game (pace). Higher pace = more counting-stat volume.
    poss ≈ FGA − OREB + TO + 0.44·FTA   (per game, from season avgs)
    """
    ts = get_team_stats(team_id)
    if not ts:
        return None
    try:
        fga = float(ts.get("avgFieldGoalsAttempted")
                    or (ts.get("avgTwoPointFieldGoalsAttempted", 0)
                        + ts.get("avgThreePointFieldGoalsAttempted", 0)))
        oreb = float(ts.get("avgOffensiveRebounds", 0))
        to   = float(ts.get("avgTurnovers", 0))
        fta  = float(ts.get("avgFreeThrowsAttempted", 0))
        poss = fga - oreb + to + 0.44 * fta
        return round(poss, 1) if poss > 0 else None
    except (TypeError, ValueError):
        return None


def get_league_avg_pace() -> Optional[float]:
    """League-average pace across all teams — baseline for matchup classification."""
    cached = _get(f"{SITE}/teams", cache_key="teams", ttl_min=720)
    if not cached:
        return None
    try:
        teams = cached["sports"][0]["leagues"][0]["teams"]
    except (KeyError, IndexError):
        return None
    paces = [p for t in teams if (p := get_team_pace(t["team"]["id"])) is not None]
    return round(sum(paces) / len(paces), 1) if paces else None


# ── Opponent defense vs stat (Filter 2) ──────────────────────────────────────

def _all_teams() -> list[dict]:
    """List of {id, abbr} for all WNBA teams."""
    data = _get(f"{SITE}/teams", cache_key="teams", ttl_min=720)
    try:
        return [{"id": t["team"]["id"], "abbr": t["team"]["abbreviation"]}
                for t in data["sports"][0]["leagues"][0]["teams"]]
    except (KeyError, IndexError, TypeError):
        return []


def _game_box(game_id) -> dict:
    """
    Per-team totals for a completed game (cached permanently — finished games
    never change). Returns {abbr: {points, rebounds, assists, threes}}.
    """
    data = _get(f"{SITE}/summary?event={game_id}",
                cache_key=f"box_{game_id}", ttl_min=525600)
    if not data:
        return {}
    out = {}
    # points come from the header score; counting stats from the box statistics
    try:
        for c in data["header"]["competitions"][0]["competitors"]:
            out.setdefault(c["team"]["abbreviation"], {})["points"] = float(c.get("score") or 0)
    except (KeyError, IndexError, TypeError, ValueError):
        return {}
    for t in data.get("boxscore", {}).get("teams", []):
        abbr = t.get("team", {}).get("abbreviation")
        if not abbr:
            continue
        stats = {x.get("name"): x.get("displayValue") for x in t.get("statistics", [])}
        d = out.setdefault(abbr, {})
        d["rebounds"] = _num(stats.get("totalRebounds"))
        d["assists"]  = _num(stats.get("assists"))
        d["threes"]   = _made(stats.get("threePointFieldGoalsMade-threePointFieldGoalsAttempted"))
    return out


def get_defense_ranks() -> dict:
    """
    Rank all teams by how much of each stat they ALLOW to opponents.
    Returns {stat: {abbr: rank}} where rank 1 = stingiest, n = most generous.
    Stats: points, rebounds, assists, threes.  Cached 6h.
    """
    cached = _get_cached("defense_ranks", ttl_min=360)
    if cached is not None:
        return cached

    teams = _all_teams()
    if not teams:
        return {}
    official = {t["abbr"] for t in teams}   # exclude exhibition/international opponents

    # Collect unique completed games across the league.
    games: dict[str, None] = {}
    for t in teams:
        sch = _get(f"{SITE}/teams/{t['id']}/schedule",
                   cache_key=f"sched_{t['id']}", ttl_min=180)
        for ev in (sch or {}).get("events", []):
            try:
                if ev["competitions"][0]["status"]["type"]["state"] == "post":
                    games[ev["id"]] = None
            except (KeyError, IndexError, TypeError):
                continue

    allowed: dict[str, dict[str, float]] = {}   # abbr → {stat: total}
    counts:  dict[str, int] = {}
    for gid in games:
        box = _game_box(gid)
        abbrs = list(box.keys())
        if len(abbrs) != 2:
            continue
        a, b = abbrs
        for team, opp in ((a, b), (b, a)):
            acc = allowed.setdefault(team, {"points": 0, "rebounds": 0, "assists": 0, "threes": 0})
            for stat in acc:
                acc[stat] += box[opp].get(stat, 0)
            counts[team] = counts.get(team, 0) + 1

    # Average allowed per game, then rank.
    ranks: dict[str, dict[str, int]] = {}
    allowed_pg: dict[str, dict[str, float]] = {}   # stat → {abbr: allowed/game}
    league_avg: dict[str, float] = {}              # stat → league mean allowed/game
    for stat in ("points", "rebounds", "assists", "threes"):
        per_game = []
        for abbr, acc in allowed.items():
            if abbr not in official:        # rank only real league teams
                continue
            n = counts.get(abbr, 0)
            if n:
                per_game.append((abbr, acc[stat] / n))
        per_game.sort(key=lambda x: x[1])   # ascending → least allowed first
        ranks[stat] = {abbr: i for i, (abbr, _) in enumerate(per_game, 1)}
        allowed_pg[stat] = {abbr: round(v, 1) for abbr, v in per_game}
        league_avg[stat] = round(sum(v for _, v in per_game) / len(per_game), 1) if per_game else 0.0

    # Sibling keys (underscore-prefixed) so existing {stat: {abbr: rank}} consumers
    # that iterate the four stat names are unaffected.
    ranks["_allowed"]    = allowed_pg
    ranks["_league_avg"] = league_avg

    _set_cached("defense_ranks", ranks)
    return ranks


# Map prop_type → the defensive stat used to rank the opponent.
DEF_STAT_FOR_PROP = {
    "points": "points", "rebounds": "rebounds", "assists": "assists",
    "threes": "threes", "pts_reb_ast": "points",   # points is the best single proxy
    "pts_reb": "points", "pts_ast": "points", "reb_ast": "rebounds",
}


# ── Injuries / lineup context (Filter 5) ─────────────────────────────────────

# Statuses that mean the player will not / likely will not play.
_OUT_STATUSES   = {"out", "injured reserve", "suspension", "doubtful"}
_QUESTIONABLE   = {"questionable", "day-to-day", "game-time decision"}


def get_injuries() -> dict:
    """
    League-wide injury report keyed by ESPN team id (str):
      {team_id: [{"name", "status", "status_norm"}]}
    Cached 45min — injury news moves through the day.
    """
    cached = _get_cached("injuries", ttl_min=45)
    if cached is not None:
        return cached
    data = _get(f"{SITE}/injuries", cache_key="injuries_raw", ttl_min=45)
    out: dict = {}
    for team in (data or {}).get("injuries", []):
        tid = str(team.get("id", ""))
        rows = []
        for inj in team.get("injuries", []):
            ath = inj.get("athlete", {}) or {}
            nm  = ath.get("displayName") or (
                f"{ath.get('firstName','')} {ath.get('lastName','')}".strip())
            status = (inj.get("status") or "").strip()
            if not nm:
                continue
            rows.append({"name": nm, "status": status,
                         "status_norm": status.lower()})
        if rows:
            out[tid] = rows
    _set_cached("injuries", out)
    return out


def player_min_l10(athlete_id) -> float:
    """Average minutes over the player's last 10 games (0 if unknown)."""
    log = get_player_gamelog(athlete_id)
    mins = [g["min"] for g in log][:10]
    return round(sum(mins) / len(mins), 1) if mins else 0.0


def key_teammate_out(team_id, exclude_name: str, starter_min: float = 24.0) -> Optional[str]:
    """
    Return the name of a starter-level teammate (≥ starter_min L10 minutes) who is
    OUT, or None. Used to flag a usage bump for the remaining player.
    """
    inj = get_injuries().get(str(team_id), [])
    for entry in inj:
        if entry["name"].lower() == (exclude_name or "").lower():
            continue
        if entry["status_norm"] not in _OUT_STATUSES:
            continue
        pid = get_player_id(entry["name"])
        if pid and player_min_l10(pid["id"]) >= starter_min:
            return entry["name"]
    return None


def player_injury_status(team_id, player_name: str) -> Optional[str]:
    """Return 'out' / 'questionable' / None for a specific player tonight."""
    inj = get_injuries().get(str(team_id), [])
    for entry in inj:
        if entry["name"].lower() == (player_name or "").lower():
            if entry["status_norm"] in _OUT_STATUSES:
                return "out"
            if entry["status_norm"] in _QUESTIONABLE:
                return "questionable"
    return None


def is_back_to_back(team_id, game_iso: str) -> bool:
    """True if the team played a game the calendar day before `game_iso`."""
    if not game_iso:
        return False
    try:
        game_day = datetime.fromisoformat(game_iso.replace("Z", "+00:00")).date()
    except ValueError:
        return False
    sch = _get(f"{SITE}/teams/{team_id}/schedule",
               cache_key=f"sched_{team_id}", ttl_min=180)
    for ev in (sch or {}).get("events", []):
        try:
            d = datetime.fromisoformat(ev["date"].replace("Z", "+00:00")).date()
        except (KeyError, ValueError, TypeError):
            continue
        if (game_day - d).days == 1:
            return True
    return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="  %(levelname)s  %(message)s")
    print("Schedule:")
    sched = get_todays_schedule()
    for g in sched:
        print(f"  {g['away']['abbr']} @ {g['home']['abbr']}  [{g['state']}]  {g['commence_time']}")
    print("\nPlayer lookup test: 'Napheesa Collier'")
    p = get_player_id("Napheesa Collier")
    print(" ", p)
    if p:
        sp = get_historical_splits(p["id"], 19.5, "points")
        print("  Points 19.5 splits:")
        print("   L5 :", sp.get("l5"))
        print("   L10:", sp.get("l10"))
        print("   L20:", sp.get("l20"))
        print("   season_avg:", sp.get("season_avg"), "min_l10:", sp.get("min_l10"))
