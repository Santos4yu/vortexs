"""
VORTEX — MLB Moneyline Model
============================
Estimates a true win probability for each side of every MLB game, compares it to
the market moneyline, and surfaces games with a real edge.

Win-probability model (the principled core):
  1. Pythagorean expectation   — each team's true talent win% from runs
                                  scored/allowed:  RS^1.83 / (RS^1.83 + RA^1.83)
  2. Log5                       — combine the two teams' talent into a head-to-head
                                  probability:  P(A) = (a - a·b) / (a + b - 2·a·b)
  3. Starting-pitcher adjustment — the biggest single-game lever. Each starter's
                                  quality (FIP/ERA vs league) shifts the matchup.
  4. Situational nudges          — bullpen, park/weather, rest, recent form,
                                  injuries. Each is a small, capped probability
                                  adjustment, never the primary driver.

The market line is de-vigged to a fair implied probability; edge = model − implied.
Only games with edge ≥ EDGE_THRESHOLD are posted (mirrors the NRFI auto-poster).

NOT modeled (no reliable free data): line movement, public/sharp split,
pitch-level velo/movement. We don't fake these.

Public API
----------
  get_moneyline_plays(game_date=None) -> list[dict]
  build_moneyline_embed(plays, date_str) -> discord.Embed
"""

import os
import json
import time
import math
from pathlib import Path

import requests
from dotenv import load_dotenv

import stats_mlb as sm
import vortextime

load_dotenv(Path(__file__).parent.parent / ".env")

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
ODDS_BASE    = "https://api.the-odds-api.com/v4"

# Cache the moneyline odds so the auto-poster and repeated /ml calls don't each
# burn an Odds API request. /ml passes force=True to pull a fresh line.
_ODDS_CACHE  = Path(__file__).parent / "cache" / "moneyline_odds.json"
ODDS_TTL_SEC = 1800   # 30 min

# An independent free-data model CANNOT systematically beat the MLB moneyline —
# the market already prices pitchers, bullpens, lineups and sharp money. So this
# is an HONEST "model vs market" read, not an edge generator:
#   • model probabilities are calibrated to realistic MLB single-game ranges,
#   • then anchored to the market (the best available baseline),
#   • we only surface MODEST disagreements as "leans" for context,
#   • and we SUPPRESS large disagreements as "model likely missing context"
#     (a bullpen game, a scratch) rather than calling them value.
LEAN_THRESHOLD  = 0.04   # blended model vs market gap to flag a lean
SANITY_CAP      = 0.16   # raw model–market gap above this = model is missing something
MARKET_ANCHOR   = 0.50   # weight on the market when blending (calibration)
PYTHAG_EXP      = 1.83    # Bill James Pythagorean exponent for MLB

_ODDS_SESSION = requests.Session()
_ODDS_SESSION.headers.update({"User-Agent": "VORTEX/1.0", "Accept": "application/json"})


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
    # regress small samples toward .500
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


def _pitcher_win_shift(home_fip: float | None, away_fip: float | None) -> float:
    """
    Convert the starting-pitcher FIP differential into a home-win-prob shift.
    A one-run swing in expected runs allowed ≈ ~7% win prob. FIP is runs/9, and a
    starter throws ~6 IP, so (away_fip − home_fip) × (6/9) runs × ~7%/run.
    Capped so a single start never dominates the team-talent base.
    """
    if home_fip is None or away_fip is None:
        return 0.0
    run_swing = (away_fip - home_fip) * (6.0 / 9.0)   # +ve = home pitcher better
    shift = run_swing * 0.07
    return max(-0.10, min(0.10, shift))   # one start never dominates team talent


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


# ── Odds fetch ───────────────────────────────────────────────────────────────

def _fetch_moneylines(force: bool = False) -> dict:
    """
    {normalized_team_name: {opp, is_home, odds, fair_implied, commence_time}} for
    every MLB game with an h2h market. Consensus odds = median across books.

    Cached for ODDS_TTL_SEC to conserve Odds API quota. `force=True` (used by /ml)
    bypasses the cache and pulls a fresh line.
    """
    # Serve from cache unless forced/stale.
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
        # fall back to stale cache if we have it
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
        # consensus = median price per side
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


def _norm(name: str) -> str:
    return (name or "").lower().strip()


# ── Pitcher FIP lookup ───────────────────────────────────────────────────────

