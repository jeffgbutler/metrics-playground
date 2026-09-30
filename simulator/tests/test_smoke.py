"""Headless smoke test: tick the engine (no HTTP, no OTLP) with every scenario, render every target.

    .venv/bin/python tests/test_smoke.py      (or pytest)
"""

import math
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from metricsim.config import Config  # noqa: E402
from metricsim.engine import Engine  # noqa: E402
from metricsim.scenarios import CATALOG  # noqa: E402

SAMPLE = re.compile(r'^[a-zA-Z_:][a-zA-Z0-9_:]*(\{.*\})? (NaN|[+-]Inf|-?[0-9.e+-]+)( -?\d+)?$')


def make_engine():
    return Engine(Config(otlp_enabled=False, seed=7, restart_downtime=0.0))


def run(eng, seconds, start):
    for i in range(seconds):
        eng.tick(start + i, 1.0)


def check_exposition(eng):
    for t in eng.targets():
        text = t.reg.render()
        for line in text.splitlines():
            if line and not line.startswith("#"):
                assert SAMPLE.match(line), f"{t.name}: bad line {line!r}"


def test_baseline():
    eng = make_engine()
    run(eng, 60, time.time())
    check_exposition(eng)
    fe = eng.by_service("frontend")[0]
    total = sum(fe.m_requests.series.values())
    assert total > 50, total
    assert eng.kafka.produced[0] > 0


def test_every_scenario():
    for key, cls in CATALOG.items():
        eng = make_engine()
        eng.scenarios.start(key, duration=40, ramp=5)
        run(eng, 45, time.time())
        check_exposition(eng)
        assert key not in eng.scenarios.active, f"{key} did not end"
    print(f"ok: {len(CATALOG)} scenarios")


def test_specific_effects():
    eng = make_engine()
    eng.scenarios.start("cardinality_explosion", duration=60, params={"customers": 50})
    run(eng, 30, time.time())
    cart = eng.by_service("cart")[0]
    assert "customer_id" in cart.m_requests.labelnames
    assert cart.reg.series_count() > 200

    eng = make_engine()
    eng.scenarios.start("nan_values", duration=60)
    run(eng, 5, time.time())
    assert math.isnan(eng.by_name["catalog-1"].m_hit_ratio.get(()))

    eng = make_engine()
    eng.scenarios.start("node_down", duration=60)
    run(eng, 5, time.time())
    assert not any(i.up for i in eng.instances if i.node == "node-c")


if __name__ == "__main__":
    test_baseline()
    test_every_scenario()
    test_specific_effects()
    print("all good")
