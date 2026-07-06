"""
Local-only dev server for predictions-site.
Serves the static files AND proxies every /api/* route to the REAL Vercel
serverless handlers in api/ (BaseHTTPRequestHandler subclasses) -- so local
testing reflects exactly what's deployed, including the auth gate. Not used
in production; Vercel runs api/*.py directly. Dev tooling only.

(Previously routed prediction/players/team-insights through the
netlify/functions/*.py dict-shaped wrappers instead -- those never carried
the auth gate added to api/*.py, so local testing silently diverged from
production. netlify/functions/ is kept only as non-functional reference,
per this project's established note that Netlify doesn't support Python
functions at all.)
"""

import importlib.util as _ilu
import socketserver
import sys
import traceback
from http.server import SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

API_DIR = Path(__file__).parent / "api"


def _load_handler(rel_path: str):
    """Import an api/*.py handler module by file path -- needed because
    "team-insights.py" has a hyphen, not a valid `import` module name."""
    file_path = API_DIR / rel_path
    mod_name = "api_" + rel_path.replace("/", "_").replace("-", "_").replace(".py", "")
    spec = _ilu.spec_from_file_location(mod_name, file_path)
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.handler


# All routes are Vercel-style: the handler classes ARE BaseHTTPRequestHandler
# subclasses with the identical do_GET(self) signature (self.path/headers/
# wfile/send_response, etc.), so calling the unbound method with OUR request
# instance as self works directly -- no need for a second real socket/request.
ROUTES = {
    "/api/prediction": _load_handler("prediction.py"),
    "/api/players": _load_handler("players.py"),
    "/api/team-insights": _load_handler("team-insights.py"),
    "/api/slate": _load_handler("slate.py"),
    "/api/auth/login": _load_handler("auth/login.py"),
    "/api/auth/callback": _load_handler("auth/callback.py"),
    "/api/auth/me": _load_handler("auth/me.py"),
    "/api/auth/logout": _load_handler("auth/logout.py"),
}


class ThreadingHTTPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        try:
            route_cls = ROUTES.get(urlparse(self.path).path)
            if route_cls:
                # Naively calling route_cls.do_GET(self) breaks the moment
                # a handler uses its own helper methods (every api/*.py
                # handler has a `_send` method dev_server's own Handler
                # class doesn't define) -- AttributeError. Instead, merge
                # route_cls's methods onto this already-initialized
                # instance by swapping __class__ to a combined type (route
                # methods take priority via MRO); safe here since both are
                # BaseHTTPRequestHandler subclasses sharing the same
                # request-time attributes (wfile, rfile, path, headers...).
                self.__class__ = type("_DispatchHandler", (route_cls, self.__class__), {})
                route_cls.do_GET(self)
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
