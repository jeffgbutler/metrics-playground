# Scorecard: Prometheus + Grafana vs Honeycomb (metrics)

Fill this in from **what you observed**, not from docs. The "expected" column is a starting hypothesis to confirm or
correct. Where it says *test it*, nobody has checked yet.

| # | Capability | Expected | Prometheus + Grafana (observed) | Honeycomb (observed) | Lab |
|---|---|---|---|---|---|
| 1 | Per-second rate of a counter | Both. PromQL `rate(x[1m])`; Honeycomb `RATE(x, 60)` | | | 01 |
| 2 | Survives counter resets | Both, via drop detection. Honeycomb also uses StartTimestamp when it's set (OTLP) | | | 01, 05 |
| 3 | Result independent of zoom level | PromQL with a fixed `[range]`; Honeycomb with an explicit range interval, not the default | | | 01 |
| 4 | Catching spikes shorter than the interval | Neither from a gauge; both from a histogram/counter | | | 02 |
| 5 | Percentiles from classic histograms | Both, and only as good as the buckets | | | 03 |
| 6 | Percentiles with no bucket design | Honeycomb with OTLP exponential histograms; Prometheus native histograms (not enabled here) | | | 03 |
| 7 | Aggregating summaries' quantiles | Wrong in both | | | 03 |
| 8 | Join a label from an info metric (`group_left`) | PromQL yes; Honeycomb no join, the attribute must already be on the data | | | 04 |
| 9 | Group by a 1000-value attribute | Prometheus: expensive, can trip `sample_limit`; Honeycomb: fine to group by, still a series per value | | | 04 |
| 10 | Count series of a metric | PromQL `count(...)`; Honeycomb only `COUNT_DISTINCT` of one attribute | | | 04 |
| 11 | Regex label matching | PromQL `=~`; Honeycomb starts-with / contains / in only | | | any |
| 12 | Multi-stage aggregation (`max by` then `sum`) | PromQL yes; Honeycomb one spatial aggregation only | | | any |
| 13 | Ratio of two metrics | PromQL binary op; Honeycomb query math `$A / $B` (same GROUP BY) | | | 01, 07 |
| 14 | "Target is down" | Prometheus `up`; Honeycomb scrape path gets `up`, OTLP path has nothing | | | 06 |
| 15 | Alert on missing data | PromQL `absent()`; Honeycomb *test it* (trigger behaviour on empty results) | | | 06 |
| 16 | NaN / +Inf handling | Prometheus stores and propagates NaN; Honeycomb *test it* | | | 06 |
| 17 | Skewed / explicit timestamps | Prometheus rejects out-of-order; Honeycomb *test it* | | | 06 |
| 18 | Forecast (`predict_linear`, `deriv`) | PromQL yes; Honeycomb no | | | 07 |
| 19 | Compare with an hour ago (`offset 1h`) | PromQL yes; Honeycomb *test it* (time comparison in the query builder) | | | 07 |
| 20 | Pre-aggregation (recording rules) | Prometheus yes; Honeycomb doesn't need them, and gets them only via federation | | | 05 |
| 21 | Alert `for:` duration / hysteresis | Prometheus `for:`; Honeycomb triggers: *test it* | | | 06, 07 |
| 22 | Ad-hoc "what's different about the slow ones?" | Grafana: you must guess the label; Honeycomb: *test BubbleUp on metrics* | | | 07, 08 |
| 23 | Keeping types through federation | Lost in both unless repaired in the collector | | | 05 |
| 24 | What drives cost | Prometheus: active series (memory); Honeycomb: *check how metrics are metered on your plan* | | | 04 |

## Drill log

| Date | Scenario (after reveal) | First signal | Time to detect | Time to cause | Faster tool, and why |
|---|---|---|---|---|---|
| | | | | | |

## Things I'd tell a customer

*
