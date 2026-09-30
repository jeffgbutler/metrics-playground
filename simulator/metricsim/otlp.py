"""The same application metrics, pushed natively with the OpenTelemetry SDK (OTLP/HTTP -> collector).

One MeterProvider per simulated instance, because in real life each process has its own SDK and its own
Resource (service.name, service.instance.id, service.version, host.name). A restart creates a new provider,
just like a real process restart.

Things worth noticing when you compare this with the Prometheus-scraped data:
  * names follow OTel semantic conventions (`http.server.request.duration`, unit "s"), not `_seconds_bucket`
  * the status code is an attribute on the duration histogram itself, so one instrument answers RED
  * temporality and histogram type are chosen by env vars, not by the metric:
        OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE = cumulative | delta | lowmemory
        OTEL_EXPORTER_OTLP_METRICS_DEFAULT_HISTOGRAM_AGGREGATION =
            explicit_bucket_histogram | base2_exponential_bucket_histogram
  * the OTel default explicit buckets are [0, 5, 10, 25, ... 10000] - made for milliseconds. Only the HTTP
    histogram here passes second-scaled bucket advice; switch to explicit buckets and look at what happens
    to `messaging.process.duration` percentiles.
"""

from __future__ import annotations

import logging
import threading

from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource

log = logging.getLogger("metricsim.otlp")

HTTP_BUCKETS = [0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1, 2.5, 5, 7.5, 10]


class OtlpManager:
    def __init__(self, cfg):
        self.cfg = cfg

    def for_instance(self, inst) -> "InstanceMeters":
        return InstanceMeters(inst, self.cfg)


class InstanceMeters:
    def __init__(self, inst, cfg):
        self.inst = inst
        res = Resource.create({
            "service.name": inst.service,
            "service.namespace": "bytemart",
            "service.version": inst.version,
            "service.instance.id": inst.identity,
            "host.name": inst.node,
            "deployment.environment.name": "playground",
            "process.runtime.name": inst.runtime,
        })
        reader = PeriodicExportingMetricReader(OTLPMetricExporter(), export_interval_millis=cfg.otlp_interval * 1000)
        self.provider = MeterProvider(resource=res, metric_readers=[reader])
        m = self.provider.get_meter("bytemart.metricsim", "1.0.0")
        svc = inst.svc

        if not svc.worker:
            self.h_dur = m.create_histogram(
                "http.server.request.duration", unit="s", description="Duration of HTTP server requests.",
                explicit_bucket_boundaries_advisory=HTTP_BUCKETS,
            )
            m.create_observable_up_down_counter(
                "http.server.active_requests", [self._obs(lambda: round(inst.inflight))], unit="{request}",
                description="Number of active HTTP server requests.",
            )
        m.create_observable_counter("process.cpu.time", [self._obs(lambda: inst.cpu_seconds)], unit="s")
        m.create_observable_up_down_counter("process.memory.usage", [self._obs(lambda: round(inst.rss))], unit="By")
        if svc.db_pool_max:
            pool = {"db.client.connection.pool.name": f"{inst.service}-pool"}

            def conns(_opts: CallbackOptions):
                used = round(inst.pool_active)
                return [
                    Observation(used, {**pool, "db.client.connection.state": "used"}),
                    Observation(max(0, svc.db_pool_max - used), {**pool, "db.client.connection.state": "idle"}),
                ]

            m.create_observable_up_down_counter("db.client.connection.count", [conns], unit="{connection}")
            m.create_observable_up_down_counter(
                "db.client.connection.max", [self._obs(lambda: svc.db_pool_max, pool)], unit="{connection}"
            )
            m.create_observable_up_down_counter(
                "db.client.connection.pending_requests", [self._obs(lambda: round(inst.pool_pending), pool)],
                unit="{request}",
            )
        if inst.service == "checkout":
            self.c_orders = m.create_counter("shop.orders.placed", unit="{order}")
            self.h_value = m.create_histogram("shop.order.value", unit="USD")
            self.c_revenue = m.create_counter("shop.revenue", unit="USD")
        if inst.service == "payments":
            self.c_pay = m.create_counter("shop.payment.attempts", unit="{attempt}")
            self.h_provider = m.create_histogram("shop.payment.provider.duration", unit="s")
        if inst.service == "notifications":
            self.h_proc = m.create_histogram("messaging.process.duration", unit="s")  # no bucket advice!
            self.c_sent = m.create_counter("shop.notifications.sent", unit="{notification}")
            self.h_delay = m.create_histogram("shop.notification.delivery_delay", unit="s")

    @staticmethod
    def _obs(fn, attrs=None):
        def cb(_opts: CallbackOptions):
            try:
                return [Observation(fn(), attrs or {})]
            except Exception:
                return []

        return cb

    # ---- recording hooks called by the engine ----------------------------------------------------------

    def request(self, route, status, latency, customer=None):
        attrs = {"http.request.method": route.method, "http.route": route.path, "http.response.status_code": status}
        if status >= 500:
            attrs["error.type"] = str(status)
        if customer:
            attrs["customer.id"] = customer
        self.h_dur.record(latency, attrs)

    def order(self, method, value):
        self.c_orders.add(1, {"payment.method": method})
        self.h_value.record(value)
        self.c_revenue.add(value, {"currency": "USD"})

    def payment(self, provider, outcome, latency):
        a = {"payment.provider": provider, "payment.outcome": outcome}
        self.c_pay.add(1, a)
        self.h_provider.record(latency, {"payment.provider": provider})

    def notification(self, channel, outcome, proc, delay):
        self.h_proc.record(proc, {"messaging.system": "kafka", "messaging.destination.name": "orders"})
        self.c_sent.add(1, {"notification.channel": channel, "notification.outcome": outcome})
        self.h_delay.record(delay)

    def shutdown(self):
        # flushing can block for seconds if the collector is unreachable; never do that on the tick loop
        threading.Thread(target=self._shutdown, daemon=True).start()

    def _shutdown(self):
        try:
            self.provider.shutdown(timeout_millis=5000)
        except Exception as exc:
            log.debug("otlp shutdown: %s", exc)
