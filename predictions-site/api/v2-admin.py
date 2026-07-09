"""
Vercel serverless function: consolidates VORTEX V2's admin-only actions
(PIN auth, odds-key view/swap, board scan) into ONE function.

This exists because of a hard constraint, not a design preference: Vercel's
Hobby plan caps a deployment at 12 serverless functions total. The site was
already at 9 (prediction, players, team-insights, game-log-filters, slate,
and the 4 auth/* routes); splitting admin auth/key/scan into 3 separate
files pushed the total to 13 and the deployment was rejected. Dispatching on
an `action` field keeps this at one function instead of upgrading plans.

GET  ?action=key-status              -- admin only
POST {"action": "auth", "pin": ...}
POST {"action": "key", "key": ...}   -- admin only
POST {"action": "scan", ...}         -- admin only
"""
import json
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from v2.board import admin_auth, store  # noqa: E402

# odds_client and build_board are NOT imported here on purpose. They pull in
# the whole ML scoring stack (scikit-learn/numpy/model files), and an
# import-time failure anywhere in that chain used to crash this entire
# function before any handler ran -- so even the PIN "auth" action got
# Vercel's plain-text crash page instead of JSON, and the admin modal showed
# "Unexpected token 'A' ... is not valid JSON". They're imported lazily
# inside the actions that actually need them, where the blanket
# except-Exception handlers turn any import failure into a readable JSON
# error instead.


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self._send(200, {})

    def do_GET(self):
        qs = parse_qs(urlparse(self.path).query)
        action = (qs.get("action") or [""])[0]

        try:
            if action == "key-status":
                if not admin_auth.is_admin_request(self.headers):
                    return self._send(401, {"error": "Admin session required"})
                current_key = store.get_odds_api_key()
                if not current_key:
                    return self._send(200, {"keySet": False})
                from v2.board import odds_client  # lazy -- see module docstring
                result = odds_client.test_key(current_key)
                result["keySet"] = True
                return self._send(200, result)
            return self._send(400, {"error": "Unknown or missing action"})
        except Exception as exc:  # noqa: BLE001 -- always return JSON, see do_POST
            return self._send(500, {"error": f"Unhandled error: {exc}"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "Invalid request body"})

        action = body.get("action", "")
        try:
            if action == "auth":
                return self._handle_auth(body)
            if action == "key":
                return self._handle_key(body)
            if action == "scan":
                return self._handle_scan(body)
            return self._send(400, {"error": "Unknown action"})
        except Exception as exc:  # noqa: BLE001 -- always return JSON, never let
            # Vercel's own generic (non-JSON) crash page reach the frontend,
            # which would otherwise fail res.json() with a confusing parse error.
            return self._send(500, {"error": f"Unhandled error: {exc}"})

    def _handle_auth(self, body):
        locked, seconds_left = store.is_pin_locked_out()
        if locked:
            return self._send(429, {"error": f"Too many attempts. Try again in {seconds_left // 60 + 1} min."})

        pin = str(body.get("pin", ""))
        if not admin_auth.check_pin(pin):
            store.record_pin_failure()
            return self._send(401, {"error": "Incorrect PIN"})

        store.clear_pin_failures()
        token = admin_auth.create_admin_token()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header(
            "Set-Cookie",
            f"{admin_auth.ADMIN_COOKIE_NAME}={token}; Path=/; HttpOnly; Secure; SameSite=Strict; "
            f"Max-Age={admin_auth.ADMIN_TTL_SEC}",
        )
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True}).encode("utf-8"))

    def _handle_key(self, body):
        if not admin_auth.is_admin_request(self.headers):
            return self._send(401, {"error": "Admin session required"})
        new_key = (body.get("key") or "").strip()
        if not new_key:
            return self._send(400, {"error": "No key provided"})
        from v2.board import odds_client  # lazy -- see module docstring
        result = odds_client.test_key(new_key)
        if not result.get("valid"):
            return self._send(400, {"saved": False, **result})
        store.set_odds_api_key(new_key)
        result["saved"] = True
        return self._send(200, result)

    def _handle_scan(self, body):
        if not admin_auth.is_admin_request(self.headers):
            return self._send(401, {"error": "Admin session required"})
        top_per_stat = int(body.get("top_per_stat", 4))
        min_edge = float(body.get("min_edge", 0.0))
        try:
            from v2.board.build_board import build  # lazy -- see module docstring
            result = build(top_per_stat=top_per_stat, min_edge=min_edge)
        except Exception as exc:  # noqa: BLE001 -- report the failure, don't leave the admin panel hanging
            return self._send(500, {"error": f"Scan failed: {exc}"})

        props = result["props"]
        bait = result["bait"]
        payload = {
            "date": datetime.now(timezone.utc).isoformat(),
            "top_per_stat": top_per_stat,
            "min_edge": min_edge,
            "props": props,
            "bait": bait,
        }
        store.set(store.BOARD_STORAGE_KEY, json.dumps(payload))
        return self._send(200, {"ok": True, "n_props": len(props), "n_bait": len(bait), "props": props})

    def _send(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode("utf-8"))
