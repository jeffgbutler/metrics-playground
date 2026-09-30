"""Scrape targets: one per simulated process. Each owns a `Registry` that the HTTP layer renders on scrape.

App instances expose the metrics a real service built with that language's standard client library would,
including the warts:
  * go      -> go_* runtime metrics, `go_gc_duration_seconds` is a SUMMARY
  * jvm     -> Micrometer-style `jvm_*`, `jvm_gc_pause_seconds` summary with no quantiles + a `_max` gauge
  * python  -> python_gc_* counters, python_info
  * node    -> prom-client style, including `nodejs_active_handles_total`, a GAUGE whose name ends in _total

Infra targets imitate node_exporter, postgres_exporter, redis_exporter and kafka_exporter metric names.
"""

from __future__ import annotations

import hashlib
import math
import random
import time

from . import prom
from .world import (
    GiB, KAFKA_PARTITIONS, NODE_CPUS, NODE_DISK, NODE_MEM, SERVICES, SKUS, InstanceDef,
)

MiB = 1024**2

LATENCY_BUCKETS = prom.DEFAULT_BUCKETS
POOL_BUCKETS = (0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1, 5, 10, 30)
VALUE_BUCKETS = (5, 10, 25, 50, 100, 250, 500, 1000)
DELAY_BUCKETS = (0.1, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600)

RUNTIME_PROFILE = {
    #          rss MiB, cpu ms/request, goroutines/threads base
    "go": (60, 0.4, 40),
    "jvm": (720, 1.2, 45),
    "python": (95, 2.5, 8),
    "node": (130, 1.5, 11),
}


class Target:
    kind = "app"

    def __init__(self, name: str, port: int, node: str):
        self.name = name
        self.identity = name
        self.port = port
        self.node = node
        self.reg = prom.Registry()
        self.available = True

    def sd_labels(self) -> dict:
        raise NotImplementedError


# =====================================================================================================
# Application instances
# =====================================================================================================


