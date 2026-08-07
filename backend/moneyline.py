"""
VORTEX — MLB Moneyline Model v4
================================
Estimates a true win probability for each side of every MLB game, compares it to
the market moneyline, and surfaces games with a real edge.

Win-probability model (the conservative core):
  1. De-vigged market consensus  — the baseline probability.
  2. Pythagorean + Log5 talent   — sample-weighted season team quality.
  3. Starter FIP                 — schedule-ID-verified and shrunk to league average.
  4. Recent bullpen sample       — relief-only, minimum 12 IP, strongly capped.
  5. Reliability gate            — no bet for unconfirmed lineups, uncertain starters,
                                  thin bullpen samples, or long starter layoffs.
  6. Dynamic market anchor       — a small contextual residual, never a free-standing guess.

The market line is de-vigged to a fair implied probability; edge = model − implied.
Only reliability-qualified prices with positive expected value are posted.

Public API
----------
  get_moneyline_plays(game_date=None, force_odds=False) -> list[dict]
  build_moneyline_embeds(plays, date_str) -> list[discord.Embed]
"""

import os
import json
import time
import math
from datetime import datetime as _dt, timezone as _tz
from pathlib import Path

import requests
from dotenv import load_dotenv

import stats_mlb as sm
import vortextime

load_dotenv(Path(__file__).parent.parent / ".env")

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
ODDS_BASE    = "https://api.the-odds-api.com/v4"

_ODDS_CACHE  = Path(__file__).parent / "cache" / "moneyline_odds.json"
_ODDS_V4_CACHE = Path(__file__).parent / "cache" / "moneyline_odds_v4.json"
ODDS_TTL_SEC = 1800   # 30 min

# ── Tuning constants ─────────────────────────────────────────────────────────
MODEL_VERSION       = "v5-expected-runs"
# v4 treats no-vig consensus as the baseline and permits only modest,
# sample-qualified movement around it.
SANITY_CAP          = 0.12
MARKET_ANCHOR_EARLY = 0.62
MARKET_ANCHOR_LATE  = 0.72
ANCHOR_SWING_HOURS  = 2
PYTHAG_EXP          = 1.83
HOME_FIELD          = 0.025
MIN_IP_STARTER      = 3.0
HARD_BOUND_LOW      = 0.05
HARD_BOUND_HIGH     = 0.95
RLM_THRESHOLD       = 0.025
LEAGUE_FIP          = 4.10
PITCHER_PRIOR_IP    = 45.0
MAX_CONTEXT_SHIFT   = 0.16
MIN_RELIABILITY     = 0.72
MIN_LEAN_EDGE       = 0.020
MIN_STRONG_EDGE     = 0.030
MIN_LEAN_WIN_PROB   = 0.52
MIN_STRONG_WIN_PROB = 0.56
MIN_LEAN_EV         = 0.012
MIN_STRONG_EV       = 0.025
LEAGUE_RUNS_PER_GAME = 4.45

MONEYLINE_FACTOR_WEIGHTS = {
    "starting_pitching": 30,
    "confirmed_offense": 25,
    "bullpen": 18,
    "team_quality": 12,
    "defense": 5,
    "environment": 5,
    "schedule": 5,
}

_ODDS_SESSION = requests.Session()
_ODDS_SESSION.headers.update({"User-Agent": "VORTEX/1.0", "Accept": "application/json"})

# ── Opening-line snapshot for reverse line movement detection ────────────────
_OPENING_LINES_CACHE = Path(__file__).parent / "cache" / "ml_opening_lines.json"


# ── Probability helpers ──────────────────────────────────────────────────────

def _pythag(rs: int, ra: int) -> float:
    """True-talent win% from runs scored/allowed."""
    if rs <= 0 or ra <= 0:
        return 0.5
    rs_e, ra_e = rs ** PYTHAG_EXP, ra ** PYTHAG_EXP
    return rs_e / (rs_e + ra_e)


def _blend_team_strength(s: dict) -> float:
    """
    Team talent estimate: blend Pythagorean (stable) with actual win% (captures
    things runs miss), weighted toward Pythag. Light early-season regression to .500.
    """
    if not s:
        return 0.5
    pythag = _pythag(s.get("rs", 0), s.get("ra", 0))
    win_pct = s.get("win_pct", 0.5) or 0.5
    base = 0.65 * pythag + 0.35 * win_pct
    gp = s.get("gp", 0) or 0
    if gp < 40:
        w = gp / 40
        base = w * base + (1 - w) * 0.5
    return max(0.30, min(0.70, base))


def _log5(a: float, b: float) -> float:
    """P(team A beats team B) given each team's win prob vs a league-average team."""
    denom = a + b - 2 * a * b
    if denom <= 0:
        return 0.5
    return (a - a * b) / denom


def american_to_prob(odds: int) -> float:
    """American odds → implied probability (with vig)."""
    if odds < 0:
        return -odds / (-odds + 100)
    return 100 / (odds + 100)


def devig_two_way(p_home: float, p_away: float) -> tuple[float, float]:
    """Remove the bookmaker hold from a two-way market → fair probabilities."""
    total = p_home + p_away
    if total <= 0:
        return 0.5, 0.5
    return p_home / total, p_away / total


def _norm(name: str) -> str:
    return (name or "").lower().strip()


# ── Odds fetch ───────────────────────────────────────────────────────────────

