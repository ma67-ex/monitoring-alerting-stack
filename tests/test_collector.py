import json, os, sys, threading, unittest, urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "collector"))
import collector
from collector import Store, Alerts, webhook_notifier, email_notifier, ntfy_notifier, probe, notifiers_from_env, parse, evaluate, deliver, save_snapshot, load_snapshot, RETRY_DELAYS, scrape, tick, make_handler, _rate


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

    def test_prune_drops_silent_series(self):
        s = Store(retention=100)
        s.add("old", {}, 0, 1)
        s.add("live", {}, 150, 1)
        s.prune(150)
        self.assertEqual([n["name"] for n in s.names()], ["live"])

    def test_window_bounds_match_and_retention(self):
        s = Store(retention=100)
        for t in range(0, 301, 10):
            s.add("m", {"k": "a"}, t, t); s.add("m", {"k": "b"}, t, t)
        got = s.window("m", 250, 270, {"k": "a"})
        self.assertEqual(got, [({"k": "a"}, [(250, 250), (260, 260), (270, 270)])])
        self.assertEqual(s.window("m", 0, 190), [])  # trimmed: oldest kept point is 200
        self.assertEqual(s.window("nope", 0, 300), [])

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

    def test_notifies_once_on_firing_and_once_on_resolve(self):
        sent, s = [], Store()
        a = Alerts([{"alert": "Down", "summary": "x", "expr": {"fn": "last", "metric": "up", "window": 15},
                     "op": "<", "threshold": 1, "for": 30}], sent.append)
        for t, v in [(0, 1.0), (10, 0.0), (30, 0.0), (41, 0.0), (45, 0.0), (50, 1.0), (55, 1.0)]:
            s.add("up", {}, t, v); a.evaluate(s, t)
        self.assertEqual([(e["alert"], e["status"], e["time"]) for e in sent], [("Down", "firing", 41), ("Down", "resolved", 50)])

    def test_webhook_posts_json_and_survives_failure(self):
        got, done = [], threading.Event()
        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200); self.send_header("Content-Length", "0"); self.end_headers(); done.set()
            def log_message(self, *a): pass
        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            webhook_notifier(f"http://127.0.0.1:{srv.server_port}/")({"alert": "A", "status": "firing"})
            self.assertTrue(done.wait(3)); self.assertEqual(got, [{"alert": "A", "status": "firing"}])
        finally:
            srv.shutdown()
        webhook_notifier("http://127.0.0.1:1/")({"alert": "A", "status": "firing"})  # unreachable: must not raise

    def test_email_notifier_sends_subject_and_body(self):
        done, smtp = threading.Event(), mock.MagicMock()
        smtp.__enter__.return_value = smtp
        smtp.send_message.side_effect = lambda m: done.set()
        with mock.patch.object(collector.smtplib, "SMTP", return_value=smtp) as cls:
            email_notifier("h", 587, "u", "p", "a@x", "b@x")(
                {"alert": "Down", "status": "firing", "summary": "app down", "value": 0.0})
            self.assertTrue(done.wait(3))
        cls.assert_called_once_with("h", 587, timeout=10)
        smtp.starttls.assert_called_once(); smtp.login.assert_called_once_with("u", "p")
        msg = smtp.send_message.call_args[0][0]
        self.assertEqual((msg["Subject"], msg["To"], msg["From"]), ("[FIRING] Down", "b@x", "a@x"))
        self.assertIn("app down", msg.get_content())

    def test_email_failure_does_not_raise(self):
        tried = threading.Event()
        def boom(*a, **k):
            tried.set(); raise OSError
        with mock.patch.object(collector.smtplib, "SMTP", side_effect=boom):
            email_notifier("h", 587, "u", "p", "a@x", "b@x")({"alert": "A", "status": "firing", "summary": "", "value": 1})
            self.assertTrue(tried.wait(3))  # the thread ran and swallowed the error

    def test_notifiers_from_env(self):
        self.assertIsNone(notifiers_from_env({}))
        self.assertIsNotNone(notifiers_from_env({"ALERT_EMAIL_TO": "b@x", "SMTP_USER": "u", "SMTP_PASS": "p"}))

    def _probe(self, **urlopen_kw):
        s = Store()
        with mock.patch.object(collector.urllib.request, "urlopen", **urlopen_kw):
            probe(s, "http://h", 1.0)
        return evaluate(s, {"fn": "last", "metric": "probe_up"}, 1.0)[()]

    def test_probe_up_only_for_200_healthy_or_warning(self):
        ok = lambda body: dict(return_value=mock.Mock(read=lambda: body))
        self.assertEqual(self._probe(**ok(b'{"status":"HEALTHY"}')), 1.0)
        self.assertEqual(self._probe(**ok(b'{"status":"WARNING"}')), 1.0)
        self.assertEqual(self._probe(**ok(b"ok")), 1.0)
        self.assertEqual(self._probe(**ok(b'{"status":"UNHEALTHY"}')), 0.0)
        self.assertEqual(self._probe(**ok(b'{"status":"REDIS_DOWN"}')), 0.0)
        self.assertEqual(self._probe(side_effect=urllib.error.HTTPError("http://h", 503, "x", {}, None)), 0.0)
        self.assertEqual(self._probe(side_effect=OSError), 0.0)

    def test_probe_down_rule_fires_per_url_then_resolves(self):
        sent, s = [], Store()
        a = Alerts(json.load(open(os.path.join(os.path.dirname(collector.__file__), "alerts.json"))), sent.append)
        for t, up in [(0, 1.0), (60, 0.0), (120, 0.0), (180, 0.0), (240, 1.0)]:
            s.add("probe_up", {"url": "http://h"}, t, up); a.evaluate(s, t)
        self.assertEqual([(e["alert"], e["status"]) for e in sent if e["alert"] == "ProbeDown"],
                         [("ProbeDown", "firing"), ("ProbeDown", "resolved")])

    def test_between_probes_last_result_is_replayed_without_a_request(self):
        s = Store()
        with mock.patch.object(collector.urllib.request, "urlopen", side_effect=OSError) as uo:
            tick(s, Alerts([]), [], 0.0, ["http://h"], True)           # probe runs, host is down
            tick(s, Alerts([]), [], 5.0, ["http://h"], False)          # not due: no request, result carried
            tick(s, Alerts([]), [], 5000.0, ["http://h"], False)
        self.assertEqual(uo.call_count, 1)
        self.assertEqual(evaluate(s, {"fn": "last", "metric": "probe_up", "window": 15}, 5000.0), {(): 0.0})

    def test_ntfy_posts_title_priority_and_body(self):
        done, req = threading.Event(), []
        with mock.patch.object(collector.urllib.request, "urlopen", side_effect=lambda r, timeout: (req.append(r), done.set(), mock.Mock())[2]):
            ntfy_notifier("https://ntfy.example/", "t0pic")({"alert": "ProbeDown", "status": "firing", "summary": "down", "value": 0.0})
            self.assertTrue(done.wait(3))
        r = req[0]
        self.assertEqual(r.full_url, "https://ntfy.example/t0pic")
        self.assertEqual((r.get_header("Title"), r.get_header("Priority")), ("[FIRING] ProbeDown", "high"))
        self.assertIn(b"down", r.data)

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

    def test_deliver_retries_then_succeeds(self):
        calls, slept = [], []
        def send(event):
            calls.append(event)
            if len(calls) < 3:
                raise OSError("down")
        self.assertTrue(deliver("t", {"alert": "A", "status": "firing"}, send, slept.append))
        self.assertEqual((len(calls), slept), (3, [1, 5]))

    def test_deliver_gives_up_after_the_last_retry_without_raising(self):
        slept = []
        def send(event):
            raise OSError("down")
        with mock.patch("builtins.print") as printed:
            self.assertFalse(deliver("webhook", {"alert": "A", "status": "firing"}, send, slept.append))
        self.assertEqual(slept, list(RETRY_DELAYS))
        self.assertIn("failed after 4 tries", printed.call_args[0][0])

    def test_webhook_is_retried_until_the_receiver_recovers(self):
        got, done, hits = [], threading.Event(), []
        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                hits.append(1)
                code = 500 if len(hits) < 3 else 200  # two failures, then it works
                if code == 200:
                    got.append(json.loads(body)); done.set()
                self.send_response(code); self.send_header("Content-Length", "0"); self.end_headers()
            def log_message(self, *a): pass
        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            webhook_notifier(f"http://127.0.0.1:{srv.server_port}/", sleep=lambda s: None)({"alert": "A", "status": "firing"})
            self.assertTrue(done.wait(5))
            self.assertEqual((len(hits), got), (3, [{"alert": "A", "status": "firing"}]))
        finally:
            srv.shutdown()

    def test_silenced_alert_still_fires_on_the_dashboard_but_sends_nothing(self):
        sent, s = [], Store()
        rule = lambda name: {"alert": name, "summary": "x", "expr": {"fn": "last", "metric": "up", "window": 15}, "op": "<", "threshold": 1}
        a = Alerts([rule("Quiet"), rule("Loud")], sent.append, silenced=["Quiet"])
        s.add("up", {}, 0, 0.0); a.evaluate(s, 0)
        self.assertEqual([e["alert"] for e in sent], ["Loud"])
        snap = {x["alert"]: x for x in a.snapshot()}
        self.assertEqual((snap["Quiet"]["state"], snap["Quiet"]["silenced"], snap["Loud"]["silenced"]), ("firing", True, False))

    def _down_rule(self):
        return {"alert": "Down", "summary": "x", "expr": {"fn": "last", "metric": "up", "window": 15}, "op": "<", "threshold": 1}

    def test_snapshot_round_trip_keeps_series_and_does_not_repage_a_firing_alert(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "collector.json")
            s, a = Store(), Alerts([self._down_rule()])
            s.add("up", {"instance": "http://app"}, 100, 0.0)
            s.add("up", {"instance": "http://app"}, 110, 0.0)
            s.add("reqs", {"status": "500", "tool": 'a"b'}, 110, 7.0)
            a.evaluate(s, 110)
            self.assertEqual(a.state["Down"]["state"], "firing")
            save_snapshot(path, s, a, 110)
            self.assertEqual(os.listdir(d), ["collector.json"])  # the temp file was renamed away
            sent, s2 = [], Store()
            a2 = Alerts([self._down_rule()], sent.append)
            self.assertEqual(load_snapshot(path, s2, a2, 120), 2)
            self.assertEqual(s2.window("up", 0, 200, {"instance": "http://app"})[0][1], [(100, 0.0), (110, 0.0)])
            self.assertEqual(s2.window("reqs", 0, 200, {"tool": 'a"b'})[0][0], {"status": "500", "tool": 'a"b'})
            self.assertEqual(a2.state["Down"]["state"], "firing")
            s2.add("up", {"instance": "http://app"}, 120, 0.0); a2.evaluate(s2, 120)
            self.assertEqual(sent, [])  # still down after the restart: no second "firing" page

    def test_snapshot_load_drops_old_points_unknown_alerts_and_bad_files(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "c.json")
            s, a = Store(retention=60), Alerts([self._down_rule()])
            s.add("m", {}, 0, 1.0); s.add("m", {}, 100, 2.0)
            a.state["Down"].update(state="firing", since=5, value=0.0)
            save_snapshot(path, s, a, 100)
            s2, a2 = Store(retention=60), Alerts([dict(self._down_rule(), alert="Renamed")])
            self.assertEqual(load_snapshot(path, s2, a2, 100), 1)
            self.assertEqual(s2.window("m", 0, 200)[0][1], [(100, 2.0)])  # the point from t=0 is older than the retention
            self.assertEqual(a2.state["Renamed"]["state"], "inactive")  # the saved "Down" rule no longer exists
            with open(path, "w") as f:
                f.write("{not json")
            self.assertEqual(load_snapshot(path, Store(), Alerts([]), 100), 0)
            self.assertEqual(load_snapshot(os.path.join(d, "missing.json"), Store(), Alerts([]), 100), 0)
            with open(path, "w") as f:
                json.dump({"series": [["m", "not a label list", [[1, 2]]]]}, f)
            self.assertEqual(load_snapshot(path, Store(), Alerts([]), 100), 0)


if __name__ == "__main__":
    unittest.main()
