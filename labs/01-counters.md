# Lab 01: Counters and rate()

**Goal:** get a gut feel for cumulative counters, why you almost never graph them raw, and how Prometheus and
Honeycomb each turn them into "per second".

## 1. Raw counters are boring and scary

```promql
http_requests_total{job="cart", route="/cart", status="200"}
```

Two instances, two lines that only go up. The absolute value is "requests since this process started", which
nobody cares about. Now:

```promql
rate(http_requests_total{job="cart", route="/cart", status="200"}[1m])
```

That's requests per second, averaged over the last minute, per series.

Try these side by side (Grafana Explore → "+ Add query"):

```promql
rate(http_requests_total{job="cart", route="/cart", status="200"}[1m])
rate(http_requests_total{job="cart", route="/cart", status="200"}[10m])
irate(http_requests_total{job="cart", route="/cart", status="200"}[1m])
increase(http_requests_total{job="cart", route="/cart", status="200"}[1m])
```

* Why is `[10m]` smoother? What does it hide?
* `irate` uses only the last two samples in the window. When is that what you want?
* `increase(...[1m])` should be a whole number of requests. Why isn't it? (Prometheus **extrapolates** to the edges
  of the window because samples rarely land exactly on them.)
* Replace `[1m]` with `[20s]`. With a 15 s scrape interval, how many samples are in the window? What happens at `[10s]`?

Grafana's `$__rate_interval` exists to pick a window that is always at least 4× the scrape interval. Look at any
panel in the starter dashboard to see it used.

## 2. Aggregate after rate, never before

```promql
sum by (job) (rate(http_requests_total[5m]))     # right
rate(sum by (job) (http_requests_total)[5m:])    # wrong: a subquery over a summed counter
```

Plot both. They look similar until a counter resets. Start the crash loop:

```bash
docker compose exec simulator metricsim trigger crash_loop --duration 10m --param every=60
```

`cart-2` now restarts every 60 s and each restart zeroes all its counters.

```promql
http_requests_total{instance="cart-2", route="/cart", status="200"}       # sawtooth
resets(http_requests_total{instance="cart-2", route="/cart", status="200"}[10m])
rate(http_requests_total{instance="cart-2"}[2m])                          # still sane
sum by (job) (rate(http_requests_total{job="cart"}[5m]))                  # still sane
rate(sum by (job) (http_requests_total{job="cart"})[5m:])                 # watch this one at each restart
```

**Why?** `rate()` knows a counter can only go up, so when the value drops it assumes a reset and counts from zero.
If you sum first, the reset of one instance is hidden inside a bigger number that just dips, and the maths goes
wrong. Rule: **`rate` first, then `sum`.**

While it runs, also look at `changes(process_start_time_seconds{instance="cart-2"}[10m])`. That's the other way
to spot restarts.

## 3. The same thing in Honeycomb

Filter to **`collection.method = prometheus-scrape`**. Find `http_requests_total` and look at its `MetricInfo`. You
should see a monotonic cumulative sum.

> **Honeycomb:** SELECT `SUM(http_requests_total)` WHERE `service.name = cart` AND `collection.method = prometheus-scrape`,
> GROUP BY `service.instance.id`.

No temporal function was given, so Honeycomb applied the default for a cumulative counter: **INCREASE** per
granularity step. The number is "requests in this step", not per second, and it changes when you change the
granularity. Try 15 s, 60 s and 5 min granularity and watch the y-axis.

> **Honeycomb:** SELECT `SUM(RATE(http_requests_total, 60))`, same filter and grouping.

Now it's per second and independent of granularity. Compare with Grafana's `sum by (instance) (rate(...[1m]))`.

* During the crash loop, does Honeycomb's RATE survive `cart-2`'s resets? (It should: INCREASE/RATE treat a drop
  as a reset.)
* Set the range interval to `15` instead of `60`. What happens, and why? (Hint: how many points are in 15 s?)

Now **`collection.method = otlp-push`**: `http.server.request.duration` is a histogram, but it has a count. The simulator
pushes **delta** temporality by default. Check `MetricInfo` on `shop.orders.placed`: `sum(delta, monotonic)`. The
default temporal function is **SUMMARIZE**: the process already sent "how many since last export", so Honeycomb just
adds them up. There's nothing to reset.

> **Honeycomb:** SELECT `SUM(shop.orders.placed)` GROUP BY `service.instance.id` (arrives only via `otlp-push`) vs
> SELECT `SUM(orders_placed_total)` WHERE `collection.method = prometheus-scrape` GROUP BY `service.instance.id`.
> Same orders, different plumbing. Put both in one query with query math and check they agree.

## Questions

1. Why do Prometheus client libraries expose cumulative counters instead of "count in the last interval"? Think
   about what happens when a scrape is missed.
2. A delta counter has the opposite trade-off. What is lost if one OTLP export is dropped?
3. Your Prometheus-scraped counters arrive in Honeycomb with `StartTimestamp = 0` (the receiver doesn't know when the
   process started). What is Honeycomb left with to detect a reset? Is there a reset it could miss? (Think: a restart
   and enough traffic between two scrapes to climb past the old value.)
4. `revenue_dollars_total` is a *float* counter. Does anything about rate/increase change?

```bash
docker compose exec simulator metricsim stop all
```
