"""Match a new task description to a task type via nearest labelled past tasks."""
from .embed import cosine, words

NEW_TYPE_SIM = 0.25  # below this, nothing in history looks alike: start a new type


def match(desc: str, past: list[dict], embed, k=7):
    """past: dicts with desc, type (+ anything). Returns (type, neighbours[with sim], is_new)."""
    q = embed(desc)
    scored = sorted(({**p, "sim": cosine(q, embed(p["desc"]))} for p in past), key=lambda p: -p["sim"])
    top = scored[:k]
    if not top or top[0]["sim"] < NEW_TYPE_SIM:
        ws = words(desc)
        return (ws[0] if ws else "other"), top[:3], True
    votes = {}
    for p in top:
        votes[p["type"]] = votes.get(p["type"], 0) + p["sim"]
    t = max(votes, key=votes.get)
    return t, [p for p in top if p["type"] == t][:4], False
