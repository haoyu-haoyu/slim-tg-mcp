"""Verify `daemon stop` refuses to SIGTERM blindly when the recorded pid
doesn't match what the running daemon reports — the round-6 MAJOR scenario.

Background: after a daemon crash, the kernel may reassign the recorded pid
to an unrelated same-user process. Blind SIGTERM = footgun.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from tgmcp.cli import main as cli_main


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(cli_main, "PID_PATH", tmp_path / "pid")
    monkeypatch.setattr(cli_main, "SOCKET_PATH", tmp_path / "sock")
    monkeypatch.setattr(cli_main, "LOG_PATH", tmp_path / "log")
    yield


def test_stop_with_no_pid_file_says_not_running():
    res = CliRunner().invoke(cli_main.cli, ["daemon", "stop"])
    assert res.exit_code == 0
    assert "not running" in res.output.lower()


def test_stop_with_unreadable_pid_cleans_up():
    cli_main.PID_PATH.write_text("not-a-number")
    # No socket: this is STALE territory.
    with patch.object(cli_main, "_probe_existing_daemon", return_value=None):
        res = CliRunner().invoke(cli_main.cli, ["daemon", "stop"])
    assert res.exit_code == 0
    assert "stale" in res.output.lower() or "not running" in res.output.lower()
    assert not cli_main.PID_PATH.exists()


def test_stop_when_socket_dead_and_pid_dead_cleans_artifacts():
    """STALE: recorded pid is not alive, socket exists but doesn't respond.
    Auto-clean is safe."""
    cli_main.PID_PATH.write_text("999999")
    cli_main.SOCKET_PATH.touch()

    def fake_kill(pid, sig):
        # Liveness check (sig=0) on a non-existent pid should report dead.
        if sig == 0:
            raise ProcessLookupError(f"no such process {pid}")
        # Anything else is recorded but silenced (we don't actually want to
        # signal pid 999999, which might be unrelated).

    with patch.object(cli_main, "_probe_existing_daemon", return_value=None):
        with patch("tgmcp.cli.main.os.kill", side_effect=fake_kill) as kill_mock:
            res = CliRunner().invoke(cli_main.cli, ["daemon", "stop"])

    assert res.exit_code == 0, res.output
    import signal as _sig

    assert all(c.args[1] != _sig.SIGTERM for c in kill_mock.call_args_list), \
        "must not SIGTERM a dead/recycled pid"
    assert not cli_main.PID_PATH.exists()
    assert not cli_main.SOCKET_PATH.exists()


def test_stop_when_lock_held_but_unreachable_refuses():
    """UNREACHABLE (lock held + /health silent): could be a hung daemon, or
    foreground startup, or a daemon mid-init. Must refuse to clean/signal —
    a wrong move would orphan a live daemon."""
    cli_main.PID_PATH.write_text("12345")
    cli_main.SOCKET_PATH.touch()

    with patch.object(cli_main, "_probe_existing_daemon", return_value=None):
        with patch(
            "tgmcp.daemon.server.is_daemon_locked",
            return_value=(True, 12345),
        ):
            with patch("tgmcp.cli.main.os.kill") as kill_mock:
                res = CliRunner().invoke(cli_main.cli, ["daemon", "stop"])

    assert res.exit_code == 0, res.output
    import signal as _sig

    assert all(c.args[1] != _sig.SIGTERM for c in kill_mock.call_args_list), \
        "UNREACHABLE must never SIGTERM — could orphan a live daemon"
    assert "refusing" in res.output.lower() or "manually" in res.output.lower()
    # Files must NOT be removed — operator must investigate.
    assert cli_main.PID_PATH.exists()
    assert cli_main.SOCKET_PATH.exists()


def test_stop_refuses_when_pid_mismatch():
    """pid file says 100 but the live daemon is at pid 999 → refuse, do not signal."""
    cli_main.PID_PATH.write_text("100")
    cli_main.SOCKET_PATH.touch()

    with patch.object(cli_main, "_probe_existing_daemon", return_value={"ok": True, "pid": 999}):
        with patch("os.kill") as kill_mock:
            res = CliRunner().invoke(cli_main.cli, ["daemon", "stop"])

    assert res.exit_code == 0
    kill_mock.assert_not_called(), "pid mismatch must NOT trigger a kill"
    assert "refusing" in res.output.lower()
    # pid file should NOT be removed — operator should investigate.
    assert cli_main.PID_PATH.exists()


def test_stop_signals_on_pid_match():
    """Happy path: pid file matches the responding daemon → SIGTERM that pid.

    After SIGTERM, /health should stop responding and the lock should
    release; only then does cleanup run.
    """
    my_pid = os.getpid()
    cli_main.PID_PATH.write_text(str(my_pid))
    cli_main.SOCKET_PATH.touch()

    # First probe (start of stop): RUNNING. After SIGTERM: None (daemon exited).
    probe_seq = iter([{"ok": True, "pid": my_pid}, None, None, None])

    def fake_probe():
        return next(probe_seq, None)

    # Lock recheck (pre-SIGTERM) sees us holding it; post-SIGTERM wait sees free.
    locked_seq = iter([(True, my_pid), (False, None), (False, None)])

    with patch.object(cli_main, "_probe_existing_daemon", side_effect=fake_probe):
        with patch(
            "tgmcp.daemon.server.is_daemon_locked",
            side_effect=lambda: next(locked_seq, (False, None)),
        ):
            with patch("tgmcp.cli.main.os.kill") as kill_mock:
                with patch("tgmcp.cli.main.time.sleep"):
                    res = CliRunner().invoke(cli_main.cli, ["daemon", "stop"])

    assert res.exit_code == 0, res.output
    # Must send SIGTERM exactly once (extra calls would be liveness probes,
    # not signals, so we filter on signal value).
    import signal

    sigterm_calls = [c for c in kill_mock.call_args_list if c.args[1] == signal.SIGTERM]
    assert len(sigterm_calls) == 1
    assert sigterm_calls[0].args[0] == my_pid
    assert not cli_main.PID_PATH.exists()
