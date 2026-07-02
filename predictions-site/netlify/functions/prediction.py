"""
Vortex — live prediction API (Netlify Function)
=================================================
Computes a real /prediction-style breakdown for an arbitrary
player/stat/line/side on demand — no database, no odds API.

Reuses the same MLB Stats API wrappers (backend/stats_mlb.py,
backend/research.py) and scoring engine (backend/analyze.py
grade_pick_both) as the Discord bot, so scores/tiers are identical.
The narrative text below is a purpose-built formatter for this
endpoint (NOT extracted from the ~1500-line Discord embed builder,
to avoid risking any change to the live bot's behavior) closely
modeled on the bot's wording.

Local dev: run with `netlify dev` from predictions-site/, or hit
this file directly via `python netlify/functions/prediction.py`
for a quick smoke test (see bottom of file).
"""

import json
import sys
from pathlib import Path

# backend/ lives two directories up from this file (predictions-site/netlify/functions/).
# Netlify functions run in an isolated bundle, so make sure it's importable.
_BACKEND_DIR = Path(__file__).resolve().parents[3] / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))
if str(_BACKEND_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR.parent))

import analyze          # backend/analyze.py — grade_pick_both, compute_hit_rates, PROP_STAT_MAP helpers
import stats_mlb         # backend/stats_mlb.py
import research as vortex_research  # backend/research.py — fuzzy_search

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Content-Type": "application/json",
}

# Maps the human-readable stat labels the website shows to the internal
# prop_type keys backend/stats_mlb.py and backend/analyze.py expect.
STAT_LABEL_TO_PROP_TYPE = {
    "Hits": "hits",
    "Total Bases": "total_bases",
    "Home Runs": "home_runs",
    "RBIs": "rbis",
    "Runs Scored": "runs_scored",
    "Strikeouts": "strikeouts",
    "Walks": "walks",
    "Hits+Runs+RBIs": "hits_runs_rbis",
}


def _response(status, body):
    return {"statusCode": status, "headers": CORS_HEADERS, "body": json.dumps(body)}


def handler(event, context):
    if (event.get("httpMethod") or "GET").upper() == "OPTIONS":
        return _response(200, {})

    params = event.get("queryStringParameters") or {}
    player_name = (params.get("player") or "").strip()
    stat_label = (params.get("stat") or "").strip()
    side = (params.get("side") or "").strip().lower()
    line_raw = params.get("line")

    if not player_name or not stat_label or not side or line_raw is None:
        return _response(400, {"error": "Missing required params: player, stat, line, side"})

    try:
        line = float(line_raw)
    except (TypeError, ValueError):
        return _response(400, {"error": f"Invalid line value: {line_raw!r}"})

    if side not in ("over", "under"):
        return _response(400, {"error": "side must be 'over' or 'under'"})

    prop_type = STAT_LABEL_TO_PROP_TYPE.get(stat_label)
    if not prop_type:
        return _response(400, {"error": f"Unknown stat: {stat_label!r}"})

    try:
        result = compute_prediction(player_name, prop_type, stat_label, line, side)
    except PlayerNotFound as exc:
        return _response(404, {"error": str(exc)})
    except NoGameFound as exc:
        return _response(404, {"error": str(exc)})
    except Exception as exc:  # noqa: BLE001 — never leak a stack trace to the client
        return _response(500, {"error": f"Live lookup failed: {exc}"})

    return _response(200, result)


class PlayerNotFound(Exception):
    pass


class NoGameFound(Exception):
    pass


