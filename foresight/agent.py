"""Local agent: capture thread writes samples; attribution loop reads them and pushes results to the hub."""
import threading
import time

from . import db
from .attribution import Cfg, attribute
from .capture.privacy import IgnoreList
from .capture.recorder import POLL_S, take_sample, write
from .embed import get_embedder

ATTRIBUTE_EVERY_S = 5.0


class Agent:
    def __init__(self, conn, hub, embed=None, cfg=None, clock=time.time):
        self.conn, self.hub, self.clock = conn, hub, clock
        self.embed, self.cfg = embed or get_embedder(), cfg  # None: reload each tick, so UI changes apply live
        self._judged = {}  # (task_id, snippet) -> verdict; one LLM call per ambiguous stretch

    def tick(self):
        task = self.hub.active_task()
        if not task:
            return None
        rows = self.conn.execute("SELECT ts, kind, exe, title, url, category, idle_s FROM samples "
                                 "WHERE ts >= ? ORDER BY ts", (task["started"],)).fetchall()

        def judge(snippet):
            k = (task["id"], snippet)
            if self._judged.get(k) is None:  # failed calls aren't cached, so they get retried
                self._judged[k] = self.hub.judge(task["id"], snippet)
            return self._judged[k]

        ov = {round(float(k), 1): v for k, v in (task.get("overrides") or {}).items()}
        cfg = self.cfg or Cfg.load(db.get_setting(self.conn, "thresholds"))
        res = attribute([dict(r) for r in rows], task, self.clock(), self.embed, cfg, judge, ov)
        self.hub.push(task["id"], res)
        return res


def run_capture(conn, source, stop: threading.Event, interval=POLL_S):
    """Like recorder.run, but honours pause and reloads the ignore list when the file changes."""
    path = db.data_dir() / "ignore.json"
    ignore, mtime = IgnoreList.load(path), None
    while not stop.is_set():
        try:
            m = path.stat().st_mtime
            if m != mtime:
                ignore, mtime = IgnoreList.load(path), m
            if db.get_setting(conn, "paused") != "1":
                write(conn, take_sample(source, ignore, time.time()))
        except Exception as e:  # keep capturing through transient Win32/DB hiccups
            print("capture error:", e)
        stop.wait(interval)


def run_attribution(agent: Agent, stop: threading.Event, every=ATTRIBUTE_EVERY_S):
    while not stop.is_set():
        try:
            agent.tick()
        except Exception as e:
            print("attribution error:", e)
        stop.wait(every)
