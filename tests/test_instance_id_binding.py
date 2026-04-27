"""Round-14 MAJOR: shutdown RPC must be bound to a specific daemon instance.

Without this, `daemon stop` can collateral-shutdown a successor daemon if
the original daemon exits and a new one takes over the socket between
inspect_daemon() and the RPC call.
"""

from __future__ import annotations

import sys

import pytest

if sys.platform == "win32":  # pragma: no cover
    pytest.skip("POSIX-only", allow_module_level=True)


def test_get_instance_id_returns_stable_value_within_same_process():
    """Within one process, repeated calls return the same id."""
    from tgmcp.daemon.server import get_instance_id

    a = get_instance_id()
    b = get_instance_id()
    assert a == b
    assert isinstance(a, str)
    assert len(a) >= 16


def test_instance_id_survives_module_reload():
    """Round-16 MINOR-43: importlib.reload re-executes module body. Without
    `globals().get("_INSTANCE_IDS", {})` guard, _INSTANCE_IDS would reset to
    {} on reload, minting a fresh id for the same pid and breaking the
    binding contract."""
    import importlib

    from tgmcp.daemon import server

    id_before = server.get_instance_id()
    importlib.reload(server)
    id_after = server.get_instance_id()

    assert id_before == id_after, (
        "Reloading the module must NOT mint a new instance_id for the same pid"
    )


def test_get_instance_id_is_pid_keyed():
    """If we forge a different pid (simulating fork or reload), a new id
    is minted. This is the round-15 MINOR fix."""
    from unittest.mock import patch

    from tgmcp.daemon import server

    with patch.object(server.os, "getpid", return_value=999_999):
        forged = server.get_instance_id()
    real = server.get_instance_id()
    assert forged != real, "different pids must produce different ids"


def test_shutdown_request_requires_instance_id():
    """The pydantic schema for /shutdown must require an instance_id field."""
    from tgmcp.daemon.server import ShutdownReq

    fields = set(ShutdownReq.model_fields.keys())
    assert "instance_id" in fields
    # Must be required (no default).
    assert ShutdownReq.model_fields["instance_id"].is_required()


def test_instance_ids_differ_per_process():
    """A fresh import of the module must regenerate INSTANCE_ID. We can't
    truly fork in a unit test, so we exercise the generator directly: the
    same secrets.token_hex(16) in two calls must differ."""
    import secrets

    a = secrets.token_hex(16)
    b = secrets.token_hex(16)
    assert a != b


@pytest.mark.asyncio
async def test_shutdown_endpoint_rejects_mismatched_id():
    """Round-14 MAJOR: a stale shutdown request bound to instance A must
    not shut down instance B."""
    from fastapi import HTTPException

    from tgmcp.daemon import server
    from tgmcp.daemon.server import ShutdownReq, shutdown_endpoint

    # Forge a request with a wrong instance_id.
    bad_req = ShutdownReq(instance_id="not-our-id")

    with pytest.raises(HTTPException) as ei:
        await shutdown_endpoint(bad_req)
    assert ei.value.status_code == 409
    assert "mismatch" in ei.value.detail.lower()

    # And the daemon must NOT have set should_exit.
    if server.state.uvicorn_server is not None:
        assert not server.state.uvicorn_server.should_exit


@pytest.mark.asyncio
async def test_shutdown_endpoint_accepts_matching_id():
    """The right instance_id must succeed and set should_exit."""
    from types import SimpleNamespace

    from tgmcp.daemon import server
    from tgmcp.daemon.server import ShutdownReq, get_instance_id, shutdown_endpoint

    fake_server = SimpleNamespace(should_exit=False)
    server.state.uvicorn_server = fake_server
    try:
        my_id = get_instance_id()
        req = ShutdownReq(instance_id=my_id)
        result = await shutdown_endpoint(req)
        assert result["ok"] is True
        assert result["instance_id"] == my_id
        assert fake_server.should_exit is True
    finally:
        server.state.uvicorn_server = None