def compute_prediction(player_name: str, prop_type: str, stat_label: str, line: float, side: str) -> dict:
    matches = vortex_research.fuzzy_search(player_name)
    if not matches:
        raise PlayerNotFound(f"Couldn't find an MLB player matching \"{player_name}\".")
    found = matches[0]
    player_id = found["id"]
    canonical_name = found.get("name", player_name)
    team_abbr = found.get("team", "")

    matchup = analyze.get_matchup_info(player_id)
    if not matchup:
        reason = analyze.get_no_game_reason(player_id)
        raise NoGameFound(reason or f"No upcoming game found for {canonical_name}.")

    splits = analyze.compute_hit_rates(player_id, line, prop_type)
    if splits.get("error"):
        raise NoGameFound(splits["error"])

    pitcher_name = matchup.get("pitcher") or ""
    pitcher = stats_mlb.get_pitcher_metrics(pitcher_name) if pitcher_name else {}
    if pitcher.get("error"):
        pitcher = {}

    bvp = None
    if pitcher.get("pitcher_id"):
        bvp_raw = stats_mlb.get_bvp_history(player_id, pitcher["pitcher_id"])
        if not bvp_raw.get("error"):
            bvp = bvp_raw

    hand_splits = stats_mlb.get_batter_hand_splits(player_id)

    is_home = bool(matchup.get("is_home"))
    # PARK_FACTOR is keyed by the HOME team's name — that's the batter's own
    # team when they're home, otherwise the opponent (whose park it is tonight).
    home_team_name = (found.get("team") or "") if is_home else (matchup.get("opponent") or "")
    park_factor = stats_mlb.PARK_FACTOR.get(home_team_name, 1.0)

    statcast = stats_mlb.get_statcast_by_id(player_id) or {}

    arsenal = []
    if pitcher.get("pitcher_id"):
        try:
            arsenal = stats_mlb.get_pitcher_arsenal(pitcher["pitcher_id"]) or []
        except Exception:
            arsenal = []

    opp_team_id = matchup.get("opp_team_id")
    vs_team = stats_mlb.get_vs_team_splits(player_id, opp_team_id, line, prop_type) if opp_team_id else {}

    weather = {}
    # found["team"]/team_abbr is actually the full team NAME (fuzzy_search's
    # "team" field), not an abbreviation -- get_game_weather needs the abbr
    # either way, so always resolve it through the lookup table.
    home_abbr = stats_mlb._MLB_TEAM_ABBR.get(home_team_name, "")
    if home_abbr:
        try:
            weather = stats_mlb.get_game_weather(home_abbr, matchup.get("game_utc", "")) or {}
        except Exception:
            weather = {}

    # Remaining signals the bot's _run_analyze() feeds into grade_pick() —
    # fetched here too so live-site scores match the Discord bot exactly,
    # not just approximate it.
    bat_vs_pitch = []
    if pitcher.get("pitcher_id"):
        try:
            bat_vs_pitch = stats_mlb.get_batter_vs_pitch_type(player_id, pitcher["pitcher_id"]) or []
        except Exception:
            bat_vs_pitch = []

    team_bvp = {}
    if opp_team_id:
        try:
            team_bvp = stats_mlb.get_team_bvp(player_id, opp_team_id) or {}
        except Exception:
            team_bvp = {}

    oaa = {}
    if opp_team_id:
        try:
            oaa = stats_mlb.get_team_defense_oaa(opp_team_id) or {}
        except Exception:
            oaa = {}

    opp_k_rank, opp_k_pct = None, None
    if opp_team_id:
        try:
            k_rates = stats_mlb.get_all_teams_k_rate() or {}
            opp_k = k_rates.get(opp_team_id) or k_rates.get(str(opp_team_id))
            if opp_k:
                opp_k_rank = opp_k.get("rank")
                # get_all_teams_k_rate returns k_pct as a percentage (e.g. 22.7);
                # grade_pick() expects a 0.0-1.0 fraction.
                raw_k_pct = opp_k.get("k_pct")
                opp_k_pct = (raw_k_pct / 100) if raw_k_pct is not None else None
        except Exception:
            pass

    lineup_spot = None
    try:
        lineup_spot = stats_mlb.get_lineup_position(player_id)
    except Exception:
        pass

    umpire = {}
    home_team_id = matchup.get("home_team_id")
    if home_team_id:
        try:
            umpire = stats_mlb.get_game_umpire(home_team_id) or {}
        except Exception:
            umpire = {}

    grade = analyze.grade_pick_both(
        splits=splits,
        line=line,
        opp_k_rank=opp_k_rank,
        opp_k_pct=opp_k_pct,
        pitcher=pitcher or None,
        bvp=bvp,
        park_factor=park_factor,
        weather=weather or None,
        team_bvp=team_bvp or None,
        oaa=oaa or None,
        prop_type=prop_type,
        lineup_spot=lineup_spot,
        statcast=statcast or None,
        team_h2h=vs_team or None,
        arsenal=arsenal or None,
        bat_vs_pitch=bat_vs_pitch or None,
        vs_hand_splits=hand_splits or None,
        umpire=umpire or None,
    )

    picked_grade = grade["over_grade"] if side == "over" else grade["under_grade"]
    picked_score = grade["over_score"] if side == "over" else grade["under_score"]

    return format_response(
        player_name=canonical_name,
        team_abbr=team_abbr,
        headshot=f"https://img.mlbstatic.com/mlb-photos/image/upload/w_180,q_auto:best/v1/people/{player_id}/headshot/67/current",
        stat_label=stat_label,
        prop_type=prop_type,
        line=line,
        side=side,
        splits=splits,
        matchup=matchup,
        pitcher=pitcher,
        bvp=bvp,
        hand_splits=hand_splits,
        park_factor=park_factor,
        statcast=statcast,
        arsenal=arsenal,
        vs_team=vs_team,
        team_bvp=team_bvp,
        weather=weather,
        grade=grade,
        picked_grade=picked_grade,
        picked_score=picked_score,
    )


