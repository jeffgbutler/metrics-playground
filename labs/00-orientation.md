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

After every scrape, Prometheus writes a few series of its own about how that scrape went. They're *synthetic*:
the target never exposes them (`curl -s localhost:9110/metrics | grep -E '^(up|scrape_)'` finds nothing), but
Prometheus stores them with the target's labels. So `up{instance="checkout-1"}` sits right next to checkout-1's real
metrics. Run each.

In the docs site, every query gets three buttons: **Grafana** and **Prometheus** open it in that UI, ready to
run, and **copy** puts it on the clipboard so you can paste it into a query box you already have open. Queries in
the rest of the labs work the same way.

```promql
up
scrape_duration_seconds
scrape_samples_scraped
scrape_series_added
```

`up` is 1 if the scrape worked, 0 if it didn't. No target can report its own `up`: a target that's down can't
answer the scrape.

## 4. The same metric, four ways

Pick `process_resident_memory_bytes` for `checkout-1`.

**Prometheus:** `process_resident_memory_bytes{instance="checkout-1"}`

**Honeycomb:** every path lands in the same metrics dataset. So the GROUP BY list always offers every attribute
that *any* path has ever sent, and filtering or grouping on one that a path doesn't send still works: those rows
just show `(No Value)`. The attribute list tells you nothing about a metric. The `(No Value)` cells do. Grouping by
`collection.method` alongside other attributes is how you see what each path actually sent.

1. Run `AVG(process_resident_memory_bytes)` WHERE `service.instance.id = checkout-1`, GROUP BY `collection.method`.
    Which methods send this metric under this exact name?
2. Add `service.name`, `node`, `team`, `cluster`, `server.address`, `server.port` and `url.scheme` to the GROUP BY.
    * Which Prometheus labels became `service.name` and `service.instance.id`? Look for `job` or `instance` in the
      GROUP BY list: neither exists in Honeycomb.
    * `node` and `team` stayed as plain attributes, with the same values on both paths.
    * `server.address`, `server.port` and `url.scheme` are derived by the receiver from `instance`. Ours isn't
      `host:port`, so what's in `server.port`?
    * `cluster` has a value on only one path. Which, and why? (It's an *external label*, which Prometheus adds only
      to data leaving it.)
3. OTLP push has no `process_resident_memory_bytes`. Its name for the same thing is `process.memory.usage`. Add
    `AVG(process.memory.usage)` as a second calculation, and add `host.name`, `service.version` and
    `deployment.environment.name` to the GROUP BY. Each row now has a value for only one of the two calculations.
    * Compare the values across the three rows.
    * Which attributes does only otlp-push have? These are resource attributes the SDK sends, which Prometheus
      data doesn't have.
    * Look at the values, not just the names. Where does `node-a` appear on the OTLP row? Where does `playground`
      appear? The same facts arrive under different keys depending on the path. A GROUP BY written for one path
      shows `(No Value)` on another, and a filter like `node = node-a` silently drops the OTLP data altogether.

## Questions

1. A scrape is a *pull* of the *current* value of every series. What happens to a value that changed and changed
   back between two scrapes?
2. The scrape and federate paths send the *same metric name* for the same instance. What keeps them apart in
   Honeycomb, and what would happen to your queries if `collection.method` weren't there?
3. In the OTLP data, metric names have dots and units live in metadata (`s`, `By`). In Prometheus the unit is in
   the name (`_seconds`, `_bytes`). What does each convention make easy or hard?
