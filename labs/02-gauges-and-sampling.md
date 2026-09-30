# Lab 02: Gauges and sampling

**Goal:** understand that a gauge is a *sample* of a value at scrape time, and what that means for spikes.

## 1. Gauges

```promql
http_requests_in_flight{job="api-gateway"}
cart_active_carts
inventory_stock_units
kafka_consumergroup_lag
```

Nothing to `rate()` here; the value itself means something. (Look at `inventory_stock_units`: stock drains,
then jumps back up when it's restocked.) Useful gauge functions:

```promql
avg_over_time(http_requests_in_flight{job="api-gateway"}[5m])
max_over_time(http_requests_in_flight{job="api-gateway"}[5m])
deriv(inventory_stock_units{sku="sku-001"}[10m])      # per-second slope of a gauge
```

`rate()` on a gauge is a mistake that Prometheus won't stop you from making. Try
`rate(inventory_stock_units[5m])` and think about what the restock does to it.

## 2. What the gauge can't see

```bash
docker compose exec simulator metricsim trigger microbursts --duration 10m
```

Every 30 s the gateway gets a **2 second** burst: concurrency jumps by ~150 and requests slow down ~12×.

```promql
http_requests_in_flight{job="api-gateway"}
max_over_time(http_requests_in_flight{job="api-gateway"}[5m])
```

You will mostly see nothing, sometimes one spike. Prometheus scrapes every 15 s and a burst lasts 2 s, so each scrape
has about a 2-in-15 chance of landing inside one.

Now the latency histogram:

```promql
histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket{job="api-gateway"}[1m])))
sum(rate(http_request_duration_seconds_bucket{job="api-gateway", le="+Inf"}[1m]))
  - sum(rate(http_request_duration_seconds_bucket{job="api-gateway", le="0.5"}[1m]))    # slow requests/s
```

The p99 shows the bursts every time.

**Why?** A histogram is a set of counters. Every request that happened between scrapes is counted, whenever it
happened. A gauge only reports whatever the value was at the moment of the scrape. **Counters integrate; gauges
sample.** If you care about peaks, measure them with a counter or histogram, or have the process track the max
since the last scrape. That's what `jvm_gc_pause_seconds_max` does, with its own caveats: it's a max over a
Micrometer window, not over your scrape interval.

## 3. Honeycomb

Filter to `collection.method = prometheus-scrape`:

> **Honeycomb:** SELECT `MAX(http_requests_in_flight)` WHERE `service.name = api-gateway`, granularity 15 s, then 5 min.

The default temporal aggregation for a gauge is **LAST** in each step. At 5 min granularity, what happens to a
spike that was captured in the middle of a step? Compare with Prometheus `max_over_time(...[5m])`, which keeps it.

> **Honeycomb:** SELECT `HEATMAP(http_request_duration_seconds)` and `P99(http_request_duration_seconds)` WHERE
> `service.name = api-gateway`.

The heatmap shows the bursts as a band of slow requests.

In `collection.method = otlp-push`, the SDK's `http.server.active_requests` is an *observable* instrument, sampled at export
time (every 15 s): the same blind spot as a scrape, just pushed instead of pulled.

## Questions

1. Make a rule: which of these should be counters and which gauges? queue depth, bytes sent, temperature, number
   of errors, current connections, CPU time, CPU utilization %.
2. `node_load1` is a gauge, but it's an exponentially-weighted moving average computed by the kernel. How does that
   change what a 15 s sample tells you?
3. Increase the scrape interval to 60 s for the `sim-apps` job in `prometheus/prometheus.yml` (reload with
   `curl -X POST localhost:9090/-/reload`). Which panels in the starter dashboard get worse, and which don't care?
   Put it back afterwards.

```bash
docker compose exec simulator metricsim stop all
```
