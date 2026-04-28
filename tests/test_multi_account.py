"""Multi-account runtime switching: schema, route, state isolation, audit safety."""

from __future__ import annotations

import inspect

import pytest

from tgmcp.daemon import server


def test_state_holds_dict_not_single_session():
    assert hasattr(server.state, "sessions")
    assert isinstance(server.state.sessions, dict)
    assert hasattr(server.state, "active_label")
    assert not hasattr(server.state, "session"), (
        "the old single-session attribute must be gone — callers should "
        "go through state.sessions[active_label] / _sess()"
    )


def test_switch_account_route_registered():
    paths = {r.path for r in server.app.routes}
    assert "/accounts/switch" in paths


def test_switch_account_request_schema():
    fields = server.SwitchAccountReq.model_fields
    assert {"label", "passphrase"}.issubset(fields.keys())
    # passphrase must be optional (keychain accounts don't need one)
    assert not fields["passphrase"].is_required()


def test_health_includes_active_label_and_loaded_list():
    """The /health payload now reports both active_label and loaded_labels
    so a CLI client can render multi-account state."""
    src = inspect.getsource(server.health)
    assert "active_label" in src
    assert "loaded_labels" in src


def test_accounts_endpoint_includes_loaded_and_active():
    src = inspect.getsource(server.accounts)
    assert "active_label" in src
    assert "loaded_labels" in src


def test_account_status_field_names_consistent():
    """`/health`, `/accounts`, and `/accounts/switch` must all expose the
    same `active_label` + `loaded_labels` field names so clients don't have
    to special-case per endpoint."""
    health_src = inspect.getsource(server.health)
    accounts_src = inspect.getsource(server.accounts)
    switch_src = inspect.getsource(server.switch_account)

    for name, src in (
        ("/health", health_src),
        ("/accounts", accounts_src),
        ("/accounts/switch", switch_src),
    ):
        assert "active_label" in src, f"{name} missing active_label"
        assert "loaded_labels" in src, f"{name} missing loaded_labels"
    # And the legacy stand-alone "active" / "loaded" must NOT be present.
    for name, src in (
        ("/accounts", accounts_src),
        ("/accounts/switch", switch_src),
    ):
        assert '"active"' not in src.replace('"active_label"', ""), f"{name} still uses bare 'active'"
        assert '"loaded"' not in src.replace('"loaded_labels"', ""), f"{name} still uses bare 'loaded'"


@pytest.mark.asyncio
async def test_switch_account_rejects_unknown_label(monkeypatch):
    """Switching to an account that doesn't exist on disk must 404."""
    from fastapi import HTTPException

    monkeypatch.setattr(server.auth, "list_accounts", lambda: ["main"])

    req = server.SwitchAccountReq(label="ghost")
    with pytest.raises(HTTPException) as ei:
        await server.switch_account(req)
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_switch_account_loads_lazily_and_caches(monkeypatch):
    """First switch loads the session; second switch reuses the cached one."""
    from types import SimpleNamespace

    monkeypatch.setattr(server.auth, "list_accounts", lambda: ["main", "alt"])

    fake_session = SimpleNamespace(
        cfg=SimpleNamespace(label="alt"),
        me_id=42,
    )

    open_calls = []

    async def fake_open(label, passphrase):
        open_calls.append((label, passphrase))
        return fake_session

    monkeypatch.setattr(server, "_open_session", fake_open)

    # Reset state.
    server.state.sessions = {}
    server.state.active_label = None

    res1 = await server.switch_account(server.SwitchAccountReq(label="alt"))
    assert res1["active_label"] == "alt"
    assert res1["me_id"] == 42
    assert open_calls == [("alt", None)]
    assert server.state.active_label == "alt"

    # Second switch to same label: no extra open.
    res2 = await server.switch_account(server.SwitchAccountReq(label="alt"))
    assert res2["ok"] is True
    assert open_calls == [("alt", None)], "must reuse cached session"


