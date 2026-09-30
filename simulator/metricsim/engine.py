"""The tick loop. Once per second:

1. build a fresh Effects and let active scenarios mutate it
2. lifecycle: hosts going down/up, restarts, OOM kills, label/bucket modes
3. traffic: Poisson arrivals per front-door route, each request walks the call graph (world.py) and records
   what every hop's own Prometheus client would record, plus the same thing via OTLP if enabled
4. queue consumers, runtime metrics, host metrics, infra exporters, ground-truth metrics

Nothing here renders exposition text; the HTTP layer does that on scrape, which is why scrape timing matters.
"""

from __future__ import annotations

import logging
import math
import random
import time
from collections import defaultdict, deque

from . import prom
from .config import Config
from .effects import Effects
from .markers import Markers
from .scenarios import CATALOG, ScenarioManager
from .targets import GiB, AppInstance, KafkaTarget, NodeTarget, PostgresTarget, RedisTarget
from .util import lognormal, poisson, weighted
from .world import (
    DIRECT_GATEWAY, DIRECT_INVENTORY, INFRA_TARGETS, JOURNEYS, KAFKA_PARTITIONS, NODE_DISK, NOTIFY_CHANNELS,
    PAYMENT_METHODS, PAYMENT_PROVIDERS, SERVICES, SKUS, build_instances,
)

log = logging.getLogger("metricsim")

CARRIERS = [("ups", 0.45), ("fedex", 0.35), ("usps", 0.2)]
CONSUMER_MSGS_PER_S = 6.0  # per notifications instance at full speed
USE_ORDER = {"redis": 0, "postgres": 1, "provider": 3, "carrier": 3, "postgres_write": 4, "kafka": 5}


