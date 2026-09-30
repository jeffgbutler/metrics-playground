# Lab 03: Histograms and summaries

**Goal:** understand how percentiles come out of metrics, when they're wrong, and why OpenTelemetry's exponential
histograms exist.

## 1. Anatomy of a classic histogram

```bash
curl -s localhost:9106/metrics | grep 'http_request_duration_seconds.*route="/products"'
```

Buckets are **cumulative**: `le="0.05"` counts every request ≤ 50 ms, including those ≤ 10 ms. `+Inf` = `_count`.
Each bucket is its own counter series, so one histogram with 11 buckets and 3 label combinations is 39 series.

```promql
histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket{job="catalog"}[5m])))
histogram_quantile(0.50, sum by (le) (rate(http_request_duration_seconds_bucket{job="catalog"}[5m])))
sum(rate(http_request_duration_seconds_sum{job="catalog"}[5m])) / sum(rate(http_request_duration_seconds_count{job="catalog"}[5m]))
```

The mean is exact. The quantiles are **estimates**: Prometheus finds the bucket the 99th percentile falls into and
interpolates linearly inside it. With default buckets, anything between 250 ms and 500 ms is a guess.

Note the `sum by (le)`. You must keep `le` when aggregating, or there's nothing left to compute a quantile from.
Try `histogram_quantile(0.99, sum(rate(...bucket[5m])))` in the Prometheus UI: an empty result plus a warning that `le` is missing.

## 2. Buckets that lie

```bash
docker compose exec simulator metricsim trigger bad_buckets --duration 12m --param mode=too_narrow
```

A "new release" of catalog uses buckets that top out at **100 ms**, and at the same time catalog gets ~4× slower.

```promql
histogram_quantile(0.99, sum by (le, route) (rate(http_request_duration_seconds_bucket{job="catalog"}[1m])))
sum by (route) (rate(http_request_duration_seconds_sum{job="catalog"}[1m])) / sum by (route) (rate(http_request_duration_seconds_count{job="catalog"}[1m]))
sum by (le) (rate(http_request_duration_seconds_bucket{job="catalog", route="/search"}[1m]))
```

Look at `/search`. After a couple of minutes its p99 is pinned at exactly **0.1** while its *mean* is about **0.2 s**:
a 99th percentile lower than the average. When the quantile lands in the `+Inf` bucket, `histogram_quantile` returns
the highest *finite* bound, because it has nothing else to go on. Your SLO dashboard now says "p99 is fine" during
an incident.

Also look at the first minute or two after the scenario starts, with a `[5m]` window. The old bucket series and the
new ones are both inside the window, so you get a *mismatched* layout (next exercise) for free. Every bucket change
does this for one range-window's worth of time.

Stop it and try the other mode:

```bash
docker compose exec simulator metricsim stop bad_buckets
docker compose exec simulator metricsim trigger bad_buckets --duration 12m --param mode=mismatched
```

Only `catalog-1` gets different bucket boundaries (0.02, 0.04, 0.08, 0.3, ...). Run the same p99 query in the
[Prometheus UI](http://localhost:9090/query){: data-link="prometheus" data-path="/query" }
(not Grafana), and read the **info** annotation under the result. Then:

```promql
sum by (le) (rate(http_request_duration_seconds_bucket{job="catalog"}[5m]))
```

Some `le` values now come from one instance only. The "cumulative" counts are no longer monotonic, Prometheus has to
patch them up, and the p99 can come out as something absurd (we saw 10 s while real latency was ~50 ms).
**Lesson: you can only aggregate histograms whose buckets match.**

### Honeycomb

Via `collection.method = prometheus-scrape` the histogram arrives as a real histogram (`MetricInfo` = `histogram(cumulative)`)
with explicit bounds.

> **Honeycomb:** SELECT `P99(http_request_duration_seconds)`, `HEATMAP(http_request_duration_seconds)` WHERE
> `service.name = catalog` AND `collection.method = prometheus-scrape` during each mode.

Honeycomb still only has the buckets it was sent. Does its p99 hit the same 0.1 s ceiling in `too_narrow` mode? What does
it do with the mismatched instance? Record what you see; this is an experiment, not a quiz.

Via `otlp-push`, `http.server.request.duration` is an **exponential histogram** (`MetricInfo` shows it). There
are no bucket boundaries to choose: the SDK picks a scale so relative error stays small, and adjusts it when the
range of values grows.

> **Honeycomb:** SELECT `P99(http.server.request.duration)` WHERE `service.name = catalog` during `too_narrow`.

Same requests, no ceiling. That is the main argument for exponential histograms.

Now a single comparison query: SELECT `P99(http_request_duration_seconds)` WHERE `service.name = catalog`,
GROUP BY `collection.method`. You should get one line (scrape). Why is there no `prometheus-federate` line? (Lab 05.)

## 3. Summaries: quantiles you can't add up

`payment_provider_latency_seconds` is a **summary**: each payments instance computes its own p50/p90/p99 over a
sliding 2-minute window and exposes the result.

```bash
docker compose exec simulator metricsim trigger payment_provider_slow --duration 10m
```

```promql
payment_provider_latency_seconds{quantile="0.99"}
avg by (provider) (payment_provider_latency_seconds{quantile="0.99"})   # looks plausible, is meaningless
```

**Why meaningless?** The average of two p99s is not the p99 of the combined traffic. If one instance served 10× the
requests, its p99 should dominate. Averaging treats them equally. There is no correct way to combine
precomputed quantiles. Summaries are fine for "how is *this instance* doing" and useless for fleet-wide SLOs. Use
histograms for anything you want to aggregate. (You *can* aggregate a summary's `_sum` and `_count` for an exact mean.)

Also compare the latency inherited up the chain while this runs:

```promql
histogram_quantile(0.99, sum by (job, le) (rate(http_request_duration_seconds_bucket{job=~"payments|checkout|api-gateway|frontend"}[5m])))
```

Honeycomb: find `payment_provider_latency_seconds` (`collection.method = prometheus-scrape`). What `MetricInfo` does a
Prometheus summary become, and what can you do with it? Then compare `P99(shop.payment.provider.duration)` (`otlp-push`):
the same measurements as a histogram.

## 4. The OTel default-bucket trap (optional)

In `.env` set `OTLP_HISTOGRAM=explicit_bucket_histogram`, then `docker compose up -d simulator`. The SDK now uses
explicit buckets. The HTTP histogram passes "advice" with second-scaled buckets, but `messaging.process.duration`
(notifications) doesn't, so it gets the OTel default boundaries `[0, 5, 10, 25, 50, ... 10000]`. Those were
designed for **milliseconds**, and our values are in seconds.

> **Honeycomb:** `P50`, `P99` of `messaging.process.duration` before and after.

Put `OTLP_HISTOGRAM=base2_exponential_bucket_histogram` back when you're done.

## Questions

1. Given only buckets, what's the worst-case error of a p99 that lands in the 250–500 ms bucket?
2. Why is `sum by (le)` legal but `avg by (le)` usually a mistake for buckets?
3. You own an SLO "99% of checkouts under 1 s". Is there a `le` bucket at exactly 1 s? Why does that matter more
   than any percentile estimate?
4. What would it cost (in series) to add 10 more buckets to every HTTP histogram in Byte Mart?

```bash
docker compose exec simulator metricsim stop all
```
