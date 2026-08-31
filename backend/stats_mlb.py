"""
Vortex — MLB Stats Engine
==========================
Pulls raw player & pitcher data from the official MLB Stats API
(statsapi.mlb.com).  No API key required.

Public functions
----------------
  get_player_id(player_name)                    -> int | None
  get_historical_splits(player_id, line, prop)  -> dict
  get_pitcher_metrics(pitcher_name)             -> dict
  get_bvp_history(batter_id, pitcher_id)        -> dict
  get_full_card(batter_name, pitcher_name,
                line, prop_type)                -> dict   ← main entry point

Supported prop_type values
--------------------------
  "hits"            H ≥ line
  "total_bases"     TB ≥ line
  "home_runs"       HR ≥ line
  "rbis"            RBI ≥ line
  "runs_scored"     R ≥ line
  "strikeouts"      K ≥ line   (batter Ks)
  "walks"           BB ≥ line
  "hits_runs_rbis"  H+R+RBI ≥ line
"""

import io
import sys
import time
import json
import math
import re
import logging
import threading
from functools import lru_cache
from pathlib import Path
from typing import Optional

import ssl
import requests
import urllib3
import vortextime
from requests.adapters import HTTPAdapter
from urllib3.poolmanager import PoolManager
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class _LaxTLSAdapter(HTTPAdapter):
    """Forces TLS 1.2 with a relaxed cipher list to fix SSLV3_ALERT_HANDSHAKE_FAILURE
    on Wispbyte's Python 3.14 when connecting to Cloudflare Workers."""
    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)

_SESSION = requests.Session()
# pool_maxsize defaults to 10 -- raised so the prediction API's parallel
# fetches (up to 11 concurrent stats_mlb calls) don't queue for a free
# connection and end up effectively serialized despite using a thread pool.
_SESSION.mount("https://", _LaxTLSAdapter(pool_connections=20, pool_maxsize=20))
_CACHE_IO_LOCK = threading.RLock()

# ── UTF-8 output on Windows (only wrap when run directly) ───────────────────
if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ── Logging ─────────────────────────────────────────────────────────────────
log = logging.getLogger("vortex.stats_mlb")
logging.basicConfig(level=logging.INFO, format="  %(levelname)s  %(message)s")

# ── Constants ────────────────────────────────────────────────────────────────
BASE          = "https://mlb-proxy.damian209466-d45.workers.dev/api/v1"
BASE_FALLBACK = "https://statsapi.mlb.com/api/v1"
SEASON        = 2026
REQUEST_DELAY = 0.2   # seconds between calls — polite rate limiting
TIMEOUT       = 12    # seconds per request
CACHE_DIR     = Path(__file__).parent / "cache" / "mlb_stats"
try:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    # Read-only deployment filesystem (e.g. Netlify/Vercel serverless functions,
    # which only allow writes under /tmp) — fall back to a writable temp dir.
    import tempfile
    CACHE_DIR = Path(tempfile.gettempdir()) / "vortex_mlb_stats_cache"
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Cache freshness. Previously cache files never expired, so a game log fetched in
# the morning was served stale all day (why /player showed the *previous* game).
# Volatile, date-keyed data now refreshes hourly; season-long aggregates daily.
_VOLATILE_PREFIXES = (
    "gamelog_", "schedule_", "gametimes_", "lineups_", "umpires_",
    "confirmed_pitchers_", "bpen_", "roof_", "profile_", "roster_",
)

def _cache_ttl_sec(cache_key: str) -> int:
    # Extended TTL so pre-warmed cache survives a full day on the server.
    # warm_cache.py is run locally each morning to refresh these files.
    if cache_key.startswith(("schedule_", "lineups_", "confirmed_pitchers_", "bpen_")):
        return 20 * 60
    if cache_key.startswith("roof_"):
        return 10 * 60
    # Player/team assignments change immediately around the trade deadline.
    # Keep these caches short so currentTeam and active-roster lookups cannot
    # remain attached to a former club for the old 48-hour default.
    if cache_key.startswith(("profile_", "roster_")):
        return 30 * 60
    # Every season-to-date number shown on an analysis card must refresh during
    # the same day.  These used to fall through to the 48-hour default, which
    # could leave team K ranks, platoon averages and pitch-mix data two games
    # behind even though the card presented them as current-season values.
    if cache_key.startswith((
        "standings_", "team_off_profile_", "season_pitch_", "bpen_season_",
        "all_teams_hit_", "all_teams_k_", "team_k_", "team_vs_",
        "batter_vs_", "arsenal_", "bat_pitch_", "bvp_pitch_",
        "pitch_adv_", "pit_split_", "bullpen_",
    )):
        return 6 * 3600
    return 6 * 3600 if cache_key.startswith(_VOLATILE_PREFIXES) else 48 * 3600

def clear_cache() -> int:
    """Delete every cached MLB API response. Used by a forced board refresh."""
    n = 0
    for f in CACHE_DIR.glob("*.json"):
        try:
            f.unlink()
            n += 1
        except OSError:
            pass
    return n

# Stat keys per prop type  →  (game_log_field, display_label)
PROP_STAT_MAP = {
    "hits":           ("hits",       "Hits"),
    "total_bases":    ("totalBases", "Total Bases"),
    "home_runs":      ("homeRuns",   "Home Runs"),
    "rbis":           ("rbi",        "RBIs"),
    "runs_scored":    ("runs",       "Runs Scored"),
    "strikeouts":     ("strikeOuts", "Strikeouts (Batter)"),
    "walks":          ("baseOnBalls","Walks"),
    "hits_runs_rbis": (None,         "H+R+RBIs"),  # computed field
    "fantasy_score":  (None,         "Fantasy Score (PP)"),  # PrizePicks scoring
    # Pitcher props
    "pitcher_outs":   ("inningsPitched", "Outs"),   # computed via IP→outs in _stat_from_game
    "pitcher_hits_allowed": ("hits",  "Hits Allowed"),
    "pitcher_earned_runs":  ("earnedRuns", "Earned Runs"),
}

# ── HTTP helpers ─────────────────────────────────────────────────────────────

def _get(endpoint: str, params: dict = None, cache_key: str = None) -> Optional[dict]:
    """
    GET a MLB Stats API endpoint.
    Optionally serves from a session-level cache file to avoid hammering
    the API when the same call is made multiple times in one run.
    """
    if cache_key:
        cache_file = CACHE_DIR / f"{cache_key}.json"
        with _CACHE_IO_LOCK:
            if cache_file.exists():
                try:
                    fresh = (time.time() - cache_file.stat().st_mtime) < _cache_ttl_sec(cache_key)
                    if fresh:
                        with open(cache_file, encoding="utf-8") as f:
                            return json.load(f)
                except (OSError, json.JSONDecodeError):
                    # A prior interrupted/non-atomic write must never crash all
                    # concurrent evaluations. Remove it and refetch below.
                    try:
                        cache_file.unlink(missing_ok=True)
                    except OSError:
                        pass

    _HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/html, application/xhtml+xml, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://www.mlb.com",
        "Referer": "https://www.mlb.com/",
    }
    for base in (BASE, BASE_FALLBACK):
        url = f"{base}{endpoint}"
        try:
            time.sleep(REQUEST_DELAY)
            r = _SESSION.get(url, params=params or {}, timeout=TIMEOUT, headers=_HEADERS)
            r.raise_for_status()
            data = r.json()
            if cache_key:
                cache_file = CACHE_DIR / f"{cache_key}.json"
                temp_file = cache_file.with_suffix(".json.tmp")
                with _CACHE_IO_LOCK:
                    try:
                        with open(temp_file, "w", encoding="utf-8") as f:
                            json.dump(data, f)
                            f.flush()
                        temp_file.replace(cache_file)
                    except OSError as exc:
                        log.warning("MLB cache write failed for %s: %s", cache_key, exc)
                        try:
                            temp_file.unlink(missing_ok=True)
                        except OSError:
                            pass
            return data
        except requests.exceptions.HTTPError as exc:
            try:
                body = exc.response.text[:400]
            except Exception:
                body = "(no body)"
            log.warning("MLB API request failed: %s  (%s) | body: %s", url, exc, body)
        except requests.RequestException as exc:
            log.warning("MLB API request failed: %s  (%s)", url, exc)
    return None

# ── 1. Player ID lookup ──────────────────────────────────────────────────────

_ACTIVE_PLAYERS_CACHE: tuple[float, list[dict]] = (0.0, [])

def _fetch_active_players() -> list[dict]:
    """Fetch all active MLB players from /sports/1/players (cached 1 hour).
    Used as a fallback when /people/search fails."""
    cached_at, cached = _ACTIVE_PLAYERS_CACHE
    if cached and time.time() - cached_at < 3600:
        return cached
    data = _get("/sports/1/players", {"season": SEASON, "hydrate": "currentTeam"},
                cache_key=f"all_players_{SEASON}")
    players = (data or {}).get("people", [])
    _ACTIVE_PLAYERS_CACHE = (time.time(), players)
    return players

def _dotted_initials(name: str) -> str:
    """'JT Ginn' -> 'J.T. Ginn', 'AJ Minter' -> 'A.J. Minter'. No-op if first token is long."""
    parts = name.strip().split()
    if len(parts) < 2:
        return name
    first = parts[0]
    if len(first) <= 3 and first.isalpha() and first.isupper():
        dotted = ".".join(list(first)) + "."
        return " ".join([dotted] + parts[1:])
    return name


def get_player_id(player_name: str) -> Optional[int]:
    """
    Return the official MLB person ID for a player name.
    Tries exact search, Last/First swap, and dotted-initial variants.
    Validates that the returned player's last name matches the query to
    prevent cross-player fuzzy mismatches (e.g. 'JT Ginn' -> 'JT Brubaker').
    """
    parts      = player_name.strip().split()
    query_last = parts[-1].lower() if parts else ""

    def _search(name: str, ck: str | None = None) -> list:
        key  = ck or f"pid_{name.lower().replace(' ', '_').replace('.', '')}"
        data = _get("/people/search", {"names": name, "sportId": 1}, cache_key=key)
        return (data or {}).get("people", [])

    def _last_matches(p: dict) -> bool:
        full_parts = (p.get("fullName") or "").split()
        return bool(full_parts) and full_parts[-1].lower() == query_last

    people = _search(player_name, f"pid_{player_name.lower().replace(' ', '_')}")

    if not people and len(parts) >= 2:
        people = _search(f"{parts[-1]}, {parts[0]}")

    if not people:
        alt = _dotted_initials(player_name)
        if alt != player_name:
            people = _search(alt)

    # Fallback: search all active players when /people/search is unavailable
    if not people and len(parts) >= 2:
        query_norm = " ".join(parts).lower()
        query_last_lower = query_last
        from difflib import SequenceMatcher
        active = _fetch_active_players()
        for p in active:
            full = (p.get("fullName") or "").lower()
            full_parts = full.split()
            if not full_parts:
                continue
            last_ok = full_parts[-1] == query_last_lower
            ratio = SequenceMatcher(None, query_norm, full).ratio()
            if last_ok and ratio >= 0.70:
                people = [p]
                break
        if not people:
            # Second pass: any player with ratio >= 0.65
            candidates = [
                p for p in active
                if SequenceMatcher(None, query_norm,
                                   (p.get("fullName") or "").lower()).ratio() >= 0.65
            ]
            if candidates:
                candidates.sort(
                    key=lambda p: SequenceMatcher(None, query_norm,
                                                  (p.get("fullName") or "").lower()).ratio(),
                    reverse=True
                )
                people = candidates[:1]

    if not people:
        log.warning("Player not found: %s", player_name)
        return None

    # Prefer players whose last name actually matches the query
    matched = [p for p in people if _last_matches(p)]
    pool    = matched if matched else people
    active  = [p for p in pool if p.get("active")]
    player  = active[0] if active else pool[0]
    log.info("Resolved '%s' -> %s (id=%s)", player_name, player["fullName"], player["id"])
    return player["id"]


def _get_player_profile(player_id: int) -> dict:
    data = _get(f"/people/{player_id}",
                {"hydrate": "currentTeam"},
                cache_key=f"profile_{player_id}")
    people = (data or {}).get("people", [])
    return people[0] if people else {}

def _resolve_opp_pitcher_hands(games: list[dict]) -> dict[int, str]:
    """
    For a list of raw batter game-log entries (each with "game":{"gamePk"}
    and "isHome"), resolve the OPPOSING starting pitcher's throwing hand for
    each game. MLB's schedule endpoint returns the actual starter (not just
    a pre-game "probable") for completed games when queried by gamePk, so
    this works retroactively for the whole game log, not just tonight.

    Returns {gamePk: "L"|"R"}, omitting games where the pitcher/hand
    couldn't be resolved. Both the gamePk->pitcher lookup and the
    pitcher->hand lookup are file-cached (_get / _get_player_profile), so
    repeat games started by the same pitcher cost one cache hit, not a
    fresh network call.
    """
    from concurrent.futures import ThreadPoolExecutor

    entries = []
    for g in games:
        pk = (g.get("game") or {}).get("gamePk")
        is_home = g.get("isHome")
        if pk is not None and is_home is not None:
            entries.append((pk, is_home))
    if not entries:
        return {}

    def _fetch_pitcher_id(pk: int, is_home: bool):
        data = _get("/schedule", {"gamePk": pk, "hydrate": "probablePitcher"},
                     cache_key=f"schedule_gamepk_{pk}")
        for date_entry in (data or {}).get("dates", []):
            for game in date_entry.get("games", []):
                teams = game.get("teams", {})
                # Our player's team was the home/away side per the game log
                # entry -- the OPPONENT's pitcher is the other side's.
                opp_side = "away" if is_home else "home"
                pp = teams.get(opp_side, {}).get("probablePitcher")
                if pp and pp.get("id"):
                    return pk, pp["id"]
        return pk, None

    pk_to_pitcher = {}
    with ThreadPoolExecutor(max_workers=min(10, len(entries))) as pool:
        for pk, pitcher_id in pool.map(lambda e: _fetch_pitcher_id(*e), entries):
            if pitcher_id:
                pk_to_pitcher[pk] = pitcher_id

    unique_pitcher_ids = set(pk_to_pitcher.values())
    hand_by_pitcher = {}
    with ThreadPoolExecutor(max_workers=min(10, len(unique_pitcher_ids) or 1)) as pool:
        def _hand(pid):
            return pid, (_get_player_profile(pid).get("pitchHand") or {}).get("code")
        for pid, hand in pool.map(_hand, unique_pitcher_ids):
            if hand:
                hand_by_pitcher[pid] = hand

    return {pk: hand_by_pitcher[pid] for pk, pid in pk_to_pitcher.items() if pid in hand_by_pitcher}


# ── 2. Historical splits (batter game log + hit rate calc) ────────────────────

def _stat_from_game(game_stat: dict, prop_type: str) -> float:
    """Extract the numeric stat value relevant to the prop from one game log entry."""
    s = game_stat
    if prop_type == "hits_runs_rbis":
        return s.get("hits", 0) + s.get("runs", 0) + s.get("rbi", 0)
    if prop_type == "fantasy_score":
        singles = max(0, int(s.get("hits", 0)) - int(s.get("doubles", 0))
                      - int(s.get("triples", 0)) - int(s.get("homeRuns", 0)))
        return (singles * 3 + int(s.get("doubles", 0)) * 5
                + int(s.get("triples", 0)) * 8 + int(s.get("homeRuns", 0)) * 10
                + int(s.get("runs", 0)) * 2 + int(s.get("rbi", 0)) * 2
                + int(s.get("baseOnBalls", 0)) * 2 + int(s.get("hitByPitch", 0)) * 2
                + int(s.get("stolenBases", 0)) * 5)
    if prop_type == "pitcher_outs":
        ip = float(s.get("inningsPitched", 0))
        return int(ip) * 3 + int(round((ip - int(ip)) * 10))
    field, _ = PROP_STAT_MAP.get(prop_type, ("hits", "Hits"))
    return float(s.get(field, 0))


