"""Text embeddings. Default: deterministic offline feature hashing (words + char trigrams).
Optional: FORESIGHT_EMBEDDER=st uses sentence-transformers if installed (local model, no network at runtime
once the model is cached)."""
import hashlib
import math
import os
import re
from functools import lru_cache

DIM = 512
_WORD = re.compile(r"[a-z0-9]+")
STOP = set("a an the to of for and or in on at with my our your is be it this that from by as".split())
STOP |= {"google", "chrome", "microsoft", "edge", "mozilla", "firefox", "visual", "studio", "code", "exe"}


def words(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if w not in STOP and len(w) > 1]


@lru_cache(maxsize=4096)
def hash_embed(text: str) -> tuple[float, ...]:
    v = [0.0] * DIM
    ws = words(text)
    feats = [(w, 1.0) for w in ws] + [(f"#{w[i:i + 3]}", 0.4) for w in ws if len(w) > 3 for i in range(len(w) - 2)]
    for f, wt in feats:
        h = int.from_bytes(hashlib.blake2b(f.encode(), digest_size=8).digest(), "little")
        v[h % DIM] += wt if (h >> 40) & 1 else -wt
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return tuple(x / n for x in v)


def cosine(a, b) -> float:
    return sum(x * y for x, y in zip(a, b))


class _ST:
    def __init__(self):
        from sentence_transformers import SentenceTransformer  # optional
        self.m = SentenceTransformer(os.environ.get("FORESIGHT_ST_MODEL", "all-MiniLM-L6-v2"))

    @lru_cache(maxsize=4096)
    def __call__(self, text):
        return tuple(self.m.encode(text, normalize_embeddings=True).tolist())


def get_embedder():
    if os.environ.get("FORESIGHT_EMBEDDER") == "st":
        return _ST()
    return hash_embed
