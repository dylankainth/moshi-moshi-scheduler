"""Local mode makes zero non-loopback connections; cloud mode sends only the whitelisted payload."""
import json
import re
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from foresight import db
from foresight.agent import Agent
from foresight.capture.recorder import Sample, write
from foresight.embed import hash_embed
from foresight.hub import CLOUD_SEGMENT_KEYS, HttpHub, LocalHub
from foresight.inference import OpenAIProvider, VultrProvider, make_provider
from foresight.replay import replay_samples
from tests.helpers import Running

LOOPBACK = {"127.0.0.1", "::1", "localhost"}


class NoEgress:
    """Patch socket.connect / getaddrinfo: any non-loopback attempt is recorded and refused."""

    def __enter__(self):
        self.attempts = []
        real_connect, real_gai = socket.socket.connect, socket.getaddrinfo
        guard = self

        def connect(sock, addr):
            if addr[0] not in LOOPBACK:
                guard.attempts.append(addr)
                raise OSError(f"egress blocked: {addr}")
            return real_connect(sock, addr)

        def gai(host, *a, **k):
            if host not in LOOPBACK:
                guard.attempts.append(host)
                raise OSError(f"dns blocked: {host}")
            return real_gai(host, *a, **k)

        self._p = [mock.patch.object(socket.socket, "connect", connect), mock.patch.object(socket, "getaddrinfo", gai)]
        for p in self._p:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._p:
            p.stop()


CODE = ("code.exe", "auth.py - webapp - Visual Studio Code")
SCRIPT = [(6, *CODE), (2, "slack.exe", "SECRET-DM with Alice - Slack"),
          (4, "chrome.exe", "Pricing for alice@corp.com order 123456 - Google Chrome"),
          (3, *CODE), (16, "chrome.exe", "r/aww - Reddit - Google Chrome")]


class LocalNoEgress(unittest.TestCase):
    def test_full_local_flow_stays_on_loopback(self):
        with NoEgress() as g:
            s = Running(mode="local", provider=make_provider("local", "none"))
            agent_conn = db.connect(":memory:")
            try:
                s.call("GET", "/")
                s.call("GET", "/api/health")
                _, e = s.call("POST", "/api/estimate", {"desc": "fix the login bug", "guess": 20})
                s.call("POST", "/api/explain", {"desc": "fix the login bug", "guess": 20})
                _, task = s.call("POST", "/api/tasks", {"desc": "fix the login bug", "guess": 20})
                t0 = task["started"]
                for smp in replay_samples(t0, t0 + 31 * 60, SCRIPT):
                    write(agent_conn, Sample(**smp))
                Agent(agent_conn, LocalHub(s.app), hash_embed, clock=lambda: t0 + 31 * 60).tick()
                s.call("GET", "/api/tasks/active")
                s.call("POST", f"/api/tasks/{task['id']}/done")
                s.call("POST", f"/api/tasks/{task['id']}/confirm", {"actual": 15})
                for path in ("/api/calibration", "/api/inference", "/api/privacy"):
                    self.assertEqual(s.call("GET", path)[0], 200, path)
                s.call("POST", "/api/privacy/pause", {"paused": True})
                h = {"X-Demo-Session": "x"}
                _, dt = s.call("POST", "/api/tasks", {"desc": "fix the login bug"}, h)
                s.call("GET", "/api/tasks/active", headers=h)
                s.call("POST", "/api/privacy/delete-all")
            finally:
                s.close()
                agent_conn.close()
        self.assertEqual(g.attempts, [])

    def test_guard_actually_trips(self):
        with NoEgress() as g:
            with self.assertRaises(OSError):
                socket.create_connection(("93.184.216.34", 443), timeout=1)
        self.assertEqual(len(g.attempts), 1)

    def test_local_refuses_vultr(self):
        with self.assertRaises(ValueError):
            make_provider("local", "vultr")
        self.assertEqual(make_provider("local", None).name, "none")

    def test_ui_has_no_external_urls(self):
        static = Path(__file__).parent.parent / "foresight" / "static"
        for f in static.rglob("*"):
            if f.is_file():
                txt = f.read_text(encoding="utf-8")
                self.assertIsNone(re.search(r"(?:https?:)?//(?!127\.0\.0\.1|localhost)[a-z0-9.-]+\.[a-z]{2,}", txt),
                                  f.name)


class Recorder:
    """Stand-in cloud server: records every request the agent makes."""

    def __init__(self, task):
        self.requests = []
        rec = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, obj):
                b = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                rec.requests.append(("GET", self.path, None, dict(self.headers)))
                self._reply({"task": task})

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"])).decode()
                rec.requests.append(("POST", self.path, body, dict(self.headers)))
                self._reply({"on_task": "auth.py" in body, "reason": "mock"} if self.path.endswith("judge") else {})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