def _fetch_moneylines(force: bool = False) -> dict:
    """
    {normalized_team_name: {opp, is_home, odds, fair_implied, commence_time}} for
    every MLB game with an h2h market. Consensus odds = median across books.
    """
    if not force and _ODDS_CACHE.exists():
        try:
            if time.time() - _ODDS_CACHE.stat().st_mtime < ODDS_TTL_SEC:
                return json.loads(_ODDS_CACHE.read_text(encoding="utf-8"))
        except Exception:
            pass
    if not ODDS_API_KEY:
        return {}
    try:
        r = _ODDS_SESSION.get(
            f"{ODDS_BASE}/sports/baseball_mlb/odds",
            params={"apiKey": ODDS_API_KEY, "regions": "us",
                    "markets": "h2h", "oddsFormat": "american"}, timeout=20)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"  [moneyline] odds fetch failed: {e}")
        if _ODDS_CACHE.exists():
            try:
                return json.loads(_ODDS_CACHE.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    out = {}
    for g in r.json():
        home, away = g.get("home_team"), g.get("away_team")
        ct = g.get("commence_time", "")
        prices: dict[str, list[int]] = {}
        for bm in g.get("bookmakers", []):
            for m in bm.get("markets", []):
                if m.get("key") != "h2h":
                    continue
                for o in m.get("outcomes", []):
                    prices.setdefault(o["name"], []).append(int(o["price"]))
        if home not in prices or away not in prices:
            continue
        def _med(lst):
            lst = sorted(lst)
            n = len(lst)
            return lst[n // 2] if n % 2 else (lst[n // 2 - 1] + lst[n // 2]) / 2
        h_odds, a_odds = _med(prices[home]), _med(prices[away])
        h_fair, a_fair = devig_two_way(american_to_prob(h_odds), american_to_prob(a_odds))
        out[_norm(home)] = {"opp": away, "is_home": True,  "odds": h_odds,
                            "fair_implied": h_fair, "commence_time": ct}
        out[_norm(away)] = {"opp": home, "is_home": False, "odds": a_odds,
                            "fair_implied": a_fair, "commence_time": ct}
    try:
        _ODDS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        _ODDS_CACHE.write_text(json.dumps(out), encoding="utf-8")
    except Exception:
        pass
    return out


# ── Opening-line snapshot (RLM detection) ───────────────────────────────────

def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def _fetch_moneylines_v4(force: bool = False) -> list[dict]:
    """Fetch odds by event, preserving an executable best price per side.

    The old team-keyed cache could overwrite a doubleheader and turned a
    synthetic median price into an apparent bettable line. This event-level
    representation retains the exact matchup, time, consensus no-vig baseline,
    and the best listed book price for each side.
    """
    if not force and _ODDS_V4_CACHE.exists():
        try:
            if time.time() - _ODDS_V4_CACHE.stat().st_mtime < ODDS_TTL_SEC:
                cached = json.loads(_ODDS_V4_CACHE.read_text(encoding="utf-8"))
                if isinstance(cached, list):
                    return cached
        except Exception:
            pass
    if not ODDS_API_KEY:
        return []
    try:
        response = _ODDS_SESSION.get(
            f"{ODDS_BASE}/sports/baseball_mlb/odds",
            params={"apiKey": ODDS_API_KEY, "regions": "us", "markets": "h2h", "oddsFormat": "american"},
            timeout=20,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        print(f"  [moneyline] odds fetch failed: {exc}")
        return []

    events: list[dict] = []
    for event in response.json():
        home, away = event.get("home_team"), event.get("away_team")
        if not home or not away:
            continue
        books = []
        for bookmaker in event.get("bookmakers", []):
            market = next((m for m in bookmaker.get("markets", []) if m.get("key") == "h2h"), None)
            if not market:
                continue
            outcomes = {o.get("name"): o.get("price") for o in market.get("outcomes", [])}
            try:
                home_price, away_price = int(outcomes[home]), int(outcomes[away])
            except (KeyError, TypeError, ValueError):
                continue
            fair_home, fair_away = devig_two_way(american_to_prob(home_price), american_to_prob(away_price))
            books.append({
                "book": bookmaker.get("title") or bookmaker.get("key") or "Unknown",
                "home_price": home_price, "away_price": away_price,
                "fair_home": fair_home, "fair_away": fair_away,
                "updated_at": bookmaker.get("last_update") or event.get("commence_time", ""),
            })
        if not books:
            continue
        best_home = max(books, key=lambda book: book["home_price"])
        best_away = max(books, key=lambda book: book["away_price"])
        events.append({
            "event_id": event.get("id"), "home": home, "away": away,
            "commence_time": event.get("commence_time", ""),
            "home_line": {"odds": best_home["home_price"], "book": best_home["book"],
                          "fair_implied": _median([book["fair_home"] for book in books]),
                          "updated_at": best_home["updated_at"]},
            "away_line": {"odds": best_away["away_price"], "book": best_away["book"],
                          "fair_implied": _median([book["fair_away"] for book in books]),
                          "updated_at": best_away["updated_at"]},
        })
    try:
        _ODDS_V4_CACHE.parent.mkdir(parents=True, exist_ok=True)
        _ODDS_V4_CACHE.write_text(json.dumps(events), encoding="utf-8")
    except Exception:
        pass
    return events


def _match_market_event(events: list[dict], home_name: str, away_name: str, game_utc: str) -> dict | None:
    """Match an MLB schedule game to exactly one Odds API event."""
    candidates = [
        event for event in events
        if _norm(event.get("home")) == _norm(home_name) and _norm(event.get("away")) == _norm(away_name)
    ]
    if not candidates:
        return None
    try:
        game_time = _dt.fromisoformat(game_utc.replace("Z", "+00:00"))
        def distance(event):
            return abs((_dt.fromisoformat(event.get("commence_time", "").replace("Z", "+00:00")) - game_time).total_seconds())
        matched = min(candidates, key=distance)
        return matched if distance(matched) <= 12 * 3600 else None
    except (TypeError, ValueError):
        return candidates[0] if len(candidates) == 1 else None


def _load_opening_lines() -> dict:
    """Load snapshot of first-seen odds per game_pk."""
    if _OPENING_LINES_CACHE.exists():
        try:
            return json.loads(_OPENING_LINES_CACHE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_opening_lines(data: dict):
    try:
        _OPENING_LINES_CACHE.parent.mkdir(parents=True, exist_ok=True)
        _OPENING_LINES_CACHE.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass


def _snapshot_opening_line(game_key: str, fair_home: float, odds_home: int):
    """Store the first-seen line for a versioned market event."""
    opening = _load_opening_lines()
    key = str(game_key)
    if key not in opening:
        opening[key] = {
            "fair_home": fair_home,
            "odds_home": odds_home,
            "timestamp": time.time(),
        }
        _save_opening_lines(opening)


def _is_rlm_trap(game_key: str, model_favors_home: bool, current_fair_home: float) -> bool:
    """
    Detect reverse line movement: model favors one side but the market has
    moved against that side since opening. Indicates sharp money on the other side.
    """
    opening = _load_opening_lines()
    entry = opening.get(str(game_key))
    if not entry:
        return False
    move = current_fair_home - entry.get("fair_home", current_fair_home)
    # Model likes Home but market moved toward Away
    if model_favors_home and move < -RLM_THRESHOLD:
        return True
    # Model likes Away but market moved toward Home
    if not model_favors_home and move > RLM_THRESHOLD:
        return True
    return False


# ── Pitcher adjustment (matchup-aware) ──────────────────────────────────────

def _decimal_ip(value) -> float:
    """Convert MLB innings notation (e.g. 56.2) to decimal innings."""
    try:
        whole, _, outs = str(value or "0").partition(".")
        return max(0.0, float(whole) + (int(outs or 0) / 3 if outs else 0.0))
    except (TypeError, ValueError):
        return 0.0


def _finite_float(value) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) and result > 0 else None
    except (TypeError, ValueError):
        return None


def _shrink_fip(fip: float | None, innings: float, prior_ip: float = PITCHER_PRIOR_IP) -> float | None:
    """Shrink noisy pitcher FIP toward league average before it moves a price."""
    fip = _finite_float(fip)
    if fip is None:
        return None
    innings = max(0.0, innings or 0.0)
    return (fip * innings + LEAGUE_FIP * prior_ip) / (innings + prior_ip)


def _pitcher_adjustment(
    pitcher_name: str | None,
    pitcher_id: int | None,
    opposing_team_id: int,
    date_str: str,
    park_factor: float,
    weather: dict,
    lineups_data: dict = None,
) -> tuple[float | None, float | None, str]:
    """Return a schedule-ID-verified, sample-shrunk starter FIP.

    Park, weather, and lineup splits remain context/variance signals; they do
    not move one team independently until they pass walk-forward validation.
    """
    if not pitcher_name:
        return None, None, "UNKNOWN"

    # The schedule supplies the authoritative MLB ID. Never resolve a starter
    # again by fuzzy name lookup when building a game-level projection.
    metrics = sm.get_pitcher_metrics(pitcher_name, pitcher_id=pitcher_id)
    if not metrics or metrics.get("error"):
        return None, None, "UNKNOWN"

    # Bullpen day detection
    role = metrics.get("validated_role", "UNKNOWN")
    avg_ip_l3 = metrics.get("avg_ip_l3")
    if role != "SP" and (avg_ip_l3 is None or avg_ip_l3 < MIN_IP_STARTER):
        return None, None, "BULLPEN"

    # Get overall FIP first
    fip = metrics.get("fip")
    if fip is None:
        era = metrics.get("era")
        try:
            fip = float(era)
        except (TypeError, ValueError):
            return None, None, role
    season_ip = _decimal_ip(metrics.get("innings_pitched"))
    fip = _shrink_fip(fip, season_ip)
    if fip is None:
        return None, None, role

    # Park, weather, and platoon splits can be useful research context, but
    # they are not independently validated enough to move a market price here.
    # Using the same sample-shrunk season FIP for scoring avoids double-counting
    # a shared park/weather environment as an artificial side edge.
    return fip, fip, role


def _compute_pitcher_shift(home_fip_adj: float | None, away_fip_adj: float | None) -> float:
    """
    Convert adjusted FIPs into a home-win-prob shift.
    1-run FIP gap ≈ 7% win prob. Scaled to ~6 IP avg starter workload.
    """
    if (home_fip_adj is None or away_fip_adj is None or
            not math.isfinite(home_fip_adj) or not math.isfinite(away_fip_adj) or
            home_fip_adj <= 0 or away_fip_adj <= 0):
        return 0.0
    run_swing = (away_fip_adj - home_fip_adj) * (6.0 / 9.0)
    shift = run_swing * 0.045
    return max(-0.06, min(0.06, shift))


# ── Situational nudges ───────────────────────────────────────────────────────

def _form_nudge(s: dict) -> float:
    """Small win-prob nudge from last-10 form vs season baseline."""
    if not s or s.get("last10_pct") is None:
        return 0.0
    return max(-0.03, min(0.03, (s["last10_pct"] - (s.get("win_pct") or 0.5)) * 0.30))


def _injury_penalty(team_name: str, injuries: dict) -> tuple[float, list]:
    """Penalty for significant absences (OUT / 60-day IL). Capped at 6%."""
    rows = injuries.get(_norm(team_name), [])
    sig = [r["name"] for r in rows
           if any(k in r["status_norm"] for k in ("out", "60-day", "injured list"))]
    pen = min(0.06, 0.02 * len(sig))
    return pen, sig[:4]


def _bullpen_fatigue_nudge(team_id: int) -> float:
    """
    Approximate bullpen fatigue from team-level bullpen ERA tier.
    WEAK bullpen → higher fatigue risk (arms are worse, more likely to be overused).
    Not perfect (can't track individual reliever usage), but adds signal.
    """
    try:
        bp = sm.get_bullpen_stats(team_id)
        tier = bp.get("tier", "AVERAGE")
        if tier == "WEAK":
            return -0.015
        elif tier == "AVERAGE":
            return -0.005
        # ELITE / SOLID → no penalty (or slight boost absorbed elsewhere)
    except Exception:
        pass
    return 0.0


def _offensive_quality_nudge(h_off: dict, a_off: dict) -> float:
    """
    Win-prob shift from comparing team offensive quality.
    Uses wRC+, ISO, BB%, K% to estimate which lineup is stronger.
    Capped at ±2%.
    """
    if not h_off or not a_off:
        return 0.0
    h_score = (h_off.get("wrc_plus", 100) / 100) * (1 + h_off.get("iso", 0)) * (1 + h_off.get("bb_pct", 8) / 100)
    a_score = (a_off.get("wrc_plus", 100) / 100) * (1 + a_off.get("iso", 0)) * (1 + a_off.get("bb_pct", 8) / 100)
    # K% penalty: higher K% = worse
    h_score *= (1 - max(0, h_off.get("k_pct", 22) - 20) * 0.005)
    a_score *= (1 - max(0, a_off.get("k_pct", 22) - 20) * 0.005)
    if h_score + a_score == 0:
        return 0.0
    # Normalize to win-prob shift: positive = home offense better
    raw = (h_score - a_score) / (h_score + a_score) * 0.04
    return max(-0.02, min(0.02, raw))


def _pitcher_venue_nudge(pitcher_id: int | None, is_home: bool) -> float:
    """
    Win-prob nudge from pitcher's home vs away split.
    If pitcher is significantly better at home and is pitching home → boost.
    Capped at ±2%.
    """
    if not pitcher_id:
        return 0.0
    try:
        splits = sm.get_pitcher_venue_splits(pitcher_id)
        if not splits:
            return 0.0
        home_fip = splits.get("home_fip")
        away_fip = splits.get("away_fip")
        home_ip  = splits.get("home_ip", 0) or 0
        away_ip  = splits.get("away_ip", 0) or 0
        if not home_fip or not away_fip or home_ip < 10 or away_ip < 10:
            return 0.0
        fip_gap = away_fip - home_fip  # positive = better at home
        # Weight by sample size (more IP = more reliable)
        ip_total = home_ip + away_ip
        weight = min(1.0, ip_total / 80)  # full weight at 80+ IP
        shift = (fip_gap * 0.007) * weight  # 1-run FIP gap ≈ 0.7% per full weight
        if is_home:
            return max(-0.02, min(0.02, shift))  # home advantage if better at home
        else:
            return max(-0.02, min(0.02, -shift))  # penalty if better at home but pitching away
    except Exception:
        return 0.0


def _h2h_nudge(team_a_id: int, team_b_id: int) -> float:
    """
    Win-prob nudge from season series record between the two teams.
    Positive = team_a has won more. Capped at ±1%.
    """
    try:
        h2h = sm.get_team_h2h_record(team_a_id, team_b_id)
        if not h2h or h2h.get("games_played", 0) < 2:
            return 0.0
        a_wins = h2h["team_a_wins"]
        b_wins = h2h["team_b_wins"]
        total  = h2h["games_played"]
        # Only meaningful if there's a clear pattern (>=3 games or >60% win rate)
        a_pct = a_wins / total
        if total < 3 or 0.35 <= a_pct <= 0.65:
            return 0.0  # too close or too few games to matter
        # Magnitude scales with sample: more games = more signal
        scale = min(1.0, total / 10)  # full weight at 10+ games
        shift = (a_pct - 0.5) * 0.02 * scale
        return max(-0.01, min(0.01, shift))
    except Exception:
        return 0.0


def _bullpen_enhanced_nudge(team_id: int) -> float:
    """
    Enhanced bullpen nudge using last-7-day data.
    Combines ERA tier with fatigued pitcher count.
    Capped at ±3%.
    """
    try:
        bp = sm.get_bullpen_stats(team_id)
        if not bp:
            return 0.0
        sample = bp.get("sample", "season")
        era = bp.get("era")
        fatigued = bp.get("fatigued_count", 0) or 0
        tier = bp.get("tier", "AVERAGE")
        nudge = 0.0
        if sample == "l7":
            # Last-7-day data available — use it
            if era is not None:
                if era >= 6.0:
                    nudge -= 0.02
                elif era >= 5.0:
                    nudge -= 0.012
                elif era <= 2.5:
                    nudge += 0.01
                elif era <= 3.2:
                    nudge += 0.005
            # Fatigued arms penalty
            if fatigued >= 4:
                nudge -= 0.01
            elif fatigued >= 3:
                nudge -= 0.005
        else:
            # Season fallback — use tier
            if tier == "WEAK":
                nudge -= 0.015
            elif tier == "AVERAGE":
                nudge -= 0.005
            elif tier == "ELITE":
                nudge += 0.005
        return max(-0.03, min(0.03, nudge))
    except Exception:
        return 0.0


def _bullpen_signal(bullpen: dict) -> float:
    """Small, sample-shrunk bullpen quality signal for one team.

    The live feed only supports a reliable relief-only sample over the prior
    seven days. Generic "appeared in 3 days" counts are not fatigue, so they
    intentionally do not alter a win probability here.
    """
    if not bullpen or bullpen.get("sample") != "l7":
        return 0.0
    innings = float(bullpen.get("total_ip") or 0)
    era = _finite_float(bullpen.get("era"))
    if era is None or innings < 12:
        return 0.0
    shrunk_era = (era * innings + LEAGUE_FIP * 25.0) / (innings + 25.0)
    return max(-0.012, min(0.012, (LEAGUE_FIP - shrunk_era) * 0.006))


def _offense_signal(home: dict, away: dict) -> float:
    """Conservative season offense residual, independent of fake wRC+ fields."""
    if not home or not away:
        return 0.0
    try:
        h_ops, a_ops = float(home.get("ops") or 0), float(away.get("ops") or 0)
        games = min(float(home.get("games") or 0), float(away.get("games") or 0))
    except (TypeError, ValueError):
        return 0.0
    if h_ops <= 0 or a_ops <= 0 or games < 15:
        return 0.0
    sample_weight = min(1.0, games / 60.0)
    shift = (h_ops - a_ops) * 0.09 * sample_weight
    return max(-0.012, min(0.012, shift))


def _usable_bullpen_era(bullpen: dict) -> tuple[float, bool]:
    """Return a regression-safe relief ERA and whether the recent sample qualified."""
    if not bullpen:
        return LEAGUE_FIP, False
    recent = bullpen.get("sample") == "l7" and float(bullpen.get("total_ip") or 0) >= 12
    try:
        era = float(bullpen.get("era") or LEAGUE_FIP)
    except (TypeError, ValueError):
        era = LEAGUE_FIP
    prior_ip = 25.0 if recent else 60.0
    sample_ip = float(bullpen.get("total_ip") or 0) if recent else 10.0
    return ((era * sample_ip + LEAGUE_FIP * prior_ip) / (sample_ip + prior_ip), recent)


def _project_team_runs(offense: dict, opposing_starter: dict, opposing_fip: float | None,
                       opposing_bullpen: dict, park_factor: float, is_home: bool) -> dict:
    """Transparent expected-runs projection for one offense."""
    games = float((offense or {}).get("games") or 0)
    try: raw_rpg = float((offense or {}).get("runs_pg") or LEAGUE_RUNS_PER_GAME)
    except (TypeError, ValueError): raw_rpg = LEAGUE_RUNS_PER_GAME
    offense_weight = min(.82, max(.25, games / 120.0))
    offense_rpg = raw_rpg * offense_weight + LEAGUE_RUNS_PER_GAME * (1 - offense_weight)

    try: starter_ip = float((opposing_starter or {}).get("avg_ip_l3") or 5.3)
    except (TypeError, ValueError): starter_ip = 5.3
    starter_ip = max(3.0, min(6.5, starter_ip))
    starter_fip = float(opposing_fip or LEAGUE_FIP)
    bullpen_era, bullpen_qualified = _usable_bullpen_era(opposing_bullpen)
    pitching_multiplier = ((starter_fip / LEAGUE_FIP) * starter_ip
                           + (bullpen_era / LEAGUE_FIP) * (9 - starter_ip)) / 9
    pitching_multiplier = max(.78, min(1.25, pitching_multiplier))
    park = max(.90, min(1.12, float(park_factor or 1.0)))
    home_run_bonus = 1.012 if is_home else .988
    expected = max(2.2, min(7.2, offense_rpg * pitching_multiplier * park * home_run_bonus))
    return {
        "expected_runs": round(expected, 2), "offense_rpg": round(offense_rpg, 2),
        "starter_ip": round(starter_ip, 1), "starter_fip": round(starter_fip, 2),
        "bullpen_era": round(bullpen_era, 2), "bullpen_qualified": bullpen_qualified,
    }


def _runs_win_probability(home_runs: float, away_runs: float) -> float:
    """Convert paired expected runs into a home win probability."""
    home = max(.1, float(home_runs)); away = max(.1, float(away_runs))
    exponent = 1.83
    return home ** exponent / (home ** exponent + away ** exponent)


def _moneyline_scorecard(rec_is_home: bool, starter_shift: float, offense_shift: float,
                         bullpen_shift: float, talent_shift: float, park_factor: float,
                         lineups_confirmed: bool, reliability: float) -> dict:
    """Screenshot-style 0-100 matchup score with fixed, visible weights."""
    direction = 1 if rec_is_home else -1
    signed_inputs = {
        "starting_pitching": starter_shift * direction / .06,
        "confirmed_offense": offense_shift * direction / .05,
        "bullpen": bullpen_shift * direction / .024,
        "team_quality": talent_shift * direction / .12,
        "defense": 0.0,
        "environment": 0.0,
        "schedule": (.35 if rec_is_home else -.10),
    }
    names = {
        "starting_pitching": "Starting pitching", "confirmed_offense": "Confirmed lineup / offense",
        "bullpen": "Bullpen quality", "team_quality": "Long-term team quality",
        "defense": "Defense and catching", "environment": "Park and weather",
        "schedule": "Home field / schedule",
    }
    details = {
        "starting_pitching": "Projected starter run prevention and innings",
        "confirmed_offense": "Season offense, activated only after lineup confirmation",
        "bullpen": "Regression-safe relief quality; recent sample must reach 12 IP",
        "team_quality": "Pythagorean and season record strength",
        "defense": "Neutral until a validated defensive feed is available",
        "environment": f"Shared run environment · {float(park_factor or 1):.2f} park factor",
        "schedule": "Home-field run and batting-last advantage" if rec_is_home else "Road-side scheduling context",
    }
    factors, total = [], 0.0
    for key, weight in MONEYLINE_FACTOR_WEIGHTS.items():
        available = key not in ("defense",) and (lineups_confirmed or key != "confirmed_offense")
        normalized = max(-1.0, min(1.0, signed_inputs[key])) if available else 0.0
        impact = round(normalized * weight)
        total += impact
        factors.append({"key": key, "name": names[key], "impact": impact, "weight": weight,
                        "available": available, "detail": details[key]})
    score = max(0, min(100, round(50 + total * .5)))
    label = "Favorable" if score >= 67 else ("Unfavorable" if score <= 33 else "Neutral")
    coverage = sum(f["weight"] for f in factors if f["available"]) / 100
    return {"score": score, "label": label, "coverage": round(coverage * reliability, 2), "factors": factors}


def _model_reliability(lineups_confirmed: bool, home_role: str, away_role: str,
                       home_fip: float | None, away_fip: float | None,
                       home_bp: dict, away_bp: dict, home_strength: dict, away_strength: dict) -> float:
    """Reliability governs how much the model may move the market baseline."""
    score = 1.0
    if not lineups_confirmed:
        score *= 0.55
    if home_role != "SP" or away_role != "SP":
        score *= 0.55
    if home_fip is None or away_fip is None:
        score *= 0.60
    if min(home_strength.get("gp", 0) or 0, away_strength.get("gp", 0) or 0) < 25:
        score *= 0.75
    def usable_bullpen(bp: dict) -> bool:
        return bool(
            bp and bp.get("sample") == "l7" and bp.get("model_usable", True)
            and (_finite_float(bp.get("total_ip")) or 0) >= 12
        )
    if not usable_bullpen(home_bp) or not usable_bullpen(away_bp):
        score *= 0.88
    return round(max(0.0, min(1.0, score)), 3)


def _dynamic_anchor(game_time_dt: _dt) -> float:
    """
    Dynamic market anchoring: allow a modest residual early, then lean more
    heavily on the market as game time approaches.
    """
    now = _dt.now(_tz.utc)
    hours_to_game = (game_time_dt - now).total_seconds() / 3600
    if hours_to_game < ANCHOR_SWING_HOURS:
        return MARKET_ANCHOR_LATE
    return MARKET_ANCHOR_EARLY


def _game_volatility(lineups_confirmed: bool, home_role: str, away_role: str,
                     home_fip: float | None, away_fip: float | None,
                     home_bp: dict, away_bp: dict, park_factor: float,
                     weather: dict) -> tuple[int, str, list[str]]:
    """Score how stable a single-game projection is, independent of the side."""
    score, reasons = 0, []
    if not lineups_confirmed:
        score += 18; reasons.append("lineups are not confirmed")
    if home_role in ("BULLPEN", "UNKNOWN") or away_role in ("BULLPEN", "UNKNOWN"):
        score += 22; reasons.append("starter role is uncertain or points to a bullpen game")
    if home_fip is None or away_fip is None:
        score += 10; reasons.append("one or both starter projections are incomplete")
    if park_factor >= 1.05:
        score += 7; reasons.append("the park amplifies scoring variance")
    if weather and not weather.get("dome") and not weather.get("error"):
        wind = weather.get("speed_mph", 0) or 0
        if wind >= 18:
            score += 12; reasons.append("extreme wind can swing run scoring")
        elif wind >= 12:
            score += 6; reasons.append("meaningful wind adds scoring uncertainty")
    score = min(100, score)
    return score, ("HIGH" if score >= 40 else "MEDIUM" if score >= 18 else "LOW"), reasons


def _fair_american_odds(probability: float) -> int | None:
    """Fair no-vig American price from a model probability."""
    if probability <= 0 or probability >= 1:
        return None
    return round(-100 * probability / (1 - probability)) if probability >= 0.5 else round(100 * (1 - probability) / probability)


def _game_archetype(home_fip: float | None, away_fip: float | None,
                    park_factor: float, volatility: str, home_bp: dict, away_bp: dict) -> str:
    if volatility == "HIGH":
        return "HIGH VARIANCE"
    if home_fip is not None and away_fip is not None and home_fip <= 3.7 and away_fip <= 3.7:
        return "PITCHING DUEL"
    if park_factor >= 1.05 and ((home_fip or 4.0) >= 4.0 or (away_fip or 4.0) >= 4.0):
        return "OFFENSIVE SHOOTOUT"
    return "BALANCED MATCHUP"


def _starter_research_profile(pitcher_name: str | None, pitcher_id: int | None) -> dict:
    """Raw, user-facing starter data; separate from the model's own scoring."""
    if not pitcher_name:
        return {"name": "TBD"}
    metrics = sm.get_pitcher_metrics(pitcher_name, pitcher_id=pitcher_id) or {}
    advanced = sm.get_pitcher_advanced_stats(pitcher_id) if pitcher_id else {}
    recent = metrics.get("last_5_starts") or []
    return {
        "name": metrics.get("name", pitcher_name), "hand": metrics.get("hand", "?"),
        "era": metrics.get("era"), "fip": metrics.get("fip"), "whip": metrics.get("whip"),
        "k_per_9": metrics.get("k_per_9"), "bb_per_9": metrics.get("bb_per_9"),
        "hr_per_9": metrics.get("hr_per_9"), "k_rate": metrics.get("season_k_rate"),
        "ground_ball_rate": metrics.get("ground_ball_rate"), "role": metrics.get("validated_role"),
        "avg_ip_l3": metrics.get("avg_ip_l3"), "workload_risk": metrics.get("workload_risk", False),
        "advanced": advanced, "recent_starts": recent[:5],
        "arsenal": sm.get_pitcher_arsenal(pitcher_id)[:4] if pitcher_id else [],
    }


# ── Main scorer ──────────────────────────────────────────────────────────────

def get_moneyline_plays(game_date: str | None = None, force_odds: bool = False,
                        require_lineups: bool = True, include_passes: bool = False,
                        log_results: bool = True) -> list[dict]:
    """
    One rich read per game whose FULL lineup is posted (both teams). For each, we
    estimate both teams' win probability, pick the model's favored side, and build
    a written insight on why.
    """
    date_str  = game_date or vortextime.vortex_board_day()
    # Moneyline never scores from an old probable-starter cache.
    schedule  = sm.get_todays_schedule(game_date=date_str, fresh=True)
    market_events = _fetch_moneylines_v4(force=force_odds)
    standings = sm.get_standings()
    # Generic injury lists lack player-value and lineup certainty, so they are
    # neither scored nor used as an explanation for a moneyline bet.
    injuries  = {}
    # Fetch lineups once — reused for posted check and handedness
    lineups_data = sm.get_lineups_data(date_str)
    posted = set()
    if lineups_data:
        for de in lineups_data.get("dates", []):
            for g in de.get("games", []):
                pk = g.get("gamePk")
                lus = g.get("lineups") or {}
                if pk and sm.has_confirmed_batting_order(lus.get("homePlayers") or []) and sm.has_confirmed_batting_order(lus.get("awayPlayers") or []):
                    posted.add(pk)
    if not schedule or not market_events:
        return []

    plays = []
    now_utc = _dt.now(_tz.utc)
    for pk, g in schedule.items():
        if require_lineups and pk not in posted:
            continue
        try:
            first_pitch = _dt.fromisoformat((g.get("game_utc") or "").replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        # /ml shares this scorer with the automation. A stale cached lineup
        # must never make a live or completed game eligible.
        if first_pitch <= now_utc:
            continue
        home_name, away_name = g["home_team_name"], g["away_team_name"]
        home_abbr, away_abbr = g.get("home_abbr", ""), g.get("away_abbr", "")
        market_event = _match_market_event(market_events, home_name, away_name, g.get("game_utc", ""))
        if not market_event:
            continue
        h_line = dict(market_event["home_line"])
        a_line = dict(market_event["away_line"])
        h_line.update({"is_home": True, "opp": away_name, "commence_time": market_event["commence_time"], "event_id": market_event.get("event_id")})
        a_line.update({"is_home": False, "opp": home_name, "commence_time": market_event["commence_time"], "event_id": market_event.get("event_id")})

        home_id, away_id = g["home_team_id"], g["away_team_id"]

        h_s = standings.get(home_id, {})
        a_s = standings.get(away_id, {})
        h_str = _blend_team_strength(h_s)
        a_str = _blend_team_strength(a_s)

        # Park factor
        park_factor = sm.PARK_FACTOR.get(home_name, 1.00)

        # Weather
        weather = {}
        if home_abbr:
            try:
                weather = sm.get_game_weather(home_abbr, h_line.get("commence_time", ""))
            except Exception:
                weather = {}

        # Umpire effects are not part of the calibrated moneyline feature set.
        ump_tier = None

        # Bullpen tiers
        h_bp, a_bp = {}, {}
        try:
            h_bp = sm.get_bullpen_stats(home_id)
            a_bp = sm.get_bullpen_stats(away_id)
            h_bp_tier = h_bp.get("tier", "AVERAGE") if h_bp else "AVERAGE"
            a_bp_tier = a_bp.get("tier", "AVERAGE") if a_bp else "AVERAGE"
        except Exception:
            h_bp_tier, a_bp_tier = "AVERAGE", "AVERAGE"

        # Team offensive profiles (ISO, BB%, K%, wRC+)
        try:
            h_off = sm.get_team_offensive_profile(home_id)
            a_off = sm.get_team_offensive_profile(away_id)
        except Exception:
            h_off, a_off = {}, {}

        # Pitcher venue splits
        h_pid = g.get("home_pitcher_id")
        a_pid = g.get("away_pitcher_id")

        # Matchup-aware pitcher adjustments
        h_fip_adj, h_fip_display, h_role = _pitcher_adjustment(
            g["home_pitcher"], g.get("home_pitcher_id"),
            away_id, date_str, park_factor, weather, lineups_data)
        a_fip_adj, a_fip_display, a_role = _pitcher_adjustment(
            g["away_pitcher"], g.get("away_pitcher_id"),
            home_id, date_str, park_factor, weather, lineups_data)
        volatility_score, volatility, volatility_reasons = _game_volatility(
            pk in posted, h_role, a_role, h_fip_display, a_fip_display,
            h_bp, a_bp, park_factor, weather)
        home_starter_profile = _starter_research_profile(g.get("home_pitcher"), h_pid)
        away_starter_profile = _starter_research_profile(g.get("away_pitcher"), a_pid)

        # v5 projects each offense's runs against the opposing starter and
        # bullpen, then blends that independent baseball estimate with stable
        # team talent before the market/reliability calibration below.
        talent_base = _log5(h_str, a_str)
        talent_shift = talent_base - 0.5
        starter_shift = _compute_pitcher_shift(h_fip_adj, a_fip_adj)
        bullpen_shift = _bullpen_signal(h_bp) - _bullpen_signal(a_bp)
        home_runs_projection = _project_team_runs(
            h_off, away_starter_profile, a_fip_adj, a_bp, park_factor, True)
        away_runs_projection = _project_team_runs(
            a_off, home_starter_profile, h_fip_adj, h_bp, park_factor, False)
        runs_home_prob = _runs_win_probability(
            home_runs_projection["expected_runs"], away_runs_projection["expected_runs"])
        offense_shift = max(-.05, min(.05, runs_home_prob - .5 - starter_shift - bullpen_shift))
        h_pen, h_inj = 0.0, []
        a_pen, a_inj = 0.0, []
        injury_shift = 0.0  # display-only until lineup/value weighted
        form_shift = venue_shift = h2h_shift = 0.0
        raw = .68 * runs_home_prob + .32 * talent_base
        raw = max(HARD_BOUND_LOW, min(HARD_BOUND_HIGH, raw))

        # Market anchoring: no-vig consensus is the baseline. Reliability
        # controls the model's permission to move it.
        market_home = h_line["fair_implied"]
        game_time_dt = None
        ct = h_line.get("commence_time", "")
        if ct:
            try:
                game_time_dt = _dt.fromisoformat(ct.replace("Z", "+00:00"))
            except Exception:
                pass
        reliability = _model_reliability(pk in posted, h_role, a_role, h_fip_display, a_fip_display,
                                         h_bp, a_bp, h_s, a_s)
        if home_starter_profile.get("workload_risk") or away_starter_profile.get("workload_risk"):
            # A starter returning from an extended gap is too uncertain for a
            # pre-game moneyline recommendation, even if the market has a line.
            reliability = min(reliability, MIN_RELIABILITY - 0.01)
        anchor = _dynamic_anchor(game_time_dt) if game_time_dt else MARKET_ANCHOR_EARLY
        anchor = min(0.92, anchor + (1.0 - reliability) * 0.16)

        raw_gap   = raw - market_home
        uncertain = abs(raw_gap) >= SANITY_CAP
        context_shift = max(-MAX_CONTEXT_SHIFT, min(MAX_CONTEXT_SHIFT, raw_gap))
        model_home = max(HARD_BOUND_LOW, min(HARD_BOUND_HIGH,
            market_home + (1.0 - anchor) * context_shift))

        # Snapshot against the exact market event, never a recycled gamePk from
        # a previous model version or season.
        opening_key = f"{MODEL_VERSION}:{date_str}:{market_event.get('event_id') or pk}"
        _snapshot_opening_line(opening_key, market_home, int(h_line["odds"]))

        # RLM trap detection
        model_favors_home = model_home >= 0.5
        rlm_trap = _is_rlm_trap(opening_key, model_favors_home, market_home)

        # Determine recommendation
        rec_is_home = model_favors_home
        if rec_is_home:
            rec_team, rec_opp = home_name, away_name
            rec_pct, opp_pct  = model_home, 1 - model_home
            line, rec_pitcher, opp_pitcher = h_line, g["home_pitcher"], g["away_pitcher"]
            rec_fip, opp_fip = h_fip_display, a_fip_display
            rec_s, opp_s = h_s, a_s
            inj_rec, inj_opp = h_inj, a_inj
        else:
            rec_team, rec_opp = away_name, home_name
            rec_pct, opp_pct  = 1 - model_home, model_home
            line, rec_pitcher, opp_pitcher = a_line, g["away_pitcher"], g["home_pitcher"]
            rec_fip, opp_fip = a_fip_display, h_fip_display
            rec_s, opp_s = a_s, h_s
            inj_rec, inj_opp = a_inj, h_inj

        lean = abs(model_home - market_home)

        # Confidence % — sigmoid calibrated from edge size
        decimal_odds = (1 + line["odds"] / 100) if line["odds"] > 0 else (1 + 100 / abs(line["odds"]))
        expected_value = rec_pct * decimal_odds - 1
        # This is the selected team's model win probability. Edge quality is
        # represented by the tier below, rather than a second "confidence" %.
        confidence_pct = round(rec_pct * 100, 1)
        direction = 1 if rec_is_home else -1
        factor_scores = {
            "pitching": round(starter_shift * direction * 100, 1),
            "offense": round(offense_shift * direction * 100, 1),
            "bullpen": round(bullpen_shift * direction * 100, 1),
            "team_quality": round(talent_shift * direction * 100, 1),
            "market_value": round((rec_pct - line["fair_implied"]) * 100, 1),
        }
        uncertainty_buffer = 0.008 + (1.0 - reliability) * 0.045 + volatility_score * 0.00025
        adjusted_edge = max(0.0, lean - uncertainty_buffer)
        scorecard = _moneyline_scorecard(
            rec_is_home, starter_shift, offense_shift, bullpen_shift,
            talent_shift, park_factor, pk in posted, reliability)
        confidence_score = round(max(0, min(100,
            reliability * 100 * (0.45 + min(0.35, abs(factor_scores["market_value"]) * 8))
            - volatility_score * 0.20 - (15 if uncertain else 0))))
        confidence_band = "HIGH" if confidence_score >= 75 else "MEDIUM" if confidence_score >= 55 else "LOW"
        archetype = _game_archetype(h_fip_display, a_fip_display, park_factor, volatility, h_bp, a_bp)

        # Tier classification
        if volatility == "HIGH":
            tier = "PASS"
        elif reliability < MIN_RELIABILITY:
            tier = "PASS"
        elif uncertain:
            tier = "UNCERTAIN"
        elif rlm_trap:
            tier = "PASS"
        elif adjusted_edge >= MIN_STRONG_EDGE and rec_pct >= MIN_STRONG_WIN_PROB and expected_value >= MIN_STRONG_EV:
            tier = "STRONG"
        elif adjusted_edge >= MIN_LEAN_EDGE and rec_pct >= MIN_LEAN_WIN_PROB and expected_value >= MIN_LEAN_EV:
            tier = "LEAN"
        else:
            tier = "PASS"

        play = {
            "game_pk":       pk,
            "market_event_id": market_event.get("event_id"),
            "model_version": MODEL_VERSION,
            "reliability":   reliability,
            "commence_time": line["commence_time"],
            # The web researcher supports either side of a matchup, not only
            # the model's recommended team.
            "home_team":     home_name,
            "away_team":     away_name,
            "home_abbr":     home_abbr,
            "away_abbr":     away_abbr,
            "home_pct":      round(model_home * 100, 1),
            "away_pct":      round((1 - model_home) * 100, 1),
            "raw_home_pct":  round(raw * 100, 1),
            "market_anchor": round(anchor, 3),
            "home_market_prob": round(h_line["fair_implied"] * 100, 1),
            "away_market_prob": round(a_line["fair_implied"] * 100, 1),
            "home_odds":     int(h_line["odds"]),
            "away_odds":     int(a_line["odds"]),
            "home_pitcher":  g["home_pitcher"] or "TBD",
            "away_pitcher":  g["away_pitcher"] or "TBD",
            "home_fip":      h_fip_display,
            "away_fip":      a_fip_display,
            "home_starter_profile": home_starter_profile,
            "away_starter_profile": away_starter_profile,
            "home_record":   f"{h_s.get('wins','?')}-{h_s.get('losses','?')}",
            "away_record":   f"{a_s.get('wins','?')}-{a_s.get('losses','?')}",
            "home_offense":  h_off,
            "away_offense":  a_off,
            "home_bullpen":  h_bp,
            "away_bullpen":  a_bp,
            "lineups_confirmed": pk in posted,
            "rec_team":      rec_team,
            "opponent":      rec_opp,
            "rec_is_home":   rec_is_home,
            "rec_abbr":      home_abbr if rec_is_home else away_abbr,
            "opp_abbr":      away_abbr if rec_is_home else home_abbr,
            "rec_pct":       round(rec_pct * 100, 1),
            "raw_model_pct": round((raw if rec_is_home else 1 - raw) * 100, 1),
            "opp_pct":       round(opp_pct * 100, 1),
            "market_prob":   round(line["fair_implied"] * 100, 1),
            "odds":          int(line["odds"]),
            "sportsbook":    line.get("book", ""),
            "market_updated_at": line.get("updated_at", ""),
            "lean":          round(lean * 100, 1),
            "uncertainty_buffer": round(uncertainty_buffer * 100, 1),
            "adjusted_edge": round(adjusted_edge * 100, 1),
            "confidence_pct": confidence_pct,
            "expected_value": round(expected_value * 100, 1),
            "fair_odds":     _fair_american_odds(rec_pct),
            "factor_scores": factor_scores,
            "moneyline_score": scorecard["score"],
            "moneyline_label": scorecard["label"],
            "moneyline_coverage": scorecard["coverage"],
            "moneyline_factors": scorecard["factors"],
            "home_expected_runs": home_runs_projection["expected_runs"],
            "away_expected_runs": away_runs_projection["expected_runs"],
            "home_runs_projection": home_runs_projection,
            "away_runs_projection": away_runs_projection,
            "confidence_score": confidence_score,
            "confidence_band": confidence_band,
            "game_archetype": archetype,
            "uncertain":     uncertain,
            "rlm_trap":      rlm_trap,
            "volatility_score": volatility_score,
            "volatility":    volatility,
            "volatility_reasons": volatility_reasons,
            "tier":          tier,
            "rec_pitcher":   rec_pitcher or "TBD",
            "opp_pitcher":   opp_pitcher or "TBD",
            "rec_fip":       rec_fip, "opp_fip": opp_fip,
            "rec_record":    f"{rec_s.get('wins','?')}-{rec_s.get('losses','?')}",
            "opp_record":    f"{opp_s.get('wins','?')}-{opp_s.get('losses','?')}",
            "rec_win_pct":   round((rec_s.get('win_pct', 0.5) or 0.5) * 100, 1),
            "opp_win_pct":   round((opp_s.get('win_pct', 0.5) or 0.5) * 100, 1),
            "rec_run_diff":  rec_s.get("run_diff"),
            "opp_run_diff":  opp_s.get("run_diff"),
            "rec_wp":        rec_s.get("win_pct"),
            "opp_wp":        opp_s.get("win_pct"),
            "rec_last10":    rec_s.get("last10_pct"),
            "injuries_rec":  inj_rec,
            "injuries_opp":  inj_opp,
            "park_factor":   park_factor,
            "weather":       weather,
            "umpire_tier":   ump_tier,
            "rec_bp_tier":   h_bp_tier if rec_is_home else a_bp_tier,
            "opp_bp_tier":   a_bp_tier if rec_is_home else h_bp_tier,
            # New enhanced factors
            "rec_off":       h_off if rec_is_home else a_off,
            "opp_off":       a_off if rec_is_home else h_off,
            "rec_venue_fip": {},  # research-only feature held out of v4
            "opp_venue_fip": {},
            "h2h":           {},
            "rec_bp_era":    (h_bp.get("era") if rec_is_home else a_bp.get("era")) if (h_bp if rec_is_home else a_bp) else None,
            "opp_bp_era":    (a_bp.get("era") if rec_is_home else h_bp.get("era")) if (a_bp if rec_is_home else h_bp) else None,
            "rec_bp_fatigued": (h_bp.get("fatigued_count", 0) if rec_is_home else a_bp.get("fatigued_count", 0)) if (h_bp if rec_is_home else a_bp) else 0,
            "opp_bp_fatigued": (a_bp.get("fatigued_count", 0) if rec_is_home else h_bp.get("fatigued_count", 0)) if (a_bp if rec_is_home else h_bp) else 0,
        }
        play["insight"] = _build_insight(play)
        plays.append(play)

    plays.sort(key=lambda p: (p["tier"] != "STRONG", p["tier"] != "LEAN", -p["lean"]))
    # Filter out PASS-tier games — not actionable, just noise
    result = plays if include_passes else [p for p in plays if p["tier"] in ("STRONG", "LEAN")]

    # Save one locked-in, lineup-confirmed snapshot per game. This gives the
    # model an honest calibration baseline even on days with no qualified bets.
    if log_results and require_lineups and plays:
        _log_moneyline_snapshots(plays, date_str)

    # Only actionable bets belong in the public betting record.
    if log_results and not include_passes and result:
        _log_moneyline_predictions(result, date_str)

    return result


def get_moneyline_research_games(game_date: str | None = None) -> list[dict]:
    """Full upcoming slate for on-demand website moneyline research."""
    return get_moneyline_plays(
        game_date=game_date,
        require_lineups=False,
        include_passes=True,
        log_results=False,
    )


# ── Moneyline prediction logging ─────────────────────────────────────────────

def _ensure_moneyline_tracking_schema(cur):
    """Create and migrate the decision record without rewriting old history."""
    cur.execute("""
        CREATE TABLE IF NOT EXISTS moneyline_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            logged_at TEXT NOT NULL, game_date TEXT NOT NULL, game_pk INTEGER,
            rec_team TEXT NOT NULL, opponent TEXT NOT NULL, odds INTEGER NOT NULL,
            model_pct REAL NOT NULL, market_pct REAL NOT NULL, edge_pct REAL NOT NULL,
            confidence REAL NOT NULL, tier TEXT NOT NULL,
            rec_pitcher TEXT, opp_pitcher TEXT, rec_fip REAL, opp_fip REAL,
            park_factor REAL, result TEXT DEFAULT NULL, actual_winner TEXT DEFAULT NULL,
            graded_at TEXT DEFAULT NULL, model_version TEXT DEFAULT 'legacy',
            market_event_id TEXT, sportsbook TEXT, market_updated_at TEXT,
            raw_model_pct REAL, reliability REAL, expected_value REAL,
            factor_json TEXT, decision_at TEXT
        )
    """)
    for column in (
        "model_version TEXT DEFAULT 'legacy'", "market_event_id TEXT", "sportsbook TEXT",
        "market_updated_at TEXT", "raw_model_pct REAL", "reliability REAL",
        "expected_value REAL", "factor_json TEXT", "decision_at TEXT",
    ):
        try:
            cur.execute(f"ALTER TABLE moneyline_predictions ADD COLUMN {column}")
        except Exception:
            pass

    cur.execute("""
        CREATE TABLE IF NOT EXISTS moneyline_model_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            logged_at TEXT NOT NULL, game_date TEXT NOT NULL, game_pk INTEGER NOT NULL,
            model_version TEXT NOT NULL, market_event_id TEXT,
            home_team TEXT NOT NULL, away_team TEXT NOT NULL,
            home_model_pct REAL NOT NULL, home_market_pct REAL NOT NULL,
            home_odds INTEGER, away_odds INTEGER, reliability REAL,
            lineups_confirmed INTEGER NOT NULL, tier TEXT NOT NULL,
            actual_home_win INTEGER DEFAULT NULL, actual_winner TEXT DEFAULT NULL,
            graded_at TEXT DEFAULT NULL
        )
    """)


def _log_moneyline_predictions(plays: list[dict], game_date: str):
    """Save moneyline picks to DB for result grading."""
    import sqlite3
    from pathlib import Path
    from datetime import datetime as _dt, timezone as _tz

    db_path = Path(__file__).resolve().parent.parent / "vortex.db"
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    _ensure_moneyline_tracking_schema(cur)

    # Ensure table exists
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

    logged_at = _dt.now(_tz.utc).isoformat()
    inserted = 0
    for p in plays:
        pk = p.get("game_pk")
        # A posted bet is locked to the game, not recalculated into the
        # opposite side later in the day.
        exists = cur.execute(
            "SELECT 1 FROM moneyline_predictions WHERE game_pk=?",
            (pk,)
        ).fetchone()
        if exists:
            continue

        cur.execute("""
            INSERT INTO moneyline_predictions
              (logged_at, game_date, game_pk, rec_team, opponent, odds,
               model_pct, market_pct, edge_pct, confidence, tier,
               rec_pitcher, opp_pitcher, rec_fip, opp_fip, park_factor,
               model_version, market_event_id, sportsbook, market_updated_at,
               raw_model_pct, reliability, expected_value, factor_json, decision_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            logged_at, game_date, pk, p["rec_team"], p["opponent"], p["odds"],
            p["rec_pct"], p["market_prob"], p["lean"], p["confidence_pct"],
            p["tier"], p["rec_pitcher"], p["opp_pitcher"],
            p.get("rec_fip"), p.get("opp_fip"), p.get("park_factor"),
            p.get("model_version", MODEL_VERSION), p.get("market_event_id"),
            p.get("sportsbook"), p.get("market_updated_at"),
            p.get("raw_model_pct"), p.get("reliability"), p.get("expected_value"),
            json.dumps(p.get("factor_scores") or {}, sort_keys=True), logged_at,
        ))
        inserted += 1

    conn.commit()
    conn.close()
    if inserted:
        print(f"  Logged {inserted} moneyline predictions for grading.")


# ── Insight builder ──────────────────────────────────────────────────────────

def _log_moneyline_snapshots(plays: list[dict], game_date: str):
    """Lock one pre-game forecast per confirmed-lineup game for calibration."""
    import sqlite3

    db_path = Path(__file__).resolve().parent.parent / "vortex.db"
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    _ensure_moneyline_tracking_schema(cur)
    logged_at = _dt.now(_tz.utc).isoformat()
    inserted = 0
    for p in plays:
        pk = p.get("game_pk")
        if not pk or not p.get("lineups_confirmed"):
            continue
        exists = cur.execute(
            "SELECT 1 FROM moneyline_model_snapshots WHERE game_pk=? AND model_version=?",
            (pk, p.get("model_version", MODEL_VERSION)),
        ).fetchone()
        if exists:
            continue
        cur.execute("""
            INSERT INTO moneyline_model_snapshots
              (logged_at, game_date, game_pk, model_version, market_event_id,
               home_team, away_team, home_model_pct, home_market_pct,
               home_odds, away_odds, reliability, lineups_confirmed, tier)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            logged_at, game_date, pk, p.get("model_version", MODEL_VERSION),
            p.get("market_event_id"), p.get("home_team"), p.get("away_team"),
            p.get("home_pct"), p.get("home_market_prob"), p.get("home_odds"),
            p.get("away_odds"), p.get("reliability"), 1, p.get("tier", "PASS"),
        ))
        inserted += 1
    conn.commit()
    conn.close()
    if inserted:
        print(f"  Logged {inserted} moneyline calibration snapshots.")


def _build_insight(p: dict) -> str:
    """Natural-language explanation of why the recommended team is favored."""
    bits = []

    if p.get("volatility") == "HIGH":
        detail = "; ".join((p.get("volatility_reasons") or [])[:2])
        return (f"High-variance game — {detail or 'key inputs are unstable'}. "
                "The side may project ahead, but VORTEX will not treat it as a play.")

    if p["uncertain"]:
        return ("The model and market disagree sharply here — likely a bullpen game, "
                "late scratch, or context the model can't see. **Treat as a pass.**")

    if p.get("rlm_trap"):
        return ("Reverse line movement detected — the model favors this side but the "
                "market has moved against them. Sharp money may disagree. **Treat as a pass.**")

    if p.get("tier") == "PASS":
        if not p.get("lineups_confirmed"):
            return "Lineups are not confirmed, so this is research only — not a moneyline play."
        if p.get("reliability", 0) < MIN_RELIABILITY:
            return "The starter or bullpen inputs are not reliable enough to move off the market price. **No bet.**"
        return "The market remains the best estimate here; VORTEX found no qualified price edge. **No bet.**"

    bits = ["The v5 expected-runs projection is calibrated back toward the no-vig market before a bet is allowed"]
    rec_runs = p.get("home_expected_runs") if p.get("rec_is_home") else p.get("away_expected_runs")
    opp_runs = p.get("away_expected_runs") if p.get("rec_is_home") else p.get("home_expected_runs")
    if rec_runs is not None and opp_runs is not None:
        bits.append(f"projected runs favor {p['rec_team']} {rec_runs:.2f} to {opp_runs:.2f}")
    rf, of = p.get("rec_fip"), p.get("opp_fip")
    pitching = (p.get("factor_scores") or {}).get("pitching", 0)
    if rf is not None and of is not None and abs(pitching) >= 0.5:
        edge_word = "edge" if pitching > 0 else "headwind"
        bits.append(f"starting pitching is a {edge_word} ({p['rec_pitcher']} {rf:.2f} FIP vs {p['opp_pitcher']} {of:.2f})")
    bullpen = (p.get("factor_scores") or {}).get("bullpen", 0)
    if abs(bullpen) >= 0.3:
        bits.append("the recent, relief-only bullpen sample supports the side" if bullpen > 0
                    else "the recent, relief-only bullpen sample works against the side")
    quality = (p.get("factor_scores") or {}).get("team_quality", 0)
    if abs(quality) >= 0.5:
        bits.append("season run differential supports the side" if quality > 0
                    else "season team quality is a headwind")
    bits.append(f"the raw {p.get('lean', 0):.1f}% pricing gap is reduced to a {p.get('adjusted_edge', 0):.1f}% uncertainty-adjusted edge")
    bits.append(f"best available price: {_fmt_odds(p['odds'])} at {p.get('sportsbook') or 'the listed book'}")
    return ". ".join(bits) + "."

    # Park factor
    pf = p.get("park_factor", 1.0)
    if pf >= 1.08:
        bits.append(f"coors-level hitter park ({pf:.2f}x) inflates scoring")
    elif pf >= 1.05:
        bits.append(f"hitter-friendly park ({pf:.2f}x)")
    elif pf <= 0.92:
        bits.append(f"extreme pitcher park ({pf:.2f}x) suppresses offense")
    elif pf <= 0.96:
        bits.append(f"pitcher-friendly park ({pf:.2f}x)")

    # Weather
    w = p.get("weather", {})
    if w and not w.get("dome") and not w.get("error"):
        spd = w.get("speed_mph", 0) or 0
        hf = w.get("hitter_friendly")
        if spd >= 12:
            if hf is True:
                bits.append(f"wind blowing out at {spd:.0f} mph favors hitters")
            elif hf is False:
                bits.append(f"wind blowing in at {spd:.0f} mph favors pitchers")

    # Pitcher edge
    rf, of = p["rec_fip"], p["opp_fip"]
    if rf is not None and of is not None:
        if rf <= of - 0.4:
            bits.append(f"**{p['rec_pitcher']}** ({rf:.2f} FIP) outclasses "
                        f"{p['opp_pitcher']} ({of:.2f} FIP) on the mound")
        elif of <= rf - 0.4:
            bits.append(f"the arms are a wash-to-slight-edge against "
                        f"({p['rec_pitcher']} {rf:.2f} FIP vs {of:.2f}) — the case rests on the bats")
        else:
            bits.append(f"starters are evenly matched "
                        f"({p['rec_pitcher']} {rf:.2f} FIP vs {p['opp_pitcher']} {of:.2f})")

    # Team strength
    rd, od = p["rec_run_diff"], p["opp_run_diff"]
    if rd is not None and od is not None:
        if rd - od >= 30:
            bits.append(f"{p['rec_team']} are the stronger club ({p['rec_record']}, "
                        f"{rd:+d} run diff vs {p['opp_record']}, {od:+d})")
        elif od - rd >= 30:
            bits.append(f"on paper {p['opponent']} are stronger ({p['opp_record']}, {od:+d}), "
                        f"so this lean leans on matchup/lineup, not raw talent")
        else:
            bits.append(f"the clubs are close in quality ({p['rec_record']} vs {p['opp_record']})")

    # Form
    if p["rec_last10"] is not None and p["rec_last10"] >= 0.6:
        bits.append(f"{p['rec_team']} are hot ({int(p['rec_last10']*10)}-{10-int(p['rec_last10']*10)} L10)")

    # Injuries
    if p["injuries_opp"]:
        bits.append(f"{p['opponent']} are missing {', '.join(p['injuries_opp'][:2])}")
    if p["injuries_rec"]:
        bits.append(f"but {p['rec_team']} are without {', '.join(p['injuries_rec'][:2])}")

    # Offensive quality (wRC+, ISO)
    rec_off = p.get("rec_off", {})
    opp_off = p.get("opp_off", {})
    if rec_off and opp_off:
        h_wrc = rec_off.get("wrc_plus", 100)
        a_wrc = opp_off.get("wrc_plus", 100)
        if h_wrc - a_wrc >= 10:
            bits.append(f"{p['rec_team']} have the better lineup ({h_wrc} wRC+ vs {a_wrc})")
        elif a_wrc - h_wrc >= 10:
            bits.append(f"{p['opponent']} have the better lineup ({a_wrc} wRC+ vs {h_wrc})")
        rec_iso = rec_off.get("iso", 0)
        if rec_iso >= 0.180:
            bits.append(f"{p['rec_team']} pack pop ({rec_iso:.3f} ISO)")

    # Pitcher venue splits
    rv = p.get("rec_venue_fip", {})
    ov = p.get("opp_venue_fip", {})
    if rv and ov:
        rec_home_fip = rv.get("home_fip")
        rec_away_fip = rv.get("away_fip")
        if rec_home_fip and rec_away_fip:
            gap = abs(rec_home_fip - rec_away_fip)
            if gap >= 0.5:
                better = "home" if rec_home_fip < rec_away_fip else "road"
                bits.append(f"{p['rec_pitcher']} is notably better on the {better} "
                            f"({rec_home_fip:.2f} home FIP vs {rec_away_fip:.2f} away)")

    # Season series H2H
    h2h = p.get("h2h", {})
    if h2h and h2h.get("games_played", 0) >= 3:
        aw = h2h.get("team_a_wins", 0)
        bw = h2h.get("team_b_wins", 0)
        if aw > bw and p["rec_is_home"]:
            bits.append(f"{p['rec_team']} lead the season series {aw}-{bw}")
        elif bw > aw and not p["rec_is_home"]:
            bits.append(f"{p['rec_team']} lead the season series {bw}-{aw}")

    # Enhanced bullpen info
    rec_bp_era = p.get("rec_bp_era")
    rec_bp_fat = p.get("rec_bp_fatigued", 0)
    if rec_bp_era is not None and rec_bp_era >= 5.0:
        bits.append(f"{p['rec_team']}'s bullpen has been shaky lately ({rec_bp_era:.2f} L7 ERA)")
    if rec_bp_fat >= 4:
        bits.append(f"{p['rec_team']} bullpen is taxed ({rec_bp_fat} arms used recently)")

    if not bits:
        return f"{p['rec_team']} are a slight model lean; the line looks roughly fair."
    return ". ".join(s[0].upper() + s[1:] for s in bits) + "."


# ── Embed builders ───────────────────────────────────────────────────────────

def _fmt_odds(o: int) -> str:
    return f"+{o}" if o > 0 else str(o)


def build_moneyline_game_embed(p: dict, date_str: str):
    """Silas-style moneyline embed with structured sections."""
    import discord
    from datetime import datetime as _dt, timezone as _tz, timedelta

    tier = p["tier"]
    lean_pct = p["lean"]

    # Tier badge + score
    if tier == "STRONG":
        badge = "⭐"
        tier_label = "STRONG"
    elif tier == "LEAN":
        badge = "🟢"
        tier_label = "LEAN"
    elif tier == "SLIGHT":  # legacy rows created before the stricter tiers
        badge = "🟡"
        tier_label = "SLIGHT"
    elif tier == "UNCERTAIN":
        badge = "❓"
        tier_label = "UNCERTAIN"
    else:
        badge = "⚪"
        tier_label = "PASS"

    spot = "🏠" if p["rec_is_home"] else "✈️"
    conf_pct = p.get("confidence_pct", 0)

    # Color
    _TIER_COLOR = {"STRONG": 0x2ECC71, "LEAN": 0xF1C40F,
                   "UNCERTAIN": 0xE67E22, "PASS": 0x95A5A6}

    embed = discord.Embed(
        title=f"{badge} {p['rec_team']} {_fmt_odds(p['odds'])}  ({spot} {'home' if p['rec_is_home'] else 'away'})",
        description=f"vs {p['opponent']} · {date_str}",
        color=_TIER_COLOR.get(tier, 0x27AE60),
    )

    # ── First pitch countdown ───────────────────────────────────────────
    ct = p.get("commence_time", "")
    countdown = ""
    if ct:
        try:
            game_dt = _dt.fromisoformat(ct.replace("Z", "+00:00"))
            now = _dt.now(_tz.utc)
            diff = game_dt - now
            mins = int(diff.total_seconds() / 60)
            if 0 < mins < 120:
                countdown = f"🏟️ FIRST PITCH IN {mins} MIN"
            elif mins >= 120:
                h, m = divmod(mins, 60)
                countdown = f"🏟️ FIRST PITCH IN {h}h {m}m"
            game_et = game_dt.astimezone(_tz(timedelta(hours=-4)))
            countdown += f" · {game_et.strftime('%I:%M %p ET').lstrip('0')}"
        except Exception:
            pass

    # ── Edge ────────────────────────────────────────────────────────────
    if p["uncertain"]:
        edge_line = "model and market **disagree sharply**"
    elif p.get("rlm_trap"):
        edge_line = "**reverse line movement** — sharp money may disagree"
    elif lean_pct >= 7:
        edge_line = f"**{tier_label.lower()}** — model is {lean_pct:.1f}% above market · win probability {conf_pct}%"
    elif lean_pct >= 5:
        edge_line = f"**{tier_label.lower()}** — model is {lean_pct:.1f}% above market · win probability {conf_pct}%"
    elif lean_pct >= 4:
        edge_line = f"**{tier_label.lower()}** — model is {lean_pct:.1f}% above market · win probability {conf_pct}%"
    else:
        edge_line = f"slight lean ({lean_pct:.1f}%) · win probability {conf_pct}%"

    embed.add_field(name="— edge", value=edge_line, inline=False)

    # ── Win probability ─────────────────────────────────────────────────
    embed.add_field(
        name="— win probability",
        value=(f"**{p['rec_team']} {p['rec_pct']}%**  ·  {p['opponent']} {p['opp_pct']}%\n"
               f"market: {p['market_prob']}% for {p['rec_team']}"),
        inline=False,
    )

    # ── Teams ───────────────────────────────────────────────────────────
    rec_rd = p.get("rec_run_diff")
    opp_rd = p.get("opp_run_diff")
    rec_l10 = p.get("rec_last10")

    lines = []
    # Home team
    h_is_rec = p["rec_is_home"]
    if h_is_rec:
        h_rec = f"{p['rec_record']} ({p['rec_win_pct']}%)"
        h_rd_str = f" · {rec_rd:+d} RD" if rec_rd is not None else ""
        h_l10 = f" · L10 {int(rec_l10*10)}-{10-int(rec_l10*10)}" if rec_l10 else ""
        lines.append(f"🏠 {p['rec_abbr']}: {h_rec}{h_rd_str}{h_l10}")
    else:
        o_rec = f"{p['opp_record']} ({p['opp_win_pct']}%)"
        o_rd_str = f" · {opp_rd:+d} RD" if opp_rd is not None else ""
        o_l10_pct = p.get("rec_last10")
        lines.append(f"🏠 {p['opp_abbr']}: {o_rec}{o_rd_str}")

    # Away team
    if not h_is_rec:
        h_rec = f"{p['rec_record']} ({p['rec_win_pct']}%)"
        h_rd_str = f" · {rec_rd:+d} RD" if rec_rd is not None else ""
        h_l10 = f" · L10 {int(rec_l10*10)}-{10-int(rec_l10*10)}" if rec_l10 else ""
        lines.append(f"✈️ {p['rec_abbr']}: {h_rec}{h_rd_str}{h_l10}")
    else:
        o_rec = f"{p['opp_record']} ({p['opp_win_pct']}%)"
        o_rd_str = f" · {opp_rd:+d} RD" if opp_rd is not None else ""
        lines.append(f"✈️ {p['opp_abbr']}: {o_rec}{o_rd_str}")

    embed.add_field(name="Teams", value="\n".join(lines), inline=False)

    # ── Starting Pitchers ───────────────────────────────────────────────
    rp, op = p["rec_pitcher"], p["opp_pitcher"]
    rf, of = p.get("rec_fip"), p.get("opp_fip")
    rp_line = f"🪣 {rp} ({p['rec_abbr']})"
    if rf is not None:
        rp_line += f" {rf:.2f} FIP"
    op_line = f"🪣 {op} ({p['opp_abbr']})"
    if of is not None:
        op_line += f" {of:.2f} FIP"
    embed.add_field(name="Starting Pitchers", value=f"{rp_line}\n{op_line}", inline=False)

    # ── Edge Factors ────────────────────────────────────────────────────
    factor_lines = []

    # Pitcher matchup
    if rf is not None and of is not None:
        if rf <= of - 0.4:
            factor_lines.append(f"✅ {rp} outclasses {op} (FIP gap {of - rf:.2f})")
        elif of <= rf - 0.4:
            factor_lines.append(f"⚠️ {op} has the edge over {rp} (FIP gap {rf - of:.2f})")
        else:
            factor_lines.append(f"· Pitchers evenly matched ({rp} {rf:.2f} vs {op} {of:.2f})")

    # Form
    if rec_l10 is not None and rec_l10 >= 0.6:
        factor_lines.append(f"✅ {p['rec_team']} hot (L10 {int(rec_l10*10)}-{10-int(rec_l10*10)})")

    # Injuries
    if p["injuries_opp"]:
        factor_lines.append(f"✅ {p['opponent']} missing {', '.join(p['injuries_opp'][:2])}")
    if p["injuries_rec"]:
        factor_lines.append(f"⚠️ {p['rec_team']} without {', '.join(p['injuries_rec'][:2])}")

    # Weather
    w = p.get("weather", {})
    if w and not w.get("dome") and not w.get("error"):
        spd = w.get("speed_mph", 0) or 0
        hf = w.get("hitter_friendly")
        temp = w.get("temp_f")
        effect = w.get("effect", "")
        wx_parts = []
        if spd > 0:
            wx_parts.append(f"wind {spd:.0f} mph {effect}")
        if temp:
            wx_parts.append(f"{temp}°F")
        if hf is True:
            wx_parts.append("hitter-friendly")
            factor_lines.append(f"⚠️ {', '.join(wx_parts)}")
        elif hf is False:
            wx_parts.append("pitcher-friendly")
            factor_lines.append(f"✅ {', '.join(wx_parts)}")
        elif wx_parts:
            factor_lines.append(f"· {', '.join(wx_parts)}")
    elif w and w.get("dome"):
        factor_lines.append(f"🏟️ Indoor — weather N/A")

    # Park factor
    pf = p.get("park_factor", 1.0)
    if pf >= 1.06:
        factor_lines.append(f"⚠️ Hitter park ({pf:.2f}x)")
    elif pf <= 0.94:
        factor_lines.append(f"✅ Pitcher park ({pf:.2f}x)")

    # Bullpen tiers
    rec_bp = p.get("rec_bp_tier", "AVERAGE")
    opp_bp = p.get("opp_bp_tier", "AVERAGE")
    if rec_bp in ("ELITE", "SOLID") and opp_bp in ("WEAK", "AVERAGE"):
        factor_lines.append(f"✅ Bullpen edge ({rec_bp} vs {opp_bp})")
    elif opp_bp in ("ELITE", "SOLID") and rec_bp in ("WEAK", "AVERAGE"):
        factor_lines.append(f"⚠️ Bullpen disadvantage ({rec_bp} vs {opp_bp})")

    # Team strength gap (win% difference)
    rec_wp = p.get("rec_wp")
    opp_wp = p.get("opp_wp")
    if rec_wp is not None and opp_wp is not None:
        wp_gap = rec_wp - opp_wp
        if wp_gap >= 0.08:
            factor_lines.append(f"✅ Big talent gap ({p['rec_team']} .{int(rec_wp*1000):03d} vs {p['opponent']} .{int(opp_wp*1000):03d})")
        elif wp_gap <= -0.08:
            factor_lines.append(f"⚠️ Outmatched on paper ({p['rec_team']} .{int(rec_wp*1000):03d} vs {p['opponent']} .{int(opp_wp*1000):03d})")

    # Run differential
    rec_rd = p.get("rec_run_diff")
    opp_rd = p.get("opp_run_diff")
    if rec_rd is not None and opp_rd is not None:
        if rec_rd >= 30 and opp_rd <= -10:
            factor_lines.append(f"✅ Run differential edge (+{rec_rd} vs {opp_rd})")
        elif opp_rd >= 30 and rec_rd <= -10:
            factor_lines.append(f"⚠️ Run differential deficit ({rec_rd} vs +{opp_rd})")

    # Offensive quality (wRC+, ISO, K%)
    rec_off = p.get("rec_off", {})
    opp_off = p.get("opp_off", {})
    if rec_off and opp_off:
        h_wrc = rec_off.get("wrc_plus", 100)
        a_wrc = opp_off.get("wrc_plus", 100)
        if h_wrc - a_wrc >= 10:
            factor_lines.append(f"✅ {p['rec_abbr']} offense ({h_wrc} wRC+ · {rec_off.get('iso',0):.3f} ISO)")
        elif a_wrc - h_wrc >= 10:
            factor_lines.append(f"⚠️ {p['opp_abbr']} offense ({a_wrc} wRC+ · {opp_off.get('iso',0):.3f} ISO)")
        # K% comparison
        h_k = rec_off.get("k_pct", 22)
        a_k = opp_off.get("k_pct", 22)
        if h_k <= 18 and a_k >= 24:
            factor_lines.append(f"✅ {p['rec_abbr']} contact-oriented ({h_k:.1f}% K)")
        elif a_k <= 18 and h_k >= 24:
            factor_lines.append(f"⚠️ {p['opp_abbr']} contact-oriented ({a_k:.1f}% K)")

    # Pitcher venue splits
    rv = p.get("rec_venue_fip", {})
    ov = p.get("opp_venue_fip", {})
    if rv and ov:
        rec_home_fip = rv.get("home_fip")
        rec_away_fip = rv.get("away_fip")
        opp_home_fip = ov.get("home_fip")
        opp_away_fip = ov.get("away_fip")
        if p["rec_is_home"] and rec_home_fip and opp_away_fip:
            gap = rec_home_fip - opp_away_fip
            if gap <= -0.5:
                factor_lines.append(f"✅ {rp} thrives at home ({rec_home_fip:.2f} FIP) vs {op} on road ({opp_away_fip:.2f})")
            elif gap >= 0.5:
                factor_lines.append(f"⚠️ {rp} worse at home ({rec_home_fip:.2f}) vs {op} strong on road ({opp_away_fip:.2f})")
        elif not p["rec_is_home"] and rec_away_fip and opp_home_fip:
            gap = rec_away_fip - opp_home_fip
            if gap <= -0.5:
                factor_lines.append(f"✅ {rp} strong on road ({rec_away_fip:.2f} FIP) vs {op} at home ({opp_home_fip:.2f})")
            elif gap >= 0.5:
                factor_lines.append(f"⚠️ {rp} struggles on road ({rec_away_fip:.2f}) vs {op} at home ({opp_home_fip:.2f})")

    # Season series H2H
    h2h = p.get("h2h", {})
    if h2h and h2h.get("games_played", 0) >= 3:
        aw = h2h.get("team_a_wins", 0)
        bw = h2h.get("team_b_wins", 0)
        if aw != bw:
            leader = p["rec_abbr"] if ((aw > bw) == p["rec_is_home"]) else p["opp_abbr"]
            factor_lines.append(f"📊 Season series: {leader} leads {max(aw,bw)}-{min(aw,bw)}")

    # Enhanced bullpen (L7 ERA + fatigue)
    rec_bp_era = p.get("rec_bp_era")
    opp_bp_era = p.get("opp_bp_era")
    rec_bp_fat = p.get("rec_bp_fatigued", 0)
    opp_bp_fat = p.get("opp_bp_fatigued", 0)
    if rec_bp_era is not None and opp_bp_era is not None:
        if rec_bp_era <= 3.0 and opp_bp_era >= 4.5:
            factor_lines.append(f"✅ {p['rec_abbr']} bullpen locked in ({rec_bp_era:.2f} L7)")
        elif opp_bp_era <= 3.0 and rec_bp_era >= 4.5:
            factor_lines.append(f"⚠️ {p['opp_abbr']} bullpen locked in ({opp_bp_era:.2f} L7)")
    if rec_bp_fat >= 4:
        factor_lines.append(f"⚠️ {p['rec_abbr']} bullpen taxed ({rec_bp_fat} arms recently)")
    elif opp_bp_fat >= 4:
        factor_lines.append(f"✅ {p['opp_abbr']} bullpen taxed ({opp_bp_fat} arms recently)")

    # Only show factors that are part of the v4 calculation. The detailed
    # research fields above remain available elsewhere, but never masquerade
    # as model inputs.
    factor_lines = [
        f"Market residual: {p.get('lean', 0):+.1f}% after reliability anchoring",
        f"Reliability: {p.get('reliability', 0) * 100:.0f}%",
    ]
    for label, key in (("Starting pitching", "pitching"), ("Bullpen sample", "bullpen"),
                       ("Season team quality", "team_quality")):
        value = float((p.get("factor_scores") or {}).get(key, 0) or 0)
        if abs(value) >= 0.1:
            factor_lines.append(f"{label}: {value:+.1f}%")

    if factor_lines:
        embed.add_field(name="⚡ Edge Factors", value="\n".join(factor_lines), inline=False)

    # ── Insight (short) ─────────────────────────────────────────────────
    insight = p.get("insight", "")
    if insight:
        embed.add_field(name="📝 Why", value=insight[:500], inline=False)

    # ── Footer ──────────────────────────────────────────────────────────
    embed.set_footer(text=f"⚖️ {tier_label.lower()} · edge = model vs market, not a guarantee")
    return embed


def build_moneyline_embeds(plays: list[dict], date_str: str) -> list:
    """One embed per game (Discord allows up to 10 per message)."""
    import discord
    if not plays:
        e = discord.Embed(
            title="💰 Moneyline — no confirmed-lineup games yet",
            description=(f"**{date_str}** · No games have full lineups posted right now. "
                         "Run **/ml** again closer to first pitch."),
            color=0x95A5A6)
        return [e]
    return [build_moneyline_game_embed(p, date_str) for p in plays[:10]]


# ── CLI dry run ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import io, sys
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    d = vortextime.vortex_board_day()
    pl = get_moneyline_plays(d, force_odds=True)
    print(f"Moneyline (confirmed-lineup games) for {d}: {len(pl)} game(s)")
    for p in pl:
        print(f"\n  [{p['tier']}] {p['rec_team']} {_fmt_odds(p['odds'])} vs {p['opponent']}")
        print(f"    {p['rec_team']} {p['rec_pct']}% · {p['opponent']} {p['opp_pct']}% (market {p['market_prob']}%)")
        print(f"    lean: {p['lean']}%  park: {p.get('park_factor', 1.0):.2f}x")
        print(f"    why: {p['insight']}")
