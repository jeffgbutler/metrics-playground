# Lab 08: Incident drills

**Goal:** put it together without knowing the answer in advance.

## Mystery mode

```bash
docker compose exec simulator metricsim mystery
```

(or the button in the control UI). A random scenario starts with random timing. Its ground truth metric is
labelled `scenario="mystery"`, so the Grafana annotation tells you *that* something is happening but not *what*.
To make it harder, turn off the "Scenarios (ground truth)" annotation toggle on the dashboard.

Protocol:

1. **Detect.** Which panel or alert told you first? How long after the start?
2. **Scope.** One instance, one service, one host, or everything? Use `by (instance)`, `by (node)`, `by (job)`.
3. **Hypothesise and test.** Write down a guess, then find the query that would *disprove* it.
4. **Do it again in Honeycomb.** Same investigation, other tool. Which one got you there faster, and why?
5. **Reveal:** `docker compose exec simulator metricsim reveal`.

Keep a log in the [scorecard](scorecard.md). After five or six drills, patterns show up in which tool you reach
for first.

## Harder drills

Combine two scenarios so the symptoms overlap:

```bash
docker compose exec simulator metricsim trigger noisy_neighbor --param node=node-a --duration 15m
docker compose exec simulator metricsim trigger payment_provider_slow --duration 15m
```

checkout-1 runs on node-a and also depends on payments. Which part of its latency is which?

```bash
docker compose exec simulator metricsim trigger scrape_timeout --param instance=payments-1 --duration 15m
docker compose exec simulator metricsim trigger memory_leak --param instance=payments-1 --duration 15m
```

The leaking instance is also the one you can't scrape. What *can* you see? (Callers' latency. The `otlp-push` data.)

```bash
docker compose exec simulator metricsim trigger crash_loop --param instance=inventory-1 --param rename=true --duration 15m
docker compose exec simulator metricsim trigger error_burst --param service=inventory --duration 15m
```

Series churn and errors at the same time. Does `rate()` by instance still give a sensible picture? What about
Honeycomb GROUP BY `service.instance.id`?

## Build your own scenario

The simulator is meant to be edited. Scenarios live in
[simulator/metricsim/scenarios.py](../simulator/metricsim/scenarios.py); each one is ~15 lines that set fields on
`Effects`. Ideas:

* **Retry storm**: a dependency fails, callers retry 3×, and traffic to the dependency quadruples.
* **Slow leak in a Go service**: `go_goroutines` climbing (goroutine leak) instead of heap.
* **A bad `le`**: add a `le="0.3"` bucket to one instance only. How small can a bucket mismatch be and still hurt?
* **Summary window surprise**: the payments summary uses a 2-minute window. What does the p99 do right after a restart?

Run the tests after changes: `cd simulator && .venv/bin/python tests/test_smoke.py`, then
`docker compose up -d --build simulator`.
