# Lab 05: Three roads to Honeycomb

**Goal:** understand what each way of getting Prometheus-world metrics into Honeycomb keeps and what it throws away.
This is the lab most relevant to customer conversations.

Everything lands in Honeycomb's one metrics store. The resource attribute `collection.method` says which road a
data point took:

| `collection.method` | path |
|---|---|
| `prometheus-scrape` | the collector's `prometheus` receiver scrapes the targets itself, alongside Prometheus |
| `prometheus-federate` | the collector scrapes **Prometheus's** `/federate` endpoint |
| `otlp-push` | the simulator pushes OTLP with the OTel SDK |
| `hostmetrics` | the collector's `hostmetrics` receiver reads the Docker host (compare with node-exporter's `node_*`) |

The two Prometheus paths send **the same metric names with the same `service.name` / `service.instance.id`**, so
the quickest comparison is always the same query with `GROUP BY collection.method`.

## 1. What federation actually serves

```bash
curl -sG localhost:9090/federate --data-urlencode 'match[]=http_requests_total{job="cart"}' | head -3
curl -sG localhost:9090/federate --data-urlencode 'match[]=http_request_duration_seconds_bucket{job="cart",route="/cart"}' | head -4
```

Three things to notice:

1. `# TYPE http_requests_total untyped`. **Every** federated series is `untyped`, whatever it was when it was scraped.
2. Every sample carries an explicit **timestamp**: the time Prometheus scraped it, not the time you federated it.
3. `cluster="playground"`: the external label from `prometheus.yml`, added on the way out.

Histograms are served as unrelated `_bucket`, `_sum` and `_count` series.

## 2. What the collector does with that

We watched the collector's debug output while building this. On the federate path, counters become **gauges**
and histograms become loose `_bucket` gauges with an `le` attribute. Confirm it in Honeycomb:

| metric | `prometheus-scrape` | `prometheus-federate` |
|---|---|---|
| `http_requests_total` | `sum(cumulative, monotonic)` | ? |
| `http_request_duration_seconds` | `histogram(cumulative)` | does it exist? |
| `http_request_duration_seconds_bucket` | does it exist? | ? (and what's the `le` attribute doing?) |
| `up` | ? | ? |

**One name, two types.** `http_requests_total` now reaches the same metrics store as a counter (scrape) *and* as a
gauge (federate). Look at what Honeycomb reports as the metric's type, and whether each `collection.method` still
behaves the way its type says it should. If the two collide badly (the federate data looks wrong or goes missing),
that's a real finding for a customer who runs both paths. The escape hatch is in `.env.example`:
`transform/federate_prefix` renames every federated metric to `federate.<name>` so the two never share a name.

> **Honeycomb:** SELECT `SUM(http_requests_total)` WHERE `service.name = cart`, GROUP BY `collection.method`.
> For the scrape series the default temporal function is INCREASE (requests per step). For a gauge it's LAST, the
> raw ever-growing counter value. Which one do you get for each method?
>
> Then SELECT `SUM(RATE(http_requests_total, 60))`, same filter and grouping. Do the two lines agree? Run
> `crash_loop` and check again: does RATE get the resets right on the federate line?
>
> SELECT `P99(http_request_duration_seconds)` WHERE `service.name = cart` GROUP BY `collection.method`. Why is there
> only one line? What would you need to get a p99 out of the federate data?

Look for the **recording rules** too (`job:http_requests:rate5m`, `node:cpu_utilization:ratio_1m`). Only
`prometheus-federate` has them, because only Prometheus computes them. Which `service.name` / `service.instance.id`
did they get? They have no `job`/`instance` of their own, so they inherit the *federate job's* identity:
`prometheus:9090`.

One more federation-only oddity: `docker compose logs otel-collector | grep "inconsistent timestamps"`. cAdvisor
stamps its own sample times, federation passes them through, and the receiver drops points that don't match the
others in their group. We saw it for a container that had just restarted.

### Repairing federation (partly)

The collector can put types back for series that follow naming conventions. Give the repaired data its own label so
you can compare it with what you already have. In `.env`:

```bash
COLLECTION_METHOD_FEDERATE=prometheus-federate-repaired
PIPELINE_FEDERATE_PROCESSORS=[memory_limiter, transform/federate_repair, resource/federate, batch]
```

`docker compose up -d otel-collector`, and look at the processor in [collector/config.yaml](../collector/config.yaml):
one OTTL statement, `convert_gauge_to_sum("cumulative", true) where IsMatch(metric.name, ".*_total$")`.

> **Honeycomb:** SELECT `SUM(RATE(http_requests_total, 60))` WHERE `service.name = cart` GROUP BY `collection.method`
> over a window that includes both the old and the repaired federate data.

* Does `prometheus-federate-repaired` now line up with `prometheus-scrape`?
* What about `pg_stat_database_xact_commit`? (postgres_exporter counters don't end in `_total`, so the repair can't recognise them.)
* Can you rebuild the histograms this way? Why not?

Put both variables back afterwards.

## 3. OTLP: temporality is a choice

`otlp-push` data is **delta** temporality by default (`OTLP_TEMPORALITY=delta` in `.env`). Look at `MetricInfo`:

* `shop.orders.placed`, `process.cpu.time`: `sum(delta, monotonic)`. The SDK sends "how many since the last export".
* `http.server.request.duration`: exponential histogram, delta.
* `http.server.active_requests`, `db.client.connection.count`: **still cumulative**. They're UpDownCounters. Delta
  preference only applies to counters and histograms, because "the change in the number of active requests since
  last time" is rarely what you want.

Switch to cumulative, with its own label:

```bash
# .env
COLLECTION_METHOD_OTLP=otlp-push-cumulative
OTLP_TEMPORALITY=cumulative
```

`docker compose up -d simulator otel-collector`. The same metric names now arrive with a different temporality,
another type change on the same name. Compare `SUM(shop.orders.placed)` GROUP BY `collection.method` across the
switch, and the `MetricInfo`. Then run `crash_loop` against `cart-2` and compare
`http.server.request.duration.count` for that instance. With OTLP cumulative, each restart has a **new
StartTimestamp**, so Honeycomb doesn't have to guess at resets the way it does with the Prometheus-scraped data
(where StartTimestamp is `0`).

Put `.env` back when done.

## 4. The same question, every way

Answer "what is the p99 latency of catalog right now, and what's its error rate?" using:

1. Grafana (PromQL)
2. `collection.method = prometheus-scrape`
3. `collection.method = prometheus-federate`
4. `collection.method = otlp-push`

Where you can, answer it in one Honeycomb query with `GROUP BY collection.method`. Write down how long each took
and how confident you are in each number.

## Questions

1. Why would a customer choose federation anyway? (Hint: they already run 40 Prometheus servers and don't want to
   touch 4,000 scrape configs. And the recording rules already encode their SLIs.)
2. When the collector scrapes the targets directly, you have **two** scrapers on every target. What happens to the
   target's load? What if they disagree about `up` (lab 06)?
3. A customer migrating gradually sends the same metric through two paths for a while. With everything in one
   metrics store, what goes wrong if they *don't* have something like `collection.method` on the data? (Try your
   queries from this lab without the GROUP BY.)
4. The OTLP path has the richest data. What does a customer have to change to get it, and who in their organisation
   owns that change?
5. Which path would you recommend to a customer who says "we just want our Grafana dashboards in Honeycomb"? And to
   one who says "we want to stop running Prometheus"?
