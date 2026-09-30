# Metrics playground

A laptop-sized, breakable system for learning metrics hands-on: a simulated microservices shop that exposes
Prometheus metrics, a Prometheus to scrape it, Grafana to build dashboards in, and an OpenTelemetry Collector that
sends the same data to Honeycomb three different ways so you can compare the tools on identical data.

It has 20 failure scenarios. About half break the *system* (latency, errors, hosts dying, disks filling). The other
half break the *metrics* (counter resets, scrape timeouts, cardinality explosions, histograms that lie, NaNs, clock
skew). Those are where tools disagree with each other, and where most of the learning is.

Start with the lab workbook at http://localhost:8081 once the stack is up (or read [labs/README.md](labs/README.md)).

## What's in the box

```
                         ┌──────────────── simulator (metricsim) ─────────────────┐
                         │  Byte Mart: 9 services, 18 instances, 4 hosts          │
                         │  + postgres / redis / kafka exporters                  │
                         │  one port per process:  :9101-9118 apps                │
                         │                         :9201-9214 infra               │
                         │  :8080 control UI + HTTP service discovery             │
                         └───┬───────────────────────┬────────────────────┬───────┘
                   scrape /metrics             scrape /metrics      OTLP push (SDK)
                         │                           │                    │
                  ┌──────▼──────┐   /federate  ┌─────▼────────────────────▼────────────────────────────────┐
  node-exporter ─►│ Prometheus  │─────────────►│                   OTel Collector                          │
  cAdvisor ──────►│   :9090     │              │ metrics/scrape      collection.method=prometheus-scrape   |
                  └──────┬──────┘              │ metrics/federate    collection.method=prometheus-federate |
                         │                     │ metrics/otlp        collection.method=otlp-push           |
                  ┌──────▼──────┐              │ metrics/hostmetrics collection.method=hostmetrics         |
                  │  Grafana    │              └────────────────────────┬──────────────────────────────────┘
                  │   :3000     │                                       ▼
                  └─────────────┘                          Honeycomb (one metrics store)
```

| Component | URL | Notes |
|---|---|---|
| Lab workbook | http://localhost:8081 | the labs rendered, with Grafana / Prometheus buttons on every PromQL query |
| Simulator control UI | http://localhost:8080 | start/stop scenarios, mystery mode, target list with links to raw `/metrics` |
| Grafana | http://localhost:3000 | no login; opens on the starter dashboard (also in the "Playground" folder) |
| Prometheus | http://localhost:9090 | `/targets`, `/alerts`, `/tsdb-status` are worth bookmarking |
| Collector zPages | http://localhost:55679/debug/pipelinez | |
| Raw exposition | http://localhost:9101/metrics ... | every simulated process; read these, they're short |

**Byte Mart** is a small online store: `frontend` (Node-flavoured) → `api-gateway` (Go) → `catalog` (Go, Redis
cache, Postgres), `cart` (Python, Redis), `checkout` (JVM) → `inventory` (Go), `payments` (JVM, external card
providers), `shipping` (Python, external carrier API), and a Kafka topic consumed by `notifications` (Python).
Each "runtime" exposes the metrics its real client library would, including the quirks: Go's GC *summary*, Micrometer's
`-1` for "no max", prom-client's gauge named `..._total`, postgres_exporter counters with no `_total`.

Traffic follows a compressed day (default 60 minutes, `SIM_DAY_LENGTH`), so `offset 1h` compares like with like.

## Quick start

```bash
cp .env.example .env        # add HONEYCOMB_API_KEY (an ingest key; a dedicated environment is best)
docker compose up -d --build
```

Then open http://localhost:8081 (labs), http://localhost:8080 (control UI) and http://localhost:3000 (Grafana). Give it ~15 minutes before rate windows and
recording rules look sensible. Stop with `docker compose down` (keeps Prometheus/Grafana data) or
`docker compose down -v` (wipes it).

Without a Honeycomb key everything local still works; the collector just logs export errors. To run
fully offline, set the three `PIPELINE_*_EXPORTERS` to `[debug]` or `[nop]` in `.env`.

### Running it on an always-on VM

See [VM.md](VM.md): publish the two home-grown images to Harbor with `scripts/publish-images.sh`, `git clone` the repo
on the VM, set `PLAYGROUND_REGISTRY` / `PLAYGROUND_TAG` and `PLAYGROUND_BIND=0.0.0.0` in `.env`, and
`docker compose up -d --no-build`. (Ports listen on `127.0.0.1` unless `PLAYGROUND_BIND` says otherwise; nothing in
the stack has authentication, so keep it on a private network.)

### Driving scenarios

The UI is easiest. From a terminal:

```bash
docker compose exec simulator metricsim scenarios                       # catalog + parameters
docker compose exec simulator metricsim trigger memory_leak --duration 20m --param mb_per_min=120
docker compose exec simulator metricsim status
docker compose exec simulator metricsim stop all
docker compose exec simulator metricsim mystery                         # a random incident; find it, then:
docker compose exec simulator metricsim reveal
```

Or with HTTP: `curl -X POST localhost:8080/api/scenarios/node_down/start -d '{"duration":"5m","params":{"node":"node-b"}}'`.

Scenarios ramp up, hold, and ramp down; several can run at once. Ground truth is always available as the
`sim_scenario_active` metric (job `simulator`) and as annotations on the Grafana dashboard. Try not to look at it
during mystery mode. With `HONEYCOMB_CONFIG_KEY` set, starts, ends, deploys and rollbacks also become
Honeycomb markers.

## Scenario catalog

**System failures**

