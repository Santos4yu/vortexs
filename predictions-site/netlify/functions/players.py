"""
Netlify Functions wrapper for player-name autocomplete.

See netlify/functions/prediction.py's docstring -- Netlify does not
support Python functions, so this is non-functional there today and
exists only for parity/reference. The real target is api/players.py
(Vercel).
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from prediction_core import search_players  # noqa: E402

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
    query = (params.get("q") or "").strip()

    try:
        results = search_players(query)
    except Exception as exc:  # noqa: BLE001
        return _response(500, {"error": f"Player search failed: {exc}"})

    return _response(200, {"players": results})
