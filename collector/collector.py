"""Collector: scrapes /metrics endpoints, stores time series in memory, answers queries, evaluates alerts.

A query is a small JSON spec:
  {"fn": "rate", "metric": "x_total", "match": {"status": "500"}, "by": ["tool"], "window": 60}
  {"fn": "last", "metric": "up", "window": 15}
  {"fn": "ratio", "num": <spec>, "den": <spec>}
  {"fn": "quantile", "q": 0.95, "metric": "x_seconds_bucket", "window": 60}
"""
import json, os, re, threading, time, urllib.request
from bisect import bisect_left, bisect_right
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

_t = lambda p: p[0]
LINE = re.compile(r"^([a-zA-Z_:][\w:]*)(?:\{(.*)\})?\s+(\S+)")
LABEL = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')


UNESC = re.compile(r"\\(.)")


def _unesc(v):
    return UNESC.sub(lambda m: "\n" if m.group(1) == "n" else m.group(1), v)


def parse(text):
    """Yield (name, labels, value) from Prometheus text format."""
    for line in text.splitlines():
        m = LINE.match(line)
        if m:
            yield m.group(1), {k: _unesc(v) for k, v in LABEL.findall(m.group(2) or "")}, float(m.group(3))


class Store:
    """series[name][sorted label tuple] = list of (t, v), oldest first. Lists, so bisect finds a window fast."""

    def __init__(self, retention=3600):
        self.retention, self.series, self.lock = retention, defaultdict(dict), threading.Lock()

    def add(self, name, labels, t, v):
        with self.lock:
            pts = self.series[name].setdefault(tuple(sorted(labels.items())), [])
            pts.append((t, v))
            old = bisect_left(pts, t - self.retention, key=_t)
            if old:
                del pts[:old]

    def prune(self, now):
        """Drop series that have not reported within the retention period."""
        with self.lock:
            for name in list(self.series):
                by = self.series[name]
                for k in [k for k, p in by.items() if p[-1][0] < now - self.retention]:
                    del by[k]
                if not by:
                    del self.series[name]

    def names(self):
        """Metric names with the label keys seen on each."""
        with self.lock:
            return [{"name": n, "labels": sorted({k for lab in by for k, _ in lab})} for n, by in sorted(self.series.items())]

    def window(self, name, t0, t1, match=None):
        """Yield (labels, points in [t0, t1]) for series of `name` whose labels contain every pair in `match`."""
        out = []
        with self.lock:
            for key, pts in self.series.get(name, {}).items():
                if match and any((k, v) not in key for k, v in match.items()):
                    continue
                sel = pts[bisect_left(pts, t0, key=_t):bisect_right(pts, t1, key=_t)]
                if sel:
                    out.append((dict(key), sel))
        return out


def _rate(pts):
    """Per-second increase of a counter, tolerating resets. None if under two points."""
    if len(pts) < 2:
        return None
    inc, prev = 0.0, pts[0][1]
    for _, v in pts[1:]:
        inc += v - prev if v >= prev else v
        prev = v
    dt = pts[-1][0] - pts[0][0]
    return inc / dt if dt > 0 else None


def _series(store, spec, t):
    return store.window(spec["metric"], t - spec.get("window", 60), t, spec.get("match"))


def _key(spec, labels):
    return tuple((b, labels.get(b, "")) for b in spec.get("by", []))


def evaluate(store, spec, t):
    """Return {group: value}, where group is a tuple of (label, value) pairs."""
    fn, out = spec["fn"], defaultdict(float)
    if fn == "rate":
        for labels, pts in _series(store, spec, t):
            r = _rate(pts)
            if r is not None:
                out[_key(spec, labels)] += r
    elif fn == "last":
        for labels, pts in _series(store, spec, t):
            out[_key(spec, labels)] += pts[-1][1]
    elif fn == "ratio":
        num, den = evaluate(store, spec["num"], t), evaluate(store, spec["den"], t)
        # a missing numerator means zero events, not "no data"
        return {k: num.get(k, 0.0) / d for k, d in den.items() if d > 0}
    elif fn == "quantile":
        by_le = defaultdict(float)
        for labels, pts in _series(store, spec, t):
            r = _rate(pts)
            if r is not None:
                by_le[float(labels["le"])] += r
        les = sorted(by_le)
        if not les or by_le[les[-1]] <= 0:
            return {}
        rank, prev_le, prev_c = spec["q"] * by_le[les[-1]], 0.0, 0.0
        for le in les:
            c = by_le[le]
            if c >= rank:
                if le == float("inf"):
                    return {(): prev_le}  # beyond last finite bucket: report its bound
                return {(): le if c == prev_c else prev_le + (le - prev_le) * (rank - prev_c) / (c - prev_c)}
            prev_le, prev_c = le, c
    else:
        raise ValueError(f"unknown fn {fn!r}")
    return dict(out)


