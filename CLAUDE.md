# CLAUDE.md

A personal learning playground for metrics (not customer facing). A Honeycomb SA uses it to build intuition for
Prometheus/PromQL, Grafana, the OTel Collector's Prometheus receiver, and how Honeycomb's metrics model differs.
Weird corner cases are a feature. Keep it runnable with one `docker compose up -d --build`.

## Layout

- `simulator/` - `metricsim`, a Python (asyncio/aiohttp) simulator of "Byte Mart", 9 services / 18 instances on 4
  simulated hosts plus simulated postgres/redis/kafka exporters. Each simulated process has its own listening port
  (9101+ apps, 9201+ infra) serving Prometheus text format; a closed socket means "process down".
  Control plane on :8080 (UI, JSON API, HTTP SD at `/sd/apps` and `/sd/infra`, ground-truth `sim_*` metrics).
  Optional OTLP push of the same app metrics with the OTel SDK (one MeterProvider per instance).
- `prometheus/` - scrape config (HTTP SD + relabeling), recording rules, alerts.
- `collector/config.yaml` - pipelines `scrape` (prometheus receiver on the same targets), `federate` (prometheus
  receiver on Prometheus `/federate`), `otlp` (simulator push) and `hostmetrics`, all to one Honeycomb exporter.
  Honeycomb keeps all metrics in one store (the dataset header is ignored for metrics), so each pipeline stamps the
  resource attribute `collection.method` (prometheus-scrape / prometheus-federate / otlp-push / hostmetrics; values
  overridable via `COLLECTION_METHOD_*` to label experiments). Labs compare paths with `GROUP BY collection.method`.
  Pipelines are toggled with `PIPELINE_*_EXPORTERS` env vars (YAML lists expanded by the collector).
- `grafana/` - provisioned datasource + starter dashboard (generated JSON; edit freely).
- `labs/` - the guided workbook. Every claim in a lab should be something that was observed in this stack.

## Simulator architecture (read in this order)

- `world.py` - static shape: services, routes (median latency, downstream calls, infra used), replicas, hosts, ports.
- `prom.py` - tiny metrics library + text exposition writer (own implementation so it can emit explicit timestamps,
  NaN/Inf, swap bucket layouts, drop series).
- `targets.py` - `AppInstance` (runtime flavour go/jvm/python/node decides runtime metric names), `NodeTarget`,
  `PostgresTarget`, `RedisTarget`, `KafkaTarget`. Each owns a `Registry`; `boot()` = process start (counters zero).
- `effects.py` - the contract: a fresh `Effects` per tick, mutated by scenarios, read by the engine.
- `scenarios.py` - catalog (`@register`), trapezoid intensity, `ScenarioManager`. Scenarios never touch metrics.
- `engine.py` - 1s tick: lifecycle (host down/up, restarts, OOM), Poisson traffic walking the call graph, queue
  consumers, runtime/host/infra updates, ground truth.
- `otlp.py` - OTel SDK push. `server.py` - sockets, control API, tick loop. `cli.py` - `metricsim` CLI.

## Conventions

- Scenarios express intent through `Effects`; stateful consequences (heap, disk, leaked connections) accumulate on
  engine objects. The engine must not know scenario names.
- A restart is `Engine.restart()` -> `AppInstance.boot()`: new registry, counters from zero, new OTLP provider.
- Real exporter names on purpose (node_exporter, postgres_exporter counters without `_total`, prom-client's
  `nodejs_active_handles_total` gauge, Micrometer's `-1` max). Don't "fix" them.
- Collector config: `$` must be `$$` inside Prometheus relabel configs.

## Test / run

```bash
cd simulator && uv venv .venv --python 3.12 && uv pip install -e . --python .venv/bin/python
.venv/bin/python tests/test_smoke.py        # headless: every scenario, exposition format check
docker compose up -d --build                # from the repo root
docker compose exec simulator metricsim scenarios
```
