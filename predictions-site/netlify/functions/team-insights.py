"""
Netlify Functions wrapper for the opposing team's Batting Order & Stats /
Batters vs Pitch Arsenal views.

See netlify/functions/prediction.py's docstring -- Netlify does not
support Python functions, so this is non-functional there today and
exists only for parity/reference. The real target is
api/team-insights.py (Vercel).
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from prediction_core import get_team_insights  # noqa: E402

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Content-Type": "application/json",
}


def _response(status, body):
    return {"statusCode": status, "headers": CORS_HEADERS, "body": json.dumps(body)}


def handler(event, context):
    if (event.get("httpMethod") or "GET").upper() == "OPTIONS":
        return _response(200, {})

    params = event.get("queryStringParameters") or {}
    team_id = (params.get("teamId") or "").strip()
    pitcher_id = (params.get("pitcherId") or "").strip()
    pitcher_name = (params.get("pitcherName") or "").strip()
    pitcher_hand = (params.get("pitcherHand") or "R").strip() or "R"
    detail = (params.get("detail") or "summary").strip().lower()

    if not team_id:
        return _response(400, {"error": "Missing teamId"})

    try:
        result = get_team_insights(
            int(team_id),
            int(pitcher_id) if pitcher_id else None,
            pitcher_name,
            pitcher_hand,
            detail,
        )
    except Exception as exc:  # noqa: BLE001
        return _response(500, {"error": f"Team insights failed: {exc}"})

    return _response(200, result)