def _pitcher_fip(name: str | None) -> float | None:
    """Fetch a starter's FIP (fall back to ERA) via the MLB stats engine."""
    if not name:
        return None
    try:
        m = sm.get_pitcher_metrics(name)
        if not m or m.get("error"):
            return None
        fip = m.get("fip")
        era = m.get("era")
        for v in (fip, era):
            try:
                f = float(v)
                if f > 0:
                    return f
            except (TypeError, ValueError):
                continue
    except Exception:
        return None
    return None


# ── Situational nudges ───────────────────────────────────────────────────────

def _form_nudge(s: dict) -> float:
    """Small win-prob nudge from last-10 form vs season baseline."""
    if not s or s.get("last10_pct") is None:
        return 0.0
    return max(-0.03, min(0.03, (s["last10_pct"] - (s.get("win_pct") or 0.5)) * 0.30))


def _injury_penalty(team_name: str, injuries: dict) -> tuple[float, list]:
    """
    Penalty for significant absences (OUT / 60-day IL). Capped — we surface the
    names so the human does the final 30-min lineup check, rather than over-trusting.
    """
    rows = injuries.get(_norm(team_name), [])
    sig = [r["name"] for r in rows
           if any(k in r["status_norm"] for k in ("out", "60-day", "injured list"))]
    pen = min(0.06, 0.02 * len(sig))     # cap at 6%
    return pen, sig[:4]


# ── Main scorer ──────────────────────────────────────────────────────────────

def get_moneyline_plays(game_date: str | None = None, force_odds: bool = False) -> list[dict]:
    """
    One rich read per game whose FULL lineup is posted (both teams). For each, we
    estimate both teams' win probability, pick the model's favored side, and build
    a written insight on why. Games without a confirmed full lineup are skipped —
    without the lineup we can't fairly judge the matchup.

    force_odds=True (from /ml) pulls a fresh moneyline; otherwise odds are cached.
    """
    date_str  = game_date or vortextime.vortex_board_day()
    schedule  = sm.get_todays_schedule(game_date=date_str)
    lines     = _fetch_moneylines(force=force_odds)
    standings = sm.get_standings()
    injuries  = sm.get_mlb_injuries()
    posted    = sm.get_lineups_posted(date_str)
    if not schedule or not lines:
        return []

    plays = []
    for pk, g in schedule.items():
        # Lineup gate — only games with both batting orders posted.
        if pk not in posted:
            continue
        home_name, away_name = g["home_team_name"], g["away_team_name"]
        h_line = lines.get(_norm(home_name))
        a_line = lines.get(_norm(away_name))
        if not h_line or not a_line:
            continue

        h_s = standings.get(g["home_team_id"], {})
        a_s = standings.get(g["away_team_id"], {})
        h_str = _blend_team_strength(h_s)
        a_str = _blend_team_strength(a_s)
        h_fip = _pitcher_fip(g["home_pitcher"])
        a_fip = _pitcher_fip(g["away_pitcher"])

        raw = _log5(h_str, a_str) + 0.035
        raw += _pitcher_win_shift(h_fip, a_fip)
        raw += _form_nudge(h_s) - _form_nudge(a_s)
        h_pen, h_inj = _injury_penalty(home_name, injuries)
        a_pen, a_inj = _injury_penalty(away_name, injuries)
        raw += a_pen - h_pen
        raw = max(0.30, min(0.70, raw))

        market_home = h_line["fair_implied"]
        raw_gap   = raw - market_home
        uncertain = abs(raw_gap) >= SANITY_CAP
        model_home = MARKET_ANCHOR * market_home + (1 - MARKET_ANCHOR) * raw

        rec_is_home = model_home >= 0.5
        if rec_is_home:
            rec_team, rec_opp = home_name, away_name
            rec_pct, opp_pct  = model_home, 1 - model_home
            line, rec_pitcher, opp_pitcher = h_line, g["home_pitcher"], g["away_pitcher"]
            rec_fip, opp_fip = h_fip, a_fip
            rec_s, opp_s = h_s, a_s
            inj_rec, inj_opp = h_inj, a_inj
        else:
            rec_team, rec_opp = away_name, home_name
            rec_pct, opp_pct  = 1 - model_home, model_home
            line, rec_pitcher, opp_pitcher = a_line, g["away_pitcher"], g["home_pitcher"]
            rec_fip, opp_fip = a_fip, h_fip
            rec_s, opp_s = a_s, h_s
            inj_rec, inj_opp = a_inj, h_inj

        lean = abs(model_home - market_home)
        play = {
            "game_pk":     pk,
            "commence_time": line["commence_time"],
            "rec_team":    rec_team,
            "opponent":    rec_opp,
            "rec_is_home": rec_is_home,
            "rec_pct":     round(rec_pct * 100, 1),
            "opp_pct":     round(opp_pct * 100, 1),
            "market_prob": round(line["fair_implied"] * 100, 1),
            "odds":        int(line["odds"]),
            "lean":        round(lean * 100, 1),
            "uncertain":   uncertain,
            "tier":        "UNCERTAIN" if uncertain else _tier(lean),
            "rec_pitcher": rec_pitcher or "TBD",
            "opp_pitcher": opp_pitcher or "TBD",
            "rec_fip":     rec_fip, "opp_fip": opp_fip,
            "rec_record":  f"{rec_s.get('wins','?')}-{rec_s.get('losses','?')}",
            "opp_record":  f"{opp_s.get('wins','?')}-{opp_s.get('losses','?')}",
            "rec_run_diff": rec_s.get("run_diff"),
            "opp_run_diff": opp_s.get("run_diff"),
            "rec_last10":  rec_s.get("last10_pct"),
            "injuries_rec": inj_rec,
            "injuries_opp": inj_opp,
        }
        play["insight"] = _build_insight(play)
        plays.append(play)

    plays.sort(key=lambda p: (p["uncertain"], -p["lean"]))
    return plays