def format_response(*, player_name, team_abbr, headshot, stat_label, prop_type, line, side, splits,
                     matchup, pitcher, bvp, hand_splits, park_factor, statcast, arsenal, vs_team, team_bvp, weather,
                     grade, picked_grade, picked_score) -> dict:
    # stats_mlb's l5/l10/l20 blocks always report the OVER side (hits = games
    # where value >= line). Flip hits/rate for an Under lookup so the display
    # actually reflects the side being shown, not always the Over numbers.
    is_under = side == "under"

    def _for_side(block):
        block = block or {}
        games = block.get("games") or 0
        over_hits = block.get("hits") or 0
        if not games:
            return {"games": 0, "hits": 0, "rate": None, "avg": block.get("avg")}
        hits = (games - over_hits) if is_under else over_hits
        return {"games": games, "hits": hits, "rate": round(hits / games * 100), "avg": block.get("avg")}

    l5 = _for_side(splits.get("l5"))
    l10 = _for_side(splits.get("l10"))
    l20 = _for_side(splits.get("l20"))

    is_home = bool(matchup.get("is_home"))
    location = "🏠 Home" if is_home else "✈️ Away"
    opponent = matchup.get("opponent", "")

    l10_rate = l10["rate"] or 0
    l10_avg = l10.get("avg") or 0
    edge = round((line - l10_avg) if is_under else (l10_avg - line), 2)

    why_it_hits = []
    if l10.get("games"):
        why_it_hits.append(
            f"Has hit {side.title()} {line} in "
            f"{l10['hits']}/{l10['games']} of the last 10 games "
            f"({l10_rate}%)."
        )
    if l5.get("rate") is not None:
        why_it_hits.append(f"L5: {l5['hits']}/{l5['games']} ({l5['rate']}%).")
    if l20.get("rate") is not None:
        why_it_hits.append(f"L20: {l20['hits']}/{l20['games']} ({l20['rate']}%).")
    if pitcher.get("era"):
        why_it_hits.append(
            f"Facing {pitcher.get('name', matchup.get('pitcher', ''))} "
            f"({pitcher.get('hand', '?')}HP) — {pitcher.get('era')} ERA · "
            f"{pitcher.get('hr_per_9')} HR/9."
        )

    # xSLG/xwOBA contact quality
    xslg_f, xwoba_f = _to_float(statcast.get("xslg")), _to_float(statcast.get("xwoba"))
    if xslg_f is not None and xwoba_f is not None:
        quality = "elite" if xwoba_f >= 0.370 else "above-average" if xwoba_f >= 0.330 else "below-average"
        why_it_hits.append(f"xSLG {xslg_f} · xwOBA {xwoba_f} — {quality} contact quality.")

    # Streak (stats_mlb's raw l10 "streak" is always Over-signed: + = Over streak, - = Under)
    raw_l10 = splits.get("l10") or {}
    raw_streak = raw_l10.get("streak") or 0
    side_streak = -raw_streak if is_under else raw_streak
    if side_streak >= 3:
        why_it_hits.append(f"🔥 {side_streak}-game {side.title()} streak — prop has hit {side_streak} straight.")

    # L3 vs season average — is he heating up or cooling off right now?
    recent = splits.get("recent_games") or []
    season_avg = splits.get("season_avg")
    if len(recent) >= 3 and season_avg is not None:
        l3_avg = round(sum(g.get("value", 0) for g in recent[:3]) / 3, 2)
        diff = round(l3_avg - season_avg, 2)
        trend = "spiking in recent sample" if diff > 0 else "dipping in recent sample" if diff < 0 else "matching season pace"
        why_it_hits.append(f"L3 avg {l3_avg} vs {season_avg} season avg ({'+' if diff >= 0 else ''}{diff}) — {trend}.")

    # Pitcher's primary pitches
    if arsenal:
        top = sorted(arsenal, key=lambda a: a.get("pct", 0), reverse=True)[:2]
        if top:
            pitch_str = " · ".join(f"{p.get('pitch_name', '?')} ({round(p.get('pct', 0))}%)" for p in top)
            why_it_hits.append(f"Primary pitches: {pitch_str}")

    # Thresholds mirror grade_pick()'s own BvP scoring bands (see its docstring:
    # Over +4 avg>=.333, +2 avg>=.260, -2 avg<=.200, -3 avg<=.150) so the note
    # only appears when the real score actually moved because of it.
    bvp_line, bvp_note = None, None
    if bvp and bvp.get("ab"):
        bvp_line = _bvp_line(bvp)
    if bvp and bvp.get("ab", 0) >= 6:
        avg_val = bvp["hits"] / bvp["ab"] if bvp["ab"] else 0
        helps_over = avg_val >= 0.260
        hurts_over = avg_val <= 0.200
        if (helps_over and side == "over") or (hurts_over and side == "under"):
            bvp_note = f"{player_name.split()[-1]} has had this pitcher's number — a boost for the {side.title()}."
        elif (hurts_over and side == "over") or (helps_over and side == "under"):
            pitcher_last = (pitcher.get("name") or matchup.get("pitcher") or "This pitcher").split()[-1]
            bvp_note = f"{pitcher_last} has the edge on {player_name.split()[-1]} — leans {('Under' if side == 'over' else 'Over')}."

    handedness_text = None
    p_hand = pitcher.get("hand")
    if p_hand and hand_splits and hand_splits.get(p_hand):
        hs = hand_splits[p_hand]
        handedness_text = (
            f"This pitcher throws {'right' if p_hand == 'R' else 'left'}-handed. "
            f"{player_name.split()[-1]} hits {p_hand}HP at {hs.get('avg', '.---')} AVG / "
            f"{hs.get('ops', '.---')} OPS ({hs.get('pa', 0)} PA)."
        )

    leash_text = ""
    avg_ip_l3 = pitcher.get("avg_ip_l3")
    if avg_ip_l3 is not None:
        leash_text = (
            f"Short leash — {avg_ip_l3} IP avg → bullpen by {int(avg_ip_l3) + 1}th inning."
            if avg_ip_l3 < 5.0 else
            f"Long leash — {avg_ip_l3} IP avg lets him work deep into games."
        )

    vs_team_text = ""
    if vs_team and vs_team.get("games"):
        side_hits = vs_team["under"] if is_under else vs_team["over"]
        side_rate = vs_team["under_rate"] if is_under else vs_team["over_rate"]
        vs_team_text = (
            f"{side.title()} {line} vs {vs_team['team_name']} this season: "
            f"{side_hits}/{vs_team['games']} hit ({side_rate}%) · avg {vs_team.get('avg', '—')}"
        )

    wind_text = ""
    if weather and weather.get("speed_mph") is not None and not weather.get("dome"):
        wind_text = f"Wind: {weather['speed_mph']} mph {weather.get('effect', '')}".strip() + "."

    tier_label = picked_grade.get("label", "Lean")
    tier_icon = picked_grade.get("emoji", "➡️")

    risk = list(picked_grade.get("penalty_desc") or [])
    if not risk:
        risk.append("No major red flags in available data.")
    stability = picked_grade.get("stability_tier")
    if stability:
        unstable = stability.upper() in ("LOW", "VOLATILE")
        risk.insert(0, f"Stability: {stability.title()} — {'inconsistent recent values, treat hit rate with caution' if unstable else 'consistent recent output'}.")
    risk.append("Live lookup — sample sizes and matchup data are current as of this request.")

    return {
        "id": f"live-{player_name.lower().replace(' ', '-')}-{prop_type}-{side}-{line}",
        "player": player_name,
        "team": team_abbr,
        "headshot": headshot,
        "sport": "MLB",
        "betType": stat_label,
        "line": line,
        "side": side.title(),
        "score": picked_score,
        "tier": tier_label,
        "tierIcon": tier_icon,
        "estHitRate": l10_rate,
        "location": location,
        "unitSize": _unit_size_for(tier_label),
        "verdict": f"{side.upper()} {line}",
        "verdictDetail": (
            f"L10 hit rate: {l10_rate}% · L10 avg: {l10_avg} vs {line} line "
            f"({'+' if edge >= 0 else ''}{edge}) · Stability: {(picked_grade.get('stability_tier') or '—').title()} · "
            f"Projection edge: {'+' if picked_grade.get('proj_edge', 0) >= 0 else ''}{picked_grade.get('proj_edge', 0)} vs line"
            + (f" · Damage: {_damage_label(picked_grade.get('damage_score'))}" if picked_grade.get("damage_score") is not None else "")
        ),
        "whyItHits": why_it_hits,
        "hitRates": {
            "l5": l5["rate"] or 0,
            "l10": l10_rate,
            "l20": l20["rate"] or 0,
        },
        "last5": [
            {
                "value": g.get("value", 0),
                "opponent": stats_mlb._MLB_TEAM_ABBR.get(g.get("opponent", ""), (g.get("opponent") or "")[:3].upper()),
                "date": _short_date(g.get("date", "")),
            }
            for g in (splits.get("recent_games") or [])[:5]
        ][::-1],
        "split": {
            "roadAvg": splits.get("away_avg"),
            "roadOverRate": splits.get("away_rate"),
            "homeAvg": splits.get("home_avg"),
            "homeOverRate": splits.get("home_rate"),
            "callout": _split_callout(splits, is_home),
            "volume": f"Season avg {splits.get('season_avg', '—')} over {splits.get('games_played', '—')} GP.",
        },
        "matchup": {
            "opponent": opponent,
            "pitcher": (
                f"Facing {pitcher.get('name', matchup.get('pitcher', ''))} "
                f"({pitcher.get('hand', '?')}HP) — {pitcher.get('era', '—')} ERA · "
                f"{pitcher.get('hr_per_9', '—')} HR/9 · {pitcher.get('fip', '—')} FIP"
                if pitcher else ""
            ),
            "bvp": bvp_line,
            "bvpNote": bvp_note,
            "leash": leash_text,
            "handedness": handedness_text or "",
        },
        "narrative": (
            f"{player_name} has hit {side.title()} {line} in {l10.get('hits', 0)}/{l10.get('games', 0)} "
            f"of the last 10 games ({l10_rate}%), averaging {l10_avg} {stat_label} per game — "
            f"{'above' if edge >= 0 else 'below'} tonight's {line} line.\n\n"
            f"{'The evidence stacks toward the ' + side.title() + '.' if picked_score > 0 else 'The signals here are mixed — treat with caution.'}"
        ),
        "seasonLine": f"Season avg {splits.get('season_avg', '—')} over {splits.get('games_played', '—')} GP",
        "vsMatchup": {
            "h2h": bvp_line,
            "h2hNote": bvp_note,
            "career": (
                f"Career vs {opponent}: {team_bvp['avg']} avg / {team_bvp['pa']} PA · OPS {team_bvp['ops']}"
                if team_bvp and team_bvp.get("pa") else ""
            ),
            "season": vs_team_text,
        },
        "environment": f"Park factor {park_factor}x.",
        "wind": wind_text,
        "risk": risk,
        "modelConfirm": (
            f"Over score {grade['over_score']} · Under score {grade['under_score']} · "
            f"Confidence: {round(grade['confidence'] * 100)}%"
        ),
        "date": "",
    }


