"""Failure scenarios.

Two families:
  * system   - something in Byte Mart breaks (latency, errors, a host dies, a disk fills ...)
  * metrics  - the *telemetry* misbehaves while the system may be fine (counter resets, scrape timeouts,
               cardinality explosions, lying histograms, NaNs, clock skew ...). These are where Prometheus,
               Grafana, the OTel collector and Honeycomb disagree with each other, which is the point.

A scenario is time-boxed. Intensity `x` ramps 0 -> 1 over `ramp`, holds, and ramps back to 0 over the last
`ramp` of `duration`. `apply(now, fx, x, eng)` mutates the tick's Effects. Scenarios never touch metrics
directly; stateful consequences (a leaking heap, a filling disk) are accumulated by the engine.

Every scenario's `look_for` lists where the evidence shows up. The labs in /labs go deeper.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass

from .util import parse_duration

CATALOG: dict[str, type["Scenario"]] = {}


def register(cls):
    CATALOG[cls.key] = cls
    return cls


@dataclass
class Param:
    default: object
    help: str
    choices: tuple = ()


class Scenario:
    key = ""
    title = ""
    category = "system"  # system | metrics
    summary = ""
    look_for: list[str] = []
    params: dict[str, Param] = {}
    duration = 600.0
    ramp = 30.0

    def __init__(self, params: dict | None = None, duration=None, ramp=None, now=None, hidden=False):
        self.p = {k: v.default for k, v in self.params.items()}
        for k, v in (params or {}).items():
            if k not in self.params:
                raise ValueError(f"{self.key}: unknown param {k!r} (known: {', '.join(self.params)})")
            d = self.params[k].default
            if isinstance(d, bool):
                v = str(v).lower() in ("1", "true", "yes", "on")
            elif isinstance(d, int):
                v = int(v)
            elif isinstance(d, float):
                v = float(v)
            if self.params[k].choices and v not in self.params[k].choices:
                raise ValueError(f"{self.key}: {k} must be one of {self.params[k].choices}")
            self.p[k] = v
        self.dur = parse_duration(duration) if duration else self.duration
        self.rmp = min(parse_duration(ramp) if ramp is not None else self.ramp, self.dur / 2)
        self.started = now or time.time()
        self.ends = self.started + self.dur
        self.hidden = hidden
        self.state: dict = {}

    def intensity(self, now):
        if now >= self.ends:
            return 0.0
        up = (now - self.started) / self.rmp if self.rmp else 1.0
        down = (self.ends - now) / self.rmp if self.rmp else 1.0
        return max(0.0, min(1.0, up, down))

    def stop(self, now):
        """Wind down over half a ramp rather than cutting off."""
        self.ends = min(self.ends, now + self.rmp / 2)

    def apply(self, now, fx, x, eng):
        pass

    def on_start(self, eng):
        pass

    def on_end(self, eng):
        pass

    def describe(self, now=None):
        now = now or time.time()
        return {
            "key": self.key, "title": self.title, "category": self.category, "params": self.p,
            "started": self.started, "ends": self.ends, "remaining_s": round(max(0, self.ends - now)),
            "intensity": round(self.intensity(now), 3), "hidden": self.hidden,
        }

    @classmethod
    def catalog_entry(cls):
        return {
            "key": cls.key, "title": cls.title, "category": cls.category, "summary": cls.summary,
            "look_for": cls.look_for, "duration_s": cls.duration, "ramp_s": cls.ramp,
            "params": {k: {"default": v.default, "help": v.help, "choices": list(v.choices)}
                       for k, v in cls.params.items()},
        }


# =====================================================================================================
# System failures
# =====================================================================================================


@register
class TrafficSurge(Scenario):
    key = "traffic_surge"
    title = "Flash sale traffic surge"
    summary = "Front-door traffic multiplies. Watch saturation spread: CPU, DB pools, in-flight, then latency."
    look_for = [
        "sum(rate(http_requests_total{job=\"frontend\"}[1m]))",
        "Node CPU: 1 - avg by (node) (rate(node_cpu_seconds_total{mode=\"idle\"}[1m]))",
        "db_pool_connections{state=\"active\"} / db_pool_connections_max",
        "Honeycomb: SUM(http_requests_total) GROUP BY service.name, then RATE() via a calculated field",
    ]
    params = {"mult": Param(3.0, "peak traffic multiplier")}
    duration, ramp = 900.0, 120.0

    def apply(self, now, fx, x, eng):
        fx.traffic_mult *= 1 + (self.p["mult"] - 1) * x


@register
class PaymentProviderSlow(Scenario):
    key = "payment_provider_slow"
    title = "Payment provider latency + errors"
    summary = ("One external card provider slows down and starts failing. payments -> checkout -> gateway -> "
               "frontend all get slower: latency is inherited up the call chain.")
    look_for = [
        "payment_provider_latency_seconds{quantile=\"0.99\"} (a SUMMARY: per-instance quantiles)",
        "histogram_quantile(0.99, sum by (le, job) (rate(http_request_duration_seconds_bucket[5m])))",
        "sum by (provider, outcome) (rate(payment_attempts_total[1m]))",
    ]
    params = {
        "provider": Param("acmepay", "which provider", ("acmepay", "globexpay")),
        "factor": Param(8.0, "latency multiplier"),
        "error_rate": Param(0.06, "extra failure probability"),
    }
    duration, ramp = 600.0, 90.0

    def apply(self, now, fx, x, eng):
        fx.provider_latency_mult[self.p["provider"]] *= 1 + (self.p["factor"] - 1) * x
        fx.provider_error_add[self.p["provider"]] += self.p["error_rate"] * x


@register
class ErrorBurst(Scenario):
    key = "error_burst"
    title = "5xx error burst"
    summary = "A service starts returning 500s on one route (or all). Upstream services turn them into 502s."
    look_for = [
        "sum by (job, status) (rate(http_requests_total{status=~\"5..\"}[1m]))",
        "Error ratio: sum(rate(...{status=~\"5..\"}[5m])) / sum(rate(...[5m]))",
        "Honeycomb: filter status starts-with 5, GROUP BY service.name, route",
    ]
    params = {
        "service": Param("inventory", "service to break"),
        "route": Param("", "e.g. 'POST /reserve'; empty = every route"),
        "rate": Param(0.25, "error probability at full intensity"),
    }
    duration, ramp = 600.0, 20.0

    def apply(self, now, fx, x, eng):
        k = f"{self.p['service']} {self.p['route']}" if self.p["route"] else self.p["service"]
        fx.error_add[k] += self.p["rate"] * x


@register
class BadDeploy(Scenario):
    key = "bad_deploy"
    title = "Bad rolling deploy (then rollback)"
    summary = ("A new version rolls out one instance at a time: each restart resets its counters, "
               "app_build_info changes version, and new-version instances are slower and error-prone. "
               "At the end everything is rolled back (another round of restarts).")
    look_for = [
        "app_build_info (count by (job, version) (app_build_info))",
        "Join version onto a rate: sum by (version) (rate(http_requests_total{status=~\"5..\"}[1m]) "
        "* on (instance) group_left(version) app_build_info)",
        "resets(http_requests_total[15m]) and process_start_time_seconds",
        "Honeycomb (collection.method=otlp-push): GROUP BY service.version is just a resource attribute - no join needed",
    ]
    params = {
        "service": Param("catalog", "service to deploy"),
        "version": Param("1.9.0", "new version"),
        "error_rate": Param(0.08, "extra error probability on new version"),
        "latency_factor": Param(2.5, "latency multiplier on new version"),
        "stagger": Param(60.0, "seconds between instance restarts"),
    }
    duration, ramp = 900.0, 5.0

    def on_start(self, eng):
        self.state["upgraded"] = []
        self.state["old"] = {i.name: i.version for i in eng.by_service(self.p["service"])}
        eng.marker(f"deploy {self.p['service']} {self.p['version']}", "deploy")

    def apply(self, now, fx, x, eng):
        insts = eng.by_service(self.p["service"])
        elapsed = now - self.started
        for i, inst in enumerate(insts):
            if inst.name not in self.state["upgraded"] and elapsed >= i * self.p["stagger"]:
                self.state["upgraded"].append(inst.name)
                eng.restart(inst, version=self.p["version"])
        for name in self.state["upgraded"]:
            fx.error_add[name] += self.p["error_rate"]
            fx.latency_mult[name] *= self.p["latency_factor"]

    def on_end(self, eng):
        for inst in eng.by_service(self.p["service"]):
            if inst.name in self.state.get("upgraded", []):
                eng.restart(inst, version=self.state["old"][inst.name])
        eng.marker(f"rollback {self.p['service']} to {self.state['old']}", "rollback")


@register
class MemoryLeak(Scenario):
    key = "memory_leak"
    title = "Memory leak -> OOM kill loop"
    summary = ("One JVM instance leaks heap. GC pauses lengthen and CPU climbs as the old gen fills, then the "
               "process is OOM-killed and restarts. Repeat. A sawtooth in memory, a reset in every counter.")
    look_for = [
        "jvm_memory_used_bytes{area=\"heap\"} / on (instance, id) jvm_memory_max_bytes  (careful with -1!)",
        "process_resident_memory_bytes by instance, process_start_time_seconds changes",
        "rate(jvm_gc_pause_seconds_sum[1m]) vs jvm_gc_pause_seconds_max",
        "changes(process_start_time_seconds[30m]) - count the restarts",
    ]
    params = {
        "instance": Param("payments-1", "instance to leak"),
        "mb_per_min": Param(80.0, "leak rate"),
    }
    duration, ramp = 1800.0, 1.0

    def apply(self, now, fx, x, eng):
        fx.heap_leak_bytes_per_s[self.p["instance"]] += self.p["mb_per_min"] * 1024 * 1024 / 60 * x


@register
class NoisyNeighbor(Scenario):
    key = "noisy_neighbor"
    title = "Noisy neighbour CPU saturation"
    summary = ("Something else on a host burns CPU and the hypervisor steals cycles. Every instance on that "
               "node slows down even though nothing in the app changed.")
    look_for = [
        "sum by (node, mode) (rate(node_cpu_seconds_total{mode!=\"idle\"}[1m]))",
        "node_load1 vs count by (node) (node_cpu_seconds_total{mode=\"idle\"})  (load per core)",
        "http latency grouped by node (the `node` label is on app targets too)",
    ]
    params = {
        "node": Param("node-b", "host", ("node-a", "node-b", "node-c", "node-d")),
        "cores": Param(3.0, "cores burned by the neighbour (node has 4)"),
        "steal": Param(0.2, "fraction of CPU time stolen"),
    }
    duration, ramp = 600.0, 60.0

    def apply(self, now, fx, x, eng):
        fx.node_cpu_hog[self.p["node"]] += self.p["cores"] * x
        fx.node_steal[self.p["node"]] += self.p["steal"] * x


@register
class DiskFill(Scenario):
    key = "disk_fill"
    title = "Disk filling up (then the database dies)"
    summary = ("A runaway log fills the root filesystem on a host. node-d hosts postgres-primary: when the disk "
               "is full postgres stops (pg_up=0 while the exporter's up=1) and every write fails.")
    look_for = [
        "1 - node_filesystem_avail_bytes{mountpoint=\"/\"} / node_filesystem_size_bytes",
        "predict_linear(node_filesystem_avail_bytes{mountpoint=\"/\"}[10m], 3600) < 0",
        "pg_up vs up{job=\"postgres\"}",
        "Honeycomb: no predict_linear - try a trigger on the gauge, or query math on two time ranges",
    ]
    params = {
        "node": Param("node-d", "host", ("node-a", "node-b", "node-c", "node-d")),
        "gb_per_min": Param(5.0, "fill rate (200 GB disk, ~45% used at start)"),
    }
    duration, ramp = 1800.0, 30.0

    def apply(self, now, fx, x, eng):
        fx.disk_fill_bytes_per_s[self.p["node"]] += self.p["gb_per_min"] * 1024**3 / 60 * x


@register
class NodeDown(Scenario):
    key = "node_down"
    title = "Host goes down"
    summary = ("A host disappears. Its targets stop answering (up=0), traffic shifts to surviving instances, and "
               "when it comes back every counter on it (and its node_exporter) has reset.")
    look_for = [
        "up == 0, and sum by (job) (up) / count by (job) (up)",
        "sum by (instance) (rate(http_requests_total{job=\"api-gateway\"}[1m]))  (load shifts)",
        "absent(up{instance=\"node-c\"} == 1)",
        "Honeycomb: there is no `up` for data that never arrived. Absence has to be inferred.",
    ]
    params = {"node": Param("node-c", "host", ("node-a", "node-b", "node-c", "node-d"))}
    duration, ramp = 300.0, 0.0

    def apply(self, now, fx, x, eng):
        if x > 0:
            fx.node_down.add(self.p["node"])


@register
class DbPoolExhaustion(Scenario):
    key = "db_pool_exhaustion"
    title = "DB connection leak -> pool exhaustion"
    summary = ("A code path forgets to return connections. Active connections climb linearly until the pool is "
               "full, then requests queue for a connection, time out after 30s, and fail.")
    look_for = [
        "db_pool_connections{state=\"active\"} vs db_pool_connections_max",
        "db_pool_connections{state=\"pending\"}, rate(db_pool_timeouts_total[1m])",
        "histogram_quantile(0.99, sum by (le) (rate(db_pool_acquire_duration_seconds_bucket[1m])))",
    ]
    params = {
        "service": Param("inventory", "service with the leak", ("catalog", "checkout", "payments", "inventory")),
        "per_min": Param(1.0, "connections leaked per minute"),
    }
    duration, ramp = 900.0, 1.0

    def apply(self, now, fx, x, eng):
        fx.pool_leak_per_s[self.p["service"]] += self.p["per_min"] / 60 * x

    def on_end(self, eng):
        for inst in eng.by_service(self.p["service"]):
            inst.leaked_conns = 0.0  # the leak detector finally reclaims them


@register
class DbSlowQueries(Scenario):
    key = "db_slow_queries"
    title = "Slow queries and lock contention"
    summary = ("A migration takes locks on postgres-primary. Queries slow down, connections are held longer, "
               "pools fill up and the replica falls behind.")
    look_for = [
        "pg_locks_count, pg_stat_activity_count{state=\"idle in transaction\"}",
        "pg_replication_lag_seconds",
        "rate(pg_stat_database_xact_commit[1m])  (a counter without _total)",
        "db_pool_utilization_ratio by job",
    ]
    params = {"factor": Param(6.0, "query latency multiplier"), "lock_waiters": Param(14.0, "lock waiters")}
    duration, ramp = 600.0, 60.0

    def apply(self, now, fx, x, eng):
        fx.db_latency_mult *= 1 + (self.p["factor"] - 1) * x
        fx.db_lock_waiters += self.p["lock_waiters"] * x
        fx.replica_lag_s += 45 * x
        for s in ("catalog", "checkout", "payments", "inventory"):
            fx.pool_hold_mult[s] *= 1 + 2 * x


@register
class CacheEviction(Scenario):
    key = "cache_eviction"
    title = "Redis eviction storm"
    summary = ("Redis maxmemory is lowered. It evicts constantly, the catalog cache hit ratio collapses and "
               "product reads fall through to postgres.")
    look_for = [
        "rate(redis_evicted_keys_total[1m]), redis_memory_used_bytes / redis_memory_max_bytes",
        "Hit ratio from counters: rate(redis_keyspace_hits_total[5m]) / (rate(hits[5m]) + rate(misses[5m]))",
        "catalog_cache_hit_ratio (the app's own gauge) - compare with the counter-derived ratio",
    ]
    params = {"maxmemory_frac": Param(0.55, "fraction of maxmemory left"),
              "hit_penalty": Param(0.55, "hit ratio drop")}
    duration, ramp = 600.0, 45.0

    def apply(self, now, fx, x, eng):
        fx.redis_maxmemory_frac *= 1 - (1 - self.p["maxmemory_frac"]) * x
        fx.cache_hit_penalty += self.p["hit_penalty"] * x


@register
class QueueBacklog(Scenario):
    key = "queue_backlog"
    title = "Kafka consumer lag"
    summary = ("The notifications consumers slow down. Orders still succeed, but lag builds and customers get "
               "their confirmation emails minutes late. Nothing returns an error.")
    look_for = [
        "sum(kafka_consumergroup_lag) - and the same from offsets: "
        "sum(kafka_topic_partition_current_offset) - sum(kafka_consumergroup_current_offset)",
        "histogram_quantile(0.9, sum by (le) (rate(notifications_delivery_delay_seconds_bucket[5m])))",
        "deriv(kafka_consumergroup_lag[5m]) - is it still growing?",
    ]
    params = {"capacity": Param(0.01, "consumer throughput fraction left")}
    duration, ramp = 900.0, 30.0

    def apply(self, now, fx, x, eng):
        fx.consumer_capacity_mult *= 1 - (1 - self.p["capacity"]) * x


# =====================================================================================================
# Metrics pathologies
# =====================================================================================================


@register
class CrashLoop(Scenario):
    key = "crash_loop"
    title = "Crash loop (counter resets / series churn)"
    category = "metrics"
    summary = ("An instance restarts every N seconds. Every counter drops to zero each time. rate() copes; "
               "naive math on raw counters does not. With rename=true each restart has a new instance id "
               "(like a Kubernetes pod), so every restart also creates brand-new series.")
    look_for = [
        "Raw http_requests_total{instance=\"cart-2\"} (sawtooth) vs rate(...[1m]) vs increase(...[5m])",
        "resets(http_requests_total{instance=\"cart-2\"}[10m])",
        "With rename=true: count(count by (instance) (up{job=\"cart\"})) over time, prometheus_tsdb_head_series",
        "Honeycomb: INCREASE/RATE handle resets; with rename, service.instance.id cardinality climbs",
    ]
    params = {
        "instance": Param("cart-2", "instance"),
        "every": Param(90.0, "seconds between crashes"),
        "rename": Param(False, "give the instance a new identity on each restart"),
    }
    duration, ramp = 600.0, 1.0

    def apply(self, now, fx, x, eng):
        last = self.state.get("last", self.started - self.p["every"] + 20)
        if now - last >= self.p["every"]:
            self.state["last"] = now
            fx.restart_now.add(self.p["instance"])
            if self.p["rename"]:
                fx.rename_on_restart.add(self.p["instance"])


@register
class ScrapeTimeout(Scenario):
    key = "scrape_timeout"
    title = "/metrics endpoint too slow (scrape timeouts)"
    category = "metrics"
    summary = ("The service is healthy but its /metrics handler stalls longer than the scrape timeout (10s). "
               "Prometheus records up=0 and gaps; the collector also drops the scrape. Monitoring failure is "
               "not service failure.")
    look_for = [
        "up{instance=\"checkout-1\"}, scrape_duration_seconds{instance=\"checkout-1\"}",
        "The service's own traffic, seen from its callers: rate(http_requests_total{job=\"api-gateway\","
        "route=\"/api/checkout\"}[1m]) - still fine",
        "Grafana: gaps in panels. Honeycomb: gaps for prometheus-scrape/-federate, but otlp-push is unaffected",
    ]
    params = {"instance": Param("checkout-1", "instance"), "delay": Param(12.0, "seconds to stall")}
    duration, ramp = 480.0, 1.0

    def apply(self, now, fx, x, eng):
        fx.scrape_delay_s[self.p["instance"]] = self.p["delay"]


@register
class CardinalityExplosion(Scenario):
    key = "cardinality_explosion"
    title = "Cardinality explosion (customer_id label)"
    category = "metrics"
    summary = ("A well-meaning developer adds customer_id as a label on http metrics. The series count per target "
               "multiplies; with enough customers Prometheus's sample_limit rejects the WHOLE scrape (up=0). "
               "This is the place where Honeycomb's model differs most.")
    look_for = [
        "prometheus_tsdb_head_series, scrape_samples_scraped{job=\"cart\"}, scrape_series_added",
        "count by (__name__) ({job=\"cart\"}) - which metric blew up?",
        "topk(5, sum by (customer_id) (rate(http_requests_total{job=\"cart\"}[5m])))",
        "Try customers=800: Prometheus hits sample_limit (up=0) while the collector keeps sending",
        "Honeycomb: GROUP BY customer_id works fine; watch your event volume instead",
    ]
    params = {"service": Param("cart", "service"), "customers": Param(150, "distinct customer_id values")}
    duration, ramp = 600.0, 1.0

    def apply(self, now, fx, x, eng):
        fx.extra_label_cardinality[self.p["service"]] = self.p["customers"]


NARROW_BUCKETS = (0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1)
ODD_BUCKETS = (0.02, 0.04, 0.08, 0.3, 0.75, 2.0, 6.0)


@register
class BadBuckets(Scenario):
    key = "bad_buckets"
    title = "Histogram buckets that lie"
    category = "metrics"
    summary = ("too_narrow: buckets top out at 100ms while real latency goes to ~400ms, so histogram_quantile "
               "can never report more than 0.1s. mismatched: one instance ships different bucket boundaries, "
               "and summing buckets across instances produces nonsense.")
    look_for = [
        "histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket{job=\"catalog\"}[5m])))",
        "Compare with the mean: rate(..._sum[5m]) / rate(..._count[5m])",
        "Look at the raw buckets: sum by (le) (rate(..._bucket{job=\"catalog\"}[5m]))",
        "Honeycomb, collection.method=otlp-push: exponential histograms, no buckets to get wrong",
    ]
    params = {
        "service": Param("catalog", "service"),
        "mode": Param("too_narrow", "which failure", ("too_narrow", "mismatched")),
        "latency_factor": Param(4.0, "latency multiplier (too_narrow mode)"),
    }
    duration, ramp = 720.0, 60.0

    def apply(self, now, fx, x, eng):
        insts = eng.by_service(self.p["service"])
        if self.p["mode"] == "too_narrow":
            for i in insts:
                fx.buckets_override[i.name] = NARROW_BUCKETS
            fx.latency_mult[self.p["service"]] *= 1 + (self.p["latency_factor"] - 1) * x
        elif insts:
            fx.buckets_override[insts[0].name] = ODD_BUCKETS


@register
class Microbursts(Scenario):
    key = "microbursts"
    title = "Microbursts a gauge can't see"
    category = "metrics"
    summary = ("Every 30s the gateway gets a 2-second burst: concurrency jumps and requests slow down. A 15s "
               "scrape of the in-flight gauge usually misses it; the latency histogram (a set of counters) "
               "never does.")
    look_for = [
        "http_requests_in_flight{job=\"api-gateway\"} and max_over_time(...[5m]) - mostly flat",
        "histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket{job=\"api-gateway\"}[1m])))",
        "Counters integrate; gauges sample.",
    ]
    params = {
        "service": Param("api-gateway", "service"),
        "concurrency": Param(150.0, "extra in-flight during a burst"),
        "every": Param(30.0, "seconds between bursts"),
        "length": Param(2.0, "burst length in seconds"),
    }
    duration, ramp = 600.0, 1.0

    def apply(self, now, fx, x, eng):
        if (now - self.started) % self.p["every"] < self.p["length"]:
            fx.microburst[self.p["service"]] = self.p["concurrency"]
            fx.latency_mult[self.p["service"]] *= 12


@register
class ClockSkew(Scenario):
    key = "clock_skew"
    title = "Clock skew (explicit timestamps)"
    category = "metrics"
    summary = ("An instance starts stamping its samples with a skewed clock. Prometheus rejects samples that go "
               "backwards (out-of-order) and stops inserting staleness markers; the collector forwards "
               "whatever timestamp it is given. The pipelines disagree about what happened when.")
    look_for = [
        "prometheus_target_scrapes_sample_out_of_order_total, ..._sample_out_of_bounds_total",
        "timestamp(up{instance=\"inventory-1\"}) - time()  vs  timestamp(http_requests_in_flight{instance=\"inventory-1\"}) - time()",
        "Honeycomb, collection.method=prometheus-scrape: points land in the future (or the past)",
    ]
    params = {"instance": Param("inventory-1", "instance"), "seconds": Param(120.0, "skew (+future / -past)")}
    duration, ramp = 480.0, 1.0

    def apply(self, now, fx, x, eng):
        fx.clock_skew_s[self.p["instance"]] = self.p["seconds"]


@register
class NanValues(Scenario):
    key = "nan_values"
    title = "NaN and +Inf gauges"
    category = "metrics"
    summary = ("A stats reset makes the catalog's ratio gauges divide by zero: catalog_cache_hit_ratio becomes "
               "NaN and db_pool_utilization_ratio becomes +Inf. One bad series poisons avg() and sum().")
    look_for = [
        "avg(catalog_cache_hit_ratio) - NaN for the whole service",
        "avg(catalog_cache_hit_ratio >= 0)  (NaN fails every comparison, so this filters it out)",
        "max(db_pool_utilization_ratio) = +Inf",
        "What did the collector / Honeycomb do with NaN?",
    ]
    params = {"instance": Param("catalog-1", "instance")}
    duration, ramp = 480.0, 1.0

    def apply(self, now, fx, x, eng):
        fx.nan_gauges.add(self.p["instance"])


@register
class SeriesDisappear(Scenario):
    key = "series_disappear"
    title = "Series disappear (route retired)"
    category = "metrics"
    summary = ("The gateway's legacy endpoint is switched off. Its series simply stop being exposed. Prometheus "
               "marks them stale; alerts on them go quiet rather than firing. When it comes back the counters "
               "start from zero.")
    look_for = [
        "http_requests_total{route=\"/api/v1/legacy-products\"} - lines end",
        "absent(http_requests_total{route=\"/api/v1/legacy-products\"})",
        "rate(...) > 0 alert: silence is not success",
    ]
    params = {"route": Param("api-gateway GET /api/v1/legacy-products", "service + route key")}
    duration, ramp = 900.0, 1.0

    def apply(self, now, fx, x, eng):
        fx.retired_routes.add(self.p["route"])


# =====================================================================================================


class ScenarioManager:
    def __init__(self, eng):
        self.eng = eng
        self.active: dict[str, Scenario] = {}
        self.history: list[dict] = []

    def start(self, key, params=None, duration=None, ramp=None, hidden=False):
        cls = CATALOG.get(key)
        if cls is None:
            raise KeyError(f"unknown scenario {key!r}")
        now = time.time()
        if key in self.active:
            self._end(key)
        sc = cls(params, duration, ramp, now, hidden)
        sc.on_start(self.eng)
        self.active[key] = sc
        self.history.append({"event": "start", "key": key, "t": now, "params": sc.p, "hidden": hidden})
        if not hidden:
            self.eng.marker(f"scenario start: {key}", "scenario")
        return sc

    def start_mystery(self):
        pool = [k for k, c in CATALOG.items() if k not in self.active and k != "bad_deploy"]
        key = random.choice(pool)
        dur = random.choice([600, 720, 900])
        return self.start(key, duration=dur, hidden=True)

    def reveal(self):
        for sc in self.active.values():
            sc.hidden = False
        return [sc.describe() for sc in self.active.values()]

    def stop(self, key):
        now = time.time()
        if key == "all":
            for sc in self.active.values():
                sc.stop(now)
            return list(self.active)
        if key not in self.active:
            raise KeyError(f"{key!r} is not active")
        self.active[key].stop(now)
        return [key]

    def _end(self, key):
        sc = self.active.pop(key)
        sc.on_end(self.eng)
        self.history.append({"event": "end", "key": key, "t": time.time()})
        if not sc.hidden:
            self.eng.marker(f"scenario end: {key}", "scenario")

    def apply(self, now, fx):
        for key in list(self.active):
            sc = self.active[key]
            if now >= sc.ends:
                self._end(key)
                continue
            x = sc.intensity(now)
            sc.apply(now, fx, x, self.eng)
            fx.active["mystery" if sc.hidden else key] = x
