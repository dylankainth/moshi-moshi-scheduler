"""Server-side logic, independent of HTTP: estimates, task lifecycle, model, calibration, privacy, health."""
import hashlib
import json
import math
import os
import threading
import time
from dataclasses import asdict
from pathlib import Path
from statistics import median

from . import __version__, db
from .attribution import Cfg, attribute, finalise
from .capture.privacy import DEFAULT_IGNORE, IgnoreList
from .embed import get_embedder
from .explain import explain, template
from .inference import Provider
from .match import match
from .model import Model, Task, bias_factor, fit_hyper
from .replay import DEMO_TASK, SCRIPT_MIN, replay_samples
from .seed import simulate

POP_HYPER = fit_hyper(simulate(n=300, seed=1))  # population prior for users with little history


class Err(Exception):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status = status


def base_user(user):
    return "demo" if user.startswith("demo") else user


class App:
    def __init__(self, conn, mode="local", provider=None, embed=None, seed=True, demo_speed=30.0, db_path=None,
                 cfg=None, ignore_path=None):
        self.conn, self.mode = conn, mode
        self.provider = provider or Provider()
        self.embed = embed or get_embedder()
        self.cfg = cfg or Cfg.load(db.get_setting(conn, "thresholds"))
        self.demo_speed = demo_speed
        self.db_path = db_path
        self.ignore_path = Path(ignore_path) if ignore_path else db.data_dir() / "ignore.json"
        self.lock = threading.RLock()  # ponytail: one lock for all state; single-user app, per-user locks if needed
        self.started_at = time.time()
        self._models, self._judged = {}, {}
        if seed:
            self.seed()

    # ---------- history & model ----------
    def seed(self):
        for user, s in (("me", 42), ("demo", 7)):
            if user == "me" and db.get_setting(self.conn, "no_seed") == "1":
                continue
            if self.conn.execute("SELECT 1 FROM tasks WHERE user=? LIMIT 1", (user,)).fetchone():
                continue
            self.insert_seed(user, s)

    def insert_seed(self, user, s, n=120):
        m, rows = Model(POP_HYPER), []
        for t in simulate(n=n, seed=s):
            e = m.predict(t.type, t.guess)
            rows.append((user, t.desc, t.type, t.guess, json.dumps(asdict(e)), t.ts - t.actual * 60, t.ts,
                         t.actual, t.actual, "done"))
            m.add(t)
        self.conn.executemany("INSERT INTO tasks(user, desc, type, guess, est, started, ended, measured, actual, "
                              "status) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        self._models.pop(user, None)

    def history(self, user):
        u = base_user(user)
        rows = [dict(r) for r in self.conn.execute(
            "SELECT * FROM tasks WHERE user=? AND status='done' ORDER BY ended", (u,))]
        if not rows and u == "demo":  # synthetic demo user is recreated on demand (e.g. after delete-all)
            self.insert_seed("demo", 7)
            return self.history(u)
        return rows

    def _model(self, user):
        u = base_user(user)
        if u not in self._models:
            tasks = [Task(r["type"], r["actual"], r["guess"], r["desc"], r["ended"]) for r in self.history(u)]
            m = Model.fit(tasks, fit_hyper(tasks) if len(tasks) >= 30 else POP_HYPER)
            self._models[u] = {"model": m, "tasks": tasks, "bias": bias_factor(tasks)}
        return self._models[u]

    # ---------- estimate & lifecycle ----------
    def estimate(self, user, desc, guess=None, type_=None):
        desc = (desc or "").strip()[:200]
        if not desc:
            raise Err(400, "describe the task")
        guess = float(guess) if guess not in (None, "", 0) else None
        if guess is not None and not 0 < guess < 24 * 60:
            raise Err(400, "guess must be between 0 and 1440 minutes")
        with self.lock:
            past = self.history(user)
            typ, nbs, is_new = match(desc, past, self.embed)
            if type_:
                typ, is_new = type_.strip()[:40], not any(p["type"] == type_ for p in past)
            m = self._model(user)
            est = m["model"].predict(typ, guess)
            out = {"desc": desc, "type": typ, "is_new_type": is_new, "guess": guess, "estimate": asdict(est),
                   "bias": m["bias"],
                   "neighbours": [{"desc": p["desc"], "type": p["type"], "actual": p["actual"], "guess": p["guess"],
                                   "sim": round(p["sim"], 2)} for p in nbs],
                   "types": sorted({p["type"] for p in past})}
            out["explanation"], out["explanation_source"] = template(out), "template"
            return out

    def explain(self, user, desc, guess=None, type_=None):
        e = self.estimate(user, desc, guess, type_)
        e["explanation"], e["explanation_source"] = explain(e, self.provider)
        return e

    def start(self, user, desc, guess=None, type_=None):
        e = self.estimate(user, desc, guess, type_)
        with self.lock:
            self.conn.execute("UPDATE tasks SET status='discarded' WHERE user=? AND status IN ('active','review')",
                              (user,))
            if user.startswith("demo:"):
                self.conn.execute("DELETE FROM tasks WHERE user LIKE 'demo:%' AND started < ?", (time.time() - 86400,))
            cur = self.conn.execute("INSERT INTO tasks(user, desc, type, guess, est, started, status) "
                                    "VALUES (?,?,?,?,?,?,'active')",
                                    (user, e["desc"], e["type"], e["guess"], json.dumps(e["estimate"]), time.time()))
            return self._task(user, cur.lastrowid)

    def _task(self, user, task_id):
        r = self.conn.execute("SELECT * FROM tasks WHERE id=? AND user=?", (task_id, user)).fetchone()
        if not r:
            raise Err(404, "no such task")
        t = dict(r)
        t["est"] = json.loads(t["est"]) if t["est"] else None
        return t

    def _active_row(self, user):
        r = self.conn.execute("SELECT id FROM tasks WHERE user=? AND status IN ('active','review') "
                              "ORDER BY id DESC LIMIT 1", (user,)).fetchone()
        return self._task(user, r[0]) if r else None

    def _overrides(self, task_id):
        return {round(r[0], 1): bool(r[1]) for r in
                self.conn.execute("SELECT start, counted FROM overrides WHERE task_id=?", (task_id,))}

    def _demo_live(self, t):
        sim_now = t["started"] + min((time.time() - t["started"]) * self.demo_speed, SCRIPT_MIN * 60)
        samples = replay_samples(t["started"], sim_now)

        def judge(snippet):
            k = (t["id"], snippet)
            if self._judged.get(k) is None:  # failed calls aren't cached, so they get retried
                self._judged[k] = self.provider.judge(t["desc"], snippet)
            return self._judged[k]

        return attribute(samples, t, sim_now, self.embed, self.cfg, judge, self._overrides(t["id"]))

    def _store_live(self, task_id, res):
        self.conn.execute("INSERT OR REPLACE INTO live(task_id, json, updated) VALUES (?,?,?)",
                          (task_id, json.dumps(res), time.time()))

    def _live(self, task_id):
        r = self.conn.execute("SELECT json, updated FROM live WHERE task_id=?", (task_id,)).fetchone()
        return (json.loads(r[0]), r[1]) if r else (None, None)

    def active(self, user):
        with self.lock:
            t = self._active_row(user)
            if not t:
                return {"task": None}
            if t["status"] == "active" and user.startswith("demo:"):
                self._store_live(t["id"], self._demo_live(t))
            live, updated = self._live(t["id"])
            agent_ok = updated is not None and time.time() - updated < 30
            return {"task": t, "live": live, "agent_connected": agent_ok or user.startswith("demo:"),
                    "replay": user.startswith("demo:"), "card": self._card(t) if t["status"] == "review" else None}

    def done(self, user, task_id):
        with self.lock:
            t = self._task(user, task_id)
            if t["status"] != "active":
                raise Err(409, "task is not running")
            res = self._demo_live(t) if user.startswith("demo:") else self._live(task_id)[0]
            res = finalise(res or {"segments": [], "totals": {"counted_s": 0, "off_s": 0, "pending_s": 0},
                                   "ask_done": False, "now": time.time()})
            self._store_live(task_id, res)
            measured = round(res["totals"]["counted_s"] / 60, 1)
            self.conn.execute("UPDATE tasks SET status='review', ended=?, measured=? WHERE id=?",
                              (res["now"], measured, task_id))
            return self._card(self._task(user, task_id))

    def _card(self, t):
        e = t["est"] or {}
        a = t["actual"] if t["actual"] is not None else t["measured"]
        c = {"id": t["id"], "desc": t["desc"], "type": t["type"], "guess": t["guess"], "estimate": e,
             "measured": t["measured"], "actual": t["actual"], "status": t["status"]}
        if a and e:
            c["in50"], c["in80"] = e["lo50"] <= a <= e["hi50"], e["lo80"] <= a <= e["hi80"]
            if t["guess"]:
                c["vs_guess"] = round(a / t["guess"], 2)
        return c

    def confirm(self, user, task_id, actual):
        try:
            actual = float(actual)
        except (TypeError, ValueError):
            raise Err(400, "actual must be minutes")
        if not 0 < actual < 24 * 60:
            raise Err(400, "actual must be between 0 and 1440 minutes")
        with self.lock:
            t = self._task(user, task_id)
            if t["status"] != "review":
                raise Err(409, "press done first")
            self.conn.execute("UPDATE tasks SET status='done', actual=? WHERE id=?", (actual, task_id))
            live, _ = self._live(task_id)
            self.conn.executemany(
                "INSERT INTO segments(task_id, start, end, counted, source, reason, kind) VALUES (?,?,?,?,?,?,?)",
                [(task_id, s["start"], s["end"], int(bool(s["counted"])), s["source"], s["reason"], s["kind"])
                 for s in (live or {}).get("segments", [])])
            card = self._card(self._task(user, task_id))
            if user.startswith("demo"):
                card["saved_to_model"] = False
                return card
            m = self._model(user)
            task = Task(t["type"], actual, t["guess"], t["desc"], t["ended"])
            m["model"].add(task)
            m["tasks"].append(task)
            m["bias"] = bias_factor(m["tasks"])
            card["saved_to_model"], card["bias"] = True, m["bias"]
            return card

    def discard(self, user, task_id):
        with self.lock:
            self._task(user, task_id)
            self.conn.execute("UPDATE tasks SET status='discarded' WHERE id=?", (task_id,))
            return {"ok": True}

    def override(self, user, task_id, start, counted):
        with self.lock:
            t = self._task(user, task_id)
            if t["status"] != "active":
                raise Err(409, "task is not running")
            self.conn.execute("INSERT OR REPLACE INTO overrides(task_id, start, counted) VALUES (?,?,?)",
                              (task_id, round(float(start), 1), int(bool(counted))))
            return {"ok": True}

    # ---------- agent hub (local in-process, or over HTTP in cloud mode) ----------
    def agent_active(self, user):
        with self.lock:
            t = self._active_row(user)
            if not t or t["status"] != "active":
                return None
            return {"id": t["id"], "desc": t["desc"], "started": t["started"],
                    "overrides": {str(k): v for k, v in self._overrides(t["id"]).items()}}

    def agent_push(self, user, task_id, res):
        with self.lock:
            t = self._task(user, int(task_id))
            if t["status"] == "active":
                self._store_live(t["id"], res)

    def agent_judge(self, user, task_id, snippet):
        t = self._task(user, int(task_id))
        snippet = str(snippet)[:160]
        h = hashlib.sha256(snippet.encode()).hexdigest()
        with self.lock:
            r = self.conn.execute("SELECT on_task, reason FROM judged WHERE task_id=? AND h=?", (t["id"], h)).fetchone()
        if r:
            return bool(r[0]), r[1]
        v = self.provider.judge(t["desc"], snippet)
        if v:  # failures aren't stored, so they get retried
            with self.lock:
                self.conn.execute("INSERT OR REPLACE INTO judged VALUES (?,?,?,?)", (t["id"], h, int(v[0]), v[1]))
        return v

    # ---------- calibration & types ----------
    def calibration(self, user):
        with self.lock:
            hist = self.history(user)
            m = self._model(user)
        bias_series, guessed, nth, widths = [], [], {}, {}
        hits50 = hits80 = 0
        rolling = []
        for i, t in enumerate(hist):
            if t["guess"]:
                guessed.append(Task(t["type"], t["actual"], t["guess"]))
                if len(guessed) >= 5:
                    bias_series.append({"i": i, "ts": t["ended"], "bias": round(bias_factor(guessed), 3)})
            e = json.loads(t["est"]) if t["est"] else None
            if e:
                h80 = e["lo80"] <= t["actual"] <= e["hi80"]
                hits50 += e["lo50"] <= t["actual"] <= e["hi50"]
                hits80 += h80
                rolling.append(h80)
                k = nth.get(t["type"], 0)
                nth[t["type"]] = k + 1
                widths.setdefault(k, []).append(e["hi80"] / e["lo80"])
        n_est = len(rolling)
        hit_series = [{"i": i, "rate": round(sum(rolling[max(0, i - 19):i + 1]) / len(rolling[max(0, i - 19):i + 1]), 3)}
                      for i in range(n_est)]
        narrowing = [{"n": k, "width": round(median(v), 2), "count": len(v)} for k, v in sorted(widths.items())
                     if k < 25]
        types = {}
        for t in hist:
            types.setdefault(t["type"], []).append(t["actual"])
        b = m["bias"]
        return {"bias": b, "bias_text": (f"You take {b:.1f}x what you say." if b else "Add a few guesses to learn your bias."),
                "bias_series": bias_series, "hit50": hits50 / n_est if n_est else None,
                "hit80": hits80 / n_est if n_est else None, "hit_series": hit_series, "narrowing": narrowing,
                "types": [{"type": k, "count": len(v), "median": round(median(v), 1)} for k, v in sorted(types.items())],
                "n": len(hist)}

    def rename_type(self, user, old, new):
        if user.startswith("demo"):
            raise Err(403, "the demo user is read-only")
        new = (new or "").strip()[:40]
        if not new:
            raise Err(400, "new name required")
        with self.lock:
            self.conn.execute("UPDATE tasks SET type=? WHERE user=? AND type=?", (new, user, old))
            self._models.pop(user, None)  # renaming onto an existing type merges them; refit
        return {"ok": True}

    # ---------- inference counters & health ----------
    def inference(self, user):
        with self.lock:
            ids = [r[0] for r in self.conn.execute("SELECT id FROM tasks WHERE user=?", (user,))]
            q = ",".join("?" * len(ids)) or "NULL"
            rows = self.conn.execute(f"SELECT source, COUNT(*) FROM segments WHERE task_id IN ({q}) "
                                     f"AND kind='window' GROUP BY source", ids).fetchall()
            by = {r[0]: r[1] for r in rows}
            t = self._active_row(user)
            if t:
                live, _ = self._live(t["id"])
                if live and t["status"] == "active":
                    for s in live["segments"]:
                        if s["kind"] == "window":
                            by[s["source"]] = by.get(s["source"], 0) + 1
        total = sum(by.values())
        return {**self.provider.info(), "resolved_by": by,
                "share": {k: round(v / total, 3) for k, v in by.items()} if total else {}}

    def health(self):
        size = 0
        if self.db_path and Path(self.db_path).exists():
            size = sum(Path(str(self.db_path) + s).stat().st_size for s in ("", "-wal")
                       if Path(str(self.db_path) + s).exists())
        return {"ok": True, "mode": self.mode, "version": __version__, "inference_provider": self.provider.name,
                "inference_model": self.provider.model, "db_bytes": size,
                "uptime_s": round(time.time() - self.started_at), "egress": {"vultr": "vultr-inference", "openai": "openai"}.get(self.provider.name, "none"),
                "demo_speed": self.demo_speed}

    # ---------- privacy (local machine controls) ----------
    def _local_only(self, user):
        if self.mode != "local" or user != "me":
            raise Err(403, "privacy controls live with your local agent")

    def get_privacy(self, user):
        self._local_only(user)
        return {"ignore": IgnoreList.load(self.ignore_path).rules, "paused": db.get_setting(self.conn, "paused") == "1",
                "defaults": DEFAULT_IGNORE}

    def put_ignore(self, user, rules):
        self._local_only(user)
        if not isinstance(rules, dict) or not all(
                isinstance(v, dict) and all(isinstance(x, str) for k in ("exe", "title") for x in v.get(k, []))
                for v in rules.values()):
            raise Err(400, "expected {category: {exe: [...], title: [...]}}")
        clean = {str(k)[:40]: {"exe": [x.strip().lower() for x in v.get("exe", []) if x.strip()],
                               "title": [x.strip() for x in v.get("title", []) if x.strip()]} for k, v in rules.items()}
        self.ignore_path.write_text(json.dumps(clean, indent=2))
        return {"ok": True, "ignore": clean}

    def get_thresholds(self, user):
        self._local_only(user)
        return {"values": asdict(self.cfg), "defaults": asdict(Cfg.load())}

    def put_thresholds(self, user, values):
        self._local_only(user)
        names = asdict(Cfg())
        try:
            clean = {k: float(v) for k, v in (values or {}).items() if k in names}
        except (TypeError, ValueError):
            raise Err(400, "thresholds must be numbers")
        if any(v <= 0 for v in clean.values()) or clean.get("sim_on", 0.5) > 1:
            raise Err(400, "thresholds must be positive (similarity at most 1)")
        with self.lock:
            db.set_setting(self.conn, "thresholds", json.dumps(clean))
            self.cfg = Cfg.load(json.dumps(clean))
        return self.get_thresholds(user)

    def pause(self, user, paused):
        self._local_only(user)
        db.set_setting(self.conn, "paused", 1 if paused else 0)
        return {"paused": bool(paused)}

    def delete_all(self, user):
        self._local_only(user)
        with self.lock:
            db.delete_all(self.conn)
            self.cfg = Cfg.load()
            self._models.clear()
            self._judged.clear()
        return {"ok": True}

    def reseed(self, user):
        self._local_only(user)
        with self.lock:
            db.set_setting(self.conn, "no_seed", 0)
            if not self.conn.execute("SELECT 1 FROM tasks WHERE user='me' LIMIT 1").fetchone():
                self.insert_seed("me", 42)
        return {"ok": True}


def demo_default():
    return {"desc": DEMO_TASK, "script_min": SCRIPT_MIN}