def _tier(lean: float) -> str:
    if   lean >= 0.07: return "NOTABLE"
    elif lean >= 0.05: return "MODEST"
    else:              return "SLIGHT"


def _build_insight(p: dict) -> str:
    """Natural-language explanation of why the recommended team is favored."""
    bits = []
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
        bits.append(f"⚠️ but {p['rec_team']} are without {', '.join(p['injuries_rec'][:2])}")

    if p["uncertain"]:
        return ("The model and market disagree sharply here — likely a bullpen game, "
                "late scratch, or context the model can't see. **Treat as a pass.**")
    if not bits:
        return f"{p['rec_team']} are a slight model lean; the line looks roughly fair."
    return ". ".join(s[0].upper() + s[1:] for s in bits) + "."


def _fmt_odds(o: int) -> str:
    return f"+{o}" if o > 0 else str(o)


_TIER_ICON  = {"NOTABLE": "⭐", "MODEST": "🟢", "SLIGHT": "🟡", "UNCERTAIN": "❓"}
_TIER_COLOR = {"NOTABLE": 0x2ECC71, "MODEST": 0x27AE60, "SLIGHT": 0xF1C40F,
               "UNCERTAIN": 0xE67E22}


def build_moneyline_game_embed(p: dict, date_str: str):
    """One embed for a single game: both teams' win %, the pick, and the why."""
    import discord
    spot  = "🏠 home" if p["rec_is_home"] else "✈️ away"
    badge = _TIER_ICON.get(p["tier"], "🟡")
    embed = discord.Embed(
        title=f"{badge} {p['rec_team']} {_fmt_odds(p['odds'])}  ({spot})",
        description=f"vs {p['opponent']} · {date_str}",
        color=_TIER_COLOR.get(p["tier"], 0x27AE60),
    )
    # Win-probability split (both teams)
    embed.add_field(
        name="— win probability (model)",
        value=(f"**{p['rec_team']} {p['rec_pct']}%**  ·  {p['opponent']} {p['opp_pct']}%\n"
               f"market implies {p['market_prob']}% for {p['rec_team']} "
               f"→ model {'leans ' + str(p['lean']) + '% stronger' if not p['uncertain'] else 'disagrees sharply'}"),
        inline=False,
    )
    embed.add_field(name="— pitching", value=f"🪣 {p['rec_pitcher']} vs {p['opp_pitcher']}", inline=False)
    embed.add_field(name="— why", value=p["insight"][:1024], inline=False)
    embed.set_footer(text=("⭐ notable · 🟢 modest · 🟡 slight · ❓ pass  ·  "
                           "model is context, not a guaranteed edge"))
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


if __name__ == "__main__":
    import io, sys
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    d = vortextime.vortex_board_day()
    pl = get_moneyline_plays(d, force_odds=True)
    print(f"Moneyline (confirmed-lineup games) for {d}: {len(pl)} game(s)")
    for p in pl:
        print(f"\n  [{p['tier']}] {p['rec_team']} {_fmt_odds(p['odds'])} vs {p['opponent']}")
        print(f"    {p['rec_team']} {p['rec_pct']}% · {p['opponent']} {p['opp_pct']}% (market {p['market_prob']}%)")
        print(f"    why: {p['insight']}")
