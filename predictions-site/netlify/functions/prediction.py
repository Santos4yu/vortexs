"""
Netlify Functions wrapper for the live prediction API.

NOTE: as of this writing, Netlify does NOT support Python serverless
functions (their own docs list only TypeScript/JavaScript/Go) -- a Python
file here will silently fail to build and every request 404s, falling
through to the SPA redirect. This file is kept for reference / in case
Netlify adds Python support later, but the real deployment target for
this endpoint is Vercel (see ../../api/prediction.py), which has solid,
current Python function support.

All actual logic lives in predictions-site/prediction_core.py so both
platform wrappers share one implementation.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from prediction_core import compute_prediction, PlayerNotFound, NoGameFound, STAT_LABEL_TO_PROP_TYPE  # noqa: E402

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

    if side != "over":
        return _response(400, {"error": "VORTEX research supports Over props only."})

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
