"""LLM providers: none | ollama (local, loopback only) | vultr (cloud, Serverless Inference) | openai (cloud).
Used only as the ambiguity judge and the explainer; never to produce durations."""
import os
import re
import threading
import time
from urllib.parse import urlparse

from .netutil import http_json

VULTR_BASE = "https://api.vultrinference.com/v1"
OPENAI_BASE = "https://api.openai.com/v1"
JUDGE_SYS = ("You decide whether a computer window belongs to a work task. Reply with exactly one line: "
             "'ON: <reason>' or 'OFF: <reason>', reason at most 8 words.")
_VERDICT = re.compile(r"^\W*(ON|OFF)\b\W*(.*)", re.I | re.S)


class Provider:
    name = "none"
    model = None

    def __init__(self):
        self.stats = {"calls": 0, "errors": 0, "tokens": 0, "latency_ms_total": 0.0, "judge": 0, "explain": 0}
        self._lock = threading.Lock()

    def _chat(self, system, user, max_tokens) -> tuple[str | None, int]:
        return None, 0

    def chat(self, system, user, max_tokens=100, purpose="judge") -> str | None:
        if self.name == "none":
            return None
        t0 = time.perf_counter()
        try:
            text, tokens = self._chat(system, user, max_tokens)
        except Exception:  # network/timeout: callers fall back
            text, tokens = None, 0
        with self._lock:
            self.stats["calls"] += 1
            self.stats[purpose] = self.stats.get(purpose, 0) + 1
            self.stats["tokens"] += tokens
            self.stats["latency_ms_total"] += (time.perf_counter() - t0) * 1000
            self.stats["errors"] += text is None
        return text

    def judge(self, task: str, snippet: str) -> tuple[bool, str] | None:
        out = self.chat(JUDGE_SYS, f'Task: "{task}"\nWindow: "{snippet}"', max_tokens=30)
        m = _VERDICT.match(out or "")
        if not m:
            return None
        return m.group(1).upper() == "ON", (m.group(2).strip().splitlines() or [""])[0][:80]

    def info(self):
        s = dict(self.stats)
        s["avg_latency_ms"] = round(s["latency_ms_total"] / s["calls"]) if s["calls"] else None
        return {"provider": self.name, "model": self.model, **s}


class OllamaProvider(Provider):
    name = "ollama"

    def __init__(self, base=None, model=None):
        super().__init__()
        self.base = (base or os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")).rstrip("/")
        if urlparse(self.base).hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("Ollama must be on loopback in local mode")
        self.model = model or os.environ.get("OLLAMA_MODEL")

    def _chat(self, system, user, max_tokens):
        if not self.model:
            _, tags = http_json("GET", f"{self.base}/api/tags", timeout=3)
            self.model = (tags or {}).get("models", [{}])[0].get("name") if (tags or {}).get("models") else None
            if not self.model:
                return None, 0
        st, r = http_json("POST", f"{self.base}/api/chat", {
            "model": self.model, "stream": False, "options": {"num_predict": max_tokens},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}, timeout=30)
        if st != 200 or not r:
            return None, 0
        return r.get("message", {}).get("content"), r.get("prompt_eval_count", 0) + r.get("eval_count", 0)


def pick_model(ids: list[str], preferred: str | None = None) -> str | None:
    if preferred and preferred in ids:
        return preferred
    chat = [i for i in ids if not re.search(r"embed|whisper|tts|stable|flux|image|vision", i, re.I)]
    return next((i for i in chat if "instruct" in i.lower()), chat[0] if chat else (ids[0] if ids else None))


class VultrProvider(Provider):
    name = "vultr"
    max_tokens_field = "max_tokens"

    def __init__(self, key=None, base=VULTR_BASE, model=None):
        super().__init__()
        self._key = key or os.environ.get("VULTR_INFERENCE_KEY")
        if not self._key:
            raise ValueError("VULTR_INFERENCE_KEY not set")
        self.base = base.rstrip("/")
        self.model = model
        self.models = []

    def _h(self):
        return {"Authorization": f"Bearer {self._key}"}

    def pick(self):
        """Choose a model from GET /v1/models at startup; never hardcoded."""
        st, r = http_json("GET", f"{self.base}/models", headers=self._h(), timeout=10)
        items = (r or {}).get("data") or (r or {}).get("models") or []
        self.models = [m["id"] if isinstance(m, dict) else m for m in items]
        self.model = pick_model(self.models, os.environ.get("VULTR_MODEL"))
        return self.model

    def _chat(self, system, user, max_tokens):
        if not self.model and not self.pick():
            return None, 0
        st, r = http_json("POST", f"{self.base}/chat/completions", {
            "model": self.model, self.max_tokens_field: max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
            headers=self._h(), timeout=20)
        if st != 200 or not r or not r.get("choices"):
            return None, 0
        return r["choices"][0]["message"]["content"], (r.get("usage") or {}).get("total_tokens", 0)


class OpenAIProvider(VultrProvider):
    """Same OpenAI chat-completions wire format; model from OPENAI_MODEL (its /models list is mostly non-chat)."""
    name = "openai"
    max_tokens_field = "max_completion_tokens"  # newer OpenAI models reject max_tokens

    def __init__(self, key=None, base=None, model=None):
        Provider.__init__(self)
        self._key = key or os.environ.get("OPENAI_API_KEY")
        if not self._key:
            raise ValueError("OPENAI_API_KEY not set")
        self.base = (base or os.environ.get("OPENAI_BASE_URL", OPENAI_BASE)).rstrip("/")
        self.model = model or os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        self.models = []

    def pick(self):
        return self.model


def make_provider(mode: str, name: str | None) -> Provider:
    name = name or ("vultr" if mode == "cloud" else "none")
    if name in ("vultr", "openai"):
        if mode == "local":
            raise ValueError(f"local mode refuses the {name} provider: nothing leaves this machine")
        return OpenAIProvider() if name == "openai" else VultrProvider()
    if name == "ollama":
        return OllamaProvider()
    return Provider()
