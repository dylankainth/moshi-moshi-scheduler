"""Attribution thresholds, agent + hub, done -> result card -> confirm feeding the model."""
import unittest

from foresight import db
from foresight.agent import Agent
from foresight.attribution import Cfg, attribute, finalise
from foresight.capture.recorder import Sample, write
from foresight.embed import hash_embed
from foresight.hub import LocalHub
from foresight.replay import DEMO_SCRIPT, SCRIPT_MIN, replay_samples
from tests.helpers import Running

T0 = 1_000_000.0
CODE = ("code.exe", "auth.py - webapp - Visual Studio Code")


def run(script, desc="fix the login bug", judge=None):
    up = T0 + sum(m for m, *_ in script) * 60
    return attribute(replay_samples(T0, up, script), {"desc": desc}, up, hash_embed, Cfg(), judge)


def counted_min(r):
    return round(r["totals"]["counted_s"] / 60, 1)


class Thresholds(unittest.TestCase):
    def test_short_slack_and_email_detours_count(self):
        for app in [("slack.exe", "#general | Acme - Slack"), ("chrome.exe", "Inbox (4) - Gmail - Google Chrome")]:
            r = run([(10, *CODE), (2, *app), (10, *CODE)])
            self.assertEqual(counted_min(r), 22, app)
            priv = [s for s in r["segments"] if s["kind"] == "private"]
            self.assertTrue(priv and all("Slack" not in s["label"] and "Gmail" not in s["label"] for s in priv))

    def test_long_unrelated_does_not_count_and_asks(self):
        r = run([(10, *CODE), (16, "chrome.exe", "Highlights - YouTube - Google Chrome")])
        self.assertEqual(counted_min(r), 10)
        self.assertTrue(r["ask_done"])
        r = run([(10, *CODE), (16, "chrome.exe", "Highlights - YouTube - Google Chrome"), (5, *CODE)])
        self.assertEqual(counted_min(r), 15)

    def test_idle_5_min_never_counts(self):
        r = run([(10, *CODE), (6, "idle", None), (4, *CODE)])
        self.assertEqual(counted_min(r), 14)
        r = run([(10, *CODE), (3, "idle", None), (4, *CODE)])  # short pause to think counts
        self.assertEqual(counted_min(r), 17)

    def test_sleep_gap_is_no_data(self):
        r = run([(10, *CODE), (45, "sleep", None), (5, *CODE)])
        self.assertEqual(counted_min(r), 15)
        self.assertTrue(any(s["kind"] == "gap" and s["counted"] is False for s in r["segments"]))

    def test_locked_never_counts(self):
        r = run([(10, *CODE), (3, "lock", None), (5, *CODE)])
        self.assertEqual(counted_min(r), 15)

    def test_distraction_never_counts_and_foresight_tab_is_neutral(self):
        yt = ("firefox.exe", "Highlights - YouTube - Mozilla Firefox")
        ui = ("firefox.exe", "Foresight - Mozilla Firefox")
        self.assertEqual(counted_min(run([(10, *CODE), (3, *yt), (10, *CODE)])), 20)
        # checking the timer between YouTube videos is not work, so the YouTube isn't a "short detour"
        self.assertEqual(counted_min(run([(1, *ui), (3, *yt), (1, *ui), (3, *yt), (1, *ui)])), 0)
        self.assertEqual(counted_min(run([(10, *CODE), (1, *ui), (10, *CODE)])), 21)

    def test_thresholds_from_env(self):
        from unittest import mock
        with mock.patch.dict("os.environ", {"FORESIGHT_DETOUR_MAX": "60", "FORESIGHT_IDLE_OFF": ""}):
            cfg = Cfg.load()
            self.assertEqual(Cfg.load('{"detour_max": 30}').detour_max, 30.0)  # UI beats env
        self.assertEqual((cfg.detour_max, cfg.idle_off), (60.0, 300.0))
        up = T0 + 12 * 60
        r = attribute(replay_samples(T0, up, [(10, *CODE), (2, "chrome.exe", "Cheap flights")]),
                      {"desc": "fix the login bug"}, up, hash_embed, cfg)
        self.assertEqual(r["totals"]["pending_s"], 0)  # 2 min > the 1 min detour limit: off, not pending
        self.assertEqual(counted_min(r), 10)

    def test_trailing_detour_is_pending_then_finalised(self):
        r = run([(10, *CODE), (2, "chrome.exe", "Cheap flights to Lisbon - Google Chrome")])
        self.assertAlmostEqual(r["totals"]["pending_s"], 120, delta=4)
        self.assertEqual(counted_min(finalise(r)), 10)

    def test_llm_only_for_long_ambiguous(self):
        calls = []
        judge = lambda snip: calls.append(snip) or (("auth.py" in snip), "verdict")
        r = run([(10, *CODE), (8, "chrome.exe", "Cheap flights to Lisbon - Google Chrome"), (5, *CODE)], judge=judge)
        # first editor stretch and the flights page are ambiguous; the second editor stretch is settled by rule
        self.assertEqual(len(calls), 2)
        self.assertEqual(counted_min(r), 15)
        self.assertIn("llm", {s["source"] for s in r["segments"]})

    def test_demo_script(self):
        r = run(DEMO_SCRIPT, desc="fix the login bug")
        self.assertEqual(counted_min(r), 31)
        self.assertTrue(r["ask_done"])


