# Lab 07: Infrastructure and correlation

**Goal:** practise going from an application symptom to an infrastructure cause, and see where each tool helps.
Run these one at a time. For each one, **start from the symptom** (RED on the starter dashboard) and work backwards.
Don't go straight to the "cause" query.

Two useful frameworks:
* **RED** for services: **R**ate, **E**rrors, **D**uration.
* **USE** for resources: **U**tilization, **S**aturation, **E**rrors. CPU, memory, disk, connection pools, queues.

Every app series carries a `node` label (from service discovery), and so does every host series. That shared
label is what lets you correlate.

## 7.1 Noisy neighbour

```bash
docker compose exec simulator metricsim trigger noisy_neighbor --duration 10m --param node=node-b
```

Symptom: latency rises on *several unrelated services* at once. That pattern is the clue.

```promql
histogram_quantile(0.99, sum by (node, le) (rate(http_request_duration_seconds_bucket[2m])))   # latency by host
sum by (node, mode) (rate(node_cpu_seconds_total{mode!="idle"}[1m]))                         # what's using CPU?
node_load1 / on (instance) count by (instance) (node_cpu_seconds_total{mode="idle"})           # load per core
rate(node_cpu_seconds_total{mode="steal", instance="node-b"}[1m])
```

`steal` is time the hypervisor gave to someone else. You can't fix it from inside the VM.

> **Honeycomb (`collection.method = prometheus-scrape`):** `P99(http_request_duration_seconds)` GROUP BY `node`. Then
> `SUM(RATE(node_cpu_seconds_total, 60))` WHERE `mode = steal` GROUP BY `service.instance.id`.

## 7.2 Disk filling up

```bash
docker compose exec simulator metricsim trigger disk_fill --duration 30m
```

node-d's root disk fills at ~5 GB/min. It hosts `postgres-primary`.

```promql
node:filesystem_used:ratio
predict_linear(node_filesystem_avail_bytes{mountpoint="/"}[10m], 3600)   # bytes free in 1h, if the trend holds
(node_filesystem_avail_bytes{mountpoint="/"}) / -deriv(node_filesystem_avail_bytes{mountpoint="/"}[10m]) / 60   # minutes left
```

Watch `DiskWillFillWithin1h` on the [alerts page](http://localhost:9090/alerts){: data-link="prometheus" data-path="/alerts" }.
When the disk is full, `pg_up` goes to 0 while `up{job="postgres"}` stays 1: the exporter is fine, the database it
watches is not. Then errors spread to every service that writes (checkout, payments, inventory).

Also compare `node_filesystem_avail_bytes` with `node_filesystem_free_bytes`. The difference is the 5% ext4
keeps for root. Which one should your alert use?

> **Honeycomb:** there's no `predict_linear`. What can you do instead? Try a query on
> `node_filesystem_avail_bytes` GROUP BY `node` and think about a trigger threshold, or query math comparing now
> with an earlier value. Is "fills in an hour" expressible? Is it necessary?

## 7.3 Memory leak

```bash
docker compose exec simulator metricsim trigger memory_leak --duration 30m
```

```promql
sum by (instance) (jvm_memory_used_bytes{area="heap"})
jvm_memory_used_bytes{area="heap", id="G1 Old Gen"} / jvm_memory_max_bytes{area="heap", id="G1 Old Gen"}
rate(jvm_gc_pause_seconds_sum{instance="payments-1"}[1m])      # fraction of time spent in GC
jvm_gc_pause_seconds_max{instance="payments-1"}
changes(process_start_time_seconds{instance="payments-1"}[30m])
```

Try `sum by (instance) (jvm_memory_used_bytes{area="heap"}) / sum by (instance) (jvm_memory_max_bytes{area="heap"})`.
Why is it wrong? (Eden's max is `-1`.)

Watch the sawtooth: heap climbs, GC time climbs, latency and CPU climb, OOM kill, restart, repeat. The restart
also resets all of payments-1's counters. Lab 01 applies.

## 7.4 Connection pool exhaustion

```bash
docker compose exec simulator metricsim trigger db_pool_exhaustion --duration 15m
```

```promql
db_pool_connections{job="inventory"}
db_pool_connections{state="active"} / on (instance) db_pool_connections_max
rate(db_pool_timeouts_total[1m])
histogram_quantile(0.99, sum by (le) (rate(db_pool_acquire_duration_seconds_bucket{job="inventory"}[1m])))
pg_stat_activity_count
```

The leak is linear, so you get a straight ramp to 100%. After that, requests wait for a connection until they time
out at 30 s. Postgres itself is fine. The saturation is in the app.

> **Honeycomb (`collection.method = otlp-push`):** `db.client.connection.count` GROUP BY `db.client.connection.state`,
> `service.instance.id`. This is an OTel UpDownCounter. What `MetricInfo` does it have, and what default temporal
> function does that imply?

## 7.5 Queue backlog (nothing errors)

```bash
docker compose exec simulator metricsim trigger queue_backlog --duration 15m
```

```promql
sum(kafka_consumergroup_lag)
sum(kafka_topic_partition_current_offset) - sum(kafka_consumergroup_current_offset)   # same thing, derived
deriv(sum(kafka_consumergroup_lag)[5m:])
histogram_quantile(0.9, sum by (le) (rate(notifications_delivery_delay_seconds_bucket[5m])))
```

Every HTTP request succeeds. Customers are getting their emails ten minutes late. Which of your RED dashboards
would have caught this? Which metric is the one that matters to the customer?

## 7.6 Cache eviction

```bash
docker compose exec simulator metricsim trigger cache_eviction --duration 10m
```

```promql
rate(redis_evicted_keys_total[1m])
redis_memory_used_bytes / redis_memory_max_bytes
sum(rate(redis_keyspace_hits_total[5m])) / (sum(rate(redis_keyspace_hits_total[5m])) + sum(rate(redis_keyspace_misses_total[5m])))
rate(pg_stat_database_blks_read[1m])
```

A cache problem shows up as database load. Compare `catalog_cache_hit_ratio` (the app's gauge) with the
counter-derived ratio.

## Build it

After running a few of these, build **one** dashboard in each tool that would have got you to the cause fastest.
Save the Grafana one with "Save as" into a new folder; save the Honeycomb one as a Board. Then write down:

* what you could express in Grafana but not Honeycomb (think `predict_linear`, `on() group_left`, `deriv`)
* what you could express in Honeycomb but not Grafana (think grouping by anything, without planning)
* what neither could tell you, and where that information would have to come from (traces? logs? deploy events?)

```bash
docker compose exec simulator metricsim stop all
```