@pytest.mark.asyncio
async def test_concurrent_switch_to_same_label_serializes_loads(monkeypatch):
    """Two concurrent /accounts/switch calls to the SAME cold label must
    NOT both call _open_session — that would create two live TGSession
    objects pointing at the same account, with the second clobbering the
    first (which would then leak past lifespan teardown)."""
    import asyncio
    from types import SimpleNamespace

    monkeypatch.setattr(server.auth, "list_accounts", lambda: ["alt"])

    open_calls = 0
    inside_open = asyncio.Event()
    release_open = asyncio.Event()

    async def slow_open(label, passphrase):
        nonlocal open_calls
        open_calls += 1
        inside_open.set()
        # Simulate a slow auth/connect — give the second caller a chance to
        # race past the pre-await check.
        await release_open.wait()
        return SimpleNamespace(cfg=SimpleNamespace(label=label), me_id=42)

    monkeypatch.setattr(server, "_open_session", slow_open)

    server.state.sessions = {}
    server.state.active_label = None
    server.state.load_locks = {}

    t1 = asyncio.create_task(server.switch_account(server.SwitchAccountReq(label="alt")))
    # Wait until the first caller is inside _open_session.
    await inside_open.wait()
    # Now fire a concurrent second switch to the same label.
    t2 = asyncio.create_task(server.switch_account(server.SwitchAccountReq(label="alt")))
    # Give task 2 a moment to advance and (correctly) block on the lock.
    await asyncio.sleep(0)
    # Release the slow open so task 1 finishes.
    release_open.set()

    res1 = await t1
    res2 = await t2

    assert open_calls == 1, (
        f"_open_session called {open_calls} times for the same cold label; "
        "concurrent switches must serialize"
    )
    assert res1["ok"] and res2["ok"]
    assert len(server.state.sessions) == 1


@pytest.mark.asyncio
async def test_failed_open_does_not_cache_and_wipes_passphrase(monkeypatch):
    """If _open_session raises (bad passphrase, RPC error), the failed
    label must NOT end up in state.sessions, so a follow-up call can retry."""
    monkeypatch.setattr(server.auth, "list_accounts", lambda: ["alt"])

    async def boom(label, passphrase):
        raise RuntimeError("auth failed")

    monkeypatch.setattr(server, "_open_session", boom)

    server.state.sessions = {}
    server.state.active_label = None
    server.state.load_locks = {}

    req = server.SwitchAccountReq(label="alt", passphrase="hunter2")
    with pytest.raises(RuntimeError):
        await server.switch_account(req)

    assert "alt" not in server.state.sessions
    assert req.passphrase is None, (
        "request body must be wiped even on the failure path"
    )


@pytest.mark.asyncio
async def test_switch_account_audit_does_not_log_passphrase(monkeypatch):
    """The audit log must record the switch but NEVER the passphrase."""
    from types import SimpleNamespace

    monkeypatch.setattr(server.auth, "list_accounts", lambda: ["main", "secret_acct"])

    async def fake_open(label, passphrase):
        return SimpleNamespace(cfg=SimpleNamespace(label=label), me_id=1)

    monkeypatch.setattr(server, "_open_session", fake_open)

    audit_calls: list[tuple] = []

    def fake_audit(action, **fields):
        audit_calls.append((action, fields))

    monkeypatch.setattr(server.audit, "log", fake_audit)

    server.state.sessions = {}
    server.state.active_label = None
    await server.switch_account(
        server.SwitchAccountReq(label="secret_acct", passphrase="hunter2")
    )

    assert len(audit_calls) == 1
    action, fields = audit_calls[0]
    assert action == "account_switch"
    flat = repr((action, fields))
    assert "hunter2" not in flat, "passphrase leaked into audit fields"
    assert "passphrase" not in fields, "passphrase key must not appear in audit"


def test_client_has_switch_account_method():
    from tgmcp.client import DaemonClient

    sig = inspect.signature(DaemonClient.switch_account)
    assert "label" in sig.parameters
    assert "passphrase" in sig.parameters


def test_cli_has_account_use_command():
    """CLI wiring: `tgmcp account use <label>` must be registered with the
    same option shape as init (flag, not value-taking)."""
    from click.testing import CliRunner

    from tgmcp.cli.main import cli

    res = CliRunner().invoke(cli, ["account", "use", "--help"])
    assert res.exit_code == 0, res.output
    assert "--passphrase" in res.output
    assert "--passphrase-stdin" in res.output
    assert "--passphrase TEXT" not in res.output, (
        "--passphrase must be a flag (hidden prompt), not a value option"
    )


@pytest.fixture(autouse=True)
def _reset_state():
    saved_sessions = dict(server.state.sessions)
    saved_active = server.state.active_label
    yield
    server.state.sessions = saved_sessions
    server.state.active_label = saved_active
