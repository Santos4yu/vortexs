"""
VORTEX — MLB Moneyline Model v2
================================
Estimates a true win probability for each side of every MLB game, compares it to
the market moneyline, and surfaces games with a real edge.

Win-probability model (the principled core):
  1. Pythagorean expectation   — each team's true talent win% from runs
                                  scored/allowed:  RS^1.83 / (RS^1.83 + RA^1.83)
  2. Log5                       — combine the two teams' talent into a head-to-head
                                  probability:  P(A) = (a - a·b) / (a + b - 2·a·b)
  3. Matchup-aware pitcher adj  — split FIP (vs LHB/RHB) blended by opposing lineup's
                                  handedness ratio, adjusted for park factor and wind.
  4. Situational nudges          — bullpen fatigue, recent form, injuries. Each is a
                                  small, capped probability adjustment.
  5. Market anchoring            — dynamic blend of model + market, shifting toward
                                  market as game time approaches (sharp money enters).

The market line is de-vigged to a fair implied probability; edge = model − implied.
Only games with edge ≥ LEAN_THRESHOLD are posted.

Public API
----------
  get_moneyline_plays(game_date=None, force_odds=False) -> list[dict]
  build_moneyline_embeds(plays, date_str) -> list[discord.Embed]
"""

import os
import json
import time
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
ODDS_TTL_SEC = 1800   # 30 min

# ── Tuning constants ─────────────────────────────────────────────────────────
LEAN_THRESHOLD     = 0.04     # blended model vs market gap to flag a lean
SANITY_CAP         = 0.16     # raw model–market gap above this = model missing context
MARKET_ANCHOR_EARLY = 0.40    # trust model more when lines are soft (>2h out)
MARKET_ANCHOR_LATE  = 0.60    # trust efficient late market more (<2h out)
ANCHOR_SWING_HOURS  = 2       # hours before game when anchor shifts to LATE
PYTHAG_EXP          = 1.83    # Bill James Pythagorean exponent for MLB
HOME_FIELD          = 0.035   # +3.5% raw win prob for home team
MIN_IP_STARTER      = 3.0     # below this avg IP = bullpen day → no pitcher adj
HARD_BOUND_LOW      = 0.05    # minimum raw model probability
HARD_BOUND_HIGH     = 0.95    # maximum raw model probability
RLM_THRESHOLD       = 0.025   # 2.5% market move against model lean = RLM trap

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


def _snapshot_opening_line(game_pk: int, fair_home: float, odds_home: int):
    """Store the first-seen line for a game as its 'opening' line."""
    opening = _load_opening_lines()
    key = str(game_pk)
    if key not in opening:
        opening[key] = {
            "fair_home": fair_home,
            "odds_home": odds_home,
            "timestamp": time.time(),
        }
        _save_opening_lines(opening)


def _is_rlm_trap(game_pk: int, model_favors_home: bool, current_fair_home: float) -> bool:
    """
    Detect reverse line movement: model favors one side but the market has
    moved against that side since opening. Indicates sharp money on the other side.
    """
    opening = _load_opening_lines()
    entry = opening.get(str(game_pk))
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

def _pitcher_adjustment(
    pitcher_name: str | None,
    pitcher_id: int | None,
    opposing_team_id: int,
    date_str: str,
    park_factor: float,
    weather: dict,
    lineups_data: dict = None,
) -> tuple[float, float | None, str]:
    """
    Matchup-aware pitcher adjustment:
      1. Fetch pitcher's FIP split vs LHB / RHB
      2. Get opposing lineup's lefty ratio
      3. Blend FIP by platoon matchup
      4. Adjust for park factor and wind
      5. Convert FIP gap to win-prob shift

    Returns (adjusted_fip, adjusted_fip, pitcher_role) where:
      adjusted_fip = park/weather-adjusted FIP for this pitcher (used by _compute_pitcher_shift)
      pitcher_role = "SP" / "BULLPEN" / "UNKNOWN"
    """
    if not pitcher_name:
        return 0.0, None, "UNKNOWN"

    metrics = sm.get_pitcher_metrics(pitcher_name)
    if not metrics or metrics.get("error"):
        return 0.0, None, "UNKNOWN"

    # Bullpen day detection
    role = metrics.get("validated_role", "UNKNOWN")
    avg_ip_l3 = metrics.get("avg_ip_l3")
    if role != "SP" and (avg_ip_l3 is None or avg_ip_l3 < MIN_IP_STARTER):
        return 0.0, None, "BULLPEN"

    # Get overall FIP first
    fip = metrics.get("fip")
    if fip is None:
        era = metrics.get("era")
        try:
            fip = float(era)
        except (TypeError, ValueError):
            return 0.0, None, role

    # Try to get split FIP (vs LHB/RHB)
    pid = pitcher_id or metrics.get("pitcher_id")
    if pid:
        splits = sm.get_pitcher_splits_by_hand(pid)
        vs_left = splits.get("vs_left", {})
        vs_right = splits.get("vs_right", {})

        fip_vs_left = vs_left.get("fip") or fip
        fip_vs_right = vs_right.get("fip") or fip

        # Get opposing lineup handedness
        lineup_hand = sm.get_lineup_handedness(date_str, opposing_team_id, _prefetched_data=lineups_data)
        lefty_ratio = lineup_hand.get("lefty_ratio", 0.5)

        # Blend FIP by platoon matchup
        blended_fip = (lefty_ratio * fip_vs_left) + ((1.0 - lefty_ratio) * fip_vs_right)
    else:
        blended_fip = fip

    # Park factor adjustment: FIP is park-neutralized, adjust back
    # Park > 1.0 = hitter-friendly = inflates FIP (pitcher worse)
    adjusted_fip = blended_fip * park_factor

    # Weather adjustment
    if weather and not weather.get("dome") and not weather.get("error"):
        wind_speed = weather.get("speed_mph", 0) or 0
        hitter_friendly = weather.get("hitter_friendly")
        if wind_speed >= 12.0:
            if hitter_friendly is True:
                adjusted_fip *= 1.04   # wind blowing out hurts pitchers
            elif hitter_friendly is False:
                adjusted_fip *= 0.97   # wind blowing in helps pitchers

    return adjusted_fip, adjusted_fip, role