MAX_STEPS = 2000
OPS = {">": lambda a, b: a > b, "<": lambda a, b: a < b}


class Alerts:
    """Each rule moves inactive -> pending -> firing once its condition has held for `for` seconds."""

    def __init__(self, rules):
        self.rules = rules
        self.state = {r["alert"]: {"state": "inactive", "since": None, "value": None} for r in rules}

    def evaluate(self, store, now):
        for r in self.rules:
            values = evaluate(store, r["expr"], now)
            hits = [v for v in values.values() if OPS[r["op"]](v, r["threshold"])]
            s = self.state[r["alert"]]
            s["value"] = max(values.values()) if values else None
            if not hits:
                s.update(state="inactive", since=None)
            elif s["since"] is None:
                s.update(state="pending", since=now)
            if hits and now - s["since"] >= r.get("for", 0):
                s["state"] = "firing"

    def snapshot(self):
        return [{"alert": r["alert"], "summary": r.get("summary", ""), "op": r["op"], "threshold": r["threshold"],
                 "unit": r.get("unit", ""), **self.state[r["alert"]]} for r in self.rules]


def scrape(store, url, now):
    try:
        text = urllib.request.urlopen(url, timeout=3).read().decode()
        samples, up = list(parse(text)), 1.0
    except (OSError, ValueError):  # unreachable, or a body that is not valid metrics text
        samples, up = [], 0.0
    store.add("up", {"instance": url}, now, up)
    for name, labels, v in samples:
        store.add(name, labels, now, v)


def tick(store, alerts, targets, now):
    """One scrape-and-evaluate cycle. A bad target or rule must never kill the loop."""
    for t in targets:
        try:
            scrape(store, t, now)
        except Exception as e:
            print(f"scrape {t} failed: {e!r}", flush=True)
    store.prune(now)
    try:
        alerts.evaluate(store, now)
    except Exception as e:
        print(f"alert evaluation failed: {e!r}", flush=True)


def make_handler(store, alerts):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"  # keep-alive; every reply sends Content-Length

        def send_json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url, q = urlparse(self.path), parse_qs(urlparse(self.path).query)
            if url.path == "/health":
                return self.send_json({"ok": True})
            if url.path == "/api/metrics":
                return self.send_json(store.names())
            if url.path == "/api/alerts":
                return self.send_json(alerts.snapshot())
            if url.path == "/api/query_range":
                try:
                    spec, end = json.loads(q["spec"][0]), float(q.get("end", [time.time()])[0])
                    span, step = float(q.get("span", [300])[0]), float(q.get("step", [5])[0])
                    if not (step > 0 and 0 <= span / step <= MAX_STEPS):
                        raise ValueError(f"need step > 0 and at most {MAX_STEPS} steps")
                    series, t = defaultdict(list), end - span
                    while t <= end:
                        for key, v in evaluate(store, spec, t).items():
                            series[key].append([t, v])
                        t += step
                    return self.send_json([{"labels": dict(k), "points": p} for k, p in series.items()])
                except (KeyError, ValueError, TypeError, AttributeError) as e:
                    return self.send_json({"error": str(e)}, 400)
            self.send_json({"error": "not found"}, 404)

        def log_message(self, *a):
            pass

    return H


def main():
    targets = os.environ.get("COLLECTOR_TARGETS", "http://app:8000/metrics").split(",")
    interval = float(os.environ.get("COLLECTOR_INTERVAL", 5))
    store = Store()
    alerts = Alerts(json.load(open(os.path.join(os.path.dirname(__file__), "alerts.json"))))

    def loop():
        while True:
            tick(store, alerts, targets, time.time())
            time.sleep(interval)

    threading.Thread(target=loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 9090), make_handler(store, alerts)).serve_forever()


if __name__ == "__main__":
    main()
