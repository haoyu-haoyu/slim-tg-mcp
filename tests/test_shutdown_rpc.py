"""Round-13 MAJOR: replace pid-based stop with daemon-side shutdown RPC.

Pid signaling has an unavoidable TOCTOU between probe and os.kill: the
daemon can exit and the kernel can recycle the pid before our signal
reaches anyone. The /shutdown endpoint sidesteps this — the daemon stops
itself via uvicorn.should_exit, and the lifespan teardown releases the
flock and unlinks the socket cleanly.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

if sys.platform == "win32":  # pragma: no cover
    pytest.skip("POSIX-only", allow_module_level=True)

from tgmcp.cli import main as cli_main  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(cli_main, "PID_PATH", tmp_path / "pid")
    monkeypatch.setattr(cli_main, "SOCKET_PATH", tmp_path / "sock")
    monkeypatch.setattr(cli_main, "LOCK_PATH", tmp_path / "lock")
    yield


def test_stop_uses_rpc_not_sigterm_on_happy_path():
    """When the /shutdown RPC succeeds, no SIGTERM should be sent."""
    my_pid = os.getpid()
    cli_main.PID_PATH.write_text(str(my_pid))
    cli_main.SOCKET_PATH.touch()

    # Lock release sequence: held → released after RPC.
    locked_seq = iter([(False, None)] * 5)

    with patch.object(
        cli_main,
        "_probe_existing_daemon",
        return_value={"ok": True, "pid": my_pid, "instance_id": "abc123"},
    ):
        with patch(
            "tgmcp.daemon.server.is_daemon_locked",
            side_effect=lambda: next(locked_seq, (False, None)),
        ):
            # Mock the DaemonClient to return success on /shutdown.
            mock_client = patch("tgmcp.client.DaemonClient").start()
            mock_client.return_value.__enter__.return_value.shutdown.return_value = {
                "ok": True,
                "pid": my_pid,
                "instance_id": "abc123",
            }
            try:
                with patch("tgmcp.cli.main.os.kill") as kill_mock:
                    with patch("tgmcp.cli.main.time.sleep"):
                        res = CliRunner().invoke(cli_main.cli, ["daemon", "stop"])
            finally:
                patch.stopall()

    assert res.exit_code == 0, res.output
    import signal as _sig

    sigterm_calls = [c for c in kill_mock.call_args_list if c.args[1] == _sig.SIGTERM]
    assert sigterm_calls == [], "RPC path should not fall back to SIGTERM"
    assert "graceful shutdown" in res.output.lower()


def test_instance_bound_rpc_failure_is_terminal_no_pid_fallback():
    """Round-16 MAJOR-42: if /health gave us an instance_id and the RPC then
    fails (timeout, transport error, 5xx), we must NOT fall back to pid-based
    SIGTERM. The daemon may have died and been replaced with the same pid;
    SIGTERM would hit the wrong process."""
    my_pid = os.getpid()
    cli_main.PID_PATH.write_text(str(my_pid))
    cli_main.SOCKET_PATH.touch()

    with patch.object(
        cli_main,
        "_probe_existing_daemon",
        return_value={"ok": True, "pid": my_pid, "instance_id": "abc123"},
    ):
        mock_client = patch("tgmcp.client.DaemonClient").start()
        mock_client.return_value.__enter__.return_value.shutdown.side_effect = (
            ConnectionError("daemon hung mid-rpc")
        )
        try:
            with patch("tgmcp.cli.main.os.kill") as kill_mock:
                res = CliRunner().invoke(cli_main.cli, ["daemon", "stop"])
        finally:
            patch.stopall()

    assert res.exit_code == 0, res.output
    import signal as _sig

    sigterm_calls = [c for c in kill_mock.call_args_list if c.args[1] == _sig.SIGTERM]
    assert sigterm_calls == [], (
        "instance-bound RPC failure must NOT fall back to SIGTERM — could "
        "hit a successor daemon with a recycled pid"
    )
    assert "refusing" in res.output.lower() and "fallback" in res.output.lower()


def test_old_daemon_no_instance_id_falls_back_with_strict_recheck():
    """Backwards-compat: if /health does NOT publish instance_id (older
    daemon), fall back to signal-based stop with strict lock recheck."""
    my_pid = os.getpid()
    cli_main.PID_PATH.write_text(str(my_pid))
    cli_main.SOCKET_PATH.touch()

    locked_seq = iter([(True, my_pid), (False, None), (False, None)])

    with patch.object(
        cli_main,
        "_probe_existing_daemon",
        return_value={"ok": True, "pid": my_pid},  # NO instance_id
    ):
        with patch(
            "tgmcp.daemon.server.is_daemon_locked",
            side_effect=lambda: next(locked_seq, (False, None)),
        ):
            with patch("tgmcp.cli.main.os.kill") as kill_mock:
                with patch("tgmcp.cli.main.time.sleep"):
                    res = CliRunner().invoke(cli_main.cli, ["daemon", "stop"])

    assert res.exit_code == 0, res.output
    import signal as _sig

    sigterm_calls = [c for c in kill_mock.call_args_list if c.args[1] == _sig.SIGTERM]
    assert len(sigterm_calls) == 1, "backwards-compat path SIGTERMs once"
    assert sigterm_calls[0].args[0] == my_pid


def test_stop_treats_409_mismatch_as_terminal_no_sigterm():
    """Round-15 MAJOR-40: when /shutdown returns 409 instance_id mismatch,
    a DIFFERENT daemon now owns the socket. Falling back to SIGTERM with
    our originally-inspected pid could kill that daemon (its pid may even
    BE our captured pid due to recycling). Must hard-stop, never signal."""
    import httpx

    my_pid = os.getpid()
    cli_main.PID_PATH.write_text(str(my_pid))
    cli_main.SOCKET_PATH.touch()

    fake_response = httpx.Response(
        status_code=409,
        json={"error": "HTTPException", "detail": "instance_id mismatch"},
        request=httpx.Request("POST", "http://daemon/shutdown"),
    )

    with patch.object(
        cli_main,
        "_probe_existing_daemon",
        return_value={"ok": True, "pid": my_pid, "instance_id": "stale-id"},
    ):
        mock_client = patch("tgmcp.client.DaemonClient").start()
        mock_client.return_value.__enter__.return_value.shutdown.side_effect = (
            httpx.HTTPStatusError("409", request=fake_response.request, response=fake_response)
        )
        try:
            with patch("tgmcp.cli.main.os.kill") as kill_mock:
                res = CliRunner().invoke(cli_main.cli, ["daemon", "stop"])
        finally:
            patch.stopall()

    assert res.exit_code == 0, res.output
    import signal as _sig

    sigterm_calls = [c for c in kill_mock.call_args_list if c.args[1] == _sig.SIGTERM]
    assert sigterm_calls == [], (
        "409 mismatch must be terminal — never fall back to SIGTERM, which "
        "could kill the successor daemon"
    )
    assert "409" in res.output or "mismatch" in res.output.lower()
    assert "refusing" in res.output.lower() or "different daemon" in res.output.lower()


def test_stop_handles_rpc_success_when_pid_unknown_but_instance_id_present():
    """Round-18 MINOR-45: when /health gives instance_id but no pid (older
    schema variant we explicitly support), the post-RPC wait loop must NOT
    misclassify the still-running same-daemon lock holder as a 'different
    daemon' just because pid is None. Should just wait for lock release."""
    cli_main.SOCKET_PATH.touch()
    # No pid file written, no pid in /health response.

    locked_seq = iter([(True, 99999), (False, None), (False, None)])

    with patch.object(
        cli_main,
        "_probe_existing_daemon",
        return_value={"ok": True, "instance_id": "abc-no-pid"},  # NO pid field
    ):
        with patch(
            "tgmcp.daemon.server.is_daemon_locked",
            side_effect=lambda: next(locked_seq, (False, None)),
        ):
            mock_client = patch("tgmcp.client.DaemonClient").start()
            mock_client.return_value.__enter__.return_value.shutdown.return_value = {
                "ok": True,
                "pid": 99999,
                "instance_id": "abc-no-pid",
            }
            try:
                with patch("tgmcp.cli.main.os.kill") as kill_mock:
                    with patch("tgmcp.cli.main.time.sleep"):
                        res = CliRunner().invoke(cli_main.cli, ["daemon", "stop"])
            finally:
                patch.stopall()

    assert res.exit_code == 0, res.output
    # No SIGTERM (RPC succeeded).
    import signal as _sig

    sigterm_calls = [c for c in kill_mock.call_args_list if c.args[1] == _sig.SIGTERM]
    assert sigterm_calls == []
    # Critical: must NOT print "different daemon" — the lock holder was
    # never compared to a None pid.
    assert "different daemon" not in res.output.lower()
    # And it should have completed normally (graceful shutdown message).
    assert "graceful shutdown" in res.output.lower()


