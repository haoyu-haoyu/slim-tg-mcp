"""Regression tests: ensure Click commands are bound to the right callbacks.

Round 4 caught a BLOCKER where `@daemon.command("start")` decorated a helper
function instead of `daemon_start`, because the helper was inserted between
the decorator stack and the intended target. This test class invokes each
CLI command via Click's testing harness to verify the wiring.
"""

from __future__ import annotations

from click.testing import CliRunner

from tgmcp.cli.main import cli


def test_root_help_lists_three_groups():
    res = CliRunner().invoke(cli, ["--help"])
    assert res.exit_code == 0, res.output
    for cmd in ("init", "account", "daemon"):
        assert cmd in res.output


def test_daemon_start_help_shows_correct_options():
    """If decorators were misattached, the options below would not appear."""
    res = CliRunner().invoke(cli, ["daemon", "start", "--help"])
    assert res.exit_code == 0, res.output
    for opt in ("--account", "--foreground", "--passphrase", "--passphrase-stdin"):
        assert opt in res.output, f"missing option: {opt}\n{res.output}"


def test_daemon_status_help():
    res = CliRunner().invoke(cli, ["daemon", "status", "--help"])
    assert res.exit_code == 0
    assert "status" in res.output.lower() or "daemon" in res.output.lower()


def test_daemon_stop_help():
    res = CliRunner().invoke(cli, ["daemon", "stop", "--help"])
    assert res.exit_code == 0


def test_init_help_shows_passphrase_flags():
    res = CliRunner().invoke(cli, ["init", "--help"])
    assert res.exit_code == 0, res.output
    assert "--passphrase" in res.output
    assert "--passphrase-stdin" in res.output
    # Critical: --passphrase should be a flag, not a value option (no METAVAR).
    # If it were value-taking, the help would show "--passphrase TEXT".
    assert "--passphrase TEXT" not in res.output


def test_account_add_help():
    res = CliRunner().invoke(cli, ["account", "add", "--help"])
    assert res.exit_code == 0
    assert "--passphrase" in res.output


def test_account_list_help():
    res = CliRunner().invoke(cli, ["account", "list", "--help"])
    assert res.exit_code == 0
