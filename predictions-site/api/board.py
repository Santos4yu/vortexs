"""
Vercel serverless function: public read of the Discord bot's props board.

backend/update_board.py (the Vortex Data Engine that feeds the Discord bot's
/menu) mirrors its props_board table to the KV store after every run — this
endpoint just reads that mirror, so the site's Props tab always shows the
exact same board the bot serves. Never computes anything and never spends
odds credits.
"""
import json
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from auth_core import session_with_live_access  # noqa: E402
from v2.board import store  # noqa: E402
from v2.board import admin_auth  # noqa: E402

BOT_BOARD_KEY = "vortex:site_board"
SPECIALS_KEY = "vortex:site_specials"


def _is_upcoming(row, now=None):
    """Never serve a prop after its game has started, even from stale KV."""
    raw = str((row or {}).get("commence_time") or "").strip()
    if not raw:
        return False
    try:
        start = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        return start.astimezone(timezone.utc) > (now or datetime.now(timezone.utc))
    except (TypeError, ValueError):
        return False


def _active_board(data):
    """Filter both public board collections at request time."""
    clean = dict(data or {})
    now = datetime.now(timezone.utc)
    clean["props"] = [row for row in clean.get("props", []) if _is_upcoming(row, now)]
    clean["pitcher_research"] = [
        row for row in clean.get("pitcher_research", []) if _is_upcoming(row, now)
    ]
    return clean


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self._send(200, {})

    def do_GET(self):
        if not session_with_live_access(self.headers):
            return self._send(401, {"error": "Sign in with Discord to use live research.", "authRequired": True})

        from urllib.parse import parse_qs, urlparse
        view = (parse_qs(urlparse(self.path).query).get("view") or [""])[0]
        if view in ("specials", "results"):
            if view == "results" and not admin_auth.is_admin_request(self.headers):
                return self._send(401, {"error": "Admin passcode required"})
            raw = store.get(SPECIALS_KEY)
            try:
                data = json.loads(raw) if raw else {"moneylines": [], "nrfi": [], "records": {}}
            except json.JSONDecodeError:
                data = {"moneylines": [], "nrfi": [], "records": {}}
            if view == "results":
                return self._send(200, {"generated_at": data.get("generated_at"), "records": data.get("records", {})})
            return self._send(200, {"generated_at": data.get("generated_at"), "moneylines": data.get("moneylines", []), "moneyline_research": data.get("moneyline_research", []), "nrfi": data.get("nrfi", [])})

        raw = store.get(BOT_BOARD_KEY)
        if not raw:
            return self._send(200, {"date": None, "generated_at": None, "props": []})
        try:
            return self._send(200, _active_board(json.loads(raw)))
        except json.JSONDecodeError:
            return self._send(200, {"date": None, "generated_at": None, "props": []})

    def _send(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode("utf-8"))
