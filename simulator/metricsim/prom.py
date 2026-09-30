"""A deliberately tiny Prometheus metrics library with a text-format writer.

Why not prometheus_client? The playground needs to do things a well-behaved client library refuses to do:
emit explicit (skewed) timestamps, emit NaN/+Inf gauges, change histogram bucket layouts on the fly, drop
series mid-run, and reset everything to zero on a simulated restart. Owning ~200 lines makes that easy and
makes the exposition format itself something you can read (see `render`).

Exposition format reference: https://prometheus.io/docs/instrumenting/exposition_formats/
"""

from __future__ import annotations

import math
import time
from bisect import bisect_left
from collections import deque

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

# The Prometheus client libraries' default buckets. Tuned for web latency in seconds; they top out at 10s.
DEFAULT_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


def fmt_value(v: float) -> str:
    if isinstance(v, int):
        return str(v)
    if math.isnan(v):
        return "NaN"
    if math.isinf(v):
        return "+Inf" if v > 0 else "-Inf"
    if v == int(v) and abs(v) < 1e15:
        return str(int(v))
    return repr(round(v, 9))


def _esc(v: str) -> str:
    return v.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def fmt_labels(pairs) -> str:
    if not pairs:
        return ""
    return "{" + ",".join(f'{k}="{_esc(str(v))}"' for k, v in pairs) + "}"


class Metric:
    type = "untyped"

    def __init__(self, name: str, help: str, labelnames=()):
        self.name = name
        self.help = help
        self.labelnames = tuple(labelnames)
        self.series: dict[tuple, object] = {}

    def _key(self, labels) -> tuple:
        if isinstance(labels, dict):
            return tuple(str(labels[n]) for n in self.labelnames)
        return tuple(str(x) for x in labels)

    def remove(self, labels):
        self.series.pop(self._key(labels), None)

    def remove_matching(self, **match):
        idx = {n: i for i, n in enumerate(self.labelnames)}
        for key in list(self.series):
            if all(key[idx[k]] == str(v) for k, v in match.items()):
                del self.series[key]

    def samples(self):
        """Yield (suffix, extra_label_pairs, key, value)."""
        raise NotImplementedError


class Counter(Metric):
    type = "counter"

    def inc(self, labels=(), v: float = 1.0):
        k = self._key(labels)
        self.series[k] = self.series.get(k, 0.0) + v

    def samples(self):
        for k, v in self.series.items():
            yield "", (), k, v


class Gauge(Metric):
    type = "gauge"

    def set(self, labels=(), v: float = 0.0):
        self.series[self._key(labels)] = v

    def inc(self, labels=(), v: float = 1.0):
        k = self._key(labels)
        self.series[k] = self.series.get(k, 0.0) + v

    def get(self, labels=(), default=0.0):
        return self.series.get(self._key(labels), default)

    def samples(self):
        for k, v in self.series.items():
            yield "", (), k, v


class _HistState:
    __slots__ = ("counts", "sum", "count")

    def __init__(self, n):
        self.counts = [0] * n
        self.sum = 0.0
        self.count = 0


class Histogram(Metric):
    """Classic Prometheus histogram: cumulative `_bucket{le=...}` counters plus `_sum` and `_count`."""

    type = "histogram"

    def __init__(self, name, help, labelnames=(), buckets=DEFAULT_BUCKETS):
        super().__init__(name, help, labelnames)
        self.buckets = tuple(sorted(buckets))

    def set_buckets(self, buckets):
        """Change the layout. Existing series are dropped: that is what a redeploy with new buckets does."""
        self.buckets = tuple(sorted(buckets))
        self.series.clear()

    def observe(self, labels, v: float):
        k = self._key(labels)
        st = self.series.get(k)
        if st is None:
            st = self.series[k] = _HistState(len(self.buckets))
        i = bisect_left(self.buckets, v)
        if i < len(self.buckets):
            st.counts[i] += 1
        st.sum += v
        st.count += 1

    def samples(self):
        for k, st in self.series.items():
            acc = 0
            for le, c in zip(self.buckets, st.counts):
                acc += c
                yield "_bucket", (("le", fmt_value(float(le))),), k, acc
            yield "_bucket", (("le", "+Inf"),), k, st.count
            yield "_sum", (), k, st.sum
            yield "_count", (), k, st.count


class Summary(Metric):
    """Prometheus summary: client-side quantiles over a sliding window, plus cumulative `_sum`/`_count`.

    The quantiles are computed per instance and CANNOT be meaningfully aggregated across instances.
    That is the whole reason this type is in the playground.
    """

    type = "summary"

    def __init__(self, name, help, labelnames=(), quantiles=(0.5, 0.9, 0.99), max_age=120.0):
        super().__init__(name, help, labelnames)
        self.quantiles = quantiles
        self.max_age = max_age

    def observe(self, labels, v: float, now: float | None = None):
        k = self._key(labels)
        st = self.series.get(k)
        if st is None:
            st = self.series[k] = {"window": deque(), "sum": 0.0, "count": 0}
        st["window"].append((now or time.time(), v))
        st["sum"] += v
        st["count"] += 1

    def samples(self):
        now = time.time()
        for k, st in self.series.items():
            w = st["window"]
            while w and now - w[0][0] > self.max_age:
                w.popleft()
            vals = sorted(x[1] for x in w)
            for q in self.quantiles:
                if vals:
                    val = vals[min(len(vals) - 1, int(q * len(vals)))]
                else:
                    val = float("nan")  # what real clients do with an empty window
                yield "", (("quantile", fmt_value(float(q))),), k, val
            yield "_sum", (), k, st["sum"]
            yield "_count", (), k, st["count"]


class Registry:
    def __init__(self):
        self.metrics: dict[str, Metric] = {}

    def _add(self, m: Metric) -> Metric:
        existing = self.metrics.get(m.name)
        if existing is not None:
            return existing
        self.metrics[m.name] = m
        return m

    def counter(self, name, help, labelnames=()) -> Counter:
        return self._add(Counter(name, help, labelnames))

    def gauge(self, name, help, labelnames=()) -> Gauge:
        return self._add(Gauge(name, help, labelnames))

    def histogram(self, name, help, labelnames=(), buckets=DEFAULT_BUCKETS) -> Histogram:
        return self._add(Histogram(name, help, labelnames, buckets))

    def summary(self, name, help, labelnames=(), **kw) -> Summary:
        return self._add(Summary(name, help, labelnames, **kw))

    def get(self, name) -> Metric | None:
        return self.metrics.get(name)

    def drop(self, name):
        self.metrics.pop(name, None)

    def series_count(self) -> int:
        return sum(1 for m in self.metrics.values() for _ in m.samples())

    def render(self, timestamp_ms: int | None = None) -> str:
        """Prometheus text format 0.0.4. If `timestamp_ms` is given every sample carries it explicitly.

        Explicit timestamps are legal but unusual; they change how Prometheus treats staleness and
        out-of-order data (see the `clock_skew` scenario).
        """
        out = []
        ts = f" {timestamp_ms}" if timestamp_ms is not None else ""
        for m in self.metrics.values():
            if not m.series:
                continue
            base = m.name  # classic text format: a counter family keeps its `_total` (if it has one at all)
            out.append(f"# HELP {base} {m.help}")
            out.append(f"# TYPE {base} {m.type}")
            for suffix, extra, key, v in m.samples():
                pairs = list(zip(m.labelnames, key)) + list(extra)
                out.append(f"{base}{suffix}{fmt_labels(pairs)} {fmt_value(v)}{ts}")
        out.append("")
        return "\n".join(out)