class AppInstance(Target):
    kind = "app"

    def __init__(self, d: InstanceDef, now: float, rng: random.Random):
        super().__init__(d.name, d.port, d.node)
        self.d = d
        self.rng = rng
        self.service = d.service
        self.svc = SERVICES[d.service]
        self.runtime = self.svc.runtime
        self.version = self.svc.version
        self.restarts = 0
        self.down_until = 0.0  # > now while restarting
        self.node_down = False
        self.label_mode = 0
        self.buckets = LATENCY_BUCKETS
        self.otlp = None  # set by engine when OTLP push is enabled
        self.boot(now - rng.uniform(2, 6) * 86400)  # pretend we have been up for days

    # ---- lifecycle ------------------------------------------------------------------------------------

    def boot(self, now: float):
        self.start_time = now
        self.reg = prom.Registry()
        self.cpu_seconds = 0.0
        self.heap_old = 0.0
        self.heap_young = 0.0
        self.leaked = 0.0
        self.gc_pressure = 0.0
        self.alloc_total = 0.0
        self.pool_util = 0.0
        self.pool_active = 0.0
        self.pool_pending = 0.0
        self.leaked_conns = 0.0
        self.heap_used = self.heap_max = 0.0
        self.last_cpu_cores = 0.0
        self.rss = 0.0
        self.inflight = 0.0
        self.cache_hits_window = []
        self.carts = 800.0
        self.stock = {s: self.rng.randint(150, 400) for s in SKUS}
        self._reset_tick()
        self._build()

    def restart(self, now: float, downtime: float, rename: bool = False):
        self.restarts += 1
        self.down_until = now + downtime
        if rename:
            h = hashlib.sha1(f"{self.name}-{self.restarts}-{now}".encode()).hexdigest()[:5]
            self.identity = f"{self.name}-{h}"
        if self.otlp:
            self.otlp.shutdown()

    @property
    def up(self) -> bool:
        return not self.node_down and time.time() >= self.down_until

    def _reset_tick(self):
        self.t_requests = 0
        self.t_latency = 0.0
        self.t_db_hold = 0.0
        self.t_bytes = 0

    # ---- metric definitions ---------------------------------------------------------------------------

    def _http_labels(self):
        base = ("method", "route", "status")
        return base + (("customer_id",) if self.label_mode else ())

    def _build(self):
        r = self.reg
        if not self.svc.worker:
            self._rebuild_http(counter=True)
            self.m_inflight = r.gauge("http_requests_in_flight", "Requests currently being served.")
        self.m_build = r.gauge(
            "app_build_info", "Always 1. The labels carry build metadata (join on it).", ("version", "commit", "runtime")
        )
        # process_* (every official client library exposes these)
        self.m_cpu = r.counter("process_cpu_seconds_total", "Total user and system CPU time spent in seconds.")
        self.m_rss = r.gauge("process_resident_memory_bytes", "Resident memory size in bytes.")
        self.m_vms = r.gauge("process_virtual_memory_bytes", "Virtual memory size in bytes.")
        self.m_start = r.gauge("process_start_time_seconds", "Start time of the process since unix epoch in seconds.")
        self.m_fds = r.gauge("process_open_fds", "Number of open file descriptors.")
        self.m_maxfds = r.gauge("process_max_fds", "Maximum number of open file descriptors.")

        rt = self.runtime
        if rt == "go":
            self.m_goroutines = r.gauge("go_goroutines", "Number of goroutines that currently exist.")
            self.m_threads = r.gauge("go_threads", "Number of OS threads created.")
            self.m_heap = r.gauge("go_memstats_heap_alloc_bytes", "Heap bytes allocated and still in use.")
            self.m_alloc = r.counter("go_memstats_alloc_bytes_total", "Total bytes allocated, even if freed.")
            self.m_gc = r.summary(
                "go_gc_duration_seconds", "A summary of the wall-time pause (stop-the-world) duration in GC cycles.",
                quantiles=(0.0, 0.25, 0.5, 0.75, 1.0), max_age=600,
            )
            r.gauge("go_info", "Information about the Go environment.", ("version",)).set(("go1.25.1",), 1)
        elif rt == "jvm":
            self.m_jvm_used = r.gauge("jvm_memory_used_bytes", "The amount of used memory", ("area", "id"))
            self.m_jvm_max = r.gauge("jvm_memory_max_bytes", "The maximum amount of memory", ("area", "id"))
            self.m_jvm_threads = r.gauge("jvm_threads_live_threads", "The current number of live threads")
            self.m_gc = r.summary(
                "jvm_gc_pause_seconds", "Time spent in GC pause", ("action", "cause"), quantiles=(), max_age=60
            )
            self.m_gc_max = r.gauge("jvm_gc_pause_seconds_max", "Time spent in GC pause (max in the last ~2 min)",
                                    ("action", "cause"))
            self._gc_max_window = []
        elif rt == "python":
            self.m_pygc = r.counter("python_gc_collections_total", "Number of times this generation was collected",
                                    ("generation",))
            self.m_pyobj = r.counter("python_gc_objects_collected_total", "Objects collected during gc", ("generation",))
            r.gauge("python_info", "Python platform information", ("implementation", "major", "minor", "version")).set(
                ("CPython", "3", "12", "3.12.11"), 1
            )
        elif rt == "node":
            self.m_lag = r.gauge("nodejs_eventloop_lag_seconds", "Lag of event loop in seconds.")
            self.m_heap_used = r.gauge("nodejs_heap_size_used_bytes", "Process heap size used from Node.js in bytes.")
            self.m_heap_total = r.gauge("nodejs_heap_size_total_bytes", "Process heap size from Node.js in bytes.")
            # prom-client really does expose this as a gauge. The name lies.
            self.m_handles = r.gauge("nodejs_active_handles_total", "Total number of active handles.")

        if self.svc.db_pool_max:
            self.m_pool = r.gauge("db_pool_connections", "Connections in the pool by state.", ("state",))
            self.m_pool_max = r.gauge("db_pool_connections_max", "Maximum size of the connection pool.")
            self.m_pool_util = r.gauge("db_pool_utilization_ratio", "active / max, computed by the app.")
            self.m_pool_wait = r.histogram("db_pool_acquire_duration_seconds", "Time waiting for a connection.",
                                           buckets=POOL_BUCKETS)
            self.m_pool_timeouts = r.counter("db_pool_timeouts_total", "Connection acquire timeouts (30s).")

        s = self.service
        if s == "catalog":
            self.m_cache = r.counter("catalog_cache_requests_total", "Product cache lookups.", ("result",))
            self.m_hit_ratio = r.gauge("catalog_cache_hit_ratio",
                                       "hits / (hits + misses) over the last 30s. NaN when there were no lookups.")
        elif s == "cart":
            self.m_items = r.counter("cart_items_added_total", "Items added to carts.")
            self.m_carts = r.gauge("cart_active_carts", "Carts touched in the last 30 minutes.")
        elif s == "checkout":
            self.m_orders = r.counter("orders_placed_total", "Orders successfully placed.", ("payment_method",))
            self.m_order_value = r.histogram("order_value_dollars", "Order value in USD.", buckets=VALUE_BUCKETS)
            self.m_revenue = r.counter("revenue_dollars_total", "Revenue in USD (a float counter).", ("currency",))
        elif s == "payments":
            self.m_pay = r.counter("payment_attempts_total", "Charge attempts by provider and outcome.",
                                   ("provider", "outcome"))
            self.m_provider = r.summary("payment_provider_latency_seconds",
                                        "Latency of the external provider call (client-side quantiles).",
                                        ("provider",), quantiles=(0.5, 0.9, 0.99), max_age=120)
        elif s == "inventory":
            self.m_stock = r.gauge("inventory_stock_units", "Units on hand.", ("sku",))
            self.m_reserve = r.counter("inventory_reservations_total", "Reservation attempts.", ("outcome",))
        elif s == "shipping":
            self.m_quotes = r.counter("shipping_quotes_total", "Quotes requested from carriers.", ("carrier",))
        elif s == "notifications":
            self.m_consumed = r.counter("notifications_messages_consumed_total", "Messages consumed.", ("topic",))
            self.m_proc = r.histogram("notifications_processing_duration_seconds", "Time to process one message.")
            self.m_sent = r.counter("notifications_sent_total", "Notifications sent.", ("channel", "outcome"))
            self.m_delay = r.histogram(
                "notifications_delivery_delay_seconds",
                "Order placed -> customer notified. Grows with consumer lag.",
                buckets=DELAY_BUCKETS,
            )

    # ---- modes driven by scenarios ------------------------------------------------------------------------

    def set_label_mode(self, n: int):
        """Cardinality explosion: add (or remove) a `customer_id` label. Old series vanish, new ones appear."""
        if n == self.label_mode or self.svc.worker:
            return
        self.label_mode = n
        self._rebuild_http(counter=True)

    def set_buckets(self, buckets):
        """A new bucket layout is a new set of `le` series; the old ones simply stop being exposed."""
        if tuple(buckets) == tuple(self.buckets) or self.svc.worker:
            return
        self.buckets = tuple(buckets)
        self._rebuild_http(counter=False)

    def _rebuild_http(self, counter: bool):
        extra = ("customer_id",) if self.label_mode else ()
        if counter:
            self.reg.drop("http_requests_total")
            self.m_requests = self.reg.counter("http_requests_total", "Total HTTP requests handled.",
                                               self._http_labels())
        self.reg.drop("http_request_duration_seconds")
        self.m_duration = self.reg.histogram("http_request_duration_seconds",
                                             "HTTP request latency, measured at the server.",
                                             ("method", "route") + extra, self.buckets)

    def retire_route(self, route_key: str):
        method, path = route_key.split(" ", 1)
        for m in (self.m_requests, self.m_duration):
            m.remove_matching(method=method, route=path)

    # ---- recording -------------------------------------------------------------------------------------

    def record_request(self, route, status: int, latency: float, customer: str | None = None):
        lbl = (route.method, route.path, str(status))
        hl = (route.method, route.path)
        if self.label_mode:
            lbl += (customer,)
            hl += (customer,)
        self.m_requests.inc(lbl)
        self.m_duration.observe(hl, latency)
        self.t_requests += 1
        self.t_latency += latency
        if self.otlp:
            self.otlp.request(route, status, latency, customer if self.label_mode else None)

    # ---- per-tick runtime update ---------------------------------------------------------------------

    def end_tick(self, now: float, dt: float, fx, burst: float, nan_mode: bool):
        rss_base, cpu_ms, base_threads = RUNTIME_PROFILE[self.runtime]
        rps = self.t_requests / dt
        self.inflight = self.t_latency / dt + burst  # Little's law: L = lambda * W
        if not self.svc.worker:
            self.m_inflight.set((), round(self.inflight + self.rng.random() * 0.3, 2) if self.inflight else 0)

        cpu = (0.012 + rps * cpu_ms / 1000.0 + self.gc_pressure * 0.8) * dt
        self.cpu_seconds += cpu
        self.last_cpu_cores = cpu / dt
        self.m_cpu.series[()] = self.cpu_seconds

        leak_rate = fx.heap_leak_bytes_per_s.get(self.name, 0.0)
        self.leaked += leak_rate * dt
        self.m_build.series.clear()
        self.m_build.set((self.version, _commit(self.service, self.version), self.runtime), 1)
        self.m_start.set((), round(self.start_time, 3))
        self.m_fds.set((), 20 + int(self.inflight) + self.rng.randint(0, 3))
        self.m_maxfds.set((), 1048576)

        rt = self.runtime
        rss = rss_base * MiB + self.leaked
        if rt == "go":
            heap = 22 * MiB + rps * 0.15 * MiB + self.leaked + self.rng.random() * 3 * MiB
            self.m_heap.set((), heap)
            self.alloc_total += rps * 40_000 * dt + 200_000
            self.m_alloc.series[()] = self.alloc_total
            self.m_goroutines.set((), base_threads + int(self.inflight * 1.3) + self.rng.randint(0, 4))
            self.m_threads.set((), 12 + self.rng.randint(0, 1))
            if self.rng.random() < 0.2 + rps / 200:
                self.m_gc.observe((), self.rng.uniform(4e-5, 3e-4) * (1 + heap / (512 * MiB)), now)
            rss = heap * 1.6 + 30 * MiB
        elif rt == "jvm":
            heap_max = 1024 * MiB
            self.heap_young += (rps * 0.6 * MiB + 1.5 * MiB) * dt
            self.heap_old = max(self.heap_old, 180 * MiB) + rps * 0.002 * MiB * dt
            used_old = self.heap_old + self.leaked
            young_cap = max(8 * MiB, heap_max * 0.95 - used_old)
            self.gc_pressure = max(0.0, (used_old / heap_max) - 0.75) * 4  # 0 .. ~1 as the old gen fills
            if self.heap_young >= min(300 * MiB, young_cap):
                pause = self.rng.uniform(0.004, 0.018) * (1 + 6 * self.gc_pressure)
                self._gc("end of minor GC", "G1 Evacuation Pause", pause, now)
                self.heap_young = self.rng.uniform(0, 10) * MiB
            if self.heap_old > 520 * MiB:
                self._gc("end of major GC", "G1 Compaction Pause", self.rng.uniform(0.15, 0.4), now)
                self.heap_old = 200 * MiB
            if self.gc_pressure > 0.5 and self.rng.random() < self.gc_pressure * 0.3:
                self._gc("end of major GC", "Allocation Failure", self.rng.uniform(0.3, 1.2), now)
            used = used_old + self.heap_young
            self.m_jvm_used.set(("heap", "G1 Eden Space"), self.heap_young)
            self.m_jvm_used.set(("heap", "G1 Old Gen"), used_old)
            self.m_jvm_used.set(("nonheap", "Metaspace"), 96 * MiB + self.rng.random() * MiB)
            self.m_jvm_max.set(("heap", "G1 Eden Space"), -1)  # Micrometer reports -1 for "undefined"
            self.m_jvm_max.set(("heap", "G1 Old Gen"), heap_max)
            self.m_jvm_max.set(("nonheap", "Metaspace"), -1)
            self.m_jvm_threads.set((), base_threads + int(self.inflight) + self.rng.randint(0, 3))
            cutoff = now - 120
            self._gc_max_window = [x for x in self._gc_max_window if x[0] >= cutoff]
            self.m_gc_max.series.clear()
            for t, key, pause in self._gc_max_window:
                self.m_gc_max.set(key, max(self.m_gc_max.get(key), pause))
            for key in list(self.m_gc.series):
                self.m_gc_max.series.setdefault(key, 0.0)
            rss = 420 * MiB + used * 0.9
            self.heap_used = used
            self.heap_max = heap_max
        elif rt == "python":
            for gen, p, objs in (("0", 0.9, 400), ("1", 0.09, 1500), ("2", 0.008, 9000)):
                if self.rng.random() < p * (0.3 + rps / 20):
                    self.m_pygc.inc((gen,))
                    self.m_pyobj.inc((gen,), self.rng.randint(objs // 2, objs))
                self.m_pygc.series.setdefault((gen,), 0.0)
                self.m_pyobj.series.setdefault((gen,), 0.0)
        elif rt == "node":
            lag = 0.0015 + (self.inflight / 400) + self.rng.random() * 0.001
            self.m_lag.set((), round(lag, 6))
            hu = 58 * MiB + rps * 0.4 * MiB + self.leaked + self.rng.random() * 6 * MiB
            self.m_heap_used.set((), hu)
            self.m_heap_total.set((), max(hu * 1.35, 90 * MiB))
            self.m_handles.set((), 14 + int(self.inflight) + self.rng.randint(0, 2))
            rss = hu * 1.5 + 50 * MiB
        self.rss = rss
        self.m_rss.set((), round(rss))
        self.m_vms.set((), round(rss * 3.4 + 300 * MiB))

        if self.svc.db_pool_max:
            mx = fx.pool_max_override.get(self.service, self.svc.db_pool_max)
            demand = self.t_db_hold / dt + self.leaked_conns  # leaked connections never come back
            self.pool_util = demand / mx
            self.pool_active = min(mx, demand)
            self.pool_pending = max(0.0, demand - mx)
            idle = max(0, mx - round(self.pool_active))
            self.m_pool.set(("active",), round(self.pool_active))
            self.m_pool.set(("idle",), idle)
            self.m_pool.set(("pending",), round(self.pool_pending))
            self.m_pool_max.set((), mx)
            self.m_pool_util.set((), float("inf") if nan_mode else round(self.pool_active / mx, 3))
            self.m_pool_timeouts.series.setdefault((), 0.0)

        if self.service == "catalog":
            cutoff = now - 30
            self.cache_hits_window = [x for x in self.cache_hits_window if x[0] >= cutoff]
            h = sum(1 for x in self.cache_hits_window if x[1])
            n = len(self.cache_hits_window)
            self.m_hit_ratio.set((), float("nan") if (nan_mode or n == 0) else round(h / n, 4))
            for res in ("hit", "miss"):
                self.m_cache.series.setdefault((res,), 0.0)
        elif self.service == "cart":
            self.carts += (rps * 0.4 - self.carts / 1800) * dt + self.rng.gauss(0, 2)
            self.m_carts.set((), max(0, round(self.carts)))
            self.m_items.series.setdefault((), 0.0)
        elif self.service == "inventory":
            for sku, units in self.stock.items():
                self.m_stock.set((sku,), units)
        self._reset_tick()

    def _gc(self, action, cause, pause, now):
        self.m_gc.observe((action, cause), pause, now)
        self._gc_max_window.append((now, (action, cause), pause))

    # ---- service discovery ------------------------------------------------------------------------------

    def sd_labels(self):
        return {
            "__meta_service": self.service,
            "__meta_instance": self.identity,
            "__meta_node": self.node,
            "__meta_team": self.svc.team,
            "__meta_runtime": self.runtime,
        }


def _commit(service, version):
    return hashlib.sha1(f"{service}@{version}".encode()).hexdigest()[:7]


# =====================================================================================================
# Infrastructure
# =====================================================================================================


class NodeTarget(Target):
    """node_exporter lookalike for one simulated host."""

    kind = "node"
    MODES = ("user", "system", "iowait", "irq", "softirq", "steal", "nice", "idle")

    def __init__(self, name, port, now, rng):
        super().__init__(name, port, name)
        self.rng = rng
        self.disk_used = NODE_DISK * rng.uniform(0.38, 0.52)
        self.disk_baseline = self.disk_used
        self.down = False
        self.boot(now - rng.uniform(20, 60) * 86400, first=True, now_real=now)
        self.busy_cores = 0.2
        self.steal = 0.0
        self.slowdown = 1.0

    def boot(self, now, first=False, now_real=None):
        self.boot_time = now
        self.reg = prom.Registry()
        r = self.reg
        self.m_cpu = r.counter("node_cpu_seconds_total", "Seconds the CPUs spent in each mode.", ("cpu", "mode"))
        self.m_load = {w: r.gauge(f"node_load{w}", f"{w}m load average.") for w in (1, 5, 15)}
        self.m_memtotal = r.gauge("node_memory_MemTotal_bytes", "Memory information field MemTotal_bytes.")
        self.m_memavail = r.gauge("node_memory_MemAvailable_bytes", "Memory information field MemAvailable_bytes.")
        self.m_memfree = r.gauge("node_memory_MemFree_bytes", "Memory information field MemFree_bytes.")
        self.m_cached = r.gauge("node_memory_Cached_bytes", "Memory information field Cached_bytes.")
        fs = ("device", "fstype", "mountpoint")
        self.m_fs_size = r.gauge("node_filesystem_size_bytes", "Filesystem size in bytes.", fs)
        self.m_fs_avail = r.gauge("node_filesystem_avail_bytes", "Filesystem space available to non-root users.", fs)
        self.m_fs_free = r.gauge("node_filesystem_free_bytes", "Filesystem free space in bytes.", fs)
        self.m_rx = r.counter("node_network_receive_bytes_total", "Network device statistic receive_bytes.", ("device",))
        self.m_tx = r.counter("node_network_transmit_bytes_total", "Network device statistic transmit_bytes.", ("device",))
        self.m_dread = r.counter("node_disk_read_bytes_total", "The total number of bytes read successfully.", ("device",))
        self.m_dwrite = r.counter("node_disk_written_bytes_total", "The total number of bytes written successfully.",
                                  ("device",))
        self.m_dio = r.counter("node_disk_io_time_seconds_total", "Total seconds spent doing I/Os.", ("device",))
        self.m_boot = r.gauge("node_boot_time_seconds", "Node boot time, in unixtime.")
        r.gauge("node_uname_info", "Labeled system information as provided by the uname system call.",
                ("machine", "nodename", "release", "sysname")).set(("x86_64", self.name, "6.8.0-1024-aws", "Linux"), 1)
        self.load = {1: 0.3, 5: 0.3, 15: 0.3}
        self.cpu_acc = {(str(c), m): 0.0 for c in range(NODE_CPUS) for m in self.MODES}
        self.rx = self.tx = self.dr = self.dw = self.dio = 0.0
        if first:
            # A box that has been up for weeks has big counters: raw values are scary, rate() doesn't care.
            # After a reboot (first=False) the kernel starts every counter from zero again.
            up = now_real - self.boot_time
            for k in self.cpu_acc:
                self.cpu_acc[k] = up * (0.9 if k[1] == "idle" else 0.015) * self.rng.uniform(0.8, 1.2)
            self.rx = up * self.rng.uniform(2e5, 6e5)
            self.tx = self.rx * 0.8
            self.dr = up * self.rng.uniform(1e5, 3e5)
            self.dw = self.dr * 1.5
            self.dio = up * 0.02

    def sd_labels(self):
        return {"__meta_service": "node", "__meta_instance": self.name, "__meta_node": self.name,
                "__meta_team": "infra"}

    def update(self, dt, busy_cores, app_rss, extra_mem, net_bytes, disk_write_bytes, fx):
        self.steal = fx.node_steal.get(self.name, 0.0)
        cores = min(float(NODE_CPUS), busy_cores + 0.15 + fx.node_cpu_hog.get(self.name, 0.0))
        self.busy_cores = cores
        util = cores / NODE_CPUS
        self.slowdown = 1 + max(0.0, util - 0.65) * 5 + self.steal * 4
        per_cpu_busy = cores / NODE_CPUS
        iow = min(0.3, disk_write_bytes / 2e8)
        for c in range(NODE_CPUS):
            j = self.rng.uniform(0.85, 1.15)
            busy = min(1.0, per_cpu_busy * j)
            steal = min(1 - busy, self.steal)
            shares = {
                "user": busy * 0.72, "system": busy * 0.2, "softirq": busy * 0.04, "irq": busy * 0.01,
                "nice": busy * 0.03, "iowait": min(1 - busy - steal, iow * j), "steal": steal,
            }
            shares["idle"] = max(0.0, 1 - sum(shares.values()))
            for m, v in shares.items():
                self.cpu_acc[(str(c), m)] += v * dt
        for k, v in self.cpu_acc.items():
            self.m_cpu.series[k] = v
        runnable = cores + iow * NODE_CPUS + (self.steal * NODE_CPUS)
        for w in (1, 5, 15):
            a = math.exp(-dt / (60.0 * w))
            self.load[w] = self.load[w] * a + runnable * (1 - a)
            self.m_load[w].set((), round(self.load[w], 2))
        used = 1.6 * GiB + app_rss + extra_mem
        cached = max(0.0, min(NODE_MEM - used, 5 * GiB + self.rng.uniform(-0.2, 0.2) * GiB))
        self.m_memtotal.set((), NODE_MEM)
        self.m_memavail.set((), max(0, round(NODE_MEM - used)))
        self.m_memfree.set((), max(0, round(NODE_MEM - used - cached)))
        self.m_cached.set((), round(cached))

        fill = fx.disk_fill_bytes_per_s.get(self.name, 0.0)
        if fill:
            self.disk_used = min(NODE_DISK, self.disk_used + fill * dt)
        elif self.disk_used > self.disk_baseline + 1e8:
            self.disk_used -= (self.disk_used - self.disk_baseline) * 0.02 * dt  # log cleanup cron catches up
        self.disk_used += disk_write_bytes * 1e-4
        root = ("/dev/nvme0n1p1", "ext4", "/")
        reserved = NODE_DISK * 0.05  # ext4 reserves 5% for root: avail < free, always
        self.m_fs_size.set(root, NODE_DISK)
        self.m_fs_free.set(root, max(0, round(NODE_DISK - self.disk_used)))
        self.m_fs_avail.set(root, max(0, round(NODE_DISK - self.disk_used - reserved)))
        run = ("tmpfs", "tmpfs", "/run")
        self.m_fs_size.set(run, 1.6 * GiB)
        self.m_fs_free.set(run, 1.6 * GiB - 3 * MiB)
        self.m_fs_avail.set(run, 1.6 * GiB - 3 * MiB)

        self.rx += net_bytes * 1.1 + 20_000 * dt
        self.tx += net_bytes * 0.9 + 15_000 * dt
        self.m_rx.series[("eth0",)] = self.rx
        self.m_tx.series[("eth0",)] = self.tx
        self.m_rx.series[("lo",)] = self.rx * 0.1
        self.m_tx.series[("lo",)] = self.rx * 0.1
        self.dw += disk_write_bytes + 40_000 * dt
        self.dr += disk_write_bytes * 0.3 + 10_000 * dt
        self.dio += min(dt, 0.02 * dt + disk_write_bytes / 3e8)
        self.m_dread.series[("nvme0n1",)] = self.dr
        self.m_dwrite.series[("nvme0n1",)] = self.dw
        self.m_dio.series[("nvme0n1",)] = self.dio
        self.m_boot.set((), round(self.boot_time))

    @property
    def disk_frac(self):
        return self.disk_used / NODE_DISK


class PostgresTarget(Target):
    """postgres_exporter lookalike. Note `pg_up` (can the exporter reach the DB?) vs Prometheus's `up`
    (can Prometheus reach the exporter?). They fail independently."""

    kind = "postgres"

    def __init__(self, name, port, node, rng, replica=False):
        super().__init__(name, port, node)
        self.rng = rng
        self.replica = replica
        self.db_up = True
        self.commits = rng.uniform(1e8, 3e8)
        self.rollbacks = self.commits * 0.002
        self.blks_hit = self.commits * 40
        self.blks_read = self.blks_hit * 0.01
        self.deadlocks = float(rng.randint(3, 20))
        self.size = rng.uniform(40, 60) * GiB
        self.build()

    def build(self):
        r = self.reg = prom.Registry()
        self.m_up = r.gauge("pg_up", "Whether the last scrape of metrics from PostgreSQL was able to connect.")
        d = ("datname",)
        # postgres_exporter's counters do NOT end in _total. Real-world naming is messy.
        self.m_commit = r.counter("pg_stat_database_xact_commit", "Transactions committed.", d)
        self.m_rollback = r.counter("pg_stat_database_xact_rollback", "Transactions rolled back.", d)
        self.m_hit = r.counter("pg_stat_database_blks_hit", "Disk blocks found in the buffer cache.", d)
        self.m_read = r.counter("pg_stat_database_blks_read", "Disk blocks read.", d)
        self.m_deadlocks = r.counter("pg_stat_database_deadlocks", "Deadlocks detected.", d)
        self.m_activity = r.gauge("pg_stat_activity_count", "Connections by state.", ("datname", "state"))
        self.m_maxconn = r.gauge("pg_settings_max_connections", "Server Parameter: max_connections.")
        self.m_size = r.gauge("pg_database_size_bytes", "Disk space used by the database.", d)
        self.m_locks = r.gauge("pg_locks_count", "Number of locks.", ("datname", "mode"))
        if self.replica:
            self.m_lag = r.gauge("pg_replication_lag_seconds", "Replication lag behind primary in seconds.")

    def sd_labels(self):
        return {"__meta_service": "postgres", "__meta_instance": self.name, "__meta_node": self.node,
                "__meta_team": "data", "__meta_role": "replica" if self.replica else "primary"}

    def update(self, dt, tx, reads, active_conns, fx):
        db = ("bytemart",)
        self.m_up.set((), 1 if self.db_up else 0)
        if not self.db_up:
            for m in list(self.reg.metrics.values()):
                if m is not self.m_up:
                    m.series.clear()
            return
        if self.replica:
            tx, active_conns = tx * 0.0, 2
        self.commits += tx * dt
        self.rollbacks += tx * 0.002 * dt
        hit_ratio = 0.99 - (0.1 if fx.cache_hit_penalty else 0)
        self.blks_hit += reads * 40 * hit_ratio * dt + tx * 20 * dt
        self.blks_read += reads * 40 * (1 - hit_ratio) * dt
        if fx.db_lock_waiters and self.rng.random() < 0.02 * fx.db_lock_waiters:
            self.deadlocks += 1
        self.size += tx * 2048 * dt
        self.m_commit.series[db] = self.commits
        self.m_rollback.series[db] = self.rollbacks
        self.m_hit.series[db] = self.blks_hit
        self.m_read.series[db] = self.blks_read
        self.m_deadlocks.series[db] = self.deadlocks
        self.m_activity.set(("bytemart", "active"), round(active_conns))
        self.m_activity.set(("bytemart", "idle"), max(0, 34 - round(active_conns)))
        self.m_activity.set(("bytemart", "idle in transaction"), round(fx.db_lock_waiters * 0.5))
        self.m_maxconn.set((), 100)
        self.m_size.set(db, round(self.size))
        self.m_locks.set(("bytemart", "accessexclusivelock"), round(fx.db_lock_waiters * 0.2))
        self.m_locks.set(("bytemart", "rowexclusivelock"), round(active_conns * 0.6 + fx.db_lock_waiters))
        self.m_locks.set(("bytemart", "accesssharelock"), round(active_conns * 1.5))
        if self.replica:
            self.m_lag.set((), round(max(0.0, self.rng.uniform(0, 0.4) + fx.replica_lag_s), 3))


class RedisTarget(Target):
    kind = "redis"

    def __init__(self, name, port, node, rng):
        super().__init__(name, port, node)
        self.rng = rng
        self.hits = rng.uniform(1e8, 2e8)
        self.misses = self.hits * 0.08
        self.evicted = 0.0
        self.cmds = self.hits * 1.7
        self.maxmem = 2 * GiB
        self.used = 1.1 * GiB
        self.boot(time.time() - rng.uniform(10, 30) * 86400)

    def boot(self, now):
        self.start = now
        r = self.reg = prom.Registry()
        self.m_up = r.gauge("redis_up", "Information about the Redis instance")
        self.m_clients = r.gauge("redis_connected_clients", "Connected clients")
        self.m_used = r.gauge("redis_memory_used_bytes", "Memory used")
        self.m_max = r.gauge("redis_memory_max_bytes", "maxmemory setting")
        self.m_hits = r.counter("redis_keyspace_hits_total", "Keyspace hits")
        self.m_misses = r.counter("redis_keyspace_misses_total", "Keyspace misses")
        self.m_evicted = r.counter("redis_evicted_keys_total", "Evicted keys")
        self.m_cmds = r.counter("redis_commands_processed_total", "Commands processed")
        self.m_keys = r.gauge("redis_db_keys", "Total number of keys by DB", ("db",))
        self.m_uptime = r.gauge("redis_uptime_in_seconds", "Uptime in seconds (a gauge that behaves like a counter)")

    def sd_labels(self):
        return {"__meta_service": "redis", "__meta_instance": self.name, "__meta_node": self.node,
                "__meta_team": "data"}

    def update(self, now, dt, hits, misses, cmds, clients, fx):
        maxmem = self.maxmem * fx.redis_maxmemory_frac
        target_used = min(maxmem * 0.985, 1.1 * GiB + (hits + misses) * 200)
        self.used += (target_used - self.used) * 0.2
        if self.used >= maxmem * 0.98:
            self.evicted += (cmds * 0.6 + 50) * dt
        self.hits += hits
        self.misses += misses
        self.cmds += cmds
        self.m_up.set((), 1)
        self.m_clients.set((), clients)
        self.m_used.set((), round(self.used))
        self.m_max.set((), round(maxmem))
        self.m_hits.series[()] = self.hits
        self.m_misses.series[()] = self.misses
        self.m_evicted.series[()] = self.evicted
        self.m_cmds.series[()] = self.cmds
        self.m_keys.set(("db0",), round(self.used / 1800))
        self.m_uptime.set((), round(now - self.start))


class KafkaTarget(Target):
    """kafka_exporter lookalike. Lag is exposed directly AND derivable from the two offsets."""

    kind = "kafka"

    def __init__(self, name, port, node, rng):
        super().__init__(name, port, node)
        r = self.reg
        self.m_brokers = r.gauge("kafka_brokers", "Number of Brokers in the Kafka Cluster.")
        self.m_parts = r.gauge("kafka_topic_partitions", "Number of partitions for this Topic", ("topic",))
        pl = ("topic", "partition")
        self.m_cur = r.gauge("kafka_topic_partition_current_offset", "Current Offset of a Broker at Topic/Partition", pl)
        gl = ("consumergroup", "topic", "partition")
        self.m_gcur = r.gauge("kafka_consumergroup_current_offset", "Current Offset of a ConsumerGroup at Topic/Partition", gl)
        self.m_lag = r.gauge("kafka_consumergroup_lag", "Current Approximate Lag of a ConsumerGroup at Topic/Partition", gl)
        self.m_members = r.gauge("kafka_consumergroup_members", "Amount of members in a consumer group", ("consumergroup",))
        base = rng.randint(4_000_000, 6_000_000)
        self.produced = [float(base + rng.randint(0, 1000)) for _ in range(KAFKA_PARTITIONS)]
        self.consumed = list(self.produced)

    def sd_labels(self):
        return {"__meta_service": "kafka", "__meta_instance": self.name, "__meta_node": self.node,
                "__meta_team": "data"}

    def update(self, members):
        self.m_brokers.set((), 3)
        self.m_parts.set(("orders",), KAFKA_PARTITIONS)
        self.m_members.set(("notifications",), members)
        for p in range(KAFKA_PARTITIONS):
            self.m_cur.set(("orders", str(p)), round(self.produced[p]))
            self.m_gcur.set(("notifications", "orders", str(p)), round(self.consumed[p]))
            self.m_lag.set(("notifications", "orders", str(p)), round(self.produced[p] - self.consumed[p]))
