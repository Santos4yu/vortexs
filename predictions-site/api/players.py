"""
Vercel serverless function for player-name autocomplete.

Lightweight sibling to api/prediction.py -- returns real MLB players
matching a partial name (via prediction_core.search_players(), which
wraps research.fuzzy_search()) so the search box can suggest players as
you type, instead of only offering a "search live" fallback after you've
already typed a full name.
"""

import json
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from prediction_core import search_players  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self._send(200, {})

    def do_GET(self):
        qs = parse_qs(urlparse(self.path).query)
        query = (qs.get("q", [""])[0]).strip()

        try:
            results = search_players(query)
        except Exception as exc:  # noqa: BLE001
            return self._send(500, {"error": f"Player search failed: {exc}"})

        return self._send(200, {"players": results})

    def _send(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode("utf-8"))
