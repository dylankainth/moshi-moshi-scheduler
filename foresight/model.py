"""Duration model: log-duration is normal, conjugate normal-inverse-gamma per task type, prior from all types.

Two conjugate regressions per type, sharing one implementation (NIG):
  plain:   log d = a_c + noise                     (no guess; = NIG on mu_c, sigma_c)
  guessed: log d = a_c + b_c (log g - c) + noise   (c = centre of log guesses, decorrelates a and b)
Priors are centred on global (pooled) fits, so a type with no history predicts like "a typical task"
with a wide range, and narrows as its own data arrives. Updates are sufficient-statistic adds.
"""
import math
from dataclasses import dataclass, field
from statistics import NormalDist, fmean, median

_Z = NormalDist()


def t_ppf(p: float, nu: float) -> float:
    # Cornish-Fisher expansion (Abramowitz & Stegun 26.7.5); error < 1e-3 for nu >= 4 at p <= 0.95.
    z = _Z.inv_cdf(p)
    g1 = (z**3 + z) / 4
    g2 = (5 * z**5 + 16 * z**3 + 3 * z) / 96
    g3 = (3 * z**7 + 19 * z**5 + 17 * z**3 - 15 * z) / 384
    g4 = (79 * z**9 + 776 * z**7 + 1482 * z**5 - 1920 * z**3 - 945 * z) / 92160
    return z + g1 / nu + g2 / nu**2 + g3 / nu**3 + g4 / nu**4


def _inv(M):
    if len(M) == 1:
        return [[1 / M[0][0]]]
    (a, b), (c, d) = M
    det = a * d - b * c
    return [[d / det, -b / det], [-c / det, a / det]]


def _mv(M, v):
    return [sum(m * x for m, x in zip(row, v)) for row in M]


def _dot(u, v):
    return sum(a * b for a, b in zip(u, v))


class NIG:
    """beta | s2 ~ N(m0, s2 V0), s2 ~ InvGamma(a0, b0); y = x.beta + N(0, s2)."""

    def __init__(self, m0, V0, a0, b0):
        self.m0, self.L0, self.a0, self.b0 = m0, _inv(V0), a0, b0
        d = len(m0)
        self.XtX = [[0.0] * d for _ in range(d)]
        self.Xty, self.yty, self.n = [0.0] * d, 0.0, 0

    def add(self, x, y):
        for i in range(len(x)):
            self.Xty[i] += x[i] * y
            for j in range(len(x)):
                self.XtX[i][j] += x[i] * x[j]
        self.yty += y * y
        self.n += 1

    def predict(self, x):
        """Posterior predictive Student-t: (loc, scale, df)."""
        Ln = [[l + s for l, s in zip(lr, sr)] for lr, sr in zip(self.L0, self.XtX)]
        Vn = _inv(Ln)
        mn = _mv(Vn, [a + b for a, b in zip(_mv(self.L0, self.m0), self.Xty)])
        an = self.a0 + self.n / 2
        bn = self.b0 + 0.5 * (self.yty + _dot(self.m0, _mv(self.L0, self.m0)) - _dot(mn, _mv(Ln, mn)))
        return _dot(x, mn), math.sqrt(bn / an * (1 + _dot(x, _mv(Vn, x)))), 2 * an


@dataclass
class Task:
    type: str
    actual: float  # minutes, user-confirmed
    guess: float | None = None
    desc: str = ""
    ts: float = 0.0


@dataclass
class Estimate:
    median: float
    lo50: float
    hi50: float
    lo80: float
    hi80: float
    n_type: int  # history behind this type
    used_guess: bool


@dataclass
class Hyper:
    mu0: float = math.log(30)  # hand-set fallbacks used when there's no data
    s2: float = 0.5
    tau2: float = 1.0
    c: float = math.log(30)
    A: float = math.log(30)
    B: float = 1.0
    r2: float = 0.3
    tau_a2: float = 0.1
    a0: float = 3.0


