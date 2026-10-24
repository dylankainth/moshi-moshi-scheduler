"""Replay backend: a scripted session played through the real capture path (privacy filter included)."""
from .capture.privacy import DEFAULT_IGNORE, IgnoreList
from .capture.recorder import POLL_S, take_sample
from .capture.source import RawWindow

# (minutes, exe or 'idle'/'lock'/'sleep', title). Played through take_sample, so Slack is redacted like real capture.
DEMO_TASK = "fix the login bug"
DEMO_SCRIPT = [
    (6, "code.exe", "auth.py - webapp - Visual Studio Code"),
    (3, "windowsterminal.exe", "pytest tests/test_auth.py - webapp"),
    (2, "slack.exe", "#eng-backend | Acme - Slack"),
    (6, "chrome.exe", "Session cookie lost after login redirect - Stack Overflow - Google Chrome"),
    (7, "code.exe", "session.py - webapp - Visual Studio Code"),
    (8, "idle", None),  # lunch: walked away with the editor open
    (5, "code.exe", "auth.py - webapp - Visual Studio Code"),
    (2, "windowsterminal.exe", "git push origin fix-login - webapp"),
    (18, "chrome.exe", "r/soccer - Reddit - Google Chrome"),
]
SCRIPT_MIN = sum(m for m, *_ in DEMO_SCRIPT)


class _At:
    def __init__(self, w, idle):
        self.w, self.idle = w, idle

    def foreground(self):
        return self.w

    def idle_seconds(self):
        return self.idle


def replay_samples(t0: float, upto: float, script=DEMO_SCRIPT, ignore=None, poll=POLL_S) -> list[dict]:
    ignore = ignore or IgnoreList(DEFAULT_IGNORE)
    out, t, last = [], 0.0, None
    for minutes, exe, title in script:
        end = t + minutes * 60
        idle_from = t
        while t < end and t0 + t <= upto:
            if exe == "sleep":
                pass
            else:
                if exe == "lock":
                    w, idle = None, t - idle_from
                elif exe == "idle":
                    w, idle = last, t - idle_from
                else:
                    w = last = RawWindow(exe, title)
                    idle = 1.0
                out.append(take_sample(_At(w, idle), ignore, t0 + t).__dict__)
            t += poll
        t = end
    return out
