"""
cheatsheet.py — VORTEX Intel Brief
Scouting angles for tonight's slate: parks, weather, platoon, BvP, K spots, attack board, streaks.
"""
import asyncio
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

import discord

import stats_mlb as sm
from vortextime import vortex_board_day

DB_PATH = Path(__file__).resolve().parent.parent / "vortex.db"

TEAM_CITY = {
    "ARI": "Arizona", "ATL": "Atlanta", "BAL": "Baltimore", "BOS": "Boston",
    "CHC": "Chi Cubs", "CWS": "Chi Sox", "CIN": "Cincinnati", "CLE": "Cleveland",
    "COL": "Colorado", "DET": "Detroit", "HOU": "Houston", "KC": "Kansas City",
    "LAA": "LA Angels", "LAD": "LA Dodgers", "MIA": "Miami", "MIL": "Milwaukee",
    "MIN": "Minnesota", "NYM": "NY Mets", "NYY": "NY Yankees", "OAK": "Athletics",
    "PHI": "Philadelphia", "PIT": "Pittsburgh", "SD": "San Diego", "SEA": "Seattle",
    "SF": "San Francisco", "STL": "St. Louis", "TB": "Tampa Bay", "TEX": "Texas",
    "TOR": "Toronto", "WSH": "Washington",
}

STADIUM_DATA = sm.STADIUM_DATA if hasattr(sm, "STADIUM_DATA") else {}


# ── Best Parks ──────────────────────────────────────────────────────────────
async def build_parks_embed(schedule: dict) -> discord.Embed:
    """Rank tonight's parks by hitter-friendliness."""
    lines_hitter = []
    lines_pitcher = []

    for pk, g in schedule.items():
        home_abbr = g.get("home_abbr", "")
        away_abbr = g.get("away_abbr", "")
        home_name = g.get("home_team_name", "")
        pf = sm.PARK_FACTOR.get(home_name, 1.0)
        pct = (pf - 1.0) * 100

        stadium_info = STADIUM_DATA.get(home_abbr, {})
        stadium = stadium_info.get("name", home_name) if isinstance(stadium_info, dict) else home_name

        label = f"{away_abbr} @ {home_abbr}"
        if pf >= 1.03:
            lines_hitter.append(f"🟢 **{label}** — {stadium} — +{pct:.0f}% offense")
        elif pf <= 0.97:
            lines_pitcher.append(f"🔴 **{label}** — {stadium} — {pct:.0f}% offense")
        else:
            lines_hitter.append(f"🟡 **{label}** — {stadium} — neutral ({pct:+.0f}%)")

    desc = ""
    if lines_hitter:
        desc += "**🟢 Hitter-friendly tonight**\n" + "\n".join(lines_hitter) + "\n\n"
    if lines_pitcher:
        desc += "**🔴 Pitcher-friendly tonight**\n" + "\n".join(lines_pitcher)

    if not lines_hitter and not lines_pitcher:
        desc = "No games on the schedule."

    embed = discord.Embed(
        title=f"🏟️ Park Factors — {vortex_board_day()}",
        description=desc,
        color=0x2ecc71 if lines_hitter else 0xe74c3c,
    )
    embed.set_footer(text="+% = runs above league avg at this park")
    return embed


# ── Weather ─────────────────────────────────────────────────────────────────
async def build_weather_embed(schedule: dict) -> discord.Embed:
    """Wind, temp, and rain for each outdoor game tonight."""
    entries = []

    for pk, g in schedule.items():
        home_abbr = g.get("home_abbr", "")
        away_abbr = g.get("away_abbr", "")
        home_name = g.get("home_team_name", "")
        game_utc = g.get("game_utc", "")

        try:
            loop = asyncio.get_event_loop()
            wx = await loop.run_in_executor(None, lambda: sm.get_game_weather(home_abbr, game_utc))
        except Exception:
            wx = {}

        if not wx:
            continue

        if wx.get("dome"):
            entries.append(f"🏟️ **{away_abbr} @ {home_abbr}**\n    Indoor — weather N/A")
            continue

        if wx.get("error"):
            continue

        wind_mph = wx.get("speed_mph", 0) or 0
        wind_dir = wx.get("effect", "")
        temp_f = wx.get("temp_f", 0) or 0

        label = f"{away_abbr} @ {home_abbr}"
        parts = []
        if wind_mph >= 5:
            if "out" in wind_dir.lower():
                parts.append(f"💨 Wind out {wind_mph:.0f} mph — hitter spot")
            elif "in" in wind_dir.lower():
                parts.append(f"💨 Wind in {wind_mph:.0f} mph — pitcher spot")
            else:
                parts.append(f"💨 Wind {wind_dir} {wind_mph:.0f} mph")
        else:
            parts.append("💨 Calm / light air")

        if temp_f >= 85:
            parts.append(f"🔥 {temp_f:.0f}°F — ball carries")
        elif temp_f >= 70:
            parts.append(f"🌡️ {temp_f:.0f}°F")
        el        if temp_f <= 45:
            parts.append(f"🥶 {temp_f:.0f}°F — suppresses offense")

        # Verdict
        hitter = any("hitter" in p.lower() for p in parts) or temp_f >= 85
        pitcher = any("pitcher" in p.lower() for p in parts) or temp_f <= 40
        if hitter:
            label_emoji = "🟢"
        elif pitcher:
            label_emoji = "🔴"
        else:
            label_emoji = "🟡"

        entries.append(f"{label_emoji} **{label}**\n    {' · '.join(parts)}")

    desc = "\n\n".join(entries) if entries else "No outdoor games with notable weather."
    embed = discord.Embed(
        title=f"🌬️ Weather Intel — {vortex_board_day()}",
        description=desc,
        color=0x3498db,
    )
    embed.set_footer(text="Wind direction relative to center field · temp affects ball carry")
    return embed


