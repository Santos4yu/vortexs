"""VORTEX — Discord prop research bot."""

import os
import sys
import json
import sqlite3
import asyncio
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import time as _dtime, timezone as _tz
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
import research as vortex_research
import stats_mlb
import init_db
import grade_results as grader
import cheatsheet
import analyze as vortex_analyze
import update_board
import vortextime
import refresh_live
import nrfi
import moneyline

load_dotenv(ROOT / ".env")

TOKEN      = os.getenv("DISCORD_TOKEN")
CLIENT_ID  = int(os.getenv("CLIENT_ID", "0"))
DB_PATH    = ROOT / "vortex.db"

ADMIN_ROLE_ID = 1516353685402292274   # VORTEX server admin role
VORTEX_GUILD  = 1515224924267216926   # VORTEX server ID (used for DM role check)

MAINTENANCE_MODE = False


async def _is_admin(interaction: discord.Interaction) -> bool:
    """True if the user has the VORTEX admin role — works in server and DMs."""
    if interaction.guild:
        return any(r.id == ADMIN_ROLE_ID for r in interaction.user.roles)
    # DM context — fetch the VORTEX guild and check membership there
    try:
        guild = interaction.client.get_guild(VORTEX_GUILD) or \
                await interaction.client.fetch_guild(VORTEX_GUILD)
        member = guild.get_member(interaction.user.id) or \
                 await guild.fetch_member(interaction.user.id)
        return any(r.id == ADMIN_ROLE_ID for r in member.roles)
    except Exception:
        return False

TIER_COLOR = {
    "ELITE":  0x00D4FF,  # cyan
    "STRONG": 0x5865F2,  # blurple
    "GOOD":   0x57F287,  # green
    "LEAN":   0x57F287,  # green
    "RISKY":  0xFEE75C,  # yellow
    "FADE":   0xED4245,  # red
    "PASS":   0x99AAB5,  # grey
}
TIER_EMOJI = {
    "ELITE":  "💎",
    "STRONG": "🔥",
    "GOOD":   "✅",
    "LEAN":   "➡️",
    "RISKY":  "⚠️",
    "FADE":   "🚫",
    "PASS":   "⚪",
}
SPORT_EMOJI = {"NBA": "🏀", "MLB": "⚾"}

def _score_emoji(score) -> str:
    """Return emoji matching the score-based verdict (same scale as Ratings legend)."""
    if score is None:
        return "⚪"
    score = int(score)
    if score >= 10: return "💎"
    if score >= 6:  return "🔥"
    if score >= 3:  return "✅"
    if score >= 0:  return "➡️"
    return "⚠️"
VORTEX_FOOTER = "VORTEX · Prop Research Engine"


# ── Claude API ─────────────────────────────────────────────────────────────────
_ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")

_VORTEX_SYSTEM_PROMPT = """\
SYSTEM INSTRUCTIONS — READ AND FOLLOW EXACTLY
You are VORTEX, an MLB prop-scoring engine. You evaluate projections, matchup polarity, trends, workload, volatility, and environment.

You are operating in ANALYZE MODE. Output a full analysis card regardless of score.

SCORING LADDER
• Elite  (10+)  → extremely strong across projection, matchup, trend, workload
• Strong (6–9)  → solid projection + supportive matchup or trend
• Good   (3–5)  → decent edge, playable with caution
• Lean   (0–2)  → marginal edge, proceed carefully
• Risky  (<0)   → risk factors outweigh positives
• Fade         → severe red flags, do not play

POLARITY RULES — apply consistently when writing the card:
• Projection > line     → positive signal
• Projection < line     → negative signal
• Opponent top-5 contact (K props)  → headwind for Over
• Opponent top-5 strikeout (K props) → tailwind for Over
• L5/L10/L20 trending up   → positive momentum
• L5/L10/L20 trending down → negative momentum
• High volatility (stdev > 2.0) → increase caution
• Strong workload (6+ IP avg)   → supports Over K props
• Weak workload  (<5 IP avg)    → suppresses Over K props

HARD RULES
• Never contradict the projection — if projection > line, the narrative must support Over
• Never recommend a side opposite the model projection
• The VORTEX score has already been computed — accept it as authoritative, do not override
• Be direct and data-driven — no filler, no hedging, no vague language
• Keep each section tight — 2–5 bullet points max

CARD FORMAT — use this exact structure, headers exactly as shown:
```
[PLAYER] — [SIDE] [LINE] [PROP]
[Team] · [Home/Away] vs [Opponent] · [Time]
[Rating Emoji] [Rating Label] (Score [N]) · [Hit Rate]% L10 · [Volatility]

— why it hits
• [Core projection vs line]
• [Trend analysis — L5/L10/L20]
• [Matchup polarity]
• [Workload / environment if relevant]

— performance
L5: [X/Y (pct%)] · L10: [X/Y (pct%)] · L20: [X/Y (pct%)]
Season avg: [N] · L10 avg: [N]
Recent: [last 5 game values]

— matchup
• [Opponent K/contact rank if relevant]
• [Pitcher ERA/K9/FIP]
• [BvP if available]
• [Home/Away split if relevant]

— risk
• [Volatility / stdev note]
• [Any red flags or penalty notes]
• [Penalty description from model if provided]

— verdict
[OVER/UNDER] [LINE] — [1 sentence confidence statement]
```
"""

def _build_prop_brief(
    player_name: str,
    team: str,
    prop_type: str,
    line: float,
    side: str,
    splits: dict,
    grade: dict,
    matchup: dict,
    pitcher: dict,
    opp_k_rank,
    opp_k_pct,
    park_factor: float,
    weather: dict,
    bvp: dict,
    statcast: dict,
    lineup_spot,
    game_time: str | None,
) -> str:
    """Format all prop data into a text brief for Claude."""
    s   = side.upper()
    pt  = prop_type.replace("_", " ").title()
    opp = matchup.get("opponent", "")
    loc = "Home" if matchup.get("is_home") else "Away"
    pitcher_name = matchup.get("pitcher", "") or ""

    l5  = splits.get("l5")  or {}
    l10 = splits.get("l10") or {}
    l20 = splits.get("l20") or {}
    is_under = side == "under"

    def _eff(r):
        raw = r.get("rate", 0) or 0
        return (100 - raw) if is_under else raw

    l5_pct  = _eff(l5)
    l10_pct = _eff(l10)
    l20_pct = _eff(l20)
    l5_g    = l5.get("games", 0)
    l10_g   = l10.get("games", 0)
    l20_g   = l20.get("games", 0)
    l5_h    = int(round(l5_g * l5_pct / 100)) if l5_g else 0
    l10_h   = int(round(l10_g * l10_pct / 100)) if l10_g else 0
    l20_h   = int(round(l20_g * l20_pct / 100)) if l20_g else 0
    l10_avg = l10.get("avg", 0) or 0
    ssn_avg = splits.get("season_avg", 0) or 0

    recent_vals = [
        str(g.get("value", "?"))
        for g in (splits.get("recent_games") or [])[:5]
    ]

    score     = grade.get("score", 0)
    label     = grade.get("label", "Lean")
    proj_edge = grade.get("proj_edge", 0) or 0
    penalties = grade.get("penalty_desc") or []
    stability = grade.get("stability_tier", "") or ""

    era  = (pitcher or {}).get("era", "?")
    k9   = (pitcher or {}).get("k_per_9",  (pitcher or {}).get("k9", "?"))
    fip  = (pitcher or {}).get("fip", "?")
    hand = (pitcher or {}).get("hand", "?")

    bvp_ab  = (bvp or {}).get("ab", 0) or 0
    bvp_avg = (bvp or {}).get("avg", "?") if bvp_ab >= 5 else "N/A (small sample)"

    sc_barrel = (statcast or {}).get("barrel_pct")
    sc_hh     = (statcast or {}).get("hard_hit_pct")
    sc_xslg   = (statcast or {}).get("xslg")

    opp_k_str = ""
    if opp_k_rank:
        opp_k_str = f"#{opp_k_rank}/30 hardest to K ({(opp_k_pct or 0)*100:.1f}% K rate)"

    weather_str = ""
    if weather and not weather.get("error") and not weather.get("dome"):
        spd = weather.get("speed_mph", 0) or 0
        hf  = weather.get("hitter_friendly")
        if spd >= 8:
            direction = "out" if hf else "in"
            weather_str = f"Wind {spd} mph blowing {direction}"

    park_str = ""
    if park_factor != 1.0:
        park_str = f"{park_factor:.3f} ({'hitter' if park_factor > 1 else 'pitcher'}-friendly)"

    spot_str = f"Batting #{lineup_spot}" if lineup_spot else ""
    time_str = game_time or ""

    proj_dir = "ABOVE" if proj_edge >= 0 else "BELOW"

    lines_out = [
        f"ANALYZE MODE — Generate a full VORTEX analysis card.",
        f"",
        f"PROP: {player_name} ({team}) — {s} {line} {pt}",
        f"MATCHUP: {loc} vs {opp} · Pitcher: {pitcher_name} ({hand}HP) · {time_str}",
        f"VORTEX SCORE: {score:+d} → {label}",
        f"",
        f"HIT RATES ({s}):",
        f"  L5:  {l5_h}/{l5_g} = {l5_pct:.0f}%",
        f"  L10: {l10_h}/{l10_g} = {l10_pct:.0f}%",
        f"  L20: {l20_h}/{l20_g} = {l20_pct:.0f}%",
        f"  L10 avg: {l10_avg}  Season avg: {ssn_avg}",
        f"  Recent game values: {', '.join(recent_vals) if recent_vals else 'N/A'}",
        f"  Projection edge: L10 avg is {abs(proj_edge):.2f} {proj_dir} the line",
        f"",
        f"MATCHUP DATA:",
        f"  Pitcher ERA: {era} · K/9: {k9} · FIP: {fip}",
        f"  Opponent K rank: {opp_k_str or 'N/A'}",
        f"  BvP ({bvp_ab} AB): avg {bvp_avg}",
        f"  Park factor: {park_str or '1.000 (neutral)'}",
        f"  Weather: {weather_str or 'None / dome'}",
    ]
    if spot_str:
        lines_out.append(f"  {spot_str}")
    if sc_barrel is not None:
        lines_out.append(f"  Statcast: {sc_barrel:.0f}% barrel · {sc_hh or '?'}% hard-hit · xSLG {sc_xslg or '?'}")
    if stability:
        lines_out.append(f"  Volatility tier: {stability}")
    if penalties:
        lines_out.append(f"")
        lines_out.append(f"MODEL PENALTIES APPLIED:")
        for p in penalties:
            # Strip markdown for the brief
            clean = p.replace("⚠️ **", "").replace("**", "")
            lines_out.append(f"  {clean}")

    return "\n".join(lines_out)


def _parse_claude_card(text: str, score: int) -> discord.Embed:
    """Convert Claude's card text into a Discord Embed."""
    color = (
        0x00D4FF if score >= 10 else
        0x5865F2 if score >= 6  else
        0x57F287 if score >= 3  else
        0xFEE75C if score >= 0  else
        0xED4245
    )
    lines = text.strip().splitlines()

    title       = lines[0].strip() if lines else "VORTEX Analysis"
    desc_lines  = []
    current_hdr = None
    current_body: list[str] = []
    fields: list[tuple[str, str]] = []

    for line in lines[1:]:
        stripped = line.strip()
        if stripped.startswith("— ") or stripped.startswith("─ "):
            # Flush previous section
            if current_hdr is not None:
                fields.append((current_hdr, "\n".join(current_body).strip()))
            elif current_body:
                desc_lines.extend(current_body)
            current_hdr  = stripped[2:].strip()
            current_body = []
        elif current_hdr is None:
            # Still in header / description area
            if stripped or desc_lines:
                desc_lines.append(line)
        else:
            current_body.append(line)

    # Flush last section
    if current_hdr is not None and current_body:
        fields.append((current_hdr, "\n".join(current_body).strip()))
    elif current_hdr is None and current_body:
        desc_lines.extend(current_body)

    description = "\n".join(desc_lines).strip() or None

    embed = discord.Embed(title=title, description=description, color=color)
    for name, value in fields:
        value = value.strip()
        if not value:
            continue
        if len(value) > 1024:
            value = value[:1021] + "..."
        embed.add_field(name=f"— {name}", value=value, inline=False)

    embed.set_footer(text="VORTEX · Claude-powered analysis engine")
    return embed


async def _claude_analyze_card(
    player_name: str,
    team: str,
    prop_type: str,
    line: float,
    side: str,
    splits: dict,
    grade: dict,
    matchup: dict,
    pitcher: dict,
    opp_k_rank,
    opp_k_pct,
    park_factor: float,
    weather: dict,
    bvp: dict,
    statcast: dict,
    lineup_spot,
    game_time: str | None,
) -> discord.Embed | None:
    """Call Claude API to generate the analysis card. Returns None if API unavailable."""
    if not _ANTHROPIC_KEY:
        return None
    try:
        import anthropic as _anthropic
        client = _anthropic.Anthropic(api_key=_ANTHROPIC_KEY)
        brief  = _build_prop_brief(
            player_name, team, prop_type, line, side,
            splits, grade, matchup, pitcher,
            opp_k_rank, opp_k_pct, park_factor,
            weather, bvp, statcast, lineup_spot, game_time,
        )
        loop    = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1500,
                system=_VORTEX_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": brief}],
            ),
        )
        card_text = response.content[0].text
        return _parse_claude_card(card_text, grade.get("score", 0))
    except Exception as exc:
        print(f"[claude] card generation failed: {exc}")
        return None


# ── DB ─────────────────────────────────────────────────────────────────────────
def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


_ALLOWED_MLB_STAT_KEYWORDS = ("hits+runs", "hrr", "total bases", "hits", "strikeout", "fantasy", "outs", "hits allowed", "earned runs")

_LIVE_FILTER = (
    "(commence_time IS NOT NULL AND commence_time != '' "
    "AND commence_time > strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
    # Upper bound: only today/next-day games. Without this, sparse WNBA slates
    # leak week-out games onto the board as 'false plays'.
    "AND commence_time < strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '+2 days'))"
)

_ALLOWED_BOOKS = {"draftkings", "prizepicks", "underdogfantasy", "underdog"}

def get_board(sport=None, tier=None, stat_filter=None, limit=None):
    conn = _db()
    # Only Strong/Elite on the board — Lean/Good/Risky/Fade are for /analyze only
    _tier_gate = "tier IN ('ELITE','STRONG')"
    q = f"SELECT * FROM props_board WHERE {_LIVE_FILTER} AND {_tier_gate}"
    p = []
    if sport:
        q += " AND sport=?"; p.append(sport)
    if tier:
        q += " AND tier=?"; p.append(tier)
    if stat_filter:
        q += " AND LOWER(stat_type) LIKE ?"; p.append(f"%{stat_filter}%")
    fetch = limit * 3 if limit else -1
    q += " ORDER BY vortex_score DESC"
    if limit:
        q += " LIMIT ?"
        p.append(fetch)
    rows = conn.execute(q, p).fetchall()
    conn.close()
    filtered = []
    for r in rows:
        book = (r["sportsbook"] or "").strip().lower()
        if book not in _ALLOWED_BOOKS:
            continue
        if r["sport"] == "MLB":
            st = (r["stat_type"] or "").lower()
            if not any(kw in st for kw in _ALLOWED_MLB_STAT_KEYWORDS):
                continue
        filtered.append(r)
    return filtered[:limit] if limit else filtered


def search_player(name: str):
    conn = _db()
    rows = conn.execute(
        "SELECT * FROM props_board WHERE LOWER(player_name) LIKE ? ORDER BY vortex_score DESC",
        (f"%{name.lower()}%",)
    ).fetchall()
    conn.close()
    return rows


# ── embed builders ─────────────────────────────────────────────────────────────
def _ev_str(ev: float) -> str:
    return f"+{ev:.1f}%" if ev >= 0 else f"{ev:.1f}%"


def _ev_display(ev: float, sj: dict) -> str:
    """Honest EV string. When a prop has no real two-sided de-vig line
    (anchor='none'), EV is not measurable — show 'N/A', never a fabricated edge."""
    if (sj or {}).get("anchor") not in ("sharp", "consensus"):
        return "N/A"
    return _ev_str(ev or 0)


def _anchor_label(sj: dict) -> str:
    """Short provenance tag so the user knows how the price was judged."""
    return {
        "sharp":      "🎯 Sharp-anchored (Pinnacle)",
        "consensus":  "📊 Consensus de-vig",
        "projection": "📈 Model projection (L10) — not a market edge",
        "none":       "⚠️ No two-sided line — EV not measurable",
    }.get((sj or {}).get("anchor", "consensus"), "📊 Consensus de-vig")


def _anchor_icon(sj: dict) -> str:
    """One-glyph trust signal for list rows. 🎯 sharp · 📊 consensus · 📈 model · ⚪ none."""
    return {
        "sharp": "🎯", "consensus": "📊", "projection": "📈", "none": "⚪",
    }.get((sj or {}).get("anchor", "consensus"), "📊")


def _row_sj(r) -> dict:
    """Safe stats_json parse for a board row."""
    try:
        return json.loads(r["stats_json"]) if r["stats_json"] else {}
    except (ValueError, TypeError):
        return {}


def _is_real_edge(r) -> bool:
    """True only when a prop has a positive edge backed by a real two-sided
    market (sharp Pinnacle or soft-book consensus) — never a model projection."""
    sj = _row_sj(r)
    return sj.get("anchor") in ("sharp", "consensus") and (r["ev_percentage"] or 0) > 0


def _implied_prob(odds: int) -> float:
    if odds > 0:
        return 100 / (odds + 100)
    return abs(odds) / (abs(odds) + 100)


def _ratings_ladder(score: int, active_tier: str = None) -> str:
    """Show the ratings scale with the current tier bolded."""
    rungs = [
        ("ELITE",  "💎 Elite (10+)"),
        ("STRONG", "🔥 Strong (6-9)"),
        ("GOOD",   "✅ Good (3-5)"),
        ("LEAN",   "➡️ Lean (0-2)"),
        ("RISKY",  "⚠️ Risky (<0)"),
        ("FADE",   "🚫 Fade (stay away)"),
    ]
    # If no explicit tier passed, derive from score (legacy path)
    if active_tier is None:
        if score >= 80:   active_tier = "ELITE"
        elif score >= 60: active_tier = "STRONG"
        elif score >= 40: active_tier = "GOOD"
        elif score >= 20: active_tier = "LEAN"
        else:             active_tier = "RISKY"
    parts = [f"**{label}**" if t == active_tier else label for t, label in rungs]
    return " · ".join(parts)


def _confidence_line(tier: str) -> str:
    if tier == "ELITE":  return "💎 **Elite play.** High confidence — standard unit."
    if tier == "STRONG": return "🔥 **Confident play.** Standard unit."
    if tier == "GOOD":   return "✅ **Good play.** Standard unit."
    if tier == "LEAN":   return "➡️ **Lean.** Half unit max."
    if tier == "FADE":   return "🚫 **Fade it.** Stay away."
    return "⚠️ **Risky.** Pass or size way down."


def _is_pitcher_k(row) -> bool:
    return "strikeout" in (row["stat_type"] or "").lower()


def _field_chunks(lines: list[str], max_chars: int = 950) -> list[str]:
    """Split a list of lines into chunks that each fit within Discord's field limit."""
    chunks, current, length = [], [], 0
    for ln in lines:
        if length + len(ln) + 1 > max_chars and current:
            chunks.append("\n".join(current))
            current, length = [], 0
        current.append(ln)
        length += len(ln) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


async def _fetch_game_times() -> dict:
    """Fetch today's MLB game start times keyed by probable pitcher name (lowercase)."""
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, stats_mlb.get_todays_game_times)
    except Exception:
        return {}


def build_wnba_detail_embed(row) -> discord.Embed:
    """
    Rich WNBA analysis card — mirrors the MLB card's depth with basketball
    signals: form, venue split, minutes/usage, pace matchup, opponent defense,
    game script, a plain-language breakdown, and risk.
    """
    sj      = json.loads(row["stats_json"]) if row["stats_json"] else {}
    side    = sj.get("side", "over")
    is_under = side == "under"
    splits  = sj.get("splits") or {}
    tier    = row["tier"] or ""
    score   = row["vortex_score"] or 0
    line    = float(row["line"])
    market  = row["stat_type"]
    player  = row["player_name"]
    first   = player.split()[0]
    sidetxt = "Under" if is_under else "Over"
    _icons  = {"ELITE": "💎", "STRONG": "🔥", "GOOD": "✅", "LEAN": "➡️", "RISKY": "⚠️", "FADE": "🚫"}
    icon    = _icons.get(tier, "🏀")

    l5  = splits.get("l5")  or {}
    l10 = splits.get("l10") or {}
    l20 = splits.get("l20") or {}

    def _eff(d):
        r = d.get("rate", 0) or 0
        return (100 - r) if is_under else r

    def _w(d):
        if not d.get("games"):
            return "—"
        return f"{_eff(d):.0f}% ({d.get('avg', 0)} avg)"

    opp      = sj.get("opponent", "?")
    is_home  = sj.get("is_home")
    spot     = "🏠 Home" if is_home is True else ("✈️ Away" if is_home is False else "")
    edge     = sj.get("proj_edge", 0) or 0
    mins10   = sj.get("minutes_l10", 0) or splits.get("min_l10", 0) or 0
    mins5    = splits.get("min_l5", 0) or 0
    stab     = (sj.get("stability") or "").capitalize()
    season_avg = splits.get("season_avg", 0) or 0
    l10_avg  = l10.get("avg", 0) or 0

    eff_l10  = _eff(l10)
    est_rate = eff_l10

    embed = discord.Embed(
        title=f"{player} — {sidetxt} {line:g} {market}",
        description=(f"{icon} **{tier.title()}** (Score {score}) · est. **{est_rate:.0f}%** hit rate "
                     f"· vs {opp} {spot}"),
        color=TIER_COLOR.get(tier, TIER_COLOR["STRONG"]),
    )

    # ── LAYER 1: why it hits (form + edge + usage + trend + floor) ───────────
    why = [
        f"📊 L5 {_w(l5)} · L10 {_w(l10)} · L20 {_w(l20)}",
        f"📈 Projection edge **{edge:+.1f}** vs the {line:g} line (L10 avg {l10_avg}).",
    ]
    # Recent trend — direction of form (L5 vs L20)
    eff_l5_v, eff_l20_v = _eff(l5), _eff(l20)
    if l5.get("games") and l20.get("games"):
        if   eff_l5_v - eff_l20_v >= 15: why.append(f"🔥 **Trending up** — {eff_l5_v:.0f}% L5 vs {eff_l20_v:.0f}% L20. Hot right now.")
        elif eff_l20_v - eff_l5_v >= 15: why.append(f"❄️ **Cooling off** — {eff_l5_v:.0f}% L5 vs {eff_l20_v:.0f}% L20. Recent form fading.")
        else:                            why.append(f"➡️ **Steady** — {eff_l5_v:.0f}% L5 vs {eff_l20_v:.0f}% L20.")
    # Minutes / usage trend
    if mins10:
        mtr = ""
        if mins5 and mins10:
            d = mins5 - mins10
            if   d >= 4:  mtr = " — role expanding 📈"
            elif d <= -5: mtr = " — minutes dropping 📉"
        why.append(f"⏱️ **{mins10:.0f} min/g** (L10){mtr}.")
    # Floor / consistency — judged by how often she CLEARS THE LINE (and whether
    # misses came on full minutes), NOT raw min-max. For an Over, a high ceiling
    # is upside, not risk — what matters is the floor relative to the line.
    recent_full = [g for g in (splits.get("recent_games") or [])[:10]
                   if isinstance(g.get("value"), (int, float))]
    recent10 = [g["value"] for g in recent_full]
    avg_min  = splits.get("min_l10") or mins10 or 0
    floor_lo = floor_hi = floor_clears = None
    is_boom = False
    if len(recent_full) >= 5:
        n = len(recent_full)
        floor_lo, floor_hi = min(recent10), max(recent10)
        floor_clears = sum(1 for g in recent_full
                           if (g["value"] < line if is_under else g["value"] > line))
        clear_rate = floor_clears / n
        # Misses that came on FULL minutes = genuine floor risk (vs rest-game noise)
        misses = [g for g in recent_full
                  if (g["value"] >= line if is_under else g["value"] < line)]
        full_misses = [g for g in misses if (g.get("min") or 0) >= 0.82 * (avg_min or 1)]
        if clear_rate >= 0.8 and len(full_misses) <= 1:
            floor_note = "✅ reliable — clears in nearly every full-minutes game"
        elif clear_rate >= 0.7:
            floor_note = "✅ solid clear rate"
        elif clear_rate >= 0.5:
            floor_note = "➡️ coin-flip floor — clears about half the time"
            is_boom = True
        else:
            floor_note = "⚠️ shaky floor — misses more than it clears"
            is_boom = True
        why.append(f"🎯 Range L10: **{floor_lo:g}–{floor_hi:g}** · clears the line "
                    f"**{floor_clears}/{n}** — {floor_note}.")
    elif stab:
        why.append(f"🎯 {stab} stability (recent variance).")

    # Outlier auto-explanation — cross-reference the worst game's MINUTES.
    # A dud on low minutes = rest/blowout (reassuring); a dud on full minutes =
    # a genuine cold game (real risk). Answers "was that 21 an injury/rest night?"
    if not is_under and len(recent_full) >= 5 and avg_min:
        worst = min(recent_full, key=lambda g: g["value"])
        wv, wm = worst["value"], (worst.get("min") or 0)
        if wv < line:   # the worst game missed the line — always explain it
            if wm and wm < 0.82 * avg_min:
                why.append(f"🟢 The **{wv:g}** dip came on a short **{wm:.0f}-min** night "
                           f"(vs {avg_min:.0f} avg) — rest/blowout, not a cold streak. The hit rate holds.")
            elif wm:
                why.append(f"🟡 The **{wv:g}** dip was a **full {wm:.0f}-min** game — "
                           f"a genuine off night, so the floor isn't bulletproof.")
    embed.add_field(name="— why it hits", value="\n".join(why)[:1024], inline=False)

    # ── LAYER 2: venue split ─────────────────────────────────────────────────
    h_avg, a_avg = splits.get("home_avg"), splits.get("away_avg")
    h_rt,  a_rt  = splits.get("home_rate"), splits.get("away_rate")
    hg, ag = splits.get("home_games", 0) or 0, splits.get("away_games", 0) or 0
    if h_avg is not None and a_avg is not None and hg >= 3 and ag >= 3:
        cur = "🏠 Home" if is_home else "✈️ Road"
        cur_avg = h_avg if is_home else a_avg
        oth_avg = a_avg if is_home else h_avg
        rt_part = ""
        cur_rt = h_rt if is_home else a_rt
        if cur_rt is not None:
            rt_part = f", hits {cur_rt:.0f}% of the time" if not is_under else f", stays Under {100-cur_rt:.0f}%"
        split_line = (f"{cur}: **{cur_avg}** avg{rt_part} · "
                      f"{'Road' if is_home else 'Home'}: {oth_avg} avg")
        embed.add_field(name="— venue split", value=split_line, inline=False)

    # ── LAYER 3: matchup (defense + pace) ────────────────────────────────────
    mlines = []
    dr, dn = sj.get("opp_def_rank"), sj.get("def_n_teams") or 15
    da, lavg = sj.get("opp_def_allowed"), sj.get("league_def_avg")
    # Concrete allowed-per-game number when we have it (e.g. "allows 33.4 vs 31.1 league avg")
    allow_part = ""
    if da is not None and lavg:
        vs = "above" if da > lavg else "below"
        allow_part = f" — allows **{da}**/g ({vs} {lavg} league avg)"
    if dr:
        if   dr >= dn - 2: mlines.append(f"🟢 **{opp} defense ranks #{dr}/{dn}** vs this stat{allow_part} — generous, prime spot.")
        elif dr <= 3:      mlines.append(f"🔴 **{opp} defense ranks #{dr}/{dn}** vs this stat{allow_part} — stingy, real resistance.")
        else:              mlines.append(f"⚪ {opp} defense ranks #{dr}/{dn} vs this stat{allow_part} — neutral.")
    tp, op_, lp = sj.get("team_pace"), sj.get("opp_pace"), sj.get("league_pace")
    if op_ and lp:
        ratio = op_ / lp
        if   ratio >= 1.04: mlines.append(f"🏃 Fast matchup — {opp} plays at {ratio:.2f}× league pace (more possessions).")
        elif ratio <= 0.96: mlines.append(f"🐢 Slow matchup — {opp} plays at {ratio:.2f}× league pace (fewer possessions).")
        else:               mlines.append(f"➡️ Average pace ({ratio:.2f}× league).")
    if mlines:
        embed.add_field(name="— matchup", value="\n".join(mlines)[:1024], inline=False)

    # ── LAYER 4: game script (usage / availability) ──────────────────────────
    # Always shown — when nothing fires, an explicit "clean" line confirms these
    # were checked (B2B, blowout, and minutes ARE flagged; their absence is signal).
    gs = []
    tmo = sj.get("teammate_out")
    flags = sj.get("game_flags") or []
    # Actual game spread — tight game = full minutes; big spread = bench risk.
    spread = sj.get("game_spread")
    spread_flags_blowout = spread is not None and spread >= 12.5
    if spread is not None:
        if   spread >= 12.5: gs.append(f"⚠️ **Spread {spread:g}** — blowout range, starters may sit in the 4th.")
        elif spread <= 5:    gs.append(f"✅ **Spread {spread:g}** — tight game, expect full starter minutes.")
        else:                gs.append(f"➡️ **Spread {spread:g}** — competitive, normal minutes expected.")
    if tmo:
        gs.append(f"⬆️ **{tmo}** (starter) is OUT — usage bumps toward {first}.")
    if "b2b" in flags:           gs.append("🔁 Back-to-back — minutes/fatigue risk.")
    # Skip the redundant blowout flag if the spread line already said it.
    if "blowout_risk" in flags and not spread_flags_blowout:
        gs.append("⚠️ Blowout risk — starters may sit in the 4th.")
    if sj.get("self_status") == "questionable":
        gs.append(f"❓ {first} is questionable — minutes uncertain.")
    if not gs:
        gs.append("✅ Clean game script — no back-to-back, blowout risk, or minutes concerns flagged.")
    embed.add_field(name="— game script", value="\n".join(gs)[:1024], inline=False)

    # ── LAYER 5: the breakdown (plain-language narrative) ─────────────────────
    para = []
    if l10.get("games"):
        para.append(
            f"{first} has gone **{sidetxt} {line:g}** in **{l10.get('hits',0)}/{l10.get('games',0)}** "
            f"of her last 10 ({eff_l10:.0f}%), averaging **{l10_avg} {market}** "
            f"— **{abs(edge):.1f} {'above' if edge >= 0 else 'below'}** the line.")
    if mins10 >= 30 and not is_boom:
        para.append(f"She's logging heavy minutes (**{mins10:.0f}/g**) and clears the line in "
                    f"**{floor_clears}/{len(recent_full)}** recent games — a dependable floor for the {sidetxt}.")
    elif mins10 >= 30 and is_boom:
        para.append(f"She plays heavy minutes (**{mins10:.0f}/g**), but the floor is shaky — only clears "
                    f"**{floor_clears}/{len(recent_full)}** recent games, so size accordingly.")
    elif mins10 and mins10 < 22:
        para.append(f"Watch the workload — only **{mins10:.0f} min/g**, which caps the ceiling.")
    if dr and dr >= dn - 2:
        para.append(f"The matchup is a green light: **{opp}** is one of the most generous defenses vs this stat (#{dr}/{dn}).")
    elif dr and dr <= 3:
        para.append(f"The catch: **{opp}** is a top-{dr} defense vs this stat — real resistance.")
    if tmo:
        para.append(f"With **{tmo}** out, expect {first}'s usage to climb.")
    # Conclusion
    if score >= 10:
        para.append(f"**Bottom line:** the signals stack cleanly on the **{sidetxt}** — one of the cleaner WNBA spots tonight.")
    elif score >= 6:
        para.append(f"**Bottom line:** the data supports the **{sidetxt}** — a confident play.")
    elif score >= 3:
        para.append(f"**Bottom line:** leans **{sidetxt}**, but size down — not a lock.")
    else:
        para.append(f"**Bottom line:** weak lean — the {sidetxt} has real arguments against it. Reduced size only.")
    body = "\n".join(para)
    embed.add_field(name="— the breakdown", value=body[:1024], inline=False)

    # ── LAYER 5b: model verdict (both sides) ─────────────────────────────────
    over_s, under_s = sj.get("over_score"), sj.get("under_score")
    verdict = sj.get("model_verdict")
    conf    = sj.get("confidence")
    if over_s is not None and under_s is not None and verdict:
        agree = (verdict == side)
        confirm = ("✅ **Model confirms** your side" if agree
                   else f"⚠️ **Model leans {verdict.title()}** — disagrees with your {sidetxt}")
        conf_txt = f" · {conf*100:.0f}% confidence" if conf else ""
        embed.add_field(
            name=f"🎯 VERDICT: {sidetxt.upper()} {line:g}",
            value=(f"{confirm}{conf_txt}\n"
                   f"Over score **{over_s}** · Under score **{under_s}**"),
            inline=False,
        )

    # ── LAYER 6: performance + risk ──────────────────────────────────────────
    recent = splits.get("recent_games") or []
    if recent:
        log = "  ".join(str(g.get("value")) for g in recent[:5])
        embed.add_field(
            name="— performance",
            value=(f"L5 {_w(l5)} · L10 {_w(l10)} · L20 {_w(l20)}\n"
                   f"Season avg {season_avg} over {splits.get('games_played','—')} GP\n"
                   f"Last 5: {log}"),
            inline=False,
        )
    risk = row["risk_summary"] or "No major red flags in available data."
    embed.add_field(name="— risk", value=str(risk)[:1024], inline=False)
    embed.set_footer(text=VORTEX_FOOTER)
    return embed


