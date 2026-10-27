import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "collector"))
import metrics, service
from collector import parse


class T(unittest.TestCase):
    def setUp(self):
        service.chaos.update(error=0.0, slow=0.0)

    def test_chaos_valid(self):
        self.assertIsNone(service.set_chaos({"error": ["0.5"], "slow": ["2"]}))
        self.assertEqual(service.chaos, {"error": 0.5, "slow": 2.0})

    def test_chaos_reset_with_no_params(self):
        service.set_chaos({"error": ["0.5"]})
        self.assertIsNone(service.set_chaos({}))
        self.assertEqual(service.chaos, {"error": 0.0, "slow": 0.0})

    def test_chaos_rejects_bad_input_and_keeps_state(self):
        service.set_chaos({"error": ["0.2"]})
        for q in [{"slow": ["-1"]}, {"slow": ["abc"]}, {"error": ["2"]}, {"error": ["nan"]}, {"slow": ["inf"]}, {"slow": ["99"]}]:
            self.assertIsNotNone(service.set_chaos(q), q)
            self.assertEqual(service.chaos, {"error": 0.2, "slow": 0.0})

    def test_render_parses_back(self):
        c = metrics.Counter("rt_total", "round trip", ["who"])
        h = metrics.Histogram("rt_seconds", "round trip", buckets=(0.1, 1))
        nasty = 'a"b\\c\nd'
        c.inc(3, who=nasty)
        h.observe(0.05); h.observe(0.5); h.observe(5)
        got = {(n, tuple(sorted(l.items()))): v for n, l, v in parse(metrics.render().decode())}
        self.assertEqual(got[("rt_total", (("who", nasty),))], 3.0)
        self.assertEqual(got[("rt_seconds_bucket", (("le", "0.1"),))], 1.0)
        self.assertEqual(got[("rt_seconds_bucket", (("le", "1"),))], 2.0)
        self.assertEqual(got[("rt_seconds_bucket", (("le", "+Inf"),))], 3.0)
        self.assertEqual(got[("rt_seconds_count", ())], 3.0)


if __name__ == "__main__":
    unittest.main()
