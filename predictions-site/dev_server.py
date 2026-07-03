"""
Local-only dev server for predictions-site.
Serves the static files AND proxies /api/prediction to
prediction_core.compute_prediction() (via the Netlify-shaped wrapper,
since its event/context dict is trivial to construct here) -- lets us
test the full flow without any platform CLI installed. Not used in
production; the real deployment target is Vercel (see api/prediction.py)
since Netlify doesn't support Python functions. Dev tooling only.
"""

import json
import socketserver
import sys
import traceback
from http.server import SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).parent / "netlify" / "functions"))
import prediction  # noqa: E402
import players  # noqa: E402

ROUTES = {"/api/prediction": prediction.handler, "/api/players": players.handler}


class ThreadingHTTPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            route = ROUTES.get(parsed.path)
            if route:
                qs = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                event = {"httpMethod": "GET", "queryStringParameters": qs}
                result = route(event, {})
                self.send_response(result["statusCode"])
                for k, v in result["headers"].items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(result["body"].encode("utf-8"))
                return
            super().do_GET()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass  # client disconnected mid-response — not a server problem
        except Exception:
            traceback.print_exc()
            try:
                self.send_response(500)
                self.end_headers()
            except Exception:
                pass

    def log_message(self, fmt, *args):
        # Don't let logging itself blow up on a dead connection.
        try:
            super().log_message(fmt, *args)
        except Exception:
            pass


if __name__ == "__main__":
    import os
    os.chdir(Path(__file__).parent)
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8793
    ThreadingHTTPServer(("", port), Handler).serve_forever()