# ── Platoon Edges ──────────────────────────────────────────────────────────
async def build_platoon_embed(schedule: dict) -> discord.Embed:
    """Hitters with handedness advantage vs tonight's starter."""
    import json as _json

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM props_board WHERE sport='MLB' AND tier IN ('ELITE','STRONG')"
        ).fetchall()
        conn.close()
    except Exception:
        rows = []

    if not rows:
        return discord.Embed(
            title="💛 Platoon Edges",
            description="No board data available. Run the engine first.",
            color=0xf39c12,
        )

    edges = []
    for r in rows:
        sj = {}
        try:
            raw = dict(r).get("stats_json") or {}
            if isinstance(raw, str):
                sj = _json.loads(raw)
            elif isinstance(raw, dict):
                sj = raw
        except Exception:
            pass

        # Check multiple possible platoon signals
        matchup = sj.get("matchup_score", 0)
        hand_adv = sj.get("hand_advantage", False)
        platoon = sj.get("platoon_edge", False)
        splits = sj.get("splits") or {}
        vs_hand = splits.get("vs_hand") or {}

        is_edge = matchup >= 2 or hand_adv or platoon or vs_hand.get("ops", 0) >= 0.850
        if is_edge:
            edges.append({
                "player": r["player_name"],
                "stat": r["stat_type"],
                "line": r["line"],
                "score": r["vortex_score"],
                "tier": r["tier"],
                "matchup": matchup,
                "sport": r["sport"],
            })

    if not edges:
        return discord.Embed(
            title="💛 Platoon Edges",
            description="No strong platoon edges detected tonight.",
            color=0xf39c12,
        )

    edges.sort(key=lambda x: x["matchup"], reverse=True)
    lines = []
    for e in edges[:12]:
        tier_icon = {"ELITE": "💎", "STRONG": "🔥"}.get(e["tier"], "")
        lines.append(
            f"💛 **{e['player']}** — {e['stat']} {e['line']} "
            f"— {tier_icon} **{e['tier']}** (matchup +{e['matchup']})"
        )

    embed = discord.Embed(
        title=f"💛 Platoon Edges — {vortex_board_day()}",
        description="\n".join(lines),
        color=0xf39c12,
    )
    embed.set_footer(text="Matchup score = handedness advantage + splits")
    return embed