class AgentAndCard(unittest.TestCase):
    def setUp(self):
        self.s = Running()
        self.agent_conn = db.connect(":memory:")

    def tearDown(self):
        self.s.close()
        self.agent_conn.close()

    def test_local_loop_done_confirm(self):
        st, task = self.s.call("POST", "/api/tasks", {"desc": "fix the login bug", "guess": 20})
        self.assertEqual(st, 200)
        t0 = task["started"]
        for smp in replay_samples(t0, t0 + 25 * 60, [(10, *CODE), (2, "slack.exe", "x - Slack"), (13, *CODE)]):
            write(self.agent_conn, Sample(**smp))
        agent = Agent(self.agent_conn, LocalHub(self.s.app), hash_embed, clock=lambda: t0 + 25 * 60)
        agent.tick()
        _, live = self.s.call("GET", "/api/tasks/active")
        self.assertTrue(live["agent_connected"])
        self.assertAlmostEqual(live["live"]["totals"]["counted_s"], 25 * 60, delta=5)

        _, card = self.s.call("POST", f"/api/tasks/{task['id']}/done")
        self.assertEqual((card["status"], round(card["measured"])), ("review", 25))
        self.assertEqual(card["guess"], 20)
        self.assertIn("in80", card)
        n_before = self.s.app._model("me")["model"].plain["bugfix"].n
        st, saved = self.s.call("POST", f"/api/tasks/{task['id']}/confirm", {"actual": 27})  # user corrects
        self.assertEqual((st, saved["actual"], saved["saved_to_model"]), (200, 27, True))
        self.assertEqual(self.s.app._model("me")["model"].plain["bugfix"].n, n_before + 1)
        self.assertEqual(self.s.call("POST", f"/api/tasks/{task['id']}/confirm", {"actual": 5})[0], 409)

    def test_unconfirmed_never_trains(self):
        _, task = self.s.call("POST", "/api/tasks", {"desc": "fix the login bug"})
        self.s.call("POST", f"/api/tasks/{task['id']}/done")
        self.s.call("POST", f"/api/tasks/{task['id']}/discard")
        hist = self.s.app.history("me")
        self.assertNotIn(task["id"], [h["id"] for h in hist])

    def test_override_is_user_source(self):
        _, task = self.s.call("POST", "/api/tasks", {"desc": "fix the login bug"})
        t0 = task["started"]
        for smp in replay_samples(t0, t0 + 600, [(5, *CODE), (5, "chrome.exe", "Cheap flights - Google Chrome")]):
            write(self.agent_conn, Sample(**smp))
        agent = Agent(self.agent_conn, LocalHub(self.s.app), hash_embed, clock=lambda: t0 + 600)
        r = agent.tick()
        flights = next(s for s in r["segments"] if "flights" in s["label"])
        self.s.call("POST", f"/api/tasks/{task['id']}/override", {"start": flights["start"], "counted": True})
        r = agent.tick()
        flights = next(s for s in r["segments"] if "flights" in s["label"])
        self.assertEqual((flights["counted"], flights["source"]), (True, "user"))

    def test_demo_session_replay(self):
        s = Running(demo_speed=1e9)  # whole script replays instantly
        try:
            h = {"X-Demo-Session": "judge1"}
            _, task = s.call("POST", "/api/tasks", {"desc": "fix the login bug", "guess": 20}, h)
            _, live = s.call("GET", "/api/tasks/active", headers=h)
            self.assertTrue(live["replay"] and live["live"]["ask_done"])
            _, card = s.call("POST", f"/api/tasks/{task['id']}/done", headers=h)
            self.assertEqual(round(card["measured"]), 31)
            _, saved = s.call("POST", f"/api/tasks/{task['id']}/confirm", {"actual": 31}, h)
            self.assertFalse(saved["saved_to_model"])  # demo user is read-only
            self.assertEqual(s.call("POST", "/api/types/rename", {"old": "bugfix", "new": "x"}, h)[0], 403)
            _, me = s.call("GET", "/api/tasks/active")
            self.assertIsNone(me["task"])  # sessions are isolated from the owner
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()


