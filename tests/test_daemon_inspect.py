"""Tests for inspect_daemon() — the single source of truth for daemon state.

Five distinct DaemonStatus values must be reachable; each command (start,
stop, status) branches on these so they need to be unambiguous.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from tgmcp.cli import main as cli_main
from tgmcp.cli.main import DaemonStatus


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(cli_main, "PID_PATH", tmp_path / "pid")
    monkeypatch.setattr(cli_main, "SOCKET_PATH", tmp_path / "sock")
    monkeypatch.setattr(cli_main, "LOG_PATH", tmp_path / "log")
    yield


def test_not_running_when_nothing_exists():
    info = cli_main.inspect_daemon()
    assert info.status == DaemonStatus.NOT_RUNNING
    assert info.pid is None


def test_running_when_health_pid_matches_pid_file():
    cli_main.PID_PATH.write_text("777")
    cli_main.SOCKET_PATH.touch()
    with patch.object(cli_main, "_probe_existing_daemon", return_value={"ok": True, "pid": 777}):
        info = cli_main.inspect_daemon()
    assert info.status == DaemonStatus.RUNNING
    assert info.pid == 777


def test_running_when_no_pid_file_but_socket_alive():
    cli_main.SOCKET_PATH.touch()
    with patch.object(cli_main, "_probe_existing_daemon", return_value={"ok": True, "pid": 555}):
        info = cli_main.inspect_daemon()
    assert info.status == DaemonStatus.RUNNING
    assert info.pid == 555


def test_foreign_owned_when_pids_disagree():
    cli_main.PID_PATH.write_text("100")
    cli_main.SOCKET_PATH.touch()
    with patch.object(cli_main, "_probe_existing_daemon", return_value={"ok": True, "pid": 999}):
        info = cli_main.inspect_daemon()
    assert info.status == DaemonStatus.FOREIGN_OWNED
    assert info.pid == 999


def test_unreachable_when_lock_held_but_no_health():
    """Lock is the authoritative liveness check. If a daemon holds the flock
    but /health doesn't respond (hung, mid-startup, foreground), inspect must
    return UNREACHABLE — never STALE — so callers refuse to clean up."""
    cli_main.PID_PATH.write_text("12345")
    cli_main.SOCKET_PATH.touch()
    with patch.object(cli_main, "_probe_existing_daemon", return_value=None):
        with patch(
            "tgmcp.daemon.server.is_daemon_locked",
            return_value=(True, 12345),
        ):
            info = cli_main.inspect_daemon()
    assert info.status == DaemonStatus.UNREACHABLE
    assert info.pid == 12345


def test_unreachable_in_foreground_mode_no_pid_file():
    """The round-8 BLOCKER scenario: foreground daemon never writes a pid
    file. Without the lock-based check, inspect would mis-classify as STALE
    and `daemon stop` would unlink the live socket."""
    cli_main.SOCKET_PATH.touch()  # daemon is running, socket exists, no pid file
    with patch.object(cli_main, "_probe_existing_daemon", return_value=None):
        with patch(
            "tgmcp.daemon.server.is_daemon_locked",
            return_value=(True, 67890),
        ):
            info = cli_main.inspect_daemon()
    assert info.status == DaemonStatus.UNREACHABLE, (
        "foreground daemon must NOT be classified as STALE — that would orphan it"
    )
    assert info.pid == 67890


def test_unreachable_during_detached_startup_window():
    """Between Popen and parent writing pid file, the daemon is alive (holds
    lock) but pid file may be missing. Must be UNREACHABLE."""
    # No pid file, no socket yet (very early), but lock IS held.
    with patch.object(cli_main, "_probe_existing_daemon", return_value=None):
        with patch(
            "tgmcp.daemon.server.is_daemon_locked",
            return_value=(True, 11111),
        ):
            info = cli_main.inspect_daemon()
    assert info.status == DaemonStatus.UNREACHABLE


def test_stale_when_files_exist_but_lock_free():
    """Lock is free → daemon is genuinely dead. Files are leftovers."""
    cli_main.PID_PATH.write_text("999999")
    cli_main.SOCKET_PATH.touch()
    with patch.object(cli_main, "_probe_existing_daemon", return_value=None):
        with patch(
            "tgmcp.daemon.server.is_daemon_locked",
            return_value=(False, None),
        ):
            info = cli_main.inspect_daemon()
    assert info.status == DaemonStatus.STALE


def test_stale_when_only_socket_exists_and_lock_free():
    cli_main.SOCKET_PATH.touch()
    with patch.object(cli_main, "_probe_existing_daemon", return_value=None):
        with patch(
            "tgmcp.daemon.server.is_daemon_locked",
            return_value=(False, None),
        ):
            info = cli_main.inspect_daemon()
    assert info.status == DaemonStatus.STALE


def test_stale_when_recycled_pid_alive_but_lock_free():
    """pid file points at a live process but the lock is free → that pid is
    NOT our daemon. Must be STALE (cleanup safe), not UNREACHABLE."""
    cli_main.PID_PATH.write_text(str(os.getpid()))  # alive
    cli_main.SOCKET_PATH.touch()
    with patch.object(cli_main, "_probe_existing_daemon", return_value=None):
        with patch(
            "tgmcp.daemon.server.is_daemon_locked",
            return_value=(False, None),
        ):
            info = cli_main.inspect_daemon()
    assert info.status == DaemonStatus.STALE


def test_pid_alive_handles_permission_error():
    """If kill(pid, 0) raises PermissionError, the pid still exists (just owned
    by another user). We should treat that as alive — never as dead — to avoid
    falsely declaring the pid free."""
    with patch("os.kill", side_effect=PermissionError):
        assert cli_main._pid_alive(123) is True


def test_pid_alive_handles_process_lookup_error():
    with patch("os.kill", side_effect=ProcessLookupError):
        assert cli_main._pid_alive(123) is False


def test_unreachable_with_unknown_holder_pid_does_not_print_none():
    """Round-20 MINOR-47: when a daemon holds the lock but the lock-file
    read races empty/unreadable, inspect_daemon returns UNREACHABLE with
    pid=None. The status/start/stop output messages MUST NOT print
    'kill None' or 'ps -p None' to the operator."""
    from click.testing import CliRunner

    cli_main.SOCKET_PATH.touch()

    with patch.object(cli_main, "_probe_existing_daemon", return_value=None):
        with patch(
            "tgmcp.daemon.server.is_daemon_locked",
            return_value=(True, None),  # lock held, holder unknown
        ):
            for cmd in (["daemon", "status"], ["daemon", "start"], ["daemon", "stop"]):
                res = CliRunner().invoke(cli_main.cli, cmd)
                assert "None" not in res.output, (
                    f"`{' '.join(cmd)}` leaked literal 'None' in output:\n{res.output}"
                )
                # Bonus: must mention pid is unknown.
                assert "unknown" in res.output.lower() or "investigate" in res.output.lower()


def test_running_with_stale_pid_file_returns_none_pid_when_health_omits_pid():
    """Round-19 MINOR-46: if /health omits pid, do NOT fall back to a
    possibly-stale pid file. inspect_daemon must report pid as unknown so
    callers (especially daemon_stop's instance-bound path) don't get
    misled into 'different daemon' false positives."""
    cli_main.PID_PATH.write_text("999999")  # stale
    cli_main.SOCKET_PATH.touch()

    with patch.object(
        cli_main,
        "_probe_existing_daemon",
        return_value={"ok": True, "instance_id": "abc"},  # NO pid
    ):
        info = cli_main.inspect_daemon()

    assert info.status == DaemonStatus.RUNNING
    assert info.pid is None, (
        "must NOT mix in stale pid_file when /health didn't authoritatively "
        "report a pid"
    )


def test_unreadable_pid_file_returns_none():
    cli_main.PID_PATH.write_text("not-a-number")
    assert cli_main._read_pid_file() is None
