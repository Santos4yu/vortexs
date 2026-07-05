"""
Vercel serverless function for the game-log modal's handedness/venue
filter chips (vs LHP / vs RHP / Home / Road).

Deliberately separate from api/prediction.py: resolving each game's
opposing starting pitcher's hand costs ~10 extra parallelized network
calls (~3s cold), which would undo the site's existing prediction-
latency work if it ran on every card load. Only called when a user
actually opens the game-log modal.
"""

import json
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from prediction_core import get_game_log_filters, PlayerNotFound, NoGameFound, STAT_LABEL_TO_PROP_TYPE  # noqa: E402
from auth_core import session_with_live_access  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self._send(200, {})

    def do_GET(self):
        if not session_with_live_access(self.headers):
            return self._send(401, {"error": "Sign in with Discord to use live research.", "authRequired": True})

        qs = parse_qs(urlparse(self.path).query)
        player_name = (qs.get("player", [""])[0]).strip()
        stat_label = (qs.get("stat", [""])[0]).strip()
        line_raw = qs.get("line", [None])[0]
        team_id = (qs.get("teamId", [""])[0]).strip()

        if not player_name or not stat_label or line_raw is None:
            return self._send(400, {"error": "Missing required params: player, stat, line"})

        try:
            line = float(line_raw)
        except (TypeError, ValueError):
            return self._send(400, {"error": f"Invalid line value: {line_raw!r}"})

        prop_type = STAT_LABEL_TO_PROP_TYPE.get(stat_label)
        if not prop_type:
            return self._send(400, {"error": f"Unknown stat: {stat_label!r}"})

        try:
            result = get_game_log_filters(player_name, prop_type, line, team_id or None)
        except PlayerNotFound as exc:
            return self._send(404, {"error": str(exc)})
        except NoGameFound as exc:
            return self._send(404, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            return self._send(500, {"error": f"Game log filter lookup failed: {exc}"})

        return self._send(200, result)

    def _send(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode("utf-8"))