def board_embed(rows, title: str, game_times: dict | None = None) -> list[discord.Embed]:
    """
    Edge-first board. Props are split into two clear sections:
      ✅ Real Edges — positive EV backed by a real two-sided market (🎯/📊)
      📋 Watchlist  — everything else (model-only or negative market EV)
    Each row carries a one-glyph trust icon so quality reads at a glance, and the
    embed is colored green when genuine edges exist. All props are kept.
    """
    gt = game_times or {}
    _tier_icons = {"ELITE": "💎", "STRONG": "🔥", "GOOD": "✅", "LEAN": "➡️", "RISKY": "⚠️", "FADE": "🚫"}

    # Render-time guard: never display a game that has already started.
    from datetime import datetime as _dt2, timezone as _tz2
    _now_iso = _dt2.now(_tz2.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    edges: list[str] = []
    watch: list[str] = []
    has_unconfirmed = False

    _i = 0
    for r in rows:
        ct = (r["commence_time"] or "").strip() if "commence_time" in r.keys() else ""
        if ct and ct <= _now_iso:
            continue   # game already started/finished — drop from the rendered board
        _i += 1
        i = _i
        sj     = _row_sj(r)
        side   = sj.get("side", "over")
        tier   = r["tier"] or ""
        sw     = "O" if side == "over" else "U"
        ev     = _ev_display(r["ev_percentage"], sj)
        icon   = _anchor_icon(sj)
        splits = sj.get("splits") or {}
        l10r   = (splits.get("l10") or {}).get("rate") or 0
        eff    = (100 - l10r) if side == "under" else l10r
        score  = r["vortex_score"] or 0
        te     = _tier_icons.get(tier, _score_emoji(score))

        pitcher_nm = (sj.get("pitcher") or {}).get("name", "").lower()
        time_tag   = f"  ·{gt[pitcher_nm]}" if pitcher_nm and pitcher_nm in gt else ""

        unconfirmed_tag = ""
        if not _is_pitcher_k(r) and sj.get("lineup_confirmed") is False:
            unconfirmed_tag = " ⏳"
            has_unconfirmed = True

        kicon = "⚾" if _is_pitcher_k(r) else ""
        row_line = (
            f"`{i:02}` {te} **{r['player_name']}**{unconfirmed_tag} · "
            f"{sw}{r['line']} {kicon}{r['stat_type']} · "
            f"**{eff:.0f}%** L10 · {ev} {icon}{time_tag}"
        )

        (edges if _is_real_edge(r) else watch).append(row_line)

    # Color the whole board green when there are genuine edges, grey when none.
    color = TIER_COLOR["GOOD"] if edges else 0x99AAB5
    embed = discord.Embed(title=title, color=color)
    embed.set_footer(text=VORTEX_FOOTER)

    if not edges and not watch:
        embed.description = "No plays on the board right now."
        return [embed]

    if edges:
        embed.description = f"✅ **{len(edges)} real edge{'s' if len(edges)!=1 else ''}** · 📋 {len(watch)} on watch"
        chunks = _field_chunks(edges)
        embed.add_field(name="✅ REAL EDGES — positive market EV", value=chunks[0], inline=False)
        for chunk in chunks[1:]:
            embed.add_field(name="✅ continued", value=chunk, inline=False)
    else:
        embed.description = (
            "⚠️ **No positive-EV edges tonight.** Every prop below is priced at or "
            "against the market — shown for research, not as recommended bets."
        )

    if watch:
        chunks = _field_chunks(watch)
        embed.add_field(name="📋 WATCHLIST — model picks / no market edge", value=chunks[0], inline=False)
        for chunk in chunks[1:]:
            embed.add_field(name="📋 continued", value=chunk, inline=False)

    # Legend so every glyph is self-explanatory.
    embed.add_field(
        name="— legend",
        value=("🎯 sharp-anchored · 📊 consensus · 📈/⚪ model-only (EV N/A) · "
               "⚾ pitcher K" + (" · ⏳ lineup not official yet" if has_unconfirmed else "")),
        inline=False,
    )

    total = len(edges) + len(watch)
    if total > 25:
        embed.add_field(
            name="— note",
            value="Only the first 25 props appear in the dropdown. Use **/prediction <player>** for any others.",
            inline=False,
        )

    return [embed]


def pick_embed(row) -> discord.Embed:
    """Full detail card for one prop row."""
    sj      = json.loads(row["stats_json"]) if row["stats_json"] else {}
    side    = sj.get("side", "over")
    tier    = row["tier"] or "—"
    ev      = row["ev_percentage"] or 0
    sport   = row["sport"]
    stat    = row["stat_type"]
    line    = row["line"]
    player  = row["player_name"]
    book    = row["sportsbook"]
    score   = row["vortex_score"] or 0
    splits  = sj.get("splits") or {}

    true_prob = sj.get("true_prob")
    best_odds = sj.get("best_odds")
    is_home   = sj.get("is_home")

    side_word = "Over" if side == "over" else "Under"
    te  = TIER_EMOJI.get(tier, "⚪")
    se  = SPORT_EMOJI.get(sport, "🎯")
    col = TIER_COLOR.get(tier, 0x99AAB5)

    # Edge framing — true prob vs market implied. Only frame a measurable edge
    # when there's a real two-sided de-vig; otherwise say so plainly.
    if sj.get("anchor") not in ("sharp", "consensus"):
        edge_str = "**EV N/A** (judged on stats, not market edge)"
    elif true_prob is not None and best_odds is not None:
        side_true   = (1 - true_prob) if side == "under" else true_prob
        market_imp  = _implied_prob(best_odds)
        edge_str    = f"est. **{side_true*100:.0f}%** vs {market_imp*100:.0f}% market · **{_ev_str(ev)} value**"
    else:
        edge_str = f"Edge **{_ev_str(ev)}**"
    edge_str += f"\n{_anchor_label(sj)}"

    # Home/away spot
    spot_str = ""
    if is_home is True:
        spot_str = "  ·  🏠 Home"
    elif is_home is False:
        spot_str = "  ·  ✈️ Away"

    _tier_icons2 = {"ELITE": "💎", "STRONG": "🔥", "GOOD": "✅", "LEAN": "➡️", "RISKY": "⚠️", "FADE": "🚫"}
    tier_badge = f"{_tier_icons2.get(tier, '⚪')} {tier}"
    side_badge = f"{'OVER' if side == 'over' else 'UNDER'} {line}"

    # Color by genuine edge first, fall back to tier color.
    if sj.get("anchor") in ("sharp", "consensus") and ev > 0:
        col = TIER_COLOR["GOOD"]          # green — a real positive-EV edge
    elif sj.get("anchor") in ("sharp", "consensus") and ev < 0:
        col = TIER_COLOR["FADE"]          # red — market prices this against you

    title = f"{_anchor_icon(sj)} {player} — {side_badge} {stat}"
    desc  = (
        f"`{tier_badge}`  ·  Score **{score}**  ·  {edge_str}{spot_str}\n"
        f"{_confidence_line(tier)}"
    )

    embed = discord.Embed(title=title, description=desc, color=col)

    # — signals block
    why_parts = []
    case = row["case_summary"] or ""
    if case:
        why_parts.append(case)
    crush = sj.get("crush_note", "")
    if crush:
        why_parts.append(crush)
    plat = sj.get("platoon_note", "")
    if plat and plat not in case:
        why_parts.append(plat)
    park = sj.get("park") or {}
    pf   = park.get("factor", 1.0)
    if pf and pf >= 1.05:
        why_parts.append(f"🟢 Hitter-friendly park ({pf:.2f}x) — boosts production")
    elif pf and pf <= 0.95:
        why_parts.append(f"🔴 Pitcher-friendly park ({pf:.2f}x) — suppresses output")
    weather_note = sj.get("weather_note", "")
    if weather_note:
        why_parts.append(weather_note)
    def_note = sj.get("defense_note", "")
    if def_note:
        why_parts.append(def_note)

    if why_parts:
        why_text = "\n".join(why_parts)
        if len(why_text) > 1024:
            why_text = why_text[:1021] + "..."
        embed.add_field(name="— signals", value=why_text, inline=False)

    # — performance
    l5  = splits.get("l5") or {}
    l10 = splits.get("l10") or {}
    l20 = splits.get("l20") or {}
    avg = splits.get("season_avg", "?")
    gp  = splits.get("games_played", "?")
    if l5 or l10 or l20:
        def hr(d):
            g = d.get("games", 0)
            h = d.get("hits", 0)
            hits_display = (g - h) if side == "under" and g else h
            return f"{hits_display}/{g or '?'}"
        splits_val = (
            f"L5 **{hr(l5)}** · L10 **{hr(l10)}** · L20 **{hr(l20)}**\n"
            f"Season avg **{avg}** over {gp} GP"
        )
        embed.add_field(name="— performance", value=splits_val, inline=False)

    # — matchup
    is_pitcher_prop = sj.get("is_pitcher", False)
    if is_pitcher_prop:
        opp_k    = sj.get("opp_k") or {}
        ss       = sj.get("season_stats") or {}
        last5    = sj.get("last_5_starts") or []
        opp_name = opp_k.get("name", "Opposing lineup")
        opp_rank = opp_k.get("rank")
        opp_kpct = opp_k.get("k_pct")
        matchup_parts = []
        if opp_rank and opp_kpct:
            matchup_parts.append(f"vs {opp_name} — #{opp_rank}/30 K rate ({opp_kpct:.1f}%)")
        if ss:
            k9  = ss.get("k_per_9", "?")
            era = ss.get("era", "?")
            kpg = ss.get("k_per_gs", "?")
            matchup_parts.append(f"{era} ERA · {k9} K/9 · {kpg} K/start avg")
        if last5:
            starts_str = "  ".join(f"{s.get('k','?')}K" for s in last5[:5])
            matchup_parts.append(f"L5 starts: {starts_str}")
        if matchup_parts:
            embed.add_field(name="— matchup", value="\n".join(matchup_parts), inline=False)
    else:
        pitcher = sj.get("pitcher") or {}
        if pitcher:
            pname = pitcher.get("name", "Opposing pitcher")
            hand  = pitcher.get("hand", "?")
            era   = pitcher.get("era", "?")
            fip   = pitcher.get("fip")
            hr9   = pitcher.get("hr_per_9", "?")
            matchup_str = f"{pname} ({hand}HP) — {era} ERA · {hr9} HR/9"
            if fip:
                matchup_str += f" · {fip} FIP"
            embed.add_field(name="— matchup", value=matchup_str, inline=False)

    # — risk
    risk = row["risk_summary"] or ""
    if risk:
        embed.add_field(name="— risk", value=risk[:1024], inline=False)

    # — rating
    embed.add_field(
        name="— rating",
        value=f"{_ratings_ladder(score, active_tier=tier)}\n*Higher score = more data behind the pick.*",
        inline=False,
    )

    embed.set_footer(text=f"VORTEX · {book}")
    return embed


def player_overview_embed(rows) -> discord.Embed:
    """Summary embed listing all props for a player."""
    player = rows[0]["player_name"]
    sport  = rows[0]["sport"]
    se     = SPORT_EMOJI.get(sport, "🎯")

    embed = discord.Embed(
        title=f"🔍 {se} {player}",
        color=TIER_COLOR["ELITE"],
    )
    embed.set_footer(text=VORTEX_FOOTER)

    for r in rows:
        sj   = json.loads(r["stats_json"]) if r["stats_json"] else {}
        side = sj.get("side", "over")
        sw   = "Over" if side == "over" else "Under"
        tier = r["tier"] or "—"
        te   = TIER_EMOJI.get(tier, "⚪")
        ev   = _ev_display(r["ev_percentage"], sj)
        splits = sj.get("splits") or {}
        l10  = (splits.get("l10") or {})
        l10r = l10.get("rate") or 0
        eff  = (100 - l10r) if side == "under" else l10r
        avg  = splits.get("season_avg", "?")

        val = (
            f"`{te} {tier}`  ·  edge **{ev}** {_anchor_icon(sj)}  ·  {r['sportsbook']}\n"
            f"L10: {l10.get('hits','?')}/{l10.get('games','?')} ({eff:.0f}%) · avg {avg}/g"
        )
        embed.add_field(
            name=f"{sw} {r['line']} {r['stat_type']}",
            value=val,
            inline=False,
        )

    return embed


# ── stat dropdown for /player ──────────────────────────────────────────────────
class StatSelect(discord.ui.Select):
    def __init__(self, rows):
        self.rows_by_label = {}
        options = []
        seen = set()
        for r in rows:
            sj   = json.loads(r["stats_json"]) if r["stats_json"] else {}
            side = sj.get("side", "over")
            sw   = "Over" if side == "over" else "Under"
            label = f"{sw} {r['line']} {r['stat_type']}"
            if label in seen:
                continue
            seen.add(label)
            self.rows_by_label[label] = r
            options.append(discord.SelectOption(label=label, value=label))

        super().__init__(
            placeholder="🔎 Drill into a specific stat...",
            min_values=1, max_values=1,
            options=options[:25],
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        row = self.rows_by_label[self.values[0]]
        if (row["sport"] or "") == "WNBA":
            await interaction.followup.send(embed=build_wnba_detail_embed(row), ephemeral=False)
            return
        sj  = json.loads(row["stats_json"]) if row["stats_json"] else {}
        prop = {
            "player_name": row["player_name"],
            "line":        row["line"],
            "side":        sj.get("side", "over"),
            "market_raw":  row["stat_type"],
        }
        try:
            await _run_analyze(interaction, prop)
        except Exception:
            import traceback
            await interaction.followup.send(
                f"❌ Error: ```{traceback.format_exc()[-1800:]}```", ephemeral=True
            )


class PlayerView(discord.ui.View):
    def __init__(self, rows):
        super().__init__(timeout=300)
        self.add_item(StatSelect(rows))


class PropSelect(discord.ui.Select):
    """Dropdown on board embeds — pick a prop to see full detail card."""
    def __init__(self, rows):
        self.row_map = {}
        options = []
        for i, r in enumerate(rows[:25]):
            sj   = json.loads(r["stats_json"]) if r["stats_json"] else {}
            side = sj.get("side", "over")
            sw   = "O" if side == "over" else "U"
            te   = _score_emoji(r["vortex_score"] or 0)
            label = f"{te} {r['player_name']} {sw}{r['line']} {r['stat_type']}"[:100]
            # Include index + stat_type to guarantee uniqueness even when
            # the same player appears twice with the same line and side.
            key   = f"{i}|{r['player_name']}|{r['line']}|{side}|{r['stat_type']}"[:100]
            self.row_map[key] = r
            options.append(discord.SelectOption(label=label, value=key))
        super().__init__(
            placeholder="📋 Select a prop for full detail card...",
            min_values=1, max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        row = self.row_map[self.values[0]]
        if (row["sport"] or "") == "WNBA":
            await interaction.followup.send(embed=build_wnba_detail_embed(row), ephemeral=False)
            return
        sj  = json.loads(row["stats_json"]) if row["stats_json"] else {}
        prop = {
            "player_name": row["player_name"],
            "line":        row["line"],
            "side":        sj.get("side", "over"),
            "market_raw":  row["stat_type"],
        }
        try:
            await _run_analyze(interaction, prop)
        except Exception:
            import traceback
            await interaction.followup.send(
                f"❌ Error: ```{traceback.format_exc()[-1800:]}```", ephemeral=True
            )


class BoardDetailView(discord.ui.View):
    def __init__(self, rows):
        super().__init__(timeout=300)
        self.add_item(PropSelect(rows))


# ── player lookup modal ────────────────────────────────────────────────────────
class PlayerLookupModal(discord.ui.Modal, title="🔍 VORTEX Player Lookup"):
    player_name = discord.ui.TextInput(
        label="Player Name",
        placeholder="e.g. Ohtani, Judge, Vlad Jr, Harper...",
        min_length=2,
        max_length=50,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        name = self.player_name.value.strip()

        matches = await asyncio.get_event_loop().run_in_executor(
            None, vortex_research.fuzzy_search, name
        )
        if not matches:
            await interaction.followup.send(
                f"No MLB player found matching **\"{name}\"**. Try a last name.",
                ephemeral=True,
            )
            return

        player    = matches[0]
        full_name = player["name"]

        try:
            card = await asyncio.get_event_loop().run_in_executor(
                None, lambda: vortex_research.get_research_card(player["id"])
            )
        except Exception as e:
            import traceback; traceback.print_exc()
            await interaction.followup.send(f"❌ Error fetching card: `{e}`", ephemeral=True)
            return

        if "error" in card:
            await interaction.followup.send(f"❌ {card['error']}", ephemeral=True)
            return

        try:
            embed, view = _send_research_card(card)
        except Exception as e:
            import traceback; traceback.print_exc()
            await interaction.followup.send(f"❌ Error building embed: `{e}`", ephemeral=True)
            return

        kw = {"embed": embed}
        if view:
            kw["view"] = view
        await interaction.followup.send(**kw)


# ── board view buttons ─────────────────────────────────────────────────────────
class BoardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    async def _send_board(self, interaction, rows, title):
        """Render a board with its prop dropdown attached for one-click detail."""
        game_times = await _fetch_game_times()
        embeds = board_embed(rows, title, game_times=game_times)
        view = BoardDetailView(rows) if rows else None
        await interaction.followup.send(embeds=embeds, view=view, ephemeral=True)

    @discord.ui.button(label="💰 Real Edges", style=discord.ButtonStyle.success)
    async def btn_edges(self, interaction: discord.Interaction, _):
        await interaction.response.defer(ephemeral=True)
        rows = [r for r in get_board(limit=40) if _is_real_edge(r)]
        title = "💰 Real Edges — positive market EV" if rows else "💰 Real Edges — none tonight"
        await self._send_board(interaction, rows, title)

    @discord.ui.button(label="⚾ MLB", style=discord.ButtonStyle.secondary)
    async def btn_mlb(self, interaction: discord.Interaction, _):
        await interaction.response.defer(ephemeral=True)
        rows = get_board(sport="MLB", limit=25)
        await self._send_board(interaction, rows, "⚾ MLB Plays Tonight")

    @discord.ui.button(label="🏀 WNBA", style=discord.ButtonStyle.secondary)
    async def btn_wnba(self, interaction: discord.Interaction, _):
        await interaction.response.defer(ephemeral=True)
        rows = get_board(sport="WNBA", limit=15)
        await self._send_board(interaction, rows, "🏀 WNBA Plays Tonight")

    @discord.ui.button(label="🔒 Locks", style=discord.ButtonStyle.primary)
    async def btn_locks(self, interaction: discord.Interaction, _):
        await interaction.response.defer(ephemeral=True)
        rows = [r for r in get_board(limit=40) if (r["vortex_score"] or 0) >= 15]
        title = "🔒 Locks — highest-confidence scores" if rows else "🔒 Locks — none tonight"
        await self._send_board(interaction, rows, title)

    @discord.ui.button(label="🔍 Lookup", style=discord.ButtonStyle.secondary)
    async def btn_lookup(self, interaction: discord.Interaction, _):
        await interaction.response.send_modal(PlayerLookupModal())

    @discord.ui.button(label="📋 Intel Brief", style=discord.ButtonStyle.secondary)
    async def btn_intel(self, interaction: discord.Interaction, _):
        loop = asyncio.get_event_loop()
        import vortextime as _vt
        schedule = await loop.run_in_executor(None, stats_mlb.get_todays_schedule, _vt.vortex_board_day())
        embed = await cheatsheet.build_parks_embed(schedule or {})
        await interaction.response.edit_message(embed=embed, view=CheatSheetView(schedule or {}))


class CheatSheetView(discord.ui.View):
    def __init__(self, schedule: dict):
        super().__init__(timeout=300)
        self.schedule = schedule

    async def _edit(self, interaction, embed):
        """Replace the current embed instead of sending a new message."""
        await interaction.edit_original_response(embed=embed, view=CheatSheetView(self.schedule))

    @discord.ui.button(label="🏟️ Parks", style=discord.ButtonStyle.secondary, row=0)
    async def btn_parks(self, interaction: discord.Interaction, _):
        await interaction.response.defer()
        embed = await cheatsheet.build_parks_embed(self.schedule)
        await self._edit(interaction, embed)

    @discord.ui.button(label="🌬️ Weather", style=discord.ButtonStyle.secondary, row=0)
    async def btn_weather(self, interaction: discord.Interaction, _):
        await interaction.response.defer()
        embed = await cheatsheet.build_weather_embed(self.schedule)
        await self._edit(interaction, embed)

    @discord.ui.button(label="💛 Platoon", style=discord.ButtonStyle.secondary, row=0)
    async def btn_platoon(self, interaction: discord.Interaction, _):
        await interaction.response.defer()
        embed = await cheatsheet.build_platoon_embed(self.schedule)
        await self._edit(interaction, embed)

    @discord.ui.button(label="⚔️ BvP", style=discord.ButtonStyle.secondary, row=1)
    async def btn_bvp(self, interaction: discord.Interaction, _):
        await interaction.response.defer()
        embed = await cheatsheet.build_bvp_embed(self.schedule)
        await self._edit(interaction, embed)

    @discord.ui.button(label="🎯 K Spots", style=discord.ButtonStyle.secondary, row=1)
    async def btn_kspots(self, interaction: discord.Interaction, _):
        await interaction.response.defer()
        embed = await cheatsheet.build_k_spots_embed(self.schedule)
        await self._edit(interaction, embed)

    @discord.ui.button(label="🎯 Attack Board", style=discord.ButtonStyle.secondary, row=1)
    async def btn_attack(self, interaction: discord.Interaction, _):
        await interaction.response.defer()
        embed = await cheatsheet.build_attack_embed(self.schedule)
        await self._edit(interaction, embed)

    @discord.ui.button(label="🔥 Streaks", style=discord.ButtonStyle.secondary, row=2)
    async def btn_streaks(self, interaction: discord.Interaction, _):
        await interaction.response.defer()
        embed = await cheatsheet.build_streaks_embed()
        await self._edit(interaction, embed)

    @discord.ui.button(label="◀ Back to Board", style=discord.ButtonStyle.primary, row=2)
    async def btn_back(self, interaction: discord.Interaction, _):
        await interaction.response.defer()
        rows = get_board(limit=30)
        game_times = await _fetch_game_times()
        embeds = board_embed(rows, "⚡ Tonight's Board — VORTEX", game_times=game_times)
        await interaction.edit_original_response(embeds=embeds, view=BoardView())


# ── bot ────────────────────────────────────────────────────────────────────────
BETA_ROLE_ID = 1515612947110690846

intents = discord.Intents.default()
intents.members = True
intents.message_content = True


class VortexTree(discord.app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Always resolve roles from the main guild — works in both server and DMs
        GUILD_ID = 1515224924267216926
        guild = interaction.guild or bot.get_guild(GUILD_ID)
        member = None
        if guild:
            member = guild.get_member(interaction.user.id)
            if member is None:
                try:
                    member = await guild.fetch_member(interaction.user.id)
                except Exception:
                    member = None

        has_role = any(r.id == BETA_ROLE_ID for r in getattr(member, "roles", []))

        # Maintenance mode — block everyone except admins (even beta role)
        if MAINTENANCE_MODE:
            is_adm = await _is_admin(interaction)
            if not is_adm:
                maint_embed = discord.Embed(
                    title="🔧 VORTEX — MAINTENANCE MODE",
                    description=(
                        "VORTEX is currently down for maintenance.\n"
                        "All commands are temporarily disabled.\n"
                        "Check back shortly."
                    ),
                    color=0xFEE75C,
                )
                await interaction.response.send_message(embed=maint_embed, ephemeral=True)
                return False

        if has_role:
            return True

        lock_embed = discord.Embed(
            title="🔒 VORTEX BETA — ACCESS RESTRICTED",
            description=(
                "**This command is currently locked to Beta Testers only.**\n\n"
                "The VORTEX team is deep in stress-testing the scoring engine — "
                "calibrating dynamic line-matching, refining the risk penalty system, "
                "and hardening the OCR pipeline before this goes wide.\n\n"
                "📋 **What's being tested:**\n"
                "› Slip OCR accuracy across all major sportsbooks\n"
                "› Risk penalty modifiers & grade downgrade logic\n"
                "› Multi-prop parlay detection & split-factor analytics\n\n"
                "🚀 **Full public drop is coming soon.** Stay locked in."
            ),
            color=0x2C2C2C,
        )
        lock_embed.set_footer(text="VORTEX · Beta Phase · Access granted by dev team only")
        await interaction.response.send_message(embed=lock_embed, ephemeral=True)
        return False


bot = commands.Bot(command_prefix="!", intents=intents, tree_cls=VortexTree)
tree = bot.tree


@tasks.loop(time=_dtime(hour=11, minute=0, tzinfo=_tz.utc))
async def nightly_grader():
    """Runs daily at 11:00 AM UTC (5 AM Mountain) — just after the betting day
    rolls over at 4 AM Mountain and every game (incl. late West Coast) is final.
    Grades the slate that just ended plus the prior one as a safety net."""
    today = vortextime.vortex_day_offset(-1)   # day that just ended at 4 AM Mountain
    prev  = vortextime.vortex_day_offset(-2)
    for d in (today, prev):
        print(f"[grader] Grading {d} ...")
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, grader.grade_date, d)
            print(f"[grader] Done for {d}")
        except Exception as e:
            print(f"[grader] Error {d}: {e}")
    _record_heartbeat("nightly_grade")


# ── Grading watchdog: alert if the learning loop ever stalls ──────────────────
# The entire engine depends on picks being graded nightly. If the bot is down
# when a slate ends, grading gaps, the learning loop starves, and hitrate gates
# never get the data they need. This watchdog makes that failure loud instead of
# silent. ALERT_CHANNEL is configurable; falls back to the moneyline channel.
ALERT_CHANNEL = int(os.getenv("ALERT_CHANNEL", "0")) or None

def _record_heartbeat(name: str):
    """Upsert a 'last successful run' timestamp for a named job."""
    from datetime import datetime as _dt, timezone as _tzc
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS system_health "
            "(job TEXT PRIMARY KEY, last_ok TEXT)"
        )
        conn.execute(
            "INSERT INTO system_health (job, last_ok) VALUES (?, ?) "
            "ON CONFLICT(job) DO UPDATE SET last_ok=excluded.last_ok",
            (name, _dt.now(_tzc.utc).isoformat()),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[health] failed to record heartbeat '{name}': {e}")


def _hours_since_heartbeat(name: str) -> float | None:
    """Hours since the named job last succeeded, or None if never recorded."""
    from datetime import datetime as _dt, timezone as _tzc
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        row = conn.execute(
            "SELECT last_ok FROM system_health WHERE job=?", (name,)
        ).fetchone()
        conn.close()
        if not row or not row[0]:
            return None
        last = _dt.fromisoformat(row[0])
        return (_dt.now(_tzc.utc) - last).total_seconds() / 3600
    except Exception:
        return None


@tasks.loop(hours=6)
async def grader_watchdog():
    """Every 6h: if no nightly grade has succeeded in >30h, raise the alarm so the
    learning loop never silently starves. 30h covers a normal once-daily cadence
    plus slack for late West Coast finishes."""
    hrs = _hours_since_heartbeat("nightly_grade")
    if hrs is None or hrs <= 30:
        return
    msg = (f"⚠️ **Vortex grading has stalled** — no successful nightly grade in "
           f"{hrs:.0f}h. The learning loop and hitrate gates are starving. "
           f"Check that the bot stayed up overnight.")
    print(f"[health] {msg}")
    if ALERT_CHANNEL:
        try:
            channel = bot.get_channel(ALERT_CHANNEL)
            if channel:
                await channel.send(msg)
        except Exception as e:
            print(f"[health] failed to send alert: {e}")


@tasks.loop(minutes=30)
async def board_purge():
    """Every 30 min: drop rows for games that have started. Zero API calls."""
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, update_board.purge_started_games)
    except Exception as e:
        print(f"[board] Purge error: {e}")


@tasks.loop(minutes=30)
async def lineup_refresh():
    """Every 30 min during game hours: update lineup status on the existing board.
    Removes scratched players, confirms lineups (⏳→✅), demotes bottom-of-order
    hitters. Uses ONLY the free MLB Stats API — zero Odds-API credits spent."""
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    hour = _dt.now(_tz(_td(hours=-4))).hour   # Eastern wall-clock gate
    if not (hour >= 9 or hour <= 3):          # 9 AM–3 AM ET (covers all slates)
        return
    try:
        loop = asyncio.get_event_loop()
        s = await loop.run_in_executor(None, refresh_live.refresh)
        if s.get("scratched") or s.get("confirmed") or s.get("demoted"):
            print(f"[lineup] refreshed — {s['confirmed']} confirmed · "
                  f"{s['scratched']} scratched · {s['demoted']} demoted")
    except Exception as e:
        print(f"[lineup] refresh error: {e}")


@tasks.loop(minutes=5)
async def live_grader():
    """Every 5 min during game hours: grade live. Overs lock ✅ the moment they
    clear, Unders lock ❌ the moment they bust; the rest finalize at game end.
    Window: 11 AM–3 AM ET (covers first pitch through late West Coast finishes)."""
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    hour = _dt.now(_tz(_td(hours=-4))).hour   # Eastern wall-clock gate
    if not (hour >= 11 or hour <= 3):
        return
    today = vortextime.vortex_day()           # betting day (rolls 4 AM Mountain)
    try:
        loop = asyncio.get_event_loop()
        summary = await loop.run_in_executor(None, grader.grade_date, today)
        if summary.get("graded", 0):
            print(f"[live_grader] Auto-graded {summary['graded']} picks for {today}")
    except Exception as e:
        print(f"[live_grader] Error: {e}")


@tasks.loop(time=_dtime(hour=14, minute=0, tzinfo=_tz.utc))  # 10 AM ET
async def daily_board_refresh():
    """Once per day at noon ET: re-fetch props from Odds API and rebuild the board."""
    print("[board] Daily board refresh ...")
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, update_board.main)
        print("[board] Daily board refresh complete")
    except Exception as e:
        print(f"[board] Daily refresh error: {e}")


@bot.event
async def on_ready():
    init_db.init()
    nightly_grader.start()
    live_grader.start()
    board_purge.start()
    lineup_refresh.start()
    daily_board_refresh.start()
    auto_nrfi.start()
    auto_moneyline.start()
    grader_watchdog.start()
    await tree.sync()
    print(f"VORTEX online as {bot.user} · {len(tree.get_commands())} commands synced")


# ── DM auto-analyze: bet slip images auto-graded in DMs ────────────────────────

_team_map_dm = {
    133:"OAK",134:"PIT",135:"SD",136:"SEA",137:"SF",138:"STL",
    139:"TB",140:"TEX",141:"TOR",142:"MIN",143:"PHI",144:"ATL",
    145:"CWS",146:"MIA",147:"NYM",158:"MIL",108:"LAA",109:"ARI",
    110:"BAL",111:"BOS",112:"CHC",113:"CIN",114:"CLE",115:"COL",
    116:"DET",117:"HOU",118:"KC",119:"LAD",120:"WSH",121:"NYY",
}


async def _grade_single_prop(prop: dict) -> tuple:
    """
    Grade a single prop dict through the full pipeline.
    Returns (embed, grade_dict, player_name, error_str).
    On error, embed is None and error_str is set.
    """
    loop = asyncio.get_event_loop()

    player_name_raw = prop["player_name"]
    line            = prop["line"]
    side            = prop.get("side", "over")
    prop_type       = prop.get("prop_type") or vortex_analyze.normalize_market(prop.get("market_raw") or "")

    print(f"[analysis] player={player_name_raw} side={side.upper()} line={line} market={prop_type}")

    # Hard gate: direction must be confirmed
    if not side or side not in ("over", "under"):
        return None, None, player_name_raw, f"Could not determine Over/Under direction for {player_name_raw}"

    # Player lookup
    try:
        matches = await loop.run_in_executor(None, vortex_research.fuzzy_search, player_name_raw)
    except Exception as exc:
        return None, None, player_name_raw, f"Player lookup failed: {exc}"

    if not matches:
        return None, None, player_name_raw, f"Couldn't find MLB player matching \"{player_name_raw}\""

    found       = matches[0]
    player_id   = found["id"]
    player_name = found["name"]

    # Resolve team
    try:
        _team_id = await loop.run_in_executor(None, lambda: stats_mlb.get_player_current_team(player_id))
        team = _team_map_dm.get(_team_id, found.get("team", ""))
    except Exception:
        team = found.get("team", "")

    # Hit rates
    if prop_type == "strikeouts":
        splits = {}
    else:
        try:
            splits = await loop.run_in_executor(
                None, lambda: vortex_analyze.compute_hit_rates(player_id, line, prop_type)
            )
        except Exception as exc:
            return None, None, player_name, f"Stats fetch failed: {exc}"
        if "error" in splits:
            return None, None, player_name, splits["error"]

    # Matchup
    try:
        matchup = await loop.run_in_executor(None, lambda: vortex_analyze.get_matchup_info(player_id))
    except Exception:
        matchup = {}

    pitcher_id  = matchup.get("pitcher_id")
    pitcher_nm  = matchup.get("pitcher")
    opp_team_id = matchup.get("opp_team_id")

    # Parallel data fetch
    async def _safe(fn, default=None):
        try:
            return await loop.run_in_executor(None, fn)
        except Exception:
            return default if default is not None else {}

    (bvp, pitcher, weather, team_bvp_data, oaa_data, arsenal, bat_vs_pitch,
     statcast_data, bullpen_data, umpire_data, batter_hand, lineup_spot,
     team_h2h_data, vs_hand_splits_data) = await asyncio.gather(
        _safe(lambda: stats_mlb.get_bvp_history(player_id, pitcher_id) if pitcher_id else {}),
        _safe(lambda: stats_mlb.get_pitcher_metrics(pitcher_nm) if pitcher_nm else {}),
        _safe(lambda: stats_mlb.get_game_weather(_team_map_dm.get(matchup.get("home_team_id"), ""), matchup.get("game_utc", "")) if matchup.get("home_team_id") else {}),
        _safe(lambda: stats_mlb.get_team_bvp(player_id, opp_team_id) if opp_team_id else {}),
        _safe(lambda: stats_mlb.get_team_defense_oaa(opp_team_id) if opp_team_id else {}),
        _safe(lambda: stats_mlb.get_pitcher_arsenal(pitcher_id) if pitcher_id else [], default=[]),
        _safe(lambda: stats_mlb.get_batter_vs_pitch_type(player_id, pitcher_id) if player_id and pitcher_id else [], default=[]),
        _safe(lambda: stats_mlb.get_statcast_by_id(player_id) if player_id else {}),
        _safe(lambda: stats_mlb.get_bullpen_stats(opp_team_id) if opp_team_id else {}),
        _safe(lambda: stats_mlb.get_game_umpire(matchup.get("home_team_id")) if matchup.get("home_team_id") else {}),
        _safe(lambda: stats_mlb.get_player_bat_side(player_id) if player_id else "", default=""),
        _safe(lambda: stats_mlb.get_lineup_position(player_id) if player_id else None, default=None),
        _safe(lambda: stats_mlb.get_vs_team_splits(player_id, opp_team_id, line, prop_type)
              if player_id and opp_team_id else {}),
        _safe(lambda: stats_mlb.get_batter_hand_splits(player_id) if player_id else {}, default={}),
    )

    # Opponent K-rate + park factor
    opp_k_rank = None
    opp_k_pct  = None
    try:
        opp_team_name = matchup.get("opponent", "")
        if opp_team_name:
            all_k_rates = stats_mlb.get_all_teams_k_rate()
            for _tid, kd in all_k_rates.items():
                if kd.get("name", "").lower() in opp_team_name.lower() or \
                   opp_team_name.lower() in kd.get("name", "").lower():
                    opp_k_rank = kd.get("rank")
                    _raw        = kd.get("k_pct")
                    opp_k_pct  = (_raw / 100) if _raw is not None else None
                    break
    except Exception:
        pass

    park_factor = 1.0
    try:
        opp_name = matchup.get("opponent", "")
        is_home  = matchup.get("is_home")
        if is_home is False and opp_name:
            park_factor = stats_mlb.PARK_FACTOR.get(opp_name, 1.0)
        elif is_home is True:
            for full_name, pf in stats_mlb.PARK_FACTOR.items():
                if team and team.upper() in full_name.upper():
                    park_factor = pf
                    break
    except Exception:
        pass

    # K-prop override
    if prop_type == "strikeouts":
        try:
            _k_card = await loop.run_in_executor(
                None, lambda: stats_mlb.get_pitcher_k_card(
                    player_name, line, opp_team_id, pitcher_id=player_id
                )
            )
            if _k_card.get("error"):
                return None, None, player_name, f"No K-prop data for {player_name}: {_k_card['error']}"
            _ks = dict(_k_card.get("splits", {}))
            _ks["recent_games"] = [
                {
                    "date":     s.get("date", ""),
                    "opponent": s.get("opponent", ""),
                    "value":    s.get("k", 0),
                    "over":     s.get("k", 0) > line,
                }
                for s in _k_card.get("last_5_starts", [])
            ]
            splits  = _ks
            pitcher = _k_card
            opp_k_d = _k_card.get("opp_k") or {}
            if opp_k_d:
                opp_k_rank = opp_k_d.get("rank")
                _raw_kpct  = opp_k_d.get("k_pct")
                opp_k_pct  = (_raw_kpct / 100) if _raw_kpct is not None else None
        except Exception:
            return None, None, player_name, f"K-prop lookup failed for {player_name}"

    # Grade BOTH sides independently
    both = vortex_analyze.grade_pick_both(
        splits, line,
        opp_k_rank=opp_k_rank, opp_k_pct=opp_k_pct,
        pitcher=pitcher, bvp=bvp, park_factor=park_factor,
        weather=weather, team_bvp=team_bvp_data, oaa=oaa_data,
        prop_type=prop_type,
        lineup_spot=lineup_spot if isinstance(lineup_spot, int) else None,
        statcast=statcast_data or None,
        team_h2h=team_h2h_data or None,
        arsenal=arsenal or None,
        bat_vs_pitch=bat_vs_pitch or None,
        vs_hand_splits=vs_hand_splits_data or None,
        umpire=umpire_data or None,
    )

    # The grade dict for the USER's selected side — so the header emoji/score
    # reflects what they actually picked. The model verdict is shown via side_comparison.
    grade = both["under_grade"] if side == "under" else both["over_grade"]

    # Debug logging
    print(f"[analysis] selected_side={side.upper()} over_score={both['over_score']} under_score={both['under_score']} model_verdict={both['model_verdict'].upper()} confidence={both['confidence']}")

    # Build embed
    _game_times = await _fetch_game_times()
    _game_time  = _game_times.get((pitcher_nm or "").lower().strip())

    vs_hand_splits = vs_hand_splits_data or {}
    embed = vortex_analyze.build_analyze_embed(
        player_name=player_name, team=team, prop_type=prop_type,
        line=line, splits=splits, grade=grade, matchup=matchup,
        bvp=bvp, side=side,
        pitcher_card=pitcher if pitcher and not pitcher.get("error") else None,
        weather=weather, team_bvp=team_bvp_data, oaa=oaa_data,
        arsenal=arsenal, bat_vs_pitch=bat_vs_pitch, statcast=statcast_data,
        bullpen=bullpen_data, umpire=umpire_data,
        batter_hand=batter_hand or "", park_factor=park_factor,
        lineup_spot=lineup_spot if isinstance(lineup_spot, int) else None,
        game_time=_game_time, vs_hand_splits=vs_hand_splits,
        team_h2h=team_h2h_data or None,
        side_comparison=both,
    )

    return embed, grade, player_name, None, splits


@bot.event
async def on_message(message: discord.Message):
    # Only auto-analyze bet slips in DMs (not in servers, not from bots)
    if message.guild is not None:
        return
    if message.author.bot:
        return

    # Must have image attachments
    images = [a for a in message.attachments
              if a.content_type and a.content_type.startswith("image/")]
    if not images:
        return

    # React to show we're working
    working_msg = None
    try:
        working_msg = await message.channel.send("🔍 Analyzing your slip...")
    except Exception:
        pass

    loop = asyncio.get_event_loop()

    all_legs = []       # list of (embed, grade_dict, player_name, prop_dict)
    errors   = []

    for img in images:
        try:
            img_bytes = await img.read()
            slip = await vortex_analyze.extract_slip_data(img_bytes)
        except Exception as exc:
            errors.append(f"Image read error: {exc}")
            continue

        if "error" in slip:
            errors.append(slip["error"])
            continue

        # Collect all props from this image
        props = slip.get("all_props") or [slip]
        for p in props:
            if not p.get("player_name") or not p.get("line"):
                errors.append(f"Skipping incomplete prop: {p}")
                continue
            # Hard gate: direction must be confirmed before analysis
            if not p.get("side") or p["side"] not in ("over", "under"):
                errors.append(f"**{p.get('player_name', '?')}**: Could not determine Over/Under direction from the slip")
                continue
            embed, grade, pname, err, prop_splits = await _grade_single_prop(p)
            if err:
                errors.append(f"**{p.get('player_name', '?')}**: {err}")
            else:
                all_legs.append((embed, grade, pname, p, prop_splits))

    if not all_legs:
        err_text = "\n".join(errors) if errors else "Could not read any props from the image."
        await message.channel.send(
            f"❌ Couldn't grade that slip:\n{err_text}",
        )
        return

    # Remove the "Analyzing your slip..." message
    if working_msg:
        try:
            await working_msg.delete()
        except Exception:
            pass

    # ── Single prop ────────────────────────────────────────────────────────────
    if len(all_legs) == 1:
        embed, grade, pname, _err, p = all_legs[0]
        await message.channel.send(embed=embed)
        if errors:
            await message.channel.send("⚠️ Notes:\n" + "\n".join(errors))
        return

    # ── Multi-leg parlay ───────────────────────────────────────────────────────
    # Send each individual card
    for embed, grade, pname, p, prop_splits in all_legs:
        try:
            await message.channel.send(embed=embed)
        except Exception:
            pass

    # Build parlay summary
    n_legs = len(all_legs)

    # Combined probability from ACTUAL L10 hit rates
    combined_prob = 1.0
    leg_probs = []
    for embed, grade, pname, p, prop_splits in all_legs:
        side = p.get("side", "over")
        l10 = (prop_splits or {}).get("l10") or {}
        l10_rate = l10.get("rate", 0) or 0
        # Effective hit rate for the selected side
        effective_l10 = (100 - l10_rate) if side == "under" else l10_rate
        # Clamp to [5%, 95%] to avoid degenerate parlay probabilities
        leg_prob = max(0.05, min(0.95, effective_l10 / 100))
        leg_probs.append(leg_prob)
        combined_prob *= leg_prob

    # Parlay penalty: each leg beyond 2 reduces confidence further
    if n_legs > 2:
        penalty = 0.95 ** (n_legs - 2)
        combined_prob *= penalty

    # Parlay tier
    if   combined_prob >= 0.35: parlay_tier = "ELITE";  parlay_emoji = "💎"
    elif combined_prob >= 0.22: parlay_tier = "STRONG"; parlay_emoji = "🔥"
    elif combined_prob >= 0.12: parlay_tier = "GOOD";   parlay_emoji = "✅"
    elif combined_prob >= 0.06: parlay_tier = "LEAN";   parlay_emoji = "➡️"
    else:                       parlay_tier = "RISKY";  parlay_emoji = "⚠️"

    # Average leg score and L10
    avg_score = sum(g.get("score", 0) for _, g, _, _, _ in all_legs) / n_legs
    avg_l10 = sum(leg_probs) / n_legs * 100

    # Tier distribution
    tier_counts = {}
    for _, g, _, _, _ in all_legs:
        t = g.get("label", "?")
        tier_counts[t] = tier_counts.get(t, 0) + 1

    tier_str = " · ".join(f"{v}×{k}" for k, v in sorted(tier_counts.items(), key=lambda x: -x[1]))

    # ── Parlay verdict + warnings ────────────────────────────────────────────
    # Find the weakest leg
    worst_idx = min(range(n_legs), key=lambda i: leg_probs[i])
    worst_embed, worst_grade, worst_name, worst_p, worst_splits = all_legs[worst_idx]
    worst_prob = leg_probs[worst_idx] * 100
    worst_label = worst_grade.get("label", "?")
    worst_side = "More" if worst_p.get("side") == "over" else "Less"
    worst_score = worst_grade.get("score", 0)

    # Find fading legs
    fading_legs = []
    for i, (_, g, pname, p, ps) in enumerate(all_legs):
        label = g.get("label", "?")
        if label in ("RISKY", "FADE") or leg_probs[i] < 0.50:
            side = "More" if p.get("side") == "over" else "Less"
            l10 = (ps or {}).get("l10") or {}
            l10_rate = l10.get("rate", 0) or 0
            eff = (100 - l10_rate) if p.get("side") == "under" else l10_rate
            fading_legs.append((pname, label, g.get("score", 0), eff, side, p["line"], p.get("market_raw", "")))

    # Verdict header
    if parlay_tier in ("ELITE", "STRONG"):
        verdict_header = f"✅ **{parlay_tier} Parlay**"
        verdict_color = discord.Color.green()
    elif parlay_tier == "GOOD":
        verdict_header = f"✅ **{parlay_tier} Parlay**"
        verdict_color = discord.Color.blue()
    elif parlay_tier == "LEAN":
        verdict_header = f"⚠️ **{parlay_tier} Parlay — Caution**"
        verdict_color = discord.Color.orange()
    else:
        verdict_header = f"⚠️ **{parlay_tier} Parlay — Risky**"
        verdict_color = discord.Color.red()

    desc_lines = [f"{verdict_header}"]

    # Warnings for bad legs
    if fading_legs:
        for fname, flabel, fscore, feff, fside, fline, fmarket in fading_legs:
            if flabel in ("RISKY", "FADE"):
                desc_lines.append(f"🔴 **{flabel}:** {fname} is {feff:.0f}% L10 — line may be mispriced.")
        if parlay_tier in ("LEAN", "RISKY"):
            desc_lines.append(f"🔴 Fade or rebuild. At least one leg is statistically poor.")
        if worst_prob < 50:
            desc_lines.append(f"❌ **Cold streak:** {worst_name} is {worst_prob:.0f}% L10 over {worst_p['line']} {worst_p.get('market_raw', '')}")

    desc_lines.append("")

    # Leg-by-leg breakdown
    for i, (embed, grade, pname, p, prop_splits) in enumerate(all_legs, 1):
        label = grade.get("label", "?")
        score = grade.get("score", 0)
        sw = "More" if p.get("side") == "over" else "Less"
        _icons = {"ELITE": "💎", "STRONG": "🔥", "GOOD": "✅", "LEAN": "➡️", "RISKY": "⚠️", "FADE": "🚫"}
        icon = _icons.get(label, "")

        side = p.get("side", "over")
        l10 = (prop_splits or {}).get("l10") or {}
        l10_rate = l10.get("rate", 0) or 0
        eff_l10 = (100 - l10_rate) if side == "under" else l10_rate
        leg_p = leg_probs[i - 1] * 100

        # Form lines
        l5 = (prop_splits or {}).get("l5") or {}
        l5_rate = l5.get("rate", 0) or 0
        eff_l5 = (100 - l5_rate) if side == "under" else l5_rate

        desc_lines.append(
            f"**{i}.** {icon} **{pname}** — {sw} {p['line']} {p.get('market_raw', '')} — "
            f"**{label}** ({score:+d})"
        )
        desc_lines.append(
            f"    L5: **{eff_l5:.0f}%** · L10: **{eff_l10:.0f}%** → implied **{leg_p:.0f}%**"
        )
        desc_lines.append("")

    summary = discord.Embed(
        title=f"📋 Parlay Summary — {n_legs} Legs",
        description="\n".join(desc_lines),
        color=verdict_color,
    )

    # Grade + probability
    summary.add_field(
        name=f"Parlay Grade",
        value=(
            f"**{parlay_tier}** — Combined: **{combined_prob * 100:.1f}%**\n"
            f"Average L10: **{avg_l10:.0f}%** · Avg score: **{avg_score:+.1f}**\n"
            f"Legs: {tier_str}"
        ),
        inline=False,
    )

    # Recommendation
    if n_legs > 2:
        summary.add_field(
            name="💡 Recommendation",
            value=f"Every additional leg **reduces** your chance to hit.\n"
                  f"Consider a **2-leg version** for better value.",
            inline=False,
        )

    summary.set_footer(text=f"VORTEX Parlay Grade · {interaction.user.display_name}")
    summary.timestamp = discord.utils.utcnow()

    await message.channel.send(embed=summary)

    if errors:
        await message.channel.send("⚠️ Notes:\n" + "\n".join(errors))


# ── /dashboard ─────────────────────────────────────────────────────────────────
@tree.command(name="dashboard", description="⚡ Tonight's full board — all props ranked by score")
async def cmd_dashboard(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    rows = get_board(limit=30)
    if not rows:
        await interaction.followup.send("Board is empty — run the engine first.", ephemeral=True)
        return
    game_times = await _fetch_game_times()
    embeds = board_embed(rows, "⚡ Tonight's Board — VORTEX", game_times=game_times)
    await interaction.followup.send(embeds=embeds, view=BoardView(), ephemeral=True)


# ── /picks ─────────────────────────────────────────────────────────────────────
@tree.command(name="picks", description="🎯 Tonight's top MLB props — ranked by hit rate")
async def cmd_picks(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        rows = get_board(sport="MLB")
        if not rows:
            await interaction.followup.send("No MLB plays on the board right now.", ephemeral=True)
            return
        game_times = await _fetch_game_times()
        embeds = board_embed(rows, "⚾ Tonight's MLB Props — VORTEX", game_times=game_times)
        view   = BoardDetailView(rows)
        await interaction.followup.send(embeds=embeds, view=view, ephemeral=True)
    except Exception as exc:
        import traceback
        await interaction.followup.send(f"❌ Error: ```{traceback.format_exc()[-1800:]}```", ephemeral=True)


# ── /elite ─────────────────────────────────────────────────────────────────────
@tree.command(name="elite", description="💎 ELITE tier plays only")
async def cmd_elite(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    rows = get_board(tier="ELITE", limit=15)
    if not rows:
        await interaction.followup.send("No ELITE plays right now.", ephemeral=True)
        return
    game_times = await _fetch_game_times()
    embeds = board_embed(rows, "💎 ELITE Plays Tonight — VORTEX", game_times=game_times)
    await interaction.followup.send(embeds=embeds, view=BoardDetailView(rows), ephemeral=True)


# ── /strikeouts ────────────────────────────────────────────────────────────────
@tree.command(name="strikeouts", description="⚾ MLB pitcher strikeout props tonight")
async def cmd_strikeouts(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    rows = get_board(sport="MLB", stat_filter="strikeout", limit=15)
    if not rows:
        await interaction.followup.send("No strikeout props on the board tonight.", ephemeral=True)
        return
    game_times = await _fetch_game_times()
    embeds = board_embed(rows, "⚾ Strikeout Props Tonight — VORTEX", game_times=game_times)
    await interaction.followup.send(embeds=embeds, view=BoardDetailView(rows), ephemeral=True)


# ══ /nuke — single strongest 2-leg MLB parlay ════════════════════════════════
# Honest scope: Vortex does not have wind-direction, injury-report, or live
# market-steam feeds, so /nuke grades each leg from the signals it DOES compute —
# real-market EV (sharp/consensus anchor), projection edge, confirmed lineup,
# batting-order (a proxy for expected PA), opposing pitcher, park, weather, and
# L10 hit rate. Confidence (x/5) is derived transparently from those below; tune
# the weights here, not in prose.
_NUKE_PITCHER_KW = ("strikeout", "outs", "earned runs", "hits allowed", "walks allowed")
_NUKE_HITTER_KW  = ("hits+runs", "hrr", "total bases", "hits", "runs", "rbi", "singles", "walks")
_NUKE_EXCLUDE_KW = ("fantasy",)

def _nuke_kind(stat_type: str):
    s = (stat_type or "").lower()
    if any(k in s for k in _NUKE_EXCLUDE_KW):
        return None
    if any(k in s for k in ("strikeout", "outs", "hits allowed", "earned runs")) or ("walks" in s and "allowed" in s):
        return "pitcher"
    if any(k in s for k in _NUKE_HITTER_KW):
        return "hitter"
    return None

def _nuke_exp_pa(sj: dict):
    lp = sj.get("lineup_pos")
    if isinstance(lp, int) and lp >= 1:
        return {1: 4.6, 2: 4.5, 3: 4.4, 4: 4.3, 5: 4.1, 6: 3.9, 7: 3.8, 8: 3.6, 9: 3.5}.get(lp, 4.0)
    return None

def _f(x):
    """Best-effort float parse ('.142', '5.10', None → None)."""
    try:
        s = str(x)
        return float("0" + s) if s.startswith(".") else float(s)
    except (ValueError, TypeError):
        return None

def _nuke_matchup(leg) -> float:
    """Side-aware MATCHUP QUALITY in roughly −1.0 … +1.0. This is the heart of a
    NUKE: it rewards GOOD MATCHUPS (weak opposing pitcher, platoon edge, favorable
    park/weather, BvP history) — NOT hot streaks. Positive = matchup favors the
    side we're betting. Neutral (0) when data is missing."""
    sj   = leg["sj"]
    over = sj.get("side") != "under"
    s    = 0.0

    if leg["kind"] == "hitter":
        pit = sj.get("pitcher") or {}
        era = _f(pit.get("era")); fip = _f(pit.get("fip")); hr9 = _f(pit.get("hr_per_9"))
        # Opposing pitcher quality — the single biggest matchup lever.
        ref = min([v for v in (era, fip) if v is not None], default=None)
        if ref is not None:
            if   ref >= 5.50: s += 0.55
            elif ref >= 4.50: s += 0.35
            elif ref >= 3.75: s += 0.10
            elif ref >= 3.20: s -= 0.20
            else:             s -= 0.45      # elite arm — bad spot for an Over
        if hr9 is not None:
            s += 0.20 if hr9 >= 1.20 else (0.0 if hr9 >= 0.90 else -0.15)
        whip = _f(pit.get("whip"))
        if whip is not None:
            s += 0.15 if whip >= 1.40 else (-0.15 if whip <= 1.10 else 0.0)
        # How hitters have actually produced against this arm.
        opsa = _f(pit.get("ops_against"))
        if opsa is not None:
            s += 0.15 if opsa >= 0.780 else (-0.15 if opsa <= 0.650 else 0.0)
        # Platoon edge (handedness) — from the prepared note.
        note = (sj.get("platoon_note") or "").lower()
        if "advantage for the batter" in note:
            s += 0.25
        elif "pitcher advantage" in note:
            s -= 0.20
        # Batter-vs-pitcher history.
        bvp = _f((sj.get("bvp") or {}).get("avg"))
        if bvp is not None and (sj.get("bvp") or {}).get("ab"):
            if   bvp >= 0.320: s += 0.15
            elif bvp <= 0.180: s -= 0.15
        # Park + weather (when populated).
        pf = _f((sj.get("park") or {}).get("factor"))
        if pf is not None:
            s += 0.15 if pf >= 1.05 else (-0.15 if pf <= 0.95 else 0.0)
        wb = sj.get("weather_boost")
        if isinstance(wb, (int, float)) and wb:
            s += 0.10 if wb > 0 else -0.10
        if not over:
            s = -s                            # everything flips for an Under
    else:
        # Pitcher prop (e.g. strikeouts Over): a weak/whiff-prone opposing lineup
        # is the good matchup. opp_k carries the opponent K profile when available.
        opp = sj.get("opp_k") or {}
        kpct = _f(opp.get("k_pct")); rank = opp.get("rank")
        if kpct is not None:
            s += 0.40 if kpct >= 25 else (0.15 if kpct >= 22 else (-0.20 if kpct <= 18 else 0.0))
        if isinstance(rank, int) and rank:
            s += 0.20 if rank <= 8 else (-0.15 if rank >= 23 else 0.0)
        if not over:
            s = -s

    return max(-1.0, min(1.0, s))

# Confidence hierarchy weights (must sum to 1.0). Matchup dominates; recent form
# is hard-capped at 10% so a hot streak alone can never manufacture a NUKE.
_NUKE_WEIGHTS = {
    "matchup":     0.35,   # 1. Matchup quality  (most important)
    "proj_edge":   0.20,   # 2. Projection edge
    "market_ev":   0.18,   # 3. Market edge / EV
    "opportunity": 0.12,   # 4. Expected opportunity (PA / lineup spot / outs)
    "park_weather":0.05,   # 5. Park & weather
    "form":        0.10,   # 6. Recent form  (least important — 10%, never more)
}

def _nuke_opportunity(leg) -> float:
    """0..1 expected-opportunity. Hitters: batting-order PA proxy. Pitchers:
    projected workload/role."""
    sj = leg["sj"]
    if leg["kind"] == "hitter":
        lp = sj.get("lineup_pos")
        if isinstance(lp, int) and lp >= 1:
            return {1: 1.0, 2: 1.0, 3: 0.9, 4: 0.85, 5: 0.7, 6: 0.55, 7: 0.45, 8: 0.3, 9: 0.25}.get(lp, 0.5)
        return 0.5
    pit = sj.get("pitcher") or {}
    ip = _f(pit.get("innings_pitched")); avg_ip = _f(pit.get("avg_ip_l3"))
    role = (pit.get("validated_role") or "").upper()
    if role == "SWINGMAN":
        return 0.4
    if avg_ip is not None:
        return max(0.3, min(1.0, avg_ip / 6.0))
    return 0.6

def _nuke_parkweather(leg) -> float:
    """0..1 park+weather, side-aware. Neutral (0.5) when not populated."""
    sj = leg["sj"]; over = sj.get("side") != "under"; s = 0.5
    pf = _f((sj.get("park") or {}).get("factor"))
    if pf is not None:
        bump = 0.25 if pf >= 1.05 else (-0.25 if pf <= 0.95 else 0.0)
        s += bump if over else -bump
    wb = sj.get("weather_boost")
    if isinstance(wb, (int, float)) and wb:
        bump = 0.2 if wb > 0 else -0.2
        s += bump if over else -bump
    return max(0.0, min(1.0, s))

def _nuke_confidence(row, sj: dict) -> float:
    """0–5 confidence built from the fixed hierarchy: matchup → projection edge →
    market EV → opportunity → park/weather → form. Each bucket scores 0..1, is
    weighted by _NUKE_WEIGHTS, summed to a 0..1 quality, then mapped to 2.5–5.0.
    Recent form can contribute at most its 10% weight — a streak can't carry a play."""
    ev   = row["ev_percentage"] or 0
    kind = _nuke_kind(row["stat_type"]) or "hitter"
    leg  = {"sj": sj, "kind": kind}

    # 1. Matchup quality (−1..+1 → 0..1)
    b_match = (_nuke_matchup(leg) + 1) / 2
    # 2. Projection edge: 0% → 0, 12% → 0.5, 25%+ → 1.0
    pe = sj.get("proj_edge")
    b_proj = min(1.0, max(0.0, pe) / 25) if isinstance(pe, (int, float)) else 0.3
    # 3. Market edge / EV: +8% → 1.0, 0 → 0.5, −8% → 0
    if sj.get("ev_real"):
        b_ev = max(0.0, min(1.0, 0.5 + ev * 0.0625))
    else:
        b_ev = 0.25                       # no real two-sided market = weak edge signal
    # 4. Expected opportunity
    b_opp = _nuke_opportunity(leg)
    # 5. Park & weather
    b_pw = _nuke_parkweather(leg)
    # 6. Recent form (capped influence by the 10% weight)
    l10 = sj.get("eff_l10")
    b_form = max(0.0, min(1.0, l10 / 100)) if isinstance(l10, (int, float)) else 0.5

    quality = (
        _NUKE_WEIGHTS["matchup"]      * b_match +
        _NUKE_WEIGHTS["proj_edge"]    * b_proj +
        _NUKE_WEIGHTS["market_ev"]    * b_ev +
        _NUKE_WEIGHTS["opportunity"]  * b_opp +
        _NUKE_WEIGHTS["park_weather"] * b_pw +
        _NUKE_WEIGHTS["form"]         * b_form
    )
    c = 2.5 + quality * 2.5
    # Unconfirmed lineup is a real risk for a max-unit play.
    if leg["kind"] == "hitter" and sj.get("lineup_confirmed") is False:
        c -= 0.3
    # Honesty cap: no real positive market edge → never a confident/NUKE play.
    if not (sj.get("ev_real") and (ev or 0) > 0):
        c = min(c, 3.9)
    return round(max(0.0, min(5.0, c)), 1)

def _nuke_teamkey(row, sj: dict) -> str:
    """Correlation key. Two hitters facing the SAME opposing pitcher are
    teammates (same lineup) → same key → blocked unless the DK exception."""
    pit = (sj.get("pitcher") or {}).get("name")
    if pit:
        return "opp:" + pit.lower()
    return "self:" + (row["player_name"] or "").lower()

def _am_to_dec(o):
    o = int(o); return (o / 100) + 1 if o > 0 else (100 / abs(o)) + 1

def _dec_to_am(d):
    return f"+{round((d-1)*100)}" if d >= 2 else f"{round(-100/(d-1))}"

def _nuke_side_prob(sj: dict) -> float | None:
    tp = sj.get("true_prob")
    if not isinstance(tp, (int, float)):
        return None
    return (1 - tp) if sj.get("side") == "under" else tp

def _nuke_stars(c: float) -> str:
    return f"{'★' * int(round(c))} {c:.1f}/5"

def _nuke_legs(rows):
    """Build annotated leg dicts for every MLB candidate row."""
    legs = []
    for r in rows:
        if (r["sport"] or "") != "MLB":
            continue
        kind = _nuke_kind(r["stat_type"])
        if kind is None:
            continue
        sj = _row_sj(r)
        legs.append({
            "row": r, "sj": sj, "kind": kind,
            "player": r["player_name"], "book": (r["sportsbook"] or "").strip(),
            "team": _nuke_teamkey(r, sj),
            "conf": _nuke_confidence(r, sj),
            "ev": r["ev_percentage"] or 0,
            "ev_real": bool(sj.get("ev_real")) and (r["ev_percentage"] or 0) > 0,
            "proj_edge": sj.get("proj_edge") if isinstance(sj.get("proj_edge"), (int, float)) else None,
            "odds": sj.get("best_odds"),
            "side_prob": _nuke_side_prob(sj),
            "exp_pa": _nuke_exp_pa(sj),
        })
    return legs

def _nuke_leg_eligible(leg) -> bool:
    """Hard gates for a NUKE leg (max-unit standard)."""
    if not leg["ev_real"]:
        return False                                   # positive REAL-market EV required
    if leg["proj_edge"] is None or leg["proj_edge"] < 12:
        return False                                   # projection edge >= 12%
    if leg["kind"] == "hitter":
        if leg["sj"].get("lineup_confirmed") is False:
            return False                               # must be in confirmed lineup
        if leg["exp_pa"] is not None and leg["exp_pa"] < 4.0:
            return False                               # expected PA >= 4.0
    if leg["conf"] < 4.5:
        return False
    if leg["odds"] is None:
        return False
    return True

def _nuke_pairs(legs, *, min_conf, require_eligible, require_diff_team):
    """Yield valid (a, b) leg pairs honoring book + team rules. Best-first by a
    composite of win prob, confidence, EV."""
    pool = [l for l in legs if l["conf"] >= min_conf and (not require_eligible or _nuke_leg_eligible(l))]
    out = []
    for i in range(len(pool)):
        for j in range(i + 1, len(pool)):
            a, b = pool[i], pool[j]
            if a["player"] == b["player"]:
                continue                               # never two legs on the same player
            if not a["book"] or a["book"] != b["book"]:
                continue                               # same sportsbook required
            same_team = a["team"] == b["team"]
            if same_team:
                # DraftKings same-team exception — both elite (>=4.8) and combined >4.8
                is_dk = a["book"].lower().replace(" ", "") in ("draftkings", "dk")
                if not (is_dk and a["conf"] >= 4.8 and b["conf"] >= 4.8
                        and (a["conf"] + b["conf"]) / 2 > 4.8):
                    continue
                if require_diff_team:
                    continue
            wp = ((a["side_prob"] or 0.5) * (b["side_prob"] or 0.5))
            key = (wp, (a["conf"] + b["conf"]) / 2, (a["ev"] + b["ev"]) / 2)
            out.append((key, a, b))
    out.sort(key=lambda t: t[0], reverse=True)
    return [(a, b) for _, a, b in out]

def select_nuke(rows):
    """Return ('NUKE', (a,b)) | ('CONFIDENT', (a,b)) | ('CONFIDENT1', (a,)) | ('NONE', None)."""
    legs = _nuke_legs(rows)
    # Top-5 projected edges on the slate (for the elite condition)
    edge_ranked = sorted([l for l in legs if l["proj_edge"] is not None],
                         key=lambda l: l["proj_edge"], reverse=True)[:5]
    top5 = {id(l) for l in edge_ranked}

    # 1) True NUKE — eligible pair, different teams (or DK exception), elite-gated
    for a, b in _nuke_pairs(legs, min_conf=4.5, require_eligible=True, require_diff_team=False):
        dec = _am_to_dec(a["odds"]) * _am_to_dec(b["odds"])
        # "-120 or better": reject only combos juiced shorter than -120 (decimal < 1.833)
        if dec < 1.833:
            continue
        wp = (a["side_prob"] or 0.5) * (b["side_prob"] or 0.5)
        elite = (id(a) in top5 or id(b) in top5
                 or wp > 0.60
                 or (a["conf"] >= 4.8 and b["conf"] >= 4.8))
        if elite:
            return "NUKE", (a, b)

    # 2) Confident 2-leg (not a nuke) — both legs >=4.0, same book, different team
    pairs = _nuke_pairs(legs, min_conf=4.0, require_eligible=False, require_diff_team=True)
    if pairs:
        return "CONFIDENT", pairs[0]

    # 3) Single confident play — best standalone >=4.0
    singles = sorted([l for l in legs if l["conf"] >= 4.0], key=lambda l: l["conf"], reverse=True)
    if singles:
        return "CONFIDENT1", (singles[0],)

    return "NONE", None

def _nuke_why(leg) -> list[str]:
    """Up to 3 concrete reasons this leg qualifies — MATCHUP first, then edge."""
    sj, bullets = leg["sj"], []
    # ── Lead with the matchup (the reason it's a NUKE leg) ──────────────────
    pit = (sj.get("pitcher") or {})
    mq  = _nuke_matchup(leg)
    if leg["kind"] == "hitter" and pit.get("name") and pit.get("era"):
        if mq >= 0.25:
            bullets.append(f"⚔️ **Plus matchup** vs {pit['name']} ({pit['era']} ERA, {pit.get('hr_per_9','?')} HR/9)")
        else:
            bullets.append(f"⚔️ vs {pit['name']} ({pit['era']} ERA)")
    note = (sj.get("platoon_note") or "")
    if "advantage for the batter" in note.lower() and len(bullets) < 3:
        bullets.append("🤝 Platoon edge — favorable handedness matchup")
    # ── Then the market edge ────────────────────────────────────────────────
    a = sj.get("anchor")
    if a == "sharp" and len(bullets) < 3:
        bullets.append(f"🎯 Sharp-anchored (Pinnacle) with **{_ev_str(leg['ev'])}** real EV")
    elif a == "consensus" and len(bullets) < 3:
        bullets.append(f"📊 Consensus-confirmed **{_ev_str(leg['ev'])}** market EV")
    if leg["proj_edge"] is not None and len(bullets) < 3:
        bullets.append(f"📈 Projection edge **{leg['proj_edge']:.0f}%** over the line")
    return bullets[:3] or ["Strong composite Vortex score on available signals"]

def _nuke_leg_block(n: int, leg) -> str:
    r, sj = leg["row"], leg["sj"]
    side = "OVER" if sj.get("side") != "under" else "UNDER"
    opp  = (sj.get("pitcher") or {}).get("name") or "—"
    l10  = sj.get("eff_l10")
    hit  = f"{l10:.0f}% L10" if isinstance(l10, (int, float)) else "—"
    pe   = f"{leg['proj_edge']:.0f}%" if leg["proj_edge"] is not None else "—"
    why  = "\n".join(f"• {b}" for b in _nuke_why(leg))
    return (
        f"**Player:** {leg['player']}\n"
        f"**Opp Pitcher:** {opp}\n"
        f"**Market:** {side} {r['line']} {r['stat_type']}\n"
        f"**Confidence:** {_nuke_stars(leg['conf'])}\n"
        f"**Projection Edge:** {pe}   ·   **Exp PA:** {leg['exp_pa'] or '—'}   ·   **Hit Rate:** {hit}\n"
        f"**Book:** {leg['book']}\n"
        f"**Why it qualifies:**\n{why}"
    )

def nuke_embed(kind, payload) -> discord.Embed:
    if kind == "NONE":
        e = discord.Embed(
            title="🚫 NO NUKE PLAY TODAY",
            description=(
                "No MLB props currently meet Vortex's elite confidence thresholds.\n"
                "The board does not contain a max-unit opportunity — and no play "
                "beats forcing a bad one.\n\n**Check back tomorrow.**"
            ),
            color=0x99AAB5,
        )
        e.set_footer(text="VORTEX · /nuke — quality over quantity")
        return e

    if kind == "CONFIDENT1":
        (leg,) = payload
        e = discord.Embed(
            title="⚠️ NO NUKE TODAY — but here's the day's most confident play",
            description=("No 2-leg parlay clears the NUKE bar, and only one prop reaches "
                         f"confident grade (**{_nuke_stars(leg['conf'])}**). Single play, size accordingly."),
            color=TIER_COLOR["RISKY"],
        )
        e.add_field(name="— CONFIDENT PLAY", value=_nuke_leg_block(1, leg), inline=False)
        e.set_footer(text="VORTEX · Not a NUKE · confident single")
        return e

    a, b = payload
    is_nuke = kind == "NUKE"
    dec = _am_to_dec(a["odds"]) * _am_to_dec(b["odds"])
    wp  = (a["side_prob"] or 0.5) * (b["side_prob"] or 0.5)
    avg_conf = (a["conf"] + b["conf"]) / 2
    avg_edge = (a["ev"] + b["ev"]) / 2
    corr = "LOW (different teams)" if a["team"] != b["team"] else "MODERATE (same team / DK exception)"

    if is_nuke:
        title = "☢️ VORTEX NUKE"
        color = TIER_COLOR["ELITE"]
        grade, risk, bank = "A+", "LOW", "🔥 MAX UNIT PLAY"
        head = f"**Sportsbook:** {a['book']}"
    else:
        title = "⚠️ NO NUKE TODAY — here's the day's most confident parlay"
        color = TIER_COLOR["GOOD"]
        grade, risk, bank = "A−", "MODERATE", "✅ STANDARD UNIT"
        head = (f"This 2-leg play didn't clear the elite NUKE bar, but both legs grade "
                f"4.0+/5.\n**Sportsbook:** {a['book']}")

    e = discord.Embed(title=title, description=head, color=color)
    e.add_field(name="☢️ LEG 1", value=_nuke_leg_block(1, a), inline=False)
    e.add_field(name="☢️ LEG 2", value=_nuke_leg_block(2, b), inline=False)
    e.add_field(
        name="— SUMMARY",
        value=(
            f"**Combined Odds:** {_dec_to_am(dec)}\n"
            f"**Est. Hit Probability:** {wp*100:.0f}%\n"
            f"**Average Confidence:** {_nuke_stars(avg_conf)}\n"
            f"**Average EV:** {_ev_str(avg_edge)}\n"
            f"**Correlation Risk:** {corr}"
        ),
        inline=False,
    )
    e.add_field(name="— GRADE", value=f"**{grade}**  ·  Risk: **{risk}**  ·  {bank}", inline=False)
    e.set_footer(text="VORTEX · /nuke" + (" · max-unit" if is_nuke else " · confident play"))
    return e

def _nuke_candidates():
    conn = _db()
    rows = conn.execute(
        f"SELECT * FROM props_board WHERE sport='MLB' AND {_LIVE_FILTER}"
    ).fetchall()
    conn.close()
    return rows

@tree.command(name="nuke", description="☢️ The single strongest MLB 2-leg parlay — or NO PLAY if none is elite")
async def cmd_nuke(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        rows = _nuke_candidates()
        kind, payload = select_nuke(rows)
        await interaction.followup.send(embed=nuke_embed(kind, payload), ephemeral=True)
    except Exception:
        import traceback
        await interaction.followup.send(f"❌ Error: ```{traceback.format_exc()[-1800:]}```", ephemeral=True)


# ── /hrr ───────────────────────────────────────────────────────────────────────
@tree.command(name="hrr", description="⚾ MLB Hits + Runs + RBIs and Home Run props")
async def cmd_hrr(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    conn = _db()
    rows = conn.execute(
        f"SELECT * FROM props_board WHERE sport='MLB' AND {_LIVE_FILTER} AND tier IN ('ELITE','STRONG') AND "
        "(LOWER(stat_type) LIKE '%hit%' OR LOWER(stat_type) LIKE '%rbi%' "
        " OR LOWER(stat_type) LIKE '%run%' OR LOWER(stat_type) LIKE '%home run%') "
        "ORDER BY vortex_score DESC LIMIT 15"
    ).fetchall()
    conn.close()
    if not rows:
        await interaction.followup.send("No H+R+RBI/HR props on the board tonight.", ephemeral=True)
        return
    game_times = await _fetch_game_times()
    embeds = board_embed(rows, "⚾ H+R+RBI & HR Props Tonight — VORTEX", game_times=game_times)
    await interaction.followup.send(embeds=embeds, view=BoardDetailView(rows), ephemeral=True)


# ── /nba ───────────────────────────────────────────────────────────────────────
@tree.command(name="nba", description="🏀 NBA props tonight")
async def cmd_nba(interaction: discord.Interaction):
    if not await _is_admin(interaction):
        await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    rows = get_board(sport="NBA", limit=10)
    if not rows:
        await interaction.followup.send("No NBA plays right now.", ephemeral=True)
        return
    embeds = board_embed(rows, "🏀 NBA Plays Tonight — VORTEX")
    await interaction.followup.send(embeds=embeds, view=BoardDetailView(rows), ephemeral=True)


# ── /rebuild ─────────────────────────────────────────────────────────────────
@tree.command(name="rebuild", description="🔄 (Admin) Rebuild the board now — re-fetches props & re-scores")
async def cmd_rebuild(interaction: discord.Interaction):
    if not await _is_admin(interaction):
        await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    await interaction.followup.send(
        "🔄 Rebuilding the board — fetching props and re-scoring all sports. "
        "This uses Odds-API credits and takes ~30–90s. I'll report back when done.",
        ephemeral=True,
    )
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, update_board.main)
    except Exception:
        import traceback
        await interaction.followup.send(
            f"❌ Rebuild failed:\n```{traceback.format_exc()[-1500:]}```", ephemeral=True
        )
        return
    # Report counts per sport
    try:
        conn = _db()
        counts = {s: conn.execute(
            f"SELECT COUNT(*) c FROM props_board WHERE sport=? AND {_LIVE_FILTER} AND tier IN ('ELITE','STRONG')",
            (s,)).fetchone()["c"] for s in ("MLB", "WNBA", "NBA")}
        conn.close()
        summary = " · ".join(f"{s}: {c}" for s, c in counts.items() if c or s in ("MLB", "WNBA"))
    except Exception:
        summary = "done"
    await interaction.followup.send(f"✅ Board rebuilt — {summary}.", ephemeral=True)


# ── /setoddskey ──────────────────────────────────────────────────────────────
# Swapping ODDS_API_KEY in .env needs a bot-host restart (Wispbyte) to take
# effect — load_dotenv only runs once at process start. This validates a new
# key against the free /sports endpoint (no credits spent) and pushes it to
# the same KV store the website reads from; update_board.refresh_live_api_key()
# picks it up on the very next /rebuild or the daily auto-refresh. No restart.
@tree.command(name="setoddskey", description="🔑 (Admin) Swap the live Odds API key — no bot restart needed")
@app_commands.describe(key="The new Odds API key")
async def cmd_setoddskey(interaction: discord.Interaction, key: str):
    if not await _is_admin(interaction):
        await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        loop = asyncio.get_event_loop()
        ok, message = await loop.run_in_executor(None, update_board.set_live_api_key, key)
    except Exception as exc:
        await interaction.followup.send(f"❌ Key swap failed: {exc}", ephemeral=True)
        return
    await interaction.followup.send(f"{'✅' if ok else '❌'} {message}", ephemeral=True)


# ── /credits ─────────────────────────────────────────────────────────────────
@tree.command(name="credits", description="📊 Check live Odds API credit balance (free, no credits spent)")
async def cmd_credits(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, update_board.test_odds_api_key, update_board.API_KEY)
    except Exception as exc:
        await interaction.followup.send(f"❌ Error checking credits: {exc}", ephemeral=True)
        return
    if not result.get("valid"):
        await interaction.followup.send(f"❌ Key invalid: {result.get('error', 'unknown')}", ephemeral=True)
        return
    remaining = result.get("requests_remaining", "?")
    used = result.get("requests_used", "?")
    embed = discord.Embed(title="Odds API Credits", color=0x00ff88)
    embed.add_field(name="Remaining", value=str(remaining), inline=True)
    embed.add_field(name="Used", value=str(used), inline=True)
    key_hint = f"…{update_board.API_KEY[-4:]}" if update_board.API_KEY else "none"
    embed.set_footer(text=f"Key: {key_hint}")
    await interaction.followup.send(embed=embed, ephemeral=True)


# ── /wnba ──────────────────────────────────────────────────────────────────────
@tree.command(name="wnba", description="🏀 WNBA props tonight")
async def cmd_wnba(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    rows = get_board(sport="WNBA", limit=15)
    if not rows:
        await interaction.followup.send(
            "No WNBA plays right now. (If a slate just posted, an admin can run **/rebuild**.)",
            ephemeral=True)
        return
    embeds = board_embed(rows, "🏀 WNBA Plays Tonight — VORTEX")
    await interaction.followup.send(embeds=embeds, view=BoardDetailView(rows), ephemeral=True)


# ── /mlb ───────────────────────────────────────────────────────────────────────
@tree.command(name="mlb", description="⚾ MLB props tonight")
async def cmd_mlb(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    rows = get_board(sport="MLB", limit=10)
    if not rows:
        await interaction.followup.send("No MLB plays right now.", ephemeral=True)
        return
    game_times = await _fetch_game_times()
    embeds = board_embed(rows, "⚾ MLB Plays Tonight — VORTEX", game_times=game_times)
    await interaction.followup.send(embeds=embeds, view=BoardDetailView(rows), ephemeral=True)


# ── /goblins ─────────────────────────────────────────────────────

@tree.command(name="goblins", description="🟢 PrizePicks goblin lines — lowest over per player")
async def cmd_goblins(interaction: discord.Interaction):
    """Show lowest over lines per player from today's board."""
    await interaction.response.defer(ephemeral=True)
    try:
        import sqlite3
        from pathlib import Path

        db_path = Path(__file__).parent.parent / "vortex.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row

        rows = conn.execute("""
            SELECT player_name, stat_type, line, side, ev_percentage, tier, sportsbook
            FROM props_board
            WHERE sport = 'MLB' AND side = 'over'
            ORDER BY line ASC
        """).fetchall()
        conn.close()

        if not rows:
            await interaction.followup.send("No over props on the board right now.", ephemeral=True)
            return

        best: dict[str, dict] = {}
        for r in rows:
            p = r["player_name"]
            try:
                ls = float(r["line"])
            except (ValueError, TypeError):
                continue
            if p not in best or ls < best[p]["_line_float"]:
                best[p] = {**dict(r), "_line_float": ls}

        goblins = sorted(best.values(), key=lambda r: r["_line_float"])

        if not goblins:
            await interaction.followup.send("No over props found.", ephemeral=True)
            return

        embed = discord.Embed(
            title="🟢 Green Goblins — Lowest Over Lines Per Player",
            description="Lowest **Over** lines on today's board, sorted safest-first",
            color=0x00D26A,
        )
        lines = []
        for i, g in enumerate(goblins, 1):
            tier_tag = ""
            t = g.get("tier", "")
            if t == "ELITE":
                tier_tag = " 💎"
            elif t == "STRONG":
                tier_tag = " 🔥"
            lines.append(
                f"`{i:02}.` **{g['player_name']}**{tier_tag}\n"
                f"       O{g['_line_float']:g} {g['stat_type']}"
            )

        for chunk in _field_chunks(lines):
            embed.add_field(name="— board overs", value=chunk, inline=False)

        embed.set_footer(text="Lowest over lines per player • Refreshed with /update")
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as exc:
        import traceback
        await interaction.followup.send(f"❌ Error: ```{traceback.format_exc()[-1800:]}```", ephemeral=True)


# ── /slate ─────────────────────────────────────────────────────

def _build_slate_embed(slate_lines: list[str], date_str: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"🗓️ Attack Board — {date_str}",
        description="All today's starting pitching matchups ranked by difficulty. "
                    "Higher score = more vulnerable starter + bullpen.",
        color=0x5865F2,
    )
    for chunk in _field_chunks(slate_lines):
        embed.add_field(name="— matchup difficulty", value=chunk, inline=False)
    embed.set_footer(text="Score = ERA*2 + HR/9*5 + bullpen (5=WEAK, 0=ELITE)")
    return embed


@tree.command(name="slate", description="🗓️ Attack Board: all starting pitching matchups ranked by difficulty")
async def cmd_slate(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    try:
        loop = asyncio.get_event_loop()
        import vortextime as _vt
        _board_day = _vt.vortex_board_day()   # advances past midnight so late-night runs see today's games
        schedule = await loop.run_in_executor(None, stats_mlb.get_todays_schedule, _board_day)
        if not schedule:
            await interaction.followup.send("No games on today's schedule.")
            return

        from datetime import date as _date, timezone as _tz, datetime as _datetime
        try:
            today_str = _datetime.strptime(_board_day, "%Y-%m-%d").strftime("%A, %b %-d")
        except Exception:
            today_str = _board_day

        entries = []
        for game_id, game in schedule.items():
            home_name = game.get("home_team_name", "?")
            away_name = game.get("away_team_name", "?")
            home_abbr = game.get("home_abbr", "")
            away_abbr = game.get("away_abbr", "")
            home_pitcher = game.get("home_pitcher", "")
            away_pitcher = game.get("away_pitcher", "")
            home_team_id = game.get("home_team_id")
            away_team_id = game.get("away_team_id")
            ct = game.get("commence_time", "")
            time_str = ""
            if ct:
                try:
                    dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
                    time_str = dt.astimezone(_tz(timedelta(hours=-7))).strftime("%I:%M %p").lstrip("0")
                except Exception:
                    time_str = ct

            for side, p_name, team_id in [
                ("home", home_pitcher, home_team_id),
                ("away", away_pitcher, away_team_id),
            ]:
                if not p_name or not team_id:
                    continue
                try:
                    pm = await loop.run_in_executor(None, stats_mlb.get_pitcher_metrics, p_name)
                    if "error" in pm:
                        continue
                    opp_team_id = away_team_id if side == "home" else home_team_id
                    bp = await loop.run_in_executor(None, stats_mlb.get_bullpen_stats, opp_team_id)
                except Exception:
                    continue

                try:
                    era = float(pm.get("era", 4.5))
                    hr9 = float(pm.get("hr_per_9", 0))
                    k9 = float(pm.get("k_per_9", 8))
                    whip = float(pm.get("whip", 1.3))
                except (ValueError, TypeError):
                    era, hr9, k9, whip = 4.5, 0, 8, 1.3

                bp_era = (bp or {}).get("era", 4.5)
                if bp_era <= 3.50:
                    bp_tier = "ELITE"
                elif bp_era <= 4.20:
                    bp_tier = "SOLID"
                elif bp_era <= 5.00:
                    bp_tier = "AVERAGE"
                else:
                    bp_tier = "WEAK"
                bp_score = {"ELITE": 0, "SOLID": 2, "AVERAGE": 3, "WEAK": 5}.get(bp_tier, 3)

                difficulty = round(era * 2 + hr9 * 5 + bp_score, 1)

                opp_name = home_name if side == "away" else away_name
                opp_abbr = home_abbr if side == "away" else away_abbr
                hand = pm.get("hand", "?")
                bp_label = {"ELITE": "🛡️", "SOLID": "✓", "AVERAGE": "~", "WEAK": "💥"}.get(bp_tier, "~")

                entries.append({
                    "score": difficulty,
                    "line": (
                        f"{'🔥' if difficulty >= 14 else '⚠️' if difficulty >= 10 else '✓'} "
                        f"**{p_name}** ({hand}) · {opp_abbr or opp_name} · {time_str}\n"
                        f"  Score **{difficulty}** · {era} ERA · {hr9} HR/9 · {k9} K/9 · "
                        f"Bullpen {bp_label} {bp_tier}"
                    ),
                })

        if not entries:
            await interaction.followup.send("No starting pitchers found for today.")
            return

        entries.sort(key=lambda e: e["score"], reverse=True)
        lines = [f"`{i:02}.` {e['line']}" for i, e in enumerate(entries, 1)]
        embed = _build_slate_embed(lines, today_str)
        await interaction.followup.send(embed=embed)

    except Exception as exc:
        import traceback
        await interaction.followup.send(f"❌ Error: ```{traceback.format_exc()[-1800:]}```", ephemeral=True)


# ── Auto NRFI loop ─────────────────────────────────────────────────────────────

NRFI_CHANNEL = 1517263414110453970

_NRFI_POSTED: set[int] = set()      # game PKs already posted today
_NRFI_POSTED_DATE: str | None = None  # tracks which date we've posted for


async def _check_and_post_nrfi():
    """Check for new NRFI/YRFI plays and post if any found (only within
    30 min of game start so picks land right before first pitch)."""
    from datetime import datetime as _dt
    global _NRFI_POSTED_DATE
    loop = asyncio.get_event_loop()
    today = vortextime.vortex_board_day()

    # Reset tracking when the board date rolls over
    if _NRFI_POSTED_DATE is not None and _NRFI_POSTED_DATE != today:
        _NRFI_POSTED.clear()
    _NRFI_POSTED_DATE = today

    # Clear stale cache so we get fresh lineup data
    for suffix in ("schedule_", "lineups_"):
        cf = stats_mlb.CACHE_DIR / f"{suffix}{today}.json"
        if cf.exists():
            cf.unlink()

    plays = await loop.run_in_executor(None, nrfi.get_nrfi_plays)
    now = _dt.now(_tz.utc)

    new_plays = []
    for p in plays:
        if p.get("game_pk") in _NRFI_POSTED:
            continue
        game_utc = p.get("game_utc", "")
        if not game_utc:
            continue
        try:
            game_time = _dt.fromisoformat(game_utc.replace("Z", "+00:00"))
        except Exception:
            continue
        # Only post if game starts within the next 30 minutes
        mins_until = (game_time - now).total_seconds() / 60
        if 0 <= mins_until <= 30:
            new_plays.append(p)

    if not new_plays:
        return

    from datetime import date as _date
    date_str = _date.today().strftime("%A, %b %-d")
    embed = nrfi.build_nrfi_embed(new_plays, date_str)

    channel = bot.get_channel(NRFI_CHANNEL)
    if channel:
        await channel.send(embed=embed)
        for p in new_plays:
            _NRFI_POSTED.add(p.get("game_pk"))


@tasks.loop(minutes=10)
async def auto_nrfi():
    """Every 10 min: check for newly confirmed lineups and post NRFI/YRFI picks."""
    try:
        await _check_and_post_nrfi()
    except Exception as e:
        print(f"[auto_nrfi] Error: {e}")


# ── Auto Moneyline loop ─────────────────────────────────────────────────────────

MONEYLINE_CHANNEL = 1517723193127862282

_ML_POSTED: set[int] = set()
_ML_POSTED_DATE: str | None = None


async def _check_and_post_moneyline():
    """Post moneyline model-vs-market leans as each game's lineups lock (~30 min out)."""
    from datetime import datetime as _dt, date as _date
    global _ML_POSTED_DATE
    loop = asyncio.get_event_loop()
    today = vortextime.vortex_board_day()

    if _ML_POSTED_DATE is not None and _ML_POSTED_DATE != today:
        _ML_POSTED.clear()
    _ML_POSTED_DATE = today

    plays = await loop.run_in_executor(None, moneyline.get_moneyline_plays, today)
    now = _dt.now(_tz.utc)

    new_plays = []
    for p in plays:
        if p.get("game_pk") in _ML_POSTED:
            continue
        ct = p.get("commence_time", "")
        if not ct:
            continue
        try:
            game_time = _dt.fromisoformat(ct.replace("Z", "+00:00"))
        except Exception:
            continue
        if 0 <= (game_time - now).total_seconds() / 60 <= 30:
            new_plays.append(p)

    if not new_plays:
        return

    date_str = _date.today().strftime("%A, %b %-d")
    embeds = moneyline.build_moneyline_embeds(new_plays, date_str)
    channel = bot.get_channel(MONEYLINE_CHANNEL)
    if channel:
        # One game per embed; Discord allows up to 10 embeds per message.
        for i in range(0, len(embeds), 10):
            await channel.send(embeds=embeds[i:i + 10])
        for p in new_plays:
            _ML_POSTED.add(p.get("game_pk"))


@tasks.loop(minutes=10)
async def auto_moneyline():
    """Every 10 min: post moneyline reads for games whose lineups just locked.
    Uses cached odds (30-min TTL) so the loop barely touches the Odds API."""
    try:
        await _check_and_post_moneyline()
    except Exception as e:
        print(f"[auto_moneyline] Error: {e}")


@tree.command(name="ml", description="💰 Refresh MLB moneylines — model vs market (confirmed lineups only)")
async def cmd_ml(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    loop = asyncio.get_event_loop()
    today = vortextime.vortex_board_day()
    # force_odds=True → this is the ONLY path that spends a fresh Odds API call
    plays = await loop.run_in_executor(
        None, lambda: moneyline.get_moneyline_plays(today, force_odds=True))
    from datetime import date as _date
    embeds = moneyline.build_moneyline_embeds(plays, _date.today().strftime("%A, %b %-d"))
    await interaction.followup.send(embeds=embeds[:10], ephemeral=True)


@tree.command(name="mlrecord", description="💰 Moneyline prediction accuracy")
async def cmd_mlrecord(interaction: discord.Interaction):
    if not await _is_admin(interaction):
        await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)

    from grade_results import get_moneyline_accuracy
    acc = get_moneyline_accuracy()

    if acc["total"] == 0:
        await interaction.followup.send(
            "No graded moneyline predictions yet. Picks are graded after games finish.",
            ephemeral=True)
        return

    lines = []
    lines.append(f"**Overall: {acc['hits']}/{acc['total']} ({acc['rate']}%)**")
    lines.append("")

    if acc["tiers"]:
        lines.append("**By Tier:**")
        for tier, data in acc["tiers"].items():
            lines.append(f"• {tier}: {data['hits']}/{data['total']} ({data['rate']}%)")
        lines.append("")

    if acc["recent_total"] > 0:
        lines.append(f"**Last 7 Days:** {acc['recent_hits']}/{acc['recent_total']} ({acc['recent_rate']}%)")

    embed = discord.Embed(
        title="💰 Moneyline Record",
        description="\n".join(lines),
        color=0x2ECC71 if acc["rate"] >= 55 else 0xE67E22 if acc["rate"] >= 50 else 0xE74C3C,
    )
    embed.set_footer(text="Moneyline picks are auto-graded after games finish")
    await interaction.followup.send(embed=embed, ephemeral=True)


@tree.command(name="results", description="📊 Today's moneyline + NRFI hit/miss results")
@app_commands.describe(date="Optional date like 7/17/2026. Blank = today.")
async def cmd_results(interaction: discord.Interaction, date: str | None = None):
    if not await _is_admin(interaction):
        await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)

    if date:
        target = _parse_user_date(date)
        if target is None:
            await interaction.followup.send(
                f"❌ Couldn't read `{date}`. Try month/day/year like `7/17/2026`.", ephemeral=True)
            return
    else:
        target = vortextime.vortex_day()

    from grade_results import get_all_results
    data = get_all_results(target)
    ml_rows = data["moneyline"]
    nrfi_rows = data["nrfi"]

    if not ml_rows and not nrfi_rows:
        await interaction.followup.send(
            f"No picks logged for **{_iso_to_us(target)}**. Picks are logged when the engine runs.",
            ephemeral=True)
        return

    embeds = []

    # ── Moneyline Results ───────────────────────────────────────────────
    if ml_rows:
        ml_hits = sum(1 for r in ml_rows if r["result"] == "hit")
        ml_total = sum(1 for r in ml_rows if r["result"])
        ml_pending = sum(1 for r in ml_rows if not r["result"])

        ml_lines = []
        if ml_total > 0:
            ml_lines.append(f"**Record: {ml_hits}/{ml_total} ({round(ml_hits/ml_total*100, 1)}%)**\n")

        for r in ml_rows:
            result = r["result"]
            if result == "hit":
                icon = "✅"
            elif result == "miss":
                icon = "❌"
            else:
                icon = "⏳"

            odds = int(r["odds"])
            odds_str = f"+{odds}" if odds > 0 else str(odds)
            tier_icon = {"NOTABLE": "⭐", "MODEST": "🟢", "SLIGHT": "🟡"}.get(r["tier"], "⚪")

            ml_lines.append(
                f"{icon} {tier_icon} **{r['rec_team']}** {odds_str} vs {r['opponent']} "
                f"— {r['model_pct']:.1f}% model · edge {r['edge_pct']:.1f}%"
            )

        if ml_pending > 0:
            ml_lines.append(f"\n⏳ {ml_pending} picks pending (games not finished)")

        ml_embed = discord.Embed(
            title=f"💰 Moneyline Results — {_iso_to_us(target)}",
            description="\n".join(ml_lines),
            color=0x2ECC71 if ml_hits > ml_total - ml_hits else 0xE74C3C,
        )
        ml_embed.set_footer(text=f"Overall: {ml_hits}W-{ml_total - ml_hits}L")
        embeds.append(ml_embed)

    # ── NRFI Results ────────────────────────────────────────────────────
    if nrfi_rows:
        nrfi_hits = sum(1 for r in nrfi_rows if r["result"] == "hit")
        nrfi_total = sum(1 for r in nrfi_rows if r["result"])
        nrfi_pending = sum(1 for r in nrfi_rows if not r["result"])

        nrfi_lines = []
        if nrfi_total > 0:
            nrfi_lines.append(f"**Record: {nrfi_hits}/{nrfi_total} ({round(nrfi_hits/nrfi_total*100, 1)}%)**\n")

        for r in nrfi_rows:
            result = r["result"]
            if result == "hit":
                icon = "✅"
            elif result == "miss":
                icon = "❌"
            else:
                icon = "⏳"

            rec = r["recommendation"]
            conf = r["confidence"]
            score = r["score"]
            actual = r["actual_result"] or "?"

            conf_icon = "🟢" if conf == "STRONG" else "🟡"

            nrfi_lines.append(
                f"{icon} {conf_icon} **{r['away_abbr']} @ {r['home_abbr']}** — "
                f"{rec} (score {score}) · actual: {actual}"
            )
            nrfi_lines.append(
                f"    🪣 {r['home_pitcher']} vs {r['away_pitcher']}"
            )

        if nrfi_pending > 0:
            nrfi_lines.append(f"\n⏳ {nrfi_pending} picks pending (games not finished)")

        nrfi_embed = discord.Embed(
            title=f"🌀 NRFI/YRFI Results — {_iso_to_us(target)}",
            description="\n".join(nrfi_lines),
            color=0x2ECC71 if nrfi_hits > nrfi_total - nrfi_hits else 0xE74C3C,
        )
        nrfi_embed.set_footer(text=f"Overall: {nrfi_hits}W-{nrfi_total - nrfi_hits}L")
        embeds.append(nrfi_embed)

    await interaction.followup.send(embeds=embeds, ephemeral=True)


# ── /nrfi ──────────────────────────────────────────────────────────────────────


async def _can_post_nrfi(interaction: discord.Interaction) -> bool:
    """Check if the user is admin or has manage_channels permission."""
    if await _is_admin(interaction):
        return True
    if interaction.guild and interaction.permissions.manage_channels:
        return True
    return False


@tree.command(name="nrfi", description="🌀 NRFI / YRFI analysis for today's games")
async def cmd_nrfi(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        loop = asyncio.get_event_loop()

        # Run analysis in executor
        plays = await loop.run_in_executor(None, nrfi.get_nrfi_plays)

        from datetime import date as _date
        date_str = _date.today().strftime("%A, %b %-d")

        embed = nrfi.build_nrfi_embed(plays, date_str)

        # Post to the NRFI channel (public)
        channel = interaction.client.get_channel(NRFI_CHANNEL)
        if channel:
            await channel.send(embed=embed)
            await interaction.followup.send(
                f"✅ NRFI/YRFI report posted to {channel.mention}",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "❌ Could not find NRFI channel. Report generated but not posted.",
                embed=embed,
                ephemeral=True,
            )

    except Exception as exc:
        import traceback
        await interaction.followup.send(f"❌ NRFI analysis failed: ```{traceback.format_exc()[-1800:]}```", ephemeral=True)


# ── /parlay ────────────────────────────────────────────────────────────────────

def _american_to_decimal(o: int) -> float:
    return (o / 100) + 1 if o > 0 else (100 / abs(o)) + 1

def _decimal_to_american(d: float) -> int:
    if d >= 2.0:
        return round((d - 1) * 100)
    return round(-100 / (d - 1))

def _parlay_legs(sport: str, n: int) -> list[dict]:
    """
    Pull board rows for `sport`, parse effective L10 hit rate for each,
    deduplicate by player, sort hit-rate descending, return top `n`.

    Selection is pure hit-rate — L10 effective rate is the primary sort key.
    EV / odds are recorded but do NOT influence selection order.
    """
    conn = _db()
    rows = conn.execute(
        f"SELECT * FROM props_board WHERE sport=? AND {_LIVE_FILTER} AND tier IN ('ELITE','STRONG') ORDER BY vortex_score DESC",
        (sport,),
    ).fetchall()
    conn.close()

    seen_players: set[str] = set()
    candidates: list[dict] = []

    for _r in rows:
        r      = dict(_r)   # sqlite3.Row → plain dict so .get() works everywhere
        player = r["player_name"]
        if player in seen_players:
            continue                          # one prop per player maximum

        sj   = json.loads(r["stats_json"]) if r["stats_json"] else {}
        side = sj.get("side", "over")
        splits = sj.get("splits") or {}
        l10  = (splits.get("l10") or {})
        l10_rate_raw = l10.get("rate") or 0

        # For under props the "hit" is when the stat goes UNDER the line,
        # so the effective hit rate is the complement of the over rate.
        effective_l10 = (100 - l10_rate_raw) if side == "under" else l10_rate_raw

        # Minimum threshold — only high hit-rate props in the parlay
        if effective_l10 < 55:
            continue

        # ── Matchup conflict check ────────────────────────────────────────────
        conflict_note = None
        case_text  = (r.get("case_summary") or "").lower()
        pitcher    = sj.get("pitcher") or {}
        hr9        = pitcher.get("hr_per_9")
        era        = pitcher.get("era")
        try:
            hr9_f = float(hr9) if hr9 and hr9 != "?" else 0
            era_f = float(era) if era and era != "?" else 0
        except (ValueError, TypeError):
            hr9_f = era_f = 0

        if side == "under":
            # Hittable pitcher contradicts Under — flag it
            if era_f >= 4.5 or hr9_f >= 1.2:
                conflict_note = (
                    f"⚠️ Matchup conflict — pitcher ERA {era or '?'} / HR/9 {hr9 or '?'} "
                    f"favors production (Over lean). Form says Under — monitor pregame."
                )
            elif "favorable matchup" in case_text or "lean over" in case_text:
                conflict_note = "⚠️ Matchup leans Over but recent form strongly supports Under — form takes priority."
        else:
            # Poor pitcher ERA contradicts Over if batter is cold
            l5_rate = (splits.get("l5") or {}).get("rate") or 0
            if l5_rate < 50 and era_f < 3.5:
                conflict_note = (
                    f"⚠️ Matchup conflict — pitcher ERA {era or '?'} is elite "
                    f"and L5 form is only {l5_rate:.0f}%. Over has headwinds."
                )

        # Combined rank: 70% weight on hit rate, 30% on board score (0–100)
        board_score  = r.get("vortex_score") or 0
        combined_rank = (effective_l10 * 0.70) + (board_score * 0.30)

        # Leg quality grade
        tier = r.get("tier", "")
        if tier == "ELITE" and effective_l10 >= 70 and not conflict_note:
            leg_quality = "💎 Anchor"
        elif tier in ("ELITE", "STRONG") and effective_l10 >= 60:
            leg_quality = "🔥 Core"
        elif conflict_note:
            leg_quality = "⚠️ Conflict"
        elif effective_l10 >= 55:
            leg_quality = "✅ Fill"
        else:
            leg_quality = "⚪ Filler"

        seen_players.add(player)
        candidates.append({
            "row":           r,
            "sj":            sj,
            "side":          side,
            "splits":        splits,
            "effective_l10": effective_l10,
            "combined_rank": combined_rank,
            "conflict_note": conflict_note,
            "leg_quality":   leg_quality,
        })

    # Sort by combined rank (hit rate + board score) — not hit rate alone
    candidates.sort(key=lambda x: x["combined_rank"], reverse=True)
    return candidates[:n]


def _build_parlay_embed(sport: str, legs_data: list[dict]) -> discord.Embed:
    n        = len(legs_data)
    sport_e  = SPORT_EMOJI.get(sport, "🎯")
    avg_l10  = round(sum(x["effective_l10"] for x in legs_data) / n, 1) if legs_data else 0

    # Combined odds
    all_odds      = [x["sj"].get("best_odds") for x in legs_data]
    has_odds      = all(o is not None for o in all_odds)
    combined_line = ""
    if has_odds:
        dec_product = 1.0
        for o in all_odds:
            dec_product *= _american_to_decimal(o)
        combined_american = _decimal_to_american(dec_product)
        payout            = round(dec_product - 1, 2)
        odds_fmt          = f"+{combined_american}" if combined_american > 0 else str(combined_american)
        combined_line     = (
            f"**{odds_fmt}** · risk $1 → win **${payout}** · "
            f"Suggested: **0.25u** · Avg L10 **{avg_l10}%**"
        )
    else:
        combined_line = f"Avg L10 hit rate: **{avg_l10}%** · Suggested: **0.25u**"

    # Pick embed color from the weakest leg's tier (conservative)
    tier_order = ["ELITE", "STRONG", "LEAN", "PASS"]
    tiers      = [x["row"]["tier"] or "PASS" for x in legs_data]
    worst_tier = max(tiers, key=lambda t: tier_order.index(t) if t in tier_order else 3)
    color      = TIER_COLOR.get(worst_tier, 0x00D4FF)

    embed = discord.Embed(
        title=f"{sport_e} {n}-Leg {sport} Parlay — Hit Rate Build",
        description=f"Best {n} props sorted by L10 hit rate — data-backed consistency only.",
        color=color,
    )

    for i, leg in enumerate(legs_data, 1):
        r      = leg["row"]
        sj     = leg["sj"]
        side   = leg["side"]
        splits = leg["splits"]

        player   = r["player_name"]
        stat     = r["stat_type"]
        line     = r["line"]
        tier     = r["tier"] or "—"
        score    = r["vortex_score"] or 0
        te       = TIER_EMOJI.get(tier, "⚪")
        sw       = "Over" if side == "over" else "Under"
        eff_l10  = leg["effective_l10"]

        # Odds string
        best_odds = sj.get("best_odds")
        odds_str  = ""
        if best_odds is not None:
            odds_str = f" @ **+{best_odds}**" if best_odds > 0 else f" @ **{best_odds}**"

        # Pitcher matchup
        pitcher   = sj.get("pitcher") or {}
        p_name    = pitcher.get("name", "")
        p_hand    = pitcher.get("hand", "?")
        p_era     = pitcher.get("era", "?")
        p_hr9     = pitcher.get("hr_per_9", "?")
        p_fip     = pitcher.get("fip", "")

        matchup_str = ""
        if p_name:
            matchup_str = f"{p_name} ({p_hand}HP) — {p_era} ERA"
            if p_hr9:
                matchup_str += f", {p_hr9} HR/9"
            if p_fip:
                matchup_str += f", {p_fip} FIP"

        # L5 / L10 / L20 hit counts
        l5  = splits.get("l5")  or {}
        l10 = splits.get("l10") or {}
        l20 = splits.get("l20") or {}

        def _hfmt(d, is_under):
            g = d.get("games", 0)
            h = d.get("hits",  0)
            hits_display = (g - h) if is_under and g else h
            return f"{hits_display}/{g}" if g else "—"

        is_under = (side == "under")
        l5_str   = _hfmt(l5,  is_under)
        l10_str  = _hfmt(l10, is_under)
        l20_str  = _hfmt(l20, is_under)
        avg_val  = l10.get("avg", splits.get("season_avg", "?"))

        # BvP
        bvp     = sj.get("bvp") or {}
        bvp_ab  = bvp.get("ab",   0)
        bvp_hits= bvp.get("hits", 0)
        bvp_avg = bvp.get("avg",  "")
        opp     = sj.get("pitcher", {}).get("name", "OPP") if p_name else "OPP"
        # derive short team tag from pitcher opponent context
        bvp_str = f"{bvp_hits}/{bvp_ab}" if bvp_ab >= 3 else "N/A"

        # Home/Away
        is_home = sj.get("is_home")
        spot    = "🏠" if is_home is True else ("✈️" if is_home is False else "")

        # Case + risk
        case = (r["case_summary"] or "").strip()
        risk = (r["risk_summary"] or "").strip()

        # Build field value
        quality = leg.get("leg_quality", "")
        lines = [
            f"{te} **{player}** {spot} · {sw} {line} {stat}{odds_str}",
            f"🟢 **{tier}** (Score {score}) · L10 **{eff_l10:.0f}%** · {quality}",
        ]
        if case:
            # Trim long case to keep under field limit
            case_short = case[:280] + ("…" if len(case) > 280 else "")
            lines.append(f"WHY: {case_short}")
        if leg.get("conflict_note"):
            lines.append(leg["conflict_note"])
        elif risk:
            risk_short = risk[:180] + ("…" if len(risk) > 180 else "")
            lines.append(f"RISK: {risk_short}")
        lines.append(
            f"📊 RECENT: L5 {l5_str} · L10 {l10_str} · L20 {l20_str} · "
            f"avg {avg_val} · vs OPP: {bvp_str}"
        )
        if matchup_str:
            lines.append(f"MATCHUP: {matchup_str}")
        lines.append("SIZING: 1u")

        field_val = "\n".join(lines)[:1020]
        embed.add_field(name=f"Leg {i}", value=field_val, inline=False)

    embed.add_field(name="💰 Combined", value=combined_line, inline=False)

    # Parlay grade from combined probability
    combined_prob = 1.0
    leg_probs = []
    for leg in legs_data:
        lp = max(0.05, min(0.95, leg["effective_l10"] / 100))
        leg_probs.append(lp)
        combined_prob *= lp
    if n > 2:
        combined_prob *= 0.95 ** (n - 2)

    if   combined_prob >= 0.35: parlay_tier = "ELITE";  parlay_emoji = "💎"
    elif combined_prob >= 0.22: parlay_tier = "STRONG"; parlay_emoji = "🔥"
    elif combined_prob >= 0.12: parlay_tier = "GOOD";   parlay_emoji = "✅"
    elif combined_prob >= 0.06: parlay_tier = "LEAN";   parlay_emoji = "➡️"
    else:                       parlay_tier = "RISKY";  parlay_emoji = "⚠️"

    prob_parts = [f"{p*100:.0f}%" for p in leg_probs]
    prob_formula = " × ".join(prob_parts)
    if n > 2:
        prob_formula += f" × 0.95^{n-2}"

    embed.add_field(
        name=f"{parlay_emoji} Parlay Grade: {parlay_tier}",
        value=(
            f"**Combined probability: {combined_prob*100:.1f}%**\n"
            f"`{prob_formula}`\n"
            f"⚠️ Every additional leg reduces your chance to hit."
        ),
        inline=False,
    )

    embed.set_footer(text=f"Green Machine · Data-driven picks · EV+ · VORTEX")
    return embed


class ParlayLegSelect(discord.ui.Select):
    """Drill into any parlay leg for the full live analysis card."""
    def __init__(self, legs_data: list[dict]):
        self._legs: dict[str, dict] = {}
        options = []
        for i, leg in enumerate(legs_data, 1):
            r    = leg["row"]
            side = leg["side"]
            sw   = "Over" if side == "over" else "Under"
            label = f"Leg {i}: {r['player_name']} {sw} {r['line']} {r['stat_type']}"[:100]
            key  = f"{i}|{r['player_name']}|{r['line']}|{side}"
            self._legs[key] = leg
            options.append(discord.SelectOption(label=label, value=key))
        super().__init__(
            placeholder="🔎 Analyze a leg in full detail...",
            min_values=1, max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        leg = self._legs[self.values[0]]
        r   = leg["row"]
        prop = {
            "player_name": r["player_name"],
            "line":        r["line"],
            "side":        leg["side"],
            "market_raw":  r["stat_type"],
            "prop_type":   vortex_analyze.normalize_market(r["stat_type"] or ""),
            "ev_pct":      r["ev_percentage"] if "ev_percentage" in r.keys() else None,
            "book_name":   r["sportsbook"] if "sportsbook" in r.keys() else None,
            "book_odds":   json.loads(r["stats_json"]).get("best_odds") if "stats_json" in r.keys() and r["stats_json"] else None,
        }
        try:
            await _run_analyze(interaction, prop)
        except Exception:
            import traceback
            await interaction.followup.send(
                f"❌ Error: ```{traceback.format_exc()[-1800:]}```", ephemeral=True
            )


class ParlayView(discord.ui.View):
    def __init__(self, legs_data: list[dict]):
        super().__init__(timeout=300)
        self.add_item(ParlayLegSelect(legs_data))


@tree.command(name="parlay", description="🎯 Build a hit-rate-backed parlay from tonight's top props")
@app_commands.describe(
    sport="Sport (MLB or NBA)",
    legs="Number of legs (2–6)",
)
@app_commands.choices(sport=[
    app_commands.Choice(name="MLB ⚾", value="MLB"),
    app_commands.Choice(name="NBA 🏀", value="NBA"),
])
async def cmd_parlay(interaction: discord.Interaction, sport: str, legs: int):
    await interaction.response.defer(ephemeral=True)
    try:
        if not (2 <= legs <= 6):
            await interaction.followup.send("❌ Legs must be between 2 and 6.", ephemeral=True)
            return

        loop       = asyncio.get_event_loop()
        legs_data  = await loop.run_in_executor(None, lambda: _parlay_legs(sport, legs))

        if not legs_data:
            await interaction.followup.send(
                f"No {sport} props with 55%+ L10 hit rate on the board right now. "
                "Check back after the engine runs.",
                ephemeral=True,
            )
            return

        if len(legs_data) < legs:
            await interaction.followup.send(
                f"⚠️ Only **{len(legs_data)}** {sport} props meet the hit-rate threshold "
                f"(≥55% L10). Building a {len(legs_data)}-leg parlay instead.",
                ephemeral=True,
            )

        embed = _build_parlay_embed(sport, legs_data)
        view  = ParlayView(legs_data)
        await interaction.followup.send(
            content=f"Let's get it **{interaction.user.display_name}** 💰 — "
                    f"your custom {len(legs_data)}-leg parlay {SPORT_EMOJI.get(sport, '')} props.",
            embed=embed,
            view=view,
            ephemeral=True,
        )
    except Exception as exc:
        import traceback
        await interaction.followup.send(f"❌ Error: ```{traceback.format_exc()[-1800:]}```", ephemeral=True)


# ── /analyze helpers ──────────────────────────────────────────────────────────

async def _run_wnba_analyze(interaction, player: str, line: float, side: str, prop_type: str):
    """Grade a single WNBA prop on demand and send the detail card.
    Uses the same grade_wnba engine + signals as the board."""
    loop = asyncio.get_event_loop()
    print(f"[wnba-analysis] player={player} side={side.upper()} line={line} prop={prop_type}")
    try:
        row = await loop.run_in_executor(
            None, lambda: update_board.analyze_wnba_prop(player, line, side, prop_type)
        )
    except Exception:
        import traceback
        await interaction.followup.send(
            f"❌ WNBA analysis failed:\n```{traceback.format_exc()[-1200:]}```", ephemeral=True
        )
        return

    if not row or "error" in row:
        await interaction.followup.send(
            f"🔒 {row.get('error', 'No WNBA data available.') if row else 'No WNBA data available.'}",
            ephemeral=True,
        )
        return

    # build_wnba_detail_embed expects a mapping with these keys — a plain dict works.
    embed = build_wnba_detail_embed(row)
    await interaction.followup.send(embed=embed)


async def _run_analyze(interaction: discord.Interaction, prop: dict):
    """Run the full analyze pipeline for a single prop dict and send the result."""
    loop = asyncio.get_event_loop()

    player_name_raw = prop["player_name"]
    line            = prop["line"]
    side            = prop.get("side", "over")
    prop_type       = prop.get("prop_type") or vortex_analyze.normalize_market(prop.get("market_raw") or "")

    print(f"[analysis] player={player_name_raw} side={side.upper()} line={line} market={prop_type}")

    # Hard gate: direction must be confirmed
    if not side or side not in ("over", "under"):
        await interaction.followup.send(
            f"❌ Could not determine Over/Under direction for **{player_name_raw}**.\n"
            "Try using the `/analyze` `side` override dropdown.",
            ephemeral=True,
        )
        return

    # ── WNBA routing: basketball stats use the WNBA pipeline, not MLB ─────────
    if prop_type in _WNBA_PROP_TYPES:
        await _run_wnba_analyze(interaction, player_name_raw, line, side, prop_type)
        return

    # ── Step 2: Resolve canonical player ID via MLB fuzzy search ─────────────
    try:
        matches = await loop.run_in_executor(None, vortex_research.fuzzy_search, player_name_raw)
    except Exception as exc:
        await interaction.followup.send(f"❌ Player lookup failed: `{exc}`", ephemeral=True)
        return

    if not matches:
        await interaction.followup.send(
            f"❌ Couldn't find an MLB player matching **\"{player_name_raw}\"**.\n"
            "The OCR may have misread the name — try a cleaner crop of the slip.",
            ephemeral=True,
        )
        return

    found       = matches[0]
    player_id   = found["id"]
    player_name = found["name"]

    # Resolve team from the MLB Stats API — never trust the OCR token because
    # the slip often shows the opponent's abbreviation instead of the player's team.
    try:
        _team_id = await loop.run_in_executor(None, lambda: stats_mlb.get_player_current_team(player_id))
        _team_map = {
            133:"OAK",134:"PIT",135:"SD",136:"SEA",137:"SF",138:"STL",
            139:"TB",140:"TEX",141:"TOR",142:"MIN",143:"PHI",144:"ATL",
            145:"CWS",146:"MIA",147:"NYM",158:"MIL",108:"LAA",109:"ARI",
            110:"BAL",111:"BOS",112:"CHC",113:"CIN",114:"CLE",115:"COL",
            116:"DET",117:"HOU",118:"KC",119:"LAD",120:"WSH",121:"NYY",
        }
        team = _team_map.get(_team_id, found.get("team", ""))
    except Exception:
        team = found.get("team", "")

    # ── Step 3: Native hit-rate computation (MLB Stats API — free) ───────────
    # For strikeouts, skip the hitting game log — the K-card override (below)
    # uses the pitching log and replaces splits entirely.
    if prop_type == "strikeouts":
        splits = {}
    else:
        try:
            splits = await loop.run_in_executor(
                None, lambda: vortex_analyze.compute_hit_rates(player_id, line, prop_type)
            )
        except Exception as exc:
            await interaction.followup.send(f"❌ Stats fetch failed: `{exc}`", ephemeral=True)
            return

        if "error" in splits:
            await interaction.followup.send(f"❌ {splits['error']}", ephemeral=True)
            return

    # ── Step 4: Tonight's matchup ─────────────────────────────────────────────
    try:
        matchup = await loop.run_in_executor(None, lambda: vortex_analyze.get_matchup_info(player_id))
    except Exception:
        matchup = {}

    # No upcoming game found → don't serve a card for a finished/non-existent game.
    # get_matchup_info scans today→day-after, so empty means nothing un-started in
    # that window. Use get_no_game_reason() to say WHY instead of guessing.
    if not matchup and prop_type != "strikeouts":
        reason = await loop.run_in_executor(
            None, lambda: vortex_analyze.get_no_game_reason(player_id)
        )
        if reason == "in_progress":
            msg = (
                f"🔒 **{player_name}**'s game today is already underway or final — "
                f"I won't grade a prop for a game in progress, and no upcoming game is "
                f"posted yet for the next slate.\nCheck back once tomorrow's schedule loads."
            )
        elif reason == "off_day":
            msg = (
                f"🔒 **{player_name}** has no game scheduled right now — it's an off day "
                f"or the next slate hasn't been posted yet.\nCheck back when the upcoming "
                f"schedule loads and grab the line early."
            )
        else:
            msg = (
                f"🔒 Couldn't find an upcoming game for **{player_name}** — the team may be "
                f"between series or the schedule isn't posted yet. Check back shortly."
            )
        await interaction.followup.send(msg, ephemeral=True)
        return

    pitcher_id  = matchup.get("pitcher_id")
    pitcher_nm  = matchup.get("pitcher")
    opp_team_id = matchup.get("opp_team_id")

    # ── Step 5: Parallel data fetch (BvP, pitcher metrics, weather, team BvP, OAA, arsenal)
    import asyncio as _asyncio

    async def _safe(fn, default=None):
        try:
            return await loop.run_in_executor(None, fn)
        except Exception:
            return default if default is not None else {}

    (bvp, pitcher, weather, team_bvp_data, oaa_data, arsenal, bat_vs_pitch,
     statcast_data, bullpen_data, umpire_data, batter_hand, lineup_spot,
     team_h2h_data, vs_hand_splits_data) = await _asyncio.gather(
        _safe(lambda: stats_mlb.get_bvp_history(player_id, pitcher_id) if pitcher_id else {}),
        _safe(lambda: stats_mlb.get_pitcher_metrics(pitcher_nm) if pitcher_nm else {}),
        _safe(lambda: stats_mlb.get_game_weather(_team_map.get(matchup.get("home_team_id"), ""), matchup.get("game_utc", "")) if matchup.get("home_team_id") else {}),
        _safe(lambda: stats_mlb.get_team_bvp(player_id, opp_team_id) if opp_team_id else {}),
        _safe(lambda: stats_mlb.get_team_defense_oaa(opp_team_id) if opp_team_id else {}),
        _safe(lambda: stats_mlb.get_pitcher_arsenal(pitcher_id) if pitcher_id else [], default=[]),
        _safe(lambda: stats_mlb.get_batter_vs_pitch_type(player_id, pitcher_id) if player_id and pitcher_id else [], default=[]),
        _safe(lambda: stats_mlb.get_statcast_by_id(player_id) if player_id else {}),
        _safe(lambda: stats_mlb.get_bullpen_stats(opp_team_id) if opp_team_id else {}),
        _safe(lambda: stats_mlb.get_game_umpire(matchup.get("home_team_id")) if matchup.get("home_team_id") else {}),
        _safe(lambda: stats_mlb.get_player_bat_side(player_id) if player_id else "", default=""),
        _safe(lambda: stats_mlb.get_lineup_position(player_id) if player_id else None, default=None),
        _safe(lambda: stats_mlb.get_vs_team_splits(player_id, opp_team_id, line, prop_type)
              if player_id and opp_team_id else {}),
        _safe(lambda: stats_mlb.get_batter_hand_splits(player_id) if player_id else {}, default={}),
    )

    # ── Step 6: Opponent K-rate + park factor ─────────────────────────────────
    opp_k_rank = None
    opp_k_pct  = None
    try:
        opp_team_name = matchup.get("opponent", "")
        if opp_team_name:
            all_k_rates = stats_mlb.get_all_teams_k_rate()
            for _tid, kd in all_k_rates.items():
                if kd.get("name", "").lower() in opp_team_name.lower() or \
                   opp_team_name.lower() in kd.get("name", "").lower():
                    opp_k_rank = kd.get("rank")
                    _raw        = kd.get("k_pct")
                    opp_k_pct  = (_raw / 100) if _raw is not None else None
                    break
    except Exception:
        pass

    # Park factor keyed by HOME team full name (from stats_mlb.PARK_FACTOR)
    park_factor = 1.0
    try:
        opp_name = matchup.get("opponent", "")
        is_home  = matchup.get("is_home")
        if is_home is False and opp_name:
            # Batter is away → park = opponent's home stadium
            park_factor = stats_mlb.PARK_FACTOR.get(opp_name, 1.0)
        elif is_home is True:
            # Batter is home → find their team's full name via reverse map
            _rev = {v: k for k, v in _team_map.items()}
            # Look it up by matching abbreviation against PARK_FACTOR keys
            for full_name, pf in stats_mlb.PARK_FACTOR.items():
                if team and team.upper() in full_name.upper():
                    park_factor = pf
                    break
    except Exception:
        pass

    # ── K-prop override: use the pitcher's own K-card for splits + pitcher_card ─
    # get_historical_splits() uses the hitting game log (wrong for pitchers).
    # get_pitcher_k_card() uses the pitching log and returns opp_k data.
    if prop_type == "strikeouts":
        try:
            _k_card = await loop.run_in_executor(
                None, lambda: stats_mlb.get_pitcher_k_card(
                    player_name, line, opp_team_id, pitcher_id=player_id
                )
            )
            if _k_card.get("error"):
                await interaction.followup.send(
                    f"❌ No K-prop data for **{player_name}**: {_k_card['error']}\n"
                    "They may not have pitched yet this season or are on the IL.",
                    ephemeral=True,
                )
                return
            _ks = dict(_k_card.get("splits", {}))
            _ks["recent_games"] = [
                {
                    "date":     s.get("date", ""),
                    "opponent": s.get("opponent", ""),
                    "value":    s.get("k", 0),
                    "over":     s.get("k", 0) > line,
                }
                for s in _k_card.get("last_5_starts", [])
            ]
            splits  = _ks
            pitcher = _k_card   # K-card IS the pitcher_card for K props
            opp_k_d = _k_card.get("opp_k") or {}
            if opp_k_d:
                opp_k_rank = opp_k_d.get("rank")
                _raw_kpct  = opp_k_d.get("k_pct")
                opp_k_pct  = (_raw_kpct / 100) if _raw_kpct is not None else None
        except Exception:
            import traceback
            await interaction.followup.send(
                f"❌ K-prop lookup failed for **{player_name}**:\n```{traceback.format_exc()[-1200:]}```",
                ephemeral=True,
            )
            return

    try:
        both = vortex_analyze.grade_pick_both(
            splits,
            line,
            opp_k_rank=opp_k_rank,
            opp_k_pct=opp_k_pct,
            pitcher=pitcher,
            bvp=bvp,
            park_factor=park_factor,
            weather=weather,
            team_bvp=team_bvp_data,
            oaa=oaa_data,
            prop_type=prop_type,
            lineup_spot=lineup_spot if isinstance(lineup_spot, int) else None,
            statcast=statcast_data or None,
            team_h2h=team_h2h_data or None,
            arsenal=arsenal or None,
            bat_vs_pitch=bat_vs_pitch or None,
            vs_hand_splits=vs_hand_splits_data or None,
            umpire=umpire_data or None,
        )

        # Use the USER's selected side grade for header — model verdict shown via side_comparison
        grade = both["under_grade"] if side == "under" else both["over_grade"]

        # Debug logging
        print(f"[analysis] selected_side={side.upper()} over_score={both['over_score']} under_score={both['under_score']} model_verdict={both['model_verdict'].upper()} confidence={both['confidence']}")

        _proj_edge = grade.get("proj_edge", 0)
        _score     = grade["score"]
        # Conflict only fires when BOTH the projection edge AND the final score
        # point away from the requested side. If the model graded the side positive
        # (score > 0), the matchup signals already overrode the edge discrepancy —
        # that's the model working, not a conflict.
        _conflict  = bool(_proj_edge < 0 and _score <= 0 and prop_type != "strikeouts")

        # ── Step 7: Build and send the card ──────────────────────────────────────
        # Look up game time via pitcher name (same key used by board_embed)
        _game_times = await _fetch_game_times()
        _game_time  = _game_times.get((pitcher_nm or "").lower().strip())

        # Try Claude-powered card first; fall back to Python embed builder
        embed = await _claude_analyze_card(
            player_name=player_name,
            team=team,
            prop_type=prop_type,
            line=line,
            side=side,
            splits=splits,
            grade=grade,
            matchup=matchup,
            pitcher=pitcher if pitcher and not pitcher.get("error") else {},
            opp_k_rank=opp_k_rank,
            opp_k_pct=opp_k_pct,
            park_factor=park_factor,
            weather=weather,
            bvp=bvp,
            statcast=statcast_data,
            lineup_spot=lineup_spot if isinstance(lineup_spot, int) else None,
            game_time=_game_time,
        )

        if embed is None:
            # No API key or Claude failed — use the existing Python embed builder
            vs_hand_splits = vs_hand_splits_data or {}
            ev_val = prop.get("ev_pct")
            bk_name = prop.get("book_name")
            embed = vortex_analyze.build_analyze_embed(
                player_name=player_name,
                team=team,
                prop_type=prop_type,
                line=line,
                splits=splits,
                grade=grade,
                matchup=matchup,
                bvp=bvp,
                side=side,
                multi_prop_note=None,
                pitcher_card=pitcher if pitcher and not pitcher.get("error") else None,
                ev_pct=ev_val,
                book_name=bk_name,
                true_prob=None,
                weather=weather,
                team_bvp=team_bvp_data,
                oaa=oaa_data,
                arsenal=arsenal,
                bat_vs_pitch=bat_vs_pitch,
                statcast=statcast_data,
                bullpen=bullpen_data,
                umpire=umpire_data,
                batter_hand=batter_hand or "",
                park_factor=park_factor,
                lineup_spot=lineup_spot if isinstance(lineup_spot, int) else None,
                game_time=_game_time,
                vs_hand_splits=vs_hand_splits,
                team_h2h=team_h2h_data or None,
                side_comparison=both,
            )

        # Model disagreement: model verdict differs from user selection
        if both["model_verdict"] != side:
            mv_label = "Over" if both["model_verdict"] == "over" else "Under"
            sel_label = "Over" if side == "over" else "Under"
            embed.insert_field_at(
                0,
                name="⚠️ Model Disagreement",
                value=(
                    f"You selected **{sel_label}** but the model favors **{mv_label}**.\n"
                    f"Over score: **{both['over_score']}** · Under score: **{both['under_score']}** · "
                    f"Confidence: **{both['confidence']:.0%}**"
                ),
                inline=False,
            )

        await interaction.followup.send(embed=embed)

    except Exception:
        import traceback
        await interaction.followup.send(
            f"❌ Error building analysis: ```{traceback.format_exc()[-1800:]}```",
            ephemeral=True,
        )


# ── Multi-prop select view ─────────────────────────────────────────────────────

class MultiPropSelect(discord.ui.Select):
    def __init__(self, all_props: list[dict]):
        self._props = {f"{p['player_name']}|{p['line']}|{p['side']}": p for p in all_props}
        options = []
        for p in all_props[:25]:
            sw    = "More" if p["side"] == "over" else "Less"
            label = f"{p['player_name']} — {sw} {p['line']} {p.get('market_raw','')}"[:100]
            key   = f"{p['player_name']}|{p['line']}|{p['side']}"
            options.append(discord.SelectOption(label=label, value=key))
        super().__init__(
            placeholder="📋 Choose a prop to analyze...",
            min_values=1, max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        prop = self._props[self.values[0]]
        await _run_analyze(interaction, prop)


class MultiPropView(discord.ui.View):
    def __init__(self, all_props: list[dict]):
        super().__init__(timeout=120)
        self.add_item(MultiPropSelect(all_props))


# ── /prediction ────────────────────────────────────────────────────────────────

_STAT_ALIASES = {
    "k":              "strikeouts",
    "ks":             "strikeouts",
    "k's":            "strikeouts",
    "strikeout":      "strikeouts",
    "strikeouts":     "strikeouts",
    "h":              "hits",
    "hit":            "hits",
    "hits":           "hits",
    "hrr":              "hits_runs_rbis",
    "hrrbi":            "hits_runs_rbis",
    "h+r+rbi":          "hits_runs_rbis",
    "h+r+rbis":         "hits_runs_rbis",
    "hits+runs+rbis":   "hits_runs_rbis",
    "hits+runs+rbi":    "hits_runs_rbis",
    "hits runs rbis":   "hits_runs_rbis",
    "hits_runs_rbis":   "hits_runs_rbis",
    "hits_runs_rbi":    "hits_runs_rbis",
    "hr":             "home_runs",
    "home_run":       "home_runs",
    "home_runs":      "home_runs",
    "homerun":        "home_runs",
    "homeruns":       "home_runs",
    "tb":             "total_bases",
    "total_bases":    "total_bases",
    "totalbases":     "total_bases",
    "rbi":            "rbis",
    "rbis":           "rbis",
    "r":              "runs_scored",
    "run":            "runs_scored",
    "runs":           "runs_scored",
    "runs_scored":    "runs_scored",
    "bb":             "walks",
    "walk":           "walks",
    "walks":          "walks",
    "outs":           "pitcher_outs",
    "pitcher_outs":   "pitcher_outs",
    "po":             "pitcher_outs",
    "ha":             "pitcher_hits_allowed",
    "pitcher_hits_allowed": "pitcher_hits_allowed",
    "hits_allowed":   "pitcher_hits_allowed",
    "er":             "pitcher_earned_runs",
    "era":            "pitcher_earned_runs",
    "pitcher_earned_runs": "pitcher_earned_runs",
    "earned_runs":    "pitcher_earned_runs",
    "fp":             "fantasy_score",
    "fs":             "fantasy_score",
    "hitter fs":      "fantasy_score",
    "fantasy_score":  "fantasy_score",
    "fantasy":        "fantasy_score",
    "fantasy score":  "fantasy_score",
    "fantasy score (pp)": "fantasy_score",
    "pp fantasy":     "fantasy_score",
    "prizepicks":     "fantasy_score",
    # ── WNBA (basketball) stats ──────────────────────────────────────────────
    "pts":            "points",
    "point":          "points",
    "points":         "points",
    "reb":            "rebounds",
    "rebs":           "rebounds",
    "rebound":        "rebounds",
    "rebounds":       "rebounds",
    "ast":            "assists",
    "asts":           "assists",
    "assist":         "assists",
    "assists":        "assists",
    "pra":            "pts_reb_ast",
    "p+r+a":          "pts_reb_ast",
    "pts+reb+ast":    "pts_reb_ast",
    "pts_reb_ast":    "pts_reb_ast",
    "pr":             "pts_reb",
    "p+r":            "pts_reb",
    "pts+reb":        "pts_reb",
    "pts_reb":        "pts_reb",
    "pa":             "pts_ast",
    "p+a":            "pts_ast",
    "pts+ast":        "pts_ast",
    "pts_ast":        "pts_ast",
    "ra":             "reb_ast",
    "r+a":            "reb_ast",
    "reb+ast":        "reb_ast",
    "reb_ast":        "reb_ast",
    "3pt":            "threes",
    "3s":             "threes",
    "3pm":            "threes",
    "threes":         "threes",
    "three_pointers": "threes",
}

# WNBA (basketball) prop_types — used to route /prediction + /analyze to the
# WNBA scoring pipeline instead of the MLB one.
_WNBA_PROP_TYPES = {
    "points", "rebounds", "assists",
    "pts_reb_ast", "pts_reb", "pts_ast", "reb_ast", "threes",
}

# ── autocomplete helpers ──────────────────────────────────────────────────────
# Build a deduplicated stat choice list: short alias → display name
_STAT_CHOICES: list[app_commands.Choice] = []
_seen_stats: set[str] = set()
for _alias, _prop in _STAT_ALIASES.items():
    if _prop not in _seen_stats and len(_alias) <= 10:
        _seen_stats.add(_prop)
        _STAT_CHOICES.append(app_commands.Choice(name=_alias, value=_alias))


async def _player_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice]:
    """Fuzzy player search — returns up to 25 matches from cached MLB roster."""
    if not current or len(current) < 1:
        return []
    try:
        matches = await asyncio.get_event_loop().run_in_executor(
            None, vortex_research.search_players_local, current, 25
        )
    except Exception:
        return []
    return [
        app_commands.Choice(
            name=f"{m['name']} ({m['team']})" if m["team"] else m["name"],
            value=m["name"],
        )
        for m in matches
    ]


async def _stat_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice]:
    """Stat type autocomplete — fuzzy match over available shortcuts."""
    if not current:
        return _STAT_CHOICES[:25]
    q = current.lower().strip()
    return [c for c in _STAT_CHOICES if q in c.name.lower()][:25]


@tree.command(name="prediction", description="📋 Type a prop manually to get the full analysis card — no image needed")
@app_commands.describe(
    player="Player full name (e.g. 'Ryne Nelson' or 'A'ja Wilson')",
    stat="MLB: K, H, HRR, HR, TB, RBI, R, BB, FS · WNBA: PTS, REB, AST, PRA, PR, PA, RA, 3PT",
    line="The prop line value (e.g. 4.5 or 4)",
    side="Which side of the prop",
)
@app_commands.choices(side=[
    app_commands.Choice(name="More / Over", value="over"),
    app_commands.Choice(name="Less / Under", value="under"),
])
async def cmd_prediction(
    interaction: discord.Interaction,
    player: str,
    stat: str,
    line: float,
    side: app_commands.Choice[str],
):
    await interaction.response.defer()

    stat_key  = stat.strip().lower().replace(" ", "_").replace("+", "+")
    prop_type = _STAT_ALIASES.get(stat_key) or _STAT_ALIASES.get(stat.strip().lower())

    if not prop_type:
        await interaction.followup.send(
            f"❌ Unknown stat **\"{stat}\"**.\n"
            "**MLB:** `K`/`Ks` (strikeouts) · `H` (hits) · `HRR` (Hits+Runs+RBIs) · "
            "`HR` (home runs) · `TB` (total bases) · `RBI` · `R` (runs) · `BB` (walks) · "
            "`HA` (hits allowed) · `ERA`/`ER` (earned runs) · `PO`/`Outs` (pitching outs) · "
            "`FP` (fantasy score)\n"
            "**WNBA:** `PTS` (points) · `REB` (rebounds) · `AST` (assists) · "
            "`PRA` (pts+reb+ast) · `PR` (pts+reb) · `PA` (pts+ast) · `RA` (reb+ast) · `3PT` (threes)",
            ephemeral=True,
        )
        return

    prop = {
        "player_name": player.strip(),
        "line":        float(line),
        "side":        side.value,
        "prop_type":   prop_type,
    }
    await _run_analyze(interaction, prop)


@cmd_prediction.autocomplete("player")
async def autocomplete_player(interaction: discord.Interaction, current: str):
    return await _player_autocomplete(interaction, current)


@cmd_prediction.autocomplete("stat")
async def autocomplete_stat(interaction: discord.Interaction, current: str):
    return await _stat_autocomplete(interaction, current)


# ── /analyze ───────────────────────────────────────────────────────────────────

@tree.command(name="analyze", description="📸 Upload a bet slip screenshot for an instant graded analysis card")
@app_commands.describe(
    slip="Screenshot of your bet slip (PNG / JPG / WEBP)",
    side="Override the pick direction — use when PrizePicks More/Less can't be auto-detected",
)
@app_commands.choices(side=[
    app_commands.Choice(name="More / Over",  value="over"),
    app_commands.Choice(name="Less / Under", value="under"),
])
async def cmd_analyze(
    interaction: discord.Interaction,
    slip: discord.Attachment,
    side: app_commands.Choice[str] | None = None,
):
    await interaction.response.defer()
    try:
        if not slip.content_type or not slip.content_type.startswith("image/"):
            await interaction.followup.send("❌ Please attach an image file (PNG, JPG, or WEBP).", ephemeral=True)
            return

        try:
            image_bytes = await slip.read()
        except Exception as exc:
            await interaction.followup.send(f"❌ Could not download attachment: `{exc}`", ephemeral=True)
            return

        slip_data = await vortex_analyze.extract_slip_data(image_bytes)
        if "error" in slip_data:
            await interaction.followup.send(f"❌ {slip_data['error']}", ephemeral=True)
            return

        # Override side if explicitly provided (fixes PrizePicks More/Less ambiguity)
        if side is not None:
            slip_data["side"] = side.value
            if slip_data.get("all_props"):
                for p in slip_data["all_props"]:
                    p["side"] = side.value

        all_props = slip_data.get("all_props") or [slip_data]
        ocr_raw   = slip_data.get("_ocr_raw", "")

        # Hard gate: check direction on all props
        no_dir = [p for p in all_props if not p.get("side") or p["side"] not in ("over", "under")]
        if no_dir:
            names = ", ".join(p.get("player_name", "?") for p in no_dir)
            await interaction.followup.send(
                f"❌ Could not determine Over/Under direction for: **{names}**\n"
                "Try using the `/analyze` `side` override dropdown.",
                ephemeral=True,
            )
            return

        if len(all_props) >= 2:
            # Multiple props detected — show a select menu so user picks one
            names = " · ".join(
                f"{p['player_name']} ({'O' if p['side']=='over' else 'U'} {p['line']})"
                for p in all_props
            )
            view = MultiPropView(all_props)
            await interaction.followup.send(
                content=(
                    f"📋 **{len(all_props)} props detected** in this slip:\n{names}\n\n"
                    "Select one below to run a full analysis:"
                ),
                view=view,
            )
            return

        # Single prop — run immediately, but if we have OCR text send a debug
        await _run_analyze(interaction, slip_data)

    except Exception as exc:
        import traceback
        await interaction.followup.send(f"❌ Error: ```{traceback.format_exc()[-1800:]}```", ephemeral=True)


# ── research helpers ───────────────────────────────────────────────────────────

def _player_thumbnail(card: dict) -> str:
    return card.get("thumbnail_url", "")


def _research_overview_embed(card: dict) -> discord.Embed:
    name     = card["name"]
    team     = card["team"]
    pos      = card["position"]
    bat_side = card.get("bat_side", "")
    pitcher  = card.get("pitcher") or {}
    gi       = card.get("game_info") or {}
    opp      = gi.get("opponent", "")
    home     = gi.get("is_home")
    slash    = card.get("slash_line") or {}
    sc       = card.get("statcast") or {}
    l10b     = card.get("l10_breakdown") or {}
    cvt      = card.get("career_vs_team") or {}

    gp   = slash.get("gp", 0)
    loc  = ("🏠 Home" if home else "✈️ Away") if home is not None else ""
    desc = f"**{team}** · {pos} · {gp} GP this season"
    if opp:
        desc += f"\n{loc} vs **{opp}** tonight"

    embed = discord.Embed(title=f"{name}", description=desc, color=0x00D4FF)
    if _player_thumbnail(card):
        embed.set_thumbnail(url=_player_thumbnail(card))

    # — per-game averages (L10)
    if l10b and l10b.get("games", 0) > 0:
        n = l10b["games"]
        embed.add_field(
            name=f"— per-game averages (L{n})",
            value=(
                f"H **{l10b.get('h','?')}**  ·  HR **{l10b.get('hr','?')}**  ·  RBI **{l10b.get('rbi','?')}**  ·  "
                f"R **{l10b.get('r','?')}**  ·  TB **{l10b.get('tb','?')}**  ·  BB **{l10b.get('bb','?')}**"
            ),
            inline=False,
        )

    # — season line
    season_parts = []
    if slash.get("avg"):
        sb = slash.get("sb", 0)
        sl = f"**{slash['avg']}** / **{slash['obp']}** / **{slash['slg']}**  ·  OPS **{slash['ops']}**"
        if sb: sl += f"  ·  {sb} SB"
        season_parts.append(sl)
    sc_parts = []
    if sc.get("exit_velocity"): sc_parts.append(f"EV {sc['exit_velocity']:.1f} mph")
    if sc.get("barrel_pct"):    sc_parts.append(f"{sc['barrel_pct']:.0f}% barrel")
    if sc.get("hard_hit_pct"):  sc_parts.append(f"{sc['hard_hit_pct']:.0f}% hard-hit")
    if sc_parts:
        season_parts.append("contact: " + "  ·  ".join(sc_parts))
    if season_parts:
        embed.add_field(name="— season line", value="\n".join(season_parts), inline=False)

    # — recent form
    splits     = card.get("splits") or {}
    l5         = splits.get("l5") or {}
    l10        = splits.get("l10") or {}
    l20        = splits.get("l20") or {}
    stat_label = vortex_research.STAT_LABELS.get(card.get("stat_type", ""), "")
    line       = splits.get("line", 1.5)
    if l10:
        r5    = (l5.get("rate") or 0) if l5 else 0
        r10   = l10.get("rate") or 0
        r20   = (l20.get("rate") or 0) if l20 else 0
        avg10 = l10.get("avg", 0)
        streak = l5.get("streak", 0) if l5 else 0
        def _ri(r): return "🔥" if r >= 70 else "✅" if r >= 50 else "❌"
        streak_txt = ""
        if streak >= 3:    streak_txt = f"\n🔥 {streak}-game streak"
        elif streak <= -3: streak_txt = f"\n❄️ {abs(streak)}-game cold streak"
        embed.add_field(
            name=f"— form · {stat_label} o{line}",
            value=(
                f"L5 {_ri(r5)} **{r5:.0f}%**  ·  L10 {_ri(r10)} **{r10:.0f}%**  ·  L20 {_ri(r20)} **{r20:.0f}%**\n"
                f"averaging **{avg10}**/g over last 10{streak_txt}"
            ),
            inline=False,
        )

    # — tonight's matchup
    pname = pitcher.get("name") or card.get("pitcher_name")
    if pname:
        era  = pitcher.get("era", "?")
        fip  = pitcher.get("fip", "?")
        hand = pitcher.get("hand", "?")
        h_note = ""
        bvh    = card.get("batter_vs_hand") or {}
        if bat_side and hand and hand != "?":
            bats   = bat_side[0].upper()
            vs_avg = bvh.get("avg", "---") if bvh.get("hand") == hand else "---"
            vs_ops = bvh.get("ops", "---") if bvh.get("hand") == hand else "---"
            vs_pa  = bvh.get("pa", 0) or 0
            first  = card.get("name", "").split()[0]
            throws = "left-handed" if hand == "L" else "right-handed"
            is_fav = (bats == "R" and hand == "L") or (bats == "L" and hand == "R")

            if bats == "S":
                h_note = f"\n↔️ switch hitter vs {throws} pitcher"
            elif vs_pa >= 20 and vs_avg not in ("---", ""):
                try:
                    _avg_f = float(vs_avg)
                    _ops_f = float(vs_ops) if vs_ops not in ("---", ".---", "") else None
                    _good  = _avg_f >= 0.280 or (_ops_f and _ops_f >= 0.800)
                    _poor  = _avg_f <= 0.220 or (_ops_f and _ops_f <= 0.650)
                    _icon  = "🟢" if _good else ("🔴" if _poor else "🟡")
                    _level = "well" if _good else ("poorly" if _poor else "at an average level")
                    _stats = f"**{vs_avg}** AVG"
                    if _ops_f:
                        _stats += f" · **{vs_ops}** OPS"
                    if _good:
                        _outcome = "a boost for production"
                    elif _poor:
                        _outcome = "a risk — this handedness suppresses his output"
                    else:
                        _outcome = "neutral"
                    h_note = (
                        f"\n{_icon} {throws} pitcher — {first} hits {hand}HP {_level} ({_stats}). {_outcome}."
                    )
                except (ValueError, TypeError):
                    h_note = f"\n{'✅ favorable platoon' if is_fav else '🟡 same-side matchup'} — {bats}HB vs {throws} pitcher"
            else:
                h_note = f"\n{'✅ favorable platoon' if is_fav else '🟡 same-side matchup'} — {bats}HB vs {throws} pitcher"
        embed.add_field(
            name="— tonight's matchup",
            value=f"**{pname}** ({hand}HP) — {era} ERA · {fip} FIP{h_note}",
            inline=False,
        )

    # — career vs team
    if cvt.get("ab", 0) >= 10:
        embed.add_field(
            name=f"— career vs {opp}",
            value=f"**{cvt['avg']}** AVG  ·  **{cvt['ops']}** OPS  ·  {cvt['ab']} AB  ·  {cvt['hr']} HR",
            inline=False,
        )

    embed.set_footer(text="VORTEX · select a stat below to drill in")
    return embed


def _research_games_embed(card: dict) -> discord.Embed:
    name   = card["name"]
    stat   = card.get("stat_type", "hits_runs_rbis")
    label  = vortex_research.STAT_LABELS.get(stat, stat)
    games  = card.get("recent_games") or []
    splits = card.get("splits") or {}
    ha     = card.get("home_away") or {}

    embed = discord.Embed(
        title=f"{name} — {label} · last {len(games)} games",
        color=0x5865F2,
    )
    if _player_thumbnail(card):
        embed.set_thumbnail(url=_player_thumbnail(card))

    if not games:
        embed.description = "No recent game log available."
        return embed

    lines = []
    for g in games:
        val      = g.get("stat_value", 0)
        raw_date = g.get("date", "")
        # Format: MM/DD
        if raw_date and len(raw_date) >= 10:
            date_fmt = raw_date[5:10].replace("-", "/")
        else:
            date_fmt = raw_date or "?"
        opp_abbr = g.get("opp_abbr") or (g.get("opponent", "?")[:3].upper())
        is_home  = g.get("is_home")
        at       = "vs" if is_home else "@"
        lines.append(f"`{date_fmt}` {at} {opp_abbr}: **{val}**")

    # Split into two columns of 5 to avoid hitting field char limits
    mid = len(lines) // 2
    col1 = "\n".join(lines[:5])
    col2 = "\n".join(lines[5:10]) if len(lines) > 5 else ""

    embed.add_field(name=f"last {min(len(lines), 5)} games", value=col1 or "—", inline=True)
    if col2:
        embed.add_field(name=f"games {6}–{min(10, len(lines))}", value=col2, inline=True)

    vals = [g.get("stat_value", 0) for g in games]
    summary_parts = []
    if len(vals) >= 5:
        l5_avg  = round(sum(vals[:5])  / 5,  2)
        summary_parts.append(f"L5 **{l5_avg}**/g")
    if len(vals) >= 10:
        l10_avg = round(sum(vals[:10]) / 10, 2)
        summary_parts.append(f"L10 **{l10_avg}**/g")
    if summary_parts:
        embed.add_field(name="— averages", value="  ·  ".join(summary_parts), inline=False)

    ha_parts = []
    if ha.get("home_avg") is not None:
        ha_parts.append(f"🏠 home: **{ha['home_avg']}**/g ({ha['home_games']}g)")
    if ha.get("away_avg") is not None:
        ha_parts.append(f"✈️ away: **{ha['away_avg']}**/g ({ha['away_games']}g)")
    if ha_parts:
        embed.add_field(name="— home / away", value="  ·  ".join(ha_parts), inline=False)

    embed.set_footer(text="VORTEX · pick another stat below")
    return embed


def _research_matchup_embed(card: dict) -> discord.Embed:
    name     = card["name"]
    pitcher  = card.get("pitcher") or {}
    bvp      = card.get("bvp") or {}
    gi       = card.get("game_info") or {}
    cvt      = card.get("career_vs_team") or {}
    bat_side = card.get("bat_side", "")
    slash    = card.get("slash_line") or {}

    pname = pitcher.get("name") or card.get("pitcher_name", "Unknown")
    hand  = pitcher.get("hand", "?")
    era   = pitcher.get("era")

    # Quality assessment — 7-tier ERA ladder
    # Neutral band: 3.50–4.49 (most MLB starters live here)
    # Favorable (good for hitter/Over): ERA ≥ 4.50
    # Tough (good for Under): ERA ≤ 3.49
    if era is not None:
        try:
            era_f = float(era)
            if era_f <= 2.24:
                quality = "🔴 VERY TOUGH MATCHUP"
                q_note  = f"Ace-level arm ({era} ERA) — strong Under lean"
            elif era_f <= 2.99:
                quality = "🔴 TOUGH MATCHUP"
                q_note  = f"Strong run prevention ({era} ERA) — fewer event chains"
            elif era_f <= 3.49:
                quality = "🔴 MILDLY TOUGH MATCHUP"
                q_note  = f"Above-average suppression ({era} ERA)"
            elif era_f <= 4.49:
                quality = "🟡 NEUTRAL MATCHUP"
                q_note  = f"Average pitcher ({era} ERA) · league-normal run environment"
            elif era_f <= 5.00:
                quality = "🟢 MILDLY FAVORABLE MATCHUP"
                q_note  = f"Slight hitter edge ({era} ERA)"
            elif era_f <= 6.00:
                quality = "🟢 FAVORABLE MATCHUP"
                q_note  = f"Weak pitcher ({era} ERA) — hitter advantage"
            else:
                quality = "🟢 HIGHLY FAVORABLE MATCHUP"
                q_note  = f"Bad pitcher ({era} ERA) — Overs spike here"
        except Exception:
            quality = "🟡 NEUTRAL MATCHUP"
            q_note  = f"{era} ERA"
    else:
        quality = "⚪ NO STARTER CONFIRMED"
        q_note  = "Check lineup closer to first pitch"

    # Handedness note
    hand_note = ""
    if bat_side and hand and hand != "?":
        throws  = "left-handed" if hand == "L" else "right-handed"
        bats    = bat_side[0].upper()
        first   = name.split()[0]
        bvh     = card.get("batter_vs_hand") or {}
        vs_avg  = bvh.get("avg", "---") if bvh.get("hand") == hand else "---"
        vs_ops  = bvh.get("ops", "---") if bvh.get("hand") == hand else "---"
        vs_pa   = bvh.get("pa", 0) or 0
        is_fav  = (bats == "R" and hand == "L") or (bats == "L" and hand == "R")

        if bats == "S":
            hand_note = f"↔️ **Handedness** — Switch hitter vs {throws} pitcher"
        elif vs_pa >= 20 and vs_avg not in ("---", ""):
            try:
                _avg_f = float(vs_avg)
                _ops_f = float(vs_ops) if vs_ops not in ("---", ".---", "") else None
                _good  = _avg_f >= 0.280 or (_ops_f and _ops_f >= 0.800)
                _poor  = _avg_f <= 0.220 or (_ops_f and _ops_f <= 0.650)
                _icon  = "🟢" if _good else ("🔴" if _poor else "🟡")
                _level = "well" if _good else ("poorly" if _poor else "at an average level")
                _stats = f"**{vs_avg}** AVG"
                if _ops_f:
                    _stats += f" · **{vs_ops}** OPS"
                if _good:
                    _outcome = "a boost for production"
                elif _poor:
                    _outcome = "a risk — this handedness suppresses his output"
                else:
                    _outcome = "neutral"
                hand_note = (
                    f"{_icon} **Handedness** — This pitcher throws {throws}, and {first} hits "
                    f"{hand}HP {_level} ({_stats}). This is {_outcome}."
                )
            except (ValueError, TypeError):
                hand_note = f"{'✅ Favorable platoon' if is_fav else '🟡 Same-side matchup'} — {bats}-handed batter vs {throws} pitcher"
        else:
            hand_note = f"{'✅ Favorable platoon' if is_fav else '🟡 Same-side matchup'} — {bats}-handed batter vs {throws} pitcher"

    opp  = gi.get("opponent", "")
    home = gi.get("is_home")
    loc  = ("🏠 Home" if home else "✈️ Away") if home is not None else ""
    spot_line = f"{loc} vs {opp}" if opp else ""

    # Batter season slash line
    batter_slash = ""
    if slash.get("avg") and slash["avg"] not in (".---", "-.--"):
        batter_slash = f"  ·  {slash['avg']}/{slash.get('obp','.---')}/{slash.get('slg','.---')} OPS {slash.get('ops','.---')}"

    desc = f"**{quality}** — {q_note}{batter_slash}"
    if hand_note:  desc += f"\n{hand_note}"
    if spot_line:  desc += f"\n{spot_line}"

    embed = discord.Embed(
        title=f"{name} — matchup vs {pname}",
        description=desc,
        color=0xFEE75C,
    )
    if _player_thumbnail(card):
        embed.set_thumbnail(url=_player_thumbnail(card))

    # Pitcher profile
    if pitcher.get("era"):
        fip   = pitcher.get("fip", "?")
        hr9   = pitcher.get("hr_per_9", "?")
        k9    = pitcher.get("k_per_9", "?")
        bb9   = pitcher.get("bb_per_9", "?")
        last5 = pitcher.get("last_5_starts") or []

        # Slash line allowed (BA/OBP/SLG against this pitcher)
        avg_a = pitcher.get("avg_against", "")
        obp_a = pitcher.get("obp_against", "")
        slg_a = pitcher.get("slg_against", "")
        slash_allowed = ""
        if avg_a and obp_a and slg_a and avg_a not in (".---", "-.--"):
            slash_allowed = f"\nAllows **{avg_a}/{obp_a}/{slg_a}**  ·  K/9 **{k9}**"

        embed.add_field(
            name=f"— {pname} ({hand}HP)",
            value=(
                f"ERA **{era}**  ·  FIP **{fip}**  ·  HR/9 **{hr9}**\n"
                f"K/9 **{k9}**  ·  BB/9 **{bb9}**"
                f"{slash_allowed}"
            ),
            inline=False,
        )

        if last5:
            starts = []
            for s in last5[:4]:
                ip  = s.get("ip", "?")
                er  = s.get("er", "?")
                ks  = s.get("k", "?")
                opp = s.get("opponent", "")
                starts.append(f"`{ip}IP {er}ER {ks}K` vs {opp}")
            embed.add_field(name="— recent starts", value="\n".join(starts), inline=False)

    # — head to head
    ab = bvp.get("ab", 0)
    if ab >= 5:
        bavg = bvp.get("avg", ".000")
        bhr  = bvp.get("hr", 0)
        bk   = bvp.get("k", 0)
        bops = bvp.get("ops", ".---")
        bobp = bvp.get("obp", ".---") if "obp" in bvp else None
        bslg = bvp.get("slg", ".---") if "slg" in bvp else None
        try:
            avg_f = float(bavg)
            owns  = "🟢 owns him" if avg_f >= 0.300 else ("🔴 struggles" if avg_f < 0.200 else "")
        except (ValueError, TypeError):
            owns = ""
        slash_bvp = f" · **{bavg}/{bobp}/{bslg}**" if bobp and bobp != ".---" else f" · **{bavg}** AVG"
        sample_note = " *(small sample)*" if ab < 12 else ""
        embed.add_field(
            name=f"— head to head vs {pname}",
            value=f"**{ab} AB**{slash_bvp} · {bhr} HR · {bk} K{sample_note}  {owns}",
            inline=False,
        )
    else:
        embed.add_field(
            name="— head to head",
            value=f"No significant history vs {pname} — fewer than 5 career AB.",
            inline=False,
        )

    if cvt.get("ab", 0) >= 5:
        opp_name = gi.get("opponent", "tonight's team")
        embed.add_field(
            name=f"— career vs {opp_name}",
            value=f"**{cvt['avg']}** AVG · **{cvt['ops']}** OPS · {cvt['ab']} AB · {cvt['hr']} HR · {cvt['rbi']} RBI",
            inline=False,
        )

    embed.set_footer(text="VORTEX · pick another stat below")
    return embed


def _research_splits_embed(card: dict) -> discord.Embed:
    name       = card["name"]
    stat_type  = card.get("stat_type", "hits_runs_rbis")
    all_splits = card.get("all_splits") or {}
    slash      = card.get("slash_line") or {}
    ha         = card.get("home_away") or {}
    sc         = card.get("statcast") or {}
    splits     = card.get("splits") or {}

    label = vortex_research.STAT_LABELS.get(stat_type, stat_type)

    # Season total + per-game
    s_avg  = splits.get("season_avg", "?")
    gp     = splits.get("games_played", 0) or slash.get("gp", 0)
    season_total = ""
    if s_avg and gp:
        try:
            total = round(float(s_avg) * int(gp))
            season_total = f"**{s_avg}**/game · {total} total in {gp} games"
        except Exception:
            season_total = f"**{s_avg}**/game"

    embed = discord.Embed(
        title=f"{name} — {label} · splits",
        description=f"season: {season_total}" if season_total else None,
        color=0x57F287,
    )
    if _player_thumbnail(card):
        embed.set_thumbnail(url=_player_thumbnail(card))

    # Home / Away
    ha_parts = []
    if ha.get("home_avg") is not None:
        ha_parts.append(f"🏠 Home: **{ha['home_avg']}**/g ({ha['home_games']}g)")
    if ha.get("away_avg") is not None:
        ha_parts.append(f"✈️ Away: **{ha['away_avg']}**/g ({ha['away_games']}g)")
    if ha_parts:
        embed.add_field(name="— home / away (L10)", value="  ·  ".join(ha_parts), inline=False)

    if slash.get("avg"):
        sl = f"**{slash['avg']}** / **{slash['obp']}** / **{slash['slg']}**  ·  OPS **{slash['ops']}**"
        embed.add_field(name="— slash line", value=sl, inline=False)

    if sc.get("hard_hit_pct") or sc.get("barrel_pct") or sc.get("exit_velocity"):
        ev  = sc.get("exit_velocity")
        hh  = sc.get("hard_hit_pct")
        brl = sc.get("barrel_pct")
        parts = []
        if hh:  parts.append(f"**{hh:.0f}%** hard-hit")
        if brl: parts.append(f"**{brl:.0f}%** barrel")
        if ev:  parts.append(f"**{ev:.1f} mph** EV")
        embed.add_field(name="— contact quality", value="  ·  ".join(parts), inline=False)

    # All stat hit rates
    def fmt(d):
        if not d: return "n/a"
        r = d.get("rate") or 0
        h = d.get("hits", 0)
        g = d.get("games", 0)
        icon = "🔥" if r >= 70 else "✅" if r >= 50 else "❌"
        return f"{icon} {r:.0f}% ({h}/{g})"

    stat_lines = []
    for st, sp in all_splits.items():
        l5   = sp.get("l5") or {}
        l10  = sp.get("l10") or {}
        l20  = sp.get("l20") or {}
        avg  = sp.get("season_avg", "?")
        line = sp.get("line", "?")
        lbl  = vortex_research.STAT_LABELS.get(st, st)
        stat_lines.append(
            f"**{lbl}** (avg {avg} · line o{line})\n"
            f"L5 {fmt(l5)} · L10 {fmt(l10)} · L20 {fmt(l20)}"
        )

    if stat_lines:
        # Discord field value limit is 1024 chars — chunk if needed
        chunk, chunks = [], []
        for line in stat_lines:
            chunk.append(line)
            if len("\n\n".join(chunk)) > 900:
                chunks.append(chunk[:-1])
                chunk = [chunk[-1]]
        if chunk:
            chunks.append(chunk)
        for i, ch in enumerate(chunks):
            embed.add_field(
                name="— hit rates by stat" if i == 0 else "— continued",
                value="\n\n".join(ch),
                inline=False,
            )

    embed.set_footer(text="VORTEX · hit rate = over line")
    return embed


def _research_pitcher_embed(card: dict) -> discord.Embed:
    """Full pitcher research card — ERA, K/9, recent starts, opponent K%, trends."""
    name      = card["name"]
    team      = card["team"]
    hand      = card.get("hand", "")
    gi        = card.get("game_info") or {}
    m         = card.get("metrics") or {}
    opp_kr      = card.get("opp_k_rate") or {}
    opp_hand    = card.get("opp_vs_hand") or {}
    cvt         = card.get("career_vs_team") or {}
    ha          = card.get("home_away_era") or {}
    k_rates     = card.get("k_hit_rates") or {}
    lineup_bvp  = card.get("lineup_bvp") or []
    l5_k_avg    = card.get("l5_k_avg")
    k_per_gs    = card.get("k_per_gs")

    opp  = gi.get("opponent", "")
    home = gi.get("is_home")
    loc  = ("🏠 Home" if home else "✈️ Away") if home is not None else ""

    desc = f"**Starting Pitcher** · {hand}HP · {team}"
    if opp:
        desc += f"\n{loc} vs **{opp}** tonight"

    embed = discord.Embed(title=f"{name} — pitcher profile", description=desc, color=0xFEE75C)
    if card.get("thumbnail_url"):
        embed.set_thumbnail(url=card["thumbnail_url"])

    # Season line
    if m.get("era"):
        ip   = m.get("innings_pitched", "?")
        gs   = m.get("games_started", "?")
        k9   = m.get("k_per_9", "?")
        bb9  = m.get("bb_per_9", "?")
        hr9  = m.get("hr_per_9", "?")
        whip = m.get("whip", "?")
        fip  = m.get("fip", "?")
        avg  = m.get("avg_against", "?")
        tot_ks = m.get("season_ks", "?")
        embed.add_field(
            name="— 2026 season",
            value=(
                f"ERA **{m['era']}**  ·  FIP **{fip}**  ·  WHIP **{whip}**\n"
                f"K/9 **{k9}**  ·  BB/9 **{bb9}**  ·  HR/9 **{hr9}**\n"
                f"IP **{ip}**  ·  GS **{gs}**  ·  total Ks **{tot_ks}**  ·  BAA **{avg}**"
            ),
            inline=False,
        )

    kr = m.get("season_k_rate")
    if kr or l5_k_avg or k_per_gs:
        parts = []
        if kr:       parts.append(f"**{kr*100:.1f}%** K rate")
        if k_per_gs: parts.append(f"**{k_per_gs}** Ks/start (season)")
        if l5_k_avg: parts.append(f"**{l5_k_avg}** Ks/start (L5)")
        if l5_k_avg and k_per_gs:
            if l5_k_avg > k_per_gs + 0.5:
                parts.append("📈 trending up")
            elif l5_k_avg < k_per_gs - 0.5:
                parts.append("📉 trending down")
        embed.add_field(name="— strikeout metrics", value="  ·  ".join(parts), inline=False)

    last5 = m.get("last_5_starts") or []
    if last5:
        starts = []
        for s in last5[:5]:
            raw_date = s.get("date", "")
            date_fmt = raw_date[5:10].replace("-", "/") if len(raw_date) >= 10 else raw_date
            opp_s = (s.get("opponent", "") or "")[:3].upper()
            ip_s  = s.get("ip", "?")
            er_s  = s.get("er", "?")
            k_s   = s.get("k", "?")
            bb_s  = s.get("bb", "?")
            starts.append(f"`{date_fmt}` vs {opp_s:<4} — **{k_s}K**  {ip_s}IP  {er_s}ER  {bb_s}BB")
        embed.add_field(name="— recent starts", value="\n".join(starts), inline=False)

    if k_rates:
        lines_text = []
        for line_str in ["4.5", "5.5", "6.5", "7.5"]:
            lr = k_rates.get(line_str, {})
            if not lr:
                continue
            parts_r = []
            for lbl in ["l5", "l10", "l20"]:
                d = lr.get(lbl)
                if d:
                    icon = "🔥" if d["rate"] >= 70 else "✅" if d["rate"] >= 50 else "❌"
                    parts_r.append(f"{icon}{d['rate']:.0f}% ({d['hits']}/{d['games']})")
            if parts_r:
                lines_text.append(f"**o{line_str}:** " + "  ·  ".join(parts_r))
        if lines_text:
            embed.add_field(
                name="— K line hit rates (L5 · L10 · L20)",
                value="\n".join(lines_text),
                inline=False,
            )

    # Opponent team K rate + ranking
    if opp_kr.get("k_pct"):
        k_pct = opp_kr["k_pct"]
        rank  = opp_kr.get("rank")
        total = opp_kr.get("total_teams", 30)
        league_avg = 22.0

        # Rank label: rank 1 = hardest to K (lowest K rate), rank 30 = easiest to K (highest K rate)
        if rank:
            if rank <= 8:
                rank_label = f"🔴 **#{rank}/30** contact lineup, low K rate — TOUGH"
            elif rank <= 20:
                rank_label = f"🟡 **#{rank}/30** average K lineup — NEUTRAL"
            else:
                rank_label = f"🟢 **#{rank}/30** strikeout-prone lineup — FAVORABLE"
        else:
            rank_label = "🟡 K rate unavailable"

        embed.add_field(
            name=f"— {opp} vs strikeouts",
            value=f"{rank_label}\n**{k_pct}%** K rate  ·  BAA **{opp_kr.get('avg', '?')}**  (league avg ~{league_avg}%)",
            inline=False,
        )

    if opp_hand.get("k_pct"):
        h_label = "LHP" if opp_hand.get("hand") == "L" else "RHP"
        embed.add_field(
            name=f"— {opp} vs {h_label}",
            value=f"**{opp_hand['k_pct']}%** K rate vs {h_label}  ·  AVG **{opp_hand['avg']}**  ·  OPS **{opp_hand['ops']}**  ({opp_hand['pa']} PA)",
            inline=False,
        )

    if lineup_bvp:
        bvp_lines = []
        for h in lineup_bvp[:5]:
            k_pct = h.get("k_pct", 0)
            k_icon = "🔥" if k_pct >= 30 else "✅" if k_pct >= 20 else "⚠️"
            bvp_lines.append(
                f"{k_icon} **{h['name']}** — {h['ab']} AB · {h['avg']} AVG · **{h['k']}K** ({k_pct:.0f}%)"
                + (f" · {h['hr']}HR" if h['hr'] else "")
            )
        embed.add_field(
            name=f"— tonight's lineup vs {name}",
            value="\n".join(bvp_lines),
            inline=False,
        )

    if cvt.get("ip", "0.0") not in ("0.0", "0", None, ""):
        embed.add_field(
            name=f"— career vs {opp}",
            value=f"**{cvt['ip']}** IP  ·  **{cvt['k']}** K  ·  **{cvt['bb']}** BB  ·  ERA **{cvt['era']}**  ·  BAA **{cvt['avg']}**",
            inline=False,
        )

    if ha.get("home_era") or ha.get("away_era"):
        parts = []
        if ha.get("home_era"): parts.append(f"🏠 home ERA: **{ha['home_era']}**")
        if ha.get("away_era"): parts.append(f"✈️ away ERA: **{ha['away_era']}**")
        embed.add_field(name="— home / away ERA", value="  ·  ".join(parts), inline=False)

    embed.set_footer(text="VORTEX · pitcher profile")
    return embed


def _send_research_card(card: dict, active_tab: str = "overview"):
    """Return (embed, view) — handles both batters and pitchers."""
    if card.get("is_pitcher"):
        return _research_pitcher_embed(card), None
    return _research_overview_embed(card), ResearchView(card, active_tab)


class ResearchStatSelect(discord.ui.Select):
    """Dropdown to switch stat type in research view."""
    def __init__(self, card: dict, current_view: "ResearchView"):
        self.card         = card
        self.current_view = current_view
        options = [
            discord.SelectOption(
                label=vortex_research.STAT_LABELS.get(st, st),
                value=st,
                default=(st == card.get("stat_type")),
            )
            for st in vortex_research.STAT_TYPES
            if st in (card.get("all_splits") or {})
        ]
        super().__init__(
            placeholder="📊 Change stat type...",
            min_values=1, max_values=1,
            options=options[:25],
        )

    async def callback(self, interaction: discord.Interaction):
        new_stat = self.values[0]
        new_card = dict(self.card)
        new_card["stat_type"] = new_stat
        splits = (self.card.get("all_splits") or {}).get(new_stat)
        if splits:
            new_card["splits"] = splits

        # Recompute stat_value in recent_games for the new stat type.
        # Each game already stores individual fields so no new API call needed.
        _field_map = {
            "hits": "h", "home_runs": "hr", "total_bases": "tb",
            "rbis": "rbi", "runs_scored": "r", "walks": "bb",
            "hits_runs_rbis": "hrr",
        }
        field = _field_map.get(new_stat, "hrr")
        new_recent = []
        for g in (self.card.get("recent_games") or []):
            updated = dict(g)
            updated["stat_value"] = g.get(field, 0)
            new_recent.append(updated)
        new_card["recent_games"] = new_recent

        # Recompute home/away averages from the updated games.
        home_vals = [g["stat_value"] for g in new_recent if g.get("is_home") is True]
        away_vals = [g["stat_value"] for g in new_recent if g.get("is_home") is False]
        new_card["home_away"] = {
            "home_avg":   round(sum(home_vals) / len(home_vals), 2) if home_vals else None,
            "home_games": len(home_vals),
            "away_avg":   round(sum(away_vals) / len(away_vals), 2) if away_vals else None,
            "away_games": len(away_vals),
        }

        view  = ResearchView(new_card, active_tab="overview")
        embed = _research_overview_embed(new_card)
        await interaction.response.edit_message(embed=embed, view=view)


TABS = ["overview", "games", "matchup", "splits"]

class ResearchView(discord.ui.View):
    def __init__(self, card: dict, active_tab: str = "overview"):
        super().__init__(timeout=300)
        self.card       = card
        self.active_tab = active_tab

        def _style(tab):
            return discord.ButtonStyle.primary if tab == active_tab else discord.ButtonStyle.secondary

        overview_btn = discord.ui.Button(label="📊 Overview", style=_style("overview"), row=0)
        games_btn    = discord.ui.Button(label="📋 Games",    style=_style("games"),    row=0)
        matchup_btn  = discord.ui.Button(label="⚔️ Matchup", style=_style("matchup"),  row=0)
        splits_btn   = discord.ui.Button(label="📈 Splits",   style=_style("splits"),   row=0)

        async def on_overview(interaction: discord.Interaction):
            view  = ResearchView(self.card, "overview")
            embed = _research_overview_embed(self.card)
            await interaction.response.edit_message(embed=embed, view=view)

        async def on_games(interaction: discord.Interaction):
            view  = ResearchView(self.card, "games")
            embed = _research_games_embed(self.card)
            await interaction.response.edit_message(embed=embed, view=view)

        async def on_matchup(interaction: discord.Interaction):
            view  = ResearchView(self.card, "matchup")
            embed = _research_matchup_embed(self.card)
            await interaction.response.edit_message(embed=embed, view=view)

        async def on_splits(interaction: discord.Interaction):
            view  = ResearchView(self.card, "splits")
            embed = _research_splits_embed(self.card)
            await interaction.response.edit_message(embed=embed, view=view)

        overview_btn.callback = on_overview
        games_btn.callback    = on_games
        matchup_btn.callback  = on_matchup
        splits_btn.callback   = on_splits

        self.add_item(overview_btn)
        self.add_item(games_btn)
        self.add_item(matchup_btn)
        self.add_item(splits_btn)
        self.add_item(ResearchStatSelect(card, self))


# ── /player ────────────────────────────────────────────────────────────────────
@tree.command(name="player", description="🔍 Research any MLB player — type partial name e.g. 'ohtani'")
@app_commands.describe(name="Player name — partial OK (e.g. 'ohtani', 'judge', 'vlad jr')")
async def cmd_player(interaction: discord.Interaction, name: str):
    await interaction.response.defer(ephemeral=True)

    # Fuzzy search MLB API
    matches = await asyncio.get_event_loop().run_in_executor(
        None, vortex_research.fuzzy_search, name
    )

    if not matches:
        await interaction.followup.send(
            f"No MLB player found matching **\"{name}\"**.\n"
            "Try a last name or more of the full name.",
            ephemeral=True,
        )
        return

    player   = matches[0]
    player_id = player["id"]
    full_name = player["name"]

    # Pull full research card in background thread
    card = await asyncio.get_event_loop().run_in_executor(
        None, lambda: vortex_research.get_research_card(player_id)
    )

    if "error" in card:
        await interaction.followup.send(f"❌ {card['error']}", ephemeral=True)
        return

    try:
        embed, view = _send_research_card(card)
    except Exception as e:
        import traceback; traceback.print_exc()
        await interaction.followup.send(f"❌ Error building embed: `{e}`", ephemeral=True)
        return

    kw = {"embed": embed, "ephemeral": True}
    if view:
        kw["view"] = view
    await interaction.followup.send(**kw)


@cmd_player.autocomplete("name")
async def autocomplete_player_name(interaction: discord.Interaction, current: str):
    return await _player_autocomplete(interaction, current)


# ── /top ───────────────────────────────────────────────────────────────────────
@tree.command(name="top", description="🏆 Top 5 plays right now by VORTEX score")
async def cmd_top(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    rows = get_board(limit=5)
    if not rows:
        await interaction.followup.send("Board is empty.", ephemeral=True)
        return
    embeds = board_embed(rows, "🏆 Top 5 Plays Right Now")
    await interaction.followup.send(embeds=embeds, view=BoardDetailView(rows), ephemeral=True)


# ── /refresh ───────────────────────────────────────────────────────────────────
@tree.command(name="refresh", description="⚡ Force-clear cache & rebuild tonight's board")
async def cmd_refresh(interaction: discord.Interaction):
    if not await _is_admin(interaction):
        await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    await interaction.followup.send("⏳ Clearing cache & rebuilding board — ~60s...", ephemeral=True)

    def _do_refresh():
        # 1. Wipe stale MLB stats cache so /player shows today's game
        cleared = stats_mlb.clear_cache()
        print(f"[refresh] Cleared {cleared} MLB cache files")

        # 2. Wipe phantom future-dated board rows (artifact of old midnight-ET bug)
        today = vortextime.vortex_day()
        conn  = sqlite3.connect(str(DB_PATH))
        deleted = conn.execute(
            "DELETE FROM predictions WHERE game_date > ?", (today,)
        ).rowcount
        conn.commit()
        conn.close()
        if deleted:
            print(f"[refresh] Deleted {deleted} phantom future-dated rows")

        # 3. Re-run the board engine
        update_board.main()

    loop = asyncio.get_event_loop()
    try:
        await asyncio.wait_for(
            loop.run_in_executor(None, _do_refresh),
            timeout=300,
        )
    except asyncio.TimeoutError:
        await interaction.followup.send("❌ Engine timed out after 5 minutes.", ephemeral=True)
        return
    except Exception as exc:
        await interaction.followup.send(f"❌ Engine error: `{exc}`", ephemeral=True)
        return

    total = len(get_board(limit=100))
    await interaction.followup.send(
        f"✅ Board refreshed — **{total} plays** loaded. Use `/dashboard` to see them.",
        ephemeral=True,
    )


# ── /grade ─────────────────────────────────────────────────────────────────────
@tree.command(name="grade", description="🏆 Grade picks now (default: today). Optional date like 6/16/2026.")
@app_commands.describe(
    date="Optional date to grade (e.g. 6/16/2026 or 'yesterday'). Blank = today.",
    force="Re-grade from scratch (wipes that day's results first, then re-grades).")
async def cmd_grade(interaction: discord.Interaction, date: str | None = None, force: bool = False):
    if not await _is_admin(interaction):
        await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    if date:
        today_et = _parse_user_date(date)
        if today_et is None:
            await interaction.followup.send(
                f"❌ Couldn't read `{date}`. Try `6/16/2026`, `6/16`, or `yesterday`.",
                ephemeral=True)
            return
    else:
        today_et = vortextime.vortex_day()   # betting day (rolls 4 AM Mountain)
    if force:
        conn = _db()
        reset = conn.execute(
            "UPDATE predictions SET result=NULL, actual_value=NULL, graded_at=NULL WHERE game_date=?",
            (today_et,)).rowcount
        conn.commit(); conn.close()
        await interaction.followup.send(
            f"♻️ Reset **{reset}** pick(s) for **{_iso_to_us(today_et)}** — re-grading...",
            ephemeral=True)
    else:
        await interaction.followup.send(
            f"⏳ Grading results for **{_iso_to_us(today_et)}**...", ephemeral=True
        )
    try:
        loop = asyncio.get_event_loop()
        summary = await loop.run_in_executor(None, grader.grade_date, today_et)
        pending    = summary.get("pending", 0)
        graded     = summary.get("graded", 0)
        voided     = summary.get("voided", 0)
        unresolved = summary.get("unresolved", 0)
        mlb_found  = summary.get("mlb_players", 0)

        _disp = _iso_to_us(today_et)
        if pending == 0:
            msg = f"⚠️ No pending picks found for **{_disp}** — the board may not have been logged yet."
        elif graded == 0 and voided == 0 and pending > 0:
            msg = (
                f"⚠️ Found **{pending}** picks for **{_disp}** but couldn't grade any.\n"
                f"MLB players fetched from API: **{mlb_found}**\n"
                f"Unresolved: **{unresolved}** (name mismatch or games not final yet)\n"
                + (("First few unresolved: " + ", ".join(summary.get("unresolved_list", []))) if summary.get("unresolved_list") else "")
            )
        else:
            msg = (
                f"✅ Graded **{graded}/{pending}** picks for **{_disp}**."
                + (f" ⚪ {voided} void (DNP)" if voided else "")
                + (f" · {unresolved} unresolved" if unresolved else "")
                + "\nUse `/record` to see results or `/accuracy` for your track record."
            )
        await interaction.followup.send(msg, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Grading error: `{e}`", ephemeral=True)


# ── /clearprop ─────────────────────────────────────────────────────────────────
@tree.command(name="clearprop", description="🛠️ Admin: remove a prop type from accuracy stats")
@app_commands.describe(prop="Prop type to clear (e.g. home_runs, rbis)")
async def cmd_clearprop(interaction: discord.Interaction, prop: str):
    if not await _is_admin(interaction):
        await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    # stat_type is stored as display name ("Home Runs") but user may type "home_runs"
    _display_map = {
        "home_runs":      "Home Runs",
        "hits":           "Hits",
        "total_bases":    "Total Bases",
        "rbis":           "RBIs",
        "strikeouts":     "Strikeouts",
        "hits_runs_rbis": "Hits+Runs+RBIs",
        "runs_scored":    "Runs Scored",
    }
    display_val = _display_map.get(prop.lower().replace(" ", "_"), prop)
    conn = _db()
    deleted = conn.execute(
        "DELETE FROM signal_accuracy WHERE value = ? OR value = ?", (prop, display_val)
    ).rowcount
    conn.commit()
    conn.close()
    await interaction.followup.send(
        f"✅ Cleared `{display_val}` from accuracy stats — **{deleted}** row(s) removed.\n"
        f"Home Runs are also permanently excluded from future accuracy rebuilds.",
        ephemeral=True
    )


# ── /accuracy ──────────────────────────────────────────────────────────────────
@tree.command(name="accuracy", description="📊 VORTEX hit rates by tier, signal, stat type")
async def cmd_accuracy(interaction: discord.Interaction):
    if not await _is_admin(interaction):
        await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    conn = _db()
    rows = conn.execute("""
        SELECT dimension, value, sport, total, hits, hit_rate, avg_ev
        FROM signal_accuracy
        WHERE total >= 5
        ORDER BY dimension, hit_rate DESC
    """).fetchall()

    # Overall record
    totals = conn.execute("""
        SELECT COUNT(*) as t,
               SUM(CASE WHEN result='hit' THEN 1 ELSE 0 END) as h
        FROM predictions WHERE result IS NOT NULL AND result != 'push'
    """).fetchone()
    conn.close()

    if not rows:
        await interaction.followup.send(
            "📊 No graded results yet — VORTEX needs a few days of data to build accuracy stats.\n"
            "Results are graded automatically each night after games finish.",
            ephemeral=True
        )
        return

    total_all = totals["t"] or 0
    hits_all  = totals["h"] or 0
    overall   = f"{hits_all}/{total_all} ({hits_all/total_all*100:.1f}%)" if total_all else "—"

    embed = discord.Embed(
        title="📊 VORTEX Accuracy Report",
        description=f"**Overall: {overall}** graded predictions",
        color=0x00D4FF,
    )

    # Group by dimension
    by_dim: dict[str, list] = {}
    for r in rows:
        by_dim.setdefault(r["dimension"], []).append(r)

    dim_labels = {
        "tier":      "💎 By Tier",
        "signal":    "🔥 By Signal",
        "stat_type": "📈 By Stat Type",
        "side":      "↕️ Over vs Under",
        "sport":     "🏆 By Sport",
        "book":      "📚 By Book",
    }

    for dim, label in dim_labels.items():
        if dim not in by_dim:
            continue
        lines = []
        for r in by_dim[dim][:6]:
            pct  = r["hit_rate"] * 100
            bar  = "█" * int(pct / 10)
            ev   = f"+{r['avg_ev']:.1f}%" if r["avg_ev"] and r["avg_ev"] >= 0 else f"{r['avg_ev']:.1f}%" if r["avg_ev"] else ""
            lines.append(f"`{r['value']:<16}` {pct:5.1f}%  {bar}  ({r['hits']}/{r['total']}) {ev}")
        if lines:
            embed.add_field(name=label, value="\n".join(lines), inline=False)

    embed.set_footer(text="VORTEX · Updated nightly after games finish")
    await interaction.followup.send(embed=embed, ephemeral=True)


def _parse_user_date(s: str) -> str | None:
    """Parse a user-typed date into ISO YYYY-MM-DD for DB lookup.
    Accepts US month/day/year (6/16/2026, 06-16-2026), month/day (6/16 → this year),
    ISO (2026-06-16), and 'today'/'yesterday'. Returns None if unreadable."""
    import re
    from datetime import timedelta, date
    now = vortextime.vortex_now()   # betting day frame (rolls 4 AM Mountain)
    s = (s or "").strip().lower()
    if s in ("yesterday", "yday", "y"):
        return (now.date() - timedelta(days=1)).isoformat()
    if s in ("today", "t", ""):
        return now.date().isoformat()
    parts = [p for p in re.split(r"[/\-.]", s) if p]
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 3:
        if nums[0] > 31:                 # ISO order: YYYY-MM-DD
            y, m, d = nums
        else:                            # US order: M/D/Y
            m, d, y = nums
            if y < 100:                  # 2-digit year → 20xx
                y += 2000
    elif len(nums) == 2:                 # M/D → assume current year
        m, d = nums
        y = now.year
    else:
        return None
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return None


def _iso_to_us(iso: str) -> str:
    """2026-06-16 → 6/16/2026 for display."""
    try:
        y, m, d = iso.split("-")
        return f"{int(m)}/{int(d)}/{y}"
    except Exception:
        return iso


# ── Record tier filter dropdown ────────────────────────────────────────────────

_RECORD_TIER_OPTIONS = [
    ("All Tiers",          None),
    ("💎 Elite",           "ELITE"),
    ("🔥 Strong",           "STRONG"),
    ("✅ Good",             "GOOD"),
    ("💎🔥 Elite + Strong", "ELITE|STRONG"),
    ("🔥✅ Strong + Good",  "STRONG|GOOD"),
]

class RecordFilterSelect(discord.ui.Select):
    def __init__(self, date_str: str):
        self.date_str = date_str
        options = [
            discord.SelectOption(label=label, value=val or "__all__")
            for label, val in _RECORD_TIER_OPTIONS
        ]
        super().__init__(placeholder="Filter by tier...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        val = self.values[0]
        tier_filter = None if val == "__all__" else val.split("|")
        embed = await _build_record_embed(self.date_str, tier_filter)
        await interaction.edit_original_response(embed=embed, view=RecordFilterView(self.date_str))


class RecordFilterView(discord.ui.View):
    def __init__(self, date_str: str):
        super().__init__(timeout=300)
        self.add_item(RecordFilterSelect(date_str))


async def _build_record_embed(today: str, tier_filter: list[str] | None = None) -> discord.Embed:
    """Build the /record embed for a given date, optionally filtered by tier list."""
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    _ET = _tz(_td(hours=-4))
    now_et = _dt.now(_ET)

    conn = _db()
    if tier_filter:
        placeholders = ",".join("?" for _ in tier_filter)
        rows = conn.execute(f"""
            SELECT player_name, sport, stat_type, line, side, tier,
                   ev_percentage, vortex_score, result, actual_value, best_book,
                   commence_time, pitcher_name
            FROM predictions
            WHERE game_date = ? AND tier IN ({placeholders})
            ORDER BY commence_time ASC NULLS LAST, vortex_score DESC
        """, (today, *tier_filter)).fetchall()
    else:
        rows = conn.execute("""
            SELECT player_name, sport, stat_type, line, side, tier,
                   ev_percentage, vortex_score, result, actual_value, best_book,
                   commence_time, pitcher_name
            FROM predictions
            WHERE game_date = ?
            ORDER BY commence_time ASC NULLS LAST, vortex_score DESC
        """, (today,)).fetchall()

    def _local_game_date(ct: str | None) -> str | None:
        if not ct:
            return None
        try:
            return _dt.fromisoformat(ct.replace("Z", "+00:00")).astimezone(
                _tz(_td(hours=-7))
            ).date().isoformat()
        except Exception:
            return None

    rows = [r for r in rows if (_local_game_date(r["commence_time"]) or today) == today]
    conn.close()

    result_emoji = {"hit": "✅", "miss": "❌", "push": "➡️", "void": "⚪"}
    sport_emoji  = {"MLB": "⚾", "NBA": "🏀"}

    def _game_status(r) -> str:
        if r["result"] is not None:
            return result_emoji.get(r["result"], "⏳")
        ct = r["commence_time"]
        if ct:
            try:
                game_start = _dt.fromisoformat(ct.replace("Z", "+00:00")).astimezone(_ET)
                if now_et >= game_start:
                    return "🔴"
            except Exception:
                pass
        return "⏳"

    def _unix(iso: str):
        try:
            return int(_dt.fromisoformat((iso or "").replace("Z", "+00:00")).timestamp())
        except Exception:
            return None

    matchup_by_pitcher: dict = {}
    matchup_by_time: dict = {}
    try:
        loop  = asyncio.get_event_loop()
        sched = await loop.run_in_executor(None, stats_mlb.get_todays_schedule, today)
        for g in (sched or {}).values():
            away = (g.get("away_abbr") or "").strip()
            home = (g.get("home_abbr") or "").strip()
            if not (away and home):
                continue
            info = {"label": f"{away} @ {home}", "utc": g.get("game_utc", "") or ""}
            for pn in (g.get("home_pitcher"), g.get("away_pitcher")):
                if pn:
                    matchup_by_pitcher[pn.lower()] = info
            u = _unix(info["utc"])
            if u is not None:
                matchup_by_time.setdefault(u, []).append(info)
    except Exception:
        pass

    def _row_game(r):
        pn = (r["pitcher_name"] or "").lower()
        if pn and pn in matchup_by_pitcher:
            return matchup_by_pitcher[pn]
        u = _unix(r["commence_time"] or "")
        if u is not None:
            g = matchup_by_time.get(u)
            if g and len(g) == 1:
                return g[0]
        return None

    TIER_RANK = {"ELITE": 0, "STRONG": 1, "GOOD": 2, "LEAN": 3, "RISKY": 4, "FADE": 5, "PASS": 6}
    groups: dict = {}
    for r in rows:
        ct  = r["commence_time"] or ""
        u   = _unix(ct)
        g   = _row_game(r)
        if g and g.get("label"):
            key, label = g["label"], g["label"]
            sortk = _unix(g.get("utc")) if _unix(g.get("utc")) is not None else (u if u is not None else 9_999_999_999)
        else:
            key, label = f"_t_{ct}", None
            sortk = u if u is not None else 9_999_999_999
        grp = groups.get(key)
        if grp is None:
            grp = {"label": label, "ct": ct, "sort": sortk, "rows": []}
            groups[key] = grp
        grp["rows"].append(r)

    def _row_sort(r):
        sc = r["vortex_score"] if r["vortex_score"] is not None else -999
        return (TIER_RANK.get(r["tier"] or "", 9), -sc)

    lines = []
    hits = misses = pending = voids = 0
    first = True

    for grp in sorted(groups.values(), key=lambda gg: gg["sort"]):
        if not first:
            lines.append("")
        first = False
        u = _unix(grp["ct"])
        time_lbl = f"<t:{u}:t>" if u is not None else ""
        if grp["label"] and time_lbl:
            lines.append(f"**{grp['label']}** · {time_lbl}")
        elif grp["label"]:
            lines.append(f"**{grp['label']}**")
        elif time_lbl:
            lines.append(f"**{time_lbl}**")

        for r in sorted(grp["rows"], key=_row_sort):
            sw     = "O" if r["side"] == "over" else "U"
            status = _game_status(r)
            se     = sport_emoji.get(r["sport"], "🎯")
            te     = TIER_EMOJI.get(r["tier"] or "", "")
            actual = f" → **{r['actual_value']}**" if r["actual_value"] is not None else ""
            lines.append(f"{status} {se} {te} **{r['player_name']}** {sw}{r['line']} {r['stat_type']}{actual}")
            res = r["result"]
            if   res == "hit":  hits += 1
            elif res == "miss": misses += 1
            elif res == "void": voids += 1
            elif res == "push": pass
            else:               pending += 1

    graded     = hits + misses
    record_str = (f"**{hits}-{misses}**"
                  + (f" ({hits/graded*100:.0f}%)" if graded else "")
                  + f" · {pending} pending"
                  + (f" · {voids} void" if voids else ""))

    if tier_filter:
        filter_label = "+".join(tier_filter)
        record_str += f"  ·  Filter: {filter_label}"

    _is_today = (today == vortextime.vortex_day())
    _title    = "🎯 Today's Record" if _is_today else "🎯 Record"
    embed = discord.Embed(
        title=f"{_title} — {_iso_to_us(today)}",
        description=record_str,
        color=0x00D4FF if not graded else (0x57F287 if hits >= misses else 0xED4245),
    )

    chunk_lines: list[str] = []
    chunk_chars = 0
    field_n = 1
    for line in lines:
        addition = len(line) + 1
        if chunk_chars + addition > 1020 and chunk_lines:
            embed.add_field(name=f"Picks {field_n}" if field_n > 1 else "Picks",
                            value="\n".join(chunk_lines), inline=False)
            field_n += 1
            chunk_lines = []
            chunk_chars = 0
        chunk_lines.append(line)
        chunk_chars += addition
    if chunk_lines:
        embed.add_field(name=f"Picks {field_n}" if field_n > 1 else "Picks",
                        value="\n".join(chunk_lines), inline=False)

    embed.set_footer(text="VORTEX · 🔴 = In-progress  ⏳ = Not started  ✅/❌ = Final  ⚪ = Void (DNP)")
    return embed


# ── /record ────────────────────────────────────────────────────────────────────
@tree.command(name="record", description="🎯 Picks with live result status (default: today)")
@app_commands.describe(date="Optional date like 6/16/2026 or 6/16, or 'yesterday'. Blank = today.")
async def cmd_record(interaction: discord.Interaction, date: str | None = None):
    if not await _is_admin(interaction):
        await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)

    if date:
        today = _parse_user_date(date)
        if today is None:
            await interaction.followup.send(
                f"❌ Couldn't read `{date}`. Try month/day/year like `6/16/2026`, "
                f"just `6/16`, or `yesterday`.",
                ephemeral=True)
            return
    else:
        today = vortextime.vortex_day()

    # Quick check if any picks exist for this date before building full embed
    conn = _db()
    has = conn.execute("SELECT 1 FROM predictions WHERE game_date=? LIMIT 1", (today,)).fetchone()
    conn.close()
    if not has:
        recent = conn.execute("""
            SELECT game_date, COUNT(*) n
            FROM predictions
            WHERE game_date <= ?
            GROUP BY game_date
            ORDER BY game_date DESC
            LIMIT 5
        """, (today,)).fetchall()
        if recent:
            opts = "\n".join(
                f"• `{_iso_to_us(r['game_date'])}` — {r['n']} picks" for r in recent)
            await interaction.followup.send(
                f"No picks logged for **{_iso_to_us(today)}**.\n\n"
                f"📅 Days with picks (use `/record date:6/16/2026`):\n{opts}",
                ephemeral=True)
        else:
            await interaction.followup.send(
                "No predictions logged yet — run the engine first.", ephemeral=True)
        return

    embed = await _build_record_embed(today)
    await interaction.followup.send(embed=embed, view=RecordFilterView(today), ephemeral=True)


@tree.command(name="cleanup", description="🧹 Remove non-ELITE/STRONG predictions from accuracy report")
async def cmd_cleanup(interaction: discord.Interaction):
    if not await _is_admin(interaction):
        await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        return
    conn = _db()

    before = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
    to_delete = conn.execute(
        "SELECT tier, COUNT(*) as cnt, "
        "SUM(CASE WHEN result=1 THEN 1 ELSE 0 END) as wins, "
        "SUM(CASE WHEN result=0 THEN 1 ELSE 0 END) as losses "
        "FROM predictions WHERE tier NOT IN ('ELITE','STRONG') GROUP BY tier"
    ).fetchall()
    if not to_delete:
        conn.close()
        await interaction.response.send_message("✅ Nothing to clean — all predictions are ELITE/STRONG.", ephemeral=True)
        return

    breakdown = "\n".join(f"• **{r[0]}** — {r[1]} predictions ({r[2] or 0}W / {r[3] or 0}L)" for r in to_delete)
    total_del = sum(r[1] for r in to_delete)
    wins_del = sum(r[2] or 0 for r in to_delete)
    losses_del = sum(r[3] or 0 for r in to_delete)

    conn.execute("DELETE FROM predictions WHERE tier NOT IN ('ELITE','STRONG')")
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
    wins = conn.execute("SELECT SUM(CASE WHEN result=1 THEN 1 ELSE 0 END) FROM predictions").fetchone()[0] or 0
    conn.close()
    pct = round(wins / after * 100, 1) if after else 0
    await interaction.response.send_message(
        f"🧹 **Removed {total_del} non-ELITE/STRONG predictions:**\n{breakdown}\n\n"
        f"**After cleanup:** {after} graded · {wins}/{after} ({pct}%)",
        ephemeral=True)


@tree.command(name="maintenance", description="🔧 Toggle maintenance mode — blocks all non-admin commands")
async def cmd_maintenance(interaction: discord.Interaction):
    if not await _is_admin(interaction):
        await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        return
    global MAINTENANCE_MODE
    MAINTENANCE_MODE = not MAINTENANCE_MODE
    state = "ENABLED" if MAINTENANCE_MODE else "DISABLED"
    emoji = "🔧" if MAINTENANCE_MODE else "✅"
    await interaction.response.send_message(
        f"{emoji} **Maintenance mode {state}.**\n"
        f"{'All non-admin commands are now blocked.' if MAINTENANCE_MODE else 'All commands are back online.'}",
        ephemeral=True,
    )


# ── entry ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not TOKEN:
        print("ERROR: DISCORD_TOKEN not set in .env")
        sys.exit(1)
    bot.run(TOKEN)
