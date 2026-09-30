# Lab 00: Orientation

**Goal:** understand what a scrape actually is and follow one metric from a process to Grafana and to all three
Honeycomb collection methods.

## 1. Read an exposition

```bash
curl -s localhost:9110/metrics      # checkout-1 (the control UI lists every port)
curl -s localhost:9112/metrics      # payments-1 (only for the last row of the table)
```

Every row below is in the checkout-1 output except the last, which is only on payments-1.

`# HELP` and `# TYPE` describe a metric *family*, not a line. A counter or gauge family is one line per label set.
A histogram family `foo` is written as `foo_bucket`, `foo_sum` and `foo_count` lines. A summary family `foo` is
`foo_sum`, `foo_count` and optionally `foo{quantile="..."}` lines. So the name in `# TYPE` often isn't on any
sample line, and `foo_count` has no HELP of its own.

Find each of these and say what type it is and what one sample means:

| line starts with | question |
|---|---|
| `http_requests_total{...}` | Is this "requests in the last 15 s" or "since the process started"? |
| `http_request_duration_seconds_bucket{...,le="0.25"}` | What does the number count? Why is `le="+Inf"` equal to `_count`? |
| `jvm_gc_pause_seconds_count` / `jvm_gc_pause_seconds_max` | `# TYPE` says `jvm_gc_pause_seconds` is a *summary*, yet it has no quantile lines (Micrometer's default), only `_sum`/`_count`. `_max` is a separate *gauge* family Micrometer adds alongside it. Does `_count` behave like a counter or a gauge? What window does `_max` cover (read its HELP)? |
| `jvm_memory_max_bytes{id="G1 Eden Space"}` | Why `-1`? What happens if you divide by it? |
| `app_build_info{...} 1` | Why would anyone expose a metric whose value is always 1? (Lab 04) |
| `payment_provider_latency_seconds{quantile="0.99"}` (**payments-1, `:9112`**) | This is a *summary*, like `jvm_gc_pause_seconds`, but with quantile lines. Where was the quantile computed? |

Now compare runtimes: `:9101` (frontend, Node), `:9104` (api-gateway, Go), `:9108` (cart, Python). Same concepts,
different names: `nodejs_heap_size_used_bytes` vs `go_memstats_heap_alloc_bytes`. Notice
`nodejs_active_handles_total` is declared `# TYPE ... gauge` even though it ends in `_total`. Names are
conventions, not contracts.

Finally, `:9211` (postgres-primary): `pg_stat_database_xact_commit` is a counter **without** `_total`. Remember this
for lab 05.

## 2. How Prometheus finds targets

Open the [Prometheus targets page, filtered to checkout-1](http://localhost:9090/targets?pool=sim-apps&search=checkout-1){: data-link="prometheus" data-path="/targets?pool=sim-apps&search=checkout-1" }
(the same as picking `sim-apps` in the scrape-pool selector and typing `checkout-1` in the filter box).

The **Endpoint** links on this page don't open, and that's expected. Each link is the URL Prometheus scrapes from
inside the Docker network, built from the discovered `__scheme__`, `__address__` and `__metrics_path__` labels, and
`simulator` only resolves there. To
see the same page yourself, swap in `localhost`: `http://simulator:9110/metrics` is `localhost:9110/metrics`, the
page you read in section 1.

Then:

```bash
curl -s localhost:8080/sd/apps | python3 -c 'import json, sys; print(json.dumps([t for t in json.load(sys.stdin) if t["labels"]["__meta_instance"] == "checkout-1"], indent=2))'
```

The simulator's HTTP service discovery returns one entry per instance, 18 in all
(`curl -s localhost:8080/sd/apps | python3 -m json.tool` shows every one). The one above is `simulator:9110`
(checkout-1) plus labels like `__meta_service`. In
[prometheus/prometheus.yml](../prometheus/prometheus.yml), `relabel_configs` copy those into `job`, `instance`,
`node`, `team`, `runtime`. Anything still starting with `__` afterwards is dropped.

* On the targets page, the chips under **Labels** are the *target labels*, after relabeling. Click the small
  chevron to the right of the chips to expand **Discovered labels**: what service discovery returned, before
  relabeling. Which labels did relabeling create? Discovered `job` is `sim-apps` (the scrape pool); what did it
  become, and which rule did that?
* Every series from `checkout-1` now carries `node="node-a"`. Why is that useful when a host misbehaves (lab 07)?

## 3. Prometheus writes metrics about scraping

For every scrape Prometheus records synthetic series. Run each:

```promql
up
scrape_duration_seconds
scrape_samples_scraped
scrape_series_added
```

`up` is not exposed by any target. Prometheus generates it: 1 if the scrape worked, 0 if it didn't.

## 4. The same metric, four ways

Pick `process_resident_memory_bytes` for `checkout-1`.

**Prometheus:** `process_resident_memory_bytes{instance="checkout-1"}`

**Honeycomb, `collection.method = prometheus-scrape`:** run `AVG(process_resident_memory_bytes)` WHERE
`service.instance.id = checkout-1` AND `collection.method = prometheus-scrape`. Then look at which attributes you can
GROUP BY for this metric. Answer:
* Which Prometheus labels became `service.name` and `service.instance.id`? Which stayed as plain attributes (`node`, `team`)?
* What are `server.address`, `server.port`, `url.scheme`? (The receiver derives them from `instance`; ours isn't `host:port`, so the port is empty.)
* Remove the `collection.method` filter and GROUP BY it instead. Which methods send this metric under this exact name?

**Honeycomb, `collection.method = prometheus-federate`:** the same query. What extra attribute appears? (`cluster`: it's an
*external label*, added only to data leaving Prometheus.)

**Honeycomb, `collection.method = otlp-push`:** there is no `process_resident_memory_bytes`. Find `process.memory.usage` instead,
WHERE `service.instance.id = checkout-1`. Compare the value. Look at `service.version`, `host.name`,
`deployment.environment.name`: resource attributes the SDK sends that Prometheus data doesn't have.

## Questions

1. A scrape is a *pull* of the *current* value of every series. What happens to a value that changed and changed
   back between two scrapes?
2. The scrape and federate paths send the *same metric name* for the same instance. What keeps them apart in
   Honeycomb, and what would happen to your queries if `collection.method` weren't there?
3. In the OTLP data, metric names have dots and units live in metadata (`s`, `By`). In Prometheus the unit is in
   the name (`_seconds`, `_bytes`). What does each convention make easy or hard?
