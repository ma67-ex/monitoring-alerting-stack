"""Minimal metrics registry that renders the Prometheus text format."""
import threading
from bisect import bisect_left

_lock = threading.Lock()
_all = []


def _fmt(labels):
    return "{" + ",".join(f'{k}="{v}"' for k, v in labels) + "}" if labels else ""


class Counter:
    def __init__(self, name, help, labelnames=()):
        self.name, self.help, self.labelnames, self.values = name, help, labelnames, {}
        _all.append(self)

    def inc(self, n=1, **labels):
        key = tuple((k, str(labels[k])) for k in self.labelnames)
        with _lock:
            self.values[key] = self.values.get(key, 0) + n

    def render(self):
        yield f"# HELP {self.name} {self.help}\n# TYPE {self.name} counter"
        for key, v in list(self.values.items()):
            yield f"{self.name}{_fmt(key)} {v}"


class Histogram:
    def __init__(self, name, help, buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5)):
        self.name, self.help, self.buckets = name, help, buckets
        self.counts, self.sum, self.count = [0] * (len(buckets) + 1), 0.0, 0
        _all.append(self)

    def observe(self, v):
        with _lock:
            self.counts[bisect_left(self.buckets, v)] += 1
            self.sum += v
            self.count += 1

    def render(self):
        yield f"# HELP {self.name} {self.help}\n# TYPE {self.name} histogram"
        cum = 0
        for le, c in zip(list(self.buckets) + ["+Inf"], self.counts):
            cum += c
            yield f'{self.name}_bucket{{le="{le}"}} {cum}'
        yield f"{self.name}_sum {self.sum}\n{self.name}_count {self.count}"


def render():
    with _lock:
        return ("\n".join(line for m in _all for line in m.render()) + "\n").encode()
