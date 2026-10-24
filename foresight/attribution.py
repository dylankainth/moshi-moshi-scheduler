"""Which captured time belongs to the active task.

Pure function of the raw samples since the task started, so it's identical in local mode, cloud mode
(runs on the agent) and the demo replay, and it finalises retroactively on every run:
  1. samples -> durations (capped, so sleep/agent-off gaps are no data) -> idle marking -> merged segments
  2. per segment, cheapest first: rules -> embedding -> LLM judge (only long ambiguous ones) -> fallback rule
  3. run-level: short off-task runs between on-task work are detours and count; long ones don't;
     the trailing run is 'pending' until it resolves, and a long one raises "done with X?".
Reasons never contain window titles, so segments can leave the machine in cloud mode.
"""
import bisect
import json
import os
import re
from dataclasses import dataclass, fields

from .embed import cosine


@dataclass
class Cfg:
    max_gap: float = 10.0        # a longer wait for the next sample is a gap (sleep/agent off) = no data
    poll: float = 3.0            # what a sample is worth when it's followed by a gap
    idle_off: float = 300.0      # 5+ min without input never counts
    detour_max: float = 300.0    # off-task runs up to 5 min between on-task work count
    unrelated_ask: float = 900.0  # 15+ min trailing off-task -> "done with X?"
    sim_on: float = 0.30         # embedding similarity that counts as on-task
    judge_min: float = 60.0      # only ask the LLM about ambiguous stretches at least this long

    @classmethod
    def load(cls, saved: str | None = None):
        """Defaults < FORESIGHT_<FIELD> env vars (e.g. FORESIGHT_DETOUR_MAX=60) < values saved from the UI (JSON)."""
        names = {f.name for f in fields(cls)}
        env = {n: float(os.environ[k]) for n in names if os.environ.get(k := f"FORESIGHT_{n.upper()}")}
        ui = {k: float(v) for k, v in json.loads(saved or "{}").items() if k in names}
        return cls(**{**env, **ui})


DISTRACTION = re.compile(r"\b(youtube|netflix|reddit|twitter|instagram|facebook|tiktok|twitch|prime video|disney\+|"
                         r"hulu|espn|9gag|imgur|steam|epic games|amazon\.co|ebay)\b", re.I)
DISTRACTION_EXES = {"steam.exe", "epicgameslauncher.exe", "spotify.exe", "battle.net.exe", "riotclientservices.exe"}
WORK_EXES = {"code.exe", "cursor.exe", "devenv.exe", "pycharm64.exe", "idea64.exe", "rider64.exe", "webstorm64.exe",
             "windowsterminal.exe", "powershell.exe", "pwsh.exe", "cmd.exe", "wt.exe", "notepad++.exe",
             "sublime_text.exe", "winword.exe", "excel.exe", "powerpnt.exe", "onenote.exe", "obsidian.exe",
             "notion.exe", "figma.exe", "postman.exe", "githubdesktop.exe", "mintty.exe", "zed.exe"}
WORK_SITES = re.compile(r"\b(github|gitlab|stack overflow|stackoverflow|localhost|127\.0\.0\.1|docs|documentation|"
                        r"jira|linear|confluence|google docs|google sheets|figma|mdn|pypi|npm|readthedocs|vultr)\b", re.I)
FORESIGHT_UI = re.compile(r"^foresight\b", re.I)
SEP = re.compile(r"\s+[-—–|•]\s+")
FILE = re.compile(r"[\w.-]+\.(?:py|js|ts|tsx|jsx|go|rs|java|cs|cpp|c|h|md|txt|json|yml|yaml|toml|html|css|sql|"
                  r"ipynb|docx|xlsx|pptx|pdf)\b")
GENERIC = {"new tab", "untitled", "home", "settings", "search", "google", "welcome", "start page", "dashboard",
           "administrator: windows powershell", "windows powershell", "command prompt", "file explorer"}
EMAIL_DIGITS = re.compile(r"\S+@\S+|\d{4,}")
# Trailing title parts that name the app or browser profile, not the work ("— Original profile — Mozilla Firefox").
APP_SUFFIX = re.compile(r"^(?:mozilla firefox|google chrome|microsoft\W*edge|brave|opera|vivaldi|visual studio code|cursor|"
                        r".*\bprofile(?: \d+)?|personal|work)$", re.I)


