"""Plain-language explanation of an estimate: deterministic template, optional LLM rewrite with template fallback."""


def fmt_min(m: float) -> str:
    m = round(m)
    return f"{m} min" if m < 60 else f"{m // 60}h {m % 60:02d}m"


def template(e: dict) -> str:
    est, t = e["estimate"], e["type"]
    s = (f"Plan for about {fmt_min(est['median'])}. Half the time it lands within "
         f"{fmt_min(est['lo50'])}–{fmt_min(est['hi50'])}, and 8 times in 10 within "
         f"{fmt_min(est['lo80'])}–{fmt_min(est['hi80'])}.")
    if e["is_new_type"] or est["n_type"] == 0:
        s += (f" There's nothing like this in your history yet, so the range is borrowed from all your tasks"
              f" and is deliberately wide. It narrows as you finish more \"{t}\" tasks.")
    else:
        s += f" Based on {est['n_type']} past \"{t}\" tasks"
        if e["neighbours"]:
            nb = e["neighbours"][0]
            s += f"; the closest, \"{nb['desc']}\", took {fmt_min(nb['actual'])}"
        s += "."
    if e.get("guess") and e.get("bias"):
        s += (f" You guessed {fmt_min(e['guess'])}; you usually take {e['bias']:.1f}x what you say,"
              f" which is already built into this range.")
    return s


_cache = {}  # same facts -> same rewrite; the UI re-asks on every pause in typing
PROMPT = ("You explain time estimates to a busy person in at most 3 short, friendly sentences. Use only the numbers "
          "given. Never invent numbers. No preamble.")


def explain(e: dict, provider=None) -> tuple[str, str]:
    """Returns (text, source) where source is 'template' or the provider name."""
    base = template(e)
    if provider is None or provider.name == "none":
        return base, "template"
    facts = {k: e[k] for k in ("type", "guess", "bias", "is_new_type")} | {
        "estimate_minutes": {k: round(v) for k, v in e["estimate"].items() if k in ("median", "lo50", "hi50", "lo80", "hi80")},
        "past_tasks_of_type": e["estimate"]["n_type"],
        "closest_past": [{"desc": n["desc"], "minutes": round(n["actual"])} for n in e["neighbours"][:2]],
        "draft": base}
    msg = f"Rewrite this estimate explanation in plain words.\nFacts: {facts}"
    key = (provider.name, provider.model, msg)
    out = _cache.get(key) or (provider.chat(PROMPT, msg, max_tokens=160, purpose="explain") or "").strip()
    if not out:
        return base, "template"
    if len(_cache) > 512:  # ponytail: crude bound, an LRU if hit rate ever matters
        _cache.clear()
    _cache[key] = out
    return out, provider.name
