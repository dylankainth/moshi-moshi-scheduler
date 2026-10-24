"""Synthetic user with known parameters: seed history, demo user, and model test fixture."""
import math
import random
import time

from .model import Task

# type: (median guess in minutes, sd of log(actual/guess) noise, description templates)
TYPES = {
    "bugfix": (30, 0.40, ["fix the {x} bug", "debug {x} crash", "{x} returns wrong result", "fix flaky {x} test"]),
    "feature": (90, 0.45, ["add {x} support", "build {x} page", "implement {x} endpoint", "new {x} option"]),
    "code review": (20, 0.30, ["review {x} PR", "review changes to {x}", "look over {x} pull request"]),
    "writing": (45, 0.40, ["write {x} docs", "draft {x} blog post", "update README for {x}", "write {x} report"]),
    "admin": (15, 0.35, ["answer emails about {x}", "file {x} expenses", "sort out {x} invoices", "book {x}"]),
    "meeting prep": (25, 0.30, ["prep slides for {x}", "agenda for {x} sync", "prepare {x} demo"]),
}
THINGS = ["login", "checkout", "search", "billing", "onboarding", "dashboard", "export", "notifications",
          "settings", "upload", "API", "auth", "calendar", "profile", "payments"]


def simulate(n=300, bias=1.6, seed=0, types=TYPES, guess_rate=0.8, guess_sd=0.5, end=None) -> list[Task]:
    """log g ~ N(log median_c, guess_sd); log d = log g + log(bias) + N(0, sd_c). So b = 1 and a = log(bias)."""
    rng = random.Random(seed)
    names = list(types)
    end = end or time.time()
    out = []
    for i in range(n):
        c = rng.choice(names)
        med, sd, tmpl = types[c]
        g = max(5, round(med * math.exp(rng.gauss(0, guess_sd)) / 5) * 5)
        actual = round(g * bias * math.exp(rng.gauss(0, sd)), 1)
        out.append(Task(c, max(actual, 1.0), g if rng.random() < guess_rate else None,
                        rng.choice(tmpl).format(x=rng.choice(THINGS)), end - (n - i) * 4 * 3600))
    return out
