import math
import unittest

from foresight.model import Model, bias_factor, fit_hyper, t_ppf
from foresight.seed import simulate


class ModelTests(unittest.TestCase):
    def test_t_ppf(self):
        for p, nu, want in [(0.9, 4, 1.533206), (0.9, 10, 1.372184), (0.75, 4, 0.740697), (0.75, 30, 0.682756)]:
            self.assertAlmostEqual(t_ppf(p, nu), want, delta=2e-3)

    def test_bias_recovered(self):
        b = bias_factor(simulate(n=500, bias=1.6, seed=3))
        self.assertAlmostEqual(b, 1.6, delta=0.1)
        self.assertAlmostEqual(bias_factor(simulate(n=500, bias=1.0, seed=4)), 1.0, delta=0.07)

    def test_coverage_online(self):
        # Prior from one simulated user, then predict-before-update on a fresh user with the same habits.
        m = Model(fit_hyper(simulate(n=300, seed=1)))
        hit50 = hit80 = 0
        test = simulate(n=400, seed=2)
        for t in test:
            e = m.predict(t.type, t.guess)
            hit50 += e.lo50 <= t.actual <= e.hi50
            hit80 += e.lo80 <= t.actual <= e.hi80
            m.add(t)
        c50, c80 = hit50 / len(test), hit80 / len(test)
        print(f"\ncoverage 50%={c50:.3f} 80%={c80:.3f}")
        self.assertTrue(0.42 <= c50 <= 0.58, c50)
        self.assertTrue(0.73 <= c80 <= 0.87, c80)

    def test_new_type_borrows_then_narrows(self):
        m = Model.fit(simulate(n=300, seed=5))
        before = m.predict("research")
        self.assertEqual(before.n_type, 0)
        self.assertTrue(5 < before.median < 300, before)
        research = {"research": (60, 0.25, ["research {x} options"])}
        for t in simulate(n=10, seed=6, types=research, guess_sd=0.3):
            m.add(t)
        after = m.predict("research")
        w = lambda e: math.log(e.hi80 / e.lo80)
        print(f"\n80% width x{math.exp(w(before)):.1f} -> x{math.exp(w(after)):.1f}")
        self.assertLess(w(after), 0.8 * w(before))

    def test_guess_tightens(self):
        m = Model.fit(simulate(n=300, seed=7))
        plain, guessed = m.predict("bugfix"), m.predict("bugfix", 30)
        self.assertLess(guessed.hi80 / guessed.lo80, plain.hi80 / plain.lo80)
        self.assertAlmostEqual(guessed.median / 30, 1.6, delta=0.25)


if __name__ == "__main__":
    unittest.main()
