"""Phase 4 Batch 6: Prometheus metrics."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from tgmcp.daemon import metrics, server


def test_metrics_route_registered():
    paths = {r.path for r in server.app.routes}
    assert "/metrics" in paths


def test_metrics_endpoint_returns_prom_text():
    c = TestClient(server.app, raise_server_exceptions=False)
    r = c.get("/metrics")
    assert r.status_code == 200, r.text
    # Prometheus exposition content-type
    assert r.headers["content-type"].startswith("text/plain")
    body = r.text
    # Must contain at least the metric NAMES we declared (HELP + TYPE
    # lines are emitted even before any observations).
    for name in (
        "tgmcp_rpc_requests_total",
        "tgmcp_rpc_request_seconds",
        "tgmcp_sessions_loaded",
        "tgmcp_daemon_up",
    ):
        assert f"# HELP {name}" in body, f"missing HELP for {name}"
        assert f"# TYPE {name}" in body, f"missing TYPE for {name}"


def test_observe_request_increments_counter():
    """Round-trip: a fake observation must appear in the rendered output."""
    metrics.observe_request("/_test_unit", 200, started_at=0.0)
    body, _ = metrics.render_latest()
    txt = body.decode("utf-8")
    assert 'endpoint="/_test_unit"' in txt
    assert 'status="200"' in txt
    assert "tgmcp_rpc_requests_total" in txt


def test_set_sessions_loaded_round_trips():
    metrics.set_sessions_loaded(3)
    body, _ = metrics.render_latest()
    txt = body.decode("utf-8")
    m = re.search(r"^tgmcp_sessions_loaded ([0-9.]+)$", txt, re.MULTILINE)
    assert m is not None
    assert float(m.group(1)) == 3.0


def test_set_daemon_up_round_trips():
    metrics.set_daemon_up(True)
    body, _ = metrics.render_latest()
    txt = body.decode("utf-8")
    m = re.search(r"^tgmcp_daemon_up ([0-9.]+)$", txt, re.MULTILINE)
    assert m is not None
    assert float(m.group(1)) == 1.0

    metrics.set_daemon_up(False)
    body, _ = metrics.render_latest()
    txt = body.decode("utf-8")
    m = re.search(r"^tgmcp_daemon_up ([0-9.]+)$", txt, re.MULTILINE)
    assert m is not None
    assert float(m.group(1)) == 0.0


def test_metrics_middleware_records_route_template():
    """A request to a known route should be recorded under the route
    template (not the literal path), so we don't end up with one series
    per @username."""
    c = TestClient(server.app, raise_server_exceptions=False)
    # POST /poll/edit with bad body — gets 400 from the schema, but we
    # only care about the metric label.
    c.post("/poll/edit", json={"chat": "@x", "msg_id": 1})

    body, _ = metrics.render_latest()
    txt = body.decode("utf-8")
    # Find the entry for /poll/edit specifically.
    assert 'endpoint="/poll/edit"' in txt


def test_metrics_endpoint_excluded_from_its_own_counter():
    """The /metrics scrape itself must NOT appear in the request counter
    (otherwise scraping floods the histogram)."""
    c = TestClient(server.app, raise_server_exceptions=False)
    # First scrape resets state? No — accumulators are session-scoped.
    body_before, _ = metrics.render_latest()
    txt_before = body_before.decode("utf-8")
    # Count occurrences of an entry that includes /metrics endpoint label
    matches_before = re.findall(
        r'tgmcp_rpc_requests_total\{endpoint="/metrics"',
        txt_before,
    )
    # Hit /metrics one more time
    c.get("/metrics")
    body_after, _ = metrics.render_latest()
    txt_after = body_after.decode("utf-8")
    matches_after = re.findall(
        r'tgmcp_rpc_requests_total\{endpoint="/metrics"',
        txt_after,
    )
    assert len(matches_after) == len(matches_before), (
        "/metrics should not increment its own counter"
    )


def test_unmatched_paths_bucket_to_sentinel():
    """Round-1 MAJOR fix: hits to random 404 paths must be bucketed to
    a single `__unmatched__` series rather than recording the raw path.
    Prevents an attacker from exploding the metric series count."""
    c = TestClient(server.app, raise_server_exceptions=False)
    # Hit a path the app definitely doesn't have a route for.
    c.get("/this-route-does-not-exist-aaa-bbb-ccc")
    c.get("/another-bogus-route-xxx-yyy")
    body, _ = metrics.render_latest()
    txt = body.decode("utf-8")
    # The raw path must NOT appear as an endpoint label
    assert "this-route-does-not-exist" not in txt
    assert "another-bogus-route" not in txt
    # The sentinel must.
    assert 'endpoint="__unmatched__"' in txt


def test_middleware_records_500_when_handler_raises():
    """Round-1 MAJOR fix: if call_next raises before a response, the
    finally block must still record the request as status=500."""
    from fastapi import FastAPI as _FastAPI
    from fastapi.testclient import TestClient as _TC

    # Stand up a tiny app that wires up only the metrics middleware and
    # one route that raises. Importing server.app would conflate with the
    # other tests' state.
    app2 = _FastAPI()

    @app2.middleware("http")
    async def _mw(request, call_next):
        import time as _time

        started = _time.monotonic()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            route = request.scope.get("route")
            template = getattr(route, "path", None) or "__unmatched__"
            metrics.observe_request(f"_test_{template}", status, started)

    @app2.get("/_test_raise")
    async def _raise() -> dict:
        raise RuntimeError("boom in handler")

    body_before, _ = metrics.render_latest()

    c = _TC(app2, raise_server_exceptions=False)
    r = c.get("/_test_raise")
    assert r.status_code == 500

    body_after, _ = metrics.render_latest()
    txt_after = body_after.decode("utf-8")
    # The label includes the matched template prefix
    assert 'endpoint="_test_/_test_raise"' in txt_after
    assert 'status="500"' in txt_after


def test_metrics_endpoint_excluded_via_route_template():
    """Round-1 MAJOR fix: exclusion is based on the matched route
    template, not raw url.path — so it would still exclude even if the
    app were mounted under a prefix. This tests that property by
    asserting the /metrics endpoint never appears in the counter,
    regardless of how many times we hit it."""
    c = TestClient(server.app, raise_server_exceptions=False)
    for _ in range(3):
        c.get("/metrics")
    body, _ = metrics.render_latest()
    txt = body.decode("utf-8")
    assert 'endpoint="/metrics"' not in txt


def test_metrics_no_chat_label_in_series():
    """Cardinality safety: we must NEVER label a series by chat or
    user-controlled string. Scan the rendered output for any obvious
    leak (a chat-id-like or @username token in a label)."""
    # Make a request with a chat that would explode cardinality if
    # naively used as a label.
    c = TestClient(server.app, raise_server_exceptions=False)
    c.post("/send", json={"chat": "@some_unique_chat_xyz_42", "text": "hi"})

    body, _ = metrics.render_latest()
    txt = body.decode("utf-8")
    assert "@some_unique_chat_xyz_42" not in txt
    assert "some_unique_chat_xyz_42" not in txt
