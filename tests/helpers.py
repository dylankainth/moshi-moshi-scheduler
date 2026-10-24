import json
import threading
import urllib.request
from urllib.error import HTTPError

from foresight import db
from foresight.app import App
from foresight.server import serve


class Running:
    """App + HTTP server on an ephemeral loopback port, in-memory DB."""

    def __init__(self, mode="local", token=None, provider=None, demo_speed=30.0, ignore_path=None):
        self.conn = db.connect(":memory:")
        self.app = App(self.conn, mode=mode, provider=provider, demo_speed=demo_speed, ignore_path=ignore_path)
        self.httpd = serve(self.app, "127.0.0.1", 0, token=token)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def call(self, method, path, body=None, headers=None):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read()
                return r.status, json.loads(raw) if "json" in r.headers.get("Content-Type", "") else raw
        except HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"{}")

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.conn.close()
