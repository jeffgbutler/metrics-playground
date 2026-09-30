"""The contract between scenarios and the engine.

A fresh neutral `Effects` is built every tick. Active scenarios mutate it (scaled by their intensity), and the
engine reads it. The engine never knows which scenario is running, only what the world should look like.
Anything stateful (a heap that grows, a disk that fills) lives on the engine's objects; scenarios only
set rates.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field


def _one():
    return 1.0


@dataclass
class Effects:
    # --- traffic -------------------------------------------------------------------------------------
    traffic_mult: float = 1.0

    # --- application behaviour -------------------------------------------------------------------------
    # latency multiplier on a service's own work, keyed by service or "service/instance"
    latency_mult: dict = field(default_factory=lambda: defaultdict(_one))
    # extra error probability keyed by "service", "service route", or "instance"
    error_add: dict = field(default_factory=lambda: defaultdict(float))
    provider_latency_mult: dict = field(default_factory=lambda: defaultdict(_one))  # payment provider name
    provider_error_add: dict = field(default_factory=lambda: defaultdict(float))
    carrier_latency_mult: float = 1.0

    # --- infrastructure ---------------------------------------------------------------------------------
    node_cpu_hog: dict = field(default_factory=lambda: defaultdict(float))  # node -> cores burned by a neighbour
    node_steal: dict = field(default_factory=lambda: defaultdict(float))  # node -> fraction of CPU stolen
    node_down: set = field(default_factory=set)
    disk_fill_bytes_per_s: dict = field(default_factory=lambda: defaultdict(float))
    heap_leak_bytes_per_s: dict = field(default_factory=lambda: defaultdict(float))  # instance -> leak rate
    db_latency_mult: float = 1.0
    db_lock_waiters: float = 0.0
    replica_lag_s: float = 0.0
    pool_max_override: dict = field(default_factory=dict)  # service -> pool size
    pool_hold_mult: dict = field(default_factory=lambda: defaultdict(_one))  # service -> connection hold time mult
    pool_leak_per_s: dict = field(default_factory=lambda: defaultdict(float))  # service -> connections leaked / s
    redis_maxmemory_frac: float = 1.0  # < 1 squeezes memory -> evictions
    cache_hit_penalty: float = 0.0  # 0..1 subtracted from hit ratio
    consumer_capacity_mult: float = 1.0

    # --- deployment / lifecycle -------------------------------------------------------------------------
    version_override: dict = field(default_factory=dict)  # instance -> version string
    restart_now: set = field(default_factory=set)  # instances to restart this tick
    rename_on_restart: set = field(default_factory=set)  # instances that get a new identity when they restart

    # --- metrics pathologies ----------------------------------------------------------------------------
    scrape_delay_s: dict = field(default_factory=dict)  # instance -> seconds to stall /metrics
    extra_label_cardinality: dict = field(default_factory=dict)  # service -> number of distinct customer ids
    buckets_override: dict = field(default_factory=dict)  # instance -> tuple of buckets
    microburst: dict = field(default_factory=dict)  # service -> burst concurrency
    clock_skew_s: dict = field(default_factory=dict)  # instance -> seconds (+ future / - past)
    nan_gauges: set = field(default_factory=set)  # instances whose ratio gauges go NaN/+Inf
    retired_routes: set = field(default_factory=set)  # "service route" keys no longer served

    # --- ground truth -----------------------------------------------------------------------------------
    active: dict = field(default_factory=dict)  # scenario key -> intensity
