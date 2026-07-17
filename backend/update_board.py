"""
Vortex Data Engine  v5
=======================
Filter pipeline  →  Multi-book filter (both sides)  →  Stats tier gate  →  DB

Both the Over AND Under sides of every prop market are evaluated independently.
A prop reaches the board via one of two paths:

  EV_EDGE     — best available odds beat consensus no-vig by ≥ MIN_EV_PCT
  STRONG_PLAY — stats tier is ELITE/STRONG with EV ≥ EV_BYPASS_FLOOR
  HOT_STREAK  — L10 hit rate ≥ MIN_L10_BYPASS with EV ≥ EV_BYPASS_FLOOR

signal_type is stored in stats_json so the Discord frontend can bucket cards
into the correct menu sections (💎 Strong Plays, 🔥 Hot Streaks, etc.).

Usage
-----
  backend\\.venv\\Scripts\\python backend/update_board.py
"""

import io
import json
import math
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/html, application/xhtml+xml, */*",
    "Origin": "https://www.mlb.com",
    "Referer": "https://www.mlb.com/"
})

# Separate session for Odds API — no MLB.com headers
ODDS_SESSION = requests.Session()
ODDS_SESSION.headers.update({
    "User-Agent": "VORTEX/1.0",
    "Accept": "application/json",
})

# Globally override requests.get to use our authenticated session
requests.get = SESSION.get
requests.post = SESSION.post

from dotenv import load_dotenv

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ── Stats engine imports ──────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
import stats_mlb
import stats_nba
import stats_wnba
import grade_wnba
import stats_mlb_enrichment as mlb_enrich
import analyze as vortex_analyze

# ── Config ─────────────────────────────────────────────────────────────────

load_dotenv(Path(__file__).parent.parent / ".env")

API_KEY           = os.getenv("ODDS_API_KEY", "")  # .env fallback, baked in at process start
BASE_URL          = "https://api.the-odds-api.com/v4"
LIVE_ODDS_KEY_KV  = "vortex:live_odds_api_key"  # set via Discord's /setoddskey — no bot restart needed
CACHE_DIR         = Path(__file__).parent / "cache"
DB_PATH           = Path(__file__).parent.parent / "vortex.db"
CACHE_TTL_MINUTES = 60
MAX_EVENTS        = 30
# Only consider games within this many days. Sparse schedules (esp. WNBA) return
# events a week+ out among the next MAX_EVENTS; without a window those leak onto
# tonight's board as "false plays" for games that aren't today/tomorrow.
MAX_DAYS_AHEAD    = 2

MIN_BOOKS       = 1      # DFS platforms (Underdog/PP) often single-book — allow 1
MIN_EV_PCT      = -10.0  # loose EV fallback — hit rate is the primary gate now
EV_BYPASS_FLOOR = -15.0  # floor for any prop to qualify
MIN_L10_BYPASS  = 60     # L10 hit-rate % threshold for HOT_STREAK
MAX_JUICE       = -200
MAX_BOARD       = 40     # fits ~35 batter props + 5 pitcher Ks
MAX_PITCHER_K   = 5      # reserve up to this many board slots for pitcher Ks
MAX_WNBA        = 15     # reserve up to this many board slots for WNBA props

# Hard hitrate gate: once a stat_type has enough graded history and is proven a
# net loser, drop it from the board entirely (the soft learned-weight multiplier
# only scales the score, it can't fully exclude a high-raw-score losing signal).
# Self-activating: does nothing until BLOCK_MIN_SAMPLE graded results exist.
BLOCK_MIN_SAMPLE = 25     # need this many graded picks before trusting a signal's hitrate
BLOCK_HITRATE    = 0.50   # below this proven hitrate → exclude the stat_type

# Minimum meaningful line per prop type.
# Under 0.5 anything is nearly trivial; HRR/hits/TB Unders need a real line.
# Lines below these thresholds are dropped before stats evaluation.
MIN_LINE = {
    "hits_runs_rbis": {"over": 1.5, "under": 1.5},   # U1.5 HRR is real; U0.5 is noise
    "hits":           {"over": 0.5, "under": 1.5},   # U0.5 hits is trivially easy
    "total_bases":    {"over": 1.5, "under": 1.5},   # U0.5 / U1.0 TB is trivial
    "runs_scored":    {"over": 0.5, "under": 0.5},   # keep but block sub-0.5
    "fantasy_score":  {"over": 5.5, "under": 5.5},
    "home_runs":      {"over": 0.5, "under": 0.5},
    "rbis":           {"over": 0.5, "under": 0.5},
    "strikeouts":     {"over": 0.5, "under": 0.5},
    "walks":                    {"over": 0.5, "under": 0.5},
    "pitcher_outs":             {"over": 0.5, "under": 0.5},
    "pitcher_hits_allowed":     {"over": 2.5, "under": 2.5},
    "pitcher_earned_runs":      {"over": 0.5, "under": 0.5},
}
# Prop types completely excluded from the board until model is improved.
# Historical accuracy data is preserved — only new picks are blocked.
SKIP_PROPS = {"home_runs", "rbis"}  # RBIs accuracy 37.5% — disabled until model improves

MAX_PICKS_PER_PLAYER = 2   # max board slots per player per day (prevents correlated losses)

# Fantasy Score requires Elite tier (score ≥ 12) due to low model accuracy (42.9%)
FANTASY_SCORE_MIN_SCORE = 12

NBA_ENABLED     = False  # stats.nba.com is blocked on server — disable until proxied
WNBA_ENABLED    = False  # disabled to save Odds API credits

# Books shown as "best price" source — checked in priority order
PREFERRED_BOOKS = ["underdogfantasy", "underdog", "draftkings", "prizepicks"]

# Tier inversion for Under rows — Over ELITE means the stat goes over a lot,
# so Under is poor. But Over LEAN/PASS means the stat is inconsistent, which
# is actually useful for Under. Keep one step of degradation max so Under plays
# aren't buried by inversion alone.
# Under ELITE is earned separately in _should_include via ≥80% effective L10.
TIER_INVERT = {"ELITE": "LEAN", "STRONG": "STRONG", "LEAN": "STRONG", "PASS": "STRONG"}

# Sharp reference book. Pinnacle is NOT a book we bet on — it is the most
# accurate "true price" available, used only to de-vig and judge whether our
# bettable books (DK/PP/FD/UD) are mispriced in our favor. This is the real
# hit-rate anchor: Pinnacle's no-vig line is the best estimate of how often a
# prop actually hits. See _sharp_no_vig_prob.
SHARP_BOOK = "pinnacle"

TARGET_BOOKS = {
    "draftkings", "underdogfantasy", "underdog", "prizepicks",
    # Secondary books kept for consensus no-vig math (more books = better true prob)
    "fanduel", "betmgm", "espnbet", "betrivers",
    # Sharp reference only (never bet) — anchors the true-probability estimate.
    SHARP_BOOK,
}

SPORT_CONFIG = {
    "NBA": {
        "key":     "basketball_nba",
        "markets": [
            "player_points", "player_rebounds", "player_assists",
            "player_points_rebounds_assists", "player_threes",
            "player_blocks", "player_steals",
        ],
    },
    "WNBA": {
        "key":     "basketball_wnba",
        "markets": [
            "player_points", "player_rebounds", "player_assists",
            "player_points_rebounds_assists",
            "player_points_rebounds", "player_points_assists", "player_rebounds_assists",
            "player_threes",
        ],
    },
    "MLB": {
        "key":     "baseball_mlb",
        "markets": [
            "batter_hits_runs_rbis",   # HRR combo — primary DK/Underdog/PP market
            "batter_total_bases",
            "batter_hits",
            "batter_fantasy_score",    # PrizePicks fantasy scoring
            "pitcher_strikeouts",
            "pitcher_outs",
            "pitcher_hits_allowed",
            "pitcher_earned_runs",
        ],
    },
}

MARKET_LABELS = {
    "player_points":                    "Points",
    "player_rebounds":                  "Rebounds",
    "player_assists":                   "Assists",
    "player_points_rebounds_assists":   "Pts + Reb + Ast",
    "player_points_rebounds":           "Pts + Reb",
    "player_points_assists":            "Pts + Ast",
    "player_rebounds_assists":          "Reb + Ast",
    "player_threes":                    "3-Pointers Made",
    "player_blocks":                    "Blocks",
    "player_steals":                    "Steals",
    "batter_hits_runs_rbis":            "Hits+Runs+RBIs",
    "batter_total_bases":               "Total Bases",
    "batter_hits":                      "Hits",
    "batter_fantasy_score":             "Fantasy Score (PP)",
    "pitcher_strikeouts":               "Strikeouts",
    "batter_home_runs":                 "Home Runs",
    "batter_rbis":                      "RBIs",
    "batter_runs_scored":               "Runs Scored",
    "pitcher_outs":                     "Outs",
    "pitcher_hits_allowed":             "Hits Allowed",
    "pitcher_earned_runs":              "Earned Runs",
}

# Maps The Odds API market key → stats_mlb prop_type
MARKET_TO_PROP_TYPE = {
    "batter_hits_runs_rbis": "hits_runs_rbis",
    "batter_total_bases":    "total_bases",
    "batter_hits":           "hits",
    "batter_fantasy_score":  "fantasy_score",
    "batter_home_runs":      "home_runs",
    "batter_rbis":           "rbis",
    "batter_runs_scored":    "runs_scored",
    "pitcher_strikeouts":    "strikeouts",
    "pitcher_outs":          "pitcher_outs",
    "pitcher_hits_allowed":  "pitcher_hits_allowed",
    "pitcher_earned_runs":   "pitcher_earned_runs",
}

# Maps The Odds API market key → stats_nba prop_type
NBA_MARKET_TO_PROP_TYPE = {
    "player_points":                  "points",
    "player_rebounds":                "rebounds",
    "player_assists":                 "assists",
    "player_points_rebounds_assists": "pts_reb_ast",
    "player_threes":                  "threes",
    "player_blocks":                  "blocks",
    "player_steals":                  "steals",
}

BOOK_DISPLAY = {
    "draftkings":      "DraftKings",
    "prizepicks":      "PrizePicks",
    "fanduel":         "FanDuel",
    "betmgm":          "BetMGM",
    "caesars":         "Caesars",
    "pointsbet":       "PointsBet",
    "bet365":          "Bet365",
    "bovada":          "Bovada",
    "mybookieag":      "MyBookie",
    "betrivers":       "BetRivers",
    "williamhill_us":  "WilliamHill",
    "espnbet":         "ESPNBet",
    "underdog":        "Underdog",
    "underdogfantasy": "Underdog",
    "ballybet":        "BallyBet",
    "hardrockbet":     "HardRock",
    "unibet_us":       "Unibet",
    "fliff":           "Fliff",
}

# ── Cache ───────────────────────────────────────────────────────────────────

CACHE_DIR.mkdir(exist_ok=True)

def _cache_path(sport_key: str, market: str) -> Path:
    return CACHE_DIR / f"{sport_key}__{market}.json"

def _is_fresh(path: Path) -> bool:
    if not path.exists():
        return False
    age_minutes = (time.time() - path.stat().st_mtime) / 60
    # After 10 PM local time, overnight lines are rolling in — use a 15-min TTL
    # so the engine picks up next-day props without a manual cache clear.
    ttl = 15 if datetime.now().hour >= 22 else CACHE_TTL_MINUTES
    return age_minutes < ttl

def _load_cache(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def _save_cache(path: Path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)

# ── Odds API ─────────────────────────────────────────────────────────────────

def _filter_live(events: list) -> list:
    """Keep only events that are upcoming AND within MAX_DAYS_AHEAD.
    Drops both already-started games and far-future games (the WNBA week-out leak)."""
    now    = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=MAX_DAYS_AHEAD)
    out = []
    for ev in events:
        ct = ev.get("commence_time", "")
        if ct:
            try:
                gs = datetime.fromisoformat(ct.replace("Z", "+00:00"))
                if gs <= now or gs > cutoff:
                    continue
            except Exception:
                pass
        out.append(ev)
    return out


def fetch_props(sport_key: str, market: str) -> list:
    path = _cache_path(sport_key, market)
    if _is_fresh(path):
        print(f"  [cache] {sport_key} / {market}")
        return _filter_live(_load_cache(path))
    if not API_KEY:
        print(f"  [warn]  ODDS_API_KEY not set — using cached data")
        return _filter_live(_load_cache(path)) if path.exists() else []
    try:
        resp = ODDS_SESSION.get(f"{BASE_URL}/sports/{sport_key}/events",
                            params={"apiKey": API_KEY, "dateFormat": "iso"}, timeout=15)
        resp.raise_for_status()
        events = resp.json()
    except requests.RequestException as exc:
        print(f"  [error] {exc}")
        return _load_cache(path) if path.exists() else []
    if not events:
        print(f"  [info]  No events today for {sport_key}")
        _save_cache(path, [])
        return []
    now_utc = datetime.now(timezone.utc)
    results = []
    had_auth_error = False
    for event in events[:MAX_EVENTS]:
        # Skip games that have already started — live props reflect mid-game stats
        ct = event.get("commence_time", "")
        if ct:
            try:
                game_start = datetime.fromisoformat(ct.replace("Z", "+00:00"))
                if game_start <= now_utc:
                    print(f"  [skip]  {event.get('home_team','?')} vs {event.get('away_team','?')} — game already started")
                    continue
                if game_start > now_utc + timedelta(days=MAX_DAYS_AHEAD):
                    print(f"  [skip]  {event.get('home_team','?')} vs {event.get('away_team','?')} — {MAX_DAYS_AHEAD}+ days out")
                    continue
            except Exception:
                pass
        try:
            r = ODDS_SESSION.get(
                f"{BASE_URL}/sports/{sport_key}/events/{event['id']}/odds",
                params={"apiKey": API_KEY, "regions": "us", "markets": market,
                        "oddsFormat": "american", "bookmakers": ",".join(TARGET_BOOKS),
                        "includeLinks": "true", "includeSids": "true"},
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()
            if data.get("bookmakers"):
                results.append(data)
            print(f"  [api]   {event['id'][:8]}... — {r.headers.get('x-requests-remaining','?')} left")
        except requests.RequestException as exc:
            print(f"  [error] {exc}")
            if "401" in str(exc) or "Unauthorized" in str(exc):
                had_auth_error = True
                break  # All calls will fail — stop burning quota
    if had_auth_error:
        print(f"  [warn]  Auth failure — keeping existing cache, not overwriting with empty data")
        return _load_cache(path) if path.exists() else []
    _save_cache(path, results)
    return results

def fetch_all_markets_batched(sport_key: str, markets: list) -> list:
    """Fetch all markets for a sport in one odds call per event (~16 calls vs ~112).

    Returns a list of event objects that each contain bookmaker data for ALL requested
    markets.  parse_events() already filters by market key so no downstream changes needed.
    """
    path = CACHE_DIR / f"{sport_key}__ALL.json"
    if _is_fresh(path):
        print(f"  [cache] {sport_key} / ALL markets")
        return _filter_live(_load_cache(path))
    if not API_KEY:
        print(f"  [warn]  ODDS_API_KEY not set — using cached data")
        return _filter_live(_load_cache(path)) if path.exists() else []
    try:
        resp = ODDS_SESSION.get(f"{BASE_URL}/sports/{sport_key}/events",
                            params={"apiKey": API_KEY, "dateFormat": "iso"}, timeout=15)
        resp.raise_for_status()
        events = resp.json()
    except requests.RequestException as exc:
        print(f"  [error] Events fetch: {exc}")
        return _load_cache(path) if path.exists() else []
    if not events:
        print(f"  [info]  No events today for {sport_key}")
        _save_cache(path, [])
        return []
    markets_param = ",".join(markets)
    now_utc = datetime.now(timezone.utc)
    results = []
    had_auth_error = False
    for event in events[:MAX_EVENTS]:
        ct = event.get("commence_time", "")
        if ct:
            try:
                game_start = datetime.fromisoformat(ct.replace("Z", "+00:00"))
                if game_start <= now_utc:
                    print(f"  [skip]  {event.get('home_team','?')} vs {event.get('away_team','?')} — game already started")
                    continue
                if game_start > now_utc + timedelta(days=MAX_DAYS_AHEAD):
                    print(f"  [skip]  {event.get('home_team','?')} vs {event.get('away_team','?')} — {MAX_DAYS_AHEAD}+ days out")
                    continue
            except Exception:
                pass
        try:
            r = ODDS_SESSION.get(
                f"{BASE_URL}/sports/{sport_key}/events/{event['id']}/odds",
                params={"apiKey": API_KEY, "regions": "us", "markets": markets_param,
                        "oddsFormat": "american", "bookmakers": ",".join(TARGET_BOOKS),
                        "includeLinks": "true", "includeSids": "true"},
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()
            if data.get("bookmakers"):
                results.append(data)
            print(f"  [api]   {event['id'][:8]}... — {r.headers.get('x-requests-remaining','?')} left")
        except requests.RequestException as exc:
            print(f"  [error] {exc}")
            if "401" in str(exc) or "Unauthorized" in str(exc):
                had_auth_error = True
                break
    if had_auth_error:
        print(f"  [warn]  Auth failure — keeping existing cache")
        return _load_cache(path) if path.exists() else []
    _save_cache(path, results)
    return results


# ── EV Math ─────────────────────────────────────────────────────────────────

def american_to_decimal(o: int) -> float:
    return (o / 100) + 1 if o > 0 else (100 / abs(o)) + 1

def american_to_implied(o: int) -> float:
    return 100 / (o + 100) if o > 0 else abs(o) / (abs(o) + 100)

def consensus_no_vig_prob(over_map: dict, under_map: dict):
    probs = []
    for b in over_map:
        if b in under_map:
            po = american_to_implied(over_map[b])
            pu = american_to_implied(under_map[b])
            probs.append(po / (po + pu))
    return sum(probs) / len(probs) if probs else None


def _sharp_no_vig_prob(over_map: dict, under_map: dict):
    """
    True-probability anchor from the SHARP_BOOK (Pinnacle) ONLY.

    Pinnacle's two-sided price, de-vigged, is the single most accurate estimate
    of how often a prop hits — it moves on real money, not public bias. When
    Pinnacle has both sides of this exact line, that de-vigged number is our
    true_prob. Returns None if Pinnacle doesn't offer both sides (then we fall
    back to the soft-book consensus, which is weaker but better than nothing).
    """
    if SHARP_BOOK in over_map and SHARP_BOOK in under_map:
        po = american_to_implied(over_map[SHARP_BOOK])
        pu = american_to_implied(under_map[SHARP_BOOK])
        if (po + pu) > 0:
            return po / (po + pu)
    return None

def compute_ev(true_prob: float, best_odds: int) -> float:
    return round(((true_prob * american_to_decimal(best_odds)) - 1) * 100, 2)

def fmt_odds(o: int) -> str:
    return f"+{o}" if o > 0 else str(o)

def compute_score(ev: float, n_books: int, line: float, best_odds: int,
                 tier: str = None, signal_type: str = None,
                 # Extended factors — all optional, passed through from enrichment
                 eff_l10: float = None,
                 eff_l5:  float = None,
                 eff_l20: float = None,
                 lineup_pos:     int   = None,
                 team_ops:       str   = None,
                 team_runs_pg:   float = None,
                 bullpen_era:    float = None,
                 park_factor:    float = None,
                 weather_boost:  int   = None,
                 barrel_pct:     float = None,
                 hard_hit_pct:   float = None,
                 umpire_tier:    str   = None,
                 pitcher_era:    float = None,
                 pitcher_fip:    float = None,
                 pitcher_hr9:    float = None,
                 side:           str   = "over",
                 ) -> int:
    """
    12-factor VORTEX EDGE SCORE (0-100).

    Weights (per spec):
      Market Edge       25 pts
      Pitcher Matchup   15 pts
      Batter Splits     12 pts
      Team Environment  10 pts
      Lineup Position   10 pts
      Recent Form        8 pts
      Ballpark           6 pts
      Weather            5 pts
      Bullpen            4 pts
      Hard Contact       3 pts
      Umpire             2 pts
      ─────────────────────────
      Total            100 pts
    """
    is_under = side == "under"
    score    = 0.0

    # ── 1. Market Edge (25 pts) ───────────────────────────────────────────────
    # EV quality (20 pts) + book consensus (5 pts)
    ev_pts   = min(20, max(0, (ev - MIN_EV_PCT) / (25 - MIN_EV_PCT) * 20))
    book_pts = min(5, (n_books - 1) * 2.5)
    if n_books <= 1:
        # Single-book (DFS/PrizePicks-style) lines have no opposing price to
        # de-vig against, so their "EV" is not a real market edge — it's noise.
        # Give it zero score credit; differentiation must come from the stats
        # factors below, not a fabricated edge number. (This is what let the
        # +41%-EV / 17%-hitrate Hits+Runs+RBIs trap onto the board.)
        ev_pts   = 0
        book_pts = 0
    score += ev_pts + book_pts

    # ── 2. Pitcher Matchup (15 pts) ───────────────────────────────────────────
    if pitcher_era is not None:
        era_use = pitcher_fip if pitcher_fip and pitcher_fip < pitcher_era else pitcher_era
        if is_under:
            # Lower ERA = better for Under
            if era_use <= 3.00:   score += 15
            elif era_use <= 3.75: score += 11
            elif era_use <= 4.50: score += 7
            elif era_use <= 5.50: score += 3
            else:                 score += 0
        else:
            # Higher ERA = better for Over
            if era_use >= 5.50:   score += 15
            elif era_use >= 4.50: score += 11
            elif era_use >= 3.75: score += 7
            elif era_use >= 3.00: score += 3
            else:                 score += 0
    else:
        score += 7   # neutral when no pitcher data

    # ── 3. Batter Splits (12 pts) ─────────────────────────────────────────────
    # L20 hit rate is the most stable split — use as primary
    if eff_l20 is not None:
        score += min(12, eff_l20 / 100 * 12)
    elif eff_l10 is not None:
        score += min(12, eff_l10 / 100 * 10)   # slight discount vs L20

    # ── 4. Team Environment (10 pts) ─────────────────────────────────────────
    if team_ops:
        try:
            ops_f = float(str(team_ops).lstrip("."))
            if "." not in str(team_ops):
                ops_f = float("0." + str(team_ops).replace(".", ""))
            ops_f = float(team_ops)
        except (ValueError, TypeError):
            ops_f = 0.728
        # wRC+ proxy: OPS relative to league avg (0.728)
        wrc_proxy = ops_f / 0.728
        if is_under:
            env_pts = min(10, max(0, (1.10 - wrc_proxy) * 20))
        else:
            env_pts = min(10, max(0, (wrc_proxy - 0.90) * 20))
        score += env_pts
    elif team_runs_pg is not None:
        if is_under:
            score += min(10, max(0, (4.5 - team_runs_pg) * 2.5))
        else:
            score += min(10, max(0, (team_runs_pg - 4.0) * 2.5))
    else:
        score += 5   # neutral

    # ── 5. Lineup Position (10 pts) ──────────────────────────────────────────
    if lineup_pos is not None:
        if   lineup_pos <= 2: score += 10
        elif lineup_pos <= 4: score += 8
        elif lineup_pos <= 6: score += 5
        elif lineup_pos == 7: score += 3
        else:                 score += 1   # 8th/9th
    else:
        score += 5   # unknown — neutral

    # ── 6. Recent Form (8 pts) ───────────────────────────────────────────────
    # Weighted: Season 40%, L20 30%, L10 20%, L5 10% (per spec)
    # We use effective rates since splits are already side-adjusted
    if eff_l10 is not None:
        form_pts = 0
        if eff_l20 is not None: form_pts += (eff_l20 / 100) * 8 * 0.30
        if eff_l10 is not None: form_pts += (eff_l10 / 100) * 8 * 0.40
        if eff_l5  is not None: form_pts += (eff_l5  / 100) * 8 * 0.30
        score += min(8, form_pts)
    else:
        score += 4

    # ── 7. Ballpark (6 pts) ──────────────────────────────────────────────────
    if park_factor is not None:
        if is_under:
            score += min(6, max(0, (1.05 - park_factor) * 30))
        else:
            score += min(6, max(0, (park_factor - 0.95) * 30))
    else:
        score += 3

    # ── 8. Weather (5 pts) ───────────────────────────────────────────────────
    if weather_boost is not None:
        if is_under:
            score += {-1: 5, 0: 2.5, 1: 0}.get(weather_boost, 2.5)
        else:
            score += {1: 5, 0: 2.5, -1: 0}.get(weather_boost, 2.5)
    else:
        score += 2.5

    # ── 9. Bullpen (4 pts) ───────────────────────────────────────────────────
    if bullpen_era is not None:
        if is_under:
            score += min(4, max(0, (4.5 - bullpen_era) * 1.5))
        else:
            score += min(4, max(0, (bullpen_era - 3.5) * 1.5))
    else:
        score += 2

    # ── 10. Hard Contact Metrics (3 pts) ─────────────────────────────────────
    if barrel_pct is not None and hard_hit_pct is not None:
        contact_score = (barrel_pct / 15 * 1.5) + (hard_hit_pct / 50 * 1.5)
        if is_under:
            score += min(3, max(0, 3 - contact_score))
        else:
            score += min(3, contact_score)
    else:
        score += 1.5

    # ── 11. Umpire (2 pts) ───────────────────────────────────────────────────
    if umpire_tier:
        if umpire_tier == "HIGH":
            score += 2 if not is_under else 0
        elif umpire_tier == "LOW":
            score += 2 if is_under else 0
        else:
            score += 1
    else:
        score += 1

    # ── Single-book penalty ───────────────────────────────────────────────────
    if n_books <= 1:
        score -= 20

    return max(0, min(100, round(score)))


def _load_learned_weights() -> dict[str, float]:
    """
    Load learned dimension weights from the score_weights table.
    Returns a dict keyed by weight_key (e.g. 'stat_type_hits', 'side_over').
    Weights represent hit_rate / 0.50 — 1.0 = baseline, >1.0 = better than baseline.
    """
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT weight_key, weight, sample_size FROM score_weights WHERE sample_size >= 20"
        ).fetchall()
        conn.close()
        return {r["weight_key"]: r["weight"] for r in rows}
    except Exception as e:
        print(f"    [warn] failed to load learned weights: {e}")
        return {}


def _load_blocked_signals() -> set[tuple[str, str]]:
    """
    Return a set of (sport, stat_type) pairs that have enough graded history to
    trust AND a proven hitrate below break-even — these are dropped from the
    board entirely. Reads signal_accuracy (populated by grade_results).

    Conservative by design: requires BLOCK_MIN_SAMPLE graded picks before a
    signal can be blocked, so it self-activates only once the data justifies it.
    """
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT sport, value, total, hit_rate FROM signal_accuracy
            WHERE dimension='stat_type' AND total >= ? AND hit_rate < ?
            """,
            (BLOCK_MIN_SAMPLE, BLOCK_HITRATE),
        ).fetchall()
        conn.close()
        blocked = {(r["sport"], r["value"]) for r in rows}
        for r in rows:
            print(f"    [hitrate-gate] BLOCK {r['sport']} / {r['value']} "
                  f"— {r['hit_rate']*100:.1f}% over {r['total']} graded")
        return blocked
    except Exception as e:
        print(f"    [warn] failed to load blocked signals: {e}")
        return set()


def _apply_learned_weight(score: float, prop_type: str, side: str,
                          weights: dict[str, float]) -> float:
    """
    Apply learned multiplicative weight modifiers to a grade_pick score.
    Looks up stat_type and side keys, computes composite multiplier, and
    scales the score. Clamped to 0.6–1.4 to prevent extreme swings.
    """
    if not weights:
        return score

    multipliers = []
    st_key = f"stat_type_{prop_type}"
    if st_key in weights:
        multipliers.append(weights[st_key])
    sd_key = f"side_{side}"
    if sd_key in weights:
        multipliers.append(weights[sd_key])

    if not multipliers:
        return score

    composite = sum(multipliers) / len(multipliers)
    composite = max(0.6, min(1.4, composite))
    return round(score * composite)


def _compute_learned_multiplier(prop_type: str, side: str,
                                weights: dict[str, float]) -> float | None:
    """
    Compute the learned weight multiplier for a given prop_type/side.
    Returns the composite multiplier (clamped 0.6–1.4), or None if no weights apply.
    """
    if not weights:
        return None

    multipliers = []
    st_key = f"stat_type_{prop_type}"
    if st_key in weights:
        multipliers.append(weights[st_key])
    sd_key = f"side_{side}"
    if sd_key in weights:
        multipliers.append(weights[sd_key])

    if not multipliers:
        return None

    composite = sum(multipliers) / len(multipliers)
    return max(0.6, min(1.4, composite))


# ── Case / Risk text builders ─────────────────────────────────────────────────

def _case_from_stats(player: str, stat: str, line: float,
                     over_map: dict, under_map: dict,
                     best_book: str, true_prob: float, ev: float,
                     card: dict, side: str = "over") -> str:
    """Narrative WHY case — specific analytical reasons this prop hits."""
    splits  = card.get("splits", {})
    pitcher = card.get("pitcher", {})
    bvp     = card.get("bvp", {})
    is_over = side == "over"
    side_label = "Over" if is_over else "Under"

    l5  = splits.get("l5") or {}
    l10 = splits.get("l10") or {}
    l20 = splits.get("l20") or {}
    season_avg = splits.get("season_avg", 0) or 0

    def hit_rate(r):
        if not r: return 0
        raw = r.get("rate", 0)
        return (100 - raw) if not is_over else raw

    r5  = hit_rate(l5)
    r10 = hit_rate(l10)
    r20 = hit_rate(l20)
    avg5  = l5.get("avg", 0) or 0
    avg10 = l10.get("avg", 0) or 0

    parts = []

    # ── 0. L5 Trap Detection ──────────────────────────────────────────────────
    # Detect when the visible recent form contradicts the matchup data.
    # Sportsbooks (especially PrizePicks) surface L5 prominently to attract
    # casual money to one side — the analyst angle is the matchup behind it.
    pname_trap = pitcher.get("name") or "tonight's pitcher"
    bvp_ab  = (bvp or {}).get("ab", 0)
    bvp_avg = _bvp_avg(bvp or {})
    bvp_avg_str = (bvp or {}).get("avg", "")
    if is_over and r5 <= 40 and bvp_ab >= 8 and bvp_avg >= 0.300:
        parts.append(
            f"⚠️ **L5 Trap** — {player} looks cold on L5 ({r5:.0f}% over rate), "
            f"but is batting **{bvp_avg_str}** in {bvp_ab} career AB vs {pname_trap}. "
            f"The recent slump is masking a favorable matchup."
        )
    elif not is_over and r5 >= 60 and bvp_ab >= 8 and bvp_avg >= 0.300:
        parts.append(
            f"⚠️ **Matchup conflict** — L5 under form looks strong ({r5:.0f}%), "
            f"but {player} is **{bvp_avg_str}** in {bvp_ab} career AB vs {pname_trap}. "
            f"Career BvP suggests this pitcher gets hit — monitor closely."
        )

    # ── 1. Lead with the hit rate story ───────────────────────────────────────
    if r10 >= 70:
        g10  = l10.get("games", 10)
        h10  = (g10 - l10.get("hits", 0)) if not is_over else l10.get("hits", 0)
        icon = "🔥" if r10 >= 80 else "✅"
        parts.append(
            f"{icon} **{player}** is hitting the {side_label} {line} {stat} in "
            f"**{h10}/{g10} of his last 10 games ({r10:.0f}%)** — "
            f"averaging {avg10} per game over that span."
        )
    elif r5 >= 70:
        g5 = l5.get("games", 5)
        h5 = (g5 - l5.get("hits", 0)) if not is_over else l5.get("hits", 0)
        parts.append(
            f"🔥 **{player}** has hit the {side_label} {line} in "
            f"**{h5}/{g5} of his last 5 games ({r5:.0f}%)** — hot stretch right now."
        )
    elif r10 >= 55:
        parts.append(
            f"✅ **{player}** is hitting this {side_label} at **{r10:.0f}% over L10** — "
            f"consistent edge at this line."
        )

    # ── 2. Trend direction ────────────────────────────────────────────────────
    # Guard: only print ONE trend label per card. "Elite form" and "cold stretch"
    # are mutually exclusive — determine which is true before writing anything.
    _trend_written = False
    if r5 and r10 and r20:
        if r5 > r10 > r20:
            parts.append(
                f"📈 **Trend accelerating** — L20 {r20:.0f}% → L10 {r10:.0f}% → L5 {r5:.0f}%. "
                f"Form is improving, not regressing."
            )
            _trend_written = True
        elif r5 >= 80 and r10 >= 70:
            # Only call it "elite form" if L5 is NOT contradicting it (i.e. L5 >= L10)
            if r5 >= r10:
                parts.append(
                    f"📈 **Elite recent form** — {r5:.0f}% L5 / {r10:.0f}% L10. "
                    f"This is a player locked in right now."
                )
                _trend_written = True
        elif r5 < 50 and r10 >= 60:
            # L5 clearly fading vs L10 baseline — say so, not both
            parts.append(
                f"📉 **Cooling off** — L10 at {r10:.0f}% but L5 has dropped to {r5:.0f}%. "
                f"Recent form is not confirming the trend."
            )
            _trend_written = True

    # ── 3. Line value vs season avg ───────────────────────────────────────────
    if season_avg and line:
        gap = season_avg - float(line)
        if is_over and gap >= 0.5:
            parts.append(
                f"📊 **Season avg {season_avg} vs line {line}** — "
                f"the line is {gap:.1f} below his full-season floor. "
                f"Books are pricing him cheap."
            )
        elif not is_over and gap <= -0.3:
            # Only print "cold stretch" if the trend block didn't already say "elite form"
            if not _trend_written or r5 < 70:
                parts.append(
                    f"📊 **Season avg {season_avg} vs line {line}** — "
                    f"even his full-season average is below this line, "
                    f"supporting the under."
                )
            # Never append the "cold stretch" line when L5 ≥ 80 — that's the contradiction
        elif not is_over and 0 < gap < 0.5:
            parts.append(
                f"📊 Season avg ({season_avg}) is only slightly above the {line} line — "
                f"recent form ({avg10} avg L10) is dragging him well under it now."
            )

    # ── 4. Pitcher matchup ────────────────────────────────────────────────────
    pname          = pitcher.get("name")
    pera           = pitcher.get("era")
    pfip           = pitcher.get("fip")
    phr9           = pitcher.get("hr_per_9")
    phand          = pitcher.get("hand", "?")
    last5          = pitcher.get("last_5_starts", [])
    validated_role = pitcher.get("validated_role")
    avg_ip_l3      = pitcher.get("avg_ip_l3")
    role_override  = pitcher.get("role_overridden", False)

    # Role integrity check — flag if depth chart says RP but game logs say SP
    if pname and validated_role:
        gs_season = pitcher.get("games_started", 0)
        if role_override:
            parts.append(
                f"📋 **Role override: {pname} flagged as Starting Pitcher** — "
                f"depth chart shows 0 GS this season, but last 3 appearances "
                f"averaged **{avg_ip_l3} IP** — treating as starter for this projection."
            )
        elif validated_role == "SWINGMAN" and avg_ip_l3:
            parts.append(
                f"📋 **{pname} in swingman role** — averaging {avg_ip_l3} IP over "
                f"last 3 outings. Outing length uncertain; K volume and run exposure "
                f"may be limited if pulled early."
            )

    if pname and pera:
        era_f = float(pera) if pera != "?" else None
        fip_f = float(pfip) if pfip and pfip != "?" else None
        hr9_f = float(phr9) if phr9 and phr9 != "?" else None

        if is_over:
            if era_f and era_f >= 4.5:
                parts.append(
                    f"⚔️ **Favourable matchup vs {pname} ({phand}HP)** — "
                    f"{pera} ERA pitcher who gives up runs. More opportunities to score."
                )
            elif era_f and era_f <= 3.20:
                parts.append(
                    f"⚔️ Facing {pname} ({phand}HP, {pera} ERA) — "
                    f"elite arm, but {player}'s recent form overrides matchup quality."
                )
            else:
                parts.append(
                    f"⚔️ Matchup vs {pname} ({phand}HP) — {pera} ERA, {phr9} HR/9."
                )
            if hr9_f and hr9_f >= 1.2:
                parts.append(
                    f"💣 **{pname} allows {phr9} HR/9** — one of the more homer-prone arms "
                    f"in the league. Extra-base threat is real."
                )
        else:
            # Under prop — high HR/9 is a DIRECT threat, not a neutral data point.
            # A homer-prone pitcher facing a batter with platoon advantage actively
            # pushes production UP, which works against the Under. Flag it clearly.
            if hr9_f and hr9_f >= 1.2:
                parts.append(
                    f"🚨 **Under risk — {pname} ({phand}HP) allows {phr9} HR/9** — "
                    f"homer-prone arm creates extra-base exposure. "
                    f"High HR/9 favors the Over, not this Under."
                )
            elif era_f and era_f <= 3.50:
                parts.append(
                    f"⚔️ **Pitcher advantage** — {pname} ({phand}HP) has a {pera} ERA. "
                    f"Quality arm suppresses production, supports the under."
                )
            elif fip_f and fip_f <= 3.20:
                parts.append(
                    f"⚔️ {pname} ({phand}HP) — FIP {pfip} suggests better than his ERA shows. "
                    f"Underlying stuff favours the under."
                )
            else:
                parts.append(
                    f"⚔️ Matchup vs {pname} ({phand}HP) — {pera} ERA / {phr9} HR/9."
                )

        # Recent pitcher form
        if last5:
            recent_er = [s.get("er", 0) for s in last5[:3]]
            avg_er = sum(recent_er) / len(recent_er) if recent_er else None
            if avg_er is not None:
                if not is_over and avg_er <= 1.5:
                    parts.append(
                        f"🔒 **{pname} on a roll** — averaging {avg_er:.1f} ER over last "
                        f"{len(recent_er)} starts. Locked in right now."
                    )
                elif is_over and avg_er >= 3.5:
                    parts.append(
                        f"💥 **{pname} struggling lately** — averaging {avg_er:.1f} ER over "
                        f"last {len(recent_er)} starts. Hitters are teeing off."
                    )

    # ── 5. BvP history ────────────────────────────────────────────────────────
    if bvp.get("ab", 0) >= 5:
        bavg = bvp.get("avg", ".000")
        ab   = bvp.get("ab", 0)
        hr   = bvp.get("hr", 0)
        try:
            avg_f = float("0" + bavg) if bavg.startswith(".") else float(bavg)
        except ValueError:
            avg_f = 0
        if is_over and avg_f >= 0.300:
            parts.append(
                f"🎯 **Owns this pitcher historically** — AVG {bavg} in {ab} career AB. "
                f"Familiarity breeds confidence."
            )
        elif is_over and hr >= 2:
            parts.append(
                f"🎯 **{hr} career HR vs {pname}** in {ab} AB — has power history here."
            )
        elif not is_over and avg_f <= 0.180:
            parts.append(
                f"🎯 **Struggles vs this pitcher** — career AVG {bavg} in {ab} AB. "
                f"History supports the under."
            )

    # ── 6. Streak ─────────────────────────────────────────────────────────────
    streak = l5.get("streak", 0) if l5 else 0
    if not is_over:
        streak = -streak
    if streak >= 4:
        parts.append(
            f"🔥 **Active {streak}-game hit streak** — momentum is real, ride it."
        )

    # ── 7. Home/Away venue split ──────────────────────────────────────────────
    ha        = card.get("home_away") or {}
    is_home   = card.get("_is_home")   # injected by enrich_mlb before this call
    home_avg  = ha.get("home_avg")
    away_avg  = ha.get("away_avg")
    home_rate = ha.get("home_rate")   # % Over at home (last 20 games)
    away_rate = ha.get("away_rate")   # % Over on the road (last 20 games)
    hg        = ha.get("home_games", 0)
    ag        = ha.get("away_games", 0)

    if hg >= 4 and ag >= 4 and home_avg is not None and away_avg is not None:
        venue_icon  = "🏠" if is_home is True else ("✈️" if is_home is False else "📍")
        venue_word  = "home" if is_home is True else ("road" if is_home is False else "unknown venue")
        cur_avg     = home_avg if is_home is True else (away_avg if is_home is False else None)
        cur_rate    = home_rate if is_home is True else (away_rate if is_home is False else None)
        other_avg   = away_avg if is_home is True else home_avg
        other_rate  = away_rate if is_home is True else home_rate
        other_word  = "road" if is_home is True else "home"

        # Build compact rate strings (show rate only if available)
        def _fmt_split(avg, rate, games):
            rate_str = f", {rate:.0f}% Over rate" if rate is not None else ""
            return f"**{avg}** avg{rate_str} ({games}G)"

        if cur_avg is not None:
            cur_str   = _fmt_split(cur_avg, cur_rate, hg if is_home else ag)
            other_str = _fmt_split(other_avg, other_rate, ag if is_home else hg)

            # Decide tone based on tonight's venue performance vs line
            if cur_rate is not None and cur_rate >= 70 and is_over:
                tone = f"strong {venue_word} history — using this split tonight."
            elif cur_rate is not None and cur_rate <= 35 and is_over:
                tone = f"struggles {venue_word} — caution on the Over."
            elif cur_rate is not None and (100 - cur_rate) >= 70 and not is_over:
                tone = f"solid Under profile {venue_word} — using this split tonight."
            elif cur_avg is not None and cur_avg >= float(line):
                tone = f"{venue_word.capitalize()} avg at or above the line."
            elif cur_avg is not None and cur_avg < float(line) * 0.80:
                tone = f"{venue_word.capitalize()} avg well below the line."
            else:
                tone = f"using {venue_word} profile tonight."

            parts.append(
                f"{venue_icon} **Venue split** — batting **{venue_word}** tonight. "
                f"{venue_word.capitalize()}: {cur_str} · {other_word.capitalize()}: {other_str}. "
                f"{tone.capitalize()}"
            )

    # ── 8. Statcast power signal ──────────────────────────────────────────────
    sc         = card.get("statcast") or {}
    barrel_pct = sc.get("barrel_pct") or 0
    try:
        hr9 = float((pitcher or {}).get("hr_per_9") or 0)
    except (ValueError, TypeError):
        hr9 = 0.0
    _power_kw = ("home run", "total base", "hits+run", "rbi")
    is_power  = is_over and any(kw in stat.lower() for kw in _power_kw)
    if is_power and barrel_pct >= 10 and hr9 >= 1.0:
        parts.append(
            f"💥 **Elite barrel rate** ({barrel_pct:.0f}% Brl) vs a pitcher "
            f"allowing {hr9:.2f} HR/9 — structural power edge."
        )
    elif is_power and barrel_pct >= 14:
        parts.append(
            f"💥 **Top-tier barrel rate** ({barrel_pct:.0f}%) — extra-base "
            f"threat regardless of matchup."
        )

    # ── Fallback ──────────────────────────────────────────────────────────────
    if not parts:
        parts.append(
            f"Edge: {side_label} {line} {stat} at {ev:+.1f}% EV. "
            f"L10 hit rate {r10:.0f}%, season avg {season_avg}."
        )

    return "\n".join(parts)


def _risk_from_stats(ev: float, n_books: int, line: float, best_odds: int,
                     card: dict, side: str = "over") -> str:
    """Specific reasons this prop could lose — actual risk factors, not generic warnings."""
    splits  = card.get("splits", {})
    pitcher = card.get("pitcher", {})
    bvp     = card.get("bvp", {})
    is_over = side == "over"

    l5  = splits.get("l5") or {}
    l10 = splits.get("l10") or {}
    l20 = splits.get("l20") or {}
    season_avg = splits.get("season_avg", 0) or 0

    def hit_rate(r):
        if not r: return None
        raw = r.get("rate", 0)
        return (100 - raw) if not is_over else raw

    r5  = hit_rate(l5)
    r10 = hit_rate(l10)
    r20 = hit_rate(l20)
    avg10 = l10.get("avg", 0) or 0

    risks = []

    # ── 1. Trend divergence ───────────────────────────────────────────────────
    if r5 and r10 and r20:
        if r5 > r10 and r20 > r10:
            risks.append(
                f"⚠️ **L10 is the weakest window** — L5 {r5:.0f}% and L20 {r20:.0f}% "
                f"both outperform L10 ({r10:.0f}%). Inconsistent pattern."
            )
        elif r5 and r10 and r5 < r10 - 15:
            risks.append(
                f"⚠️ **Fading recently** — L10 at {r10:.0f}% but L5 dropped to {r5:.0f}%. "
                f"Form may be turning against this side."
            )
        if r20 and r10 and r20 < 50 and r10 >= 65:
            risks.append(
                f"⚠️ **L20 only {r20:.0f}%** — the hot streak is recent. "
                f"Longer-term tendency is actually the other side."
            )

    # ── 2. Season avg vs line ─────────────────────────────────────────────────
    if season_avg and line:
        gap = season_avg - float(line)
        if is_over and gap < 0.2:
            risks.append(
                f"⚠️ **Season avg {season_avg} barely clears the {line} line** — "
                f"no comfortable buffer. One cold game misses."
            )
        elif not is_over and gap >= 0.8:
            risks.append(
                f"⚠️ **Season avg {season_avg} is well above the {line} line** — "
                f"his true average says he should clear this most nights. "
                f"Recent cold stretch may not last."
            )

    # ── 3. Pitcher risk ───────────────────────────────────────────────────────
    pname = pitcher.get("name")
    pera  = pitcher.get("era")
    pfip  = pitcher.get("fip")
    phr9  = pitcher.get("hr_per_9")
    phand = pitcher.get("hand", "?")
    last5 = pitcher.get("last_5_starts", [])

    if pname and pera and pera != "?":
        era_f = float(pera)
        fip_f = float(pfip) if pfip and pfip != "?" else None
        hr9_f = float(phr9) if phr9 and phr9 != "?" else None

        if is_over and era_f <= 3.20:
            risks.append(
                f"⚠️ **{pname} is an elite arm** ({pera} ERA) — "
                f"could neutralise even hot hitters. Upside is capped."
            )
            if fip_f and fip_f <= 3.00:
                risks.append(
                    f"⚠️ **FIP {pfip}** confirms the ERA isn't luck — "
                    f"this pitcher genuinely suppresses contact."
                )
        elif not is_over and era_f >= 4.80:
            risks.append(
                f"⚠️ **{pname} has a {pera} ERA** — a run-prone arm is the main counter to this UNDER. "
                f"If he leaks tonight, even a cold hitter can generate extra production."
            )
        if not is_over and hr9_f and hr9_f >= 1.2:
            risks.append(
                f"⚠️ **{pname} allows {phr9} HR/9** — extra-base vulnerability. "
                f"One swing could push the over."
            )

        # Pitcher recent form risk
        if last5:
            recent_er = [s.get("er", 0) for s in last5[:3]]
            avg_er = sum(recent_er) / len(recent_er) if recent_er else None
            if avg_er is not None:
                if is_over and avg_er <= 1.0:
                    risks.append(
                        f"⚠️ **{pname} is locked in** — averaging only {avg_er:.1f} ER "
                        f"over his last {len(recent_er)} starts. Tough to score on right now."
                    )
                elif not is_over and avg_er >= 4.0:
                    risks.append(
                        f"⚠️ **{pname} is struggling lately** — {avg_er:.1f} ER per start "
                        f"recently. May give up more than expected tonight."
                    )

    elif not pname:
        risks.append(
            "⚠️ **No confirmed starter** — if an opener or bullpen game is used, "
            "matchup dynamics are unpredictable."
        )

    # ── 4. BvP risk ───────────────────────────────────────────────────────────
    if bvp.get("ab", 0) >= 8:
        try:
            bavg = bvp.get("avg", ".000")
            avg_f = float("0" + bavg) if bavg.startswith(".") else float(bavg)
            if is_over and avg_f <= 0.175:
                risks.append(
                    f"⚠️ **Struggles vs {pname}** — career AVG {bavg} in {bvp['ab']} AB. "
                    f"Historical matchup works against this over."
                )
            elif not is_over and avg_f >= 0.320:
                risks.append(
                    f"⚠️ **Hits this pitcher well** — career AVG {bavg} in {bvp['ab']} AB. "
                    f"He tends to produce when they meet."
                )
        except (ValueError, TypeError):
            pass

    # ── 5. Miss streak warning ────────────────────────────────────────────────
    streak = (l5.get("streak", 0) if l5 else 0)
    if not is_over:
        streak = -streak
    if streak <= -3:
        risks.append(
            f"⚠️ **Active {abs(streak)}-game miss streak** — "
            f"momentum is working against this side right now."
        )

    # ── 6. Line thinness ─────────────────────────────────────────────────────
    if float(line) <= 0.5:
        risks.append(
            f"⚠️ **Binary line at {line}** — zero room for error. "
            f"One bad at-bat ends it. Size down on this one."
        )

    # ── 7. Single book risk ───────────────────────────────────────────────────
    if n_books == 1:
        risks.append(
            f"⚠️ **Only 1 book offering this line** — low market confidence. "
            f"If that book adjusts before game time, the edge disappears."
        )

    if not risks:
        risks.append(
            "✅ No major red flags identified. Standard pre-game checks apply — "
            "confirm lineup and starting pitcher before locking in."
        )

    return "\n".join(risks)


def _case_odds_only(player: str, stat: str, line: float,
                    over_map: dict, under_map: dict,
                    best_book: str, true_prob: float, ev: float,
                    sport: str, side: str = "over") -> str:
    """Fallback case summary (no stats layer) for NBA or missed lookups."""
    is_over   = side == "over"
    side_label = "Over" if is_over else "Under"
    price_map  = over_map if is_over else under_map
    n_books    = len(price_map)

    odds_parts = []
    for b in sorted(over_map, key=lambda b: american_to_decimal(over_map[b]), reverse=True):
        o = fmt_odds(over_map[b])
        u = fmt_odds(under_map[b]) if b in under_map else "n/a"
        odds_parts.append(f"{BOOK_DISPLAY.get(b,b)}: O {o} / U {u}")
    fair = (round((1/true_prob - 1)*100) if true_prob <= 0.5
            else round(-100 / (1 - true_prob)))
    return (
        f"**Line:** {side_label} {line} {stat}  |  **Best price:** {fmt_odds(price_map.get(best_book,0))} "
        f"at {BOOK_DISPLAY.get(best_book,best_book)}\n"
        f"**Odds board:** {' · '.join(odds_parts)}\n"
        f"**Fair value (no-vig):** {fmt_odds(fair)}  |  **Edge:** {ev:+.1f}%\n"
        f"Consensus across {n_books} book(s) places the true probability at "
        f"{true_prob*100:.1f}%. {BOOK_DISPLAY.get(best_book,best_book)} is offering "
        f"better-than-fair odds on the {side_label} in {sport}."
    )


def _risk_odds_only(ev: float, n_books: int, line: float, best_odds: int) -> str:
    tier = "strong" if ev >= 6 else "solid" if ev >= 3 else "marginal"
    juice = ("Standard pricing." if best_odds >= -150
             else f"Juiced to {fmt_odds(best_odds)} — edge compressed.")
    line_note = ("Thin line ≤0.5 — binary result, max 0.5u."
                 if line <= 0.5 else f"Line {line} has solid sample depth.")
    return (
        f"{tier.capitalize()} edge at +{ev:.1f}% EV across {n_books} book(s). "
        f"{juice} {line_note} "
        f"Watch for juice movement pre-game — EV evaporates if best book tightens."
    )

# ── NBA stats text builders ───────────────────────────────────────────────────

def _case_from_nba_stats(player: str, stat: str, line: float,
                          over_map: dict, under_map: dict,
                          best_book: str, true_prob: float, ev: float,
                          card: dict, side: str = "over") -> str:
    """Rich case summary using NBA statistical payload."""
    is_over    = side == "over"
    side_label = "Over" if is_over else "Under"
    price_map  = over_map if is_over else under_map
    splits  = card.get("splits", {})
    defense = card.get("defense", {})

    l5  = splits.get("l5")
    l10 = splits.get("l10")
    l20 = splits.get("l20")

    def _rate(r, invert=False):
        if not r: return "n/a"
        rate = (100 - r["rate"]) if invert else r["rate"]
        hits = (r["games"] - r["hits"]) if invert else r["hits"]
        icon = "🔥" if rate >= 70 else "✅" if rate >= 50 else "❌"
        return f"{icon} {rate}% ({hits}/{r['games']}) avg {r['avg']}"

    odds_parts = []
    for b in sorted(over_map, key=lambda b: american_to_decimal(over_map[b]), reverse=True):
        o_str = fmt_odds(over_map[b])
        u_str = fmt_odds(under_map[b]) if b in under_map else "n/a"
        odds_parts.append(f"{BOOK_DISPLAY.get(b,b)}: O {o_str} / U {u_str}")

    fair_american = (
        round((1 / true_prob - 1) * 100) if true_prob <= 0.5
        else round(-100 / (1 - true_prob))
    )

    rank = defense.get("league_rank")
    rank_str = f"#{rank}/30" if rank else "n/a"
    quality = ("elite" if rank and rank <= 5 else
               "stingy" if rank and rank <= 10 else
               "average" if rank and rank <= 20 else
               "generous" if rank and rank <= 25 else
               "very generous") if rank else "unknown"

    return (
        f"**Line:** {side_label} {line} {stat}  |  **Best price:** {fmt_odds(price_map.get(best_book,0))} at {BOOK_DISPLAY.get(best_book,best_book)}\n"
        f"**Odds board:** {' · '.join(odds_parts)}\n"
        f"**Fair value (no-vig):** {fmt_odds(fair_american)}  |  **Edge:** {ev:+.1f}%\n"
        f"**Splits —** L5: {_rate(l5, not is_over)}  ·  L10: {_rate(l10, not is_over)}  ·  L20: {_rate(l20, not is_over)}\n"
        f"**Season avg:** {splits.get('season_avg','?')} {stat} / game  "
        f"({splits.get('games_played','?')} G)\n"
        f"**Trend:** {card.get('trend_signal','?')}\n"
        f"**Opponent defense:** {defense.get('team_name','?')} — "
        f"allows {defense.get('avg_allowed','?')} {stat}/game  "
        f"(rank {rank_str} — {quality})"
    )


def _risk_from_nba_stats(ev: float, n_books: int, line: float, best_odds: int,
                          card: dict, side: str = "over") -> str:
    """Risk summary enriched with NBA stats-layer signals."""
    tier    = card.get("tier", "LEAN")
    splits  = card.get("splits", {})
    defense = card.get("defense", {})

    ev_tier    = "strong" if ev >= 6 else "solid" if ev >= 3 else "marginal"
    juice_note = (
        "Standard pricing — full size playable."
        if best_odds >= -150
        else f"Juiced to {fmt_odds(best_odds)} — edge exists but payout is compressed."
    )
    line_note = (
        "Thin line (≤0.5) — binary result, max 0.5u."
        if line <= 0.5
        else f"Line of {line} has solid sample depth."
    )

    # Streak note — invert for Under (a miss streak on Over = hit streak on Under)
    is_over = side == "over"
    l5      = splits.get("l5", {})
    streak  = l5.get("streak", 0) if l5 else 0
    if not is_over:
        streak = -streak
    streak_note = ""
    if streak <= -3:
        streak_note = f" ⚠️ Active {abs(streak)}-game miss streak — fade risk."
    elif streak >= 4:
        streak_note = f" 🔥 Active {streak}-game hit streak — momentum play."

    # Defense matchup note (favorable for Over = unfavorable for Under)
    rank     = defense.get("league_rank")
    def_note = ""
    if rank is not None:
        if is_over and rank >= 25:
            def_note = f" Favorable matchup — opponent ranks #{rank}/30 (very generous defense)."
        elif is_over and rank <= 5:
            def_note = f" ⚠️ Tough matchup — opponent ranks #{rank}/30 (elite defense)."
        elif not is_over and rank <= 5:
            def_note = f" Favorable matchup — opponent ranks #{rank}/30 (elite defense, Under-friendly)."
        elif not is_over and rank >= 25:
            def_note = f" ⚠️ Tough matchup for Under — opponent ranks #{rank}/30 (very generous defense)."

    return (
        f"**Stats tier:** {tier}  |  **Market tier:** {ev_tier} edge at {ev:+.1f}% EV  "
        f"across {n_books} book(s).\n"
        f"{juice_note} {line_note}{streak_note}{def_note}\n"
        f"Watch for line or juice movement before tip-off — "
        f"sharp action will compress EV quickly."
    )


# ── NBA stats enrichment ──────────────────────────────────────────────────────

def enrich_nba(rows: list[dict], opp_lookup: dict[int, int]) -> list[dict]:
    """
    For each NBA row:
      1. Resolve the player's current team → find today's opponent team_id.
      2. Call stats_nba.get_full_card().
      3. Apply _should_include gate (EV floor + stats-tier bypass).
      4. Enrich summaries with statistical payload.
    """
    enriched      = []
    passed        = 0
    discarded     = 0
    no_opponent   = 0

    for row in rows:
        player     = row["player_name"]
        line       = row["line"]
        ev         = row["ev_percentage"]
        side       = row.get("side", "over")
        market_key = row["market_key"]
        prop_type  = NBA_MARKET_TO_PROP_TYPE.get(market_key, "points")

        # ── find today's opponent ────────────────────────────────────────────
        player_id  = stats_nba.get_player_id(player)
        opp_id     = None
        if player_id:
            team_id = stats_nba.get_player_current_team(player_id)
            if team_id:
                opp_id = opp_lookup.get(team_id)

        if opp_id is None:
            no_opponent += 1
            print(f"    DROP  {player} — no NBA game today (opp_id=None)")
            continue

        # ── stats card ───────────────────────────────────────────────────────
        side_label = "O" if side == "over" else "U"
        print(f"    Stats: {player} ({prop_type} {side_label}{line}) vs team_id={opp_id}")
        card = stats_nba.get_full_card(player, opp_id, line, prop_type)

        if "error" in card:
            no_opponent += 1
            print(f"    DROP  {player} — {card['error']}")
            continue

        raw_tier = card.get("tier", "PASS")
        # For Under side, invert the stats tier (an Over ELITE is a Under PASS)
        tier = TIER_INVERT.get(raw_tier, raw_tier) if side == "under" else raw_tier

        splits = card.get("splits", {})
        include, signal_type = _should_include(ev, tier, splits, side)
        if not include:
            discarded += 1
            print(f"    PASS  {player} {side_label}{line} — tier={tier} ev={ev:+.1f}%")
            continue

        passed += 1
        row["vortex_score"] = compute_score(
            ev, row["n_books"], line, row["best_odds"], tier, signal_type)
        row["case_summary"] = _case_from_nba_stats(
            player, row["stat_type"], line,
            row["over_map"], row["under_map"],
            row["best_book"], row["true_prob"],
            ev, card, side)
        row["risk_summary"] = _risk_from_nba_stats(
            ev, row["n_books"], line, row["best_odds"], card, side)
        row["sportsbook"]   = BOOK_DISPLAY.get(row["best_book"], row["best_book"])
        row["stats_json"]   = json.dumps({
            "player_id":    player_id,
            "tier":         tier,
            "signal_type":  signal_type,
            "side":         side,
            "splits":       card.get("splits"),
            "defense":      card.get("defense"),
            "trend_signal": card.get("trend_signal"),
            "true_prob":    row.get("true_prob"),
            "best_odds":    row.get("best_odds"),
        }, default=str)
        row["tier"] = tier
        enriched.append(row)

    print(f"  NBA stats filter: {passed} kept · "
          f"{discarded} PASS/below-bypass · {no_opponent} dropped (no game / cross-sport)")
    return enriched


# ── WNBA enrichment (ESPN data + basketball scoring engine) ──────────────────

_WNBA_TIER = {"Elite": "ELITE", "Strong": "STRONG", "Good": "GOOD",
              "Lean": "LEAN", "Risky": "RISKY", "Fade": "FADE"}


def _case_wnba(player, stat_label, line, side, grade, splits, opp_abbr) -> str:
    """Plain-language case line for a WNBA board card."""
    l10 = splits.get("l10") or {}
    rate = l10.get("rate", 0) or 0
    eff  = (100 - rate) if side == "under" else rate
    avg  = l10.get("avg", 0) or 0
    sidetxt = "Under" if side == "under" else "Over"
    edge = grade.get("proj_edge", 0)
    parts = [
        f"{player} {sidetxt} {line:g} {stat_label} vs {opp_abbr}",
        f"L10 {eff:.0f}% · avg {avg} (edge {edge:+.1f})",
        f"{grade['minutes_l10']:.0f} min/g",
    ]
    if grade.get("stability"):
        parts.append(grade["stability"].capitalize() + " stability")
    return " · ".join(parts)


def _risk_wnba(grade) -> str:
    """Risk line from the grade's flags."""
    flags = grade.get("risk_flags") or []
    label = {
        "low_minutes": "⚠️ low minutes role",
        "minutes_dropping": "⚠️ minutes trending down",
        "sharp_line": "⚠️ line far from recent avg (sharp money)",
        "inconsistent": "📊 L10 diverges from season baseline",
        "cold_form": "📉 cold recent form",
        "volatile": "⚡ high variance — boom/bust",
        "b2b": "🔁 back-to-back game",
        "blowout_risk": "💥 blowout risk — bench minutes",
        "teammate_out": "⬆️ starter teammate OUT — usage bump",
        "minutes_uncertain": "❓ player questionable — minutes risk",
    }
    out = [label.get(f, f) for f in flags]
    if not out:
        out = ["No major red flags in available data."]
    return " · ".join(out)


_WNBA_BLOWOUT_SPREAD = 12.5   # |spread| at/above this → bench-minutes risk both sides

# Odds-API abbr/name → ESPN abbr (only where they differ)
_WNBA_NAME_TO_ABBR = {
    "atlanta dream": "ATL", "chicago sky": "CHI", "connecticut sun": "CON",
    "dallas wings": "DAL", "golden state valkyries": "GS", "indiana fever": "IND",
    "las vegas aces": "LV", "los angeles sparks": "LA", "minnesota lynx": "MIN",
    "new york liberty": "NY", "phoenix mercury": "PHX", "seattle storm": "SEA",
    "washington mystics": "WSH", "toronto tempo": "TOR", "portland fire": "POR",
}


def _wnba_spreads() -> dict:
    """
    One Odds-API call for WNBA game spreads. Returns {ESPN_abbr: |spread|} for
    every team playing — used for both blowout detection and surfacing the actual
    number on the card (tight game = full minutes; big spread = bench risk).
    """
    if not API_KEY:
        return {}
    try:
        resp = ODDS_SESSION.get(
            f"{BASE_URL}/sports/basketball_wnba/odds",
            params={"apiKey": API_KEY, "regions": "us",
                    "markets": "spreads", "oddsFormat": "american"},
            timeout=15)
        if resp.status_code != 200:
            return {}
        out = {}
        for game in resp.json():
            home = (game.get("home_team") or "").lower()
            away = (game.get("away_team") or "").lower()
            spread = None
            for bm in game.get("bookmakers", []):
                for mkt in bm.get("markets", []):
                    if mkt.get("key") != "spreads":
                        continue
                    for oc in mkt.get("outcomes", []):
                        pt = oc.get("point")
                        if pt is not None:
                            spread = abs(float(pt))
                            break
                    if spread is not None:
                        break
                if spread is not None:
                    break
            if spread is not None:
                for nm in (home, away):
                    ab = _WNBA_NAME_TO_ABBR.get(nm)
                    if ab:
                        out[ab] = round(spread, 1)
        return out
    except Exception as e:
        print(f"  [warn] WNBA spreads fetch failed: {e}")
        return {}


def _wnba_blowout_teams() -> set:
    """Set of ESPN team abbrs in games with |spread| ≥ blowout threshold."""
    return {ab for ab, s in _wnba_spreads().items() if s >= _WNBA_BLOWOUT_SPREAD}


def enrich_wnba(rows: list[dict], opp_lookup: dict, league_pace: float | None,
                def_ranks: dict | None = None, blowout_abbrs: set | None = None) -> list[dict]:
    """
    For each WNBA row:
      1. Resolve player → ESPN id, team, tonight's opponent (must be pre-game).
      2. Pull L5/L10/L20 splits + minutes from ESPN.
      3. Apply the 4-filter funnel: form, minutes, pace, opponent defense rank,
         and game-script flags (back-to-back, blowout risk).
      4. Score both sides with grade_wnba; keep Strong/Elite for the board.
    """
    enriched = passed = discarded = no_game = 0
    out = []
    def_ranks     = def_ranks or {}
    blowout_abbrs = blowout_abbrs or set()
    _pace_cache: dict = {}
    _b2b_cache: dict = {}

    def _pace(team_id):
        if team_id not in _pace_cache:
            _pace_cache[team_id] = stats_wnba.get_team_pace(team_id)
        return _pace_cache[team_id]

    def _b2b(team_id, iso):
        if team_id not in _b2b_cache:
            _b2b_cache[team_id] = stats_wnba.is_back_to_back(team_id, iso)
        return _b2b_cache[team_id]

    for row in rows:
        player     = row["player_name"]
        line       = row["line"]
        side       = row.get("side", "over")
        market_key = row["market_key"]
        prop_type  = stats_wnba.MARKET_TO_PROP_TYPE.get(market_key)
        if not prop_type:
            continue

        pinfo = stats_wnba.get_player_id(player)
        if not pinfo:
            no_game += 1
            print(f"    DROP  {player} — no WNBA player match")
            continue

        matchup = opp_lookup.get(pinfo["abbr"])
        if not matchup:
            no_game += 1
            print(f"    DROP  {player} — no WNBA game today ({pinfo['abbr']})")
            continue
        if matchup.get("state") == "post":
            no_game += 1
            print(f"    DROP  {player} — game already finished (state=post)")
            continue

        # ── Filter 5: injuries / lineup context ──────────────────────────────
        # Never bet a player who is ruled OUT — drop the prop entirely.
        self_status = stats_wnba.player_injury_status(pinfo["team_id"], player)
        if self_status == "out":
            no_game += 1
            print(f"    DROP  {player} — ruled OUT (injury)")
            continue
        # A starter-level teammate sitting bumps this player's usage → Over lean.
        teammate_out = stats_wnba.key_teammate_out(pinfo["team_id"], player)

        splits = stats_wnba.get_historical_splits(pinfo["id"], line, prop_type)
        if not splits or not (splits.get("l10") or {}).get("games"):
            discarded += 1
            print(f"    PASS  {player} — no gamelog data")
            continue

        # Filter 2 — opponent defense vs this stat
        def_stat = stats_wnba.DEF_STAT_FOR_PROP.get(prop_type)
        stat_ranks = def_ranks.get(def_stat, {}) if def_stat else {}
        opp_def_rank = stat_ranks.get(matchup["opp_abbr"])
        n_teams = len(stat_ranks) or 15
        opp_def_allowed = (def_ranks.get("_allowed", {}).get(def_stat, {}) or {}).get(matchup["opp_abbr"]) if def_stat else None
        league_def_avg  = (def_ranks.get("_league_avg", {}) or {}).get(def_stat) if def_stat else None

        # Filter 4 — game-script flags
        game_flags = []
        if _b2b(pinfo["team_id"], matchup.get("commence_time", "")):
            game_flags.append("b2b")
        if pinfo["abbr"] in blowout_abbrs:
            game_flags.append("blowout_risk")

        _kw = dict(prop_type=prop_type,
                   team_pace=_pace(pinfo["team_id"]), opp_pace=_pace(matchup["opp_id"]),
                   league_pace=league_pace, opp_def_rank=opp_def_rank, n_teams=n_teams,
                   game_flags=game_flags, teammate_out=teammate_out, self_status=self_status)
        both  = grade_wnba.grade_pick_both(splits, line, **_kw)
        grade = both["under_grade"] if side == "under" else both["over_grade"]

        if grade["label"] not in ("Elite", "Strong"):
            discarded += 1
            side_label = "O" if side == "over" else "U"
            print(f"    PASS  {player} {side_label}{line:g} — {grade['label']} ({grade['score']})")
            continue

        passed += 1
        tier = _WNBA_TIER.get(grade["label"], "STRONG")
        stat_label = MARKET_LABELS.get(market_key, market_key)
        row["vortex_score"] = grade["score"]
        row["tier"]         = tier
        row["case_summary"] = _case_wnba(player, stat_label, line, side, grade,
                                         splits, matchup["opp_abbr"])
        row["risk_summary"] = _risk_wnba(grade)
        row["sportsbook"]   = BOOK_DISPLAY.get(row["best_book"], row["best_book"])
        row["stats_json"]   = json.dumps({
            "tier":        tier,
            "side":        side,
            "prop_type":   prop_type,
            "splits":      splits,
            "proj_edge":   grade.get("proj_edge"),
            "minutes_l10": grade.get("minutes_l10"),
            "stability":   grade.get("stability"),
            "risk_flags":  grade.get("risk_flags"),
            "opponent":    matchup["opp_abbr"],
            "is_home":     matchup.get("is_home"),
            "opp_def_rank": opp_def_rank,
            "def_n_teams":  n_teams,
            "opp_def_allowed": opp_def_allowed,
            "league_def_avg":  league_def_avg,
            "def_stat":     def_stat,
            "team_pace":    _pace(pinfo["team_id"]),
            "opp_pace":     _pace(matchup["opp_id"]),
            "league_pace":  league_pace,
            "game_flags":   game_flags,
            "teammate_out": teammate_out,
            "self_status":  self_status,
            "over_score":    both["over_score"],
            "under_score":   both["under_score"],
            "model_verdict": both["model_verdict"],
            "confidence":    both["confidence"],
            "true_prob":   row.get("true_prob"),
            "best_odds":   row.get("best_odds"),
        }, default=str)
        out.append(row)

    print(f"  WNBA stats filter: {passed} kept · {discarded} below-tier · "
          f"{no_game} dropped (no game / no match)")
    return out


def analyze_wnba_prop(player: str, line: float, side: str, prop_type: str) -> dict:
    """
    Grade ONE WNBA prop on demand (for /prediction and /analyze) with fresh data —
    mirrors enrich_wnba's per-row logic. Returns a row-like dict ready for
    build_wnba_detail_embed, or {"error": "..."} on any failure.

    No tier gate here: /prediction shows the real grade whatever it is (unlike the
    board, which only keeps Strong/Elite).
    """
    schedule    = stats_wnba.get_todays_schedule()
    opp_lookup  = stats_wnba.get_opponent_lookup(schedule)
    league_pace = stats_wnba.get_league_avg_pace()
    def_ranks   = stats_wnba.get_defense_ranks()
    spreads     = _wnba_spreads()
    blowout     = {ab for ab, s in spreads.items() if s >= _WNBA_BLOWOUT_SPREAD}

    pinfo = stats_wnba.get_player_id(player)
    if not pinfo:
        return {"error": f"Couldn't find WNBA player **{player}**."}

    matchup = opp_lookup.get(pinfo["abbr"])
    if not matchup:
        return {"error": f"**{player}** ({pinfo['abbr']}) has no upcoming WNBA game right now."}
    if matchup.get("state") == "post":
        return {"error": f"**{player}**'s game has already finished — no live play."}

    self_status  = stats_wnba.player_injury_status(pinfo["team_id"], player)
    if self_status == "out":
        return {"error": f"**{player}** is ruled OUT (injury) — skipping."}
    teammate_out = stats_wnba.key_teammate_out(pinfo["team_id"], player)

    splits = stats_wnba.get_historical_splits(pinfo["id"], line, prop_type)
    if not splits or not (splits.get("l10") or {}).get("games"):
        return {"error": f"No recent game-log data for **{player}**."}

    def_stat   = stats_wnba.DEF_STAT_FOR_PROP.get(prop_type)
    stat_ranks = def_ranks.get(def_stat, {}) if def_stat else {}
    opp_def_rank = stat_ranks.get(matchup["opp_abbr"])
    n_teams = len(stat_ranks) or 15
    # Concrete allowed-per-game number for this defense (and league avg)
    opp_def_allowed = (def_ranks.get("_allowed", {}).get(def_stat, {}) or {}).get(matchup["opp_abbr"]) if def_stat else None
    league_def_avg  = (def_ranks.get("_league_avg", {}) or {}).get(def_stat) if def_stat else None

    game_flags = []
    if stats_wnba.is_back_to_back(pinfo["team_id"], matchup.get("commence_time", "")):
        game_flags.append("b2b")
    if pinfo["abbr"] in blowout:
        game_flags.append("blowout_risk")

    team_pace = stats_wnba.get_team_pace(pinfo["team_id"])
    opp_pace  = stats_wnba.get_team_pace(matchup["opp_id"])

    _kw = dict(prop_type=prop_type,
               team_pace=team_pace, opp_pace=opp_pace, league_pace=league_pace,
               opp_def_rank=opp_def_rank, n_teams=n_teams,
               game_flags=game_flags, teammate_out=teammate_out, self_status=self_status)
    both  = grade_wnba.grade_pick_both(splits, line, **_kw)
    grade = both["under_grade"] if side == "under" else both["over_grade"]

    tier       = _WNBA_TIER.get(grade["label"], "LEAN")
    stat_label = stats_wnba.PROP_LABEL.get(prop_type, prop_type)
    return {
        "player_name":  player,
        "sport":        "WNBA",
        "stat_type":    stat_label,
        "line":         line,
        "vortex_score": grade["score"],
        "tier":         tier,
        "risk_summary": _risk_wnba(grade),
        "case_summary": _case_wnba(player, stat_label, line, side, grade,
                                   splits, matchup["opp_abbr"]),
        "stats_json":   json.dumps({
            "tier":        tier,
            "side":        side,
            "prop_type":   prop_type,
            "splits":      splits,
            "proj_edge":   grade.get("proj_edge"),
            "minutes_l10": grade.get("minutes_l10"),
            "stability":   grade.get("stability"),
            "risk_flags":  grade.get("risk_flags"),
            "opponent":    matchup["opp_abbr"],
            "is_home":     matchup.get("is_home"),
            "opp_def_rank": opp_def_rank,
            "def_n_teams":  n_teams,
            "opp_def_allowed": opp_def_allowed,
            "league_def_avg":  league_def_avg,
            "def_stat":     def_stat,
            "game_spread":  spreads.get(pinfo["abbr"]),
            "team_pace":    team_pace,
            "opp_pace":     opp_pace,
            "league_pace":  league_pace,
            "game_flags":   game_flags,
            "teammate_out": teammate_out,
            "self_status":  self_status,
            # Model verdict (both sides) — mirrors MLB
            "over_score":    both["over_score"],
            "under_score":   both["under_score"],
            "model_verdict": both["model_verdict"],
            "confidence":    both["confidence"],
        }, default=str),
    }


# ── Inclusion gate (EV floor + stats-tier bypass) ────────────────────────────

def _bvp_avg(bvp_data: dict) -> float:
    """Parse BvP avg string ('.333') → float (0.333). Returns 0.0 on bad data."""
    s = (bvp_data or {}).get("avg", ".000")
    try:
        return float("0" + s) if isinstance(s, str) and s.startswith(".") else float(s or 0)
    except (ValueError, TypeError):
        return 0.0


def _parse_ip_str(ip_str: str) -> float:
    """'6.2' → 6.667  (each partial-inning digit = 1/3 of an inning)."""
    try:
        parts = str(ip_str).split(".")
        full   = int(parts[0])
        thirds = int(parts[1]) / 3 if len(parts) > 1 and parts[1] else 0
        return full + thirds
    except (ValueError, IndexError):
        return 0.0


def _recent_k9(last_5_starts: list, n: int = 3) -> float | None:
    """K/9 from last N starts. Returns None if fewer than 2 usable starts."""
    sample   = (last_5_starts or [])[:n]
    total_ip = sum(_parse_ip_str(s.get("ip", "0.0")) for s in sample)
    total_k  = sum(int(s.get("k", 0)) for s in sample)
    if total_ip < 3:
        return None
    return round(total_k / total_ip * 9, 1)


def _should_include(ev: float, tier: str, splits: dict,
                    side: str = "over",
                    l10_bypass_override: int = None,
                    bvp_data: dict = None,
                    prop_type: str = None,
                    line: float = None) -> tuple[bool, str]:
    """
    Return (include, signal_type).

    Hit rate / tier is the primary gate — price is secondary.

    Path 1 : ELITE or STRONG stats tier                                    → STRONG_PLAY
    Path 2 : L10 hit-rate ≥ l10_bypass (default MIN_L10_BYPASS) → HOT_STREAK
    Path 3 : BvP edge — strong career history vs tonight's arm  → BVP_EDGE
    Path 4 : EV ≥ MIN_EV_PCT (loose fallback)                  → EV_EDGE
    Hard floor: ev < EV_BYPASS_FLOOR → always drop

    l10_bypass_override: optional per-caller threshold (e.g. 50 for pitcher K).
    bvp_data: career batter-vs-pitcher stats — allows a matchup override when
              recent L5/L10 form is misleading (recency-bias / L5 trap scenario).
    prop_type / line: used to enforce MIN_LINE thresholds (trivial under filter).
    """
    l10_bypass = l10_bypass_override if l10_bypass_override is not None else MIN_L10_BYPASS
    if ev < EV_BYPASS_FLOOR:
        return False, ""

    # ── Minimum line guard: drop trivially easy lines (e.g. U0.5 HRR, U0.5 TB)
    if prop_type and line is not None:
        floor = MIN_LINE.get(prop_type, {}).get(side)
        if floor is not None and line < floor:
            return False, ""

    # Anti-slump guard: if L10 hit rate < 40% on OVER, cap tier at LEAN regardless
    # of season stats or BvP. A cold streak this severe overrides historical baseline.
    l10_raw = ((splits or {}).get("l10") or {}).get("rate") or 50
    effective_l10 = (100 - l10_raw) if side == "under" else l10_raw
    if side == "over" and effective_l10 < 40 and tier in ("ELITE", "STRONG"):
        tier = "LEAN"

    # Coin-flip filter: drop 48-52% effective L10 unless ELITE tier overrides.
    # Near-50% hit rates are noise — no edge to justify a board slot.
    if 48 <= effective_l10 <= 52 and tier not in ("ELITE",):
        return False, ""

    # Under ELITE gate: inversion alone cannot produce ELITE.
    # Requires ≥80% effective L10 Under hit rate — strong, consistent Under edge.
    if side == "under" and tier == "STRONG" and effective_l10 >= 80:
        tier = "ELITE"

    if tier in ("ELITE", "STRONG"):
        return True, "STRONG_PLAY"
    l10 = (splits or {}).get("l10")
    if l10:
        raw_rate = l10.get("rate") or 0
        effective_rate = (100 - raw_rate) if side == "under" else raw_rate
        if effective_rate >= l10_bypass:
            return True, "HOT_STREAK"
    # BvP matchup override: meaningful sample + strong head-to-head history
    # beats a cold L5/L10 because the matchup is the most specific data we have.
    if bvp_data:
        ab  = bvp_data.get("ab", 0)
        avg = _bvp_avg(bvp_data)
        if side == "over"  and ab >= 8 and avg >= 0.300:
            return True, "BVP_EDGE"
        if side == "under" and ab >= 10 and avg <= 0.180:
            return True, "BVP_EDGE"
    if ev >= MIN_EV_PCT:
        return True, "EV_EDGE"
    return False, ""


# ── Market parser — evaluates BOTH Over and Under sides ───────────────────────

def parse_events(events_data: list, sport: str, market: str) -> list[dict]:
    """
    Build prop_map from raw bookmaker data, then emit one candidate row for
    EACH side (over / under) that clears the multi-book and juice guards.
    The EV floor is NOT applied here — it runs downstream after stats enrichment
    so the stats-tier bypass can override it.
    """
    prop_map: dict[str, dict[float, dict]] = {}
    link_map: dict[str, dict[float, dict]] = {}  # player → line → {over: {book: link}, under: {book: link}}
    # commence_time is carried per player+line directly from the source event so it
    # is never lost to a name-keyed re-lookup (which left finished games on the board).
    commence_map: dict[str, dict[float, str]] = {}  # player → line → commence_time ISO

    for event in events_data:
        ct = event.get("commence_time", "")
        for bm in event.get("bookmakers", []):
            book = bm.get("key", "").lower()
            for mkt in bm.get("markets", []):
                if mkt.get("key") != market:
                    continue
                for outcome in mkt.get("outcomes", []):
                    player = outcome.get("description") or outcome.get("name", "")
                    side   = outcome.get("name", "").lower()
                    pt, price = outcome.get("point"), outcome.get("price")
                    link   = outcome.get("link") or ""  # deep link to add to betslip
                    if not player or pt is None or price is None:
                        continue
                    if side not in ("over", "under"):
                        continue
                    prop_map.setdefault(player, {}).setdefault(
                        float(pt), {"over": {}, "under": {}})
                    prop_map[player][float(pt)][side][book] = int(price)
                    # Store deep links per book per side
                    if link:
                        link_map.setdefault(player, {}).setdefault(
                            float(pt), {"over": {}, "under": {}})
                        link_map[player][float(pt)][side][book] = link
                    # Bind this exact prop's start time to its source event.
                    if ct:
                        commence_map.setdefault(player, {})[float(pt)] = ct

    rows: list[dict] = []
    rej = {"single_book": 0, "juice_cap": 0, "no_pair": 0}

    base_label = MARKET_LABELS.get(market, market)

    for player, line_map in prop_map.items():
        for line, sides in line_map.items():
            over_map  = sides["over"]
            under_map = sides["under"]

            # Anchor priority — and we TRACK which one was used so the board can
            # report EV honestly. EV is only a real, measurable edge when there is
            # a genuine two-sided de-vig (sharp Pinnacle line, or soft-book O/U
            # consensus). A one-sided raw-implied estimate is NOT a measurable edge.
            #   1. Pinnacle de-vig  → anchor "sharp"     (EV real)
            #   2. Soft O/U consensus→ anchor "consensus" (EV real)
            #   3. Raw one-sided     → anchor "none"      (EV NOT measurable)
            true_prob_over = _sharp_no_vig_prob(over_map, under_map)
            sharp_anchored = true_prob_over is not None
            consensus_real = False
            if true_prob_over is None:
                cons = consensus_no_vig_prob(over_map, under_map)
                if cons is not None:
                    true_prob_over = cons
                    consensus_real = True
            if true_prob_over is None:
                # Raw one-sided implied prob — used only to position the prop; the
                # resulting EV is not a real market edge (no opposing price to de-vig).
                if over_map:
                    probs = [american_to_implied(p) for p in over_map.values()]
                    true_prob_over = sum(probs) / len(probs)
                elif under_map:
                    probs = [american_to_implied(p) for p in under_map.values()]
                    true_prob_over = 1.0 - sum(probs) / len(probs)
                else:
                    rej["no_pair"] += 1
                    continue

            anchor  = "sharp" if sharp_anchored else ("consensus" if consensus_real else "none")
            ev_real = anchor != "none"

            def _add_side(side_key: str, price_map: dict, true_prob: float):

                # Filter out line noise below minimum operational thresholds
                prop_type = MARKET_TO_PROP_TYPE.get(market) or NBA_MARKET_TO_PROP_TYPE.get(market)
                if prop_type in MIN_LINE:
                    min_allowed = MIN_LINE[prop_type].get(side_key)
                    if min_allowed is not None and line < min_allowed:
                        return
                # Pinnacle is a reference price only — never a book we bet on.
                # Strip it before choosing best_book / best_odds, but it has
                # already done its job anchoring true_prob above.
                bettable = {b: o for b, o in price_map.items() if b != SHARP_BOOK}
                if len(bettable) < MIN_BOOKS:
                    rej["single_book"] += 1
                    return
                # Prefer DK/Underdog/PrizePicks as best_book if they have the line;
                # fall back to best odds across all available bettable books.
                best_book = max(bettable, key=lambda b: american_to_decimal(bettable[b]))
                for pref in PREFERRED_BOOKS:
                    if pref in bettable:
                        best_book = pref
                        break
                best_odds = bettable[best_book]
                if best_odds < MAX_JUICE:
                    rej["juice_cap"] += 1
                    return
                # Honest EV: only report a measurable edge when there's a real
                # two-sided de-vig. Without one, the measured edge is none (0.0) —
                # we never fabricate a positive EV from a one-sided line.
                ev = compute_ev(true_prob, best_odds) if ev_real else 0.0
                # Collect deep links for this side
                side_links = link_map.get(player, {}).get(line, {}).get(side_key, {})
                # Prefer best_book link, fall back to any available
                export_link = side_links.get(best_book) or next(iter(side_links.values()), "")
                rows.append({
                    "player_name":   player,
                    "sport":         sport,
                    "stat_type":     base_label,
                    "market_key":    market,
                    "side":          side_key,
                    "line":          line,
                    "ev_percentage": ev,
                    "over_map":      over_map,
                    "under_map":     under_map,
                    "best_book":     best_book,
                    "best_odds":     best_odds,
                    "true_prob":     true_prob,
                    "anchor":        anchor,
                    "ev_real":       ev_real,
                    "n_books":       len(price_map),
                    "commence_time": commence_map.get(player, {}).get(line, ""),
                    "export_link":   export_link,
                    "all_links":     side_links,
                })

            _add_side("over",  over_map,  true_prob_over)
            _add_side("under", under_map, 1.0 - true_prob_over)

    if any(rej.values()):
        tot = sum(rej.values())
        print(f"  Market filter: {tot} removed — "
              f"{rej['single_book']} single-book · "
              f"{rej['juice_cap']} over-juiced · "
              f"{rej['no_pair']} no O/U pair")

    return rows

# ── Pitcher game lookup ──────────────────────────────────────────────────────

def build_team_game_lookup(schedule: dict) -> dict[int, dict]:
    """Map team_id → {is_home, home_team_name, away_team_name}"""
    lookup: dict[int, dict] = {}
    for game in schedule.values():
        h_id   = game.get("home_team_id")
        a_id   = game.get("away_team_id")
        h_name = game.get("home_team_name", "")
        a_name = game.get("away_team_name", "")
        if h_id:
            lookup[h_id] = {"is_home": True,  "home_team": h_name, "away_team": a_name}
        if a_id:
            lookup[a_id] = {"is_home": False, "home_team": h_name, "away_team": a_name}
    return lookup


def build_pitcher_game_lookup(schedule: dict) -> dict[str, dict]:
    """
    Map pitcher_name.lower() → {pitcher_id, team_id, team_name,
                                 opp_team_id, opp_team_name, is_home}
    Used to find a pitcher's opponent from today's schedule.
    """
    lookup: dict[str, dict] = {}
    for game in schedule.values():
        hp, hp_id = game.get("home_pitcher"), game.get("home_pitcher_id")
        ap, ap_id = game.get("away_pitcher"), game.get("away_pitcher_id")
        if hp:
            _hp_entry = {
                "pitcher_id":    hp_id,
                "team_id":       game.get("home_team_id"),
                "team_name":     game.get("home_team_name"),
                "opp_team_id":   game.get("away_team_id"),
                "opp_team_name": game.get("away_team_name"),
                "is_home":       True,
            }
            lookup[hp.lower()] = _hp_entry
            if hp_id:
                lookup[hp_id] = _hp_entry
        if ap:
            _ap_entry = {
                "pitcher_id":    ap_id,
                "team_id":       game.get("away_team_id"),
                "team_name":     game.get("away_team_name"),
                "opp_team_id":   game.get("home_team_id"),
                "opp_team_name": game.get("home_team_name"),
                "is_home":       False,
            }
            lookup[ap.lower()] = _ap_entry
            if ap_id:
                lookup[ap_id] = _ap_entry
    return lookup


def _case_from_pitcher_k(player: str, line: float,
                          over_map: dict, under_map: dict,
                          best_book: str, true_prob: float, ev: float,
                          card: dict, side: str, game_info: dict | None) -> str:
    is_over    = side == "over"
    side_label = "Over" if is_over else "Under"
    splits     = card.get("splits", {})
    l5         = splits.get("l5") or {}
    l10        = splits.get("l10") or {}
    l20        = splits.get("l20") or {}
    season_avg = splits.get("season_avg", 0) or 0
    opp_k      = card.get("opp_k", {})
    ss         = card.get("season_stats", {})

    def hit_rate(r):
        if not r: return 0
        raw = r.get("rate", 0)
        return (100 - raw) if not is_over else raw

    r5  = hit_rate(l5)
    r10 = hit_rate(l10)
    r20 = hit_rate(l20)
    g5  = l5.get("games", 5)
    g10 = l10.get("games", 10)
    g20 = l20.get("games", 0)
    h5  = l5.get("hits", 0) if is_over else (g5 - l5.get("hits", 0))
    h10 = l10.get("hits", 0) if is_over else (g10 - l10.get("hits", 0))
    h20 = l20.get("hits", 0) if is_over else (g20 - l20.get("hits", 0))
    avg10 = l10.get("avg", season_avg)

    # Pull projection signals stashed by _enrich_pitcher_k_row
    proj_ks      = card.get("_proj_ks")
    avg_ip       = card.get("_avg_ip")
    pip_cap      = card.get("_pip_cap")
    park_f       = card.get("_park_f", 1.0)
    opp_kpct_val = card.get("_opp_kpct")
    is_home_t    = card.get("_is_home")
    home_era_val = card.get("home_era")
    away_era_val = card.get("away_era")
    rec_k9       = card.get("_rec_k9")
    ssn_k9       = card.get("_ssn_k9") or 0
    ump_name     = card.get("_ump_name")
    ump_tier     = card.get("_ump_tier")
    opp_name     = (game_info or {}).get("opp_team_name", "")
    opp_rank     = opp_k.get("rank")
    opp_kpct_ov  = opp_k.get("k_pct")
    opp_k_hand   = card.get("opp_k_vs_hand") or {}
    opp_kpct_h   = opp_k_hand.get("k_pct")
    hand_label   = "LHP" if (card.get("hand") or "R") == "L" else "RHP"
    # Home/away venue K-split for the opposing lineup
    opp_bats_home = card.get("_opp_bats_home")
    opp_home_rank = card.get("_opp_home_rank"); opp_home_kpct = card.get("_opp_home_kpct")
    opp_away_rank = card.get("_opp_away_rank"); opp_away_kpct = card.get("_opp_away_kpct")

    parts = []

    # ── PILLAR 1: HISTORICAL CEILING ─────────────────────────────────────────
    parts.append("**📊 HISTORICAL CEILING**")

    threshold_phrase = (f"{int(line)+1}+ strikeouts" if is_over
                        else f"{int(line)} or fewer strikeouts")
    if g10 >= 5:
        parts.append(
            f"**{player}** has had {threshold_phrase} in "
            f"**{h10}/{g10} of his last 10 starts ({r10:.0f}%)**"
            + (f", and {h20}/{g20} over his last 20 ({r20:.0f}%)." if g20 >= 10 else ".")
        )
    if g5 >= 3:
        trend_arrow = "📈" if r5 > r10 + 10 else "📉" if r5 < r10 - 10 else "➡️"
        parts.append(f"{trend_arrow} L5: **{h5}/{g5} ({r5:.0f}%)** — "
                     + ("heating up." if r5 > r10 + 10
                        else "cooling off." if r5 < r10 - 10
                        else "consistent with L10."))

    if rec_k9 is not None and ssn_k9 > 0:
        delta = rec_k9 - ssn_k9
        if delta >= 1.5:
            kdir = "📈" if is_over else "⚠️"
            parts.append(f"{kdir} K/9 surging in L3 starts: **{rec_k9}** vs {ssn_k9:.1f} season avg — "
                         + ("stuff is sharper recently." if is_over else "headwind for the Under."))
        elif delta <= -1.5:
            kdir = "📉" if is_over else "✅"
            parts.append(f"{kdir} K/9 dipping in L3 starts: **{rec_k9}** vs {ssn_k9:.1f} season avg — "
                         + ("less swing-and-miss lately." if is_over else "supports the Under."))

    # ── PILLAR 2: SPLIT FACTOR ────────────────────────────────────────────────
    parts.append("\n**📍 SPLIT FACTOR**")

    if home_era_val and away_era_val:
        venue_label = "home" if is_home_t else "road"
        venue_era   = home_era_val if is_home_t else away_era_val
        other_era   = away_era_val if is_home_t else home_era_val
        split_icon  = "✅" if venue_era <= other_era else "⚠️"
        parts.append(
            f"{split_icon} **ERA split** — {venue_label.capitalize()} ERA: **{venue_era:.2f}** "
            f"vs {other_era:.2f} {'away' if is_home_t else 'home'}."
        )
        if venue_era > other_era * 1.3:
            parts.append(f"Significant {venue_label} ERA blowup → shorter outings, fewer K opportunities.")
        elif other_era > venue_era * 1.3:
            parts.append(f"Strong {venue_label} ERA advantage → pitches deeper, more K volume.")

    if pip_cap and home_era_val and away_era_val:
        venue_label = "home" if is_home_t else "road"
        parts.append(
            f"🛑 **IP ceiling: {pip_cap} projected innings** — "
            f"ERA disparity ({home_era_val:.2f} home / {away_era_val:.2f} away) "
            f"caps K volume for this {venue_label} start."
        )

    bf, gs_val = ss.get("batters_faced", 0), ss.get("games_started", 1)
    if bf and gs_val:
        avg_bf = round(bf / gs_val, 1)
        vol_icon = "✅" if avg_bf >= 25 else "⚠️" if avg_bf < 20 else "➡️"
        parts.append(f"{vol_icon} Volume: **{avg_bf} batters/start** — "
                     + ("deep outings create K opportunities." if avg_bf >= 25
                        else "shallow outings limit K ceiling." if avg_bf < 20
                        else "average workload."))

    if park_f >= 1.10:
        parts.append(f"🏟️ Hitter-friendly venue (park factor {park_f:.2f}) — "
                     f"pitchers pulled earlier, K volume compressed.")
    elif park_f <= 0.93:
        parts.append(f"🏟️ Pitcher-friendly park (factor {park_f:.2f}) — "
                     f"deeper outings are the norm here.")

    # Contact pitcher warning for high K lines
    if ssn_k9 > 0 and ssn_k9 < 7.0 and (avg_ip or 0) >= 6.0 and line >= 7.0:
        parts.append(f"⚡ **Contact pitcher profile** — {ssn_k9:.1f} K/9 with deep outings "
                     f"signals pitch-to-contact style. High-K lines ({int(line)}+) carry extra risk.")

    # ── PILLAR 3: MATCHUP DYNAMIC ─────────────────────────────────────────────
    parts.append("\n**⚡ MATCHUP DYNAMIC**")

    if opp_name and opp_rank and opp_kpct_ov:
        if opp_rank >= 23:
            matchup_icon = "🟢" if is_over else "🔴"
            parts.append(f"{matchup_icon} **{opp_name}** ranks #{opp_rank}/30 — "
                         f"strikeout-prone lineup ({opp_kpct_ov:.1f}% K rate). "
                         + ("Favorable for the Over." if is_over else "Headwind for the Under."))
        elif opp_rank <= 8:
            matchup_icon = "🔴" if is_over else "🟢"
            parts.append(f"{matchup_icon} **{opp_name}** ranks #{opp_rank}/30 — "
                         f"contact lineup ({opp_kpct_ov:.1f}% K rate). "
                         + ("Headwind for the Over." if is_over else "Favorable for the Under."))
        else:
            parts.append(f"➡️ **{opp_name}** is a neutral matchup (#{opp_rank}/30, "
                         f"{opp_kpct_ov:.1f}% K rate).")

    # ── Home/away venue K-split — uses tonight's venue, shows both for context ──
    if opp_home_rank and opp_away_rank and opp_home_kpct and opp_away_kpct:
        team_label = opp_name or "This lineup"
        venue_word = "at home" if opp_bats_home else "on the road"
        cur_rank   = opp_home_rank if opp_bats_home else opp_away_rank
        cur_kpct   = opp_home_kpct if opp_bats_home else opp_away_kpct
        # Lower rank # = harder to strike out at that venue
        tougher_at_home = opp_home_rank < opp_away_rank
        note = ("Tougher to strike out at home — using their home profile tonight."
                if (opp_bats_home and tougher_at_home) else
                "Easier to strike out on the road — using their road profile tonight."
                if ((not opp_bats_home) and not tougher_at_home) else
                f"Using their {venue_word.split()[-1]} profile tonight.")
        parts.append(
            f"📍 **Venue K-split** — {team_label} are batting **{venue_word}** "
            f"(**{cur_kpct:.1f}% K, #{cur_rank}/30**). "
            f"Home: {opp_home_kpct:.1f}% (#{opp_home_rank}) · "
            f"Road: {opp_away_kpct:.1f}% (#{opp_away_rank}). {note}"
        )

    if opp_kpct_h and opp_kpct_ov and abs(opp_kpct_h - opp_kpct_ov) >= 2.0:
        diff = opp_kpct_h - opp_kpct_ov
        hand_icon = ("🟢" if (diff > 0) == is_over else "🔴")
        parts.append(
            f"{hand_icon} **Handedness ({hand_label})** — {opp_name} K rate vs {hand_label}: "
            f"**{opp_kpct_h:.1f}%** ({diff:+.1f}% vs overall {opp_kpct_ov:.1f}%). "
            + ("Extra vulnerability against this arm type." if diff > 0
               else "Extra contact ability against this arm type.")
        )

    if ump_name and ump_tier:
        ump_icon = ("🟢" if (ump_tier == "HIGH") == is_over else "🔴")
        ump_desc = ("wide zone — boosts K totals" if ump_tier == "HIGH"
                    else "tight zone — suppresses Ks")
        parts.append(f"{ump_icon} **Umpire: {ump_name}** — {ump_desc}. "
                     + ("Favorable for Over." if (ump_tier == "HIGH") == is_over
                        else "Headwind."))

    if proj_ks is not None and opp_kpct_val and ssn_k9:
        ip_used   = pip_cap if pip_cap else avg_ip
        k_factor  = opp_kpct_val / 22.0
        proj_icon = "📈" if proj_ks >= line else "📉"
        lean_lbl  = "OVER" if proj_ks >= line else "UNDER"
        parts.append(
            f"{proj_icon} **Adjusted projection: {proj_ks} Ks** — "
            f"{ssn_k9:.1f} K/9 × {k_factor:.3f} K-factor × {ip_used:.1f} IP "
            f"→ model leans **{lean_lbl}** the {line} line."
        )

    # ── VERDICT ───────────────────────────────────────────────────────────────
    if proj_ks is not None:
        verdict_side = "OVER" if proj_ks >= line else "UNDER"
    else:
        verdict_side = side_label.upper() if r10 >= 50 else ("UNDER" if is_over else "OVER")

    ev_str = f"+{ev:.1f}%" if ev >= 0 else f"{ev:.1f}%"

    # Conflicted signal detection — matchup and form point in opposite directions
    matchup_favors_over  = opp_rank and opp_rank >= 23   # K-prone lineup → tailwind for Over
    matchup_favors_under = opp_rank and opp_rank <= 8    # Contact lineup → tailwind for Under

    over_vs_contact  = is_over and matchup_favors_under       # form says Over, lineup says Under
    under_vs_k_prone = (not is_over and matchup_favors_over   # form says Under, lineup says Over
                        and not pip_cap and line <= 6.5)

    if over_vs_contact:
        # Explain WHY we still lean Over despite the contact lineup
        form_reason = (
            f"{r10:.0f}% L10 hit rate"
            + (f" and a recent K/9 of {rec_k9}" if rec_k9 and rec_k9 >= ssn_k9 - 0.5 else
               f" — though K/9 has dipped to {rec_k9} recently (was {ssn_k9:.1f} season avg)" if rec_k9 else "")
        )
        proj_note = (f"Even adjusted for the contact lineup, model projects **{proj_ks} Ks** "
                     f"({ssn_k9:.1f} K/9 × {(opp_kpct_ov or 22)/22:.3f} K-factor × {avg_ip:.1f} IP) — "
                     f"still clears the {line} line." if proj_ks and proj_ks >= line
                     else f"Model projects only **{proj_ks} Ks** vs this lineup — does NOT clear the {line} line, "
                          f"lean is based on form only." if proj_ks
                     else f"Projection unavailable — lean driven by form only.")
        parts.append(
            f"\n**⚠️ CONFLICTED SIGNAL — {verdict_side} {line}**\n"
            f"**Why still {verdict_side}:** Historical form is strong ({form_reason}). "
            f"{proj_note}\n"
            f"**The headwind:** {opp_name or 'This lineup'} ranks #{opp_rank}/30 — one of the hardest "
            f"lineups to strikeout ({opp_kpct_ov:.1f}% K rate). This suppresses the ceiling.\n"
            f"📌 Reduce size vs a normal matchup. Form wins here — but confirm starter is going deep."
        )
    elif under_vs_k_prone:
        # Explain WHY we still lean Under despite the K-prone lineup
        form_reason = (
            f"{r10:.0f}% L10 hit rate for the Under"
            + (f", including {r5:.0f}% over the last 5 starts" if r5 else "")
        )
        k9_note = (f"K/9 has dropped to {rec_k9} in recent starts (was {ssn_k9:.1f} season avg) — "
                   f"less swing-and-miss stuff even vs K-prone lineups." if rec_k9 and rec_k9 < ssn_k9 - 1.0
                   else f"Season K/9 of {ssn_k9:.1f} with projected {proj_ks} Ks even vs this lineup — "
                        f"form suggests the rate is trending down." if proj_ks
                   else f"Form suggests this pitcher runs under this line regardless of lineup.")
        parts.append(
            f"\n**⚠️ CONFLICTED SIGNAL — {verdict_side} {line}**\n"
            f"**Why still {verdict_side}:** {form_reason}. {k9_note}\n"
            f"**The headwind:** {opp_name or 'This lineup'} ranks #{opp_rank}/30 in K rate "
            f"({opp_kpct_ov:.1f}%) — a strikeout-prone lineup that normally boosts K totals.\n"
            f"📌 Form overrides matchup here, but the conflicting signal reduces confidence. "
            f"Consider half-unit or pass if you need a clean read."
        )
    else:
        proj_str = f"{proj_ks} projected Ks" if proj_ks is not None else f"{season_avg:.1f} season avg"
        parts.append(
            f"\n**🎯 VERDICT: {verdict_side} {line}** — "
            f"{proj_str} · {r10:.0f}% L10 · {ev_str} EV edge."
        )

    return "\n".join(parts)


def _risk_from_pitcher_k(ev: float, n_books: int, line: float, best_odds: int,
                          card: dict, side: str) -> str:
    is_over = side == "over"
    splits  = card.get("splits", {})
    l5      = splits.get("l5") or {}
    l10     = splits.get("l10") or {}
    opp_k   = card.get("opp_k", {})

    def hit_rate(r):
        if not r: return None
        raw = r.get("rate", 0)
        return (100 - raw) if not is_over else raw

    r5  = hit_rate(l5)
    r10 = hit_rate(l10)

    risks = []

    # Cooling trend
    if r5 is not None and r10 is not None and r5 < r10 - 15:
        risks.append(
            f"⚠️ **Fading recently** — L10 at {r10:.0f}% but L5 dropped to {r5:.0f}%. "
            f"Form may be turning."
        )

    # Opponent contact
    opp_rank = opp_k.get("rank")
    opp_kpct = opp_k.get("k_pct")
    opp_name = opp_k.get("name", "Opposing lineup")
    if opp_rank and opp_kpct:
        if is_over and opp_rank <= 8:
            risks.append(
                f"⚠️ **{opp_name} makes more contact than average** "
                f"({opp_kpct:.1f}% K rate, #{opp_rank}/30 hardest to K) "
                f"— a meaningful headwind for the over."
            )
        elif not is_over and opp_rank >= 23:
            risks.append(
                f"⚠️ **{opp_name} strikes out a lot** "
                f"({opp_kpct:.1f}% K rate, #{opp_rank}/30) "
                f"— lineup is K-friendly, headwind for the under."
            )

    # Park factor risk (only flag when it contradicts the bet)
    park_f    = card.get("_park_f", 1.0)
    is_home_t = card.get("_is_home")
    if is_over and park_f >= 1.10 and is_home_t is False:
        opp_name_p = opp_k.get("name", "Opponent")
        risks.append(
            f"⚠️ **Hitter-friendly venue** (park factor {park_f:.2f}) at {opp_name_p} — "
            f"raises pitcher early exit risk, capping K volume for the Over."
        )
    elif not is_over and park_f <= 0.93:
        risks.append(
            f"⚠️ **Pitcher-friendly park** (factor {park_f:.2f}) — "
            f"pitchers tend to go deeper here, headwind for the Under."
        )

    juice = (
        "Standard pricing." if best_odds >= -150
        else f"Juiced to {fmt_odds(best_odds)} — edge is compressed."
    )

    if not risks:
        risks.append(
            f"✅ No major red flags identified. {juice} "
            f"Confirm starter is still pitching before locking in."
        )
    else:
        risks.append(juice)

    return "\n".join(risks)


def _enrich_pitcher_k_row(row: dict, pitcher_game_lookup: dict,
                           umpire_lookup: dict = None,
                           learned_weights: dict[str, float] | None = None) -> dict | None:
    """Enrich a pitcher_strikeouts row using pitching-specific stats."""
    player = row["player_name"]
    line   = row["line"]
    ev     = row["ev_percentage"]
    side   = row.get("side", "over")

    # Find game info by fuzzy matching pitcher name, then fall back to ID lookup
    game_info = None
    pname_low = player.lower()
    pitcher_pid = stats_mlb.get_player_id(player)
    for key, info in pitcher_game_lookup.items():
        if isinstance(key, str) and (pname_low in key or key in pname_low):
            game_info = info
            break
    if game_info is None and pitcher_pid:
        game_info = pitcher_game_lookup.get(pitcher_pid)

    opp_team_id = game_info["opp_team_id"] if game_info else None
    opp_team_name = game_info.get("opp_team_name", "") if game_info else ""

    side_label = "O" if side == "over" else "U"
    print(f"    K-stats: {player} ({side_label}{line}) vs team_id={opp_team_id}")
    card = stats_mlb.get_pitcher_k_card(player, line, opp_team_id)

    if "error" in card:
        print(f"    DROP  {player} K — {card['error']}")
        return None

    raw_tier = card.get("tier", "PASS")
    tier     = TIER_INVERT.get(raw_tier, raw_tier) if side == "under" else raw_tier
    splits   = card.get("splits", {})

    # DFS platform EV override
    DFS_BOOKS = {"underdogfantasy", "underdog", "prizepicks"}
    is_dfs    = row.get("best_book") in DFS_BOOKS or all(
        b in DFS_BOOKS
        for b in list(row.get("over_map", {}).keys()) + list(row.get("under_map", {}).keys())
        if b
    )
    # L10 projection is a MODEL estimate, not a market edge. Only use it when
    # there is no real two-sided market to trust — otherwise the market price
    # wins. Never overwrite a real market EV with the rosier L10 number, and
    # always tag a projection as such so it is never mistaken for market edge.
    if is_dfs and not row.get("ev_real", False):
        l10 = (splits or {}).get("l10") or {}
        if l10 and l10.get("games", 0) >= 5:
            raw_rate = l10.get("rate", 50)
            hit_rate = (100 - raw_rate) / 100 if side == "under" else raw_rate / 100
            ev = compute_ev(hit_rate, row["best_odds"])
            row["ev_percentage"] = ev
            row["anchor"] = "projection"

    # ── Recent K/9 from last 3 starts ───────────────────────────────────────
    last_5   = card.get("last_5_starts") or []
    rec_k9   = _recent_k9(last_5, n=3)
    try:
        ssn_k9 = float((card.get("season_stats") or {}).get("k_per_9") or
                       card.get("k_per_9") or 0)
    except (ValueError, TypeError):
        ssn_k9 = 0.0

    # ── Umpire lookup ────────────────────────────────────────────────────────
    ump_name = None
    ump_tier = None
    if umpire_lookup and game_info:
        team_id  = game_info.get("team_id")
        ump_name = (umpire_lookup or {}).get(team_id)
        if ump_name:
            ump_tier = stats_mlb.UMPIRE_K_TIER.get(ump_name)
            print(f"    [umpire] {ump_name} → {ump_tier or 'NEUTRAL'}")

    # ── Average IP from last 3 starts ───────────────────────────────────────
    ip_vals = [_parse_ip_str(s.get("ip", "0.0")) for s in last_5[:3]]
    avg_ip  = round(sum(ip_vals) / len(ip_vals), 1) if ip_vals else None

    # ── K-factor projection: prefer handedness-specific K rate ───────────────
    LEAGUE_K_PCT = 22.0
    opp_kpct_hand    = (card.get("opp_k_vs_hand") or {}).get("k_pct")
    opp_kpct_overall = (card.get("opp_k") or {}).get("k_pct")
    # Use handedness-specific rate when available (more precise signal)
    opp_kpct = opp_kpct_hand or opp_kpct_overall

    # ── Home/away venue K-split ──────────────────────────────────────────────
    # The opposing lineup bats at home when the pitcher's team is away. Some
    # lineups whiff far less at home (Coors is the classic example), so blend
    # the venue-specific K rate into the season rate before projecting.
    opp_is_home = (game_info or {}).get("is_home") is False
    _home_tbl = stats_mlb.get_all_teams_k_rate_home_away(True)  if opp_team_id else {}
    _away_tbl = stats_mlb.get_all_teams_k_rate_home_away(False) if opp_team_id else {}
    _opp_home = _home_tbl.get(opp_team_id, {})
    _opp_away = _away_tbl.get(opp_team_id, {})
    # tonight's venue for the opposing lineup
    opp_venue      = _opp_home if opp_is_home else _opp_away
    opp_kpct_venue = opp_venue.get("k_pct")
    opp_venue_rank = opp_venue.get("rank")     # venue-aware rank (1 = hardest to K here)
    if opp_kpct and opp_kpct_venue:
        # 55% venue / 45% season — venue samples are smaller, so don't fully trust them
        opp_kpct = round(0.55 * opp_kpct_venue + 0.45 * opp_kpct, 1)
    elif opp_kpct_venue:
        opp_kpct = opp_kpct_venue

    proj_ks  = None
    if ssn_k9 and avg_ip and opp_kpct:
        k_factor = opp_kpct / LEAGUE_K_PCT
        proj_ks  = round(ssn_k9 * k_factor / 9 * avg_ip, 1)

    # ── Venue ERA cap: if ERA at tonight's venue >> other venue, early exit ──
    is_home_tonight = (game_info or {}).get("is_home")
    home_era_val = card.get("home_era")
    away_era_val = card.get("away_era")
    pip_cap = None
    if home_era_val and away_era_val and home_era_val > 0 and away_era_val > 0:
        venue_era  = home_era_val if is_home_tonight else away_era_val
        other_era  = away_era_val if is_home_tonight else home_era_val
        if venue_era > other_era * 1.5:
            era_ratio    = venue_era / other_era
            ip_reduction = 1.5 if era_ratio >= 3.0 else 1.0 if era_ratio >= 2.0 else 0.5
            pip_cap = round(max(3.0, (avg_ip or 5.5) - ip_reduction), 1)
            if ssn_k9 and opp_kpct:
                proj_ks = round(ssn_k9 * (opp_kpct / LEAGUE_K_PCT) / 9 * pip_cap, 1)

    # ── Park factor ──────────────────────────────────────────────────────────
    opp_team_name = (game_info or {}).get("opp_team_name", "")
    park_f = stats_mlb.PARK_FACTOR.get(opp_team_name, 1.0)

    # Stash all signals in the card so case/risk builders can access them
    card["_rec_k9"]      = rec_k9
    card["_ssn_k9"]      = ssn_k9
    card["_ump_name"]    = ump_name
    card["_ump_tier"]    = ump_tier
    card["_avg_ip"]      = avg_ip
    card["_proj_ks"]     = proj_ks
    card["_pip_cap"]     = pip_cap
    card["_park_f"]      = park_f
    card["_opp_kpct"]    = opp_kpct
    card["_opp_kpct_venue"] = opp_kpct_venue
    card["_opp_venue_rank"] = opp_venue_rank
    card["_opp_bats_home"]  = opp_is_home
    card["_opp_home_rank"]  = _opp_home.get("rank")
    card["_opp_home_kpct"]  = _opp_home.get("k_pct")
    card["_opp_away_rank"]  = _opp_away.get("rank")
    card["_opp_away_kpct"]  = _opp_away.get("k_pct")
    card["_is_home"]     = is_home_tonight

    # Pitcher K gets a relaxed HOT_STREAK threshold (50% vs 60% for batters)
    # because starters have inherently higher game-to-game K variance.
    include, signal_type = _should_include(ev, tier, splits, side,
                                           l10_bypass_override=50)
    if not include:
        print(f"    PASS  {player} K {side_label}{line} — tier={tier} ev={ev:+.1f}%")
        return None

    # Prefer the venue-aware rank (home/away) over the season rank.
    _opp_k_rank = opp_venue_rank or (card.get("opp_k") or {}).get("rank")
    _k_grade = vortex_analyze.grade_pick(
        splits=splits,
        line=float(line),
        side=side,
        opp_k_rank=_opp_k_rank,
        opp_k_pct=(opp_kpct / 100) if opp_kpct else None,
        prop_type="strikeouts",
        learned_weight=_compute_learned_multiplier("strikeouts", side, learned_weights or {}),
    )
    if _k_grade["score"] < 6:
        print(f"    DROP  {player} K {side_label}{line} — grade={_k_grade['score']} ({_k_grade['label']}) — below Strong threshold")
        return None
    row["vortex_score"]  = _k_grade["score"]
    # Tier must match what grade_pick computed — not the raw compute_score tier.
    # This ensures /elite only surfaces K props that grade_pick actually calls Elite.
    _GRADE_TIER = {"Elite": "ELITE", "Strong": "STRONG", "Good": "GOOD",
                   "Lean": "LEAN", "Risky": "RISKY", "Fade": "FADE"}
    tier = _GRADE_TIER.get(_k_grade.get("label", ""), tier)
    row["case_summary"] = _case_from_pitcher_k(
        player, line,
        row["over_map"], row["under_map"],
        row["best_book"], row["true_prob"],
        ev, card, side, game_info)
    row["risk_summary"] = _risk_from_pitcher_k(
        ev, row["n_books"], line, row["best_odds"], card, side)
    row["sportsbook"]   = BOOK_DISPLAY.get(row["best_book"], row["best_book"])
    row["stats_json"]   = json.dumps({
        "player_id":    pitcher_pid,
        "tier":         tier,
        "signal_type":  signal_type,
        "side":         side,
        "splits":       splits,
        "opp_k":        card.get("opp_k"),
        "trend_signal": card.get("trend_signal"),
        "season_stats": card.get("season_stats"),
        "last_5_starts": card.get("last_5_starts"),
        "recent_k9":    rec_k9,
        "ump_name":     ump_name,
        "ump_tier":     ump_tier,
        "proj_ks":       card.get("_proj_ks"),
        "avg_ip":        card.get("_avg_ip"),
        "pip_cap":       card.get("_pip_cap"),
        "park_f":        card.get("_park_f"),
        "opp_kpct":      card.get("_opp_kpct"),
        "opp_k_vs_hand": card.get("opp_k_vs_hand"),
        "home_era":      card.get("home_era"),
        "away_era":      card.get("away_era"),
        "is_pitcher":   True,
        "is_home":      game_info.get("is_home") if game_info else None,
        "opponent":     opp_team_name,
        "true_prob":    row.get("true_prob"),
        "best_odds":    row.get("best_odds"),
        "export_link":  row.get("export_link", ""),
        "all_links":    row.get("all_links", {}),
    }, default=str)
    row["tier"] = tier
    print(f"    KEEP  {player} K {side_label}{line} — tier={tier} ev={ev:+.1f}%")
    return row


# ── Pitcher props (outs / hits_allowed / earned_runs) ────────────────────────

_PITCHER_PROP_CONFIG = {
    "pitcher_outs": {
        "label":      "Outs",
        "season_key": "innings_pitched",
        "recent_key": "outs",
    },
    "pitcher_hits_allowed": {
        "label":      "Hits Allowed",
        "season_key": "hits_per_9",
        "recent_key": "hits",
    },
    "pitcher_earned_runs": {
        "label":      "Earned Runs",
        "season_key": "era",
        "recent_key": "er",
    },
}


def _ip_to_dec(ip_str: str) -> float:
    """Convert '6.0' → 6.0, '5.1' → 5.33, '6.2' → 6.67."""
    try:
        parts = str(ip_str).split(".")
        return int(parts[0]) + (int(parts[1]) / 3) if len(parts) == 2 else float(ip_str)
    except (ValueError, IndexError):
        return 0.0


def _compute_pitcher_hit_rate(last_starts: list[dict], line: float, side: str, stat_key: str) -> float | None:
    """Compute hit rate from last N starts for a given pitcher stat."""
    hits = 0
    total = 0
    for s in last_starts:
        val = s.get(stat_key)
        if val is None:
            continue
        try:
            v = float(val)
        except (ValueError, TypeError):
            continue
        total += 1
        if side == "over" and v > line:
            hits += 1
        elif side == "under" and v < line:
            hits += 1
    if total < 3:
        return None
    return round(hits / total * 100, 1)


def _enrich_pitcher_stat_row(row: dict, pitcher_game_lookup: dict,
                             market_key: str,
                             learned_weights: dict[str, float] | None = None) -> dict | None:
    """
    Enrich pitcher props (outs, hits_allowed, earned_runs) using a 7-component
    scoring engine:
      1) Projection Delta (anchor)
      2) Opponent Contact Profile (team K%)
      3) Pitch-Mix Fit proxy (K/9 + BB/9)
      4) TTO / Efficiency
      5) Bullpen Leash (IP/start + bullpen fatigue)
      6) Recent Form Stability (stdev of last 5 starts)
      7) Run Environment (park factor)
    """
    player = row["player_name"]
    line   = row["line"]
    ev     = row["ev_percentage"]
    side   = row.get("side", "over")
    side_label = "O" if side == "over" else "U"
    prop_label = _PITCHER_PROP_CONFIG.get(market_key, {}).get("label", market_key)

    # ── pitcher metrics ────────────────────────────────────────────────────────
    pm = stats_mlb.get_pitcher_metrics(player)
    if "error" in pm:
        print(f"    DROP  {player} {prop_label} — {pm['error']}")
        return None

    last_5 = pm.get("last_5_starts") or []

    # ── hit rates from game logs ───────────────────────────────────────────────
    rec_key = _PITCHER_PROP_CONFIG[market_key]["recent_key"]
    l5_rate  = _compute_pitcher_hit_rate(last_5[:5], line, side, rec_key)
    l10_rate = _compute_pitcher_hit_rate(last_5[:10], line, side, rec_key)

    splits = {
        "l5":  {"rate": l5_rate,  "games": min(len(last_5), 5)},
        "l10": {"rate": l10_rate, "games": min(len(last_5), 10)},
    }

    # ── season avg projection ──────────────────────────────────────────────────
    try:
        era_f  = float(pm.get("era", 4.5))
        ip_f   = _ip_to_dec(pm.get("innings_pitched", "0.0"))
        h9_f   = float(pm.get("hits_per_9", 9.0))
    except (ValueError, TypeError):
        era_f, ip_f, h9_f = 4.5, 5.0, 9.0

    ip_per_start = round(ip_f / max(pm.get("games_started", 10), 1), 1)
    if market_key == "pitcher_outs":
        season_avg = round(ip_per_start * 3, 1)
    elif market_key == "pitcher_hits_allowed":
        season_avg = round(h9_f * ip_per_start / 9, 1)
    elif market_key == "pitcher_earned_runs":
        season_avg = round(era_f * ip_per_start / 9, 1)
    else:
        season_avg = 0.0
    splits["season_avg"] = season_avg

    # ── resolve opponent team from pitcher_game_lookup ─────────────────────────
    game_info = None
    pname_low = player.lower()
    for key, info in pitcher_game_lookup.items():
        if isinstance(key, str) and (pname_low in key or key in pname_low):
            game_info = info
            break
    if game_info is None:
        pid = stats_mlb.get_player_id(player)
        if pid:
            game_info = pitcher_game_lookup.get(pid)

    opp_team_id   = game_info.get("opp_team_id")      if game_info else None
    opp_team_name = game_info.get("opp_team_name", "") if game_info else ""

    # ── Opponent contact profile ───────────────────────────────────────────────
    opp_stats = {}
    opp_k_rate = 22.0
    if opp_team_id:
        opp_stats = stats_mlb.get_team_opponent_stats(opp_team_id)
        opp_k_rate = opp_stats.get("k_rate", 22.0)

    # ── Park factor ────────────────────────────────────────────────────────────
    park_info = {}
    if opp_team_name:
        park_info = mlb_enrich.get_park_factor(opp_team_name)

    # ═══════════════════════════════════════════════════════════════════════════
    #  7-COMPONENT SCORING
    # ═══════════════════════════════════════════════════════════════════════════
    comp = {}

    # 1. Projection Delta (anchor)
    delta = season_avg - line if side == "over" else line - season_avg
    if delta >= 0:
        comp["proj_delta"] = 2
    elif delta >= -0.5:
        comp["proj_delta"] = 1
    else:
        comp["proj_delta"] = 0

    # 2. Opponent Contact Profile — team K% as primary signal
    if opp_k_rate >= 24:
        comp["opp_matchup"] = 2
    elif opp_k_rate >= 22:
        comp["opp_matchup"] = 1
    elif opp_k_rate <= 18:
        comp["opp_matchup"] = -2
    elif opp_k_rate <= 20:
        comp["opp_matchup"] = -1
    else:
        comp["opp_matchup"] = 0

    # 3. Pitch-Mix Fit proxy — K/9 + BB/9
    try:
        k9   = float(pm.get("k_per_9", 8.0) or 8.0)
        bb9  = float(pm.get("bb_per_9", 3.0) or 3.0)
    except (ValueError, TypeError):
        k9, bb9 = 8.0, 3.0
    comp["pitch_mix"] = 1 if (k9 > 9.0 and bb9 < 3.0) else 0

    # 4. TTO / Efficiency
    comp["tto"] = 0
    if ip_per_start >= 5.5 and k9 > 9.0:
        comp["tto"] = 1
    elif ip_per_start >= 6.0:
        comp["tto"] = 1

    # 5. Bullpen Leash
    avg_ip_l3 = float(pm.get("avg_ip_l3", 5.0) or 5.0)
    leash = 0.0
    if avg_ip_l3 >= 6.0:
        leash += 1.0
    elif avg_ip_l3 >= 5.5:
        leash += 0.5
    if opp_team_id:
        bp = stats_mlb.get_bullpen_stats(opp_team_id)
        if bp.get("fatigued_count", 0) >= 3:
            leash += 0.5
    comp["leash"] = leash

    # 6. Recent Form Stability — stdev of last 5 starts
    stat_vals = []
    for s in last_5[:5]:
        v = s.get(rec_key)
        if v is not None:
            try:
                stat_vals.append(float(v))
            except (ValueError, TypeError):
                pass
    if len(stat_vals) >= 3:
        mean_v  = sum(stat_vals) / len(stat_vals)
        var     = sum((x - mean_v)**2 for x in stat_vals) / len(stat_vals)
        stdev   = var ** 0.5
        if stdev < 0.5 * max(season_avg, 0.1):
            comp["form"] = 2
        elif stdev < max(season_avg, 0.1):
            comp["form"] = 1
        else:
            comp["form"] = 0
    else:
        comp["form"] = 0

    # 7. Run Environment — park factor
    park_factor = park_info.get("factor", 1.0)
    if park_factor < 0.95:
        comp["park"] = 1
    elif park_factor > 1.05:
        comp["park"] = -1
    else:
        comp["park"] = 0

    # ── aggregate ──────────────────────────────────────────────────────────────
    total = sum(v for v in comp.values())
    score = min(max(round(total), 0), 10)

    # ── Apply learned weight modifier ───────────────────────────────────────
    prop_key = market_key.replace("pitcher_", "")
    lw = _compute_learned_multiplier(prop_key, side, learned_weights or {})
    if lw is not None and lw != 1.0:
        score = min(max(round(score * lw), 0), 10)

    # ── Tier from composite score ──────────────────────────────────────────────
    if score >= 8:
        raw_tier = "ELITE"
    elif score >= 5:
        raw_tier = "STRONG"
    elif score >= 3:
        raw_tier = "GOOD"
    elif score >= 1:
        raw_tier = "LEAN"
    else:
        raw_tier = "PASS"

    tier = TIER_INVERT.get(raw_tier, raw_tier) if side == "under" else raw_tier

    include, signal_type = _should_include(ev, tier, splits, side)
    if not include:
        print(f"    PASS  {player} {prop_label} {side_label}{line} — tier={tier} ev={ev:+.1f}% score={score}")
        return None

    # ── enrich row ─────────────────────────────────────────────────────────────
    delta_str = f"Δ{delta:+.1f}"
    row["vortex_score"]  = score
    row["case_summary"]  = (
        f"{prop_label}: {delta_str} · K% opp {opp_k_rate:.1f}% · "
        f"{'park +' if comp['park'] > 0 else 'park ' if comp['park'] < 0 else 'park ~'}{park_factor:.2f}"
    )
    row["risk_summary"]  = f"Score {score}/10 · L10 {l10_rate or 50:.0f}% · {ip_per_start} IP/start"
    row["sportsbook"]    = BOOK_DISPLAY.get(row["best_book"], row["best_book"])
    row["stats_json"]    = json.dumps({
        "player_id":      pid,
        "tier":            tier,
        "signal_type":     signal_type,
        "side":            side,
        "splits":          splits,
        "pitcher":         pm,
        "season_avg":      season_avg,
        "is_pitcher":      True,
        "opponent":        opp_team_name,
        "score_breakdown": comp,
        "opp_stats":       opp_stats,
        "park":            park_info,
        "true_prob":       row.get("true_prob"),
        "best_odds":       row.get("best_odds"),
        "export_link":     row.get("export_link", ""),
        "all_links":       row.get("all_links", {}),
    }, default=str)
    row["tier"] = tier
    print(f"    KEEP  {player} {prop_label} {side_label}{line} — tier={tier} ev={ev:+.1f}% score={score} delta={delta:+.1f}")
    return row


# ── MLB stats enrichment ──────────────────────────────────────────────────────

def enrich_mlb(rows: list[dict], pitcher_lookup: dict[int, str],
               pitcher_game_lookup: dict[str, dict] = None,
               team_game_lookup: dict[int, dict] = None,
               umpire_lookup: dict[int, str] = None,
               learned_weights: dict[str, float] | None = None) -> list[dict]:
    """
    For each MLB row:
      1. Resolve batter's current team → find opposing pitcher.
      2. Call stats_mlb.get_full_card().
      3. Apply _should_include gate (EV floor + stats-tier bypass).
      4. Enrich summaries with statistical payload.
    """
    enriched = []
    passed         = 0
    discarded_pass = 0
    no_pitcher     = 0
    below_floor    = 0
    _card_cache: dict[tuple, dict] = {}  # (player, line, prop_type) → card

    for row in rows:
        player     = row["player_name"]
        line       = row["line"]
        ev         = row["ev_percentage"]
        side       = row.get("side", "over")
        market_key = row["market_key"]
        prop_type  = MARKET_TO_PROP_TYPE.get(market_key, "hits")

        # ── skip disabled prop types ─────────────────────────────────────────
        if prop_type in SKIP_PROPS:
            continue

        # ── pitcher props (strikeouts / outs / hits_allowed / earned_runs) ────
        if market_key in ("pitcher_strikeouts", "pitcher_outs", "pitcher_hits_allowed", "pitcher_earned_runs"):
            if market_key == "pitcher_strikeouts":
                result = _enrich_pitcher_k_row(row, pitcher_game_lookup or {}, umpire_lookup or {},
                                                  learned_weights=learned_weights)
            else:
                result = _enrich_pitcher_stat_row(row, pitcher_game_lookup or {}, market_key,
                                                   learned_weights=learned_weights)
            if result:
                passed += 1
                enriched.append(result)
            else:
                discarded_pass += 1
            continue

        # ── DFS platform flag ───────────────────────────────────────────────
        # Underdog/PrizePicks price all props at fixed -137/-137 regardless of
        # true probability. Market-implied EV is always -13.5% — meaningless.
        # We flag these rows so stats-based EV override can run after splits load.
        DFS_BOOKS = {"underdogfantasy", "underdog", "prizepicks"}
        is_dfs = row.get("best_book") in DFS_BOOKS or all(
            b in DFS_BOOKS
            for b in list(row.get("over_map", {}).keys()) + list(row.get("under_map", {}).keys())
            if b
        )
        # For DFS props, bypass the EV floor here — EV will be recalculated from
        # stats hit rate after splits are loaded.
        if is_dfs and ev < MIN_EV_PCT:
            ev = 0.0  # placeholder — real value set after stats

        # ── find pitcher ────────────────────────────────────────────────────
        batter_id    = stats_mlb.get_player_id(player)
        pitcher_name = None
        team_id      = None
        if batter_id:
            team_id = stats_mlb.get_player_current_team(batter_id)
            if team_id:
                pitcher_name = pitcher_lookup.get(team_id)

        team_info   = (team_game_lookup or {}).get(team_id) if team_id else None
        is_home     = team_info.get("is_home") if team_info else None
        opp_team_id = team_info.get("opp_team_id") if team_info else None
        opp_team_name = team_info.get("opp_team_name", "") if team_info else ""

        if not pitcher_name and not is_dfs:
            # Non-DFS, no pitcher match — apply plain EV floor, no bypass
            if ev < MIN_EV_PCT:
                below_floor += 1
                continue
            no_pitcher += 1
            row["vortex_score"] = compute_score(ev, row["n_books"], line, row["best_odds"])
            row["case_summary"] = _case_odds_only(
                player, row["stat_type"], line,
                row["over_map"], row["under_map"],
                row["best_book"], row["true_prob"],
                ev, row["sport"], side)
            row["risk_summary"] = _risk_odds_only(ev, row["n_books"], line, row["best_odds"])
            row["sportsbook"]   = BOOK_DISPLAY.get(row["best_book"], row["best_book"])
            row["stats_json"]   = json.dumps({
                "player_id": batter_id, "side": side, "is_home": is_home,
                "opponent": opp_team_name,
                "true_prob": row.get("true_prob"), "best_odds": row.get("best_odds"),
            }, default=str)
            row["tier"]         = None
            enriched.append(row)
            continue
        # For DFS props without a pitcher match, fall through to stats card
        # using pitcher_name=None (get_full_card handles this gracefully)

        # ── stats card — cached per (player, line, prop_type, side)
        # side is included so Over/Under of same prop get independent tier scores
        side_label = "O" if side == "over" else "U"
        _ck = (player, line, prop_type, side)
        if _ck not in _card_cache:
            print(f"    Stats: {player} vs {pitcher_name} ({prop_type} {side_label}{line})")
            _card_cache[_ck] = stats_mlb.get_full_card(player, pitcher_name, line, prop_type, side,
                                                       opp_team_id=opp_team_id)
        card = _card_cache[_ck]

        if "error" in card:
            print(f"    [warn] {card['error']} — odds-only fallback")
            if ev < MIN_EV_PCT:
                below_floor += 1
                continue
            row["vortex_score"] = compute_score(ev, row["n_books"], line, row["best_odds"])
            row["case_summary"] = _case_odds_only(
                player, row["stat_type"], line,
                row["over_map"], row["under_map"],
                row["best_book"], row["true_prob"],
                ev, row["sport"], side)
            row["risk_summary"] = _risk_odds_only(ev, row["n_books"], line, row["best_odds"])
            row["sportsbook"]   = BOOK_DISPLAY.get(row["best_book"], row["best_book"])
            row["stats_json"]   = json.dumps({
                "player_id": batter_id, "side": side, "is_home": is_home,
                "opponent": opp_team_name,
                "true_prob": row.get("true_prob"), "best_odds": row.get("best_odds"),
            }, default=str)
            row["tier"]         = None
            no_pitcher += 1
            enriched.append(row)
            continue

        # tier is already side-correct — _confidence_tier() was called with side
        tier = card.get("tier", "PASS")

        splits = card.get("splits", {})

        # ── L10 projection for DFS props WITHOUT a real market ───────────────
        # This is a MODEL estimate (recent hit rate vs payout), not a market
        # edge. Only use it when there's no real two-sided de-vig to trust; when
        # a real market exists we keep the honest market EV. Tagged "projection"
        # so it is never presented as a market edge.
        if is_dfs and not row.get("ev_real", False):
            l10 = (splits or {}).get("l10") or {}
            if l10 and l10.get("games", 0) >= 5:
                raw_rate = l10.get("rate", 50)
                hit_rate = (100 - raw_rate) / 100 if side == "under" else raw_rate / 100
                ev = compute_ev(hit_rate, row["best_odds"])
                row["ev_percentage"] = ev
                row["anchor"] = "projection"

        # ── home/away tier suppress ──────────────────────────────────────────
        # Road batters with a poor away average get one tier level of caution.
        # Needs ≥4 away games in the sample to be meaningful.
        ha = card.get("home_away") or {}
        if (is_home is False and side == "over"
                and ha.get("away_games", 0) >= 4
                and ha.get("away_avg") is not None
                and ha["away_avg"] < float(line) * 0.80):
            _suppress = {"ELITE": "STRONG", "STRONG": "LEAN", "LEAN": "PASS"}
            if tier in _suppress:
                tier = _suppress[tier]
                print(f"    [road-split] {player} away_avg={ha['away_avg']} < {float(line)*0.80:.2f} → tier→{tier}")

        # ── barrel% power boost ──────────────────────────────────────────────
        # Elite barrel rate vs HR-prone pitcher = structural power edge.
        _POWER_KW = ("home run", "total base", "hits+run", "rbi")
        stat_lower = (row.get("stat_type") or "").lower()
        sc = card.get("statcast") or {}
        barrel_pct = sc.get("barrel_pct") or 0
        try:
            hr9_val = float((card.get("pitcher") or {}).get("hr_per_9") or 0)
        except (ValueError, TypeError):
            hr9_val = 0.0
        is_power = side == "over" and any(kw in stat_lower for kw in _POWER_KW)
        if is_power and barrel_pct >= 10 and hr9_val >= 1.0 and tier in ("LEAN", "PASS"):
            tier = "STRONG" if tier == "LEAN" else "LEAN"
            print(f"    [barrel] {player} {barrel_pct:.0f}% Brl vs {hr9_val:.2f} HR/9 → tier→{tier}")

        bvp_data = card.get("bvp") or {}
        include, signal_type = _should_include(
            ev, tier, splits, side,
            bvp_data=bvp_data,
            prop_type=prop_type,
            line=line,
        )
        if not include:
            discarded_pass += 1
            print(f"    PASS  {player} {side_label}{line} — tier={tier} ev={ev:+.1f}%")
            continue

        # ── enrichment (park / weather / pitch BvP / platoon / OAA) ────────
        pitcher_data = card.get("pitcher", {})
        pitcher_hand = pitcher_data.get("hand", "R")
        pitcher_id   = pitcher_data.get("pitcher_id")
        batter_id_e  = card.get("batter_id") or batter_id
        game_info    = row.get("game_info", {})
        home_team    = game_info.get("home_team", "")
        away_team    = game_info.get("away_team", "")
        batter_team  = game_info.get("batter_team", "")

        enrich = {}
        if batter_id_e and pitcher_id and home_team:
            try:
                enrich = mlb_enrich.enrich_mlb_card(
                    batter_id_e, pitcher_id, home_team, away_team,
                    batter_team, pitcher_hand,
                )
            except Exception as e:
                print(f"    [warn] enrichment failed: {e}")

        # ── New: lineup position, team environment, bullpen ──────────────────
        lineup_pos   = None
        team_hitting = {}
        bullpen      = {}
        opp_team_id  = None

        scratch_detected = False
        lineup_confirmed = False
        if batter_id_e:
            try:
                lineup_pos = stats_mlb.get_lineup_position(batter_id_e)
            except Exception:
                lineup_pos = None
            if team_id:
                try:
                    lineup_ids = stats_mlb.get_game_lineup_ids(team_id)
                    if lineup_ids:                       # lineup IS posted for this game
                        lineup_confirmed = True
                        if batter_id_e not in lineup_ids:
                            scratch_detected = True       # posted but he's not in it → scratched
                except Exception:
                    pass
        if scratch_detected:
            print(f"    [scratch] {player} confirmed out of lineup — skipping")
            continue
        # Soft gate: confirmed lineups are locked; unconfirmed are projected.
        if lineup_confirmed and isinstance(lineup_pos, int) and lineup_pos >= 8:
            print(f"    [lineup] {player} confirmed batting {lineup_pos} — demoted, capping tier")

        # Find opposing team ID from schedule
        schedule = stats_mlb.get_todays_schedule()
        for _gpk, _game in schedule.items():
            if team_id in (_game.get("home_team_id"), _game.get("away_team_id")):
                opp_team_id = (
                    _game["away_team_id"] if team_id == _game.get("home_team_id")
                    else _game["home_team_id"]
                )
                break

        if opp_team_id:
            try:
                bullpen = stats_mlb.get_bullpen_stats(opp_team_id)
            except Exception:
                pass

        if team_id:
            try:
                team_hitting = stats_mlb.get_team_hitting_stats(team_id)
            except Exception:
                pass

        # Compound spot: starter + bullpen both vulnerable → attack signal
        _compound_spot = False
        try:
            p_era_f = float(pitcher_data.get("era", 4.5))
            p_hr9_f = float(pitcher_data.get("hr_per_9", 0))
        except (ValueError, TypeError):
            p_era_f, p_hr9_f = 4.5, 0
        bp_tier = (bullpen or {}).get("tier", "")
        starter_vuln = p_era_f >= 4.5 or p_hr9_f >= 1.2
        bullpen_vuln = bp_tier in ("WEAK", "AVERAGE")
        if starter_vuln and bullpen_vuln:
            _compound_spot = True

        # ── Weather boost flag (−1 / 0 / +1) ────────────────────────────────
        weather     = enrich.get("weather") or {}
        weather_note_txt = enrich.get("weather_note", "")
        if "hitter-friendly wind" in weather_note_txt.lower() or weather.get("carries"):
            weather_boost = 1
        elif "pitcher-friendly wind" in weather_note_txt.lower():
            weather_boost = -1
        else:
            weather_boost = 0

        # ── Splits for scoring ────────────────────────────────────────────────
        l10_raw  = (splits.get("l10") or {}).get("rate") or 50
        l5_raw   = (splits.get("l5")  or {}).get("rate") or 50
        l20_raw  = (splits.get("l20") or {}).get("rate") or 50
        eff_l10  = (100 - l10_raw) if side == "under" else l10_raw
        eff_l5   = (100 - l5_raw)  if side == "under" else l5_raw
        eff_l20  = (100 - l20_raw) if side == "under" else l20_raw

        # ── Park factor ───────────────────────────────────────────────────────
        park      = enrich.get("park") or {}
        park_f    = park.get("run_factor") or park.get("factor")
        try:
            park_factor = float(park_f) if park_f else None
        except (ValueError, TypeError):
            park_factor = None

        # ── Pitcher ERA / FIP for scoring ─────────────────────────────────────
        try:
            p_era = float(pitcher_data.get("era") or 4.5)
            p_fip = float(pitcher_data.get("fip") or p_era)
            p_hr9 = float(pitcher_data.get("hr_per_9") or 0)
        except (ValueError, TypeError):
            p_era = p_fip = 4.5; p_hr9 = 0

        # ── Statcast ─────────────────────────────────────────────────────────
        sc          = card.get("statcast") or {}
        barrel_pct  = sc.get("barrel_pct")
        hard_hit    = sc.get("hard_hit_pct")

        # ── Umpire tier ───────────────────────────────────────────────────────
        ump_tier_val = enrich.get("umpire_tier") or None

        passed += 1
        card["_is_home"] = is_home

        # ── Unified scoring via grade_pick (single source of truth) ──────────
        # Opponent K rate (for contact-quality context on bat props)
        _opp_k       = card.get("opp_k") or {}
        _opp_k_rank  = _opp_k.get("rank")
        _raw_opp_k   = _opp_k.get("k_pct")
        _opp_k_pct   = (_raw_opp_k / 100) if _raw_opp_k is not None else None

        _grade = vortex_analyze.grade_pick(
            splits=splits,
            line=float(line),
            side=side,
            opp_k_rank=_opp_k_rank,
            opp_k_pct=_opp_k_pct,
            pitcher=pitcher_data,
            bvp=card.get("bvp"),
            park_factor=park_factor or 1.0,
            weather=enrich.get("weather"),
            team_bvp=card.get("team_bvp") or None,
            oaa=card.get("oaa") or None,
            prop_type=prop_type,
            lineup_spot=lineup_pos if isinstance(lineup_pos, int) else None,
            statcast=sc or None,
            team_h2h=card.get("team_h2h") or None,
            arsenal=card.get("arsenal") or None,
            bat_vs_pitch=card.get("bat_vs_pitch") or None,
            vs_hand_splits=card.get("vs_hand_splits") or None,
            learned_weight=_compute_learned_multiplier(prop_type, side, learned_weights or {}),
            is_home=is_home,
        )
        row["vortex_score"] = _grade["score"]
        # Override stats-tier with grade_pick label so board emoji matches analysis
        _GRADE_TIER = {"Elite": "ELITE", "Strong": "STRONG", "Good": "GOOD",
                       "Lean": "LEAN", "Risky": "RISKY", "Fade": "FADE"}
        tier = _GRADE_TIER.get(_grade.get("label", ""), tier)

        row["case_summary"] = _case_from_stats(
            player, row["stat_type"], line,
            row["over_map"], row["under_map"],
            row["best_book"], row["true_prob"],
            ev, card, side)
        row["risk_summary"] = _risk_from_stats(
            ev, row["n_books"], line, row["best_odds"], card, side)
        row["sportsbook"]   = BOOK_DISPLAY.get(row["best_book"], row["best_book"])
        # ── Risk downgrade: cap tier when SEVERE structural risks stack ─────────
        # Only apply when flags are severe AND hit rate doesn't override them.
        # Light penalties (L5 dip, single book) alone don't kill a 90% L10 play.
        _risk_flags = 0

        # Flag 1: Under on a very homer-prone pitcher (HR/9 ≥ 1.5) — hard cap risk
        if side == "under" and p_hr9 >= 1.5:
            _risk_flags += 1

        # Flag 2: L5 collapsed 25+ pts below L10 — momentum clearly reversing
        if eff_l5 <= eff_l10 - 25 and eff_l10 >= 60:
            _risk_flags += 1

        # Flag 3: Binary line (≤ 0.5) AND L10 < 80% — thin margin with weak rate
        if float(line) <= 0.5 and eff_l10 < 80:
            _risk_flags += 1

        # Apply cap only when 3+ flags stack OR 2 flags AND tier is ELITE
        # (a 90%+ L10 play overrides most risk flags — form is the primary signal)
        if _risk_flags >= 3 and tier in ("ELITE", "STRONG"):
            tier = "STRONG"   # knock one level, don't bury it
            print(f"    [risk-cap] {player} {_risk_flags} flags → tier→STRONG")
        elif _risk_flags >= 2 and tier == "ELITE" and eff_l10 < 85:
            tier = "STRONG"
            print(f"    [risk-cap] {player} {_risk_flags} flags + L10<85% → tier→STRONG")

        # Lineup demotion cap: a CONFIRMED bottom-of-order spot (8–9) means fewer
        # PAs than the prop assumed — knock counting-stat Overs down one tier.
        _demote_kw = ("hits", "total base", "hits+run", "rbi", "run", "fantasy")
        if (lineup_confirmed and isinstance(lineup_pos, int) and lineup_pos >= 8
                and side == "over"
                and any(kw in (row.get("stat_type") or "").lower() for kw in _demote_kw)):
            _down = {"ELITE": "STRONG", "STRONG": "GOOD", "GOOD": "LEAN"}
            if tier in _down:
                tier = _down[tier]
                print(f"    [lineup-cap] {player} confirmed batting {lineup_pos} → tier→{tier}")

        row["stats_json"]   = json.dumps({
            "player_id":     batter_id,
            "tier":          tier,
            "lineup_confirmed": lineup_confirmed,
            "signal_type":   signal_type,
            "side":          side,
            "splits":        card.get("splits"),
            "pitcher":       pitcher_data,
            "bvp":           card.get("bvp"),
            "platoon_note":  enrich.get("platoon_note") or card.get("platoon_note"),
            "trend_signal":  card.get("trend_signal"),
            "park":          enrich.get("park"),
            "weather":       enrich.get("weather"),
            "weather_note":  enrich.get("weather_note"),
            "crush_note":    enrich.get("crush_note"),
            "defense_note":  enrich.get("defense_note"),
            "is_home":       is_home,
            "opponent":      opp_team_name,
            "true_prob":     row.get("true_prob"),
            "best_odds":     row.get("best_odds"),
            "lineup_pos":    lineup_pos,
            "team_hitting":  team_hitting,
            "bullpen":       bullpen,
            "weather_boost": weather_boost,
            "eff_l10":       eff_l10,
            "eff_l5":        eff_l5,
            "eff_l20":       eff_l20,
            "compound_spot": _compound_spot,
            "power_shape": {
                "barrel_pct":   barrel_pct,
                "hard_hit_pct": hard_hit,
                "label":  ("💥 Power" if (barrel_pct or 0) >= 10
                           else "🔨 Contact" if (hard_hit or 0) >= 40
                           else "Average") if barrel_pct is not None or hard_hit is not None else None,
            },
            "export_link":   row.get("export_link", ""),
            "all_links":     row.get("all_links", {}),
            # grade_pick sub-scores (for _rebuild_weights learning)
            "proj_edge":      _grade.get("proj_edge"),
            "damage_score":   _grade.get("damage_score"),
            "stability_tier": _grade.get("stability_tier"),
            "lineup_spot":    _grade.get("lineup_spot"),
        }, default=str)
        row["tier"] = tier
        enriched.append(row)

    print(f"  Stats filter: {passed} kept · {discarded_pass} PASS/bypass · "
          f"{below_floor} below-EV-floor · {no_pitcher} odds-only", flush=True)
    return enriched

# ── DB write ─────────────────────────────────────────────────────────────────

def _log_predictions(rows: list[dict], conn: sqlite3.Connection):
    """
    Save props to the predictions table for result grading later.
    Rows with a start time are dated by the local game day; rows without one
    fall back to the VORTEX betting day.
    """
    import vortextime
    fallback_day = vortextime.vortex_day()   # used only when a row has no start time
    logged_at = datetime.now(timezone.utc).isoformat()
    cur = conn.cursor()

    def _game_date(row: dict) -> str:
        ct = (row.get("commence_time") or "").strip()
        if not ct:
            return fallback_day
        try:
            game_start = datetime.fromisoformat(ct.replace("Z", "+00:00"))
            return game_start.astimezone(timezone(timedelta(hours=-7))).date().isoformat()
        except Exception:
            return fallback_day

    inserted = 0
    for row in rows:
        tier = row.get("tier")
        if tier not in ("ELITE", "STRONG"):
            continue
        game_date = _game_date(row)
        sj    = json.loads(row.get("stats_json") or "{}")
        side  = sj.get("side", "over")
        splits = sj.get("splits") or {}
        l5     = (splits.get("l5") or {}).get("rate")
        l10    = (splits.get("l10") or {}).get("rate")
        l20    = (splits.get("l20") or {}).get("rate")
        pitcher = sj.get("pitcher") or {}

        # Skip if already logged for this game date
        exists = cur.execute("""
            SELECT 1 FROM predictions
            WHERE game_date=? AND player_name=? AND market_key=? AND line=? AND side=?
        """, (game_date, row["player_name"], row.get("market_key",""), row["line"], side)).fetchone()
        if exists:
            continue

        cur.execute("""
            INSERT INTO predictions
              (logged_at, game_date, sport, player_name, stat_type, market_key,
               line, side, tier, signal_type, ev_percentage, vortex_score,
               best_book, best_odds, n_books,
               l5_rate, l10_rate, l20_rate, season_avg,
               pitcher_name, pitcher_era, park_factor,
               proj_edge, damage_score, stability_tier, lineup_spot,
               commence_time)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            logged_at, game_date, row["sport"], row["player_name"], row["stat_type"],
            row.get("market_key", ""), row["line"], side,
            row.get("tier"), sj.get("signal_type"),
            row.get("ev_percentage"), row.get("vortex_score"),
            row.get("best_book"), row.get("best_odds"), row.get("n_books"),
            l5, l10, l20, splits.get("season_avg"),
            pitcher.get("name"), pitcher.get("era"),
            (sj.get("park") or {}).get("factor"),
            row.get("proj_edge"), row.get("damage_score"),
            row.get("stability_tier"), row.get("lineup_spot"),
            row.get("commence_time"),
        ))
        inserted += 1

    conn.commit()
    if inserted:
        print(f"  Logged {inserted} new predictions for grading.")


def update_database(rows: list[dict]):
    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS props_board (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            player_name   TEXT    NOT NULL,
            sport         TEXT    NOT NULL,
            stat_type     TEXT    NOT NULL,
            line          REAL    NOT NULL,
            vortex_score  INTEGER NOT NULL,
            ev_percentage REAL    NOT NULL,
            case_summary  TEXT    NOT NULL,
            risk_summary  TEXT    NOT NULL,
            sportsbook    TEXT    NOT NULL,
            stats_json    TEXT    DEFAULT NULL,
            tier          TEXT    DEFAULT NULL,
            commence_time TEXT    DEFAULT NULL
        )
    """)
    try:
        cur.execute("ALTER TABLE props_board RENAME COLUMN silas_score TO vortex_score")
    except Exception:
        pass  # already renamed or column doesn't exist
    try:
        cur.execute("ALTER TABLE props_board ADD COLUMN commence_time TEXT DEFAULT NULL")
    except Exception:
        pass  # column already exists
    cur.execute("""
        CREATE TABLE IF NOT EXISTS predictions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            logged_at     TEXT    NOT NULL,
            game_date     TEXT    NOT NULL,
            sport         TEXT    NOT NULL,
            player_name   TEXT    NOT NULL,
            stat_type     TEXT    NOT NULL,
            market_key    TEXT    NOT NULL,
            line          REAL    NOT NULL,
            side          TEXT    NOT NULL,
            tier          TEXT,
            signal_type   TEXT,
            ev_percentage REAL,
            vortex_score  INTEGER,
            best_book     TEXT,
            best_odds     INTEGER,
            n_books       INTEGER,
            l5_rate       REAL,
            l10_rate      REAL,
            l20_rate      REAL,
            season_avg    REAL,
            pitcher_name  TEXT,
            pitcher_era   REAL,
            park_factor   REAL,
            proj_edge      REAL    DEFAULT NULL,
            damage_score   INTEGER DEFAULT NULL,
            stability_tier TEXT    DEFAULT NULL,
            lineup_spot    INTEGER DEFAULT NULL,
            commence_time  TEXT    DEFAULT NULL,
            result        TEXT    DEFAULT NULL,
            actual_value  REAL    DEFAULT NULL,
            graded_at     TEXT    DEFAULT NULL
        )
    """)
    try:
        cur.execute("ALTER TABLE predictions ADD COLUMN commence_time TEXT DEFAULT NULL")
    except Exception:
        pass  # column already exists
    for _col in (
        "result TEXT DEFAULT NULL",
        "actual_value REAL DEFAULT NULL",
        "graded_at TEXT DEFAULT NULL",
    ):
        try:
            cur.execute(f"ALTER TABLE predictions ADD COLUMN {_col}")
        except Exception:
            pass  # column already exists
    cur.execute("""
        CREATE TABLE IF NOT EXISTS moneyline_predictions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            logged_at     TEXT    NOT NULL,
            game_date     TEXT    NOT NULL,
            game_pk       INTEGER,
            rec_team      TEXT    NOT NULL,
            opponent      TEXT    NOT NULL,
            odds          INTEGER NOT NULL,
            model_pct     REAL    NOT NULL,
            market_pct    REAL    NOT NULL,
            edge_pct      REAL    NOT NULL,
            confidence    REAL    NOT NULL,
            tier          TEXT    NOT NULL,
            rec_pitcher   TEXT,
            opp_pitcher   TEXT,
            rec_fip       REAL,
            opp_fip       REAL,
            park_factor   REAL,
            result        TEXT    DEFAULT NULL,
            actual_winner TEXT    DEFAULT NULL,
            graded_at     TEXT    DEFAULT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS nrfi_predictions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            logged_at     TEXT    NOT NULL,
            game_date     TEXT    NOT NULL,
            game_pk       INTEGER,
            home_abbr     TEXT    NOT NULL,
            away_abbr     TEXT    NOT NULL,
            home_pitcher  TEXT,
            away_pitcher  TEXT,
            recommendation TEXT   NOT NULL,
            confidence    TEXT    NOT NULL,
            score         INTEGER NOT NULL,
            confidence_pct REAL   NOT NULL,
            result        TEXT    DEFAULT NULL,
            actual_result TEXT    DEFAULT NULL,
            graded_at     TEXT    DEFAULT NULL
        )
    """)
    cur.execute("DELETE FROM props_board")
    if rows:
        cur.executemany(
            """
            INSERT INTO props_board
              (player_name, sport, stat_type, line, vortex_score, ev_percentage,
               case_summary, risk_summary, sportsbook, stats_json, tier, commence_time)
            VALUES
              (:player_name, :sport, :stat_type, :line, :vortex_score, :ev_percentage,
               :case_summary, :risk_summary, :sportsbook, :stats_json, :tier, :commence_time)
            """,
            rows,
        )
        _log_predictions(rows, conn)
    conn.commit()
    conn.close()
    print(f"  Wrote {len(rows)} props to DB.")

# ── Live-game purge (zero API calls) ─────────────────────────────────────────

def purge_started_games():
    """Remove rows from props_board whose game has already started. No API calls."""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()
    try:
        cur.execute(
            "DELETE FROM props_board WHERE commence_time != '' AND commence_time IS NOT NULL AND commence_time <= ?",
            (now_iso,),
        )
        deleted = cur.rowcount
        conn.commit()
        if deleted:
            print(f"[board] Purged {deleted} props for games that have started")
    finally:
        conn.close()
    if deleted:
        publish_board_to_site()


# ── Live odds-key override ────────────────────────────────────────────────────
# Swapping ODDS_API_KEY in .env requires a bot-host restart to take effect
# (load_dotenv only runs once, at import). This lets Discord's /setoddskey
# admin command push a replacement key to the same KV store the website
# uses, which every board rebuild picks up on its NEXT run -- no restart.

def refresh_live_api_key():
    """Call at the start of any board run. Overwrites the module-level
    API_KEY (read by every fetch_* function below) with the KV override if
    one is set; otherwise leaves the .env-loaded default untouched. KV
    lookup failures (no creds locally, network hiccup) just fall back
    silently -- a stale-but-working key beats crashing the whole rebuild."""
    global API_KEY
    kv_url   = (os.getenv("KV_REST_API_URL") or "").rstrip("/")
    kv_token = os.getenv("KV_REST_API_TOKEN") or ""
    if not kv_url or not kv_token:
        return
    try:
        resp = ODDS_SESSION.get(
            f"{kv_url}/get/{LIVE_ODDS_KEY_KV}",
            headers={"Authorization": f"Bearer {kv_token}"},
            timeout=10,
        )
        resp.raise_for_status()
        live_key = (resp.json() or {}).get("result")
        if live_key and live_key.strip():
            API_KEY = live_key.strip()
            print(f"  Using live-swapped odds key from KV (…{API_KEY[-4:]}).")
    except Exception as e:  # noqa: BLE001 — never block a rebuild on this
        print(f"  [warn] Could not check live odds key override: {e}")


def test_odds_api_key(candidate_key: str) -> dict:
    """Validate a NOT-YET-SAVED key against the free /sports endpoint (no
    credits spent) before /setoddskey overwrites the live one with it."""
    try:
        r = ODDS_SESSION.get(f"{BASE_URL}/sports", params={"apiKey": candidate_key}, timeout=10)
        if r.status_code == 401:
            return {"valid": False, "error": "Key rejected (401 Unauthorized)"}
        r.raise_for_status()
        return {
            "valid": True,
            "requests_remaining": r.headers.get("x-requests-remaining", "?"),
            "requests_used": r.headers.get("x-requests-used", "?"),
        }
    except requests.RequestException as exc:
        return {"valid": False, "error": str(exc)}


def set_live_api_key(new_key: str) -> tuple[bool, str]:
    """Validate + push a new odds key to KV (LIVE_ODDS_KEY_KV) so the NEXT
    board rebuild picks it up with zero bot-host restart. Returns (ok, message)."""
    new_key = (new_key or "").strip()
    if not new_key:
        return False, "Key can't be empty."

    check = test_odds_api_key(new_key)
    if not check.get("valid"):
        return False, f"Key rejected: {check.get('error', 'unknown error')}"

    kv_url   = (os.getenv("KV_REST_API_URL") or "").rstrip("/")
    kv_token = os.getenv("KV_REST_API_TOKEN") or ""
    if not kv_url or not kv_token:
        return False, "KV_REST_API_URL/KV_REST_API_TOKEN not set — can't publish a live key."

    try:
        resp = ODDS_SESSION.post(
            f"{kv_url}/set/{LIVE_ODDS_KEY_KV}",
            data=new_key.encode("utf-8"),
            headers={"Authorization": f"Bearer {kv_token}"},
            timeout=10,
        )
        resp.raise_for_status()
    except Exception as e:  # noqa: BLE001
        return False, f"Key validated but KV write failed: {e}"

    global API_KEY
    API_KEY = new_key  # take effect immediately for this process too, not just future ones
    remaining = check.get("requests_remaining", "?")
    return True, f"Live key updated — {remaining} credits remaining. No restart needed."


# ── Website mirror ────────────────────────────────────────────────────────────

SITE_BOARD_KV_KEY = "vortex:site_board"


def publish_board_to_site():
    """
    Mirror the exact board the Discord bot serves (the props_board table) to
    the website's Upstash KV store, where predictions-site/api/board.py reads
    it. Runs AFTER the DB write/purge and reads the rows back from sqlite, so
    the site can never drift from what /menu shows.

    Uses the same KV_REST_API_URL / KV_REST_API_TOKEN the deployed site uses
    (root .env locally, Vercel env vars in prod). Missing creds or a network
    error must never kill an engine run — the DB write already succeeded and
    the Discord bot doesn't depend on this.
    """
    kv_url   = (os.getenv("KV_REST_API_URL") or "").rstrip("/")
    kv_token = os.getenv("KV_REST_API_TOKEN") or ""
    if not kv_url or not kv_token:
        print("  [skip] KV_REST_API_URL/KV_REST_API_TOKEN not set — site board not published.")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS props_board (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                player_name   TEXT    NOT NULL,
                sport         TEXT    NOT NULL,
                stat_type     TEXT    NOT NULL,
                line          REAL    NOT NULL,
                vortex_score  INTEGER NOT NULL,
                ev_percentage REAL    NOT NULL,
                case_summary  TEXT    NOT NULL,
                risk_summary  TEXT    NOT NULL,
                sportsbook    TEXT    NOT NULL,
                stats_json    TEXT    DEFAULT NULL,
                tier          TEXT    DEFAULT NULL,
                commence_time TEXT    DEFAULT NULL
            )
        """)
        rows = conn.execute(
            "SELECT * FROM props_board ORDER BY vortex_score DESC"
        ).fetchall()
    finally:
        conn.close()

    props = []
    for r in rows:
        d = dict(r)
        d.pop("id", None)
        try:
            d["stats"] = json.loads(d.pop("stats_json", None) or "{}")
        except (ValueError, TypeError):
            d["stats"] = {}
        # Skip ghost entries: props without a valid player_id are phantom rows
        # from stale odds data — never publish them to the site.
        if not d.get("stats", {}).get("player_id"):
            continue
        props.append(d)

    import vortextime
    payload = json.dumps({
        "date":         vortextime.vortex_board_day(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "props":        props,
    }, default=str)

    try:
        resp = ODDS_SESSION.post(
            f"{kv_url}/set/{SITE_BOARD_KV_KEY}",
            data=payload.encode("utf-8"),
            headers={"Authorization": f"Bearer {kv_token}"},
            timeout=15,
        )
        resp.raise_for_status()
        print(f"  Published {len(props)} props to the website board.")
    except Exception as e:  # noqa: BLE001 — mirror failure must not fail the run
        print(f"  [warn] Website board publish failed: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 55)
    print("  Vortex Data Engine  v5")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  EV floor: +{MIN_EV_PCT}%  |  {MIN_BOOKS}+ books  |  juice <= {MAX_JUICE}")
    print("=" * 55)

    refresh_live_api_key()
    if not API_KEY:
        print("\n  ODDS_API_KEY not set — using cached data only.\n")

    # Pre-fetch tomorrow's MLB schedule + pitcher lookups (1 API call, cached)
    import vortextime
    board_date = vortextime.vortex_board_day()
    print(f"\n  Loading MLB schedule for {board_date}...")
    schedule              = stats_mlb.get_todays_schedule(game_date=board_date)
    pitcher_lookup        = stats_mlb.build_pitcher_lookup(schedule)
    pitcher_game_lookup   = build_pitcher_game_lookup(schedule)
    team_game_lookup      = build_team_game_lookup(schedule)
    print(f"  {len(schedule)} games found · {len(pitcher_lookup)} teams have a probable pitcher")

    umpire_lookup: dict[int, str] = {}
    try:
        umpire_lookup = stats_mlb.get_game_umpires()
        print(f"  Umpires: {len(umpire_lookup)//2} games assigned ({len(umpire_lookup)} team entries)")
    except Exception as _ue:
        print(f"  [warn] Umpire fetch failed: {_ue}")

    # Load learned weights from score_weights table (populated by grade_results)
    learned_weights = _load_learned_weights()
    if learned_weights:
        print(f"  Learned weights: {len(learned_weights)} signals loaded")
    else:
        print("  No learned weights available — using hardcoded scoring")

    # Hard hitrate gate: stat_types proven net-losers (enough graded history) are
    # dropped from the board entirely. Empty until ~25 graded picks accumulate.
    blocked_signals = _load_blocked_signals()
    if blocked_signals:
        print(f"  Hitrate gate: {len(blocked_signals)} stat_type(s) blocked as net losers")

    # Pre-fetch today's NBA schedule + opponent lookup
    nba_schedule   = {}
    nba_opp_lookup = {}
    if NBA_ENABLED:
        print("\n  Loading NBA schedule...")
        try:
            nba_schedule   = stats_nba.get_todays_schedule()
            nba_opp_lookup = stats_nba.build_opponent_lookup(nba_schedule)
            print(f"  {len(nba_schedule)} NBA games found · {len(nba_opp_lookup)} teams have a game today")
        except Exception as e:
            print(f"  [skip] NBA schedule unavailable: {e}")
    else:
        print("\n  NBA disabled — skipping schedule fetch")

    # Pre-fetch today's WNBA schedule + opponent lookup + league pace + defense ranks
    wnba_opp_lookup = {}
    wnba_league_pace = None
    wnba_def_ranks = {}
    wnba_blowout_abbrs: set = set()
    if WNBA_ENABLED:
        print("\n  Loading WNBA schedule (ESPN)...")
        try:
            wnba_schedule    = stats_wnba.get_todays_schedule()
            wnba_opp_lookup  = stats_wnba.get_opponent_lookup(wnba_schedule)
            wnba_league_pace = stats_wnba.get_league_avg_pace()
            _pre = sum(1 for g in wnba_schedule if g.get("state") == "pre")
            print(f"  {len(wnba_schedule)} WNBA games ({_pre} upcoming) · league pace {wnba_league_pace}")
            print("  Computing WNBA opponent defense ranks (cached 6h)...")
            wnba_def_ranks = stats_wnba.get_defense_ranks()
            print(f"  Defense ranks ready for {len(wnba_def_ranks.get('points', {}))} teams")
            wnba_blowout_abbrs = _wnba_blowout_teams()
            if wnba_blowout_abbrs:
                print(f"  Blowout-risk teams tonight: {sorted(wnba_blowout_abbrs)}")
        except Exception as e:
            print(f"  [skip] WNBA enrichment data unavailable: {e}")
    else:
        print("\n  WNBA disabled — skipping schedule fetch")

    all_rows: list[dict] = []

    for sport, cfg in SPORT_CONFIG.items():
        # Fetch all markets for this sport in one batch (1 API call per game vs 1 per market per game)
        print(f"\n  Fetching {sport} props ({len(cfg['markets'])} markets batched) ...")
        batched_events = fetch_all_markets_batched(cfg["key"], cfg["markets"])

        for market in cfg["markets"]:
            label = MARKET_LABELS.get(market, market)
            print(f"\n  {sport} / {label}", flush=True)
            try:
                events_data = batched_events
                ev_rows     = parse_events(events_data, sport, market)
                print(f"  {len(ev_rows)} candidate rows (O+U, pre-stats-gate)", flush=True)

                if sport == "MLB" and ev_rows:
                    ev_rows = enrich_mlb(ev_rows, pitcher_lookup, pitcher_game_lookup, team_game_lookup, umpire_lookup,
                                        learned_weights=learned_weights)
                elif sport == "NBA" and ev_rows:
                    if not NBA_ENABLED:
                        print(f"  [skip] NBA disabled — {len(ev_rows)} prop(s) dropped")
                        ev_rows = []
                    elif not nba_schedule or not nba_opp_lookup:
                        print(f"  [skip] NBA unavailable — {len(ev_rows)} prop(s) dropped")
                        ev_rows = []
                    else:
                        ev_rows = enrich_nba(ev_rows, nba_opp_lookup)
                elif sport == "WNBA" and ev_rows:
                    if not WNBA_ENABLED or not wnba_opp_lookup:
                        print(f"  [skip] WNBA unavailable — {len(ev_rows)} prop(s) dropped")
                        ev_rows = []
                    else:
                        ev_rows = enrich_wnba(ev_rows, wnba_opp_lookup, wnba_league_pace,
                                              def_ranks=wnba_def_ranks,
                                              blowout_abbrs=wnba_blowout_abbrs)
                else:
                    # Any other sport: odds-only fallback
                    for row in ev_rows:
                        row["vortex_score"] = compute_score(
                            row["ev_percentage"], row["n_books"],
                            row["line"], row["best_odds"])
                        row["case_summary"] = _case_odds_only(
                            row["player_name"], row["stat_type"], row["line"],
                            row["over_map"], row["under_map"],
                            row["best_book"], row["true_prob"],
                            row["ev_percentage"], row["sport"])
                        row["risk_summary"] = _risk_odds_only(
                            row["ev_percentage"], row["n_books"],
                            row["line"], row["best_odds"])
                        row["sportsbook"]   = BOOK_DISPLAY.get(row["best_book"], row["best_book"])
                        row["stats_json"]   = None
                        row["tier"]         = None

                all_rows.extend(ev_rows)
            except BaseException as exc:
                import traceback, sys
                print(f"  [error] {type(exc).__name__}: {exc}", flush=True)
                traceback.print_exc(file=sys.stdout)
                sys.stdout.flush()

    # ── Prop-type allowlist: only keep quality props ─────────────────────────
    _ALLOWED_MLB_MARKET_KEYS = {
        "batter_hits_runs_rbis",
        "batter_total_bases",
        "batter_hits",
        "batter_fantasy_score",
        "pitcher_strikeouts",
        "pitcher_outs",
        "pitcher_hits_allowed",
        "pitcher_earned_runs",
    }
    _filtered_rows = []
    for row in all_rows:
        if row.get("sport") == "MLB":
            mk = row.get("market_key") or ""
            if mk not in _ALLOWED_MLB_MARKET_KEYS:
                print(f"  [prop-filter] dropped {row['player_name']} {mk} (not in allowlist)")
                continue
        # Hard hitrate gate: skip stat_types proven to lose over a real sample.
        if (row.get("sport"), row.get("stat_type")) in blocked_signals:
            print(f"  [hitrate-gate] dropped {row['player_name']} {row.get('stat_type')} "
                  f"(stat_type below {BLOCK_HITRATE*100:.0f}% over {BLOCK_MIN_SAMPLE}+ graded)")
            continue
        _filtered_rows.append(row)
    all_rows = _filtered_rows

    # ── Side resolution: ONE play per player+stat (never contradictory) ──────
    # Group every candidate row for the same player+stat together — across BOTH
    # directions AND every line the books offer. Vortex must emit at most one
    # play per player+stat, so it can never tell you both "Freeland K under 4.5"
    # and "Freeland K over 3.5" on the same slate.
    # Rules:
    #   1. Keep the single highest vortex_score candidate (any line, any side).
    #   2. If the best Over and best Under are within _SIDE_MARGIN, the matchup
    #      is a genuine toss-up — drop the player+stat entirely.
    #   3. NOTE: line is deliberately NOT in the group key. Two different lines
    #      for the same player+stat are still the same bet decision; showing
    #      more than one (especially in opposite directions) is the contradiction
    #      we are eliminating.
    _prop_groups: dict[str, dict] = {}   # key (player|stat) → {over: row, under: row}
    for row in all_rows:
        key = f"{row['player_name']}|{row['stat_type']}"
        if key not in _prop_groups:
            _prop_groups[key] = {}
        side = row.get("side", "over")
        existing = _prop_groups[key].get(side)
        # within a side, keep the best-scoring line
        if existing is None or row["vortex_score"] > existing["vortex_score"]:
            _prop_groups[key][side] = row

    _SIDE_MARGIN = 5   # if best O and best U are this close, prop is too noisy to show

    all_rows_filtered: list[dict] = []
    for key, sides in _prop_groups.items():
        over_row  = sides.get("over")
        under_row = sides.get("under")
        if over_row and under_row:
            # Both directions cleared the gate for this player+stat. That is the
            # contradiction signal — only keep it if one side is decisively better.
            over_score  = over_row["vortex_score"]
            under_score = under_row["vortex_score"]
            if abs(over_score - under_score) <= _SIDE_MARGIN:
                print(f"  [side-filter] dropped toss-up: {key} (O={over_score} U={under_score})")
                continue
            winner = over_row if over_score > under_score else under_row
            all_rows_filtered.append(winner)
        elif over_row:
            all_rows_filtered.append(over_row)
        elif under_row:
            all_rows_filtered.append(under_row)

    # ── Fantasy Score minimum score gate ─────────────────────────────────────
    # Model accuracy on FS props is 42.9% — require Elite-level score to include.
    _fs_dropped = 0
    _fs_filtered: list[dict] = []
    for row in all_rows_filtered:
        if row.get("stat_type") == "fantasy_score" and row["vortex_score"] < FANTASY_SCORE_MIN_SCORE:
            _fs_dropped += 1
            print(f"  [fs-gate] dropped sub-Elite FS prop: {row['player_name']} score={row['vortex_score']}")
        else:
            _fs_filtered.append(row)
    if _fs_dropped:
        print(f"  [fs-gate] dropped {_fs_dropped} Fantasy Score props below score {FANTASY_SCORE_MIN_SCORE}")
    all_rows_filtered = _fs_filtered

    # ── Board cap: pitcher + WNBA get reserved slots, MLB batters fill the rest ─
    _PITCHER_MK = {"pitcher_strikeouts", "pitcher_outs", "pitcher_hits_allowed", "pitcher_earned_runs"}
    seen_k: dict[str, dict] = {}   # MLB pitcher props
    seen_w: dict[str, dict] = {}   # WNBA props (own reserved pool)
    seen_b: dict[str, dict] = {}   # MLB batter props
    for row in all_rows_filtered:
        key = f"{row['player_name']}|{row['stat_type']}|{row['line']}"
        if row.get("sport") == "WNBA":
            bucket = seen_w
        elif row.get("market_key") in _PITCHER_MK:
            bucket = seen_k
        else:
            bucket = seen_b
        if key not in bucket or row["vortex_score"] > bucket[key]["vortex_score"]:
            bucket[key] = row

    def _cap_by_player(rows: list[dict], limit: int) -> list[dict]:
        """Take highest-scoring rows up to `limit`, max MAX_PICKS_PER_PLAYER each."""
        counts: dict[str, int] = {}
        kept: list[dict] = []
        for row in sorted(rows, key=lambda r: r["vortex_score"], reverse=True):
            if len(kept) >= limit:
                break
            pname = row["player_name"]
            if counts.get(pname, 0) >= MAX_PICKS_PER_PLAYER:
                print(f"  [player-cap] skipped {pname} — already {MAX_PICKS_PER_PLAYER} picks")
                continue
            kept.append(row)
            counts[pname] = counts.get(pname, 0) + 1
        return kept

    top_k = sorted(seen_k.values(), key=lambda r: r["vortex_score"], reverse=True)[:MAX_PITCHER_K]
    top_w = _cap_by_player(list(seen_w.values()), MAX_WNBA)
    batter_slots = MAX_BOARD - len(top_k) - len(top_w)
    top_b = _cap_by_player(list(seen_b.values()), batter_slots)

    final = top_k + top_w + top_b
    print(f"  Board: {len(top_k)} pitcher + {len(top_w)} WNBA + {len(top_b)} batter props")

    # Strip internal-only fields before DB write
    db_rows = []
    for r in final:
        # Stamp the price-anchor truth into stats_json so the frontend can label
        # each card honestly (sharp-anchored vs consensus vs no two-sided line)
        # and never present a fabricated edge.
        anchor  = r.get("anchor", "consensus")
        ev_real = r.get("ev_real", True)
        try:
            sj = json.loads(r.get("stats_json") or "{}")
        except (ValueError, TypeError):
            sj = {}
        sj["anchor"]  = anchor
        sj["ev_real"] = ev_real
        stats_json = json.dumps(sj)

        db_rows.append({
            "player_name":   r["player_name"],
            "sport":         r["sport"],
            "stat_type":     r["stat_type"],
            "line":          r["line"],
            "vortex_score":   r["vortex_score"],
            # Honest EV: 0.0 when there is no real two-sided de-vig (never a
            # fabricated positive edge). The `anchor` tag tells the UI to show N/A.
            "ev_percentage": r["ev_percentage"] if ev_real else 0.0,
            "anchor":        anchor,
            "case_summary":  r.get("case_summary", ""),
            "risk_summary":  r.get("risk_summary", ""),
            "sportsbook":    r.get("sportsbook", ""),
            "stats_json":    stats_json,
            "tier":          r.get("tier"),
            "commence_time": r.get("commence_time", ""),
        })

    print(f"\n{'=' * 55}")
    print(f"  Props on board : {len(db_rows)}")
    if db_rows:
        print(f"\n  {'SCR':>3}  {'EV':>7}  {'TIER':>6}  PLAYER + STAT")
        print(f"  {'─'*3}  {'─'*7}  {'─'*6}  {'─'*36}")
        for r in db_rows:
            ev_str   = f"{r['ev_percentage']:+.1f}%" if r.get("anchor") in ("sharp", "consensus") else "N/A"
            tier_str = r.get("tier") or "—"
            side_pfx = "U" if r.get("side") == "under" else "O"
            print(f"  {r['vortex_score']:>3}  {ev_str:>7}  {tier_str:>6}  "
                  f"{r['player_name']} — {r['stat_type']} {side_pfx}{r['line']}")
    print(f"{'=' * 55}")

    if not db_rows:
        print(
            "\n  Board is empty — no new edges found. Preserving existing DB rows\n"
            "  so the Discord bot stays live. Re-run closer to game time.\n"
        )
    else:
        update_database(db_rows)
    # Always mirror to the website — on an empty run this re-publishes the
    # preserved DB rows, keeping the site identical to the Discord bot.
    publish_board_to_site()

if __name__ == "__main__":
    main()
