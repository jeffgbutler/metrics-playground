import math
import random
import re

_DUR = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h)?\s*$")


def parse_duration(v) -> float:
    """'30s', '10m', '2h', '500ms', 90 -> seconds."""
    if isinstance(v, (int, float)):
        return float(v)
    m = _DUR.match(str(v))
    if not m:
        raise ValueError(f"bad duration {v!r} (use e.g. 30s, 10m, 2h)")
    n, unit = float(m.group(1)), m.group(2) or "s"
    return n * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]


def poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    if lam > 30:
        return max(0, int(round(rng.gauss(lam, math.sqrt(lam)))))
    L, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= L:
            return k
        k += 1


def lognormal(rng: random.Random, median: float, sigma: float) -> float:
    return median * math.exp(rng.gauss(0.0, sigma))


def weighted(rng: random.Random, pairs):
    r = rng.random() * sum(w for _, w in pairs)
    for v, w in pairs:
        r -= w
        if r <= 0:
            return v
    return pairs[-1][0]
