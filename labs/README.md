# Lab workbook

Hands-on exercises, roughly in order. Each lab has you **break something**, **look at it in Prometheus/Grafana**,
**look at the same thing in Honeycomb**, and then answer a few questions about why they differ. The questions matter
more than the steps. Write your answers down (the [scorecard](scorecard.md) is a place for them).

| # | Lab | You'll learn | Scenarios |
|---|---|---|---|
| 00 | [Orientation](00-orientation.md) | exposition format, targets, service discovery, relabeling, what each collection method sends to Honeycomb | none |
| 01 | [Counters and rate()](01-counters.md) | cumulative counters, `rate`/`irate`/`increase`, resets, extrapolation, Honeycomb temporal aggregation | `crash_loop` |
| 02 | [Gauges and sampling](02-gauges-and-sampling.md) | what a gauge can't tell you, `*_over_time`, why counters beat gauges | `microbursts` |
| 03 | [Histograms and summaries](03-histograms-and-summaries.md) | buckets, `histogram_quantile`, when quantiles lie, summaries vs histograms, exponential histograms | `bad_buckets`, `payment_provider_slow` |
| 04 | [Labels, joins, cardinality](04-labels-joins-cardinality.md) | vector matching, info metrics, series cardinality vs Honeycomb's model | `bad_deploy`, `cardinality_explosion` |
| 05 | [Three roads to Honeycomb](05-three-paths.md) | direct scrape vs federation vs OTLP push, temporality, what federation throws away | none, then `crash_loop` |
| 06 | [Absence, staleness and time](06-absence-and-time.md) | `up`, staleness, `absent()`, silent alerts, NaN, clock skew | `node_down`, `scrape_timeout`, `series_disappear`, `nan_values`, `clock_skew` |
| 07 | [Infrastructure and correlation](07-infrastructure.md) | USE method, connecting app symptoms to host causes, forecasting, alert design | `noisy_neighbor`, `disk_fill`, `memory_leak`, `db_pool_exhaustion`, `queue_backlog`, `cache_eviction` |
| 08 | [Incident drills](08-incident-drills.md) | putting it together under uncertainty, building dashboards in both tools | mystery mode |

Read these in a browser at **http://localhost:8081** (linked from the control UI). Every PromQL query there has
**Grafana** and **Prometheus** buttons that open it ready to run, and shell commands have a copy button.

## Before you start

* `docker compose up -d --build` and let it run **at least 15 minutes** before lab 01. An hour is better for lab 07
  (one full simulated day of traffic).
* Stop everything between labs so scenarios don't overlap: `docker compose exec simulator metricsim stop all`.
* Grafana **Explore** (compass icon) is the best place to type PromQL. Prometheus's own UI at
  http://localhost:9090/query is plainer but shows warnings and "info" annotations Grafana hides. Use both.
* In Honeycomb, pick the environment your ingest key points at. All the playground's metrics land in one metrics
  store. Every data point carries a resource attribute **`collection.method`** that tells you which road it took:
  `prometheus-scrape`, `prometheus-federate`, `otlp-push` or `hostmetrics`. Most Honeycomb steps in the labs start
  with `WHERE collection.method = ...`, and the most useful comparisons are `GROUP BY collection.method`.

## Conventions used in the labs

```promql
# PromQL goes in blocks like this: paste into Grafana Explore or Prometheus
```

> **Honeycomb:** query-builder steps look like this. `SUM(RATE(http_requests_total, 60))` is shorthand for "SELECT
> the SUM of the metric with the RATE temporal function and a 60 s range interval". If your builder doesn't take
> that form directly, create a calculated field `RATE($http_requests_total, 60)` and SELECT `SUM` of it.

**Why?** sections explain the mechanism. Read them *after* you've tried to answer the questions yourself.

## Two mental models to keep in your head

**Prometheus** stores *series*: a metric name plus a unique label set, each a list of (timestamp, value) samples.
Every query is "select series → do math per series over time → aggregate across series". Label sets are the unit
of cost: every distinct combination is a series that lives in memory.

**Honeycomb** also stores metrics as time series now, in a native metrics store: a series is a metric name plus
its resource and data-point attributes. Queries do temporal aggregation per series first (LAST / INCREASE / RATE /
SUMMARIZE), then **one** spatial aggregation (SUM/AVG/MAX/P99/HEATMAP...) across the series. There are no joins
between different metrics and no nested aggregations, so an attribute you want to group by must be on the metric
itself. The upside is that any attribute that *is* on the data can be grouped or filtered without pre-planning.

Most differences you'll find in these labs follow from those two sentences.

## Cheat sheet

```bash
docker compose exec simulator metricsim scenarios         # what can break
docker compose exec simulator metricsim trigger KEY [--duration 10m] [--param k=v]
docker compose exec simulator metricsim status | stop KEY | stop all | mystery | reveal
curl -s localhost:9101/metrics | less                     # frontend-1's raw exposition (ports in the UI)
curl -sG localhost:9090/federate --data-urlencode 'match[]={job="cart"}' | head   # what federation serves
curl -X POST localhost:9090/-/reload                      # after editing prometheus/*.yml
docker compose restart otel-collector                     # after editing collector/config.yaml
```
