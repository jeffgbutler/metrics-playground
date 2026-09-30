"""The simulated system: a small online store ("Byte Mart") on four hosts.

Everything here is static shape. Mutable runtime state lives on `Instance`/`Node`/`Infra` objects and is
driven by `engine.py`. Ports are fixed so Prometheus and the collector can find targets through the
simulator's HTTP service discovery endpoints (`/sd/apps`, `/sd/infra`).

    browser ─> frontend ─> api-gateway ─┬─> catalog ──> redis, postgres
                                        ├─> cart ─────> redis
                                        └─> checkout ─┬─> cart
                                                      ├─> inventory ─> postgres
                                                      ├─> payments ──> postgres, external card provider
                                                      ├─> shipping ──> external carrier API
                                                      └─> kafka "orders" ─> notifications (consumer)
"""

from __future__ import annotations

from dataclasses import dataclass, field

APP_PORT_BASE = 9101
INFRA_PORT_BASE = 9201

GiB = 1024**3


@dataclass
class Route:
    method: str
    path: str
    median_ms: float  # own work, excluding downstream calls
    sigma: float = 0.35  # lognormal shape; bigger = longer tail
    calls: tuple = ()  # ((service, route_key, probability), ...)
    uses: tuple = ()  # infra touched: "redis", "postgres", "postgres_write", "kafka", "provider:x", "carrier"
    base_error: float = 0.001
    timeout_s: float = 5.0

    @property
    def key(self):
        return f"{self.method} {self.path}"


@dataclass
class ServiceDef:
    name: str
    runtime: str  # go | jvm | python | node : decides which runtime metrics the instance exposes
    team: str
    replicas: int
    version: str
    routes: dict = field(default_factory=dict)
    db_pool_max: int = 0  # 0 = no DB pool metrics
    worker: bool = False  # queue consumer (no HTTP routes)


def _routes(*rs: Route) -> dict:
    return {r.key: r for r in rs}


SERVICES: dict[str, ServiceDef] = {
    s.name: s
    for s in [
        ServiceDef(
            "frontend", "node", "web", 3, "4.12.0",
            _routes(
                Route("GET", "/", 18, calls=(("api-gateway", "GET /api/products", 1.0),)),
                Route("GET", "/product/:id", 22, calls=(("api-gateway", "GET /api/products/:id", 1.0),)),
                Route("GET", "/cart", 15, calls=(("api-gateway", "GET /api/cart", 1.0),)),
                Route("POST", "/cart", 12, calls=(("api-gateway", "POST /api/cart/items", 1.0),)),
                Route("POST", "/checkout", 25, calls=(("api-gateway", "POST /api/checkout", 1.0),), timeout_s=10),
            ),
        ),
        ServiceDef(
            "api-gateway", "go", "platform", 2, "2.3.1",
            _routes(
                Route("GET", "/api/products", 2, calls=(("catalog", "GET /products", 1.0),)),
                Route("GET", "/api/products/:id", 2, calls=(("catalog", "GET /products/:id", 1.0),)),
                Route("GET", "/api/search", 2, calls=(("catalog", "GET /search", 1.0),)),
                Route("GET", "/api/cart", 2, calls=(("cart", "GET /cart", 1.0),)),
                Route("POST", "/api/cart/items", 2, calls=(("cart", "POST /cart/items", 1.0),)),
                Route("POST", "/api/checkout", 3, calls=(("checkout", "POST /checkout", 1.0),), timeout_s=8),
                # A low-traffic legacy endpoint that the `series_disappear` scenario retires.
                Route("GET", "/api/v1/legacy-products", 40, calls=(("catalog", "GET /products", 1.0),)),
            ),
        ),
        ServiceDef(
            "catalog", "go", "catalog", 2, "1.8.4",
            _routes(
                Route("GET", "/products", 6, uses=("redis", "postgres")),
                Route("GET", "/products/:id", 4, uses=("redis", "postgres")),
                Route("GET", "/search", 45, sigma=0.6, uses=("postgres",)),
            ),
            db_pool_max=20,
        ),
        ServiceDef(
            "cart", "python", "checkout", 2, "3.1.0",
            _routes(
                Route("GET", "/cart", 5, uses=("redis",)),
                Route("POST", "/cart/items", 7, uses=("redis",)),
            ),
        ),
        ServiceDef(
            "checkout", "jvm", "checkout", 2, "7.4.2",
            _routes(
                Route(
                    "POST", "/checkout", 30,
                    calls=(
                        ("cart", "GET /cart", 1.0),
                        ("inventory", "POST /reserve", 1.0),
                        ("payments", "POST /charge", 1.0),
                        ("shipping", "POST /quote", 1.0),
                    ),
                    uses=("postgres_write", "kafka"),
                    timeout_s=8,
                ),
            ),
            db_pool_max=10,
        ),
        ServiceDef(
            "payments", "jvm", "payments", 2, "5.0.3",
            _routes(Route("POST", "/charge", 12, uses=("provider", "postgres_write"), timeout_s=5)),
            db_pool_max=10,
        ),
        ServiceDef(
            "inventory", "go", "catalog", 2, "2.2.0",
            _routes(
                Route("POST", "/reserve", 8, uses=("postgres_write",)),
                Route("GET", "/stock/:sku", 3, uses=("postgres",)),
            ),
            db_pool_max=8,
        ),
        ServiceDef(
            "shipping", "python", "fulfilment", 1, "1.4.0",
            _routes(Route("POST", "/quote", 10, uses=("carrier",))),
        ),
        ServiceDef("notifications", "python", "fulfilment", 2, "2.0.1", worker=True),
    ]
}