def _compute_pitcher_shift(home_fip_adj: float | None, away_fip_adj: float | None) -> float:
    """
    Convert adjusted FIPs into a home-win-prob shift.
    1-run FIP gap ≈ 7% win prob. Scaled to ~6 IP avg starter workload.
    """
    if home_fip_adj is None or away_fip_adj is None:
        return 0.0
    run_swing = (away_fip_adj - home_fip_adj) * (6.0 / 9.0)
    shift = run_swing * 0.07
    return max(-0.10, min(0.10, shift))


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


def _dynamic_anchor(game_time_dt: _dt) -> float:
    """
    Dynamic market anchoring: trust model more early (lines are soft),
    trust market more as game time approaches (sharp money enters).
    """
    now = _dt.now(_tz.utc)
    hours_to_game = (game_time_dt - now).total_seconds() / 3600
    if hours_to_game < ANCHOR_SWING_HOURS:
        return MARKET_ANCHOR_LATE   # 0.60 — trust market more
    return MARKET_ANCHOR_EARLY      # 0.40 — trust model more


# ── Main scorer ──────────────────────────────────────────────────────────────

def get_moneyline_plays(game_date: str | None = None, force_odds: bool = False) -> list[dict]:
    """
    One rich read per game whose FULL lineup is posted (both teams). For each, we
    estimate both teams' win probability, pick the model's favored side, and build
    a written insight on why.
    """
    date_str  = game_date or vortextime.vortex_board_day()
    schedule  = sm.get_todays_schedule(game_date=date_str)
    lines     = _fetch_moneylines(force=force_odds)
    standings = sm.get_standings()
    injuries  = sm.get_mlb_injuries()
    # Fetch lineups once — reused for posted check and handedness
    lineups_data = sm.get_lineups_data(date_str)
    posted = set()
    if lineups_data:
        for de in lineups_data.get("dates", []):
            for g in de.get("games", []):
                pk = g.get("gamePk")
                lus = g.get("lineups") or {}
                if pk and len(lus.get("homePlayers") or []) >= 9 and len(lus.get("awayPlayers") or []) >= 9:
                    posted.add(pk)
    if not schedule or not lines:
        return []

    plays = []
    for pk, g in schedule.items():
        if pk not in posted:
            continue
        home_name, away_name = g["home_team_name"], g["away_team_name"]
        home_abbr, away_abbr = g.get("home_abbr", ""), g.get("away_abbr", "")
        h_line = lines.get(_norm(home_name))
        a_line = lines.get(_norm(away_name))
        if not h_line or not a_line:
            continue

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

        # Umpire
        ump_tier = None
        try:
            ump_lookup = sm.get_game_umpires()
            ump_name = ump_lookup.get(home_id, "")
            if ump_name:
                ump_tier = sm.UMPIRE_K_TIER.get(ump_name)
        except Exception:
            pass

        # Bullpen tiers
        try:
            h_bp = sm.get_bullpen_stats(home_id)
            a_bp = sm.get_bullpen_stats(away_id)
            h_bp_tier = h_bp.get("tier", "AVERAGE") if h_bp else "AVERAGE"
            a_bp_tier = a_bp.get("tier", "AVERAGE") if a_bp else "AVERAGE"
        except Exception:
            h_bp_tier, a_bp_tier = "AVERAGE", "AVERAGE"

        # Matchup-aware pitcher adjustments
        h_fip_adj, h_fip_display, h_role = _pitcher_adjustment(
            g["home_pitcher"], g.get("home_pitcher_id"),
            away_id, date_str, park_factor, weather, lineups_data)
        a_fip_adj, a_fip_display, a_role = _pitcher_adjustment(
            g["away_pitcher"], g.get("away_pitcher_id"),
            home_id, date_str, park_factor, weather, lineups_data)

        # Build raw probability
        raw = _log5(h_str, a_str) + HOME_FIELD
        raw += _compute_pitcher_shift(h_fip_adj, a_fip_adj)
        raw += _form_nudge(h_s) - _form_nudge(a_s)
        h_pen, h_inj = _injury_penalty(home_name, injuries)
        a_pen, a_inj = _injury_penalty(away_name, injuries)
        raw += a_pen - h_pen

        # Bullpen fatigue
        raw += _bullpen_fatigue_nudge(home_id)
        raw -= _bullpen_fatigue_nudge(away_id)

        raw = max(HARD_BOUND_LOW, min(HARD_BOUND_HIGH, raw))

        # Market anchoring (dynamic)
        market_home = h_line["fair_implied"]
        game_time_dt = None
        ct = h_line.get("commence_time", "")
        if ct:
            try:
                game_time_dt = _dt.fromisoformat(ct.replace("Z", "+00:00"))
            except Exception:
                pass
        anchor = _dynamic_anchor(game_time_dt) if game_time_dt else MARKET_ANCHOR_EARLY

        raw_gap   = raw - market_home
        uncertain = abs(raw_gap) >= SANITY_CAP
        model_home = anchor * market_home + (1 - anchor) * raw

        # Snapshot opening line for RLM detection
        _snapshot_opening_line(pk, market_home, int(h_line["odds"]))

        # RLM trap detection
        model_favors_home = model_home >= 0.5
        rlm_trap = _is_rlm_trap(pk, model_favors_home, market_home)

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

        # Tier classification
        if uncertain:
            tier = "UNCERTAIN"
        elif rlm_trap:
            tier = "PASS"
        elif lean >= 0.07:
            tier = "NOTABLE"
        elif lean >= 0.05:
            tier = "MODEST"
        elif lean >= LEAN_THRESHOLD:
            tier = "SLIGHT"
        else:
            tier = "PASS"

        play = {
            "game_pk":       pk,
            "commence_time": line["commence_time"],
            "rec_team":      rec_team,
            "opponent":      rec_opp,
            "rec_is_home":   rec_is_home,
            "rec_abbr":      home_abbr if rec_is_home else away_abbr,
            "opp_abbr":      away_abbr if rec_is_home else home_abbr,
            "rec_pct":       round(rec_pct * 100, 1),
            "opp_pct":       round(opp_pct * 100, 1),
            "market_prob":   round(line["fair_implied"] * 100, 1),
            "odds":          int(line["odds"]),
            "lean":          round(lean * 100, 1),
            "uncertain":     uncertain,
            "rlm_trap":      rlm_trap,
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
            "rec_last10":    rec_s.get("last10_pct"),
            "injuries_rec":  inj_rec,
            "injuries_opp":  inj_opp,
            "park_factor":   park_factor,
            "weather":       weather,
            "umpire_tier":   ump_tier,
            "rec_bp_tier":   h_bp_tier if rec_is_home else a_bp_tier,
            "opp_bp_tier":   a_bp_tier if rec_is_home else h_bp_tier,
        }
        play["insight"] = _build_insight(play)
        plays.append(play)

    plays.sort(key=lambda p: (p["uncertain"], p["tier"] != "NOTABLE", p["tier"] != "MODEST", -p["lean"]))
    # Filter out PASS-tier games — not actionable, just noise
    return [p for p in plays if p["tier"] != "PASS"]


