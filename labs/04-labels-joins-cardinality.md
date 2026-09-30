# Lab 04: Labels, joins and cardinality

**Goal:** see labels as the thing that makes metrics powerful *and* expensive, and see how differently Prometheus
and Honeycomb pay for them.

## 1. Info metrics and joins

`app_build_info` is always 1. Its value is useless; its labels are the point:

```promql
app_build_info
count by (job, version) (app_build_info)
```

Why not put `version` on every metric? Because each label value multiplies series, and the version changes on
every deploy. The Prometheus convention is to keep it on one info series per target and *join* it in when needed.

```bash
docker compose exec simulator metricsim trigger bad_deploy --duration 12m
```

`catalog` rolls out 1.9.0, one instance every 60 s. The new version is slower and throws errors. At the end it's
rolled back.

```promql
count by (version) (app_build_info{job="catalog"})

# 5xx per second by version: "multiply" by the info metric to copy its version label across
sum by (version) (
    rate(http_requests_total{job="catalog", status=~"5.."}[1m])
  * on (instance) group_left (version)
    app_build_info{job="catalog"}
)
```

Read it slowly:
* `* on (instance)` matches each request series with the `app_build_info` series from the same instance.
* `group_left (version)` means many left series can match one right series, and copy `version` onto the result.
* Multiplying by 1 leaves the value alone. The whole trick is to borrow the label.

Also look at `changes(process_start_time_seconds{job="catalog"}[15m])` and the counter resets from the rolling
restarts. Everything from lab 01 applies.

### Honeycomb

Via **`otlp-push`**, `service.version` is a resource attribute on every data point:

> **Honeycomb:** SELECT `SUM(http.server.request.duration.count)` WHERE `service.name = catalog` AND
> `http.response.status_code >= 500`, GROUP BY `service.version`.

No join needed: the attribute is already on the data.

With **`collection.method = prometheus-scrape`**, try to answer the same question. `app_build_info` is there, but its
`version` attribute only exists on `app_build_info`'s own series. `http_requests_total` series don't have it, and
Honeycomb has no join between metrics. What would you have to change in the collector (or the app) to make the
question answerable? (One answer: a
collector processor that copies version onto every series from the same target, or better, have the app put it
on its resource. That's what OTel SDKs do.)

## 2. Cardinality: Prometheus

Get a baseline first:

```promql
prometheus_tsdb_head_series
sum by (job) (scrape_samples_scraped)
count by (__name__) ({job="cart"})
```

and open [TSDB status](http://localhost:9090/tsdb-status){: data-link="prometheus" data-path="/tsdb-status" }
(top metrics by series count, top label names by value count).

```bash
docker compose exec simulator metricsim trigger cardinality_explosion --duration 15m
```

A developer added `customer_id` as a label on `http_requests_total` and the duration histogram for `cart`. 150
customers. Each new customer who shows up creates new series: one counter per status and 12 buckets + sum + count
per route.

```promql
scrape_samples_scraped{job="cart"}
scrape_series_added{job="cart"}
prometheus_tsdb_head_series
topk(5, sum by (customer_id) (rate(http_requests_total{job="cart"}[5m])))
```

Refresh `/tsdb-status` and watch `customer_id` climb the label-value table.

Now the cliff. Prometheus's `sim-apps` job has `sample_limit: 10000`. If a single scrape returns more samples than
that, **the whole scrape is thrown away**:

```bash
docker compose exec simulator metricsim trigger cardinality_explosion --duration 20m --param customers=800
docker compose exec simulator metricsim trigger traffic_surge --duration 20m --param mult=4   # new customers arrive faster
```

Watch `scrape_samples_scraped{job="cart"}` climb toward 10000, then `up{job="cart"}` drop to **0**. The error on the
[targets page](http://localhost:9090/targets){: data-link="prometheus" data-path="/targets" } says why.
You've lost *every* metric from cart, including CPU and memory, not just the high-cardinality ones. The protection
works, and it takes the whole target with it.

## 3. Cardinality: Honeycomb

The collector's scrape path has **no** sample_limit, so it keeps sending.

> **Honeycomb:** SELECT `SUM(RATE(http_requests_total, 60))` WHERE `service.name = cart` AND `collection.method = prometheus-scrape`,
> GROUP BY `customer_id`, ORDER BY descending, LIMIT 10.
>
> Then SELECT `COUNT_DISTINCT(customer_id)` for the same filter.

The `otlp-push` data has the same story with a `customer.id` attribute on `http.server.request.duration`.

Grouping by a high-cardinality attribute is normal for Honeycomb. It's still a new series per customer in
Honeycomb's metrics store too, and that is what grows. Find out how your Honeycomb plan meters metrics (series?
data points?) and watch your usage page. Look at the Grafana dashboard panel
"Collector: data points / s by path" before and during the scenario, and check your Honeycomb usage page.

## Questions

1. In Prometheus, which grew faster: the number of series or the number of samples per scrape? What's the
   difference?
2. With `sample_limit` in place, what is the *first* signal that tells an on-call engineer something is wrong,
   and does it point at the cause?
3. `customer_id` is useful in Honeycomb and dangerous in Prometheus. Where is the line for you? `route`?
   `status`? `sku`? `user_agent`? `trace_id`?
4. Metrics are a poor place for per-customer questions in *either* tool. Why might a wide event per request (a
   trace span) be the better home for `customer_id`? What would you lose?

```bash
docker compose exec simulator metricsim stop all
```
