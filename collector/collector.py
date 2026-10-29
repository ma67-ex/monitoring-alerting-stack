"""Collector: scrapes /metrics endpoints, stores time series in memory, answers queries, evaluates alerts.

A query is a small JSON spec:
  {"fn": "rate", "metric": "x_total", "match": {"status": "500"}, "by": ["tool"], "window": 60}
  {"fn": "last", "metric": "up", "window": 15}
  {"fn": "ratio", "num": <spec>, "den": <spec>}
  {"fn": "quantile", "q": 0.95, "metric": "x_seconds_bucket", "window": 60}
"""
import json, os, re, signal, smtplib, sys, threading, time, urllib.request
from email.message import EmailMessage
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
        self.last_probe = {}  # url -> last probe result, replayed between probes

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

    def __init__(self, rules, notify=None, silenced=()):
        self.rules, self.notify, self.silenced = rules, notify, set(silenced)  # a silenced alert still shows its state, it just sends nothing
        self.state = {r["alert"]: {"state": "inactive", "since": None, "value": None} for r in rules}

    def evaluate(self, store, now):
        for r in self.rules:
            values = evaluate(store, r["expr"], now)
            hits = [v for v in values.values() if OPS[r["op"]](v, r["threshold"])]
            s = self.state[r["alert"]]
            was = s["state"]
            s["value"] = max(values.values()) if values else None
            if not hits:
                s.update(state="inactive", since=None)
            elif s["since"] is None:
                s.update(state="pending", since=now)
            if hits and now - s["since"] >= r.get("for", 0):
                s["state"] = "firing"
            if self.notify and r["alert"] not in self.silenced and (was == "firing") != (s["state"] == "firing"):
                self.notify({"alert": r["alert"], "status": "firing" if s["state"] == "firing" else "resolved",
                             "summary": r.get("summary", ""), "value": s["value"], "time": now})

    def snapshot(self):
        return [{"alert": r["alert"], "summary": r.get("summary", ""), "op": r["op"], "threshold": r["threshold"],
                 "unit": r.get("unit", ""), "silenced": r["alert"] in self.silenced, **self.state[r["alert"]]} for r in self.rules]


RETRY_DELAYS = (1, 5, 20)  # seconds to wait between attempts: four tries in all, then the failure is logged and dropped


def deliver(label, event, send, sleep=time.sleep):
    """Run send(event), retrying after a failure with the delays above. Never raises; True once it went through."""
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            send(event)
            return True
        except Exception as e:
            if attempt == len(RETRY_DELAYS):
                print(f"{label} {event['alert']} {event['status']} failed after {attempt + 1} tries: {e!r}", flush=True)
                return False
            sleep(RETRY_DELAYS[attempt])


def _background(label, send, sleep):
    return lambda event: threading.Thread(target=deliver, args=(label, event, send, sleep), daemon=True).start()