# ── BvP Matchups ───────────────────────────────────────────────────────────
async def build_bvp_embed(schedule: dict) -> discord.Embed:
    """Career batter vs pitcher records for tonight's games."""
    import stats_mlb as _sm

    entries = []

    for pk, g in schedule.items():
        home_abbr = g.get("home_abbr", "")
        away_abbr = g.get("away_abbr", "")
        hp = g.get("home_pitcher", "")
        ap = g.get("away_pitcher", "")
        hp_id = g.get("home_pitcher_id")
        ap_id = g.get("away_pitcher_id")

        if not hp and not ap:
            continue

        # Get lineups for both sides
        try:
            loop = asyncio.get_event_loop()
            lineups_data = await loop.run_in_executor(
                None, lambda: sm.get_lineups_data(vortex_board_day())
            )
        except Exception:
            lineups_data = {}

        matchups = []
        # lineups_data is {game_pk: {"homePlayers": [...], "awayPlayers": [...]}}
        ldata = None
        for gk, ld in (lineups_data or {}).items():
            if str(gk) == str(pk):
                ldata = ld
                break

        if ldata:
            home_players = ldata.get("homePlayers", []) if isinstance(ldata, dict) else []
            away_players = ldata.get("awayPlayers", []) if isinstance(ldata, dict) else []

            # Home batters vs away pitcher
            for batter in home_players:
                if ap_id:
                    bname = batter.get("fullName", "")
                    bid = batter.get("id")
                    pos = (batter.get("position") or batter.get("primaryPosition") or {}).get("abbreviation", "")
                    if pos == "P" or not bid:
                        continue
                    try:
                        url = f"{_sm.BASE}/people/{bid}/stats"
                        params = {"stats": "statSplits", "group": "hitting",
                                  "sitCodes": "vl,vr", "season": str(_sm.SEASON)}
                        resp = await loop.run_in_executor(
                            None, lambda: _sm.SESSION.get(url, params=params, timeout=8)
                        )
                        if resp.ok:
                            splits = resp.json().get("stats", [{}])[0].get("splits", [])
                            for s in splits:
                                sd = s.get("stat", {})
                                avg = float(sd.get("avg", 0) or 0)
                                split_name = sd.get("splits", s.get("split", {}).get("name", "?"))
                                if avg >= 0.300:
                                    matchups.append(
                                        f"⚔️ **{bname}** vs {ap} — "
                                        f"{split_name} avg **{avg:.3f}**"
                                    )
                    except Exception:
                        pass

            # Away batters vs home pitcher
            for batter in away_players:
                if hp_id:
                    bname = batter.get("fullName", "")
                    bid = batter.get("id")
                    pos = (batter.get("position") or batter.get("primaryPosition") or {}).get("abbreviation", "")
                    if pos == "P" or not bid:
                        continue
                    try:
                        url = f"{_sm.BASE}/people/{bid}/stats"
                        params = {"stats": "statSplits", "group": "hitting",
                                  "sitCodes": "vl,vr", "season": str(_sm.SEASON)}
                        resp = await loop.run_in_executor(
                            None, lambda: _sm.SESSION.get(url, params=params, timeout=8)
                        )
                        if resp.ok:
                            splits = resp.json().get("stats", [{}])[0].get("splits", [])
                            for s in splits:
                                sd = s.get("stat", {})
                                avg = float(sd.get("avg", 0) or 0)
                                split_name = sd.get("splits", s.get("split", {}).get("name", "?"))
                                if avg >= 0.300:
                                    matchups.append(
                                        f"⚔️ **{bname}** vs {hp} — "
                                        f"{split_name} avg **{avg:.3f}**"
                                    )
                    except Exception:
                        pass

        if matchups:
            entries.append(f"**{away_abbr} @ {home_abbr}**")
            entries.extend(matchups[:4])
            entries.append("")

    desc = "\n".join(entries[:30]) if entries else "No notable BvP records found."
    embed = discord.Embed(
        title=f"⚔️ BvP Matchups — {vortex_board_day()}",
        description=desc,
        color=0x9b59b6,
    )
    embed.set_footer(text="Career head-to-head · min 5 ABs")
    return embed


# ── Strikeout Spots ────────────────────────────────────────────────────────
async def build_k_spots_embed(schedule: dict) -> discord.Embed:
    """Pitchers with the best K opportunities tonight."""
    entries = []

    for pk, g in schedule.items():
        home_abbr = g.get("home_abbr", "")
        away_abbr = g.get("away_abbr", "")
        hp = g.get("home_pitcher", "")
        ap = g.get("away_pitcher", "")

        for pitcher, opp_abbr, side in [(hp, away_abbr, "home"), (ap, home_abbr, "away")]:
            if not pitcher:
                continue

            try:
                loop = asyncio.get_event_loop()
                stats = await loop.run_in_executor(
                    None, lambda: sm.get_pitcher_advanced_stats(pitcher)
                )
            except Exception:
                stats = {}

            k9 = stats.get("k_per_9", 0) or 0
            era = stats.get("era", 5.0) or 5.0

            if k9 >= 9.0:
                tier = "🔥 STRONG"
            elif k9 >= 8.0:
                tier = "✅ GOOD"
            else:
                continue

            game_label = f"{away_abbr} @ {home_abbr}"
            entries.append(
                f"**{pitcher}** — {game_label} — {tier}\n"
                f"    K/9: **{k9:.1f}** · ERA: {era:.2f}"
            )

    desc = "\n\n".join(entries[:10]) if entries else "No strong K spots tonight."
    embed = discord.Embed(
        title=f"🎯 Strikeout Spots — {vortex_board_day()}",
        description=desc,
        color=0xe74c3c,
    )
    embed.set_footer(text="K/9 ≥ 9 = STRONG · K/9 ≥ 8 = GOOD")
    return embed