def _hit_rate(games: list[dict], line: float, prop_type: str, n: int) -> Optional[dict]:
    """
    Given a list of game-log entries (newest first), calculate hit rate
    for the most recent N games.
    Returns None if fewer than n//2 games are available.
    """
    sample = games[:n]
    if len(sample) < max(1, n // 2):
        return None

    values  = [_stat_from_game(g["stat"], prop_type) for g in sample]
    hits    = sum(1 for v in values if v >= line)
    avg_val = round(sum(values) / len(values), 2)

    return {
        "games":    len(sample),
        "hits":     hits,
        "rate":     round(hits / len(sample) * 100, 1),
        "avg":      avg_val,
        "streak":   _current_streak(values, line),
        "values":   values,  # per-game outcomes, newest first -- real distribution data
    }


def _current_streak(values: list[float], line: float) -> int:
    """
    Positive = consecutive games OVER the line (most recent first).
    Negative = consecutive games UNDER.
    """
    if not values:
        return 0
    over = values[0] >= line
    streak = 0
    for v in values:
        if (v >= line) == over:
            streak += 1
        else:
            break
    return streak if over else -streak


def get_historical_splits(player_id: int, line: float,
                           prop_type: str = "hits",
                           include_hand_venue: bool = False) -> dict:
    """
    Fetch the player's current-season game log and compute L5/L10/L20 hit rates.

    For batter props queries group=hitting; for pitcher props group=pitching.

    Returns a dict with keys:
      l5, l10, l20          — hit rate dicts (games/hits/rate/avg/streak)
      season_avg            — season average for the stat
      games_played          — total games in the season
      prop_label            — human-readable prop name
      recent_games          — list of last 5 game summaries

    include_hand_venue: resolves each game_log entry's opposing starter's
    hand (isHome is free either way). Costs ~10 extra parallelized network
    calls cold, so it's OFF by default -- the main prediction card doesn't
    need it and shouldn't pay for it. Only the game-log modal's on-demand
    handedness/venue filters turn it on (see get_game_log_filters_data()).
    """
    from datetime import date as _date
    _today = _date.today().isoformat()

    _PITCHER_PROPS = {"pitcher_outs", "pitcher_hits_allowed", "pitcher_earned_runs"}
    is_pitcher = prop_type in _PITCHER_PROPS
    group = "pitching" if is_pitcher else "hitting"
    prefix = "pitch" if is_pitcher else "hit"

    data = _get(f"/people/{player_id}/stats", {
        "stats": "gameLog", "group": group,
        "season": SEASON, "sportId": 1,
    }, cache_key=f"gamelog_{prefix}_{player_id}_{SEASON}_{_today}")

    if not data:
        return {"error": "Could not fetch game log"}

    raw_splits = (data.get("stats") or [{}])[0].get("splits", [])
    if not raw_splits:
        return {"error": "No game log data found for this season"}

    # Newest games first
    splits = list(reversed(raw_splits))
    _, prop_label = PROP_STAT_MAP.get(prop_type, ("hits", "Hits"))

    # Season totals from last entry's cumulative stat is NOT in the game log —
    # pull season stat separately
    season_data = _get(f"/people/{player_id}/stats", {
        "stats": "season", "group": group,
        "season": SEASON, "sportId": 1,
    }, cache_key=f"season_{prefix}_{player_id}_{SEASON}")

    season_splits = ((season_data or {}).get("stats") or [{}])[0].get("splits", [])
    season_stat = season_splits[0]["stat"] if season_splits else {}

    recent_batting_form = None
    if not is_pitcher:
        def _ops_from_stats(rows):
            totals = {k: 0 for k in ("ab", "h", "tb", "bb", "hbp", "sf")}
            for row in rows:
                stat = row.get("stat", row)
                totals["ab"] += int(stat.get("atBats", 0) or 0)
                totals["h"] += int(stat.get("hits", 0) or 0)
                totals["tb"] += int(stat.get("totalBases", 0) or 0)
                totals["bb"] += int(stat.get("baseOnBalls", 0) or 0)
                totals["hbp"] += int(stat.get("hitByPitch", 0) or 0)
                totals["sf"] += int(stat.get("sacFlies", 0) or 0)
            obp_den = totals["ab"] + totals["bb"] + totals["hbp"] + totals["sf"]
            obp = (totals["h"] + totals["bb"] + totals["hbp"]) / obp_den if obp_den else 0
            slg = totals["tb"] / totals["ab"] if totals["ab"] else 0
            return obp + slg, totals["ab"] + totals["bb"] + totals["hbp"]
        l10_ops, l10_pa = _ops_from_stats(splits[:10])
        try: season_ops = float(season_stat.get("ops", 0) or 0)
        except (TypeError, ValueError): season_ops = 0.0
        if l10_pa >= 20 and l10_ops > 0 and season_ops > 0:
            recent_batting_form = {
                "l10_ops": round(l10_ops, 3), "season_ops": round(season_ops, 3),
                "delta_pct": round((l10_ops / season_ops - 1) * 100, 1), "pa": l10_pa,
            }

    if prop_type == "hits_runs_rbis":
        h = int(season_stat.get("hits", 0))
        r = int(season_stat.get("runs", 0))
        rbi = int(season_stat.get("rbi", 0))
        games_played = int(season_stat.get("gamesPlayed", len(splits)))
        season_avg = round((h + r + rbi) / max(games_played, 1), 2)
    elif prop_type == "fantasy_score":
        h = int(season_stat.get("hits", 0))
        d = int(season_stat.get("doubles", 0))
        t = int(season_stat.get("triples", 0))
        hr = int(season_stat.get("homeRuns", 0))
        singles = max(0, h - d - t - hr)
        r = int(season_stat.get("runs", 0))
        rbi = int(season_stat.get("rbi", 0))
        bb = int(season_stat.get("baseOnBalls", 0))
        hbp = int(season_stat.get("hitByPitch", 0))
        sb = int(season_stat.get("stolenBases", 0))
        total_fp = (singles * 3 + d * 5 + t * 8 + hr * 10
                    + r * 2 + rbi * 2 + bb * 2 + hbp * 2 + sb * 5)
        games_played = int(season_stat.get("gamesPlayed", len(splits)))
        season_avg = round(total_fp / max(games_played, 1), 2)
    elif prop_type == "pitcher_outs":
        ip = float(season_stat.get("inningsPitched", 0))
        total_outs = int(ip) * 3 + int(round((ip - int(ip)) * 10))
        games_played = int(season_stat.get("gamesPlayed", len(splits)))
        season_avg = round(total_outs / max(games_played, 1), 2)
    else:
        field, _ = PROP_STAT_MAP.get(prop_type, ("hits", "Hits"))
        games_played = int(season_stat.get("gamesPlayed", len(splits)))
        total = int(season_stat.get(field, 0))
        season_avg = round(total / max(games_played, 1), 2)

    # Recent game summaries (last 5)
    recent = []
    for g in splits[:5]:
        s = g["stat"]
        val = _stat_from_game(s, prop_type)
        recent.append({
            "date":     g.get("date", ""),
            "opponent": g.get("opponent", {}).get("name", ""),
            "value":    val,
            "over":     val >= line,
            "summary":  s.get("summary", ""),
        })

    # Home/away averages + hit rates from last 20 games of the game log
    home_vals = [_stat_from_game(g["stat"], prop_type)
                 for g in splits[:20] if g.get("isHome") is True]
    away_vals = [_stat_from_game(g["stat"], prop_type)
                 for g in splits[:20] if g.get("isHome") is False]

    def _avg(vals):
        return round(sum(vals) / len(vals), 2) if vals else None

    def _rate(vals, ln):
        """% of games where stat exceeded the line (Over hit rate)."""
        if not vals:
            return None
        return round(sum(1 for v in vals if v > ln) / len(vals) * 100, 1)

    # Detailed per-game log for the website's expandable L5/L10/L15/L20 chart
    # -- a NEW field, deliberately separate from "recent_games" above (which
    # the Discord bot's embed builder reads and expects capped at 5; changing
    # that list's length would silently change the bot's own displayed text).
    opp_hand_by_gamepk = _resolve_opp_pitcher_hands(splits[:20]) if include_hand_venue else {}
    game_log = [
        {
            "date":     g.get("date", ""),
            "opponent": g.get("opponent", {}).get("name", ""),
            "value":    _stat_from_game(g["stat"], prop_type),
            "isHome":   g.get("isHome"),
            "oppHand":  opp_hand_by_gamepk.get((g.get("game") or {}).get("gamePk")),
        }
        for g in splits[:20]
    ]

    l5_hr, l10_hr, l20_hr = (_hit_rate(splits, line, prop_type, 5),
                             _hit_rate(splits, line, prop_type, 10),
                             _hit_rate(splits, line, prop_type, 20))

    # Trend label -- same tiering the pitcher K-card already used, now also
    # available for batter props (previously pitcher-only).
    l5r  = (l5_hr  or {}).get("rate", 50)
    l10r = (l10_hr or {}).get("rate", 50)
    l20r = (l20_hr or {}).get("rate", 50)
    if l5r >= 80 and l10r >= 70:   trend = "HOT"
    elif l5r >= 60 and l10r >= 60: trend = "WARM"
    elif l5r < 40 and l10r < 40:   trend = "COLD"
    elif l5r > l20r + 15:          trend = "HEATING UP"
    elif l5r < l20r - 15:          trend = "COOLING"
    else:                          trend = "NEUTRAL"

    # Real (not modeled) floor/median/ceiling from the actual last-20-game
    # value distribution -- percentiles, not a guess.
    last20_vals = sorted(_stat_from_game(g["stat"], prop_type) for g in splits[:20])

    def _percentile(vals, pct):
        if not vals:
            return None
        idx = min(len(vals) - 1, max(0, round(pct * (len(vals) - 1))))
        return vals[idx]

    floor_ceiling = {
        "floor":   _percentile(last20_vals, 0.10),
        "median":  _percentile(last20_vals, 0.50),
        "ceiling": _percentile(last20_vals, 0.90),
    } if last20_vals else {"floor": None, "median": None, "ceiling": None}

    # Real (not modeled) plate-appearance distribution for batter props --
    # actual PA counts from the last 20 games, not a lineup-spot lookup
    # table. Pitcher props don't have a batter PA concept, so skip.
    pa_distribution = None
    if not is_pitcher:
        pa_vals = [int(g["stat"].get("plateAppearances", 0) or 0) for g in splits[:20] if g["stat"].get("plateAppearances") is not None]
        if pa_vals:
            n = len(pa_vals)
            buckets = {}
            for v in pa_vals:
                key = str(v) if v <= 5 else "6+"
                buckets[key] = buckets.get(key, 0) + 1
            # Fill every count between the observed min and 5 with an
            # explicit 0% row, even ones that never occurred -- a silently
            # skipped bucket (e.g. jumping 1 -> 3 because 2 PA never
            # happened) reads as broken/missing data, not "this didn't
            # occur in the sample."
            lo = min((int(k) for k in buckets if k != "6+"), default=1)
            for v in range(lo, 6):
                buckets.setdefault(str(v), 0)
            pa_distribution = {
                "avg_pa": round(sum(pa_vals) / n, 2),
                "games_sampled": n,
                "buckets": [{"pa": k, "pct": round(c / n * 100, 1)} for k, c in sorted(buckets.items(), key=lambda kv: (kv[0] == "6+", int(kv[0]) if kv[0] != "6+" else 0))],
            }

    return {
        "player_id":    player_id,
        "prop_type":    prop_type,
        "prop_label":   prop_label,
        "line":         line,
        "season_avg":   season_avg,
        "games_played": games_played,
        "l5":           l5_hr,
        "l10":          l10_hr,
        "l20":          l20_hr,
        "trend":        trend,
        "floor_ceiling": floor_ceiling,
        "pa_distribution": pa_distribution,
        "recent_batting_form": recent_batting_form,
        "recent_games": recent,
        "game_log":     game_log,
        "home_avg":     _avg(home_vals),
        "home_games":   len(home_vals),
        "home_rate":    _rate(home_vals, line),   # % Over at home (last 20)
        "away_avg":     _avg(away_vals),
        "away_games":   len(away_vals),
        "away_rate":    _rate(away_vals, line),   # % Over on the road (last 20)
    }

# ── 3. Team H2H prop splits ──────────────────────────────────────────────────

def get_vs_team_splits(player_id: int, opp_team_id: int,
                       line: float, prop_type: str = "hits",
                       include_hand_venue: bool = False) -> dict:
    """
    How often has this player's prop gone Over/Under vs a specific opponent
    this season? Reuses the already-cached game log — zero extra API calls.

    Returns {team_name, games, over, under, push, over_rate, under_rate, avg}
    or {} if the player has never faced this team in the current season.

    include_hand_venue: see get_historical_splits -- off by default, only
    turned on for the game-log modal's lazy handedness/venue filter fetch.
    """
    from datetime import date as _date
    _today = _date.today().isoformat()

    data = _get(f"/people/{player_id}/stats", {
        "stats": "gameLog", "group": "hitting",
        "season": SEASON, "sportId": 1,
    }, cache_key=f"gamelog_hit_{player_id}_{SEASON}_{_today}")

    if not data:
        return {}

    raw_splits = (data.get("stats") or [{}])[0].get("splits", [])
    team_games  = [g for g in raw_splits
                   if g.get("opponent", {}).get("id") == opp_team_id]
    if not team_games:
        return {}

    team_name  = (team_games[0].get("opponent") or {}).get("name", "")
    values     = [_stat_from_game(g["stat"], prop_type) for g in team_games]
    total      = len(values)
    over_cnt   = sum(1 for v in values if v > line)
    under_cnt  = sum(1 for v in values if v < line)
    push_cnt   = sum(1 for v in values if v == line)
    avg_val    = round(sum(values) / total, 2) if total else 0

    opp_hand_by_gamepk = _resolve_opp_pitcher_hands(team_games) if include_hand_venue else {}
    game_log = [
        {
            "date": g.get("date", ""), "opponent": team_name,
            "value": _stat_from_game(g["stat"], prop_type),
            "isHome": g.get("isHome"),
            "oppHand": opp_hand_by_gamepk.get((g.get("game") or {}).get("gamePk")),
        }
        for g in team_games
    ]

    return {
        "team_name":  team_name,
        "games":      total,
        "over":       over_cnt,
        "under":      under_cnt,
        "push":       push_cnt,
        "over_rate":  round(over_cnt  / total * 100, 1) if total else 0,
        "under_rate": round(under_cnt / total * 100, 1) if total else 0,
        "avg":        avg_val,
        "game_log":   game_log,  # this season's games vs this opponent, newest last
    }


# ── 4. Pitcher metrics ────────────────────────────────────────────────────────

def get_pitcher_metrics(pitcher_name: str, pitcher_id: int | None = None) -> dict:
    """
    Fetch the pitcher's current-season stats + handedness.

    Returns a dict with keys:
      pitcher_id, name, hand
      era, whip, k_per_9, bb_per_9, hr_per_9
      innings_pitched, games_started
      last_5_starts       — list of recent start summaries
      season_k_rate       — K / batter faced
    """
    pitcher_id = pitcher_id or get_player_id(pitcher_name)
    if pitcher_id is None:
        return {"error": f"Pitcher not found: {pitcher_name}"}

    profile = _get_player_profile(pitcher_id)
    hand    = profile.get("pitchHand", {}).get("code", "?")

    season_data = _get(f"/people/{pitcher_id}/stats", {
        "stats": "season", "group": "pitching",
        "season": SEASON, "sportId": 1,
    }, cache_key=f"season_pitch_{pitcher_id}_{SEASON}")

    season_splits = ((season_data or {}).get("stats") or [{}])[0].get("splits", [])
    if not season_splits:
        return {"error": f"No 2025 pitching stats for {pitcher_name}"}

    s = season_splits[0]["stat"]
    batters_faced = int(s.get("battersFaced", 1)) or 1

    # Recent starts
    from datetime import date as _date, timedelta as _td
    # Use the VORTEX day frame so the log cutoff cannot roll early because of
    # the host machine's timezone.
    _yesterday = (
        _date.fromisoformat(vortextime.vortex_day()) - _td(days=1)
    ).strftime("%Y-%m-%d")
    log_data = _get(f"/people/{pitcher_id}/stats", {
        "stats": "gameLog", "group": "pitching",
        "season": SEASON, "sportId": 1,
        "endDate": _yesterday,
    }, cache_key=f"gamelog_pitch_{pitcher_id}_{SEASON}_{_yesterday}")

    log_splits = list(reversed(
        ((log_data or {}).get("stats") or [{}])[0].get("splits", [])
    ))

    last_5 = []
    # Retain ten starts: callers slice this into L3/L5/L10 windows.  Keeping
    # only five here silently turned every supposed L10 filter into L5 data.
    for g in log_splits[:10]:
        gs = g["stat"]
        last_5.append({
            "date":     g.get("date", ""),
            "opponent": g.get("opponent", {}).get("name", ""),
            "ip":       gs.get("inningsPitched", "0.0"),
            "k":        int(gs.get("strikeOuts", 0)),
            "er":       int(gs.get("earnedRuns", 0)),
            "bb":       int(gs.get("baseOnBalls", 0)),
            "summary":  gs.get("summary", ""),
        })

    # Flag skipped turns / potential pitch restrictions for the board model.
    days_since_last_start = None
    try:
        from datetime import date as _date2
        if last_5 and last_5[0].get("date"):
            days_since_last_start = (_date2.fromisoformat(vortextime.vortex_day()) - _date2.fromisoformat(last_5[0]["date"])).days
    except (ValueError, TypeError):
        pass
    workload_risk = bool(days_since_last_start is not None and days_since_last_start >= 8)

    # Ground-ball arms carry a different contact/run profile than fly-ball arms.
    try:
        ground_outs = int(s.get("groundOuts", 0) or 0)
        fly_outs = int(s.get("airOuts", 0) or 0)
        ground_ball_rate = round(ground_outs / (ground_outs + fly_outs) * 100, 1) if ground_outs + fly_outs >= 20 else None
    except (TypeError, ValueError):
        ground_ball_rate = None

    # FIP = (13*HR + 3*BB - 2*K) / IP + FIP_constant (~3.10)
    try:
        ip_raw = s.get("inningsPitched", "0.0")
        ip_dec = float(ip_raw.split(".")[0]) + float(ip_raw.split(".")[1]) / 3
        fip = round(
            (13 * int(s.get("homeRuns", 0)) +
             3  * int(s.get("baseOnBalls", 0)) -
             2  * int(s.get("strikeOuts", 0))) / max(ip_dec, 1) + 3.10, 2
        )
    except Exception:
        fip = None

    # ── Role validation: override depth-chart tags with game-log evidence ────
    # If a pitcher is labelled "reliever" in the schedule API but has been
    # logging starter workloads in recent appearances, we override the tag.
    games_started_season = int(s.get("gamesStarted", 0))
    recent_ips = [_ip_to_dec(g["ip"]) for g in last_5[:3]]
    avg_ip_l3  = round(sum(recent_ips) / len(recent_ips), 1) if recent_ips else None

    if avg_ip_l3 is None:
        validated_role = "UNKNOWN"
    elif avg_ip_l3 >= 4.0:
        validated_role = "SP"       # starter workload verified
    elif avg_ip_l3 >= 2.0:
        validated_role = "SWINGMAN" # bulk/opener role
    else:
        validated_role = "RP"       # reliever workload

    # Flag when game-log role contradicts season GS count
    role_overridden = (games_started_season == 0 and validated_role == "SP")

    return {
        "pitcher_id":      pitcher_id,
        "name":            profile.get("fullName", pitcher_name),
        "hand":            hand,
        "era":             s.get("era", "-.--"),
        "whip":            s.get("whip", "-.--"),
        "fip":             fip,
        "k_per_9":         s.get("strikeoutsPer9Inn", "-.--"),
        "bb_per_9":        s.get("walksPer9Inn", "-.--"),
        "hr_per_9":        s.get("homeRunsPer9", "-.--"),
        "innings_pitched": s.get("inningsPitched", "0.0"),
        "games_started":   games_started_season,
        "season_k_rate":   round(int(s.get("strikeOuts", 0)) / batters_faced, 3),
        "season_ks":       int(s.get("strikeOuts", 0)),
        "hits_per_9":      s.get("hitsPer9Inn", "-.--"),
        "avg_against":     s.get("avg", ".---"),
        "obp_against":     s.get("obp", ".---"),
        "slg_against":     s.get("slg", ".---"),
        "ops_against":     s.get("ops", ".---"),
        "last_5_starts":   last_5,
        "validated_role":  validated_role,   # "SP" | "RP" | "SWINGMAN" | "UNKNOWN"
        "avg_ip_l3":       avg_ip_l3,        # avg IP over last 3 appearances
        "role_overridden": role_overridden,  # True if depth chart said RP but logs say SP
        "days_since_last_start": days_since_last_start,
        "workload_risk":  workload_risk,
        "ground_ball_rate": ground_ball_rate,
    }


# ── 4a. Advanced pitcher stats ─────────────────────────────────────────────────

def get_pitcher_advanced_stats(pitcher_id: int) -> dict:
    """
    Fetch advanced pitching stats: BABIP, GB/FB ratio, K/9, BB/9, HR/9.
    Returns empty dict on failure.
    """
    season    = SEASON
    cache_key = f"pitch_adv_{pitcher_id}_{season}"
    data      = _get(f"/people/{pitcher_id}/stats", {
        "stats":   "seasonAdvanced",
        "group":   "pitching",
        "season":  season,
        "sportId": 1,
    }, cache_key=cache_key)
    if not data:
        return {}
    splits = ((data.get("stats") or [{}])[0]).get("splits") or []
    if not splits:
        return {}
    s = splits[0].get("stat", {})
    try:
        babip_f = float(s.get("babip", ".000") or 0)
        fip_f   = float(s.get("fip", "4.50") or 4.50)
    except (ValueError, TypeError):
        babip_f, fip_f = 0.290, 4.50
    return {
        "babip":    babip_f,
        "fip":      fip_f,
        "k_per_9":  s.get("strikeoutsPer9Inn", "-.--"),
        "bb_per_9": s.get("walksPer9Inn",      "-.--"),
        "hr_per_9": s.get("homeRunsPer9",      "-.--"),
    }


# ── 4b. Pitcher splits by batter handedness ────────────────────────────────────

def get_pitcher_splits_by_hand(pitcher_id: int) -> dict:
    """
    Fetch pitcher's FIP split vs LHB and RHB using MLB statSplits.
    Returns {"vs_left": {"fip": float, "era": float, "ip": float},
             "vs_right": {"fip": float, "era": float, "ip": float}}.
    Falls back gracefully if split data is unavailable or sample too small.
    """
    season = SEASON
    result = {}

    for sit_code, key in [("vl", "vs_left"), ("vr", "vs_right")]:
        data = _get(f"/people/{pitcher_id}/stats", {
            "stats": "statSplits", "group": "pitching",
            "season": season, "sportId": 1, "sitCodes": sit_code,
        }, cache_key=f"pit_split_{pitcher_id}_{sit_code}_{season}")
        splits = ((data or {}).get("stats") or [{}])[0].get("splits", [])
        if not splits:
            continue
        s = splits[0].get("stat", {})
        try:
            ip_raw = s.get("inningsPitched", "0.0")
            parts = str(ip_raw).split(".")
            ip = float(parts[0]) + float(parts[1]) / 3 if len(parts) > 1 and parts[1] else float(parts[0])
        except (ValueError, IndexError):
            ip = 0.0
        if ip < 10:
            continue
        try:
            era = float(s.get("era", 0) or 0)
        except (ValueError, TypeError):
            era = None
        try:
            hr = int(s.get("homeRuns", 0) or 0)
            bb = int(s.get("baseOnBalls", 0) or 0)
            k  = int(s.get("strikeOuts", 0) or 0)
            fip = round((13 * hr + 3 * bb - 2 * k) / max(ip, 1) + 3.10, 2) if ip > 0 else None
        except (ValueError, TypeError):
            fip = None
        result[key] = {"fip": fip, "era": era, "ip": round(ip, 1)}

    return result


# ── 4c. Lineup handedness ratio ───────────────────────────────────────────────

def get_lineup_handedness(date_str: str, team_id: int, _prefetched_data=None) -> dict:
    """
    Count lefty/righty/switch batters in a team's confirmed lineup.
    Returns {"lefty_ratio": float, "lefties": int, "righties": int, "switch": int}.
    lefty_ratio = (L + 0.5*S) / total — switch hitters bat left vs RHP (most common).
    Pass _prefetched_data to avoid a duplicate API call (the schedule+lineups response).
    """
    data = _prefetched_data
    if data is None:
        data = _fetch_lineups_data(date_str)
    if not data:
        return {"lefty_ratio": 0.5, "lefties": 0, "righties": 0, "switch": 0}

    for date_entry in data.get("dates", []):
        for g in date_entry.get("games", []):
            ht = (g.get("teams", {}).get("home", {}).get("team", {}).get("id"))
            at = (g.get("teams", {}).get("away", {}).get("team", {}).get("id"))
            if team_id not in (ht, at):
                continue
            side = "homePlayers" if team_id == ht else "awayPlayers"
            players = (g.get("lineups") or {}).get(side) or []
            lefties = righties = switch = 0
            for p in players:
                if p.get("position", {}).get("abbreviation") == "P":
                    continue
                hand = (p.get("batSide") or {}).get("code", "R")
                if hand == "L":
                    lefties += 1
                elif hand == "S":
                    switch += 1
                else:
                    righties += 1
            total = lefties + righties + switch
            ratio = (lefties + 0.5 * switch) / total if total > 0 else 0.5
            return {"lefty_ratio": round(ratio, 3),
                    "lefties": lefties, "righties": righties, "switch": switch}

    return {"lefty_ratio": 0.5, "lefties": 0, "righties": 0, "switch": 0}


# ── 4. BvP history ────────────────────────────────────────────────────────────

def get_bvp_history(batter_id: int, pitcher_id: int) -> dict:
    """
    Pull career head-to-head stats between a specific batter and pitcher.

    The API returns a list of 'splits' by season — we aggregate them
    into career totals and also return the most recent season breakdown.
    """
    data = _get(f"/people/{batter_id}/stats", {
        "stats":             "vsPlayer",
        "group":             "hitting",
        "opposingPlayerId":  pitcher_id,
        "sportId":           1,
    }, cache_key=f"bvp_{batter_id}_vs_{pitcher_id}")

    if not data:
        return {"error": "BvP fetch failed"}

    splits = (data.get("stats") or [{}])[0].get("splits", [])
    if not splits:
        return {
            "ab": 0, "hits": 0, "avg": ".---", "hr": 0, "rbi": 0,
            "k": 0, "bb": 0, "tb": 0, "ops": ".---",
            "sample": "no history",
            "seasons": [],
        }

    # Aggregate career totals
    career: dict = {
        "ab": 0, "hits": 0, "hr": 0, "rbi": 0,
        "k": 0, "bb": 0, "tb": 0, "pa": 0,
    }
    seasons = []
    for sp in splits:
        s = sp.get("stat", {})
        career["ab"]   += int(s.get("atBats", 0))
        career["hits"] += int(s.get("hits", 0))
        career["hr"]   += int(s.get("homeRuns", 0))
        career["rbi"]  += int(s.get("rbi", 0))
        career["k"]    += int(s.get("strikeOuts", 0))
        career["bb"]   += int(s.get("baseOnBalls", 0))
        career["tb"]   += int(s.get("totalBases", 0))
        career["pa"]   += int(s.get("plateAppearances", 0))
        seasons.append({
            "season": sp.get("season", "?"),
            "ab":     int(s.get("atBats", 0)),
            "hits":   int(s.get("hits", 0)),
            "hr":     int(s.get("homeRuns", 0)),
            "avg":    s.get("avg", ".---"),
            "ops":    s.get("ops", ".---"),
            "k":      int(s.get("strikeOuts", 0)),
        })

    ab   = career["ab"] or 1
    avg  = f".{int(career['hits'] / ab * 1000):03d}"
    slg  = career["tb"] / ab
    obp  = (career["hits"] + career["bb"]) / max(career["pa"], 1)
    ops  = f"{slg + obp:.3f}"

    sample_label = (
        "large sample"  if career["ab"] >= 20 else
        "moderate sample" if career["ab"] >= 10 else
        "small sample"  if career["ab"] >= 5  else
        "very small sample — treat with caution"
    )

    return {
        "ab":      career["ab"],
        "hits":    career["hits"],
        "avg":     avg,
        "obp":     f".{int(obp * 1000):03d}",
        "slg":     f".{int(slg * 1000):03d}",
        "hr":      career["hr"],
        "rbi":     career["rbi"],
        "k":       career["k"],
        "bb":      career["bb"],
        "tb":      career["tb"],
        "ops":     ops,
        "sample":  sample_label,
        "seasons": seasons,
    }

# ── 5. Full card (main entry point) ──────────────────────────────────────────

def get_full_card(batter_name: str, pitcher_name: str,
                  line: float, prop_type: str = "total_bases",
                  side: str = "over", opp_team_id: int | None = None) -> dict:
    """
    Assemble a complete analytical card for a prop:
      - batter splits (L5/L10/L20)
      - pitcher metrics
      - BvP history
      - platoon matchup note
      - suggested confidence tier

    side="under" causes _confidence_tier to evaluate Under strength directly,
    so the returned tier reflects the correct side without TIER_INVERT.
    Returns a unified dict ready to enrich update_board.py summaries.
    """
    log.info("Building card: %s vs %s  |  %s O%.1f",
             batter_name, pitcher_name, prop_type, line)

    # 1. Batter
    batter_id = get_player_id(batter_name)
    if batter_id is None:
        return {"error": f"Batter not found: {batter_name}"}

    batter_profile = _get_player_profile(batter_id)
    bat_side = batter_profile.get("batSide", {}).get("code", "?")

    splits  = get_historical_splits(batter_id, line, prop_type)
    pitcher = get_pitcher_metrics(pitcher_name) if pitcher_name else {}
    bvp     = get_bvp_history(batter_id, pitcher.get("pitcher_id", 0)) \
              if pitcher.get("pitcher_id") else {}

    # 2. Platoon note
    pitcher_hand = pitcher.get("hand", "?")
    platoon_note = _platoon_note(bat_side, pitcher_hand)

    # 3. Trend signals
    trend = _trend_signal(splits)

    # 4. Confidence tier — side-aware so Under props score correctly
    tier = _confidence_tier(splits, pitcher, bvp, trend, side)

    # Home/away per-game averages + hit rates (from splits game log — no extra API call)
    home_away = {
        "home_avg":   splits.get("home_avg"),
        "home_games": splits.get("home_games", 0),
        "home_rate":  splits.get("home_rate"),   # % Over at home
        "away_avg":   splits.get("away_avg"),
        "away_games": splits.get("away_games", 0),
        "away_rate":  splits.get("away_rate"),   # % Over on the road
    }

    # Statcast barrel% / exit velocity (cached daily from Baseball Savant)
    statcast = _get_batter_statcast(batter_name)

    # A walk consumes a PA without creating a hit. Carry individual BB% into
    # board scoring so high-walk hitters are not treated like normal hit props.
    bb_rate = None
    try:
        bat_season = _get(f"/people/{batter_id}/stats", {
            "stats": "season", "group": "hitting", "season": SEASON, "sportId": 1,
        }, cache_key=f"season_bat_discipline_{batter_id}_{SEASON}")
        bat_splits = ((bat_season or {}).get("stats") or [{}])[0].get("splits") or []
        if bat_splits:
            bat_stat = bat_splits[0].get("stat") or {}
            pa = int(bat_stat.get("plateAppearances", 0) or 0)
            if pa >= 40:
                bb_rate = round(int(bat_stat.get("baseOnBalls", 0) or 0) / pa * 100, 1)
    except (TypeError, ValueError):
        pass

    # ── Full matchup signal set (so the BOARD scores everything it displays) ──
    # These were previously only fetched by the /analyze command, which left the
    # board's grade_pick call blind to handedness, pitch-mix, and team history.
    pitcher_id = pitcher.get("pitcher_id", 0)
    _hand = pitcher.get("hand", "R") or "R"
    try:
        vs_hand_splits = get_batter_hand_splits(batter_id, _hand)
    except Exception:
        vs_hand_splits = {}
    try:
        arsenal = get_pitcher_arsenal(pitcher_id) if pitcher_id else []
    except Exception:
        arsenal = []
    try:
        # Season-wide Savant performance by pitch type is the correct input for
        # arsenal fit. The older MLB vsPlayer endpoint is sparse and commonly
        # returned an empty list even for everyday hitters.
        bat_vs_pitch = get_batter_arsenal_stats(batter_id)
        if not bat_vs_pitch and pitcher_id:
            bat_vs_pitch = get_batter_vs_pitch_type(batter_id, pitcher_id)
    except Exception:
        bat_vs_pitch = []
    try:
        team_bvp = get_team_bvp(batter_id, opp_team_id) if opp_team_id else {}
    except Exception:
        team_bvp = {}
    try:
        team_h2h = get_vs_team_splits(batter_id, opp_team_id, line, prop_type) if opp_team_id else {}
    except Exception:
        team_h2h = {}
    try:
        oaa = get_team_defense_oaa(opp_team_id) if opp_team_id else {}
    except Exception:
        oaa = {}

    return {
        "batter_name":   batter_name,
        "batter_id":     batter_id,
        "bat_side":      bat_side,
        "prop_type":     prop_type,
        "prop_label":    splits.get("prop_label", prop_type),
        "line":          line,
        "splits":        splits,
        "pitcher":       pitcher,
        "bvp":           bvp,
        "platoon_note":  platoon_note,
        "trend_signal":  trend,
        "tier":          tier,
        "home_away":     home_away,
        "statcast":      statcast,
        "vs_hand_splits": vs_hand_splits,
        "arsenal":       arsenal,
        "bat_vs_pitch":  bat_vs_pitch,
        "team_bvp":      team_bvp,
        "team_h2h":      team_h2h,
        "oaa":           oaa,
        "bb_rate":       bb_rate,
    }


def _platoon_note(bat_side: str, pitch_hand: str) -> str:
    matchups = {
        ("L", "R"): "LHB vs RHP — standard platoon advantage for the batter.",
        ("R", "L"): "RHB vs LHP — standard platoon advantage for the batter.",
        ("L", "L"): "LHB vs LHP — same-hand matchup, slight pitcher advantage.",
        ("R", "R"): "RHB vs RHP — same-hand matchup, slight pitcher advantage.",
        ("S", "R"): "Switch hitter bats LEFT vs RHP — favorable side.",
        ("S", "L"): "Switch hitter bats RIGHT vs LHP — favorable side.",
    }
    return matchups.get((bat_side, pitch_hand),
                        f"Bat: {bat_side} vs Pitch: {pitch_hand}")


def _trend_signal(splits: dict) -> str:
    l5  = splits.get("l5")
    l10 = splits.get("l10")
    l20 = splits.get("l20")
    if not l5 or not l10:
        return "insufficient data"

    r5  = l5["rate"]
    r10 = l10["rate"]
    r20 = l20["rate"] if l20 else r10
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


def _confidence_tier(splits: dict, pitcher: dict, bvp: dict, trend: str, side: str = "over") -> str:
    """
    Assign ELITE / STRONG / LEAN / PASS based on multiple signals.
    Does NOT use EV (that's update_board.py's job) — this is purely stats.

    Scoring guide (max ~16 pts):
      L10 hit rate    0–6
      L5  momentum    0–3
      L20 sustained   0–2
      Pitcher ERA     0–2
      BvP history     -1–2
      Trend           -2–2
    Thresholds: ELITE ≥ 9 · STRONG ≥ 6 · LEAN ≥ 3 · PASS < 3

    For side="under", all rates are inverted (rate fields are always Over%).
    """
    score = 0
    is_under = (side == "under")
    # All split "rate" fields store the Over hit rate; invert when evaluating Under
    _eff = (lambda r: 100.0 - float(r)) if is_under else (lambda r: float(r))

    l10 = splits.get("l10")
    l5  = splits.get("l5")
    l20 = splits.get("l20")

    # L10 — primary signal (0–6 pts)
    if l10 and _eff(l10["rate"]) >= 90:   score += 6
    elif l10 and _eff(l10["rate"]) >= 80: score += 4
    elif l10 and _eff(l10["rate"]) >= 70: score += 2
    elif l10 and _eff(l10["rate"]) >= 60: score += 1
    # below 60% effective = no points

    # L5 — momentum confirmation (0–3 pts)
    if l5 and _eff(l5["rate"]) >= 100:    score += 3
    elif l5 and _eff(l5["rate"]) >= 80:   score += 2
    elif l5 and _eff(l5["rate"]) >= 60:   score += 1

    # L20 — sustained consistency (0–2 pts)
    if l20 and _eff(l20["rate"]) >= 80:   score += 2
    elif l20 and _eff(l20["rate"]) >= 70: score += 1

    # Pitcher: high ERA helps Over (batter produces), low ERA helps Under
    try:
        era = float(pitcher.get("era", "99"))
        if is_under:
            if era <= 3.0:   score += 2
            elif era <= 4.0: score += 1
        else:
            if era >= 5.5:   score += 2
            elif era >= 4.5: score += 1
    except (ValueError, TypeError):
        pass

    # BvP — only weight with a meaningful sample
    # High career avg vs this pitcher = good for Over, bad for Under
    if bvp and bvp.get("ab", 0) >= 8:
        try:
            avg_raw = bvp.get("avg", ".000")
            avg = float(avg_raw.lstrip(".").zfill(4)) / 1000 if avg_raw.startswith(".") else float(avg_raw)
            if is_under:
                if avg <= 0.150:   score += 1   # batter struggles vs pitcher → good for Under
                elif avg >= 0.333: score -= 1   # batter dominates pitcher → bad for Under
            else:
                if avg >= 0.333:   score += 2
                elif avg >= 0.260: score += 1
                elif avg <= 0.150: score -= 1
        except (ValueError, TypeError):
            pass

    # Trend (Over-centric from _trend_signal) — invert meaning for Under
    if is_under:
        if "COLD" in trend:    score += 2   # cold on Over = strong for Under
        elif "COOLING" in trend: score += 1
        elif "HOT" in trend:   score -= 2   # hot on Over = weak for Under
        elif "HEATING" in trend: score -= 1
        elif "WARM" in trend:  score -= 1
    else:
        if "HOT" in trend:    score += 2
        elif "WARM" in trend: score += 1
        elif "COLD" in trend: score -= 2

    if score >= 9:  return "ELITE"
    if score >= 6:  return "STRONG"
    if score >= 3:  return "LEAN"
    return "PASS"

# ── 6. Pitcher strikeout card (pitcher K prop analytics) ────────────────────

def get_all_teams_k_rate() -> dict:
    """
    K% for all 30 MLB teams this season.
    Returns {team_id: {k_pct, avg, name, rank}} where rank 1 = hardest to K.
    """
    data = _get(
        "/teams/stats",
        {"stats": "season", "group": "hitting", "season": SEASON, "sportId": 1},
        cache_key=f"all_teams_hit_{SEASON}",
    )
    if not data:
        return {}
    result = {}
    for split in (data.get("stats") or [{}])[0].get("splits", []):
        tid  = split.get("team", {}).get("id")
        name = split.get("team", {}).get("name", "")
        s    = split.get("stat", {})
        pa   = int(s.get("plateAppearances", 1)) or 1
        ks   = int(s.get("strikeOuts", 0))
        if tid:
            result[tid] = {
                "k_pct": round(ks / pa * 100, 1),
                "avg":   s.get("avg", ".---"),
                "name":  name,
            }
    # rank 1 = lowest K rate = hardest to K
    sorted_teams = sorted(result.items(), key=lambda x: x[1]["k_pct"])
    for rank, (tid, _) in enumerate(sorted_teams, 1):
        result[tid]["rank"] = rank
    return result


def get_team_k_rate_vs_hand(team_id: int, pitcher_hand: str) -> dict:
    """
    Team K rate when facing a specific pitcher handedness (L or R).
    Returns {k_pct, avg, pa, ks} or {} if insufficient sample.
    """
    split_code = "vl" if pitcher_hand == "L" else "vr"
    data = _get(
        f"/teams/{team_id}/stats",
        {"stats": "statSplits", "group": "hitting",
         "season": SEASON, "sportId": 1, "sitCodes": split_code},
        cache_key=f"team_vs_{split_code}_{team_id}_{SEASON}",
    )
    if not data:
        return {}
    for split in (data.get("stats") or [{}])[0].get("splits", []):
        s  = split.get("stat", {})
        pa = int(s.get("plateAppearances", 1)) or 1
        ks = int(s.get("strikeOuts", 0))
        if pa >= 50:
            return {
                "k_pct": round(ks / pa * 100, 1),
                "avg":   s.get("avg", ".---"),
                "pa":    pa,
                "ks":    ks,
            }
    return {}


def get_all_teams_k_rate_home_away(is_home: bool) -> dict:
    """
    K rate + rank for ALL teams at home (is_home=True) or on the road.
    One API call. Returns {team_id: {k_pct, rank, name}} where rank 1 = hardest
    to strike out at that venue. Lets the K matchup use a venue-aware rank instead
    of the season rank (e.g. Colorado is far tougher to K at Coors than overall).
    """
    sit = "h" if is_home else "a"
    data = _get(
        "/teams/stats",
        {"stats": "statSplits", "group": "hitting",
         "season": SEASON, "sportId": 1, "sitCodes": sit},
        cache_key=f"all_teams_k_{sit}_{SEASON}",
    )
    if not data:
        return {}
    result = {}
    for sp in (data.get("stats") or [{}])[0].get("splits", []):
        tid = sp.get("team", {}).get("id")
        s   = sp.get("stat", {})
        pa  = int(s.get("plateAppearances", 1)) or 1
        ks  = int(s.get("strikeOuts", 0))
        if tid and pa >= 50:
            result[tid] = {"k_pct": round(ks / pa * 100, 1),
                           "name": sp.get("team", {}).get("name", "")}
    # rank 1 = lowest K% = hardest to strike out at this venue
    for rank, (tid, _) in enumerate(
            sorted(result.items(), key=lambda x: x[1]["k_pct"]), 1):
        result[tid]["rank"] = rank
    return result


def get_team_k_rate_home_away(team_id: int, is_home: bool) -> dict:
    """
    Team strikeout rate at home vs on the road. A lineup can whiff a lot overall
    but make far more contact at home (e.g. Colorado at Coors) — using the venue
    split avoids over-rating a pitcher's K prop against a tough-at-home lineup.
    is_home=True → the team's HOME split. Returns {k_pct, pa} or {} if sample < 50 PA.
    """
    sit = "h" if is_home else "a"
    data = _get(
        f"/teams/{team_id}/stats",
        {"stats": "statSplits", "group": "hitting",
         "season": SEASON, "sportId": 1, "sitCodes": sit},
        cache_key=f"team_k_{sit}_{team_id}_{SEASON}",
    )
    if not data:
        return {}
    for split in (data.get("stats") or [{}])[0].get("splits", []):
        s  = split.get("stat", {})
        pa = int(s.get("plateAppearances", 1)) or 1
        ks = int(s.get("strikeOuts", 0))
        if pa >= 50:
            return {"k_pct": round(ks / pa * 100, 1), "pa": pa}
    return {}


def get_batter_hand_splits(player_id: int, pitcher_hand: str = "R") -> dict:
    """
    Batter's season stats vs both LHP and RHP.
    Returns {"L": {avg, ops, pa, ab, hr, rbi, k_pct}, "R": {...}}.
    pitcher_hand is kept for API compatibility but both hands are always fetched.
    """
    result = {}
    for ph in ("L", "R"):
        site_code = "vl" if ph == "L" else "vr"
        data = _get(
            f"/people/{player_id}/stats",
            {"stats": "statSplits", "group": "hitting",
             "season": SEASON, "sportId": 1, "sitCodes": site_code},
            cache_key=f"batter_vs_{site_code}_{player_id}_{SEASON}",
        )
        if not data:
            continue
        splits = (data.get("stats") or [{}])[0].get("splits", [])
        if not splits:
            continue
        s = splits[0].get("stat", {})
        pa = int(s.get("plateAppearances", 0) or 0)
        so = int(s.get("strikeOuts", 0) or 0)
        result[ph] = {
            "avg":   s.get("avg", "---"),
            "ops":   s.get("ops", "---"),
            "pa":    pa,
            "ab":    int(s.get("atBats", 0) or 0),
            "hr":    int(s.get("homeRuns", 0) or 0),
            "rbi":   int(s.get("rbi", 0) or 0),
            "k_pct": round(so / pa * 100, 1) if pa else None,
        }
    return result


def get_team_bullpen(team_id: int) -> dict:
    """
    Team bullpen (reliever-only) season quality, from the MLB Stats API's
    team statSplits with sitCode "rp". Real relief-innings-only numbers,
    not the team's overall pitching line.

    Returns {era, whip, ops_against, avg_against, ip} or {} when the split
    isn't available / the sample is tiny (<50 IP early in a season a
    bullpen "quality" number is noise).
    """
    if not team_id:
        return {}
    data = _get(f"/teams/{team_id}/stats", {
        "stats": "statSplits", "group": "pitching",
        "season": SEASON, "sitCodes": "rp",
    }, cache_key=f"bullpen_{team_id}_{SEASON}")
    splits = ((data or {}).get("stats") or [{}])[0].get("splits", [])
    if not splits:
        return {}
    s = splits[0].get("stat", {})
    try:
        ip_raw = s.get("inningsPitched", "0.0")
        ip = float(ip_raw.split(".")[0]) + float(ip_raw.split(".")[1]) / 3
    except (ValueError, IndexError):
        ip = 0.0
    if ip < 50:
        return {}
    try:
        era = float(s.get("era"))
    except (TypeError, ValueError):
        return {}
    return {
        "era": era,
        "whip": s.get("whip"),
        "ops_against": s.get("ops"),
        "avg_against": s.get("avg"),
        "ip": round(ip, 1),
    }


def get_batter_arsenal_stats(batter_id: int) -> list[dict]:
    """
    Batter's REAL season performance vs each pitch type, from Baseball
    Savant's pitch-arsenal-stats leaderboard (the MLB Stats API's
    "pitchArsenal" hitting endpoint only returns pitch frequencies seen,
    not performance -- Savant is the only free source of the actual
    BA/SLG/wOBA-vs-pitch-type numbers).

    Season-wide vs ALL pitchers who throw that pitch, not vs one specific
    pitcher -- callers should label it that way.

    The leaderboard is one ~350KB CSV covering every qualified batter, so
    it's fetched once, parsed, and file-cached as JSON keyed by player id
    (12h TTL); per-player calls after that are a dict lookup.

    Returns [{pitch_type, pitch_name, pa, pitches, avg, slg, woba, whiff_pct,
    k_pct}], PA >= 10.
    """
    cache_file = CACHE_DIR / f"savant_batter_arsenal_v2_{SEASON}.json"
    table = None
    stale_table = None
    if cache_file.exists():
        try:
            stale_table = json.loads(cache_file.read_text(encoding="utf-8"))
            if (time.time() - cache_file.stat().st_mtime) < 43200:  # 12h
                table = stale_table
        except (OSError, json.JSONDecodeError):
            pass

    if table is None:
        import csv as _csv
        import io as _io
        try:
            r = None
            for attempt in range(3):
                try:
                    r = requests.get(
                        "https://baseballsavant.mlb.com/leaderboard/pitch-arsenal-stats",
                        params={"type": "batter", "pitchType": "", "year": SEASON,
                                "team": "", "min": "10", "csv": "true"},
                        timeout=15,
                        headers={"User-Agent": "Mozilla/5.0"},
                    )
                    if r.ok:
                        break
                except requests.RequestException:
                    r = None
                if attempt < 2:
                    time.sleep(0.6 * (2 ** attempt))
            if r is None or not r.ok:
                return (stale_table or {}).get(str(batter_id), [])
            table = {}
            reader = _csv.DictReader(_io.StringIO(r.content.decode("utf-8-sig")))
            for row in reader:
                pid = row.get("player_id", "").strip()
                pa = int(float(row.get("pa", 0) or 0))
                if not pid or pa < 10:
                    continue
                table.setdefault(pid, []).append({
                    "pitch_type": row.get("pitch_type", ""),
                    "pitch_name": row.get("pitch_name", ""),
                    "pa":         pa,
                    "pitches":    int(float(row.get("pitches", 0) or 0)),
                    "avg":        row.get("ba", ""),
                    "slg":        row.get("slg", ""),
                    "woba":       row.get("woba", ""),
                    "whiff_pct":  row.get("whiff_percent", ""),
                    "k_pct":      row.get("k_percent", ""),
                })
            # Do not replace a healthy cache with a partial HTTP-200 response.
            if len(table) >= 100:
                try:
                    cache_file.write_text(json.dumps(table), encoding="utf-8")
                except OSError:
                    pass
            elif stale_table:
                table = stale_table
        except requests.RequestException:
            return (stale_table or {}).get(str(batter_id), [])

    return table.get(str(batter_id), [])


def _pitcher_stat_from_game(s: dict, prop_type: str) -> float:
    """
    Extract the numeric value for a pitcher prop from one game-log entry.
    Mirrors _stat_from_game's role for batters. "outs" comes straight from
    the API (no IP-string parsing needed); QS = >=18 outs (6 IP) AND <=3 ER,
    the standard definition.
    """
    if prop_type == "pitcher_outs":
        return float(s.get("outs", 0) or 0)
    if prop_type == "pitcher_earned_runs":
        return float(s.get("earnedRuns", 0) or 0)
    if prop_type == "pitcher_hits_allowed":
        return float(s.get("hits", 0) or 0)
    if prop_type == "pitcher_fantasy_score":
        outs = int(s.get("outs", 0) or 0)
        er = int(s.get("earnedRuns", 0) or 0)
        win = int(s.get("wins", 0) or 0)
        qs = 1 if (outs >= 18 and er <= 3) else 0
        return float(win * 6 + qs * 4 + er * -3 + int(s.get("strikeOuts", 0) or 0) * 3 + outs * 1)
    return float(s.get("strikeOuts", 0) or 0)  # "strikeouts" and any unrecognized fallback


def get_pitcher_k_card(pitcher_name: str, line: float,
                       opp_team_id: int = None,
                       pitcher_id: int = None,
                       prop_type: str = "strikeouts") -> dict:
    """
    Analytical card for a pitcher counting-stat prop: strikeouts (the
    original/default), pitching outs, earned runs allowed, hits allowed, or
    a pitcher fantasy-score composite.

    Returns L5/L10/L20 hit rates from the pitching game log, season stats,
    and the opposing team's K rate/rank (opp_k is only strikeout-specific
    context -- still computed for every prop_type since it's cheap and
    informative, but grade_pick() only actually uses it when
    prop_type == "strikeouts").

    pitcher_id: if already resolved by the caller, skip the name lookup.
    """
    if pitcher_id is None:
        pitcher_id = get_player_id(pitcher_name)
    if pitcher_id is None:
        return {"error": f"Pitcher not found: {pitcher_name}"}

    profile = _get_player_profile(pitcher_id)
    hand    = profile.get("pitchHand", {}).get("code", "?")

    # Season pitching stats
    season_data = _get(f"/people/{pitcher_id}/stats", {
        "stats": "season", "group": "pitching",
        "season": SEASON, "sportId": 1,
    }, cache_key=f"season_pitch_{pitcher_id}_{SEASON}")

    season_splits = ((season_data or {}).get("stats") or [{}])[0].get("splits", [])
    if not season_splits:
        return {"error": f"No {SEASON} pitching stats for {pitcher_name}"}

    s             = season_splits[0]["stat"]
    season_ks     = int(s.get("strikeOuts", 0))
    season_bb     = int(s.get("baseOnBalls", 0))
    gs            = int(s.get("gamesStarted", 0))
    batters_faced = int(s.get("battersFaced", 1)) or 1
    k_per_gs      = round(season_ks / gs, 1) if gs else None
    # Prop-agnostic season per-start average (used by non-strikeout props --
    # k_per_gs above stays strikeout-specific for the K/9-style season line).
    stat_per_gs   = round(_pitcher_stat_from_game(s, prop_type) / gs, 1) if gs else None

    # FIP
    try:
        ip_raw = s.get("inningsPitched", "0.0")
        ip_dec = float(ip_raw.split(".")[0]) + float(ip_raw.split(".")[1]) / 3
        fip    = round(
            (13 * int(s.get("homeRuns", 0)) +
              3  * int(s.get("baseOnBalls", 0)) -
              2  * int(s.get("strikeOuts", 0))) / max(ip_dec, 1) + 3.10, 2
        )
    except Exception:
        fip = None

    # Pitching game log
    from vortextime import vortex_day_offset
    _yesterday = vortex_day_offset(-1)
    log_data = _get(f"/people/{pitcher_id}/stats", {
        "stats": "gameLog", "group": "pitching",
        "season": SEASON, "sportId": 1,
        "endDate": _yesterday,
    }, cache_key=f"gamelog_pitch_{pitcher_id}_{SEASON}_{_yesterday}")

    log_splits = list(reversed(
        ((log_data or {}).get("stats") or [{}])[0].get("splits", [])
    ))
    k_vals = [_pitcher_stat_from_game(g["stat"], prop_type) for g in log_splits]

    def _hr_k(n):
        sample = k_vals[:n]
        if len(sample) < max(1, n // 2):
            return None
        hits = sum(1 for k in sample if k > line)
        return {
            "games":  len(sample),
            "hits":   hits,
            "rate":   round(hits / len(sample) * 100, 1),
            "avg":    round(sum(sample) / len(sample), 1),
            "streak": _current_streak([float(k) for k in sample], line),
            "values": sample,  # per-start outcomes -- real distribution data
        }

    splits = {
        "l5":          _hr_k(5),
        "l10":         _hr_k(10),
        "l20":         _hr_k(20),
        "season_avg":  k_per_gs if prop_type == "strikeouts" else stat_per_gs,
        "games_played": gs,
        "game_log": [
            {"date": g.get("date", ""), "opponent": g.get("opponent", {}).get("name", ""),
             "value": _pitcher_stat_from_game(g["stat"], prop_type),
             "isHome": g.get("isHome")}
            for g in log_splits[:20]
        ],
    }

    # Recent starts + home/away ERA split from game log
    last_5 = []
    home_er, home_ip_dec = 0, 0.0
    away_er, away_ip_dec = 0, 0.0
    for g in log_splits[:5]:
        gs_stat = g["stat"]
        ip_raw = gs_stat.get("inningsPitched", "0.0")
        last_5.append({
            "date":     g.get("date", ""),
            "opponent": g.get("opponent", {}).get("name", ""),
            "ip":       ip_raw,
            "outs":     int(_ip_to_dec(ip_raw) * 3),
            "k":        int(gs_stat.get("strikeOuts", 0)),
            "er":       int(gs_stat.get("earnedRuns", 0)),
            "bb":       int(gs_stat.get("baseOnBalls", 0)),
            "hits":     int(gs_stat.get("hits", 0)),
            "value":    _pitcher_stat_from_game(gs_stat, prop_type),
        })
    home_ks, away_ks = [], []
    for g in log_splits:
        is_h    = g.get("isHome")
        gs_stat = g["stat"]
        er  = int(gs_stat.get("earnedRuns", 0))
        ip  = _ip_to_dec(gs_stat.get("inningsPitched", "0.0"))
        if is_h is True:
            home_er += er;  home_ip_dec += ip
            home_ks.append(_pitcher_stat_from_game(gs_stat, prop_type))
        elif is_h is False:
            away_er += er;  away_ip_dec += ip
            away_ks.append(_pitcher_stat_from_game(gs_stat, prop_type))

    home_era_val = round(home_er / home_ip_dec * 9, 2) if home_ip_dec >= 3 else None
    away_era_val = round(away_er / away_ip_dec * 9, 2) if away_ip_dec >= 3 else None

    # Home/away K splits at this line (same > criterion as _hr_k above).
    # 2+ starts minimum -- a single start isn't a split, it's an anecdote.
    def _k_split(ks):
        if len(ks) < 2:
            return {"avg": None, "over_rate": None, "starts": len(ks)}
        return {
            "avg": round(sum(ks) / len(ks), 1),
            "over_rate": round(sum(1 for k in ks if k > line) / len(ks) * 100, 1),
            "starts": len(ks),
        }
    home_k_split = _k_split(home_ks)
    away_k_split = _k_split(away_ks)

    # Trend
    l5r  = (splits["l5"]  or {}).get("rate", 50)
    l10r = (splits["l10"] or {}).get("rate", 50)
    l20r = (splits["l20"] or {}).get("rate", 50)
    if l5r >= 80 and l10r >= 70:   trend = "HOT"
    elif l5r >= 60 and l10r >= 60: trend = "WARM"
    elif l5r < 40 and l10r < 40:   trend = "COLD"
    elif l5r > l20r + 15:          trend = "HEATING UP"
    elif l5r < l20r - 15:          trend = "COOLING"
    else:                          trend = "NEUTRAL"

    # Tier
    score = 0
    if l10r >= 70:   score += 3
    elif l10r >= 60: score += 2
    elif l10r >= 50: score += 1
    if l5r >= 80:    score += 2
    elif l5r >= 60:  score += 1
    if trend == "HOT":                      score += 2
    elif trend in ("WARM", "HEATING UP"):   score += 1
    elif trend == "COLD":                   score -= 2
    elif trend == "COOLING":                score -= 1
    # Pitchers have higher variance than batters — lower thresholds
    tier = ("ELITE"  if score >= 6 else
            "STRONG" if score >= 4 else
            "LEAN"   if score >= 2 else "PASS")

    # Opponent team K rate (overall + handedness-specific)
    opp_k = {}
    opp_k_vs_hand = {}
    if opp_team_id:
        all_k         = get_all_teams_k_rate()
        opp_k         = all_k.get(opp_team_id, {})
        opp_k_vs_hand = get_team_k_rate_vs_hand(opp_team_id, hand)

    return {
        "pitcher_id":    pitcher_id,
        "pitcher_name":  profile.get("fullName", pitcher_name),
        "hand":          hand,
        "prop_type":     prop_type,
        "is_pitcher":    True,
        "line":          line,
        "splits":        splits,
        "season_stats":  {
            "era":              s.get("era", "-.--"),
            "whip":             s.get("whip", "-.--"),
            "fip":              fip,
            "k_per_9":          s.get("strikeoutsPer9Inn", "-.--"),
            "k_total":          season_ks,
            "k_pct":            round(season_ks / batters_faced * 100, 1),
            "bb_pct":           round(season_bb / batters_faced * 100, 1),
            "games_started":    gs,
            "batters_faced":    batters_faced,
            "innings_pitched":  s.get("inningsPitched", "0.0"),
            "avg_against":      s.get("avg", ".---"),
            "k_per_gs":         k_per_gs,
        },
        "last_5_starts": last_5,
        "trend_signal":  trend,
        "tier":          tier,
        "opp_k":          opp_k,
        "opp_k_vs_hand":  opp_k_vs_hand,
        "home_era":       home_era_val,
        "away_era":       away_era_val,
        "home_k_split":   home_k_split,
        "away_k_split":   away_k_split,
    }


# ── 7. Today's schedule + probable pitchers ──────────────────────────────────

_MLB_TEAM_ABBR = {
    "Arizona Diamondbacks": "AZ",  "Athletics": "ATH",          "Oakland Athletics": "OAK",
    "Atlanta Braves": "ATL",       "Baltimore Orioles": "BAL",  "Boston Red Sox": "BOS",
    "Chicago Cubs": "CHC",         "Chicago White Sox": "CWS",  "Cincinnati Reds": "CIN",
    "Cleveland Guardians": "CLE",  "Colorado Rockies": "COL",   "Detroit Tigers": "DET",
    "Houston Astros": "HOU",       "Kansas City Royals": "KC",  "Los Angeles Angels": "LAA",
    "Los Angeles Dodgers": "LAD",  "Miami Marlins": "MIA",      "Milwaukee Brewers": "MIL",
    "Minnesota Twins": "MIN",      "New York Mets": "NYM",      "New York Yankees": "NYY",
    "Philadelphia Phillies": "PHI","Pittsburgh Pirates": "PIT", "San Diego Padres": "SD",
    "San Francisco Giants": "SF",  "Seattle Mariners": "SEA",   "St. Louis Cardinals": "STL",
    "Tampa Bay Rays": "TB",        "Texas Rangers": "TEX",      "Toronto Blue Jays": "TOR",
    "Washington Nationals": "WSH",
}


def _team_abbr(team: dict) -> str:
    """Best-effort team abbreviation from an MLB API team object."""
    a = (team or {}).get("abbreviation")
    if a:
        return a
    name = (team or {}).get("name", "")
    return _MLB_TEAM_ABBR.get(name, name[:3].upper())


def _get_confirmed_pitchers(date_str: str) -> dict[int, tuple[str | None, int | None]]:
    """
    Fetch confirmed starting pitchers from the lineups hydrate.
    Returns {game_pk: (pitcher_name, pitcher_id)} for both home and away.
    When lineups are posted, these are the ACTUAL starters — more reliable
    than probablePitcher which can be stale.
    """
    data = _get("/schedule", {
        "sportId": 1, "date": date_str, "gameType": "R",
        "hydrate": "lineups",
    }, cache_key=f"confirmed_pitchers_{date_str}")
    if not data:
        return {}

    result = {}
    for date_entry in data.get("dates", []):
        for g in date_entry.get("games", []):
            pk = g.get("gamePk")
            if not pk:
                continue
            lineups = g.get("lineups") or {}
            for side, team_side in [("homePlayers", "home"), ("awayPlayers", "away")]:
                players = lineups.get(side) or []
                pitchers = [p for p in players if (p.get("position", {}).get("abbreviation") == "P"
                                                   or p.get("primaryPosition", {}).get("abbreviation") == "P")]
                if pitchers:
                    pp = pitchers[0]
                    name = pp.get("fullName")
                    pid = pp.get("id")
                    if name:
                        result.setdefault(pk, {})
                        result[pk][f"{team_side}_pitcher"] = (name, pid)
    return result


def _fetch_lineups_data(date_str: str) -> dict:
    """Fetch schedule with hydrate=lineups. No file cache — lineups change through the day."""
    return _get("/schedule", {
        "sportId": 1, "date": date_str, "gameType": "R",
        "hydrate": "lineups",
    }, cache_key=None)


def has_confirmed_batting_order(players: list[dict]) -> bool:
    """True when MLB supplies nine unique hitters in its posted lineup order."""
    hitters = [p for p in (players or [])
               if (p.get("position") or p.get("primaryPosition") or {}).get("abbreviation") != "P"
               and p.get("id")]
    if len({p.get("id") for p in hitters[:9]}) < 9:
        return False
    # Some MLB hydrate responses include 100..900 battingOrder codes; others
    # return the confirmed nine already sequenced without those codes.
    if not any(str(p.get("battingOrder", "")).strip() for p in hitters[:9]):
        return True
    slots = set()
    player_ids = set()
    for player in hitters:
        order = str(player.get("battingOrder", "")).strip()
        player_id = player.get("id")
        if not order or not player_id:
            continue
        try:
            slot = int(order[0])
        except (TypeError, ValueError):
            continue
        if 1 <= slot <= 9:
            slots.add(slot)
            player_ids.add(player_id)
    return slots == set(range(1, 10)) and len(player_ids) >= 9


def get_lineups_posted(date_str: str) -> set:
    """
    Return the set of game_pks whose BOTH teams have a full batting order posted
    (≥9 position players each). Used to gate moneyline cards on confirmed lineups.
    Fresh data each call (short cache) since lineups fill in through the day.
    """
    data = _fetch_lineups_data(date_str)
    posted = set()
    if not data:
        return posted
    for date_entry in data.get("dates", []):
        for g in date_entry.get("games", []):
            pk = g.get("gamePk")
            lineups = g.get("lineups") or {}
            home = lineups.get("homePlayers") or []
            away = lineups.get("awayPlayers") or []
            # A prefilled player array is not a confirmed batting order.
            if pk and has_confirmed_batting_order(home) and has_confirmed_batting_order(away):
                posted.add(pk)
    return posted


def get_lineups_data(date_str: str) -> dict:
    """Return the raw schedule+lineups API response for a date. Used by moneyline.py."""
    return _fetch_lineups_data(date_str)


def get_todays_schedule(game_date: str | None = None, fresh: bool = False) -> dict[int, dict]:
    """
    Fetch the MLB schedule (default today) with hydrated probable pitchers.

    Returns a dict keyed by MLB game_pk:
      {
        game_pk: {
          "home_team_id":   int,
          "away_team_id":   int,
          "home_team_name": str,
          "away_team_name": str,
          "home_abbr":      str,
          "away_abbr":      str,
          "home_pitcher":   str | None,
          "away_pitcher":   str | None,
          "home_pitcher_id": int | None,
          "away_pitcher_id": int | None,
        }
      }
    """
    import vortextime
    today = game_date or vortextime.vortex_day()
    data  = _get("/schedule", {
        "sportId": 1,
        "date":    today,
        "hydrate": "probablePitcher,team",
    }, cache_key=None if fresh else f"schedule_{today}")

    if not data:
        return {}

    games = {}
    for date_entry in data.get("dates", []):
        for g in date_entry.get("games", []):
            pk    = g.get("gamePk")
            teams = g.get("teams", {})
            home  = teams.get("home", {})
            away  = teams.get("away", {})

            def _pitcher(side: dict):
                pp = side.get("probablePitcher")
                if not pp:
                    return None, None
                return pp.get("fullName"), pp.get("id")

            hp_name, hp_id = _pitcher(home)
            ap_name, ap_id = _pitcher(away)

            games[pk] = {
                "gamePk":          pk,
                "home_team_id":    home.get("team", {}).get("id"),
                "away_team_id":    away.get("team", {}).get("id"),
                "home_team_name":  home.get("team", {}).get("name", ""),
                "away_team_name":  away.get("team", {}).get("name", ""),
                "home_abbr":       _team_abbr(home.get("team", {})),
                "away_abbr":       _team_abbr(away.get("team", {})),
                "home_pitcher":    hp_name,
                "away_pitcher":    ap_name,
                "home_pitcher_id": hp_id,
                "away_pitcher_id": ap_id,
                "game_utc":        g.get("gameDate", ""),  # e.g. "2026-06-14T23:10:00Z"
                # "Preview" | "Live" | "Final" -- lets callers distinguish
                # "hasn't started" from "in progress" from "over", instead of
                # just comparing against first-pitch time (a live game isn't
                # "started" in the sense of "irrelevant now" -- it's still
                # tonight's real matchup until it's actually final).
                "status":          g.get("status", {}).get("abstractGameState", ""),
            }

    log.info("Schedule: %d games today", len(games))

    # The lineup hydrate contains the nine hitters, not confirmed pitchers.
    # A fresh probable-pitcher schedule is safer than inventing a lineup-based
    # starter override.
    return games

# ── 7b. Standings (team strength for moneyline win-prob model) ───────────────

def get_standings() -> dict:
    """
    Team strength table for the log5 / Pythagorean win-probability model.
    Returns {team_id: {name, win_pct, rs, ra, run_diff, gp, last10_pct, streak}}.
    Tries the current season, falls back to the prior year if it has no games yet.
    """
    for season in (SEASON, SEASON - 1):
        data = _get("/standings",
                    {"leagueId": "103,104", "season": str(season),
                     "standingsTypes": "regularSeason", "hydrate": "team"},
                    cache_key=f"standings_{season}")
        out = {}
        for rec in (data or {}).get("records", []):
            for t in rec.get("teamRecords", []):
                tid = t.get("team", {}).get("id")
                if not tid:
                    continue
                gp = int(t.get("gamesPlayed", 0) or 0)
                # last-10 from split records
                last10_pct = None
                for sr in (t.get("records", {}) or {}).get("splitRecords", []):
                    if sr.get("type") == "lastTen":
                        w, l = int(sr.get("wins", 0)), int(sr.get("losses", 0))
                        if w + l:
                            last10_pct = round(w / (w + l), 3)
                try:
                    win_pct = float(t.get("winningPercentage", 0) or 0)
                except (ValueError, TypeError):
                    win_pct = 0.0
                out[tid] = {
                    "name":       t.get("team", {}).get("name", ""),
                    "wins":       int(t.get("wins", 0) or 0),
                    "losses":     int(t.get("losses", 0) or 0),
                    "win_pct":    win_pct,
                    "rs":         int(t.get("runsScored", 0) or 0),
                    "ra":         int(t.get("runsAllowed", 0) or 0),
                    "run_diff":   int(t.get("runDifferential", 0) or 0),
                    "gp":         gp,
                    "last10_pct": last10_pct,
                    "streak":     (t.get("streak", {}) or {}).get("streakCode", ""),
                }
        # Use this season only if it actually has games played.
        if out and max(v["gp"] for v in out.values()) >= 1:
            return out
    return {}


def get_mlb_injuries() -> dict:
    """
    ESPN MLB injury report keyed by lowercased team name:
      {team_name_lower: [{"name", "status", "status_norm"}]}
    Cached 45min. Used to flag significant absences in the moneyline model.
    """
    cache_file = CACHE_DIR / "mlb_injuries.json"
    try:
        if cache_file.exists() and (time.time() - cache_file.stat().st_mtime) < 2700:
            return json.loads(cache_file.read_text(encoding="utf-8"))
    except Exception:
        pass
    out = {}
    try:
        r = _SESSION.get("https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/injuries",
                         timeout=15)
        if r.status_code == 200:
            for team in r.json().get("injuries", []):
                nm = (team.get("displayName") or "").lower()
                rows = []
                for inj in team.get("injuries", []):
                    ath = inj.get("athlete", {}) or {}
                    pname = ath.get("displayName") or ""
                    status = (inj.get("status") or "").strip()
                    if pname:
                        rows.append({"name": pname, "status": status,
                                     "status_norm": status.lower()})
                if nm and rows:
                    out[nm] = rows
        cache_file.write_text(json.dumps(out), encoding="utf-8")
    except Exception as e:
        log.warning("MLB injuries fetch failed: %s", e)
    return out


# ── 7a. Today's game start times ─────────────────────────────────────────────

def get_todays_game_times() -> dict:
    """
    Return {pitcher_name_lower: start_time_et_str} for all MLB games today.
    Both home and away probable pitchers are keyed so any board row can look
    up its game time via the opposing pitcher name stored in stats_json.
    Status codes: I/IR/MA → '🔴 LIVE', F/FT/O/DR → '✅ Final'.
    """
    from datetime import date as _date, datetime, timezone, timedelta
    today = vortex_day()

    sched = _get("/schedule", {
        "sportId": 1, "date": today, "gameType": "R",
        "hydrate": "probablePitcher",
    }, cache_key=f"gametimes_{today}")

    result: dict[str, str] = {}
    for date_entry in (sched or {}).get("dates", []):
        for game in date_entry.get("games", []):
            gdate       = game.get("gameDate", "")
            status_code = (game.get("status") or {}).get("statusCode", "")

            time_str = ""
            try:
                utc  = datetime.strptime(gdate, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                et   = utc + timedelta(hours=-4)   # EDT (Mar–Nov)
                h    = et.hour % 12 or 12
                ampm = "PM" if et.hour >= 12 else "AM"
                time_str = f"{h}:{et.strftime('%M')} {ampm} ET"
            except Exception:
                pass

            if status_code in ("I", "IR", "MA"):
                time_str = "🔴 LIVE"
            elif status_code in ("F", "FT", "O", "DR"):
                time_str = "✅ Final"

            if not time_str:
                continue

            teams = game.get("teams", {})
            for side in ("home", "away"):
                pitcher = (teams.get(side) or {}).get("probablePitcher") or {}
                pname   = (pitcher.get("fullName") or "").lower().strip()
                if pname:
                    result[pname] = time_str

    return result


# ── 8. Lineup position ────────────────────────────────────────────────────────

def get_lineup_position(player_id: int) -> int | None:
    """
    Return today's confirmed batting order position (1-9) for this player.
    Returns None if lineup hasn't been posted yet or player isn't found.
    """
    # The board can roll to tomorrow while the system date is still today.
    # Check both VORTEX dates from a fresh lineup hydrate before the legacy
    # schedule fallback below; lineup data must never wait on a stale cache.
    from vortextime import vortex_board_day
    # Never fall back to the prior game's date after the board rolls forward.
    # That is how yesterday's lineup was being presented as tonight's.
    for lineup_day in (vortex_board_day(),):
        fresh = _get("/schedule", {
            "sportId": 1, "date": lineup_day, "hydrate": "lineups",
        }, cache_key=None)
        for date_entry in (fresh or {}).get("dates", []):
            for game in date_entry.get("games", []):
                lineups = game.get("lineups") or {}
                for side in ("homePlayers", "awayPlayers"):
                    for person in lineups.get(side, []):
                        if str(person.get("id")) != str(player_id):
                            continue
                        order = str(person.get("battingOrder", ""))
                        if order:
                            return int(order[0])
                        # A player list without an MLB battingOrder is not a
                        # posted lineup; never infer a spot from array order.
                        return None

    from vortextime import vortex_board_day
    today = vortex_board_day()
    data  = _get("/schedule", {
        "sportId": 1,
        "date":    today,
        "hydrate": "lineups",
    }, cache_key=None)
    if not data:
        return None
    for date_entry in data.get("dates", []):
        for g in date_entry.get("games", []):
            lineups = g.get("lineups") or {}
            for side in ("homePlayers", "awayPlayers"):
                for p in lineups.get(side, []):
                    if p.get("id") == player_id:
                        order = str(p.get("battingOrder", ""))
                        if order:
                            return int(order[0])  # "100"→1, "500"→5, "900"→9
    return None


def get_game_lineup_ids(team_id: int) -> list[int]:
    """
    Return list of confirmed player IDs in today's lineup for the given team.
    Empty list means lineup data is not yet posted.
    Used for scratch detection: if lineup IS posted but player isn't in it,
    they are likely scratched.
    """
    from vortextime import vortex_board_day
    today = vortex_board_day()
    data  = _get("/schedule", {
        "sportId": 1,
        "date":    today,
        "hydrate": "lineups",
    }, cache_key=None)
    if not data:
        return []
    team_str = str(team_id)
    for date_entry in data.get("dates", []):
        for g in date_entry.get("games", []):
            lineups = g.get("lineups") or {}
            home_id = str((g.get("teams") or {}).get("home", {}).get("team", {}).get("id", ""))
            away_id = str((g.get("teams") or {}).get("away", {}).get("team", {}).get("id", ""))
            side = "homePlayers" if team_str == home_id else ("awayPlayers" if team_str == away_id else None)
            if not side:
                continue
            ids = [p.get("id") for p in lineups.get(side, []) if p.get("id")]
            return ids
    return []


def get_team_lineup(team_id: int) -> list[dict]:
    """
    Return today's confirmed batting order for a team, in order (1-9).
    The schedule endpoint's lineups.{home,away}Players array IS already in
    batting-order sequence -- list index + 1 is the order, no separate
    battingOrder-code parsing needed.

    Returns [] if the lineup hasn't been posted yet.
    Returns [{order, id, name, position}], position = fielding abbreviation
    (e.g. "SS", "DH").
    """
    from vortextime import vortex_board_day
    today = vortex_board_day()
    data = _get("/schedule", {
        "sportId": 1, "date": today,
        "hydrate": "lineups",
    }, cache_key=f"lineups_{today}")
    if not data:
        return []
    team_str = str(team_id)
    for date_entry in data.get("dates", []):
        for g in date_entry.get("games", []):
            lineups = g.get("lineups") or {}
            home_id = str((g.get("teams") or {}).get("home", {}).get("team", {}).get("id", ""))
            away_id = str((g.get("teams") or {}).get("away", {}).get("team", {}).get("id", ""))
            side = "homePlayers" if team_str == home_id else ("awayPlayers" if team_str == away_id else None)
            if not side:
                continue
            players = lineups.get(side, [])
            confirmed = [p for p in players if str(p.get("battingOrder", "")).strip()]
            if len(confirmed) < 9:
                return []
            return [
                {
                    "order": int(str(p.get("battingOrder"))[0]),
                    "id": p.get("id"),
                    "name": p.get("fullName", ""),
                    "position": (p.get("primaryPosition") or {}).get("abbreviation", ""),
                }
                for p in sorted(confirmed, key=lambda item: int(str(item.get("battingOrder"))[0]))
                if p.get("id")
            ]
    return []


def get_team_hitters_roster(team_id: int) -> list[dict]:
    """
    Active-roster position players (non-pitchers), used as a fallback for
    the team-insights lineup view before tonight's actual batting order has
    been posted (get_team_lineup returns [] until then, often just a few
    hours before first pitch). No real batting-order exists yet, so this
    is sorted alphabetically instead of numbered 1-9.
    """
    if not team_id:
        return []
    data = _get(f"/teams/{team_id}/roster", {
        "rosterType": "active",
    }, cache_key=f"roster_active_{team_id}")
    if not data:
        return []
    out = []
    for p in data.get("roster", []):
        pos = (p.get("position") or {}).get("abbreviation", "")
        if pos == "P":
            continue
        person = p.get("person") or {}
        pid = person.get("id")
        if not pid:
            continue
        out.append({"id": pid, "name": person.get("fullName", ""), "position": pos})
    out.sort(key=lambda r: r["name"])
    return out


def get_batter_season_line(player_id: int) -> dict:
    """
    Quick season batting line for the lineup table: AB, AVG, HR, RBI, OPS, K%.
    Separate from get_pitcher_metrics/get_bvp_history's fuller stat sets --
    this is deliberately just the 6 columns the lineup grid displays.
    """
    data = _get(f"/people/{player_id}/stats", {
        "stats": "season", "group": "hitting",
        "season": SEASON, "sportId": 1,
    }, cache_key=f"season_hit_{player_id}_{SEASON}")
    splits = ((data or {}).get("stats") or [{}])[0].get("splits", [])
    if not splits:
        return {}
    s = splits[0].get("stat", {})
    pa = int(s.get("plateAppearances", 0) or 0)
    so = int(s.get("strikeOuts", 0) or 0)
    return {
        "ab":    int(s.get("atBats", 0) or 0),
        "avg":   s.get("avg", "---"),
        "hr":    int(s.get("homeRuns", 0) or 0),
        "rbi":   int(s.get("rbi", 0) or 0),
        "ops":   s.get("ops", "---"),
        "k_pct": round(so / pa * 100, 1) if pa else None,
    }


# ── 9. Team hitting environment ───────────────────────────────────────────────

def get_team_hitting_stats(team_id: int) -> dict:
    """
    Season team hitting stats from the MLB Stats API.
    Returns: avg, obp, slg, ops, runs_pg, wrc_proxy
    wrc_proxy = OPS / league_avg_ops * 100  (approximates wRC+)
    """
    from datetime import date as _date
    season     = _date.today().year
    cache_key  = f"team_hit_{team_id}_{season}"
    data       = _get(f"/teams/{team_id}/stats", {
        "stats":  "season",
        "group":  "hitting",
        "season": season,
    }, cache_key=cache_key)
    if not data:
        return {}
    splits = ((data.get("stats") or [{}])[0]).get("splits") or []
    if not splits:
        return {}
    s  = splits[0].get("stat", {})
    gp = max(int(s.get("gamesPlayed", 1) or 1), 1)
    try:
        ops_f     = float(s.get("ops", "0") or 0)
        wrc_proxy = round(ops_f / 0.728 * 100)   # 0.728 ≈ MLB avg OPS
    except (ValueError, TypeError):
        wrc_proxy = 100
    return {
        "avg":       s.get("avg",      ".000"),
        "obp":       s.get("obp",      ".000"),
        "slg":       s.get("slg",      ".000"),
        "ops":       s.get("ops",      ".000"),
        "runs_pg":   round(int(s.get("runs", 0) or 0) / gp, 2),
        "wrc_proxy": wrc_proxy,
        "games":     gp,
    }


_LEAGUE_AVG_ERA = 4.05  # modern-era MLB baseline, used only to scale runs_pg by pitching quality

def get_team_run_environment(team_id: int, opp_starter_era, opp_bullpen_era, park_factor: float = 1.0) -> dict:
    """
    Simple, transparent projected-runs estimate for a team tonight:
    their own season runs/game, scaled by how the tonight's blended
    opposing pitching quality (60% starter / 40% bullpen -- a starter
    throws ~60% of a game's innings on average) compares to league-average
    ERA, then adjusted for park factor. This is NOT a full run-expectancy
    model (no lineup construction, baserunning, or weather beyond park
    factor) -- it's a deliberately simple, explainable estimate from real
    season data, not a black box.
    """
    hitting = get_team_hitting_stats(team_id)
    runs_pg = hitting.get("runs_pg")
    if runs_pg is None:
        return {}

    eras = [e for e in (opp_starter_era, opp_bullpen_era) if e]
    if len(eras) == 2:
        blended_era = float(opp_starter_era) * 0.6 + float(opp_bullpen_era) * 0.4
    elif eras:
        blended_era = float(eras[0])
    else:
        blended_era = _LEAGUE_AVG_ERA

    # Higher opposing ERA (worse pitching) -> MORE expected runs, not fewer.
    pitching_factor = blended_era / _LEAGUE_AVG_ERA
    projected_runs = round(runs_pg * pitching_factor * park_factor, 2)

    return {
        "season_runs_pg": runs_pg,
        "opp_blended_era": round(blended_era, 2),
        "projected_runs": projected_runs,
    }


# ── 10a. Opponent hitting stats against a team's pitching ─────────────────────

def get_team_opponent_stats(team_id: int) -> dict:
    """
    Get opponent hitting stats *against* this team's pitching (group=hitting,
    opposing=true).  Returns opp_avg, opp_obp, opp_slg, opp_ops, opp_k_rate,
    opp_bb_rate.
    """
    from datetime import date as _date
    season    = _date.today().year
    cache_key = f"team_opp_hit_{team_id}_{season}"
    data      = _get(f"/teams/{team_id}/stats", {
        "stats":   "season",
        "group":   "hitting",
        "opposing": "true",
        "season":  season,
    }, cache_key=cache_key)
    if not data:
        return {}
    splits = ((data.get("stats") or [{}])[0]).get("splits") or []
    if not splits:
        return {}
    s = splits[0].get("stat", {})
    try:
        pa  = max(int(s.get("plateAppearances", 1) or 1), 1)
        k_rate = round(int(s.get("strikeOuts", 0)) / pa * 100, 1)
        bb_rate = round(int(s.get("baseOnBalls", 0)) / pa * 100, 1)
        avg_f = float(s.get("avg", ".000") or 0)
    except (ValueError, TypeError):
        k_rate, bb_rate, avg_f = 22.0, 8.5, 0.250
    return {
        "avg":      s.get("avg",   ".---"),
        "obp":      s.get("obp",   ".---"),
        "slg":      s.get("slg",   ".---"),
        "ops":      s.get("ops",   ".---"),
        "k_rate":   k_rate,
        "bb_rate":  bb_rate,
        "avg_f":    avg_f,
    }


# ── 10. Bullpen stats ─────────────────────────────────────────────────────────

def get_bullpen_stats(team_id: int, starter_id: int = None) -> dict:
    """
    Approximate bullpen quality from the opposing team's overall pitching stats.
    Uses team-level pitching as a proxy (ERA, WHIP, K/9).
    Cache TTL: 1 hour (same as other stats).
    """
    from datetime import date as _date
    season    = _date.today().year
    cache_key = f"bullpen_{team_id}_{season}"
    data      = _get(f"/teams/{team_id}/stats", {
        "stats":  "season",
        "group":  "pitching",
        "season": season,
    }, cache_key=cache_key)
    if not data:
        return {}
    splits = ((data.get("stats") or [{}])[0]).get("splits") or []
    if not splits:
        return {}
    s = splits[0].get("stat", {})
    try:
        era_f  = float(s.get("era",  "4.50") or 4.50)
        whip_f = float(s.get("whip", "1.30") or 1.30)
        k9_f   = float(s.get("strikeoutsPer9Inn", "8.5") or 8.5)
    except (ValueError, TypeError):
        era_f, whip_f, k9_f = 4.50, 1.30, 8.5
    # Bullpen quality tier
    if era_f <= 3.50:
        tier = "ELITE"
    elif era_f <= 4.20:
        tier = "SOLID"
    elif era_f <= 5.00:
        tier = "AVERAGE"
    else:
        tier = "WEAK"
    return {
        "era":  s.get("era",  "-.--"),
        "whip": s.get("whip", "-.--"),
        "k9":   s.get("strikeoutsPer9Inn", "-.--"),
        "era_f":  era_f,
        "whip_f": whip_f,
        "k9_f":   k9_f,
        "tier":   tier,
    }


def get_player_current_team(player_id: int) -> int | None:
    """Return the team ID the player is currently rostered on."""
    profile = _get_player_profile(player_id)
    return profile.get("currentTeam", {}).get("id")


def build_pitcher_lookup(schedule: dict[int, dict]) -> dict[int, str]:
    """
    From today's schedule dict, build a map:
        team_id  →  opposing_pitcher_name

    For a batter on the home team, the opposing pitcher is the away starter,
    and vice versa.
    """
    lookup: dict[int, str] = {}
    for game in schedule.values():
        home_id = game.get("home_team_id")
        away_id = game.get("away_team_id")
        hp      = game.get("home_pitcher")   # home team's probable starter
        ap      = game.get("away_pitcher")   # away team's probable starter

        # Home batters face the away pitcher
        if home_id and ap:
            lookup[home_id] = ap
        # Away batters face the home pitcher
        if away_id and hp:
            lookup[away_id] = hp

    return lookup


def build_pitcher_id_lookup(schedule: dict[int, dict]) -> dict[int, int]:
    """
    From today's schedule, build a map:
        batter_team_id  →  opposing_pitcher_mlb_id

    Uses IDs directly from the schedule (no name search), so BvP lookups
    always reference the exact pitcher without any name-matching error.
    """
    lookup: dict[int, int] = {}
    for game in schedule.values():
        home_id = game.get("home_team_id")
        away_id = game.get("away_team_id")
        hp_id   = game.get("home_pitcher_id")
        ap_id   = game.get("away_pitcher_id")

        if home_id and ap_id:
            lookup[home_id] = ap_id
        if away_id and hp_id:
            lookup[away_id] = hp_id

    return lookup


# ── Park factors (2025 — update annually from Baseball Reference) ────────────
# Values > 1.0 = hitter-friendly (more runs, shorter pitcher outings)
# Values < 1.0 = pitcher-friendly (fewer runs, deeper outings)
PARK_FACTOR: dict[str, float] = {
    "Colorado Rockies":        1.38,  # Coors Field
    "Cincinnati Reds":         1.12,  # Great American Ball Park
    "Texas Rangers":           1.09,  # Globe Life Field
    "Baltimore Orioles":       1.08,  # Camden Yards
    "Boston Red Sox":          1.07,  # Fenway Park
    "Chicago Cubs":            1.05,  # Wrigley Field
    "Philadelphia Phillies":   1.04,
    "Toronto Blue Jays":       1.03,  # Rogers Centre
    "New York Yankees":        1.02,  # Yankee Stadium
    "Atlanta Braves":          1.01,  # Truist Park
    "Pittsburgh Pirates":      1.00,  # PNC Park
    "Kansas City Royals":      0.99,
    "Houston Astros":          0.99,  # Minute Maid Park
    "Los Angeles Angels":      0.99,
    "Arizona Diamondbacks":    0.99,  # Chase Field (retractable roof)
    "St. Louis Cardinals":     0.99,
    "Milwaukee Brewers":       0.99,
    "Chicago White Sox":       0.98,
    "Detroit Tigers":          0.98,
    "Tampa Bay Rays":          0.97,
    "Minnesota Twins":         0.97,  # Target Field
    "New York Mets":           0.97,  # Citi Field
    "Cleveland Guardians":     0.97,
    "Athletics":               1.35,  # Sutter Health Park
    "Oakland Athletics":       1.35,  # legacy API/team-name alias
    "Washington Nationals":    0.96,  # Nationals Park
    "Miami Marlins":           0.96,  # loanDepot Park
    "Los Angeles Dodgers":     0.96,  # Dodger Stadium
    "Seattle Mariners":        0.95,  # T-Mobile Park
    "San Diego Padres":        0.94,  # Petco Park
    "San Francisco Giants":    0.93,  # Oracle Park
}


def _ip_to_dec(ip_str: str) -> float:
    """Convert MLB innings pitched string (e.g. '6.1') to decimal innings."""
    try:
        parts = str(ip_str).split(".")
        full   = int(parts[0])
        thirds = int(parts[1]) / 3 if len(parts) > 1 and parts[1] else 0
        return full + thirds
    except (ValueError, IndexError):
        return 0.0


# ── Umpire K-rate lookup (update at season start) ────────────────────────────
# Tier is based on balls/strikes tendencies that affect total Ks per game.
# HIGH = umpire calls more Ks than league average → boosts K over props
# LOW  = umpire squeezes the zone → suppresses K over, helps K unders
# Source: cross-ref MLB Statcast umpire data / umpscorecards.com each spring.
UMPIRE_K_TIER: dict[str, str] = {
    # High K rate umpires
    "Vic Carapazza":    "HIGH",
    "Lance Barksdale":  "HIGH",
    "Mike Muchlinski":  "HIGH",
    "Alex Tosi":        "HIGH",
    "Phil Cuzzi":       "HIGH",
    "Stu Scheurwater":  "HIGH",
    # Low K rate umpires
    "Doug Eddings":     "LOW",
    "Jerry Meals":      "LOW",
    "Marty Foster":     "LOW",
    "Will Little":      "LOW",
    "Bill Miller":      "LOW",
    "Mike Estabrook":   "LOW",
}


def get_game_umpires() -> dict[int, str]:
    """
    Returns {team_id: home_plate_ump_name} for today's games.
    Calls the MLB schedule API with officials hydration.
    """
    from datetime import date
    today = date.today().strftime("%Y-%m-%d")
    data = _get("/schedule", {
        "sportId": 1,
        "date":    today,
        "hydrate": "officials,team",
    }, cache_key=f"umpires_{today}")

    result: dict[int, str] = {}
    for date_entry in (data or {}).get("dates", []):
        for g in date_entry.get("games", []):
            teams   = g.get("teams", {})
            home_id = teams.get("home", {}).get("team", {}).get("id")
            away_id = teams.get("away", {}).get("team", {}).get("id")

            ump_name = None
            for official in g.get("officials", []):
                if official.get("officialType") == "Home Plate":
                    ump_name = official.get("official", {}).get("fullName", "")
                    break

            if ump_name:
                if home_id:
                    result[home_id] = ump_name
                if away_id:
                    result[away_id] = ump_name

    return result


def _get_batter_statcast(player_name: str) -> dict:
    """
    Fetch barrel%, exit velocity, hard-hit% from the Baseball Savant
    batter leaderboard (cached daily).  Returns {} on any failure.
    """
    import csv, io as _io
    from datetime import date

    today      = date.today().isoformat()
    cache_file = CACHE_DIR / f"savant_batters_{today}.json"

    # Load or fetch the full leaderboard
    if cache_file.exists():
        with open(cache_file, encoding="utf-8") as f:
            leaderboard = json.load(f)
    else:
        try:
            import requests as _req
            r = _req.get(
                "https://baseballsavant.mlb.com/leaderboard/statcast",
                params={"type": "batter", "year": SEASON,
                        "position": "", "team": "", "min": 5, "csv": "true"},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=12,
            )
            if r.status_code != 200:
                return {}
            text   = r.content.decode("utf-8-sig")
            reader = csv.DictReader(_io.StringIO(text))
            leaderboard = []
            for row in reader:
                raw = row.get("last_name, first_name", "")
                if ", " in raw:
                    last, first = raw.split(", ", 1)
                    name = f"{first} {last}"
                else:
                    name = raw
                try:
                    barrel = float(row.get("brl_percent") or row.get("barrel_batted_rate") or 0)
                    ev     = float(row.get("avg_hit_speed") or 0)
                    hh     = float(row.get("ev95percent") or 0)
                except (ValueError, TypeError):
                    barrel = ev = hh = 0.0
                leaderboard.append({"name": name, "barrel_pct": barrel,
                                     "exit_velocity": ev, "hard_hit_pct": hh})
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(leaderboard, f)
        except Exception as exc:
            log.warning("Savant batter leaderboard failed: %s", exc)
            return {}

    # Match by name (flexible: partial or full match)
    target = player_name.lower().strip()
    for entry in leaderboard:
        ename = entry.get("name", "").lower()
        if target == ename or target in ename or ename in target:
            return entry
    return {}


# ── 11. Pitch arsenal + batter vs pitch-type splits ─────────────────────────

_PITCH_NAMES = {
    "FF": "Four-seam FB", "SI": "Sinker", "FC": "Cutter",
    "SL": "Slider",       "ST": "Sweeper","SV": "Sweeper",
    "CU": "Curveball",    "KC": "Knuckle-curve",
    "CH": "Changeup",     "FS": "Splitter",
    "KN": "Knuckleball",  "EP": "Eephus",
}


def get_pitcher_arsenal(pitcher_id: int) -> list[dict]:
    """
    Return the pitcher's pitch mix sorted by usage%.
    Each entry: {pitch_type, pitch_name, pct, avg_speed}
    Uses MLB Stats API pitch arsenal endpoint (no key needed).
    """
    data = _get(
        f"/people/{pitcher_id}/stats",
        {"stats": "pitchArsenal", "season": SEASON, "sportId": 1,
         "group": "pitching"},
        cache_key=f"arsenal_{pitcher_id}_{SEASON}",
    )
    splits = ((data or {}).get("stats") or [{}])[0].get("splits", [])
    result = []
    for sp in splits:
        s  = sp.get("stat", {})
        pt = s.get("type", {}).get("code", "")
        pct = float(s.get("percentage", 0) or 0)
        spd = s.get("averageSpeed") or s.get("avgSpeed")
        result.append({
            "pitch_type": pt,
            "pitch_name": _PITCH_NAMES.get(pt, pt),
            "pct":        round(pct * 100, 1),
            "avg_speed":  round(float(spd), 1) if spd else None,
        })
    result.sort(key=lambda x: x["pct"], reverse=True)
    return result


def get_pitcher_strikeout_profile(pitcher_id: int) -> dict:
    """Return free Statcast pitcher skill and pitch-level swing/miss data.

    The MLB Stats API supplies the results (K/9, BB/9); Baseball Savant
    supplies the process underneath them (Whiff%, Chase%, velocity and
    expected contact).  Leaderboards are downloaded once per VORTEX day and
    cached as a player-id table so enriching a full slate stays inexpensive.
    Missing columns remain absent rather than being converted to fake zeroes.
    """
    import csv as _csv
    import io as _io
    from vortextime import vortex_day

    today = vortex_day()
    cache_file = CACHE_DIR / f"savant_pitcher_k_profile_v2_{today}.json"
    table = None
    if cache_file.exists():
        try:
            table = json.loads(cache_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            table = None

    def _number(row: dict, *keys, percent: bool = False):
        for key in keys:
            raw = str(row.get(key, "")).strip().replace("%", "")
            if not raw or raw.lower() in ("null", "none", "nan"):
                continue
            try:
                value = float(raw)
                if percent and 0 < abs(value) <= 1:
                    value *= 100
                return round(value, 2)
            except (TypeError, ValueError):
                continue
        return None

    def _fetch(url: str, params: dict):
        try:
            response = requests.get(url, params=params,
                                    headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
            if response.ok:
                return list(_csv.DictReader(_io.StringIO(
                    response.content.decode("utf-8-sig"))))
        except requests.RequestException as exc:
            log.warning("Statcast pitcher leaderboard failed: %s", exc)
        return []

    if table is None:
        table = {}
        expected_rows = _fetch(
            "https://baseballsavant.mlb.com/leaderboard/expected_statistics",
            {"type": "pitcher", "year": SEASON, "min": "1", "csv": "true"},
        )
        for row in expected_rows:
            pid = str(row.get("player_id", "")).strip()
            if not pid:
                continue
            profile = table.setdefault(pid, {})
            aliases = {
                "xba": ("xba", "est_ba"),
                "xslg": ("xslg", "est_slg"),
                "xwoba": ("xwoba", "est_woba"),
                "hard_hit_pct": ("hard_hit_percent", "hardhit_percent"),
                "barrel_pct": ("brl_percent", "barrel_percent"),
                "gb_pct": ("groundballs_percent", "ground_ball_percent", "gb_percent"),
            }
            for name, keys in aliases.items():
                value = _number(row, *keys, percent=name.endswith("_pct"))
                if value is not None:
                    profile[name] = value

        discipline_rows = _fetch(
            "https://baseballsavant.mlb.com/leaderboard/plate-discipline",
            {"type": "pitcher", "year": SEASON, "min": "1", "csv": "true"},
        )
        for row in discipline_rows:
            pid = str(row.get("player_id", "")).strip()
            if not pid:
                continue
            profile = table.setdefault(pid, {})
            aliases = {
                "whiff_pct": ("whiff_percent", "whiff_pct"),
                "chase_pct": ("o_swing_percent", "chase_rate", "chase_percent"),
                "zone_contact_pct": ("z_contact_percent", "zone_contact_percent"),
                "fastball_velocity": ("fastball_avg_speed", "fb_velocity", "avg_fastball_speed"),
            }
            for name, keys in aliases.items():
                value = _number(row, *keys, percent=name.endswith("_pct"))
                if value is not None:
                    profile[name] = value

        arsenal_rows = _fetch(
            "https://baseballsavant.mlb.com/leaderboard/pitch-arsenal-stats",
            {"type": "pitcher", "pitchType": "", "year": SEASON,
             "team": "", "min": "1", "csv": "true"},
        )
        for row in arsenal_rows:
            pid = str(row.get("player_id", "")).strip()
            if not pid:
                continue
            pitch_type = str(row.get("pitch_type", "")).strip()
            pitches = int(_number(row, "pitches") or 0)
            if not pitch_type or pitches < 10:
                continue
            pitch = {
                "pitch_type": pitch_type,
                "pitch_name": row.get("pitch_name") or _PITCH_NAMES.get(pitch_type, pitch_type),
                "pitches": pitches,
            }
            for name, keys in {
                "usage_pct": ("pitch_usage", "usage_percent", "pitch_percent"),
                "velocity": ("velocity", "avg_speed"),
                "whiff_pct": ("whiff_percent", "whiff_pct"),
                "putaway_pct": ("put_away", "putaway_percent", "put_away_percent"),
                "k_pct": ("k_percent", "k_pct"),
                "hard_hit_pct": ("hard_hit_percent", "hardhit_percent"),
            }.items():
                value = _number(row, *keys, percent=name.endswith("_pct"))
                if value is not None:
                    pitch[name] = value
            table.setdefault(pid, {}).setdefault("arsenal", []).append(pitch)

        for profile in table.values():
            profile["arsenal"] = sorted(
                profile.get("arsenal", []), key=lambda p: p.get("pitches", 0), reverse=True
            )[:8]
            total_pitches = sum(p.get("pitches", 0) for p in profile["arsenal"])
            if total_pitches:
                for metric in ("whiff_pct", "putaway_pct", "k_pct", "hard_hit_pct"):
                    available = [p for p in profile["arsenal"] if p.get(metric) is not None]
                    metric_pitches = sum(p.get("pitches", 0) for p in available)
                    if metric_pitches:
                        profile[metric] = round(sum(
                            p[metric] * p.get("pitches", 0) for p in available
                        ) / metric_pitches, 2)
        if table:
            try:
                cache_file.write_text(json.dumps(table), encoding="utf-8")
            except OSError:
                pass

    return (table or {}).get(str(pitcher_id), {})


def get_lineup_arsenal_matchup(team_id: int, pitcher_id: int) -> dict:
    """Compare a pitcher's mix with tonight's confirmed nine-man lineup.

    Returns lineup-weighted Whiff% and K% against the pitcher's actual pitch
    types.  No roster proxy is used: before a lineup is confirmed this signal
    is deliberately unavailable and has no effect on the grade.
    """
    lineup = get_team_lineup(team_id)
    arsenal = get_pitcher_arsenal(pitcher_id)
    if len(lineup) < 9 or not arsenal:
        return {}

    pitch_weights = {
        p.get("pitch_type"): float(p.get("pct") or 0) / 100
        for p in arsenal if p.get("pitch_type") and float(p.get("pct") or 0) >= 5
    }
    total_mix = sum(pitch_weights.values())
    if total_mix <= 0:
        return {}

    batters = []
    for batter in lineup:
        by_pitch = {p.get("pitch_type"): p for p in get_batter_arsenal_stats(batter["id"])}
        covered = whiff_sum = k_sum = 0.0
        for pitch_type, weight in pitch_weights.items():
            stats = by_pitch.get(pitch_type) or {}
            if int(stats.get("pa") or 0) < 10:
                continue
            try:
                whiff = float(stats.get("whiff_pct") or 0)
                kpct = float(stats.get("k_pct") or 0)
            except (TypeError, ValueError):
                continue
            if 0 < whiff <= 1:
                whiff *= 100
            if 0 < kpct <= 1:
                kpct *= 100
            covered += weight
            whiff_sum += whiff * weight
            k_sum += kpct * weight
        if covered >= 0.35:
            batters.append({
                "order": batter.get("order"), "name": batter.get("name", ""),
                "coverage": round(covered / total_mix * 100, 1),
                "whiff_pct": round(whiff_sum / covered, 1),
                "k_pct": round(k_sum / covered, 1),
            })

    if len(batters) < 6:
        return {}
    return {
        "confirmed": True,
        "batters": batters,
        "coverage": round(sum(b["coverage"] for b in batters) / len(batters), 1),
        "whiff_pct": round(sum(b["whiff_pct"] for b in batters) / len(batters), 1),
        "k_pct": round(sum(b["k_pct"] for b in batters) / len(batters), 1),
        "threats": sorted(batters, key=lambda b: b["k_pct"])[:3],
    }


def get_batter_vs_pitch_type(batter_id: int, pitcher_id: int) -> list[dict]:
    """
    Return batter's career stats split by pitch type vs this pitcher.
    Uses MLB Stats API vsPlayer splits with pitchType group.
    Falls back to season-wide batter pitch-type splits if no vs-pitcher data.
    Each entry: {pitch_type, pitch_name, pa, avg, slg, ops, whiff_pct}
    """
    # Try vs-pitcher first
    data = _get(
        f"/people/{batter_id}/stats",
        {"stats": "vsPlayer", "opposingPlayerId": pitcher_id,
         "group": "hitting", "season": SEASON, "sportId": 1},
        cache_key=f"bvp_pitch_{batter_id}_{pitcher_id}_{SEASON}",
    )
    splits = ((data or {}).get("stats") or [{}])[0].get("splits", [])

    # Season-wide pitch-type splits as fallback
    if not splits:
        data = _get(
            f"/people/{batter_id}/stats",
            {"stats": "pitchArsenal", "group": "hitting",
             "season": SEASON, "sportId": 1},
            cache_key=f"bat_pitch_{batter_id}_{SEASON}",
        )
        splits = ((data or {}).get("stats") or [{}])[0].get("splits", [])

    result = []
    for sp in splits:
        s  = sp.get("stat", {})
        pt = sp.get("pitchType", {}).get("code", "") or s.get("type", {}).get("code", "")
        if not pt:
            continue
        pa = int(s.get("plateAppearances", 0) or s.get("atBats", 0) or 0)
        if pa < 5:
            continue
        result.append({
            "pitch_type": pt,
            "pitch_name": _PITCH_NAMES.get(pt, pt),
            "pa":         pa,
            "avg":        s.get("avg", ".---"),
            "slg":        s.get("slg", ".---"),
            "ops":        s.get("ops", ".---"),
        })
    result.sort(key=lambda x: x["pa"], reverse=True)
    return result


# ── 12. Stadium coordinates + weather ───────────────────────────────────────
# (lat, lon, cf_bearing°, is_dome)
# cf_bearing: compass direction from home plate toward center field
# Wind FROM cf_bearing → blowing IN (pitcher-friendly)
# Wind FROM cf_bearing±180 → blowing OUT (hitter-friendly)
STADIUM_DATA: dict[str, tuple[float, float, int, bool]] = {
    "ATL": (33.8900, -84.4681,  35, False),  # Truist Park
    "BAL": (39.2838, -76.6215,  10, False),  # Camden Yards
    "BOS": (42.3467, -71.0972,  50, False),  # Fenway Park
    "CHC": (41.9484, -87.6553,  30, False),  # Wrigley Field
    "CWS": (41.8300, -87.6340,  20, False),  # Guaranteed Rate
    "CIN": (39.0975, -84.5069,  30, False),  # GABP
    "CLE": (41.4962, -81.6852,  25, False),  # Progressive Field
    "COL": (39.7561,-104.9942,  20, False),  # Coors Field
    "DET": (42.3390, -83.0485,  30, False),  # Comerica Park
    "KC":  (39.0517, -94.4803,  20, False),  # Kauffman Stadium
    "LAA": (33.8003,-117.8827, 355, False),  # Angel Stadium
    "LAD": (34.0739,-118.2400,  20, False),  # Dodger Stadium
    "MIN": (44.9817, -93.2778,  30, False),  # Target Field
    "NYM": (40.7571, -73.8458,  20, False),  # Citi Field
    "NYY": (40.8296, -73.9262,  10, False),  # Yankee Stadium
    "OAK": (37.7516,-122.2005,  30, False),  # Oakland Coliseum
    "PHI": (39.9061, -75.1665,  15, False),  # Citizens Bank Park
    "PIT": (40.4469, -80.0057,  15, False),  # PNC Park
    "SD":  (32.7076,-117.1570, 335, False),  # Petco Park
    "SEA": (47.5914,-122.3325,  25, False),  # T-Mobile Park
    "SF":  (37.7786,-122.3893, 350, False),  # Oracle Park
    "STL": (38.6226, -90.1928,  20, False),  # Busch Stadium
    "WSH": (38.8730, -77.0074,  30, False),  # Nationals Park
    # Retractable roof / dome — weather not applicable
    "ARI": (33.4453,-112.0667, 320,  True),
    "HOU": (29.7573, -95.3554,  25,  True),
    "MIA": (25.7781, -80.2196,  30,  True),
    "MIL": (43.0283, -87.9712,  20,  True),
    "TB":  (27.7682, -82.6534,  20,  True),
    "TEX": (32.7512, -97.0832,  30,  True),
    "TOR": (43.6414, -79.3894,  25,  True),
}

# Elevation materially changes air density. Values are deliberately rounded;
# they are environment inputs, not pretend fence-level precision.
STADIUM_ELEVATION_FT = {
    "ATL": 1000, "ARI": 1080, "BAL": 40, "BOS": 20, "CHC": 600,
    "CIN": 480, "CLE": 650, "COL": 5200, "CWS": 595, "DET": 600,
    "HOU": 45, "KC": 750, "LAA": 150, "LAD": 520, "MIA": 10,
    "MIL": 635, "MIN": 840, "NYM": 20, "NYY": 55, "OAK": 20,
    "PHI": 20, "PIT": 730, "SD": 15, "SEA": 15, "SF": 10,
    "STL": 465, "TB": 45, "TEX": 550, "TOR": 250, "WSH": 25,
}
RETRACTABLE_ROOF_PARKS = {"ARI", "HOU", "MIA", "MIL", "TEX", "TOR"}
FIXED_ROOF_PARKS = {"TB"}


def _hr_external_get(url: str, *, params: dict, timeout: int):
    """Low-volume external fetch with bounded backoff for HR model sources."""
    last_error = None
    headers = {"User-Agent": "VORTEX-HR-Sniper/1.0 github.com/Santos4yu/vortexs"}
    for attempt in range(3):
        try:
            response = requests.get(url, params=params, headers=headers, timeout=timeout)
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(.6 * (2 ** attempt))
    raise last_error


def get_hr_park_factors_by_hand() -> dict[str, dict[str, float]]:
    """Three-year rolling Statcast HR park index by home team and bat side.

    Baseball Savant defines 100 as neutral and controls the comparison for the
    batters and pitchers appearing both in that venue and elsewhere.
    """
    cache_file = CACHE_DIR / f"savant_hr_park_by_hand_{SEASON}.json"
    stale = {}
    if cache_file.exists():
        try:
            stale = json.loads(cache_file.read_text(encoding="utf-8"))
            if time.time() - cache_file.stat().st_mtime < 7 * 86400:
                return stale
        except (OSError, json.JSONDecodeError):
            pass
    result: dict[str, dict[str, float]] = {}
    for side in ("L", "R"):
        try:
            response = _hr_external_get(
                "https://baseballsavant.mlb.com/leaderboard/statcast-park-factors",
                params={"batSide": side, "condition": "All", "parks": "mlb",
                        "rolling": "3", "stat": "index_HR", "type": "venue",
                        "year": SEASON},
                timeout=25,
            )
            response.raise_for_status()
            match = re.search(r"var data\s*=\s*(\[.*?\]);", response.text, re.S)
            if not match:
                continue
            rows = json.loads(match.group(1))
            value_key = f"metric_value_{SEASON}"
            for row in rows:
                team_id = str(row.get("main_team_id") or "")
                try:
                    value = float(row.get(value_key))
                except (TypeError, ValueError):
                    continue
                if team_id and 60 <= value <= 160:
                    result.setdefault(team_id, {})[side] = value
        except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
            log.warning("Savant HR park-factor fetch failed for %s: %s", side, exc)
    if result:
        try:
            cache_file.write_text(json.dumps(result), encoding="utf-8")
        except OSError:
            pass
    return result or stale


def get_batter_batted_ball_profiles() -> dict[str, dict]:
    """Cached Savant air/pull profile for hitters with at least 25 BBE."""
    cache_file = CACHE_DIR / f"savant_batted_ball_profiles_{SEASON}.json"
    stale = {}
    if cache_file.exists():
        try:
            stale = json.loads(cache_file.read_text(encoding="utf-8"))
            if time.time() - cache_file.stat().st_mtime < 12 * 3600:
                return stale
        except (OSError, json.JSONDecodeError):
            pass
    result = {}
    try:
        response = _hr_external_get(
            "https://baseballsavant.mlb.com/leaderboard/batted-ball",
            params={"type": "batter", "year": SEASON, "min": 25},
            timeout=25,
        )
        response.raise_for_status()
        match = re.search(r"const data\s*=\s*(\[.*?\]);", response.text, re.S)
        if match:
            for row in json.loads(match.group(1)):
                pid = str(row.get("savant_batter_id") or row.get("id") or "")
                try:
                    bbe = int(row.get("num_bbe") or 0)
                    pull_air = float(row.get("pull_air_rate"))
                    air = float(row.get("air_rate"))
                    fly_ball = float(row.get("fb_rate"))
                except (TypeError, ValueError):
                    continue
                if pid and bbe >= 25 and all(0 <= v <= 1 for v in (pull_air, air, fly_ball)):
                    result[pid] = {"bbe": bbe, "pull_air_rate": pull_air,
                                   "air_rate": air, "fly_ball_rate": fly_ball}
    except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
        log.warning("Savant batted-ball profile fetch failed: %s", exc)
    if result:
        try:
            cache_file.write_text(json.dumps(result), encoding="utf-8")
        except OSError:
            pass
    return result or stale


def get_game_roof_status(game_pk: int, home_team_abbr: str) -> str:
    """Return open/closed/fixed/outdoor/unknown without guessing roof state."""
    abbr = (home_team_abbr or "").upper()
    if abbr in FIXED_ROOF_PARKS:
        return "fixed"
    if abbr not in RETRACTABLE_ROOF_PARKS:
        return "outdoor"

    # The v1 live-feed route returns 404 before many future games are
    # initialized. Schedule hydration is the supported pregame source and
    # avoids filling logs with harmless 404s during an overnight board scan.
    data = _get("/schedule", {
        "sportId": 1,
        "gamePk": game_pk,
        "hydrate": "venue(fieldInfo),weather",
    }, cache_key=f"roof_schedule_{game_pk}") or {}
    games = [game for date_entry in (data.get("dates") or [])
             for game in (date_entry.get("games") or [])]
    game = games[0] if games else {}
    venue = game.get("venue") or {}
    candidates = [
        (game.get("weather") or {}).get("condition"),
        (game.get("weather") or {}).get("wind"),
        (venue.get("fieldInfo") or {}).get("roofType"),
    ]
    text = " ".join(str(v or "") for v in candidates).lower()
    if "closed" in text or "dome" in text or "indoor" in text:
        return "closed"
    if "open" in text:
        return "open"
    return "unknown"


def get_commercial_game_weather(home_team_abbr: str, game_time_utc: str,
                                game_pk: int) -> dict:
    """Commercially reusable game-time forecast from MET Norway (CC BY 4.0).

    Cached by venue/game hour. The service requires an identifying User-Agent;
    VORTEX uses its public repository rather than a fake browser identity.
    """
    from datetime import datetime, timezone as _tz
    abbr = (home_team_abbr or "").upper()
    stadium = STADIUM_DATA.get(abbr)
    if not stadium:
        return {"error": "stadium metadata missing", "source": "MET Norway"}
    lat, lon, cf_bearing, _ = stadium
    roof = get_game_roof_status(game_pk, abbr)
    if roof in ("fixed", "closed"):
        return {"dome": True, "roof_status": roof, "source": "MET Norway",
                "attribution": "Weather data: MET Norway (CC BY 4.0)"}
    if roof == "unknown":
        return {"error": "retractable roof status unknown", "roof_status": roof,
                "source": "MET Norway"}
    try:
        target = datetime.fromisoformat(str(game_time_utc).replace("Z", "+00:00")).astimezone(_tz.utc)
    except (TypeError, ValueError):
        return {"error": "invalid game time", "roof_status": roof, "source": "MET Norway"}
    cache_file = CACHE_DIR / f"metno_{abbr}_{target.strftime('%Y%m%d%H')}.json"
    payload = None
    stale_payload = None
    used_stale = False
    if cache_file.exists():
        try:
            stale_payload = json.loads(cache_file.read_text(encoding="utf-8"))
            if time.time() - cache_file.stat().st_mtime < 3600:
                payload = stale_payload
        except (OSError, json.JSONDecodeError):
            pass
    if payload is None:
        try:
            response = _hr_external_get(
                "https://api.met.no/weatherapi/locationforecast/2.0/compact",
                params={"lat": round(lat, 4), "lon": round(lon, 4),
                        "altitude": STADIUM_ELEVATION_FT.get(abbr, 0) // 3},
                timeout=12,
            )
            response.raise_for_status()
            payload = response.json()
            cache_file.write_text(json.dumps(payload), encoding="utf-8")
        except (requests.RequestException, OSError, ValueError) as exc:
            if stale_payload is None:
                return {"error": str(exc), "roof_status": roof, "source": "MET Norway"}
            payload = stale_payload
            used_stale = True
    points = ((payload.get("properties") or {}).get("timeseries") or [])
    if not points:
        return {"error": "empty forecast", "roof_status": roof, "source": "MET Norway"}
    def point_time(point):
        try:
            return datetime.fromisoformat(point["time"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            return datetime.max.replace(tzinfo=_tz.utc)
    point = min(points, key=lambda p: abs((point_time(p) - target).total_seconds()))
    instant = (((point.get("data") or {}).get("instant") or {}).get("details") or {})
    next_hour = (((point.get("data") or {}).get("next_1_hours") or {}).get("details") or {})
    def number(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    temp_c = number(instant.get("air_temperature"))
    wind_ms = number(instant.get("wind_speed"))
    wind_from = number(instant.get("wind_from_direction"))
    effect, friendly = _wind_effect(wind_from, cf_bearing) if wind_from is not None else ("unknown", None)
    return {
        "temp_f": round(temp_c * 9 / 5 + 32, 1) if temp_c is not None else None,
        "speed_mph": round(wind_ms * 2.23694, 1) if wind_ms is not None else None,
        "direction_deg": wind_from, "effect": effect, "hitter_friendly": friendly,
        "humidity_pct": number(instant.get("relative_humidity")),
        "pressure_hpa": number(instant.get("air_pressure_at_sea_level")),
        # Locationforecast exposes forecast precipitation amount, not a
        # probability. Preserve that distinction; never relabel amount as PoP.
        "precip_amount_mm": number(next_hour.get("precipitation_amount")),
        "precip_probability_pct": number(next_hour.get("probability_of_precipitation")),
        "elevation_ft": STADIUM_ELEVATION_FT.get(abbr), "dome": False,
        "roof_status": roof, "forecast_time": point.get("time"), "source": "MET Norway",
        "stale_fallback": used_stale,
        "attribution": "Weather data: MET Norway (CC BY 4.0)",
    }


def _wind_effect(wind_from_deg: float, cf_bearing: int) -> tuple[str, bool | None]:
    """Return (description, is_hitter_friendly) for given wind vs park orientation."""
    diff = (wind_from_deg - cf_bearing) % 360
    if diff > 180:
        diff -= 360
    abs_diff = abs(diff)
    if abs_diff <= 45:
        side = "LCF" if diff < -15 else ("RCF" if diff > 15 else "CF")
        return f"in from {side}", False
    elif abs_diff >= 135:
        side = "CF" if abs_diff >= 165 else ("LF" if diff < 0 else "RF")
        return f"out to {side}", True
    elif diff < 0:
        return "crosswind (RF→LF)", None
    else:
        return "crosswind (LF→RF)", None


def get_game_weather(home_team_abbr: str, game_time_utc: str = "") -> dict:
    """
    Fetch wind conditions at tonight's venue via Open-Meteo (free, no key).

    If game_time_utc is provided (e.g. "2026-06-14T23:10:00Z"), uses the hourly
    forecast for that specific hour — giving game-time conditions, not current ones.
    Falls back to current conditions if the time is missing or the forecast fails.

    Returns {"speed_mph", "direction_deg", "effect", "hitter_friendly", "dome", "forecast"}.
    """
    from datetime import datetime, timezone as _tz

    stadium = STADIUM_DATA.get(home_team_abbr.upper())
    if not stadium:
        return {"error": "Stadium not found"}

    lat, lon, cf_bearing, is_dome = stadium
    if is_dome:
        return {"dome": True, "effect": "Indoor — wind N/A", "hitter_friendly": None,
                "speed_mph": 0, "forecast": False}

    # ── Try hourly game-time forecast first ───────────────────────────────────
    if game_time_utc:
        try:
            gdt        = datetime.strptime(game_time_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_tz.utc)
            game_date  = gdt.strftime("%Y-%m-%d")
            target_str = gdt.strftime("%Y-%m-%dT%H:00")   # e.g. "2026-06-14T23:00"

            url = (
                f"https://api.open-meteo.com/v1/forecast"
                f"?latitude={lat}&longitude={lon}"
                f"&hourly=wind_speed_10m,wind_direction_10m,temperature_2m"
                f"&wind_speed_unit=mph&temperature_unit=fahrenheit&timezone=UTC"
                f"&start_date={game_date}&end_date={game_date}"
            )
            resp   = requests.get(url, timeout=8)
            resp.raise_for_status()
            hourly = resp.json().get("hourly", {})
            times  = hourly.get("time", [])
            speeds = hourly.get("wind_speed_10m", [])
            dirs   = hourly.get("wind_direction_10m", [])
            temps  = hourly.get("temperature_2m", [])

            if target_str in times:
                idx = times.index(target_str)
            else:
                idx = min(gdt.hour, len(speeds) - 1)

            speed_mph = round(speeds[idx], 1)
            wind_from = dirs[idx]
            temp_f    = round(temps[idx]) if idx < len(temps) else None
            effect, hf = _wind_effect(wind_from, cf_bearing)
            return {
                "speed_mph":       speed_mph,
                "direction_deg":   wind_from,
                "effect":          effect,
                "hitter_friendly": hf,
                "temp_f":          temp_f,
                "dome":            False,
                "forecast":        True,
            }
        except Exception:
            pass   # fall through to current conditions

    # ── Fallback: current conditions ──────────────────────────────────────────
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&current=wind_speed_10m,wind_direction_10m,temperature_2m"
        f"&wind_speed_unit=mph&temperature_unit=fahrenheit&timezone=auto"
    )
    try:
        resp      = requests.get(url, timeout=6)
        resp.raise_for_status()
        curr      = resp.json().get("current", {})
        speed_mph = round(curr.get("wind_speed_10m", 0), 1)
        wind_from = curr.get("wind_direction_10m", 0)
        temp_f    = round(curr["temperature_2m"]) if curr.get("temperature_2m") is not None else None
        effect, hf = _wind_effect(wind_from, cf_bearing)
        return {
            "speed_mph":       speed_mph,
            "direction_deg":   wind_from,
            "effect":          effect,
            "hitter_friendly": hf,
            "temp_f":          temp_f,
            "dome":            False,
            "forecast":        False,
        }
    except Exception as exc:
        return {"error": str(exc)}


# ── 12. Career stats vs opposing team ────────────────────────────────────────

def get_team_bvp(batter_id: int, opp_team_id: int) -> dict:
    """
    Career hitting stats for this batter against the opposing team (all pitchers).
    Uses the vsTeam split from the MLB Stats API.
    """
    data = _get(f"/people/{batter_id}/stats", {
        "stats":            "vsTeam",
        "group":            "hitting",
        "season":           SEASON,
        "opposingTeamId":   opp_team_id,
        "sportId":          1,
    }, cache_key=f"team_bvp_{batter_id}_vs_{opp_team_id}_{SEASON}")

    splits = ((data or {}).get("stats") or [{}])[0].get("splits", [])
    if not splits:
        return {"pa": 0, "ab": 0}

    ab = hits = hr = rbi = bb = k = pa = tb = 0
    for sp in splits:
        s     = sp.get("stat", {})
        ab   += int(s.get("atBats",          0))
        hits += int(s.get("hits",            0))
        hr   += int(s.get("homeRuns",        0))
        rbi  += int(s.get("rbi",             0))
        bb   += int(s.get("baseOnBalls",     0))
        k    += int(s.get("strikeOuts",      0))
        pa   += int(s.get("plateAppearances",0))
        tb   += int(s.get("totalBases",      0))

    # Standard float formatting — robust when AVG = 1.000 or OPS ≥ 1.000
    # (the old ".{x*1000:03d}" pattern mangled any value ≥ 1.0, e.g. 1.194 → ".1194").
    avg = f"{hits / ab:.3f}" if ab > 0 else ".---"
    obp = round((hits + bb) / pa, 3) if pa > 0 else 0
    slg = round(tb / ab, 3) if ab > 0 else 0
    ops = round(obp + slg, 3)
    ops_str = f"{ops:.3f}" if ab > 0 else ".---"

    return {
        "pa":    pa,
        "ab":    ab,
        "hits":  hits,
        "avg":   avg,
        "hr":    hr,
        "rbi":   rbi,
        "ops":   ops_str,
    }


# ── 13. Team defense — Outs Above Average (Baseball Savant) ─────────────────

def _fetch_team_oaa_table() -> dict[str, int]:
    """Download OAA leaderboard CSV from Baseball Savant and sum by team_id."""
    import csv
    from io import StringIO
    url = (
        "https://baseballsavant.mlb.com/leaderboard/outs_above_average"
        f"?type=Fielding&year={SEASON}&min=1&pos=all&team=all&csv=true"
    )
    try:
        resp = requests.get(url, timeout=10,
                            headers={"User-Agent": "Mozilla/5.0 Vortex/1.0"})
        resp.raise_for_status()
        reader = csv.DictReader(StringIO(resp.text))
        table: dict[str, int] = {}
        for row in reader:
            try:
                tid = str(int(float(row.get("team_id") or 0)))
                if tid == "0":
                    continue
                # Column name varies slightly by year
                oaa_raw = (row.get("outs_above_average") or
                           row.get("n_oaa") or row.get("oaa") or "0")
                oaa = int(float(oaa_raw))
                table[tid] = table.get(tid, 0) + oaa
            except (ValueError, TypeError):
                continue
        return table
    except Exception as exc:
        log.warning("OAA fetch failed: %s", exc)
        return {}


def get_team_defense_oaa(team_id: int) -> dict:
    """Return the opposing team's season OAA (Outs Above Average)."""
    from datetime import date as _date
    today      = _date.today().strftime("%Y-%m-%d")
    cache_path = CACHE_DIR / f"team_oaa_{today}.json"

    if cache_path.exists():
        try:
            table = json.loads(cache_path.read_text())
        except Exception:
            table = {}
    else:
        table = _fetch_team_oaa_table()
        try:
            cache_path.write_text(json.dumps(table))
        except Exception:
            pass

    oaa = table.get(str(team_id))
    if oaa is None:
        return {"error": "No OAA data available"}
    return {"team_id": team_id, "oaa": int(oaa)}


# ── 13. Statcast by player ID (Baseball Savant expected stats) ───────────────

def get_statcast_by_id(player_id: int) -> dict:
    """
    Fetch Statcast quality-of-contact + plate discipline from Baseball Savant.
    Combines expected_statistics (xSLG, xwOBA, Barrel%, HH%) and plate-discipline
    (Chase%, Zone-contact%, Whiff%) leaderboards — both cached once per day.
    Returns merged dict: {barrel_pct, hard_hit_pct, xslg, xwoba, chase_pct,
                          zone_contact_pct, whiff_pct, ...}
    """
    import csv, io as _io
    from vortextime import vortex_day
    today      = vortex_day()
    cache_file = CACHE_DIR / f"savant_full_{today}.json"

    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text("utf-8")).get(str(player_id), {})
        except Exception:
            pass

    def _sf(row: dict, *keys) -> float:
        for k in keys:
            try:
                v = str(row.get(k, "")).strip()
                if v:
                    return float(v)
            except Exception:
                pass
        return 0.0

    all_data: dict[str, dict] = {}

    # 1. Expected stats (xSLG, xwOBA, Barrel%, Hard-Hit%)
    try:
        r = requests.get(
            "https://baseballsavant.mlb.com/leaderboard/expected_statistics",
            params={"type": "batter", "year": SEASON, "min": "q", "csv": "true"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=25,
        )
        if r.ok:
            for row in csv.DictReader(_io.StringIO(r.content.decode("utf-8-sig"))):
                pid = str(row.get("player_id", "")).strip()
                if not pid:
                    continue
                all_data.setdefault(pid, {}).update({
                    "barrel_pct":     _sf(row, "brl_percent", "barrel_percent"),
                    "hard_hit_pct":   _sf(row, "hard_hit_percent"),
                    "sweet_spot_pct": _sf(row, "sweet_spot_percent"),
                    "exit_velocity":  _sf(row, "exit_velocity_avg"),
                    "xba":   str(row.get("xba") or row.get("est_ba", "")).strip(),
                    "xslg":  str(row.get("xslg") or row.get("est_slg", "")).strip(),
                    "xwoba": str(row.get("xwoba") or row.get("est_woba", "")).strip(),
                })
    except Exception as e:
        log.warning("Statcast expected_statistics fetch failed: %s", e)

    # 2. Plate discipline (Chase%, Zone-contact%, Whiff%)
    try:
        r2 = requests.get(
            "https://baseballsavant.mlb.com/leaderboard/plate-discipline",
            params={"type": "batter", "year": SEASON, "min": "q", "csv": "true"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=25,
        )
        if r2.ok:
            for row in csv.DictReader(_io.StringIO(r2.content.decode("utf-8-sig"))):
                pid = str(row.get("player_id", "")).strip()
                if not pid:
                    continue
                all_data.setdefault(pid, {}).update({
                    "chase_pct":        _sf(row, "o_swing_percent", "chase_rate"),
                    "zone_contact_pct": _sf(row, "z_contact_percent"),
                    "whiff_pct":        _sf(row, "whiff_percent"),
                    "zone_pct":         _sf(row, "zone_percent"),
                })
    except Exception as e:
        log.warning("Statcast plate discipline fetch failed: %s", e)

    if all_data:
        try:
            cache_file.write_text(json.dumps(all_data), encoding="utf-8")
        except Exception:
            pass

    return all_data.get(str(player_id), {})


# ── 14. Bullpen stats (last 7 days) ─────────────────────────────────────────

def _ip_to_float(ip: str) -> float:
    """'5.1' → 5.333 (5 innings + 1 out = 5⅓ IP)."""
    try:
        parts = str(ip).split(".")
        return int(parts[0]) + (int(parts[1]) if len(parts) > 1 else 0) / 3
    except Exception:
        return 0.0


def get_bullpen_stats(opp_team_id: int) -> dict:
    """
    Get opposing bullpen ERA, WHIP, HR/9 from last 7 days of games,
    and fatigued_count (pitchers with appearances in last 3 days).

    NOTE: `/schedule?hydrate=boxscore` does NOT actually embed per-player
    boxscore data (verified -- the "games" entries it returns have no
    "boxscore"/"liveData" key at all), so the previous version of this
    function silently returned {} for every team, every time. That's why
    the Attack Board bullpen tier always showed "AVERAGE" -- callers fell
    through to the hardcoded era=4.5 default. Fixed by pulling each
    completed game's real boxscore from /game/{gamePk}/boxscore.
    """
    if not opp_team_id:
        return {}

    from datetime import date as _date, timedelta
    today = _date.fromisoformat(vortextime.vortex_day())
    start = (today - timedelta(days=7)).isoformat()
    end   = today.isoformat()

    sched = _get("/schedule", {
        "sportId": 1, "teamId": opp_team_id,
        "startDate": start, "endDate": end,
        "gameType": "R",
    }, cache_key=f"bpen_sched_{opp_team_id}_{start}")

    if not sched:
        return _get_bullpen_stats_season_fallback(opp_team_id)

    game_pks: list[tuple[int, str]] = []
    for date_entry in sched.get("dates", []):
        game_date = date_entry.get("date", "")
        for game in date_entry.get("games", []):
            if (game.get("status") or {}).get("abstractGameState") != "Final":
                continue
            pk = game.get("gamePk")
            if pk:
                game_pks.append((pk, game_date))

    recent_apps: dict[int, list[str]] = {}
    total_ip = total_er = total_h = total_bb = total_hr = 0.0

    for pk, game_date in game_pks:
        box = _get(f"/game/{pk}/boxscore", cache_key=f"box_{pk}")
        if not box:
            continue
        for side in ("home", "away"):
            team_data = (box.get("teams") or {}).get(side, {})
            if (team_data.get("team") or {}).get("id") != opp_team_id:
                continue
            pitchers = team_data.get("pitchers", [])
            players  = team_data.get("players", {})
            for pid in pitchers[1:]:   # skip starter
                pdata = players.get(f"ID{pid}", {})
                stats = pdata.get("stats", {}).get("pitching", {})
                ip    = _ip_to_float(str(stats.get("inningsPitched", "0")))
                if ip <= 0:
                    continue
                total_ip += ip
                total_er += float(stats.get("earnedRuns",  0) or 0)
                total_h  += float(stats.get("hits",        0) or 0)
                total_bb += float(stats.get("baseOnBalls", 0) or 0)
                total_hr += float(stats.get("homeRuns",    0) or 0)
                recent_apps.setdefault(pid, []).append(game_date)
            break

    if total_ip < 3:
        # Too few relief innings logged in the last 7 days (early season,
        # off-days, etc.) to trust a recency-weighted read -- fall back to
        # full-season team bullpen quality instead of a fake "AVERAGE".
        return _get_bullpen_stats_season_fallback(opp_team_id)

    era  = round((total_er / total_ip) * 9, 2)
    whip = round((total_h + total_bb) / total_ip, 2)
    hr9  = round((total_hr / total_ip) * 9, 2)

    cutoff   = (today - timedelta(days=3)).isoformat()
    fatigued = sum(1 for dates in recent_apps.values()
                   if any(d >= cutoff for d in dates))

    return {
        "era":            era,
        "whip":           whip,
        "hr9":            hr9,
        "fatigued_count": fatigued,
        "total_pitchers": len(recent_apps),
        "total_ip":       round(total_ip, 1),
        "sample":         "l7",
        "model_usable":   total_ip >= 12,
    }


def _get_bullpen_stats_season_fallback(opp_team_id: int) -> dict:
    """Season-long team relief pitching as a fallback when the last-7-days
    boxscore sample is too thin to trust. Uses the same MLB team pitching
    stats already fetched elsewhere in this module (get_bullpen_stats'
    season-based sibling), just re-shaped to this function's field names."""
    season = SEASON
    data = _get(f"/teams/{opp_team_id}/stats", {
        "stats": "season", "group": "pitching", "season": season,
    }, cache_key=f"bpen_season_{opp_team_id}_{season}")
    if not data:
        return {}
    splits = ((data.get("stats") or [{}])[0]).get("splits") or []
    if not splits:
        return {}
    s = splits[0].get("stat", {})
    try:
        era  = float(s.get("era",  "4.50") or 4.50)
        whip = float(s.get("whip", "1.30") or 1.30)
        ip   = _ip_to_float(str(s.get("inningsPitched", "0")))
        hr9  = round((float(s.get("homeRuns", 0) or 0) / ip) * 9, 2) if ip > 0 else None
    except (ValueError, TypeError):
        era, whip, hr9 = 4.50, 1.30, None
    return {
        "era":            era,
        "whip":           whip,
        "hr9":            hr9,
        "fatigued_count": 0,
        "total_pitchers": None,
        "total_ip":       None,
        "sample":         "season",
        # The endpoint contains whole-team pitching, not relief-only stats.
        # Moneyline must not use this fallback as a bullpen model input.
        "model_usable":   False,
    }


# ── 15b. Team offensive profile (ISO, BB%, K%, wRC+) ────────────────────────

def get_team_offensive_profile(team_id: int) -> dict:
    """
    Enhanced team hitting stats: avg, obp, slg, ops, ISO, BB%, K%, runs/game,
    wRC+ approximation.  One API call, cached daily.
    """
    season = SEASON
    cache_key = f"team_off_profile_{team_id}_{season}"
    data = _get(f"/teams/{team_id}/stats", {
        "stats": "season", "group": "hitting",
        "season": season, "sportId": 1,
    }, cache_key=cache_key)
    if not data:
        return {}
    splits = ((data.get("stats") or [{}])[0]).get("splits") or []
    if not splits:
        return {}
    s = splits[0].get("stat", {})
    try:
        pa  = max(int(s.get("plateAppearances", 1) or 1), 1)
        ab  = max(int(s.get("atBats", 1) or 1), 1)
        hits = int(s.get("hits", 0) or 0)
        doubles = int(s.get("doubles", 0) or 0)
        triples = int(s.get("triples", 0) or 0)
        hr  = int(s.get("homeRuns", 0) or 0)
        bb  = int(s.get("baseOnBalls", 0) or 0)
        k   = int(s.get("strikeOuts", 0) or 0)
        sb  = int(s.get("stolenBases", 0) or 0)
        runs = int(s.get("runs", 0) or 0)
        gp  = max(int(s.get("gamesPlayed", 1) or 1), 1)
        iso = round((doubles + 2 * triples + 3 * hr) / ab, 3) if ab else 0
        bb_pct = round(bb / pa * 100, 1) if pa else 0
        k_pct  = round(k / pa * 100, 1) if pa else 0
        ops_f  = float(s.get("ops", 0) or 0)
        wrc_plus = round(ops_f / 0.728 * 100) if ops_f else 100
    except (ValueError, TypeError):
        iso = 0.0; bb_pct = 0.0; k_pct = 0.0; wrc_plus = 100
        hits = doubles = triples = hr = bb = k = sb = runs = gp = 0; pa = 1
    return {
        "avg":       s.get("avg", ".000"),
        "obp":       s.get("obp", ".000"),
        "slg":       s.get("slg", ".000"),
        "ops":       s.get("ops", ".000"),
        "iso":       iso,
        "bb_pct":    bb_pct,
        "k_pct":     k_pct,
        "runs_pg":   round(runs / gp, 2) if gp else 0,
        "wrc_plus":  wrc_plus,
        # This is an OPS-derived proxy, not a park-adjusted wRC+ statistic.
        "ops_plus_proxy": wrc_plus,
        "hr":        hr,
        "hits":      hits,
        "doubles":   doubles,
        "triples":   triples,
        "sb":        sb,
        "games":     gp,
    }


# ── 15c. Pitcher home/away venue splits ──────────────────────────────────────

def get_pitcher_venue_splits(pitcher_id: int) -> dict:
    """
    Pitcher home vs away ERA and FIP splits for the current season.
    Returns {home_era, away_era, home_fip, away_fip, home_ip, away_ip} or {}.
    """
    season = SEASON
    result = {}
    for sit_code, key in [("h", "home"), ("a", "away")]:
        data = _get(f"/people/{pitcher_id}/stats", {
            "stats": "statSplits", "group": "pitching",
            "season": season, "sportId": 1, "sitCodes": sit_code,
        }, cache_key=f"pit_venue_{pitcher_id}_{sit_code}_{season}")
        splits = ((data or {}).get("stats") or [{}])[0].get("splits", [])
        if not splits:
            continue
        s = splits[0].get("stat", {})
        try:
            ip_raw = s.get("inningsPitched", "0")
            parts = str(ip_raw).split(".")
            ip = float(parts[0]) + float(parts[1]) / 3 if len(parts) > 1 and parts[1] else float(parts[0])
        except (ValueError, IndexError):
            ip = 0.0
        if ip < 5:
            continue
        try:
            era = float(s.get("era", 0) or 0)
        except (ValueError, TypeError):
            era = None
        try:
            hr = int(s.get("homeRuns", 0) or 0)
            bb = int(s.get("baseOnBalls", 0) or 0)
            k  = int(s.get("strikeOuts", 0) or 0)
            fip = round((13 * hr + 3 * bb - 2 * k) / max(ip, 1) + 3.10, 2) if ip > 0 else None
        except (ValueError, TypeError):
            fip = None
        result[f"{key}_era"] = era
        result[f"{key}_fip"] = fip
        result[f"{key}_ip"]  = round(ip, 1)
    return result


# ── 15d. Team-vs-team season series record ───────────────────────────────────

def get_team_h2h_record(team_a_id: int, team_b_id: int) -> dict:
    """
    Season series record between two teams (completed games only).
    Returns {team_a_wins, team_b_wins, games_played} or {} if no games found.
    """
    season = SEASON
    start = f"{season}-03-01"
    end   = f"{season}-11-30"
    data = _get("/schedule", {
        "sportId": 1, "teamId": team_a_id,
        "startDate": start, "endDate": end,
        "gameType": "R",
    }, cache_key=f"h2h_sched_{team_a_id}_{season}")
    if not data:
        return {}
    a_wins = b_wins = 0
    for date_entry in data.get("dates", []):
        for game in date_entry.get("games", []):
            if (game.get("status") or {}).get("abstractGameState") != "Final":
                continue
            teams = game.get("teams", {})
            away = teams.get("away", {})
            home = teams.get("home", {})
            away_id = (away.get("team") or {}).get("id")
            home_id = (home.get("team") or {}).get("id")
            if away_id == team_b_id or home_id == team_b_id:
                # Determine if team A won
                if home_id == team_a_id:
                    if home.get("isWinner"):
                        a_wins += 1
                    else:
                        b_wins += 1
                elif away_id == team_a_id:
                    if away.get("isWinner"):
                        a_wins += 1
                    else:
                        b_wins += 1
    total = a_wins + b_wins
    if total == 0:
        return {}
    return {
        "team_a_wins":  a_wins,
        "team_b_wins":  b_wins,
        "games_played": total,
    }


# ── 16. Home plate umpire ─────────────────────────────────────────────────────

def get_game_umpire(home_team_id: int) -> dict:
    """
    Return today's home plate umpire for the game with home_team_id.
    Tries umpscorecards.com for K-rate tendency; falls back to name-only.
    Returns {name, k_boost} — k_boost is +/- vs league avg (float or None).
    """
    if not home_team_id:
        return {}

    from datetime import date as _date
    today = _date.today().isoformat()

    sched = _get("/schedule", {
        "sportId": 1, "date": today,
        "hydrate": "officials", "gameType": "R",
    }, cache_key=f"officials_{today}")

    ump_name = ""
    for date_entry in (sched or {}).get("dates", []):
        for game in date_entry.get("games", []):
            teams   = game.get("teams", {})
            home_id = (teams.get("home", {}).get("team") or {}).get("id")
            if home_id != home_team_id:
                continue
            for official in game.get("officials", []):
                if official.get("officialType") == "Home Plate":
                    ump_name = (official.get("official") or {}).get("fullName", "")
                    break
            if ump_name:
                break
        if ump_name:
            break

    if not ump_name:
        return {}

    try:
        r = requests.get(
            "https://umpscorecards.com/api/umpires",
            timeout=8,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if r.ok:
            for ump in r.json():
                name = (ump.get("name") or ump.get("umpire_name") or "").lower()
                if ump_name.lower() in name or name in ump_name.lower():
                    k_boost = (ump.get("k_pct_delta") or ump.get("strikeout_rate_delta")
                               or ump.get("k_boost") or ump.get("k_delta"))
                    return {"name": ump_name,
                            "k_boost": float(k_boost) if k_boost is not None else None}
    except Exception:
        pass

    return {"name": ump_name, "k_boost": None}


# ── 16. Batter handedness ─────────────────────────────────────────────────────

def get_player_bat_side(player_id: int) -> str:
    """Return batter's handedness: 'L', 'R', or 'S' (switch). Empty string on failure."""
    try:
        p = _get_player_profile(player_id)
        return (p.get("batSide") or {}).get("code", "")
    except Exception:
        return ""


# ── Pretty print for dry run ─────────────────────────────────────────────────

def _print_card(card: dict):
    divider = "━" * 55

    if "error" in card:
        print(f"\n  ERROR: {card['error']}")
        return

    s   = card["splits"]
    p   = card["pitcher"]
    bvp = card["bvp"]

    print(f"\n{divider}")
    print(f"  VORTEX CARD  |  {card['batter_name'].upper()}")
    print(f"  Prop : O{card['line']} {card['prop_label']}")
    print(f"  Tier : {card['tier']}")
    print(divider)

    # Splits
    def _rate_str(r):
        if not r: return "n/a"
        icon = "🔥" if r["rate"] >= 70 else "✅" if r["rate"] >= 50 else "❌"
        return f"{icon} {r['rate']}% ({r['hits']}/{r['games']})  avg {r['avg']}"

    print(f"\n  SPLITS  (season avg: {s.get('season_avg')}  |  {s.get('games_played')} G)")
    print(f"    L5  : {_rate_str(s.get('l5'))}")
    print(f"    L10 : {_rate_str(s.get('l10'))}")
    print(f"    L20 : {_rate_str(s.get('l20'))}")
    print(f"  Trend : {card['trend_signal']}")

    # Recent games
    print(f"\n  RECENT GAMES")
    for g in s.get("recent_games", []):
        icon = "✅" if g["over"] else "❌"
        print(f"    {icon}  {g['date']}  vs {g['opponent']:25}  {card['prop_label']}: {g['value']}")

    # Pitcher
    print(f"\n  PITCHER  :  {p.get('name')}  ({p.get('hand')}HP)")
    print(f"    ERA {p.get('era')}  |  FIP {p.get('fip')}  |  WHIP {p.get('whip')}")
    print(f"    K/9 {p.get('k_per_9')}  |  BB/9 {p.get('bb_per_9')}  |  HR/9 {p.get('hr_per_9')}")
    print(f"    AVG against: {p.get('avg_against')}  |  {p.get('innings_pitched')} IP  ({p.get('games_started')} GS)")
    print(f"\n  RECENT STARTS")
    for st in p.get("last_5_starts", []):
        print(f"    {st['date']}  vs {st['opponent']:20}  {st['ip']} IP  {st['k']}K  {st['er']}ER")

    # BvP
    print(f"\n  BvP HISTORY  ({bvp.get('sample', 'n/a')})")
    if bvp.get("ab", 0) > 0:
        print(f"    {bvp['ab']} AB  |  {bvp['hits']} H  |  AVG {bvp['avg']}  |  {bvp['hr']} HR  |  {bvp['k']} K")
        print(f"    TB: {bvp['tb']}  |  OPS: {bvp['ops']}")
    else:
        print("    No head-to-head history on record.")

    # Platoon
    print(f"\n  PLATOON  : {card['platoon_note']}")
    print(f"\n{divider}\n")


# ── Dry run ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 55)
    print("  Vortex MLB Stats Engine — Dry Run")
    print(f"  Season: {SEASON}")
    print("=" * 55)

    test_cases = [
        # (batter,             pitcher,       line,  prop_type)
        ("Christian Yelich",  "Aaron Nola",   1.5,   "total_bases"),
        ("Freddie Freeman",   "Aaron Nola",   1.5,   "total_bases"),
        ("Shohei Ohtani",     "Aaron Nola",   0.5,   "home_runs"),
    ]

    for batter, pitcher, line, prop in test_cases:
        card = get_full_card(batter, pitcher, line, prop)
        _print_card(card)
        time.sleep(0.5)

    print("  Dry run complete.")