def title_parts(title: str | None) -> list[str]:
    t = re.sub(r"^[●•*]\s*|^\(\d+\)\s*", "", (title or "").strip())
    t = re.sub(r"\s+and \d+ more pages?\b", "", t)  # Edge tab groups
    parts = [p.strip() for p in SEP.split(t) if p.strip()]
    n = len(parts)
    while len(parts) > 1 and APP_SUFFIX.match(parts[-1]):
        parts.pop()
    if len(parts) == n and n > 1:
        parts = parts[:-1]  # unknown app: Windows apps put their own name last
    return parts


def clean_title(title: str | None) -> str:
    return " - ".join(title_parts(title))


def anchors(title: str | None, url: str | None) -> set[str]:
    """Things that identify 'the same work': repo/doc/page names, file names, domains."""
    t = (title or "").lower()
    out = {p for p in (x.lower() for x in title_parts(title)) if len(p) >= 3 and p not in GENERIC}
    out |= set(FILE.findall(t))
    if url:
        out.add(re.sub(r"^https?://", "", url).split("/")[0])
    return out


def redact(title: str | None, url: str | None) -> str:
    """Snippet for the LLM judge: no emails or long numbers, trimmed."""
    s = EMAIL_DIGITS.sub("…", clean_title(title))
    if url:
        s += f" ({re.sub(r'^https?://', '', url).split('/')[0]})"
    return s[:140]


def _label(s):
    return {"window": lambda: f"{s['exe'] or '?'} · {s['title']}", "private": lambda: f"private · {s['category']}",
            "idle": lambda: "idle", "nowindow": lambda: "locked / secure desktop",
            "gap": lambda: "no data (asleep or agent off)"}[s["kind"]]()


def segments(samples: list[dict], now: float, cfg: Cfg) -> list[dict]:
    n = len(samples)
    ts = [s["ts"] for s in samples]
    idle = [False] * n
    for j in range(n - 1, -1, -1):  # retroactively mark everything since the last input
        if samples[j]["idle_s"] >= cfg.idle_off:
            i = j
            lo = bisect.bisect_left(ts, ts[j] - samples[j]["idle_s"])
            while i >= lo and not idle[i]:
                idle[i] = True
                i -= 1
    out = []
    for i, s in enumerate(samples):
        nxt = ts[i + 1] if i + 1 < n else now
        dur = max(0.0, nxt - s["ts"] if nxt - s["ts"] <= cfg.max_gap else cfg.poll)
        kind = "idle" if idle[i] else s["kind"]
        key = (kind,) if kind in ("idle", "nowindow") else (kind, s.get("exe"), s.get("title"), s.get("category"))
        if out and out[-1]["_key"] == key and abs(out[-1]["end"] - s["ts"]) < 1e-6:
            out[-1]["end"] = s["ts"] + dur
        else:
            out.append({"_key": key, "start": s["ts"], "end": s["ts"] + dur, "kind": kind, "exe": s.get("exe"),
                        "title": s.get("title"), "url": s.get("url"), "category": s.get("category")})
        if nxt - s["ts"] > cfg.max_gap:
            out.append({"_key": ("gap",), "start": s["ts"] + dur, "end": nxt, "kind": "gap", "exe": None,
                        "title": None, "url": None, "category": None})
    for o in out:
        o["dur"] = o["end"] - o["start"]
        o["anchors"] = anchors(o["title"], o["url"]) if o["kind"] == "window" else set()
    return out


def _cheap(seg, task_vec, on_anchors, embed, cfg):
    """(on, source, reason, hard) or None if ambiguous. hard = never counts, not even as a detour."""
    k = seg["kind"]
    if k == "gap":
        return False, "rule", "no data", True
    if k == "nowindow":
        return False, "rule", "screen locked", True
    if k == "idle":
        return False, "rule", f"idle {cfg.idle_off / 60:.0f}+ min", True
    if k == "private":
        return False, "rule", f"private ({seg['category']}), content not read", False
    text = f"{seg['title'] or ''} {seg['url'] or ''}"
    if FORESIGHT_UI.search(seg["title"] or ""):  # neutral: counts inside a detour, never makes YouTube one
        return False, "rule", "Foresight timer", False
    if (seg["exe"] or "") in DISTRACTION_EXES or DISTRACTION.search(text):
        return False, "rule", "known distraction app/site", True
    if seg["anchors"] & on_anchors:
        return True, "rule", "same file/repo/page as earlier in this task", False
    sim = cosine(task_vec, embed(f"{clean_title(seg['title'])} {seg['url'] or ''}"))
    if sim >= cfg.sim_on:
        return True, "embedding", f"matches the task description (similarity {sim:.2f})", False
    return None