| key | what happens |
|---|---|
| `traffic_surge` | 3x traffic; saturation spreads (CPU, pools, in-flight, latency) |
| `payment_provider_slow` | one card provider slows and errors; latency is inherited up the call chain |
| `error_burst` | a service/route returns 500s; callers turn them into 502s |
| `bad_deploy` | rolling deploy of a bad version (restarts, `app_build_info` changes), then a rollback |
| `memory_leak` | JVM heap leak → longer GC → OOM kill → restart, repeat |
| `noisy_neighbor` | CPU burned and stolen on one host; everything on it slows |
| `disk_fill` | root disk fills; on node-d postgres dies (`pg_up=0` while the exporter's `up=1`) |
| `node_down` | a host vanishes: `up=0`, traffic shifts, counters reset on return |
| `db_pool_exhaustion` | a connection leak climbs linearly until requests time out waiting for a connection |
| `db_slow_queries` | lock contention, slow queries, replica lag |
| `cache_eviction` | Redis evicts, hit ratio collapses, reads fall through to postgres |
| `queue_backlog` | consumers slow; Kafka lag and notification delay grow while nothing errors |

**Metrics pathologies**

| key | what happens |
|---|---|
| `crash_loop` | counter resets every N seconds; `rename=true` also churns the instance identity |
| `scrape_timeout` | `/metrics` stalls past the 10s timeout: the service is fine, the monitoring isn't |
| `cardinality_explosion` | a `customer_id` label appears; `customers=800` trips Prometheus's `sample_limit` |
| `bad_buckets` | buckets too narrow for real latency, or mismatched across instances |
| `microbursts` | 2s bursts every 30s that a 15s gauge scrape mostly misses |
| `clock_skew` | explicit sample timestamps from a skewed clock |
| `nan_values` | ratio gauges go NaN / +Inf and poison aggregations |
| `series_disappear` | a route is retired; its series vanish (and an alert silently resolves) |

## The three paths to Honeycomb

Honeycomb stores all metrics together (the `x-honeycomb-dataset` header is ignored for metrics), so the collector
stamps every data point with a resource attribute, **`collection.method`**. Filter on it to look at one path, or
GROUP BY it to put the paths side by side in one query.

| `collection.method` | how the data gets there | what's special |
|---|---|---|
| `prometheus-scrape` | collector's `prometheus` receiver scrapes the same targets as Prometheus, independently | typed data (counters are cumulative sums, histograms are histograms), no `sample_limit`, start time unknown |
| `prometheus-federate` | collector scrapes Prometheus's `/federate` | **every series arrives `untyped`**: counters become gauges, histograms fall apart into `_bucket` gauges with an `le` attribute; recording rules and the `cluster` external label come along |
| `otlp-push` | simulator pushes with the OTel SDK | semconv names, delta temporality and exponential histograms by default, real start times |
| `hostmetrics` | the collector's `hostmetrics` receiver reads the Docker host | OTel semconv host metrics (`system.*`), to compare with node-exporter's `node_*` |

Because the scrape and federate paths send the **same metric names** with the same service/instance attributes, a
query like `P99(http_request_duration_seconds) GROUP BY collection.method` compares them directly. The catch is
that a name can arrive with **different types**: `http_requests_total` is a counter via scrape and a gauge via
federate (lab 05 explains why). If Honeycomb shows the federate data oddly, enable `transform/federate_prefix` (see
`.env.example`) to rename federated metrics to `federate.*`.

To label an experiment, change `COLLECTION_METHOD_*` in `.env` (for example `prometheus-federate-repaired`) and
restart the collector. Old and new data stay distinguishable.

Toggle a path off with `PIPELINE_<NAME>_EXPORTERS=[nop]` in `.env`, then `docker compose up -d otel-collector`.

**Volume.** Measured at startup: roughly 110 data points/s on the scrape path, 200/s on federate (because of the
exploded buckets), and 10–15/s on OTLP, about 1,600 active series per path. Both jump while `cardinality_explosion`
runs. The dashboard's "Collector: data points / s by path" panel shows the live rate; switch paths you're not
using to `[nop]`.

## Configuration

Everything is in `.env` (see [.env.example](.env.example)): Honeycomb key/endpoint, `collection.method` labels, per-pipeline exporters,
`SIM_BASE_RPS`, `SIM_DAY_LENGTH`, and the OTLP knobs `OTLP_TEMPORALITY` (delta/cumulative) and `OTLP_HISTOGRAM`
(exponential/explicit). The configs are meant to be edited as part of the labs:

- [prometheus/prometheus.yml](prometheus/prometheus.yml): scrape config, relabeling, `sample_limit`. Reload with `curl -X POST localhost:9090/-/reload`.
- [prometheus/rules/](prometheus/rules/): recording rules and alerts (no Alertmanager; watch http://localhost:9090/alerts).
- [collector/config.yaml](collector/config.yaml): the three pipelines. `docker compose restart otel-collector` after edits.
- [grafana/dashboards/](grafana/dashboards/): provisioned dashboards. Save your own work with "Save as" into another folder.

## Troubleshooting

- **cAdvisor shows no per-container series** (only `{id="/"}`): it could not find containerd. Check
  `docker compose logs cadvisor | grep factory`. The default socket path works on OrbStack; on Docker Desktop try
  `CADVISOR_CONTAINERD_SOCK=/run/containerd/containerd.sock` in `.env`.
- **"host" metrics look odd on a Mac**: node-exporter, cAdvisor and `hostmetrics` see the Docker VM, not macOS.
- **Collector errors `401`/`403`**: `HONEYCOMB_API_KEY` is missing or not an ingest key for that environment.
- **Simulator tests**: `cd simulator && uv venv .venv --python 3.12 && uv pip install -e . --python .venv/bin/python && .venv/bin/python tests/test_smoke.py`
