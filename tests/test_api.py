import time
import unittest

from tests.helpers import Running


class EstimateAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = Running()

    @classmethod
    def tearDownClass(cls):
        cls.s.close()

    def test_estimate_fast_and_sensible(self):
        t0 = time.perf_counter()
        st, r = self.s.call("POST", "/api/estimate", {"desc": "fix the login bug", "guess": 20})
        dt = time.perf_counter() - t0
        print(f"\nestimate in {dt * 1000:.0f} ms: {r['type']} median {r['estimate']['median']:.0f} min")
        self.assertEqual(st, 200)
        self.assertLess(dt, 1.0)
        self.assertEqual(r["type"], "bugfix")
        e = r["estimate"]
        self.assertTrue(e["lo80"] < e["lo50"] < e["median"] < e["hi50"] < e["hi80"])
        self.assertGreater(e["median"], 20)  # seeded user takes ~1.6x their guess
        self.assertTrue(r["neighbours"] and r["explanation"])

    def test_new_type_is_wide(self):
        _, known = self.s.call("POST", "/api/estimate", {"desc": "review checkout PR"})
        _, new = self.s.call("POST", "/api/estimate", {"desc": "assemble ikea wardrobe"})
        self.assertTrue(new["is_new_type"])
        w = lambda e: e["estimate"]["hi80"] / e["estimate"]["lo80"]
        self.assertGreater(w(new), w(known))

    def test_validation(self):
        self.assertEqual(self.s.call("POST", "/api/estimate", {"desc": ""})[0], 400)
        self.assertEqual(self.s.call("POST", "/api/estimate", {"desc": "x", "guess": -3})[0], 400)
        self.assertEqual(self.s.call("GET", "/api/nope")[0], 404)

    def test_host_header_guard(self):
        st, _ = self.s.call("GET", "/api/health", headers={"Host": "evil.example:80"})
        self.assertEqual(st, 403)

    def test_health(self):
        st, h = self.s.call("GET", "/api/health")
        self.assertEqual((st, h["mode"], h["egress"]), (200, "local", "none"))


if __name__ == "__main__":
    unittest.main()
