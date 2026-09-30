"""Optional Honeycomb markers for deploys and scenarios (needs HONEYCOMB_CONFIG_KEY with 'Manage Markers').

Grafana gets the same information as annotations from the `sim_scenario_active` metric instead.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.request

log = logging.getLogger("metricsim.markers")


class Markers:
    def __init__(self, cfg):
        self.key = cfg.honeycomb_config_key
        self.url = f"{cfg.honeycomb_api}/1/markers/{cfg.marker_dataset}"

    def send(self, message: str, kind: str):
        if not self.key:
            return
        threading.Thread(target=self._post, args=(message, kind), daemon=True).start()

    def _post(self, message, kind):
        body = json.dumps({"message": message, "type": kind}).encode()
        req = urllib.request.Request(
            self.url, data=body, method="POST",
            headers={"X-Honeycomb-Team": self.key, "Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=5).read()
        except Exception as exc:  # markers are a nicety; never let them break the sim
            log.warning("marker failed: %s", exc)
