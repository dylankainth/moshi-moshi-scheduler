import sys
import tempfile
import threading
import unittest
from pathlib import Path

from foresight import db
from foresight.capture.privacy import DEFAULT_IGNORE, IgnoreList
from foresight.capture.recorder import run, take_sample, write
from foresight.capture.source import RawWindow, url_from_title
from foresight.capture.win32 import idle_ms, resolve_uwp


class Fake:
    def __init__(self, windows, idle=0.0):
        self.windows, self.idle = list(windows), idle

    def foreground(self):
        return self.windows.pop(0) if len(self.windows) > 1 else self.windows[0]

    def idle_seconds(self):
        return self.idle


IGN = IgnoreList(DEFAULT_IGNORE)


class CaptureTests(unittest.TestCase):
    def test_window_sample(self):
        s = take_sample(Fake([RawWindow("code.exe", "auth.py - foresight - VS Code")], idle=4), IGN, 100.0)
        self.assertEqual((s.kind, s.exe, s.idle_s), ("window", "code.exe", 4))

    def test_null_window_is_nowindow(self):
        s = take_sample(Fake([None]), IGN, 1.0)
        self.assertEqual((s.kind, s.exe, s.title), ("nowindow", None, None))

    def test_private_redacted(self):
        cases = {
            RawWindow("slack.exe", "general | Acme"): "messaging",
            RawWindow("msedge.exe", "secret plans - [InPrivate] - Microsoft Edge"): "private_browsing",
            RawWindow("chrome.exe", "Inbox (3) - me@x.com - Gmail - Google Chrome"): "email",
            RawWindow("chrome.exe", "Monzo – Online Banking"): "banking",
            RawWindow("keepassxc.exe", "vault.kdbx"): "password_manager",
        }
        for w, cat in cases.items():
            s = take_sample(Fake([w]), IGN, 1.0)
            self.assertEqual((s.kind, s.category, s.exe, s.title, s.url), ("private", cat, None, None, None), w)

    def test_word_boundary(self):
        self.assertIsNone(IGN.category(RawWindow("code.exe", "bankruptcy_notes.md - VS Code")))

    def test_private_content_never_hits_disk(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "t.db"
            conn = db.connect(path)
            write(conn, take_sample(Fake([RawWindow("slack.exe", "SECRET-CHANNEL-XYZ")]), IGN, 1.0))
            conn.close()
            raw = b"".join(p.read_bytes() for p in Path(d).iterdir())
            self.assertNotIn(b"SECRET-CHANNEL-XYZ", raw)
            self.assertNotIn(b"slack", raw)
            self.assertNotIn("SECRET-CHANNEL-XYZ".encode("utf-16-le"), raw)

    def test_run_loop_writes(self):
        conn, stop = db.connect(":memory:"), threading.Event()
        n = []
        run(conn, Fake([RawWindow("code.exe", "x")]), IGN, stop, interval=0,
            on_sample=lambda s: (n.append(s), len(n) >= 3 and stop.set()))
        self.assertEqual(conn.execute("select count(*) from samples").fetchone()[0], 3)

    def test_idle_wraparound(self):
        self.assertEqual(idle_ms(5000, 1000), 4000)
        self.assertEqual(idle_ms(500, 0xFFFFFF00), 500 + 0x100)

    def test_uwp(self):
        exe_of = {7: "calculatorapp.exe"}.get
        self.assertEqual(resolve_uwp(1, "applicationframehost.exe", [1, 7], exe_of), "calculatorapp.exe")
        self.assertEqual(resolve_uwp(1, "applicationframehost.exe", [1], exe_of), "applicationframehost.exe")
        self.assertEqual(resolve_uwp(1, "code.exe", [7], exe_of), "code.exe")

    def test_url_from_title(self):
        self.assertEqual(url_from_title(RawWindow("chrome.exe", "localhost:8000 - http://localhost:8000/docs - Chrome")),
                         "http://localhost:8000/docs")
        self.assertEqual(url_from_title(RawWindow("firefox.exe", "github.com/foo/bar - Firefox")), "github.com/foo/bar")
        self.assertIsNone(url_from_title(RawWindow("code.exe", "github.com - VS Code")))
        self.assertIsNone(url_from_title(RawWindow("chrome.exe", "Fix the login bug - Google Docs")))


@unittest.skipUnless(sys.platform == "win32", "real Win32")
class RealWin32(unittest.TestCase):
    def test_smoke(self):
        from foresight.capture.win32 import Win32Source
        src = Win32Source()
        w = src.foreground()
        self.assertTrue(w is None or isinstance(w.title, str))
        self.assertGreaterEqual(src.idle_seconds(), 0)


if __name__ == "__main__":
    unittest.main()
