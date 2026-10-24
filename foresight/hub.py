"""The agent talks to a hub: in-process (local mode) or HTTP (cloud mode). Attribution code is identical."""
from .netutil import http_json

# Exactly what may leave the machine per segment in cloud mode. No titles, apps, URLs or labels.
CLOUD_SEGMENT_KEYS = ("start", "end", "counted", "source", "reason", "kind")


def cloud_payload(task_id: int, res: dict) -> dict:
    return {"task_id": task_id,
            "segments": [{k: s[k] for k in CLOUD_SEGMENT_KEYS} for s in res["segments"]],
            "totals": res["totals"], "ask_done": res["ask_done"], "now": res["now"]}


class LocalHub:
    def __init__(self, app, user="me"):
        self.app, self.user = app, user

    def active_task(self):
        return self.app.agent_active(self.user)

    def push(self, task_id, res):
        self.app.agent_push(self.user, task_id, res)  # stays on this machine, labels included

    def judge(self, task_id, snippet):
        return self.app.agent_judge(self.user, task_id, snippet)


class HttpHub:
    def __init__(self, base: str, token: str, timeout=20.0):
        self.base, self.timeout = base.rstrip("/"), timeout
        self._h = {"Authorization": f"Bearer {token}"}

    def active_task(self):
        st, r = http_json("GET", f"{self.base}/api/agent/active", headers=self._h, timeout=self.timeout)
        return (r or {}).get("task") if st == 200 else None

    def push(self, task_id, res):
        http_json("POST", f"{self.base}/api/agent/segments", cloud_payload(task_id, res), self._h, self.timeout)

    def judge(self, task_id, snippet):
        st, r = http_json("POST", f"{self.base}/api/agent/judge", {"task_id": task_id, "snippet": snippet},
                          self._h, self.timeout)
        return (r["on_task"], r.get("reason", "")) if st == 200 and r and r.get("on_task") is not None else None
