"""
Vercel serverless function: admin-only "Scan Now" trigger. Runs the full
VORTEX V2 board build (free model scoring across today's slate, then real
odds for the shortlist only -- see v2/board/build_board.py) and writes the
result to the live store so api/v2-board.py can serve it to everyone else
without re-running anything.

This can legitimately take over a minute (scoring several hundred batters
against the live MLB Stats API is the slow part, not the odds calls) --
predictions-site/vercel.json raises this function's maxDuration accordingly.
"""
import json
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from v2.board import admin_auth, store  # noqa: E402
from v2.board.build_board import build  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self._send(200, {})

    def do_POST(self):
        if not admin_auth.is_admin_request(self.headers):
            return self._send(401, {"error": "Admin session required"})

        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except json.JSONDecodeError:
            body = {}
        top_per_stat = int(body.get("top_per_stat", 8))
        min_edge = float(body.get("min_edge", 0.0))

        try:
            props = build(top_per_stat=top_per_stat, min_edge=min_edge)
        except Exception as exc:  # noqa: BLE001 -- report the failure, don't leave the admin panel hanging
            return self._send(500, {"error": f"Scan failed: {exc}"})

        payload = {
            "date": datetime.now(timezone.utc).isoformat(),
            "top_per_stat": top_per_stat,
            "min_edge": min_edge,
            "props": props,
        }
        store.set(store.BOARD_STORAGE_KEY, json.dumps(payload))
        return self._send(200, {"ok": True, "n_props": len(props), "props": props})

    def _send(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode("utf-8"))
