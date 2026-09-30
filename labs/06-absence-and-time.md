# Lab 06: Absence, staleness and time

**Goal:** learn to reason about data that *isn't there*, data that's poisoned, and data from the wrong time. This
is where monitoring systems fail quietly.

## 1. A host dies

```bash
docker compose exec simulator metricsim trigger node_down --duration 5m --param node=node-c
```

```promql
up == 0
sum by (job) (up) / count by (job) (up)                     # fraction of each service that's reachable
sum by (instance) (rate(http_requests_total{job="api-gateway"}[1m]))   # surviving instance picks up the load
ALERTS{alertname="TargetDown"}                              # pending first, firing after `for: 1m`
```

On http://localhost:9090/targets the errors say `connection refused`. The simulator really closes those sockets.

When the host comes back (5 min), look at `node_boot_time_seconds{instance="node-c"}` and at the node's counters.
It rebooted, so everything on it starts from zero.

### Honeycomb

* `collection.method = prometheus-scrape`: the collector's receiver also writes `up` (we checked: it sends `up = 0` for the
  unreachable targets). SELECT `MIN(up)` GROUP BY `service.instance.id`.
* `collection.method = otlp-push`: there's nothing. A dead process doesn't push. Nothing in the data says "down". The data just
  stops. How would you alert on that in Honeycomb? Look at what a trigger can do when a query returns no rows.
  This is the pull-vs-push difference in one picture.

## 2. Alive but unscrapeable

```bash
docker compose exec simulator metricsim trigger scrape_timeout --duration 6m
```

`checkout-1`'s `/metrics` handler now takes 12 s, longer than the 10 s scrape timeout.

```promql
up{instance="checkout-1"}
scrape_duration_seconds{instance="checkout-1"}
sum(rate(http_requests_total{job="api-gateway", route="/api/checkout", status="200"}[1m]))   # checkout works fine
```

`TargetDown` will fire for a service that is serving traffic normally. Via `otlp-push`, `checkout-1` doesn't
even have a gap: push doesn't care how slow the scrape endpoint is.

* Which signal do you trust when `up` and the caller's view disagree?
* How would you write an alert that pages for "checkout is broken" and not for "checkout's /metrics is slow"?

## 3. Series that just stop

```bash
docker compose exec simulator metricsim trigger series_disappear --duration 10m
```

The gateway retires `/api/v1/legacy-products`. Its series are no longer exposed at all.

```promql
http_requests_total{route="/api/v1/legacy-products"}
absent(http_requests_total{route="/api/v1/legacy-products"})
```

The lines end within one scrape. **Why so quickly?** When a series vanishes from a scrape, Prometheus writes a
*staleness marker* so queries stop returning it immediately. Without markers, instant queries would keep returning
the last value for the 5-minute *lookback* window.

Now read the `LegacyEndpointErrors` alert in [prometheus/rules/alerts.yml](../prometheus/rules/alerts.yml). With no
series there is no result, and no result means no alert. **A metric that disappears can't fire an alert.** That's
why `absent()` exists, and why real setups pair important alerts with "the data is still there" alerts.

When the route comes back (scenario end), what value do its counters start at?

## 4. Poison: NaN and +Inf

```bash
docker compose exec simulator metricsim trigger nan_values --duration 8m
```

```promql
catalog_cache_hit_ratio
avg(catalog_cache_hit_ratio)            # NaN: one bad series poisons the aggregate
avg(catalog_cache_hit_ratio >= 0)       # NaN fails every comparison, so this drops it
max(db_pool_utilization_ratio)          # +Inf
```

(`catalog_cache_hit_ratio` goes NaN naturally too: it's hits/(hits+misses) over 30 s, and a catalog instance with
no traffic divides zero by zero. The scenario just forces it.)

The collector passes `NaN` and `+Inf` straight through (we checked its debug output). What does **Honeycomb** do with
them? SELECT `AVG(catalog_cache_hit_ratio)` and `MAX(db_pool_utilization_ratio)` WHERE `service.name = catalog`,
GROUP BY `service.instance.id`, `collection.method`, and write down what you find. Does federation (which went
through Prometheus's storage first) treat NaN the same as the direct scrape?

Now compare with the counter-based version, which can't produce NaN per series:

```promql
sum(rate(catalog_cache_requests_total{result="hit"}[5m])) / sum(rate(catalog_cache_requests_total[5m]))
```

**Lesson:** export the counters and do the division at query time. A ratio gauge is a pre-computed answer, and
it can be wrong in ways you can't fix afterwards.

## 5. The wrong time

```bash
docker compose exec simulator metricsim trigger clock_skew --duration 6m --param seconds=120
```

`inventory-1` now puts an explicit timestamp on every sample, **two minutes in the future**. Explicit timestamps
are legal in the exposition format, just rarely used. Federation uses them, for example.

```promql
http_requests_in_flight{instance="inventory-1"}
timestamp(http_requests_in_flight{instance="inventory-1"}) - time()
up{instance="inventory-1"}      # Prometheus's own series: normal timestamps
```

While it runs, the newest samples are invisible: they're "in the future" until the wall clock catches up. Once the
scenario ends, the samples go back to the real time, which is now *earlier* than what's already stored. Prometheus
rejects them as out of order for about two minutes:

```promql
sum(increase(prometheus_target_scrapes_sample_out_of_order_total[10m]))
count_over_time(http_requests_in_flight{instance="inventory-1"}[10m])   # vs inventory-2
```

(We saw ~600 samples rejected after a two-minute skew.) The collector forwards whatever timestamp it's given. Find
`inventory-1` (`collection.method = prometheus-scrape`): where on the time axis did its points land? And via
`prometheus-federate`, where Prometheus had already rejected the out-of-order ones?

## Questions

1. Name three different reasons a series could have no recent data. How would you tell them apart?
2. Pull (Prometheus) gives you `up` for free. Push (OTLP) doesn't. What *does* push give you that pull doesn't?
3. Why is a ratio gauge worse than two counters? Think about aggregation, NaN, and changing the window afterwards.
4. Where else do explicit timestamps show up? (Federation, which you saw in lab 05; exporters that relay data from
   elsewhere, like CloudWatch.) What does that do to "how fresh is this?"

```bash
docker compose exec simulator metricsim stop all
```
