"""HTTP layer.

* one listening socket per simulated process (ports 9101+ for apps, 9201+ for infra). When a process is down
  or restarting its socket is really closed, so scrapers see "connection refused" exactly as they would in
  real life, rather than a polite 503.
* the control plane on :8080 - UI, JSON API, HTTP service discovery for Prometheus/the collector, and the
  simulator's own ground-truth metrics.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from importlib import resources

from aiohttp import web

from . import prom
from .engine import Engine
from .scenarios import CATALOG
from .targets import AppInstance

log = logging.getLogger("metricsim.http")


class TargetServer:
    def __init__(self, eng: Engine, target):
        self.eng = eng
        self.t = target
        self.runner: web.AppRunner | None = None

    @property
    def listening(self):
        return self.runner is not None

    async def start(self):
        app = web.Application()
        app.router.add_get("/metrics", self.metrics)
        app.router.add_get("/", self.index)
        self.runner = web.AppRunner(app, access_log=None, handle_signals=False)
        await self.runner.setup()
        await web.TCPSite(self.runner, "0.0.0.0", self.t.port, reuse_address=True).start()

    async def stop(self):
        runner, self.runner = self.runner, None
        if runner:
            await runner.cleanup()  # closes the listener AND open keep-alive connections

    async def index(self, request):
        return web.Response(text=f"{self.t.name} ({self.t.kind}) - metrics at /metrics\n")

    async def metrics(self, request):
        fx = self.eng.fx
        delay = fx.scrape_delay_s.get(self.t.name)
        if delay:
            await asyncio.sleep(delay)
        skew = fx.clock_skew_s.get(self.t.name)
        ts = int((time.time() + skew) * 1000) if skew else None
        body = self.t.reg.render(ts)
        return web.Response(body=body.encode(), headers={"Content-Type": prom.CONTENT_TYPE})


def _is_up(t):
    return t.up if isinstance(t, AppInstance) else t.available


class ControlPlane:
    def __init__(self, eng: Engine):
        self.eng = eng
        self.servers = {t.name: TargetServer(eng, t) for t in eng.targets()}

    # ---- lifecycle -------------------------------------------------------------------------------------

    async def reconcile(self):
        for t in self.eng.targets():
            srv = self.servers[t.name]
            want = _is_up(t)
            if want and not srv.listening:
                try:
                    await srv.start()
                except OSError as exc:
                    log.warning("cannot listen on %s: %s", t.port, exc)
            elif not want and srv.listening:
                await srv.stop()

    async def loop(self):
        last = time.time()
        next_t = last
        while True:
            now = time.time()
            try:
                self.eng.tick(now, max(0.2, min(5.0, now - last)))
            except Exception:
                log.exception("tick failed")
            last = now
            await self.reconcile()
            if int(now) % 30 == 0:
                st = self.eng.status()
                act = ", ".join(a["key"] for a in st["active"]) or "none"
                log.info("frontend %.1f rps (x%.2f)  tick %.0fms  restarts %d  active: %s",
                         st["frontend_rps"], st["traffic_multiplier"], st["tick_ms"], st["restarts"], act)
            next_t += 1.0
            await asyncio.sleep(max(0.0, next_t - time.time()))

    # ---- control API -----------------------------------------------------------------------------------

    def app(self) -> web.Application:
        app = web.Application()
        r = app.router
        r.add_get("/", self.ui)
        r.add_get("/metrics", self.self_metrics)
        r.add_get("/sd/apps", self.sd_apps)
        r.add_get("/sd/infra", self.sd_infra)
        r.add_get("/api/status", self.api_status)
        r.add_get("/api/scenarios", self.api_scenarios)
        r.add_post("/api/scenarios/{key}/start", self.api_start)
        r.add_post("/api/scenarios/{key}/stop", self.api_stop)
        r.add_post("/api/stop_all", self.api_stop_all)
        r.add_post("/api/mystery", self.api_mystery)
        r.add_post("/api/reveal", self.api_reveal)
        return app

    async def ui(self, request):
        html = resources.files("metricsim").joinpath("ui.html").read_text()
        return web.Response(text=html, content_type="text/html")

    async def self_metrics(self, request):
        return web.Response(body=self.eng.self_reg.render().encode(), headers={"Content-Type": prom.CONTENT_TYPE})

    def _sd(self, targets):
        host = self.eng.cfg.advertise_host
        return web.json_response([{"targets": [f"{host}:{t.port}"], "labels": t.sd_labels()} for t in targets])

    async def sd_apps(self, request):
        return self._sd(self.eng.instances)

    async def sd_infra(self, request):
        return self._sd(self.eng.infra)

    async def api_status(self, request):
        return web.json_response(self.eng.status())

    async def api_scenarios(self, request):
        return web.json_response({
            "catalog": [c.catalog_entry() for c in CATALOG.values()],
            "active": self.eng.status()["active"],
            "history": self.eng.scenarios.history[-50:],
        })

    async def api_start(self, request):
        key = request.match_info["key"]
        try:
            body = await request.json() if request.can_read_body else {}
        except json.JSONDecodeError:
            return web.json_response({"error": "body must be JSON"}, status=400)
        try:
            sc = self.eng.scenarios.start(key, body.get("params") or {}, body.get("duration"), body.get("ramp"))
        except (KeyError, ValueError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response(sc.describe())

    async def api_stop(self, request):
        try:
            return web.json_response({"stopping": self.eng.scenarios.stop(request.match_info["key"])})
        except KeyError as exc:
            return web.json_response({"error": str(exc)}, status=404)

    async def api_stop_all(self, request):
        return web.json_response({"stopping": self.eng.scenarios.stop("all")})

    async def api_mystery(self, request):
        sc = self.eng.scenarios.start_mystery()
        return web.json_response({"started": "mystery", "ends_in_s": round(sc.ends - time.time())})

    async def api_reveal(self, request):
        return web.json_response({"active": self.eng.scenarios.reveal()})


async def serve(eng: Engine):
    cp = ControlPlane(eng)
    runner = web.AppRunner(cp.app(), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", eng.cfg.control_port).start()
    log.info("control UI on :%d, %d targets", eng.cfg.control_port, len(cp.servers))
    await cp.reconcile()
    await cp.loop()
