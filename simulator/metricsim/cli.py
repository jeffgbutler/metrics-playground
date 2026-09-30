"""metricsim CLI.

    metricsim run                                   # the simulator (what the container runs)
    metricsim scenarios                             # catalog with params
    metricsim trigger memory_leak --duration 20m --param mb_per_min=120
    metricsim stop memory_leak | all
    metricsim status
    metricsim mystery / metricsim reveal

Everything except `run` talks to the control API ($SIM_URL, default http://localhost:8080), so from the host:
    docker compose exec simulator metricsim trigger node_down
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import textwrap
import urllib.error
import urllib.request

URL = os.environ.get("SIM_URL", "http://localhost:8080")


def _call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(URL + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"error: {json.loads(e.read()).get('error', e)}")
    except urllib.error.URLError as e:
        sys.exit(f"cannot reach simulator at {URL}: {e.reason}")


def cmd_run(args):
    from .config import Config
    from .engine import Engine
    from .server import serve

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    eng = Engine(Config.from_env())
    try:
        asyncio.run(serve(eng))
    except KeyboardInterrupt:
        pass


def cmd_scenarios(args):
    cat = _call("GET", "/api/scenarios")["catalog"]
    for cat_name in ("system", "metrics"):
        print(f"\n== {cat_name} ==")
        for s in (s for s in cat if s["category"] == cat_name):
            print(f"\n{s['key']}  -  {s['title']}  (default {int(s['duration_s'] // 60)}m)")
            print(textwrap.indent(textwrap.fill(s["summary"], 96), "    "))
            for k, p in s["params"].items():
                ch = f" {p['choices']}" if p["choices"] else ""
                print(f"    --param {k}={p['default']}   {p['help']}{ch}")


def cmd_trigger(args):
    params = dict(p.split("=", 1) for p in args.param)
    body = {"params": params}
    if args.duration:
        body["duration"] = args.duration
    if args.ramp:
        body["ramp"] = args.ramp
    print(json.dumps(_call("POST", f"/api/scenarios/{args.key}/start", body), indent=2))


def cmd_stop(args):
    path = "/api/stop_all" if args.key == "all" else f"/api/scenarios/{args.key}/stop"
    print(json.dumps(_call("POST", path, {}), indent=2))


def cmd_status(args):
    st = _call("GET", "/api/status")
    print(f"frontend {st['frontend_rps']} rps (x{st['traffic_multiplier']}), restarts {st['restarts']}, "
          f"otlp push {'on' if st['otlp'] else 'off'}")
    for a in st["active"]:
        print(f"  ACTIVE {a['key']:24s} intensity {a['intensity']:.2f}  {a['remaining_s']}s left  {a['params']}")
    down = [t["name"] for t in st["targets"] if not t["up"]]
    print("  down targets:", ", ".join(down) or "none")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="metricsim")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run").set_defaults(fn=cmd_run)
    sub.add_parser("scenarios").set_defaults(fn=cmd_scenarios)
    t = sub.add_parser("trigger")
    t.add_argument("key")
    t.add_argument("--duration")
    t.add_argument("--ramp")
    t.add_argument("--param", action="append", default=[], metavar="K=V")
    t.set_defaults(fn=cmd_trigger)
    s = sub.add_parser("stop")
    s.add_argument("key")
    s.set_defaults(fn=cmd_stop)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("mystery").set_defaults(fn=lambda a: print(json.dumps(_call("POST", "/api/mystery", {}))))
    sub.add_parser("reveal").set_defaults(fn=lambda a: print(json.dumps(_call("POST", "/api/reveal", {}), indent=2)))
    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
