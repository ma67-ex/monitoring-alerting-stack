import json, os, sys, threading, unittest, urllib.error, urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "collector"))
import collector
from collector import Store, Alerts, parse, evaluate, scrape, tick, make_handler, _rate


def counter(store, name, labels, start, per_sec, n=13, dt=5):
    for i in range(n):
        store.add(name, labels, start + i * dt, per_sec * i * dt)


class T(unittest.TestCase):
    def test_parse(self):
        got = list(parse('# HELP x y\nx_total{a="1",b="two"} 3\nup 1\n'))
        self.assertEqual(got, [("x_total", {"a": "1", "b": "two"}, 3.0), ("up", {}, 1.0)])

    def test_rate_handles_counter_reset(self):
        self.assertAlmostEqual(_rate([(0, 10), (5, 20), (10, 5), (15, 15)]), 25 / 15)

    def test_rate_needs_two_points(self):
        self.assertIsNone(_rate([(0, 1)]))

    def test_ratio_missing_numerator_is_zero(self):
        s = Store()
        counter(s, "r", {"status": "200"}, 0, 2)
        spec = {"fn": "ratio", "num": {"fn": "rate", "metric": "r", "match": {"status": "500"}},
                "den": {"fn": "rate", "metric": "r"}}
        self.assertEqual(evaluate(s, spec, 60), {(): 0.0})

    def test_ratio(self):
        s = Store()
        counter(s, "r", {"status": "200"}, 0, 3)
        counter(s, "r", {"status": "500"}, 0, 1)
        spec = {"fn": "ratio", "num": {"fn": "rate", "metric": "r", "match": {"status": "500"}},
                "den": {"fn": "rate", "metric": "r"}}
        self.assertAlmostEqual(evaluate(s, spec, 60)[()], 0.25)

    def test_quantile(self):
        s = Store()
        # 100 requests per 60s: 50 under 0.1s, 90 under 0.5s, all under 1s
        for le, per in [("0.1", 50), ("0.5", 90), ("1", 100), ("+Inf", 100)]:
            counter(s, "h_bucket", {"le": le}, 0, per / 60)
        v = evaluate(s, {"fn": "quantile", "q": 0.95, "metric": "h_bucket"}, 60)[()]
        self.assertAlmostEqual(v, 0.75)  # halfway through the 0.5..1 bucket

    def test_names(self):
        s = Store()
        s.add("a", {"x": "1", "y": "2"}, 0, 1)
        s.add("a", {"x": "3"}, 0, 1)
        s.add("b", {}, 0, 1)
        self.assertEqual(s.names(), [{"name": "a", "labels": ["x", "y"]}, {"name": "b", "labels": []}])

    def test_group_by(self):
        s = Store()
        counter(s, "t", {"tool": "a"}, 0, 1)
        counter(s, "t", {"tool": "b"}, 0, 2)
        out = evaluate(s, {"fn": "rate", "metric": "t", "by": ["tool"]}, 60)
        self.assertAlmostEqual(out[(("tool", "b"),)], 2.0)

    def test_alert_lifecycle(self):
        s = Store()
        a = Alerts([{"alert": "Down", "expr": {"fn": "last", "metric": "up", "window": 15},
                     "op": "<", "threshold": 1, "for": 30}])
        state = lambda: a.state["Down"]["state"]
        s.add("up", {}, 0, 1.0); a.evaluate(s, 0);   self.assertEqual(state(), "inactive")
        s.add("up", {}, 10, 0.0); a.evaluate(s, 10); self.assertEqual(state(), "pending")
        s.add("up", {}, 30, 0.0); a.evaluate(s, 30); self.assertEqual(state(), "pending")
        s.add("up", {}, 41, 0.0); a.evaluate(s, 41); self.assertEqual(state(), "firing")
        s.add("up", {}, 50, 1.0); a.evaluate(s, 50); self.assertEqual(state(), "inactive")

    def test_rate_same_timestamp_is_none(self):
        self.assertIsNone(_rate([(5, 1), (5, 2)]))

    def test_scrape_garbage_body_marks_target_down(self):
        s = Store()
        resp = mock.Mock(); resp.read.return_value = b"x_total notanumber\n"
        with mock.patch.object(collector.urllib.request, "urlopen", return_value=resp):
            scrape(s, "http://t", 1.0)
        self.assertEqual(evaluate(s, {"fn": "last", "metric": "up"}, 1.0), {(): 0.0})

    def test_tick_survives_bad_alert_rule(self):
        s = Store()
        s.add("h_bucket", {"le": "oops"}, 0, 1); s.add("h_bucket", {"le": "oops"}, 5, 2)
        a = Alerts([{"alert": "Bad", "expr": {"fn": "quantile", "q": 0.9, "metric": "h_bucket"},
                     "op": ">", "threshold": 1}])
        with mock.patch.object(collector.urllib.request, "urlopen", side_effect=OSError):
            tick(s, a, ["http://t"], 6.0)  # must not raise

    def _get(self, path):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Store(), Alerts([])))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            return urllib.request.urlopen(f"http://127.0.0.1:{srv.server_port}{path}", timeout=3)
        except urllib.error.HTTPError as e:
            return e
        finally:
            srv.shutdown()

    def test_query_range_rejects_zero_step(self):
        spec = urllib.request.quote(json.dumps({"fn": "last", "metric": "up"}))
        self.assertEqual(self._get(f"/api/query_range?spec={spec}&step=0").code, 400)

    def test_query_range_rejects_too_many_steps(self):
        spec = urllib.request.quote(json.dumps({"fn": "last", "metric": "up"}))
        self.assertEqual(self._get(f"/api/query_range?spec={spec}&span=1e9&step=1").code, 400)

    def test_query_range_rejects_non_object_spec(self):
        self.assertEqual(self._get("/api/query_range?spec=%5B1%5D").code, 400)


if __name__ == "__main__":
    unittest.main()