class ThresholdsApi(unittest.TestCase):
    def test_set_from_ui_applies_live_and_validates(self):
        s = Running()
        try:
            st, r = s.call("PUT", "/api/thresholds", {"values": {"detour_max": 45, "bogus": 1}})
            self.assertEqual((st, r["values"]["detour_max"], r["defaults"]["detour_max"]), (200, 45.0, 300.0))
            self.assertNotIn("bogus", r["values"])
            self.assertEqual(s.app.cfg.detour_max, 45.0)
            self.assertEqual(Cfg.load(db.get_setting(s.conn, "thresholds")).detour_max, 45.0)  # what the agent reads each tick
            self.assertEqual(s.call("PUT", "/api/thresholds", {"values": {"idle_off": -1}})[0], 400)
            self.assertEqual(s.call("PUT", "/api/thresholds", {"values": {"sim_on": "x"}})[0], 400)
            self.assertEqual(s.call("PUT", "/api/thresholds", {"values": {}})[1]["values"]["detour_max"], 300.0)
        finally:
            s.close()


class JudgeCache(unittest.TestCase):
    def test_verdicts_survive_restart_and_failures_retry(self):
        from foresight.app import App
        from foresight.inference import Provider

        class P(Provider):
            name, calls, up = "openai", 0, False
            def _chat(self, *a):
                P.calls += 1
                if not P.up:
                    raise TimeoutError
                return "OFF: flights", 5

        conn = db.connect(":memory:")
        a = App(conn, provider=P())
        tid = a.start("me", "fix the login bug")["id"]
        self.assertIsNone(a.agent_judge("me", tid, "Cheap flights"))  # failure: not stored
        P.up = True
        self.assertEqual(a.agent_judge("me", tid, "Cheap flights"), (False, "flights"))
        b = App(conn, provider=P())  # restart on the same DB
        self.assertEqual(b.agent_judge("me", tid, "Cheap flights"), (False, "flights"))
        self.assertEqual(P.calls, 2)
        raw = "".join(map(str, conn.execute("SELECT * FROM judged").fetchall()))
        self.assertNotIn("Cheap", raw)


class Explanations(unittest.TestCase):
    def test_template_and_llm_fallback(self):
        from foresight.explain import explain
        from foresight.inference import Provider
        s = Running()
        try:
            e = s.app.estimate("me", "fix the login bug", 20)
            self.assertIn("1.6x", e["explanation"])
            self.assertEqual(explain(e, Provider()), (e["explanation"], "template"))

            class Down(Provider):
                name = "vultr"
                def _chat(self, *a):
                    raise TimeoutError
            down = Down()
            self.assertEqual(explain(e, down), (e["explanation"], "template"))
            self.assertEqual(down.stats["errors"], 1)

            class Up(Provider):
                name = "vultr"
                def _chat(self, system, user, max_tokens):
                    return "Plan for about half an hour.", 12
            self.assertEqual(explain(e, Up()), ("Plan for about half an hour.", "vultr"))
        finally:
            s.close()


class Titles(unittest.TestCase):
    def test_browser_profile_suffix_is_not_an_anchor(self):
        # Real Firefox titles from `foresight check` on Windows 11
        from foresight.attribution import anchors, clean_title
        a = anchors("Hackclock — Original profile — Mozilla Firefox", None)
        b = anchors("dylankainth (Dylan K) — Original profile — Mozilla Firefox", None)
        self.assertEqual(a & b, set())
        self.assertEqual(clean_title("Hackclock — Original profile — Mozilla Firefox"), "Hackclock")
        self.assertEqual(clean_title("Fix bug and 2 more pages - Personal - Microsoft​ Edge"), "Fix bug")
        self.assertEqual(anchors("auth.py - webapp - Visual Studio Code", None), {"auth.py", "webapp"})
        self.assertEqual(clean_title("Windows PowerShell"), "Windows PowerShell")