def test_stop_refuses_when_health_has_no_pid_and_no_instance_id_and_no_pid_file():
    """Round-17 MINOR-44: an older /health that publishes neither pid nor
    instance_id, AND no pid file on disk, leaves us with nothing actionable.
    Must refuse cleanly instead of crashing on the old `assert pid is not None`."""
    cli_main.SOCKET_PATH.touch()
    # Note: no PID_PATH written.

    with patch.object(
        cli_main, "_probe_existing_daemon", return_value={"ok": True}
    ):
        with patch("tgmcp.cli.main.os.kill") as kill_mock:
            res = CliRunner().invoke(cli_main.cli, ["daemon", "stop"])

    # No exception (the old `assert` would have raised).
    assert res.exit_code == 0, res.output
    assert res.exception is None, res.exception
    import signal as _sig

    sigterm_calls = [c for c in kill_mock.call_args_list if c.args[1] == _sig.SIGTERM]
    assert sigterm_calls == []
    assert "refusing" in res.output.lower() or "blind" in res.output.lower() or "manually" in res.output.lower()


def test_stop_post_rpc_messages_dont_print_none():
    """Round-21 MINOR-49: after a successful instance-bound /shutdown, the
    post-shutdown reinspection messages must not leak literal 'None' when
    pid is unknown for either the original or the successor daemon."""
    cli_main.SOCKET_PATH.touch()
    # No pid file. /health gives instance_id but no pid.

    # After RPC, lock stays held during the wait (timeout path), then a
    # successor daemon (also pid-unknown) is detected by final inspect_daemon().
    locked_seq = iter([(True, None)] * 60)  # never releases

    def fake_inspect_after_rpc():
        # Simulate the final.status RUNNING but pid=None case.
        return cli_main.DaemonInfo(cli_main.DaemonStatus.UNREACHABLE, None, None)

    with patch.object(
        cli_main,
        "_probe_existing_daemon",
        return_value={"ok": True, "instance_id": "abc-no-pid"},
    ):
        with patch(
            "tgmcp.daemon.server.is_daemon_locked",
            side_effect=lambda: next(locked_seq, (True, None)),
        ):
            mock_client = patch("tgmcp.client.DaemonClient").start()
            mock_client.return_value.__enter__.return_value.shutdown.return_value = {
                "ok": True,
                "pid": None,
                "instance_id": "abc-no-pid",
            }
            try:
                # Force the post-RPC re-inspect to land on UNREACHABLE/None.
                with patch.object(cli_main, "inspect_daemon") as inspect_mock:
                    inspect_mock.side_effect = [
                        cli_main.DaemonInfo(
                            cli_main.DaemonStatus.RUNNING,
                            None,
                            {"ok": True, "instance_id": "abc-no-pid"},
                        ),
                        # post-loop final re-inspect:
                        fake_inspect_after_rpc(),
                    ]
                    with patch("tgmcp.cli.main.os.kill"):
                        # Fast-forward time past the deadline.
                        t = [0.0]
                        with patch("tgmcp.cli.main.time.time", side_effect=lambda: t[0]):
                            def fake_sleep(_s):
                                t[0] += 11.0
                            with patch("tgmcp.cli.main.time.sleep", side_effect=fake_sleep):
                                res = CliRunner().invoke(cli_main.cli, ["daemon", "stop"])
            finally:
                patch.stopall()

    assert res.exit_code == 0, res.output
    assert "None" not in res.output, (
        f"stop output leaked literal 'None':\n{res.output}"
    )


def test_old_daemon_fallback_aborts_when_lock_holder_unreadable():
    """In the backwards-compat path (no instance_id), the lock recheck still
    gates SIGTERM: if holder pid is unreadable (race), abort."""
    my_pid = os.getpid()
    cli_main.PID_PATH.write_text(str(my_pid))
    cli_main.SOCKET_PATH.touch()

    with patch.object(
        cli_main,
        "_probe_existing_daemon",
        return_value={"ok": True, "pid": my_pid},  # no instance_id
    ):
        with patch("tgmcp.daemon.server.is_daemon_locked", return_value=(True, None)):
            with patch("tgmcp.cli.main.os.kill") as kill_mock:
                res = CliRunner().invoke(cli_main.cli, ["daemon", "stop"])

    assert res.exit_code == 0, res.output
    import signal as _sig

    sigterm_calls = [c for c in kill_mock.call_args_list if c.args[1] == _sig.SIGTERM]
    assert sigterm_calls == [], "must NOT SIGTERM when lock holder is unreadable"
    assert "unreadable" in res.output.lower() or "blind" in res.output.lower()