def _var(xs, m):
    return sum((x - m) ** 2 for x in xs) / max(len(xs) - 1, 1)


def fit_hyper(tasks: list[Task]) -> Hyper:
    """Empirical-Bayes moment estimates of the global prior; no MCMC."""
    h = Hyper()
    by = {}
    for t in tasks:
        by.setdefault(t.type, []).append(t)
    groups = [[math.log(t.actual) for t in ts] for ts in by.values()]
    if len(groups) >= 2 and sum(map(len, groups)) > len(groups) + 2:
        means = [fmean(g) for g in groups]
        h.mu0 = fmean(means)
        h.s2 = sum((y - m) ** 2 for g, m in zip(groups, means) for y in g) / (sum(map(len, groups)) - len(groups))
        h.tau2 = max(_var(means, h.mu0) - h.s2 / fmean(map(len, groups)), 0.05)

    g = [t for t in tasks if t.guess]
    if len(g) >= 5:
        h.c = fmean(math.log(t.guess) for t in g)
        x = [math.log(t.guess) - h.c for t in g]
        y = [math.log(t.actual) for t in g]
        h.A = fmean(y)  # x is centred, so OLS intercept is mean(y)
        h.B = _dot(x, y) / _dot(x, x)
        res = {}
        for t, xi, yi in zip(g, x, y):
            res.setdefault(t.type, []).append(yi - h.A - h.B * xi)
        h.r2 = sum(r * r for rs in res.values() for r in rs) / (len(g) - 2)
        rmeans = [fmean(rs) for rs in res.values()]
        h.tau_a2 = max(_var(rmeans, 0.0) - h.r2 / fmean(map(len, res.values())), 0.01) if len(rmeans) > 1 else 0.1
    return h


@dataclass
class Model:
    h: Hyper
    plain: dict = field(default_factory=dict)
    guessed: dict = field(default_factory=dict)

    @classmethod
    def fit(cls, tasks, hyper: Hyper | None = None):
        m = cls(hyper or fit_hyper(tasks))
        for t in tasks:
            m.add(t)
        return m

    def _p(self, c):
        h = self.h
        if c not in self.plain:
            self.plain[c] = NIG([h.mu0], [[h.tau2 / h.s2]], h.a0, (h.a0 - 1) * h.s2)
        return self.plain[c]

    def _g(self, c):
        h = self.h
        if c not in self.guessed:
            self.guessed[c] = NIG([h.A, h.B], [[h.tau_a2 / h.r2, 0], [0, 0.1**2 / h.r2]], h.a0, (h.a0 - 1) * h.r2)
        return self.guessed[c]

    def add(self, t: Task):
        y = math.log(t.actual)
        self._p(t.type).add([1.0], y)
        if t.guess:
            self._g(t.type).add([1.0, math.log(t.guess) - self.h.c], y)

    def predict(self, type_: str, guess: float | None = None) -> Estimate:
        n = self._p(type_).n
        if guess:
            loc, scale, df = self._g(type_).predict([1.0, math.log(guess) - self.h.c])
        else:
            loc, scale, df = self._p(type_).predict([1.0])
        q50, q80 = t_ppf(0.75, df) * scale, t_ppf(0.9, df) * scale
        e = math.exp
        return Estimate(e(loc), e(loc - q50), e(loc + q50), e(loc - q80), e(loc + q80), n, bool(guess))


def bias_factor(tasks: list[Task]) -> float | None:
    """exp(a + (b-1) log g) at the typical (median) guess: 'you take X times what you say'."""
    g = [t for t in tasks if t.guess]
    if len(g) < 3:
        return None
    lg = [math.log(t.guess) for t in g]
    c = fmean(lg)
    x = [v - c for v in lg]
    y = [math.log(t.actual) for t in g]
    A, B = fmean(y), (_dot(x, y) / _dot(x, x) if _dot(x, x) else 1.0)
    typ = math.log(median(t.guess for t in g))
    return math.exp(A + B * (typ - c) - typ)
