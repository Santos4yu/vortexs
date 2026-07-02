"""
Vortex — NBA Stats Engine
==========================
Pulls player & team data from the official NBA Stats API
(stats.nba.com).  No API key required.

Public functions
----------------
  get_player_id(player_name)                       -> int | None
  get_player_current_team(player_id)               -> int | None
  get_historical_splits(player_id, line, prop)     -> dict
  get_opponent_defense(opp_team_id, prop_type)     -> dict
  get_full_card(player_name, opp_team_abbr,
                line, prop_type)                   -> dict   ← main entry point
  get_todays_schedule()                            -> dict
  build_opponent_lookup(schedule)                  -> dict[int, int]

Supported prop_type values
--------------------------
  "points"       PTS ≥ line
  "rebounds"     REB ≥ line  (total)
  "assists"      AST ≥ line
  "pts_reb_ast"  PTS+REB+AST ≥ line
  "threes"       FG3M ≥ line
  "blocks"       BLK ≥ line
  "steals"       STL ≥ line
"""

import io
import sys
import time
import json
import logging
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Optional

import requests

# ── UTF-8 output on Windows (only wrap when run directly) ───────────────────
if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ── Logging ─────────────────────────────────────────────────────────────────
log = logging.getLogger("vortex.stats_nba")
logging.basicConfig(level=logging.INFO, format="  %(levelname)s  %(message)s")

# ── Constants ────────────────────────────────────────────────────────────────
BASE          = "https://stats.nba.com/stats"
SEASON        = "2025-26"
REQUEST_DELAY = 0.6   # NBA Stats API warrants extra courtesy
TIMEOUT       = 12    # reduced — fail fast when NBA is off-season
MAX_RETRIES   = 1
RETRY_WAIT    = 2     # seconds between retry attempts
CACHE_DIR     = Path(__file__).parent / "cache" / "nba_stats"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Full Chrome-replica headers — stats.nba.com does active fingerprinting
# and drops connections that look like scripts. Every field here matches
# what Chrome 124 actually sends when a user browses stats.nba.com.
HEADERS = {
    "User-Agent":          ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0.0.0 Safari/537.36"),
    "Accept":              "application/json, text/plain, */*",
    "Accept-Language":     "en-US,en;q=0.9",
    "Accept-Encoding":     "gzip, deflate, br",
    "Connection":          "keep-alive",
    "DNT":                 "1",
    "Referer":             "https://www.nba.com/",
    "Origin":              "https://www.nba.com",
    "Sec-Fetch-Dest":      "empty",
    "Sec-Fetch-Mode":      "cors",
    "Sec-Fetch-Site":      "same-site",
    "Sec-Ch-Ua":           '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile":    "?0",
    "Sec-Ch-Ua-Platform":  '"Windows"',
    "x-nba-stats-origin":  "stats",
    "x-nba-stats-token":   "true",
}

# Persistent session — carries cookies & keep-alive across all calls,
# which is the single biggest signal to stats.nba.com that this is a
# real browser session rather than a one-shot script.
_SESSION = requests.Session()
_SESSION.headers.update(HEADERS)
_SESSION_WARMED = False   # tracks whether we've done the nba.com cookie handshake


def _warm_session() -> None:
    """
    Hit the nba.com homepage once to collect the cookies that
    stats.nba.com expects to see on every API request.
    This mirrors what a browser does before opening the stats site.
    """
    global _SESSION_WARMED
    if _SESSION_WARMED:
        return
    try:
        log.info("Warming NBA session via nba.com...")
        _SESSION.get("https://www.nba.com/", timeout=TIMEOUT)
        _SESSION_WARMED = True
        log.info("Session warmed — cookies: %s", list(_SESSION.cookies.keys()))
        time.sleep(0.5)   # brief pause after landing page, like a human would
    except requests.RequestException as exc:
        log.warning("Session warm-up failed (continuing anyway): %s", exc)

