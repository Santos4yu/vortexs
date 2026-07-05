"""
Vercel serverless function for the opposing team's Batting Order & Stats
and Batters vs Pitch Arsenal views.

Deliberately separate from api/prediction.py: fetching season/hand/BvP
stats for all 9 lineup batters takes several seconds, which would undo
the latency work already done on the main prediction card. This is only
called when the user opens that view from a loaded card, using the
lightweight teamId/pitcherId/pitcherName/pitcherHand the main card
already returned.
"""

import json
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from prediction_core import get_team_insights  # noqa: E402
from auth_core import session_with_live_access  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self._send(200, {})

    def do_GET(self):
        # Live (uncached) Discord role check -- see api/prediction.py.
        if not session_with_live_access(self.headers):
            return self._send(401, {"error": "Sign in with Discord to view team insights.", "authRequired": True})

        qs = parse_qs(urlparse(self.path).query)
        team_id = (qs.get("teamId", [""])[0]).strip()
        pitcher_id = (qs.get("pitcherId", [""])[0]).strip()
        pitcher_name = (qs.get("pitcherName", [""])[0]).strip()
        pitcher_hand = (qs.get("pitcherHand", ["R"])[0]).strip() or "R"

        if not team_id:
            return self._send(400, {"error": "Missing teamId"})

        try:
            result = get_team_insights(
                int(team_id),
                int(pitcher_id) if pitcher_id else None,
                pitcher_name,
                pitcher_hand,
            )
        except Exception as exc:  # noqa: BLE001
            return self._send(500, {"error": f"Team insights failed: {exc}"})

        return self._send(200, result)

    def _send(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode("utf-8"))