def _split_callout(splits: dict, is_home: bool) -> str:
    home_avg, away_avg = splits.get("home_avg"), splits.get("away_avg")
    if home_avg is None or away_avg is None:
        return ""
    stronger_home = home_avg >= away_avg
    tonight_is_stronger = stronger_home == is_home
    return (
        "Tonight's location is the stronger split — tailwind."
        if tonight_is_stronger else
        "The other venue has actually been stronger this season — monitor carefully."
    )


def _bvp_line(bvp: dict) -> str:
    """Matches the bot's BvP formatting: '2/10 (.200 AVG · 0.750 OPS) · 3 K · 1 HR — moderate sample'."""
    ab = bvp.get("ab", 0) or 0
    segments = [f"{bvp.get('hits', 0)}/{ab} ({bvp.get('avg', '.---')} AVG · {bvp.get('ops', '.---')} OPS)"]
    if bvp.get("k"):
        segments.append(f"{bvp['k']} K")
    if bvp.get("hr"):
        segments.append(f"{bvp['hr']} HR")
    sample = (
        "large sample" if ab >= 20 else
        "moderate sample" if ab >= 10 else
        "small sample" if ab >= 5 else
        "very small sample — treat with caution"
    )
    return " · ".join(segments) + f" — {sample}"


def _short_date(iso_date: str) -> str:
    try:
        from datetime import datetime as _dt
        d = _dt.strptime(iso_date, "%Y-%m-%d")
        return f"{d.month}/{d.day}"
    except (ValueError, TypeError):
        return ""


def _to_float(val):
    try:
        return round(float(val), 3)
    except (TypeError, ValueError):
        return None


def _damage_label(damage_score) -> str:
    try:
        d = float(damage_score)
    except (TypeError, ValueError):
        return "—"
    if d >= 0.75:
        return "💥 High"
    if d >= 0.4:
        return "Medium"
    return "Low"


def _unit_size_for(tier_label: str) -> str:
    return {
        "Elite": "1.0u", "Strong": "0.75u", "Good": "0.5u",
        "Lean": "0.25u", "Risky": "0u", "Fade": "0u",
    }.get(tier_label, "0.25u")


if __name__ == "__main__":
    # Quick local smoke test: python netlify/functions/prediction.py "Shohei Ohtani" "Total Bases" 1.5 over
    import sys as _sys
    args = _sys.argv[1:]
    if len(args) == 4:
        name, stat, line_arg, side_arg = args
        out = compute_prediction(name, STAT_LABEL_TO_PROP_TYPE[stat], stat, float(line_arg), side_arg)
        print(json.dumps(out, indent=2))
    else:
        print("Usage: python prediction.py <player> <stat label> <line> <over|under>")