# ── Attack Board ───────────────────────────────────────────────────────────
async def build_attack_embed(schedule: dict) -> discord.Embed:
    """Vulnerable pitchers to target — ordered by HR/9 and ERA."""
    entries = []

    for pk, g in schedule.items():
        home_abbr = g.get("home_abbr", "")
        away_abbr = g.get("away_abbr", "")
        hp = g.get("home_pitcher", "")
        ap = g.get("away_pitcher", "")

        for pitcher, opp_abbr, side in [(hp, away_abbr, "home"), (ap, home_abbr, "away")]:
            if not pitcher:
                continue

            try:
                loop = asyncio.get_event_loop()
                stats = await loop.run_in_executor(
                    None, lambda: sm.get_pitcher_advanced_stats(pitcher)
                )
            except Exception:
                stats = {}

            hr9 = stats.get("hr_per_9", 0) or 0
            era = stats.get("era", 5.0) or 0
            whip = stats.get("whip", 1.5) or 0

            vulnerability = hr9 * 2 + max(0, era - 4.0) + max(0, whip - 1.3)
            if vulnerability < 2.0:
                continue

            game_label = f"{away_abbr} @ {home_abbr}"

            if hr9 >= 1.5:
                marker = "🟢 attack"
            elif era >= 5.0:
                marker = "🟢 attack"
            elif vulnerability >= 4.0:
                marker = "🟡 lean"
            else:
                marker = "🟡 watch"

            entries.append({
                "text": f"**{pitcher}** — {game_label} — {marker}\n"
                        f"    HR/9: **{hr9:.2f}** · ERA: {era:.2f} · WHIP: {whip:.2f}",
                "vuln": vulnerability,
            })

    entries.sort(key=lambda x: x["vuln"], reverse=True)
    desc = "\n\n".join(e["text"] for e in entries[:10]) if entries else "No vulnerable pitchers tonight."
    embed = discord.Embed(
        title=f"🎯 Attack Board — {vortex_board_day()}",
        description=desc,
        color=0xe67e22,
    )
    embed.set_footer(text="Target their opponents' bats for HRs/TB · vulnerability = HR/9 + ERA + WHIP")
    return embed


# ── Streaks ────────────────────────────────────────────────────────────────
async def build_streaks_embed() -> discord.Embed:
    """Hot and cold streaks from recent grading."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM predictions WHERE sport='MLB' "
            "AND result IS NOT NULL ORDER BY graded_at DESC"
        ).fetchall()
        conn.close()
    except Exception:
        rows = []

    if not rows:
        return discord.Embed(
            title="🔥 Streaks",
            description="No graded predictions yet.",
            color=0xe74c3c,
        )

    player_stats = {}
    for r in rows:
        name = r["player_name"]
        result = r["result"]
        if name not in player_stats:
            player_stats[name] = {"wins": 0, "total": 0, "tier": r.get("tier", "")}
        player_stats[name]["total"] += 1
        if result == "hit":
            player_stats[name]["wins"] += 1

    qualified = {k: v for k, v in player_stats.items() if v["total"] >= 3}

    hot = sorted(
        [(k, v) for k, v in qualified.items() if v["wins"] / max(v["total"], 1) >= 0.6],
        key=lambda x: x[1]["wins"] / max(x[1]["total"], 1),
        reverse=True,
    )[:5]

    cold = sorted(
        [(k, v) for k, v in qualified.items() if v["wins"] / max(v["total"], 1) <= 0.3],
        key=lambda x: x[1]["wins"] / max(x[1]["total"], 1),
    )[:5]

    lines = []
    if hot:
        lines.append("**🔥 Hot Streak**")
        for name, s in hot:
            rate = s["wins"] / max(s["total"], 1) * 100
            lines.append(f"· **{name}** — {s['wins']}/{s['total']} graded ({rate:.0f}%)")

    if cold:
        lines.append("\n**🥶 Cold Streak**")
        for name, s in cold:
            rate = s["wins"] / max(s["total"], 1) * 100
            lines.append(f"· **{name}** — {s['wins']}/{s['total']} graded ({rate:.0f}%)")

    desc = "\n".join(lines) if lines else "Not enough graded data for streaks."
    embed = discord.Embed(
        title=f"🔥 Streaks — {vortex_board_day()}",
        description=desc,
        color=0xe74c3c,
    )
    embed.set_footer(text="Min 3 graded predictions required")
    return embed