class Engine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.rng = random.Random(cfg.seed)
        now = time.time()
        self.t0 = now
        self.instances = [AppInstance(d, now, self.rng) for d in build_instances()]
        self.by_name = {i.name: i for i in self.instances}
        self._by_service = defaultdict(list)
        for i in self.instances:
            self._by_service[i.service].append(i)
        self.nodes: dict[str, NodeTarget] = {}
        self.infra = []
        for kind, name, node, port in INFRA_TARGETS:
            if kind == "node":
                t = NodeTarget(name, port, now, self.rng)
                self.nodes[name] = t
            elif kind == "postgres":
                t = PostgresTarget(name, port, node, self.rng, replica=name.endswith("replica"))
            elif kind == "redis":
                t = RedisTarget(name, port, node, self.rng)
            else:
                t = KafkaTarget(name, port, node, self.rng)
            self.infra.append(t)
        self.pg = next(t for t in self.infra if t.name == "postgres-primary")
        self.pg_replica = next(t for t in self.infra if t.name == "postgres-replica")
        self.redis = next(t for t in self.infra if t.kind == "redis")
        self.kafka = next(t for t in self.infra if t.kind == "kafka")
        self.queue = [deque() for _ in range(KAFKA_PARTITIONS)]
        self.scenarios = ScenarioManager(self)
        self.markers = Markers(cfg)
        self.fx = Effects()
        self.down_nodes: set[str] = set()
        self._rr = defaultdict(int)
        self._kafka_rr = 0
        self.stats = defaultdict(float)
        self.traffic_now = 0.0
        self.tick_seconds = 0.0
        self.otlp = None
        if cfg.otlp_enabled:
            from .otlp import OtlpManager

            self.otlp = OtlpManager(cfg)
            for inst in self.instances:
                inst.otlp = self.otlp.for_instance(inst)
        self._build_self_metrics()

    # ---- helpers used by scenarios ----------------------------------------------------------------------

    def by_service(self, service):
        return self._by_service.get(service, [])

    def marker(self, message, kind):
        log.info("marker [%s] %s", kind, message)
        self.markers.send(message, kind)

    def restart(self, inst: AppInstance, version=None, rename=False, reason="restart"):
        now = time.time()
        inst.restart(now, self.cfg.restart_downtime, rename)
        if version:
            inst.version = version
        inst.boot(now)
        inst.leaked_conns = 0.0
        if self.otlp and not inst.node_down:
            inst.otlp = self.otlp.for_instance(inst)
        self.stats["restarts"] += 1
        log.info("%s %s (version %s, identity %s)", reason, inst.name, inst.version, inst.identity)

    # ---- the tick ---------------------------------------------------------------------------------------

    def tick(self, now: float, dt: float):
        t_start = time.perf_counter()
        fx = Effects()
        self.scenarios.apply(now, fx)
        self.fx = fx
        self._lifecycle(now, fx)

        self.ups = {s: [i for i in insts if i.up] for s, insts in self._by_service.items()}
        self.acc = {
            "redis_hits": 0, "redis_misses": 0, "redis_cmds": 0, "pg_tx": 0.0, "pg_reads": 0.0,
            "net": defaultdict(float), "disk_write": defaultdict(float),
        }
        diurnal = self.diurnal(now)
        self.traffic_now = diurnal * fx.traffic_mult
        rps = self.cfg.base_rps * self.traffic_now
        for key, share in JOURNEYS:
            for _ in range(poisson(self.rng, rps * share * dt)):
                self.call("frontend", key)
        direct_mult = self.cfg.base_rps / 30.0 * self.traffic_now
        for key, r in DIRECT_GATEWAY:
            if f"api-gateway {key}" in fx.retired_routes:
                continue
            for _ in range(poisson(self.rng, r * direct_mult * dt)):
                self.call("api-gateway", key)
        for key, r in DIRECT_INVENTORY:
            for _ in range(poisson(self.rng, r * direct_mult * dt)):
                self.call("inventory", key)
        self._consume(now, dt)
        self._end_tick(now, dt)
        self.stats["ticks"] += 1
        self.tick_seconds = time.perf_counter() - t_start

    def diurnal(self, now):
        """A compressed 'day' (default 60 minutes) so you can watch seasonality without waiting 24h."""
        phase = 2 * math.pi * ((now % self.cfg.day_length) / self.cfg.day_length)
        v = 0.6 + 0.38 * math.sin(phase) + 0.07 * math.sin(3 * phase + 1.3) + self.rng.gauss(0, 0.025)
        return max(0.12, v)

    # ---- lifecycle --------------------------------------------------------------------------------------

    def _lifecycle(self, now, fx):
        # hosts
        for name, node in self.nodes.items():
            want_down = name in fx.node_down
            if want_down and name not in self.down_nodes:
                self.down_nodes.add(name)
                node.available = False
                log.info("node %s DOWN", name)
                for inst in self.instances:
                    if inst.node == name:
                        inst.node_down = True
                        if inst.otlp:
                            inst.otlp.shutdown()
                            inst.otlp = None
                for t in self.infra:
                    if t.node == name:
                        t.available = False
            elif not want_down and name in self.down_nodes:
                self.down_nodes.discard(name)
                log.info("node %s back UP (rebooted)", name)
                node.boot(now)
                node.available = True
                for t in self.infra:
                    if t.node == name:
                        t.available = True
                        if isinstance(t, RedisTarget):
                            t.boot(now)
                            t.hits = t.misses = t.cmds = t.evicted = 0.0
                        elif isinstance(t, PostgresTarget):  # pg_stat_* counters reset on server restart
                            t.commits = t.rollbacks = t.blks_hit = t.blks_read = t.deadlocks = 0.0
                            t.build()
                        # kafka offsets live on disk: they survive a reboot, which is realistic
                for inst in self.instances:
                    if inst.node == name:
                        inst.node_down = False
                        self.restart(inst, reason="boot after host recovery")
        # explicit restarts (crash loop)
        for name in fx.restart_now:
            inst = self.by_name.get(name)
            if inst and not inst.node_down:
                self.restart(inst, rename=name in fx.rename_on_restart, reason="crash")
        # per-instance modes
        for inst in self.instances:
            inst.set_label_mode(fx.extra_label_cardinality.get(inst.service, 0))
            inst.set_buckets(fx.buckets_override.get(inst.name, prom.DEFAULT_BUCKETS))
            for rk in fx.retired_routes:
                svc, _, route = rk.partition(" ")
                if svc == inst.service and not inst.svc.worker:
                    inst.retire_route(route)
        # postgres lives or dies with its disk
        full = self.nodes[self.pg.node].disk_frac >= 0.995
        if full and self.pg.db_up:
            log.info("postgres-primary: No space left on device -> shutting down")
        if not full and not self.pg.db_up:
            log.info("postgres-primary recovered")
        self.pg.db_up = not full and self.pg.available

    # ---- request handling --------------------------------------------------------------------------------

    def _pick(self, service):
        ups = self.ups.get(service) or []
        if not ups:
            return None
        self._rr[service] += 1
        return ups[self._rr[service] % len(ups)]

    def call(self, service, key):
        """Serve one request at `service`. Returns (status, latency_seconds) as seen by the caller."""
        fx, rng = self.fx, self.rng
        route = SERVICES[service].routes[key]
        inst = self._pick(service)
        if inst is None:
            return 503, 0.002  # connection refused: nobody to talk to
        node = self.nodes[inst.node]
        lat = lognormal(rng, route.median_ms / 1000.0, route.sigma)
        lat *= fx.latency_mult[service] * fx.latency_mult[inst.name] * node.slowdown * (1 + 3 * inst.gc_pressure)
        status = 200
        err_p = route.base_error + fx.error_add[service] + fx.error_add[f"{service} {key}"] + fx.error_add[inst.name]
        uses = sorted(route.uses, key=USE_ORDER.get)

        for u in (u for u in uses if USE_ORDER[u] < 2):
            st, l = self._use(u, inst, service)
            lat += l
            if st >= 400:
                status = st
                break
        if status < 400:
            for dep, dkey, p in route.calls:
                if p < 1 and rng.random() > p:
                    continue
                st, l = self.call(dep, dkey)
                lat += l
                if st >= 500:
                    status = 502 if service == "api-gateway" else 500
                    break
                if st >= 400:
                    status = st
                    break
        if status < 400:
            for u in (u for u in uses if USE_ORDER[u] >= 2):
                st, l = self._use(u, inst, service)
                lat += l
                if st >= 400:
                    status = st
                    break
        if status < 400 and rng.random() < err_p:
            status = 500
        if status == 200 and service == "frontend" and key == "GET /product/:id" and rng.random() < 0.015:
            status = 404  # bots asking for products that don't exist: 4xx noise that is not an outage
        if lat > route.timeout_s:
            lat, status = route.timeout_s, 504

        customer = f"cust-{rng.randint(1, inst.label_mode):05d}" if inst.label_mode else None
        inst.record_request(route, status, lat, customer)
        self.acc["net"][inst.node] += 3000
        if status < 300:
            self._business(inst, service, key)
        return status, lat

    def _use(self, u, inst, service):
        fx, rng = self.fx, self.rng
        if u == "redis":
            if not self.redis.available:
                return (500, 1.0) if service == "cart" else (200, 0.05)  # catalog degrades to postgres
            self.acc["redis_cmds"] += 1
            lat = lognormal(rng, 0.0004, 0.3) * self.nodes[self.redis.node].slowdown
            if service == "catalog":
                hit = rng.random() < max(0.0, 0.93 - fx.cache_hit_penalty)
                inst.cache_hits_window.append((time.time(), hit))
                inst.m_cache.inc(("hit" if hit else "miss",))
                self.acc["redis_hits" if hit else "redis_misses"] += 1
                if not hit:
                    st, l = self._use("postgres", inst, service)
                    return st, lat + l
            else:
                self.acc["redis_hits"] += 1
            return 200, lat
        if u in ("postgres", "postgres_write"):
            write = u == "postgres_write"
            if not self.pg.db_up:
                return 500, 0.003
            wait = self._pool_wait(inst)
            if wait >= 30.0:
                inst.m_pool_timeouts.inc()
                inst.m_pool_wait.observe((), 30.0)
                return 500, 30.0
            if inst.svc.db_pool_max:
                inst.m_pool_wait.observe((), wait)
            q = lognormal(rng, 0.004 if write else 0.002, 0.5) * fx.db_latency_mult
            q *= self.nodes[self.pg.node].slowdown
            if write and fx.db_lock_waiters:
                q += rng.random() * fx.db_lock_waiters * 0.01
            inst.t_db_hold += (q + 0.003) * fx.pool_hold_mult[service]
            if write:
                self.acc["pg_tx"] += 1
                self.acc["disk_write"][self.pg.node] += 16384
            else:
                self.acc["pg_reads"] += 1
            return 200, wait + q
        if u == "kafka":
            if not self.kafka.available:
                return 500, 1.0
            self._kafka_rr = (self._kafka_rr + 1) % KAFKA_PARTITIONS
            self.kafka.produced[self._kafka_rr] += 1
            self.queue[self._kafka_rr].append(time.time())
            return 200, lognormal(rng, 0.002, 0.3)
        if u == "provider":
            provider = weighted(rng, PAYMENT_PROVIDERS)
            lat = lognormal(rng, 0.18, 0.4) * fx.provider_latency_mult[provider]
            r = rng.random()
            if lat > 4.0:
                outcome, st, lat = "timeout", 504, 4.0
            elif r < 0.004 + fx.provider_error_add[provider]:
                outcome, st = "error", 502
            elif r < 0.044 + fx.provider_error_add[provider]:
                outcome, st = "declined", 402
            else:
                outcome, st = "approved", 200
            inst.m_pay.inc((provider, outcome))
            inst.m_provider.observe((provider,), lat)
            if inst.otlp:
                inst.otlp.payment(provider, outcome, lat)
            return st, lat
        if u == "carrier":
            carrier = weighted(rng, CARRIERS)
            inst.m_quotes.inc((carrier,))
            return 200, lognormal(rng, 0.09, 0.35) * fx.carrier_latency_mult
        return 200, 0.0

    def _pool_wait(self, inst):
        mx = inst.svc.db_pool_max
        if not mx:
            return 0.0
        mx = self.fx.pool_max_override.get(inst.service, mx)
        free = mx - inst.leaked_conns
        if free < 1:
            return 30.0
        eff = (inst.pool_util * mx - inst.leaked_conns) / free
        if eff < 0.7:
            return self.rng.uniform(0.00005, 0.0004)
        if eff < 1.0:
            return min(5.0, 0.002 / (1.0 - eff)) * self.rng.uniform(0.5, 1.5)
        return min(30.0, 1.0 + (eff - 1.0) * 20) * self.rng.uniform(0.7, 1.3)

    def _business(self, inst, service, key):
        rng = self.rng
        if service == "checkout":
            method = weighted(rng, PAYMENT_METHODS)
            value = round(lognormal(rng, 42.0, 0.7), 2)
            inst.m_orders.inc((method,))
            inst.m_order_value.observe((), value)
            inst.m_revenue.inc(("USD",), value)
            if inst.otlp:
                inst.otlp.order(method, value)
        elif service == "cart" and key == "POST /cart/items":
            inst.m_items.inc((), rng.randint(1, 3))
        elif service == "inventory" and key == "POST /reserve":
            sku = rng.choice(SKUS)
            if inst.stock[sku] <= 0:
                inst.m_reserve.inc(("out_of_stock",))
            else:
                inst.stock[sku] -= rng.randint(1, 2)
                inst.stock[sku] = max(0, inst.stock[sku])
                inst.m_reserve.inc(("reserved",))

    # ---- queue consumers -----------------------------------------------------------------------------

    def _consume(self, now, dt):
        consumers = self.ups.get("notifications") or []
        if not consumers or not self.kafka.available:
            return
        mult = self.fx.consumer_capacity_mult
        budget = len(consumers) * CONSUMER_MSGS_PER_S * mult * dt
        budget = int(budget) + (1 if self.rng.random() < budget - int(budget) else 0)
        ci = 0
        while budget > 0 and any(self.queue):
            for p in range(KAFKA_PARTITIONS):
                if budget <= 0 or not self.queue[p]:
                    continue
                produced_at = self.queue[p].popleft()
                self.kafka.consumed[p] += 1
                budget -= 1
                inst = consumers[ci % len(consumers)]
                ci += 1
                proc = lognormal(self.rng, 0.035, 0.4) / max(mult, 0.05) ** 0.5
                delay = max(0.0, now - produced_at) + proc
                channel = weighted(self.rng, NOTIFY_CHANNELS)
                outcome = "failed" if self.rng.random() < 0.01 else "sent"
                inst.m_consumed.inc(("orders",))
                inst.m_proc.observe((), proc)
                inst.m_sent.inc((channel, outcome))
                inst.m_delay.observe((), delay)
                inst.t_requests += 1
                if inst.otlp:
                    inst.otlp.notification(channel, outcome, proc, delay)

    # ---- end of tick: runtime, hosts, infra, ground truth ----------------------------------------------

    def _end_tick(self, now, dt):
        fx = self.fx
        node_cpu = defaultdict(float)
        node_rss = defaultdict(float)
        for inst in self.instances:
            if inst.node_down:
                continue
            if not inst.up:  # restarting: the process is gone, nothing to update
                continue
            inst.leaked_conns = min(float(inst.svc.db_pool_max or 0),
                                    inst.leaked_conns + fx.pool_leak_per_s.get(inst.service, 0.0) * dt)
            burst = fx.microburst.get(inst.service, 0.0)
            inst.end_tick(now, dt, fx, burst, inst.name in fx.nan_gauges)
            node_cpu[inst.node] += inst.last_cpu_cores
            node_rss[inst.node] += inst.rss
            # OOM killer
            if inst.runtime == "jvm" and inst.heap_used >= inst.heap_max * 0.97:
                log.info("%s: java.lang.OutOfMemoryError: Java heap space -> killed", inst.name)
                self.stats["oom_kills"] += 1
                self.restart(inst, reason="OOM kill")
            elif inst.rss > 3 * GiB:
                self.stats["oom_kills"] += 1
                self.restart(inst, reason="OOM kill")

        a = self.acc
        extra_mem = {"node-d": 6 * GiB, "node-c": 5 * GiB, "node-b": self.redis.used + 0.2 * GiB, "node-a": 3 * GiB}
        infra_cpu = {
            self.pg.node: 0.2 + a["pg_tx"] * 0.004 + a["pg_reads"] * 0.001 + fx.db_lock_waiters * 0.02,
            self.redis.node: 0.05 + a["redis_cmds"] * 0.0002,
            self.kafka.node: 0.3,
            self.pg_replica.node: 0.15,
        }
        for name, node in self.nodes.items():
            if name in self.down_nodes:
                continue
            node.update(dt, node_cpu[name] + infra_cpu.get(name, 0.0), node_rss[name], extra_mem[name],
                        a["net"][name], a["disk_write"][name] + 20_000, fx)

        if self.pg.available:
            active = sum(i.pool_active for i in self.instances if i.svc.db_pool_max and i.up)
            self.pg.update(dt, a["pg_tx"] / dt, a["pg_reads"] / dt, active + fx.db_lock_waiters, fx)
        if self.pg_replica.available:
            self.pg_replica.update(dt, 0, a["pg_reads"] * 0.1, 2, fx)
        if self.redis.available:
            clients = sum(1 for i in self.instances if i.service in ("catalog", "cart") and i.up) * 10
            self.redis.update(now, dt, a["redis_hits"], a["redis_misses"], a["redis_cmds"], clients, fx)
        if self.kafka.available:
            self.kafka.update(len(self.ups.get("notifications") or []))
        self._update_self_metrics(now)

    # ---- ground truth (job="simulator") ----------------------------------------------------------------

    def _build_self_metrics(self):
        r = self.self_reg = prom.Registry()
        self.m_sc_active = r.gauge("sim_scenario_active", "1 while a scenario is running (ground truth).",
                                   ("scenario", "category"))
        self.m_sc_int = r.gauge("sim_scenario_intensity", "Scenario intensity 0..1 (ground truth).", ("scenario",))
        self.m_target_up = r.gauge("sim_target_available", "Is the simulated process really up? (ground truth)",
                                   ("target", "kind"))
        self.m_traffic = r.gauge("sim_traffic_multiplier", "Current traffic multiplier (diurnal x surge).")
        self.m_tick = r.gauge("sim_tick_duration_seconds", "Wall time spent simulating the last 1s tick.")
        self.m_restarts = r.counter("sim_restarts_total", "Simulated process restarts.")
        self.m_series = r.gauge("sim_exposed_series", "Series each target currently exposes.", ("target",))

    def _update_self_metrics(self, now):
        active = self.fx.active
        for key, cls in CATALOG.items():
            self.m_sc_active.set((key, cls.category), 1 if key in active else 0)
            self.m_sc_int.set((key,), round(active.get(key, 0.0), 3))
        self.m_sc_active.set(("mystery", "unknown"), 1 if "mystery" in active else 0)
        self.m_sc_int.set(("mystery",), round(active.get("mystery", 0.0), 3))
        for t in self.targets():
            up = t.up if isinstance(t, AppInstance) else t.available
            self.m_target_up.set((t.name, t.kind), 1 if up else 0)
        self.m_traffic.set((), round(self.traffic_now, 3))
        self.m_tick.set((), round(self.tick_seconds, 4))
        self.m_restarts.series[()] = self.stats["restarts"]
        if int(now) % 15 == 0:
            for t in self.targets():
                self.m_series.set((t.name,), t.reg.series_count())

    def targets(self):
        return self.instances + self.infra

    def status(self):
        now = time.time()
        return {
            "uptime_s": round(now - self.t0),
            "traffic_multiplier": round(self.traffic_now, 3),
            "frontend_rps": round(self.cfg.base_rps * self.traffic_now, 1),
            "tick_ms": round(self.tick_seconds * 1000, 1),
            "restarts": int(self.stats["restarts"]),
            "oom_kills": int(self.stats["oom_kills"]),
            "otlp": bool(self.otlp),
            "active": [s.describe(now) if not s.hidden else {**s.describe(now), "key": "mystery", "title": "???",
                                                              "params": {}}
                       for s in self.scenarios.active.values()],
            "targets": [
                {"name": t.name, "identity": t.identity, "kind": t.kind, "node": t.node, "port": t.port,
                 "up": t.up if isinstance(t, AppInstance) else t.available,
                 "version": getattr(t, "version", None)}
                for t in self.targets()
            ],
        }
