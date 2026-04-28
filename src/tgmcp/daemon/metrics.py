"""Prometheus metrics for the daemon.

We expose a small surface (counter + histogram + gauge) and let
Prometheus do the aggregation. Cardinality is intentionally low:
labels are `endpoint` (the FastAPI route path) and `status` (the
HTTP status code as a string). We do NOT label by `chat`, `account`,
or any user-controllable string — those would let a noisy caller
explode the metric series.
"""

from __future__ import annotations

import time
from contextvars import ContextVar
from typing import Any

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

REGISTRY = CollectorRegistry()

# Per-endpoint request counter, partitioned by status.
RPC_TOTAL = Counter(
    "tgmcp_rpc_requests_total",
    "Total number of RPC requests handled by the daemon.",
    labelnames=("endpoint", "status"),
    registry=REGISTRY,
)

# Latency histogram. Buckets tuned for typical Telethon round-trips:
# fast (<100ms) for cached reads, ~1s for typical RPCs, several
# seconds for media upload / search.
RPC_LATENCY = Histogram(
    "tgmcp_rpc_request_seconds",
    "End-to-end request latency at the daemon.",
    labelnames=("endpoint",),
    buckets=(
        0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0
    ),
    registry=REGISTRY,
)

# Loaded session count (gauge — set on lifespan + switch).
SESSIONS_LOADED = Gauge(
    "tgmcp_sessions_loaded",
    "Number of TGSession instances currently loaded in this daemon.",
    registry=REGISTRY,
)

# Daemon's authoritative liveness (1 if uvicorn is serving, 0 otherwise).
DAEMON_UP = Gauge(
    "tgmcp_daemon_up",
    "1 if the daemon's lifespan startup completed; 0 otherwise.",
    registry=REGISTRY,
)


# Active-request context. We capture the start time and endpoint path in
# a ContextVar so a single decorator at the FastAPI middleware layer can
# emit both the counter and histogram observation per request.
_REQUEST_START: ContextVar[float | None] = ContextVar("rpc_start", default=None)


def render_latest() -> tuple[bytes, str]:
    """Return (body, content-type) for an HTTP /metrics response."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


def observe_request(endpoint: str, status: int, started_at: float) -> None:
    """Record one request's outcome. `started_at` is from time.monotonic()."""
    elapsed = max(0.0, time.monotonic() - started_at)
    RPC_TOTAL.labels(endpoint=endpoint, status=str(status)).inc()
    RPC_LATENCY.labels(endpoint=endpoint).observe(elapsed)


def set_sessions_loaded(n: int) -> None:
    SESSIONS_LOADED.set(n)


def set_daemon_up(up: bool) -> None:
    DAEMON_UP.set(1 if up else 0)