def webhook_notifier(url, sleep=time.sleep):
    """Return a notify(event) that POSTs the event as JSON in a background thread, retrying on failure."""
    def post(event):
        req = urllib.request.Request(url, json.dumps(event).encode(), {"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).close()
    return _background("webhook", post, sleep)


def email_notifier(host, port, user, password, sender, to, sleep=time.sleep):
    """Return a notify(event) that sends one plain-text email per event over STARTTLS, in a background thread, retrying on failure."""
    def send(event):
        msg = EmailMessage()
        msg["Subject"] = f"[{event['status'].upper()}] {event['alert']}"
        msg["From"], msg["To"] = sender, to
        msg.set_content(f"{event['alert']} is {event['status']}.\n{event['summary']}\nValue: {event['value']}\n")
        with smtplib.SMTP(host, port, timeout=10) as smtp:
            smtp.starttls()
            smtp.login(user, password)
            smtp.send_message(msg)
    return _background("email", send, sleep)


def ntfy_notifier(server, topic, sleep=time.sleep):
    """Return a notify(event) that pushes to a phone via ntfy, in a background thread, retrying on failure."""
    def send(event):
        firing = event["status"] == "firing"
        headers = {"Title": f"[{event['status'].upper()}] {event['alert']}", "Priority": "high" if firing else "default",
                   "Tags": "rotating_light" if firing else "white_check_mark"}
        body = f"{event['summary']}\nvalue: {event['value']}".encode()
        urllib.request.urlopen(urllib.request.Request(f"{server.rstrip('/')}/{topic}", body, headers), timeout=5).close()
    return _background("ntfy", send, sleep)


def notifiers_from_env(env):
    """Build the notify callable from environment variables, or None if nothing is configured."""
    out = []
    if env.get("ALERT_WEBHOOK_URL"):
        out.append(webhook_notifier(env["ALERT_WEBHOOK_URL"]))
    if env.get("NTFY_TOPIC"):
        out.append(ntfy_notifier(env.get("NTFY_SERVER", "https://ntfy.sh"), env["NTFY_TOPIC"]))
    if env.get("ALERT_EMAIL_TO"):
        out.append(email_notifier(env.get("SMTP_HOST", "smtp.gmail.com"), int(env.get("SMTP_PORT", 587)),
                                  env["SMTP_USER"], env["SMTP_PASS"], env.get("ALERT_EMAIL_FROM", env["SMTP_USER"]),
                                  env["ALERT_EMAIL_TO"]))
    return (lambda event: [n(event) for n in out]) if out else None


def save_snapshot(path, store, alerts, now):
    """Write the series and alert states to `path` atomically, so a crash mid-write never leaves a half-written file."""
    with store.lock:
        series = [[n, [list(kv) for kv in key], pts] for n, by in store.series.items() for key, pts in by.items()]
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump({"saved": now, "series": series, "alerts": alerts.state}, f)
    os.replace(tmp, path)


def load_snapshot(path, store, alerts, now):
    """Restore what save_snapshot wrote: points still inside the retention period, and the state of known alerts.
    A missing, unreadable or malformed file just means starting empty. Returns the number of series restored."""
    try:
        with open(path) as f:
            snap = json.load(f)
        loaded = 0
        for name, labels, pts in snap["series"]:
            key = tuple(sorted((k, v) for k, v in labels))
            keep = [(t, v) for t, v in pts if t >= now - store.retention]
            if keep:
                store.series[name][key] = keep
                loaded += 1
        for name, st in snap.get("alerts", {}).items():
            if name in alerts.state:  # a rule renamed or removed since the snapshot is ignored
                alerts.state[name] = {"state": st["state"], "since": st["since"], "value": st["value"]}
        return loaded
    except (OSError, ValueError, KeyError, TypeError):
        return 0


def scrape(store, url, now):
    try:
        text = urllib.request.urlopen(url, timeout=3).read().decode()
        samples, up = list(parse(text)), 1.0
    except (OSError, ValueError):  # unreachable, or a body that is not valid metrics text
        samples, up = [], 0.0
    store.add("up", {"instance": url}, now, up)
    for name, labels, v in samples:
        store.add(name, labels, now, v)


def probe(store, url, now):
    """Record probe_up{url} for an HTTP health endpoint: 1 if it answers 200 and, when the JSON body has a
    status, that status is HEALTHY or WARNING. A non-200, an unreachable host or any other status is 0."""
    try:
        body = urllib.request.urlopen(url, timeout=10).read()
        status = json.loads(body).get("status") if body.lstrip().startswith(b"{") else None
        up = 1.0 if status in (None, "HEALTHY", "WARNING") else 0.0
    except (OSError, ValueError, AttributeError):  # HTTPError (e.g. 503) is an OSError
        up = 0.0
    store.last_probe[url] = up
    store.add("probe_up", {"url": url}, now, up)


def tick(store, alerts, targets, now, probes=(), probe_due=True):
    """One scrape-and-evaluate cycle. A bad target or rule must never kill the loop."""
    for t in targets:
        try:
            scrape(store, t, now)
        except Exception as e:
            print(f"scrape {t} failed: {e!r}", flush=True)
    for u in probes:
        try:
            if probe_due:
                probe(store, u, now)
            elif u in store.last_probe:  # between probes, keep the last result fresh so retention and alert windows never see a gap
                store.add("probe_up", {"url": u}, now, store.last_probe[u])
        except Exception as e:
            print(f"probe {u} failed: {e!r}", flush=True)
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
    # no notifier configured means alerts show on the dashboard only
    silenced = [a.strip() for a in os.environ.get("ALERT_SILENCE", "").split(",") if a.strip()]
    alerts = Alerts(json.load(open(os.path.join(os.path.dirname(__file__), "alerts.json"))), notifiers_from_env(os.environ), silenced)
    snapshot = os.environ.get("COLLECTOR_DATA", "")  # a file path turns on saving the data, so a restart does not start empty
    snapshot_every, last_saved = float(os.environ.get("SNAPSHOT_INTERVAL", 60)), time.time()
    if snapshot:
        print(f"restored {load_snapshot(snapshot, store, alerts, time.time())} series from {snapshot}", flush=True)

    probes = [u for u in os.environ.get("PROBE_URLS", "").split(",") if u]
    probe_every, last_probe = float(os.environ.get("PROBE_INTERVAL", 300)), 0.0  # probes run less often than scrapes

    def loop():
        nonlocal last_probe, last_saved
        while True:
            now = time.time()
            due = now - last_probe >= probe_every
            tick(store, alerts, targets, now, probes, due)
            if due:
                last_probe = now
            if snapshot and now - last_saved >= snapshot_every:
                last_saved = now
                try:
                    save_snapshot(snapshot, store, alerts, now)
                except OSError as e:
                    print(f"saving {snapshot} failed: {e!r}", flush=True)
            time.sleep(interval)

    def stop(*_):  # docker stop sends SIGTERM: save one last time so the restart picks up where this left off
        if snapshot:
            try:
                save_snapshot(snapshot, store, alerts, time.time())
            except OSError:
                pass
        sys.exit(0)
    signal.signal(signal.SIGTERM, stop)
    threading.Thread(target=loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 9090), make_handler(store, alerts)).serve_forever()


if __name__ == "__main__":
    main()
