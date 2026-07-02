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

    home_team_id = matchup.get("home_team_id")
    park_factor = 1.0
    if home_team_id:
        # PARK_FACTOR is keyed by team name; look up via the schedule's team info.
        home_name = found.get("team") or ""
        park_factor = stats_mlb.PARK_FACTOR.get(home_name, 1.0)

    grade = analyze.grade_pick_both(
        splits=splits,
        line=line,
        pitcher=pitcher or None,
        bvp=bvp,
        park_factor=park_factor,
        prop_type=prop_type,
        vs_hand_splits=hand_splits or None,
    )

    picked_grade = grade["over_grade"] if side == "over" else grade["under_grade"]
    picked_score = grade["over_score"] if side == "over" else grade["under_score"]

    return format_response(
        player_name=canonical_name,
        team_abbr=team_abbr,
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
        grade=grade,
        picked_grade=picked_grade,
        picked_score=picked_score,
    )


def format_response(*, player_name, team_abbr, stat_label, prop_type, line, side, splits,
                     matchup, pitcher, bvp, hand_splits, park_factor, grade, picked_grade, picked_score) -> dict:
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

    bvp_line, bvp_note = None, None
    if bvp and bvp.get("ab"):
        bvp_line = f"{bvp['hits']}/{bvp['ab']} ({bvp.get('avg', '.---')} AVG · {bvp.get('ops', '.---')} OPS)"
        avg_val = bvp["hits"] / bvp["ab"] if bvp["ab"] else 0
        if avg_val >= 0.300:
            bvp_note = f"{player_name.split()[-1]} has had this pitcher's number — a boost for the {side.title()}."
        elif avg_val <= 0.180 and bvp["ab"] >= 6:
            bvp_note = f"This pitcher has the edge historically — leans {('Under' if side == 'over' else 'Over')}."

    handedness_text = None
    p_hand = pitcher.get("hand")
    if p_hand and hand_splits and hand_splits.get(p_hand):
        hs = hand_splits[p_hand]
        handedness_text = (
            f"This pitcher throws {'right' if p_hand == 'R' else 'left'}-handed. "
            f"{player_name.split()[-1]} hits {p_hand}HP at {hs.get('avg', '.---')} AVG / "
            f"{hs.get('ops', '.---')} OPS ({hs.get('pa', 0)} PA)."
        )

    tier_label = picked_grade.get("label", "Lean")
    tier_icon = picked_grade.get("emoji", "➡️")

    risk = list(picked_grade.get("penalty_desc") or [])
    if not risk:
        risk.append("No major red flags in available data.")
    risk.append("Live lookup — sample sizes and matchup data are current as of this request.")

    return {
        "id": f"live-{player_name.lower().replace(' ', '-')}-{prop_type}-{side}-{line}",
        "player": player_name,
        "team": team_abbr,
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
            f"({'+' if edge >= 0 else ''}{edge}) · Projection edge: "
            f"{'+' if picked_grade.get('proj_edge', 0) >= 0 else ''}{picked_grade.get('proj_edge', 0)} vs line"
        ),
        "whyItHits": why_it_hits,
        "hitRates": {
            "l5": l5["rate"] or 0,
            "l10": l10_rate,
            "l20": l20["rate"] or 0,
        },
        "last5": [g.get("value", 0) for g in (splits.get("recent_games") or [])[:5]][::-1],
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
            "leash": "",
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
            "career": "",
            "season": "",
        },
        "environment": f"Park factor {park_factor}x.",
        "wind": "",
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