class CloudPayload(unittest.TestCase):
    def test_agent_sends_only_whitelisted_data(self):
        t0 = 2_000_000.0
        task = {"id": 7, "desc": "fix the login bug", "started": t0, "overrides": {}}
        rec = Recorder(task)
        conn = db.connect(":memory:")
        try:
            for smp in replay_samples(t0, t0 + 31 * 60, SCRIPT):
                write(conn, Sample(**smp))
            res = Agent(conn, HttpHub(rec.url, "tok"), hash_embed, clock=lambda: t0 + 31 * 60).tick()
        finally:
            rec.httpd.shutdown()
            conn.close()
        self.assertGreater(res["totals"]["counted_s"], 0)
        paths = {(m, p) for m, p, _, _ in rec.requests}
        self.assertLessEqual(paths, {("GET", "/api/agent/active"), ("POST", "/api/agent/judge"),
                                     ("POST", "/api/agent/segments")})
        everything = " ".join(b or "" for _, _, b, _ in rec.requests)
        for secret in ("SECRET", "Alice", "Slack", "slack", "alice@corp.com", "123456", "Reddit", "r/aww", "code.exe",
                       "chrome.exe"):
            self.assertNotIn(secret, everything, secret)

        pushes = [json.loads(b) for m, p, b, _ in rec.requests if p == "/api/agent/segments"]
        self.assertEqual(len(pushes), 1)
        self.assertEqual(set(pushes[0]), {"task_id", "segments", "totals", "ask_done", "now"})
        for seg in pushes[0]["segments"]:
            self.assertEqual(set(seg), set(CLOUD_SEGMENT_KEYS))
        judged = [json.loads(b)["snippet"] for m, p, b, _ in rec.requests if p == "/api/agent/judge"]
        # Only the ambiguous stretches: first editor stretch and the (redacted) pricing page.
        self.assertEqual(len(judged), 2)
        self.assertTrue(all(set(json.loads(b)) == {"task_id", "snippet"}
                            for m, p, b, _ in rec.requests if p == "/api/agent/judge"))
        self.assertTrue(all(h.get("Authorization") == "Bearer tok" for *_, h in rec.requests))


class FakeVultr:
    def __init__(self):
        self.seen = []
        seen = self.seen

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, obj):
                b = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                seen.append(("GET", self.path, self.headers.get("Authorization")))
                self._reply({"object": "list", "data": [{"id": "bge-embed"}, {"id": "llama-3.1-70b-instruct"}]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen.append(("POST", self.path, body))
                self._reply({"choices": [{"message": {"content": "ON: same login code"}}],
                             "usage": {"total_tokens": 42}})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


class CloudServer(unittest.TestCase):
    def test_vultr_provider_picks_model_and_counts(self):
        fv = FakeVultr()
        try:
            p = VultrProvider(key="k-secret", base=fv.url)
            self.assertEqual(p.pick(), "llama-3.1-70b-instruct")
            self.assertEqual(p.judge("fix the login bug", "auth.py - webapp"), (True, "same login code"))
            post = next(x for x in fv.seen if x[0] == "POST")
            self.assertEqual(post[1], "/v1/chat/completions")
            self.assertEqual(set(post[2]), {"model", "messages", "max_tokens"})
            self.assertEqual(fv.seen[0][2], "Bearer k-secret")
            info = p.info()
            self.assertEqual((info["calls"], info["tokens"]), (1, 42))
            self.assertNotIn("k-secret", json.dumps(info))
        finally:
            fv.httpd.shutdown()

    def test_openai_provider(self):
        fv = FakeVultr()
        try:
            p = OpenAIProvider(key="sk-secret", base=fv.url, model="gpt-x")
            self.assertEqual(p.judge("fix the login bug", "auth.py - webapp"), (True, "same login code"))
            post = next(x for x in fv.seen if x[0] == "POST")
            self.assertEqual(set(post[2]), {"model", "messages", "max_completion_tokens"})
            self.assertNotIn("sk-secret", json.dumps(p.info()))
            with self.assertRaises(ValueError):
                make_provider("local", "openai")
        finally:
            fv.httpd.shutdown()

    def test_owner_token_and_demo(self):
        s = Running(mode="cloud", token="t0ken")
        try:
            self.assertEqual(s.call("GET", "/api/agent/active")[0], 403)
            self.assertEqual(s.call("GET", "/api/agent/active", headers={"Authorization": "Bearer t0ken"})[0], 200)
            _, cfg = s.call("GET", "/api/config")
            self.assertEqual((cfg["mode"], cfg["user"]), ("cloud", "demo"))
            self.assertEqual(s.call("POST", "/api/privacy/delete-all", headers={"Authorization": "Bearer t0ken"})[0],
                             403)  # privacy controls live with the local agent
            # agent push over HTTP is stored without labels
            auth = {"Authorization": "Bearer t0ken"}
            _, task = s.call("POST", "/api/tasks", {"desc": "fix the login bug"}, auth)
            seg = {"start": task["started"], "end": task["started"] + 60, "counted": True, "source": "rule",
                   "reason": "x", "kind": "window", "label": "SHOULD-DROP", "title": "SHOULD-DROP"}
            s.call("POST", "/api/agent/segments", {"task_id": task["id"], "segments": [seg], "ask_done": False,
                                                   "now": task["started"] + 60, "totals": {"counted_s": 60}}, auth)
            _, live = s.call("GET", "/api/tasks/active", headers=auth)
            self.assertNotIn("SHOULD-DROP", json.dumps(live))
            self.assertEqual(live["live"]["totals"]["counted_s"], 60)
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()