# prop_type → (game_log_column, display_label, opponent_defense_column)
PROP_STAT_MAP = {
    "points":      ("PTS",  "Points",             "OPP_PTS"),
    "rebounds":    ("REB",  "Rebounds",            "OPP_REB"),
    "assists":     ("AST",  "Assists",             "OPP_AST"),
    "threes":      ("FG3M", "3-Pointers Made",     "OPP_FG3M"),
    "blocks":      ("BLK",  "Blocks",              "OPP_BLK"),
    "steals":      ("STL",  "Steals",              "OPP_STL"),
    "pts_reb_ast": (None,   "Pts + Reb + Ast",     None),        # computed
}

# Maps The Odds API market key → stats_nba prop_type
MARKET_TO_PROP_TYPE = {
    "player_points":                  "points",
    "player_rebounds":                "rebounds",
    "player_assists":                 "assists",
    "player_points_rebounds_assists": "pts_reb_ast",
    "player_threes":                  "threes",
    "player_blocks":                  "blocks",
    "player_steals":                  "steals",
}

# ── HTTP helpers ─────────────────────────────────────────────────────────────

def _get(endpoint: str, params: dict = None,
         cache_key: str = None) -> Optional[dict]:
    """
    GET a NBA Stats API endpoint.
    Serves from a session-level cache file when cache_key is provided.
    """
    if cache_key:
        cache_file = CACHE_DIR / f"{cache_key}.json"
        if cache_file.exists():
            with open(cache_file, encoding="utf-8") as f:
                return json.load(f)

    _warm_session()   # ensure cookies are present before first real API call

    url = f"{BASE}/{endpoint}"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            time.sleep(REQUEST_DELAY)
            r = _SESSION.get(url, params=params or {}, timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json()
            if cache_key:
                with open(CACHE_DIR / f"{cache_key}.json", "w", encoding="utf-8") as f:
                    json.dump(data, f)
            return data
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt < MAX_RETRIES:
                log.warning("NBA API timeout/connection error (attempt %d/%d): %s — retrying in %ds",
                            attempt, MAX_RETRIES, exc, RETRY_WAIT)
                time.sleep(RETRY_WAIT)
            else:
                log.warning("NBA API failed after %d attempts: %s  (%s)", MAX_RETRIES, url, exc)
                return None
        except requests.RequestException as exc:
            # Non-retryable errors (4xx, bad JSON, etc.) — fail immediately
            log.warning("NBA API request failed: %s  (%s)", url, exc)
            return None
    return None


def _result_to_dicts(data: dict, result_set_index: int = 0) -> list[dict]:
    """
    Convert an NBA Stats API resultSet (headers + rowSet) to a list of dicts.
    Returns [] on any parse failure.
    """
    try:
        rs      = data["resultSets"][result_set_index]
        headers = rs["headers"]
        return [dict(zip(headers, row)) for row in rs["rowSet"]]
    except (KeyError, IndexError, TypeError):
        return []

# ── 1. Player ID lookup ──────────────────────────────────────────────────────

@lru_cache(maxsize=256)
def get_player_id(player_name: str) -> Optional[int]:
    """
    Return the official NBA person ID for a player name.
    Searches the current-season active roster; falls back to all-time list.
    Returns None if not found.
    """
    data = _get("commonallplayers", {
        "LeagueID":            "00",
        "Season":              SEASON,
        "IsOnlyCurrentSeason": 1,
    }, cache_key=f"nba_allplayers_{SEASON}")

    if not data:
        return None

    players = _result_to_dicts(data)

    def _normalize(s: str) -> str:
        """Strip diacritics and lowercase — maps 'Jokić' → 'jokic'."""
        return unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode().lower().strip()

    needle = _normalize(player_name)

    # Exact match (diacritic-insensitive)
    for p in players:
        if _normalize(p.get("DISPLAY_FIRST_LAST", "")) == needle:
            log.info("Resolved '%s' -> id=%s", player_name, p["PERSON_ID"])
            return int(p["PERSON_ID"])

    # Partial match — every word in the query must appear in the normalized name
    parts = needle.split()
    for p in players:
        full = _normalize(p.get("DISPLAY_FIRST_LAST", ""))
        if all(part in full for part in parts):
            log.info("Fuzzy-resolved '%s' -> %s (id=%s)",
                     player_name, p["DISPLAY_FIRST_LAST"], p["PERSON_ID"])
            return int(p["PERSON_ID"])

    log.warning("Player not found: %s", player_name)
    return None


def get_player_current_team(player_id: int) -> Optional[int]:
    """Return the NBA team ID the player is currently rostered on."""
    data = _get("commonallplayers", {
        "LeagueID":            "00",
        "Season":              SEASON,
        "IsOnlyCurrentSeason": 1,
    }, cache_key=f"nba_allplayers_{SEASON}")

    if not data:
        return None

    for p in _result_to_dicts(data):
        if int(p.get("PERSON_ID", -1)) == player_id:
            team_id = p.get("TEAM_ID")
            return int(team_id) if team_id and int(team_id) != 0 else None
    return None

# ── 2. Historical splits (player game log + hit rate) ───────────────────────

def _stat_from_game(game: dict, prop_type: str) -> float:
    """Extract the numeric prop stat from one game log row dict."""
    if prop_type == "pts_reb_ast":
        return (float(game.get("PTS", 0)) +
                float(game.get("REB", 0)) +
                float(game.get("AST", 0)))
    col, _, _ = PROP_STAT_MAP.get(prop_type, ("PTS", "Points", "OPP_PTS"))
    return float(game.get(col, 0))


def _hit_rate(games: list[dict], line: float,
              prop_type: str, n: int) -> Optional[dict]:
    """
    Hit rate for the most recent N games (games already newest-first).
    Returns None when fewer than n//2 games are available.
    """
    sample = games[:n]
    if len(sample) < max(1, n // 2):
        return None

    values = [_stat_from_game(g, prop_type) for g in sample]
    hits   = sum(1 for v in values if v >= line)
    avg    = round(sum(values) / len(values), 1)

    return {
        "games":  len(sample),
        "hits":   hits,
        "rate":   round(hits / len(sample) * 100, 1),
        "avg":    avg,
        "streak": _current_streak(values, line),
    }


def _current_streak(values: list[float], line: float) -> int:
    """Positive = consecutive overs (newest first). Negative = consecutive unders."""
    if not values:
        return 0
    over   = values[0] >= line
    streak = 0
    for v in values:
        if (v >= line) == over:
            streak += 1
        else:
            break
    return streak if over else -streak


def _fetch_gamelog(player_id: int, season_type: str) -> list[dict]:
    """Fetch a single season-type game log. Returns rows newest-first, or []."""
    data = _get("playergamelog", {
        "PlayerID":   player_id,
        "Season":     SEASON,
        "SeasonType": season_type,
        "LeagueID":   "00",
    }, cache_key=f"nba_gamelog_{player_id}_{SEASON}_{season_type.replace(' ', '_')}")
    if not data:
        return []
    return _result_to_dicts(data) or []


def get_historical_splits(player_id: int, line: float,
                           prop_type: str = "points") -> dict:
    """
    Fetch the player's game log (Regular Season + Playoffs merged, most recent
    first) and compute L5/L10/L20 hit rates.

    Using both season types ensures that during the postseason the L5/L10
    windows reflect actual recent performance rather than stale regular-season
    numbers from months ago.

    Returns a dict with keys:
      l5, l10, l20     — hit rate dicts (games/hits/rate/avg/streak)
      season_avg       — season average for the stat per game
      games_played     — total games in the season
      prop_label       — human-readable prop name
      recent_games     — list of last 5 game summaries
    """
    _, prop_label, _ = PROP_STAT_MAP.get(prop_type, (None, prop_type, None))

    reg_games    = _fetch_gamelog(player_id, "Regular Season")
    playoff_games = _fetch_gamelog(player_id, "Playoffs")

    # Merge: sort by parsed date descending so most recent games come first.
    # GAME_DATE is "Mon DD, YYYY" (e.g. "Jun 10, 2026") — alphabetic sort is
    # unreliable across month-name boundaries, so parse to a real date object.
    from datetime import datetime as _dt
    def _parse_date(g: dict) -> _dt:
        try:
            return _dt.strptime(g.get("GAME_DATE", ""), "%b %d, %Y")
        except ValueError:
            return _dt.min

    all_games = sorted(
        reg_games + playoff_games,
        key=_parse_date,
        reverse=True,
    )

    if not all_games:
        return {"error": "No game log data found for this season"}

    # Season average uses regular season only (bigger sample, stable baseline).
    # Hit rates use the merged list so L5/L10/L20 reflect actual recent games
    # including playoff rounds.
    reg_played = len(reg_games)
    src = reg_games if reg_games else all_games
    if prop_type == "pts_reb_ast":
        total = sum(_stat_from_game(g, prop_type) for g in src)
    else:
        col = PROP_STAT_MAP[prop_type][0] if prop_type in PROP_STAT_MAP else "PTS"
        total = sum(float(g.get(col, 0)) for g in src)
    season_avg = round(total / max(len(src), 1), 1)

    # Recent game summaries from merged list — shows true most recent games
    recent = []
    for g in all_games[:5]:
        val = _stat_from_game(g, prop_type)
        matchup = g.get("MATCHUP", "")
        recent.append({
            "date":     g.get("GAME_DATE", ""),
            "opponent": matchup,
            "value":    val,
            "over":     val >= line,
            "wl":       g.get("WL", ""),
        })

    return {
        "player_id":    player_id,
        "prop_type":    prop_type,
        "prop_label":   prop_label,
        "line":         line,
        "season_avg":   season_avg,
        "games_played": reg_played,
        "l5":           _hit_rate(all_games, line, prop_type, 5),
        "l10":          _hit_rate(all_games, line, prop_type, 10),
        "l20":          _hit_rate(all_games, line, prop_type, 20),
        "recent_games": recent,
    }

# ── 3. Opponent team defensive profile ───────────────────────────────────────

# Hardcoded abbreviations keyed by the last word of the team name (nickname).
# Used only when the API response omits TEAM_ABBREVIATION.
_NICKNAME_TO_ABBR = {
    "Hawks": "ATL", "Celtics": "BOS", "Nets": "BKN", "Hornets": "CHA",
    "Bulls": "CHI", "Cavaliers": "CLE", "Mavericks": "DAL", "Nuggets": "DEN",
    "Pistons": "DET", "Warriors": "GSW", "Rockets": "HOU", "Pacers": "IND",
    "Clippers": "LAC", "Lakers": "LAL", "Grizzlies": "MEM", "Heat": "MIA",
    "Bucks": "MIL", "Timberwolves": "MIN", "Pelicans": "NOP", "Knicks": "NYK",
    "Thunder": "OKC", "Magic": "ORL", "76ers": "PHI", "Suns": "PHX",
    "Blazers": "POR", "Kings": "SAC", "Spurs": "SAS", "Raptors": "TOR",
    "Jazz": "UTA", "Wizards": "WAS",
}

def _abbr_from_name(team_name: str) -> str:
    """Derive a 3-letter abbreviation from the team's full name."""
    nickname = team_name.strip().split()[-1] if team_name.strip() else ""
    return _NICKNAME_TO_ABBR.get(nickname, nickname[:3].upper() or "???")


def _get_team_defense_stats() -> list[dict]:
    """
    Fetch league-wide opponent stats per team (what each team allows per game).
    Cached once per session.
    """
    # The NBA Stats API returns 500 if any of these "optional" params are absent.
    # Every field here matches what the nba.com stats page sends in its XHR.
    data = _get("leaguedashteamstats", {
        "Season":        SEASON,
        "SeasonType":    "Regular Season",
        "MeasureType":   "Opponent",
        "PerMode":       "PerGame",
        "LeagueID":      "00",
        "LastNGames":    0,
        "Month":         0,
        "OpponentTeamID": 0,
        "PaceAdjust":    "N",
        "PlusMinus":     "N",
        "Rank":          "N",
        "Period":        0,
        "GameScope":     "",
        "PlayerExperience": "",
        "PlayerPosition": "",
        "StarterBench":  "",
    }, cache_key=f"nba_team_defense_{SEASON}")

    return _result_to_dicts(data) if data else []


def get_opponent_defense(opp_team_id: int, prop_type: str) -> dict:
    """
    Return the opponent team's defensive profile for the given prop type.
    This is the NBA analog of get_pitcher_metrics() in stats_mlb.py.

    Returns a dict with keys:
      team_id, team_name, team_abbr
      avg_allowed          — opponent avg per game for this stat
      league_rank          — 1 = best defense (allows fewest), 30 = worst
      recent_games_allowed — list of last 5 game-by-game values allowed
      def_rating           — overall defensive rating if available
    """
    _, prop_label, opp_col = PROP_STAT_MAP.get(
        prop_type, (None, prop_type, "OPP_PTS"))

    team_rows = _get_team_defense_stats()
    if not team_rows:
        return {"error": "Could not fetch team defense stats"}

    # Find the target team
    target = None
    for row in team_rows:
        if int(row.get("TEAM_ID", -1)) == opp_team_id:
            target = row
            break

    if target is None:
        return {"error": f"Team ID {opp_team_id} not found in defensive stats"}

    # Determine the relevant allowed column for pts_reb_ast (sum components)
    if prop_type == "pts_reb_ast" or opp_col is None:
        avg_allowed = (
            float(target.get("OPP_PTS", 0)) +
            float(target.get("OPP_REB", 0)) +
            float(target.get("OPP_AST", 0))
        )
        avg_allowed = round(avg_allowed, 1)
    else:
        avg_allowed = round(float(target.get(opp_col, 0)), 1)

    # League rank: sort all teams by this column (ascending = best defense)
    if prop_type == "pts_reb_ast" or opp_col is None:
        ranked = sorted(team_rows, key=lambda r: (
            float(r.get("OPP_PTS", 0)) +
            float(r.get("OPP_REB", 0)) +
            float(r.get("OPP_AST", 0))
        ))
    else:
        ranked = sorted(team_rows, key=lambda r: float(r.get(opp_col, 0)))

    rank = next(
        (i + 1 for i, r in enumerate(ranked)
         if int(r.get("TEAM_ID", -1)) == opp_team_id),
        None
    )

    # Recent game-by-game opponent performance for this team
    recent_allowed = _get_recent_opponent_games(opp_team_id, prop_type)

    # leaguedashteamstats Opponent measure omits TEAM_ABBREVIATION —
    # derive it from whichever abbreviation column the response actually includes.
    abbr = (target.get("TEAM_ABBREVIATION")
            or target.get("ABBREVIATION")
            or _abbr_from_name(target.get("TEAM_NAME", "")))

    return {
        "team_id":        opp_team_id,
        "team_name":      target.get("TEAM_NAME", "Unknown"),
        "team_abbr":      abbr,
        "prop_label":     prop_label,
        "avg_allowed":    avg_allowed,
        "league_rank":    rank,      # 1 = stingiest, 30 = most generous
        "def_rating":     target.get("DEF_RATING"),
        "recent_allowed": recent_allowed,
    }


def _get_recent_opponent_games(team_id: int, prop_type: str,
                                n: int = 5) -> list[dict]:
    """
    Pull the team's last N games and return what the OPPONENT averaged
    in the relevant stat. Uses the team's game log from the opponent side.
    """
    _, _, opp_col = PROP_STAT_MAP.get(prop_type, (None, None, "OPP_PTS"))

    data = _get("teamgamelog", {
        "TeamID":     team_id,
        "Season":     SEASON,
        "SeasonType": "Regular Season",
    }, cache_key=f"nba_teamgamelog_{team_id}_{SEASON}")

    if not data:
        return []

    games = _result_to_dicts(data)[:n]
    results = []
    for g in games:
        matchup = g.get("MATCHUP", "")
        # pts allowed is total game pts for the opponent = total pts - team pts
        # For simplicity, note this endpoint doesn't carry opp breakdown;
        # we surface game-level context (win/loss + pts allowed via score)
        results.append({
            "date":    g.get("GAME_DATE", ""),
            "matchup": matchup,
            "wl":      g.get("WL", ""),
            "pts":     int(g.get("PTS", 0)),    # this team's pts (not opponent)
        })
    return results

# ── 4. Full card (main entry point) ──────────────────────────────────────────

def get_full_card(player_name: str, opp_team_id: int,
                  line: float, prop_type: str = "points") -> dict:
    """
    Assemble a complete analytical card for an NBA prop:
      - player splits (L5/L10/L20)
      - opponent team defensive profile
      - trend signal
      - confidence tier

    Returns a unified dict ready to enrich update_board.py summaries.
    """
    log.info("Building card: %s vs team_id=%s  |  %s O%.1f",
             player_name, opp_team_id, prop_type, line)

    player_id = get_player_id(player_name)
    if player_id is None:
        return {"error": f"Player not found: {player_name}"}

    splits   = get_historical_splits(player_id, line, prop_type)
    if "error" in splits:
        return {"error": splits["error"]}

    defense  = get_opponent_defense(opp_team_id, prop_type)
    trend    = _trend_signal(splits)
    tier     = _confidence_tier(splits, defense, trend)

    return {
        "player_name":  player_name,
        "player_id":    player_id,
        "prop_type":    prop_type,
        "prop_label":   splits.get("prop_label", prop_type),
        "line":         line,
        "splits":       splits,
        "defense":      defense,
        "trend_signal": trend,
        "tier":         tier,
    }


def _trend_signal(splits: dict) -> str:
    l5  = splits.get("l5")
    l10 = splits.get("l10")
    l20 = splits.get("l20")
    if not l5 or not l10:
        return "insufficient data"

    r5     = l5["rate"]
    r10    = l10["rate"]
    r20    = l20["rate"] if l20 else r10
    streak = l5.get("streak", 0)

    if r5 >= 80 and r10 >= 70:
        trend = "HOT — elite recent form"
    elif r5 >= 60 and r10 >= 60:
        trend = "WARM — consistent recent form"
    elif r5 < 40 and r10 < 40:
        trend = "COLD — struggling recently"
    elif r5 > r20 + 15:
        trend = "IMPROVING — trending up over season baseline"
    elif r5 < r20 - 15:
        trend = "FADING — trending down from season baseline"
    else:
        trend = "NEUTRAL — no strong directional signal"

    if streak >= 4:
        trend += f" | Active {streak}-game hit streak"
    elif streak <= -4:
        trend += f" | Active {abs(streak)}-game miss streak"

    return trend


def _confidence_tier(splits: dict, defense: dict, trend: str) -> str:
    """
    Assign ELITE / STRONG / LEAN / PASS based on multiple signals.
    Defense rank is the NBA analog of pitcher ERA tier.
    """
    score = 0

    l10 = splits.get("l10")
    l5  = splits.get("l5")
    if l10 and l10["rate"] >= 70: score += 3
    elif l10 and l10["rate"] >= 60: score += 2
    elif l10 and l10["rate"] >= 50: score += 1

    if l5 and l5["rate"] >= 80: score += 2
    elif l5 and l5["rate"] >= 60: score += 1

    # Defensive rank — higher rank (worse defense) benefits the over
    rank = defense.get("league_rank")
    if rank is not None:
        if rank >= 25:   score += 2   # bottom-5 defense: very generous
        elif rank >= 20: score += 1   # bottom-third
        elif rank <= 5:  score -= 1   # elite defense: tough matchup

    # Trend
    if "HOT" in trend:    score += 2
    elif "WARM" in trend: score += 1
    elif "COLD" in trend: score -= 2

    if score >= 8: return "ELITE"
    if score >= 5: return "STRONG"
    if score >= 3: return "LEAN"
    return "PASS"

# ── 5. Today's schedule + opponent lookup ────────────────────────────────────

_STATIC_SCHEDULE_URL = (
    "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2_1.json"
)


def _get_schedule_from_cdn() -> dict[str, dict]:
    """
    Pull home/away team IDs from the NBA static season schedule JSON.
    Returns {game_id: {home_team_id, away_team_id, ...}}.
    This is more reliable than scoreboardv2 for Finals / playoff games
    where the live endpoint sometimes omits team IDs pre-game.
    """
    from datetime import date
    today = date.today().strftime("%m/%d/%Y")
    try:
        r = _SESSION.get(_STATIC_SCHEDULE_URL, timeout=TIMEOUT)
        r.raise_for_status()
        schedule = r.json().get("leagueSchedule", {})
    except Exception:
        return {}

    games: dict[str, dict] = {}
    for date_block in schedule.get("gameDates", []):
        if not date_block.get("gameDate", "").startswith(today):
            continue
        for g in date_block.get("games", []):
            gid     = g.get("gameId", "")
            home    = g.get("homeTeam", {})
            away    = g.get("awayTeam", {})
            home_id = int(home.get("teamId") or 0)
            away_id = int(away.get("teamId") or 0)
            if home_id and away_id:
                games[gid] = {
                    "home_team_id":   home_id,
                    "away_team_id":   away_id,
                    "home_team_name": home.get("teamCity", "") + " " + home.get("teamName", ""),
                    "away_team_name": away.get("teamCity", "") + " " + away.get("teamName", ""),
                    "home_team_abbr": home.get("teamTricode", ""),
                    "away_team_abbr": away.get("teamTricode", ""),
                    "game_status":    1,
                }
    return games


def get_todays_schedule() -> dict[int, dict]:
    """
    Fetch today's NBA schedule.

    Primary: CDN static season schedule (reliable team IDs even pre-game).
    Overlay: scoreboardv2 game_status for live/final detection.

    Returns a dict keyed by game_id (str):
      {
        game_id: {
          "home_team_id":   int,
          "away_team_id":   int,
          "home_team_name": str,
          "away_team_name": str,
          "home_team_abbr": str,
          "away_team_abbr": str,
          "game_status":    int,   # 1=pre, 2=live, 3=final
        }
      }
    """
    import vortextime
    today = vortextime.vortex_day()

    # ── Primary: static CDN schedule (has team IDs for all game types) ─────────
    games = _get_schedule_from_cdn()

    # ── Overlay: scoreboardv2 for live game_status ─────────────────────────────
    sb_data = _get("scoreboardv2", {
        "GameDate":  today,
        "LeagueID":  "00",
        "DayOffset": 0,
    }, cache_key=f"nba_scoreboard_{today}")

    if sb_data:
        try:
            for gh in _result_to_dicts(sb_data, result_set_index=0):
                gid = gh.get("GAME_ID", "")
                if gid in games:
                    games[gid]["game_status"] = int(gh.get("GAME_STATUS_ID") or 1)
                elif gh.get("HOME_TEAM_ID") and gh.get("VISITOR_TEAM_ID"):
                    # Game in scoreboardv2 but not CDN (shouldn't happen, but handle it)
                    home_id = int(gh["HOME_TEAM_ID"])
                    away_id = int(gh["VISITOR_TEAM_ID"])
                    games[gid] = {
                        "home_team_id":   home_id,
                        "away_team_id":   away_id,
                        "home_team_name": "",
                        "away_team_name": "",
                        "home_team_abbr": "",
                        "away_team_abbr": "",
                        "game_status":    int(gh.get("GAME_STATUS_ID") or 1),
                    }
        except (IndexError, KeyError):
            pass

    log.info("NBA schedule: %d games today", len(games))
    return games


def build_opponent_lookup(schedule: dict) -> dict[int, int]:
    """
    From today's schedule dict, build:
        team_id  →  opponent_team_id

    Home team's opponent = away team, and vice versa.
    """
    lookup: dict[int, int] = {}
    for game in schedule.values():
        home_id = game.get("home_team_id")
        away_id = game.get("away_team_id")
        if home_id and away_id:
            lookup[home_id] = away_id
            lookup[away_id] = home_id
    return lookup

# ── Pretty print for dry run ─────────────────────────────────────────────────

def _print_card(card: dict):
    divider = "━" * 55

    if "error" in card:
        print(f"\n  ERROR: {card['error']}")
        return

    s   = card["splits"]
    d   = card["defense"]

    print(f"\n{divider}")
    print(f"  VORTEX NBA CARD  |  {card['player_name'].upper()}")
    print(f"  Prop   : O{card['line']} {card['prop_label']}")
    print(f"  Tier   : {card['tier']}")
    print(divider)

    def _rate_str(r):
        if not r: return "n/a"
        icon = "🔥" if r["rate"] >= 70 else "✅" if r["rate"] >= 50 else "❌"
        return f"{icon} {r['rate']}% ({r['hits']}/{r['games']})  avg {r['avg']}"

    print(f"\n  SPLITS  (season avg: {s.get('season_avg')}  |  {s.get('games_played')} G)")
    print(f"    L5  : {_rate_str(s.get('l5'))}")
    print(f"    L10 : {_rate_str(s.get('l10'))}")
    print(f"    L20 : {_rate_str(s.get('l20'))}")
    print(f"  Trend  : {card['trend_signal']}")

    print(f"\n  RECENT GAMES")
    for g in s.get("recent_games", []):
        icon = "✅" if g["over"] else "❌"
        print(f"    {icon}  {g['date']}  {g['opponent']:30}  "
              f"{card['prop_label']}: {g['value']}")

    print(f"\n  OPPONENT DEFENSE  :  {d.get('team_name')} ({d.get('team_abbr')})")
    print(f"    Avg {card['prop_label']} allowed : {d.get('avg_allowed')} / game")
    rank = d.get("league_rank")
    if rank:
        quality = ("elite" if rank <= 5 else "stingy" if rank <= 10
                   else "average" if rank <= 20 else "generous" if rank <= 25
                   else "very generous")
        print(f"    Defensive rank  : #{rank}/30 ({quality})")
    if d.get("def_rating"):
        print(f"    Def rating      : {d['def_rating']}")

    print(f"\n{divider}\n")


# ── Dry run ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 55)
    print("  Vortex NBA Stats Engine — Dry Run")
    print(f"  Season: {SEASON}")
    print("=" * 55)

    print("\n  Loading today's NBA schedule...")
    schedule = get_todays_schedule()
    opp_lookup = build_opponent_lookup(schedule)
    print(f"  {len(schedule)} games found")

    if schedule:
        print("\n  Today's games:")
        for gid, g in schedule.items():
            print(f"    {g['away_team_abbr']} @ {g['home_team_abbr']}")

    # Test cases: (player_name, opp_team_id, line, prop_type)
    # These use real team IDs: LAL=1610612747, BOS=1610612738, GSW=1610612744
    test_cases: list[tuple] = [
        ("LeBron James",   1610612738, 24.5, "points"),
        ("Stephen Curry",  1610612747, 5.5,  "threes"),
        ("Nikola Jokic",   1610612744, 9.5,  "rebounds"),
    ]

    for player, opp_id, line, prop in test_cases:
        print(f"\n  Testing: {player}  O{line} {prop}  vs team_id={opp_id}")
        card = get_full_card(player, opp_id, line, prop)
        _print_card(card)
        time.sleep(0.5)

    print("  Dry run complete.")