# ── Insight builder ──────────────────────────────────────────────────────────

def _build_insight(p: dict) -> str:
    """Natural-language explanation of why the recommended team is favored."""
    bits = []

    if p["uncertain"]:
        return ("The model and market disagree sharply here — likely a bullpen game, "
                "late scratch, or context the model can't see. **Treat as a pass.**")

    if p.get("rlm_trap"):
        return ("Reverse line movement detected — the model favors this side but the "
                "market has moved against them. Sharp money may disagree. **Treat as a pass.**")

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
    if tier == "NOTABLE":
        badge = "⭐"
        tier_label = "NOTABLE"
    elif tier == "MODEST":
        badge = "🟢"
        tier_label = "MODEST"
    elif tier == "SLIGHT":
        badge = "🟡"
        tier_label = "SLIGHT"
    elif tier == "UNCERTAIN":
        badge = "❓"
        tier_label = "UNCERTAIN"
    else:
        badge = "⚪"
        tier_label = "PASS"

    spot = "🏠" if p["rec_is_home"] else "✈️"

    # Color
    _TIER_COLOR = {"NOTABLE": 0x2ECC71, "MODEST": 0x27AE60, "SLIGHT": 0xF1C40F,
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
        edge_line = f"**{tier_label.lower()}** — model is {lean_pct:.1f}% above market"
    elif lean_pct >= 5:
        edge_line = f"**{tier_label.lower()}** — model is {lean_pct:.1f}% above market"
    elif lean_pct >= 4:
        edge_line = f"**{tier_label.lower()}** — model is {lean_pct:.1f}% above market"
    else:
        edge_line = f"slight lean ({lean_pct:.1f}%) — line looks roughly fair"

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
