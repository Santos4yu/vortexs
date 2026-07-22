import json
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from prediction_core import compute_tool
from auth_core import session_with_live_access

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if not session_with_live_access(self.headers): return self._send(401, {"error":"Sign in required"})
        tool = parse_qs(urlparse(self.path).query).get("tool", ["attack"])[0]
        if tool not in {"attack","parks","weather","strikeouts"}: return self._send(200, {"entries":[],"tool":tool})
        try: return self._send(200, compute_tool(tool))
        except Exception as exc: return self._send(500, {"error":str(exc)})
    def _send(self,status,body):
        self.send_response(status); self.send_header("Content-Type","application/json"); self.end_headers(); self.wfile.write(json.dumps(body).encode())