# Share of front-door page views. Checkout is rare, which is realistic and makes its metrics noisy.
JOURNEYS = [
    ("GET /", 0.34),
    ("GET /product/:id", 0.40),
    ("GET /cart", 0.12),
    ("POST /cart", 0.10),
    ("POST /checkout", 0.04),
]
# Requests that hit the gateway directly (mobile app / partners), per second at peak.
DIRECT_GATEWAY = [("GET /api/search", 3.0), ("GET /api/v1/legacy-products", 0.3), ]
DIRECT_INVENTORY = [("GET /stock/:sku", 2.0)]

NODES = ["node-a", "node-b", "node-c", "node-d"]
NODE_CPUS = 4
NODE_MEM = 16 * GiB
NODE_DISK = 200 * 1024**3

PAYMENT_PROVIDERS = [("acmepay", 0.7), ("globexpay", 0.3)]
PAYMENT_METHODS = [("card", 0.72), ("wallet", 0.2), ("gift_card", 0.08)]
SKUS = [f"sku-{i:03d}" for i in range(1, 21)]
NOTIFY_CHANNELS = [("email", 0.7), ("sms", 0.2), ("push", 0.1)]
KAFKA_PARTITIONS = 3


@dataclass
class InstanceDef:
    service: str
    index: int
    node: str
    port: int

    @property
    def name(self):
        return f"{self.service}-{self.index}"


def build_instances() -> list[InstanceDef]:
    """Spread replicas across nodes deterministically (round-robin with an offset per service)."""
    out = []
    port = APP_PORT_BASE
    for si, svc in enumerate(SERVICES.values()):
        for i in range(svc.replicas):
            node = NODES[(si + i) % len(NODES)]
            out.append(InstanceDef(svc.name, i + 1, node, port))
            port += 1
    return out


# Simulated infrastructure exporters. Each is its own scrape target, just like the real exporters.
#   (kind, name, node, port)
INFRA_TARGETS = [("node", n, n, INFRA_PORT_BASE + i) for i, n in enumerate(NODES)] + [
    ("postgres", "postgres-primary", "node-d", INFRA_PORT_BASE + 10),
    ("postgres", "postgres-replica", "node-c", INFRA_PORT_BASE + 11),
    ("redis", "redis", "node-b", INFRA_PORT_BASE + 12),
    ("kafka", "kafka", "node-a", INFRA_PORT_BASE + 13),
]
