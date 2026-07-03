"""
Vercel serverless function for the live prediction API.

Vercel auto-detects any api/*.py file with a class named `handler`
subclassing BaseHTTPRequestHandler and routes /api/prediction to it.
Vercel has solid, current Python function support (unlike Netlify --
see ../netlify/functions/prediction.py for why that path doesn't work).

All actual logic lives in predictions-site/prediction_core.py so both
platform wrappers share one implementation.
"""

import json
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from prediction_core import compute_prediction, PlayerNotFound, NoGameFound, STAT_LABEL_TO_PROP_TYPE  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self._send(200, {})

    def do_GET(self):
        qs = parse_qs(urlparse(self.path).query)
        player_name = (qs.get("player", [""])[0]).strip()
        stat_label = (qs.get("stat", [""])[0]).strip()
        side = (qs.get("side", [""])[0]).strip().lower()
        line_raw = qs.get("line", [None])[0]

        if not player_name or not stat_label or not side or line_raw is None:
            return self._send(400, {"error": "Missing required params: player, stat, line, side"})

        try:
            line = float(line_raw)
        except (TypeError, ValueError):
            return self._send(400, {"error": f"Invalid line value: {line_raw!r}"})

        if side not in ("over", "under"):
            return self._send(400, {"error": "side must be 'over' or 'under'"})

        prop_type = STAT_LABEL_TO_PROP_TYPE.get(stat_label)
        if not prop_type:
            return self._send(400, {"error": f"Unknown stat: {stat_label!r}"})

        try:
            result = compute_prediction(player_name, prop_type, stat_label, line, side)
        except PlayerNotFound as exc:
            return self._send(404, {"error": str(exc)})
        except NoGameFound as exc:
            return self._send(404, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 — never leak a stack trace to the client
            return self._send(500, {"error": f"Live lookup failed: {exc}"})

        return self._send(200, result)

    def _send(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode("utf-8"))
