"""HTTP API + static UI on the stdlib server. Local mode: loopback only, Host-checked, strict CSP (no external
requests from the page). Cloud mode: owner token for your data, anyone else gets a private demo session."""
import hmac
import json
import mimetypes
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .app import Err, demo_default
from .hub import CLOUD_SEGMENT_KEYS

STATIC = Path(__file__).parent / "static"
MAX_BODY = 256 * 1024
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


def _clean_push(b):
    segs = b.get("segments")
    if not isinstance(segs, list) or len(segs) > 20000:
        raise Err(400, "segments must be a list")
    clean = [{k: s.get(k) for k in CLOUD_SEGMENT_KEYS} for s in segs if isinstance(s, dict)]
    for s in clean:
        s["dur"] = float(s["end"] or 0) - float(s["start"] or 0)
        s["label"] = None
    t = b.get("totals") or {}
    return {"segments": clean, "ask_done": bool(b.get("ask_done")), "now": float(b.get("now") or 0),
            "totals": {k: float(t.get(k) or 0) for k in ("counted_s", "off_s", "pending_s")}, "current": None}


def routes(app):
    n = lambda m: int(m.group(1))
    return [
        ("GET", r"/api/health", "any", lambda u, b, m: app.health()),
        ("GET", r"/api/config", "any", lambda u, b, m: {"mode": app.mode, "user": "me" if u == "me" else "demo",
                                                         "demo": demo_default(), "demo_speed": app.demo_speed,
                                                         "inference": app.provider.name}),
        ("POST", r"/api/estimate", "any", lambda u, b, m: app.estimate(u, b.get("desc"), b.get("guess"), b.get("type"))),
        ("POST", r"/api/explain", "any", lambda u, b, m: app.explain(u, b.get("desc"), b.get("guess"), b.get("type"))),
        ("POST", r"/api/tasks", "any", lambda u, b, m: app.start(u, b.get("desc"), b.get("guess"), b.get("type"))),
        ("GET", r"/api/tasks/active", "any", lambda u, b, m: app.active(u)),
        ("POST", r"/api/tasks/(\d+)/done", "any", lambda u, b, m: app.done(u, n(m))),
        ("POST", r"/api/tasks/(\d+)/confirm", "any", lambda u, b, m: app.confirm(u, n(m), b.get("actual"))),
        ("POST", r"/api/tasks/(\d+)/discard", "any", lambda u, b, m: app.discard(u, n(m))),
        ("POST", r"/api/tasks/(\d+)/override", "any",
         lambda u, b, m: app.override(u, n(m), b.get("start"), b.get("counted"))),
        ("GET", r"/api/calibration", "any", lambda u, b, m: app.calibration(u)),
        ("GET", r"/api/inference", "any", lambda u, b, m: app.inference(u)),
        ("POST", r"/api/types/rename", "owner", lambda u, b, m: app.rename_type(u, b.get("old"), b.get("new"))),
        ("GET", r"/api/privacy", "owner", lambda u, b, m: app.get_privacy(u)),
        ("PUT", r"/api/privacy/ignore", "owner", lambda u, b, m: app.put_ignore(u, b.get("ignore"))),
        ("GET", r"/api/thresholds", "owner", lambda u, b, m: app.get_thresholds(u)),
        ("PUT", r"/api/thresholds", "owner", lambda u, b, m: app.put_thresholds(u, b.get("values"))),
        ("POST", r"/api/privacy/pause", "owner", lambda u, b, m: app.pause(u, b.get("paused"))),
        ("POST", r"/api/privacy/delete-all", "owner", lambda u, b, m: app.delete_all(u)),
        ("POST", r"/api/privacy/reseed", "owner", lambda u, b, m: app.reseed(u)),
        ("GET", r"/api/agent/active", "owner", lambda u, b, m: {"task": app.agent_active(u)}),
        ("POST", r"/api/agent/segments", "owner",
         lambda u, b, m: app.agent_push(u, int(b.get("task_id")), _clean_push(b)) or {"ok": True}),
        ("POST", r"/api/agent/judge", "owner", lambda u, b, m: (lambda v: {"on_task": v[0] if v else None,
                                                                            "reason": v[1] if v else ""})(
            app.agent_judge(u, b.get("task_id"), b.get("snippet", "")))),
    ]


def make_handler(app, token=None):
    table = [(meth, re.compile(p + "$"), acc, fn) for meth, p, acc, fn in routes(app)]

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "Foresight"

        def log_message(self, *a):
            pass

        def _send(self, status, body: bytes, ctype="application/json"):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status, obj):
            self._send(status, json.dumps(obj).encode())

        def _user(self):
            demo = re.sub(r"[^a-zA-Z0-9]", "", self.headers.get("X-Demo-Session", ""))[:32]
            if app.mode == "local":
                return f"demo:{demo}" if demo else "me"
            auth = self.headers.get("Authorization", "")
            if token and hmac.compare_digest(auth.encode(), f"Bearer {token}".encode()):
                return "me"
            return f"demo:{demo or 'anon'}"

        def _handle(self, method):
            port = self.server.server_address[1]
            if app.mode == "local" and self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
                return self._json(403, {"error": "bad host"})  # DNS-rebinding guard
            path = self.path.split("?", 1)[0]
            if not path.startswith("/api/"):
                return self._static(path) if method == "GET" else self._json(405, {"error": "method"})
            for meth, rx, acc, fn in table:
                m = rx.match(path)
                if m and meth == method:
                    user = self._user()
                    if acc == "owner" and user != "me":
                        return self._json(403, {"error": "owner only"})
                    try:
                        ln = int(self.headers.get("Content-Length") or 0)
                        if ln > MAX_BODY:
                            raise Err(413, "too large")
                        body = json.loads(self.rfile.read(ln) or b"{}") if ln else {}
                        if not isinstance(body, dict):
                            raise Err(400, "expected a JSON object")
                        return self._json(200, fn(user, body, m))
                    except Err as e:
                        return self._json(e.status, {"error": str(e)})
                    except (ValueError, TypeError, KeyError) as e:
                        return self._json(400, {"error": f"bad request: {type(e).__name__}"})
            return self._json(404, {"error": "not found"})

        def _static(self, path):
            f = (STATIC / (path.lstrip("/") or "index.html")).resolve()
            if not f.is_relative_to(STATIC.resolve()) or not f.is_file():
                f = STATIC / "index.html"
            self._send(200, f.read_bytes(), mimetypes.guess_type(f.name)[0] or "application/octet-stream")

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_PUT(self):
            self._handle("PUT")

    return H


def serve(app, host="127.0.0.1", port=8765, token=None):
    if app.mode == "local" and host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("local mode binds to loopback only")
    httpd = ThreadingHTTPServer((host, port), make_handler(app, token))
    httpd.daemon_threads = True
    return httpd