def _fallback(seg):
    if (seg["exe"] or "") in WORK_EXES or WORK_SITES.search(f"{seg['title'] or ''} {seg['url'] or ''}"):
        return True, "rule", "work app/site, no sign it's unrelated", False
    return False, "default", "no sign it's related to the task", False


def attribute(samples, task: dict, now: float, embed, cfg: Cfg = Cfg(), judge=None, overrides=None) -> dict:
    """task: {desc, ...}. judge(snippet) -> (on, reason) | None. overrides: {round(start,1): bool}."""
    segs = segments(samples, now, cfg)
    vec = embed(task["desc"])
    anchors_on = set()
    for _ in range(2):  # cheap pass to a rough fixed point so later evidence settles earlier segments
        for s in segs:
            d = _cheap(s, vec, anchors_on, embed, cfg)
            if d and d[0]:
                anchors_on |= s["anchors"]
    for s in segs:
        d = _cheap(s, vec, anchors_on, embed, cfg)
        if d is None and judge and s["dur"] >= cfg.judge_min:
            v = judge(redact(s["title"], s["url"]))
            if v:
                d = (v[0], "llm", v[1] or ("looks related" if v[0] else "looks unrelated"), False)
        d = d or _fallback(s)
        s["on"], s["source"], s["reason"], s["hard"] = d
        if s["on"]:
            anchors_on |= s["anchors"]
    for s in segs:  # retroactive: something judged off earlier may share anchors with later on-task work
        if not s["on"] and not s["hard"] and s["source"] in ("default", "llm") and s["anchors"] & anchors_on:
            s["on"], s["source"], s["reason"] = True, "rule", "same file/repo/page as elsewhere in this task"
    for s in segs:
        u = (overrides or {}).get(round(s["start"], 1))
        if u is not None:
            s["on"], s["source"], s["reason"], s["hard"] = bool(u), "user", "you marked this", not u

    i, n = 0, len(segs)
    ask = False
    while i < n:
        if segs[i]["on"]:
            segs[i]["counted"] = True
            i += 1
            continue
        j = i
        while j < n and not segs[j]["on"]:
            j += 1
        run = segs[i:j]
        soft = sum(s["dur"] for s in run if not s["hard"])
        bounded = j < n  # task start counts as an on-task boundary on the left
        for s in run:
            if s["hard"]:
                s["counted"] = False
            elif bounded and soft <= cfg.detour_max:
                s["counted"], s["reason"] = True, f"short detour, counted ({s['reason']})"
                s["source"] = s["source"] if s["source"] == "user" else "rule"
            elif not bounded and soft <= cfg.detour_max:
                s["counted"] = None
                s["reason"] = f"pending: counts if you're back within {cfg.detour_max / 60:g} min ({s['reason']})"
            else:
                s["counted"] = False
        if not bounded:
            ask = sum(s["dur"] for s in run) >= cfg.unrelated_ask
        i = j

    tot = {"counted_s": 0.0, "off_s": 0.0, "pending_s": 0.0}
    for s in segs:
        tot[{True: "counted_s", False: "off_s", None: "pending_s"}[s["counted"]]] += s["dur"]
    return {"segments": [{"start": s["start"], "end": s["end"], "dur": s["dur"], "kind": s["kind"],
                          "counted": s["counted"], "source": s["source"], "reason": s["reason"], "label": _label(s)}
                         for s in segs],
            "totals": tot, "ask_done": ask, "now": now,
            "current": _label(segs[-1]) if segs else None}


def finalise(res: dict) -> dict:
    """At 'done': pending time didn't come back, so it doesn't count."""
    for s in res["segments"]:
        if s["counted"] is None:
            s["counted"], s["reason"] = False, re.sub(r"^pending: counts if you're back within \S+ min",
                                                      "after you stopped", s["reason"])
    t = res["totals"]
    t["off_s"] += t["pending_s"]
    t["pending_s"] = 0.0
    return res
