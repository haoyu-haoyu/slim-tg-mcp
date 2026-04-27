"""tgmcp CLI: account management + daemon lifecycle.

Commands:
    tgmcp init                # interactive first-time login → encrypted session
    tgmcp account list
    tgmcp account add <label>
    tgmcp account remove <label>
    tgmcp daemon start [--account=<label>] [--foreground]
    tgmcp daemon status
    tgmcp daemon stop
"""

from __future__ import annotations

import enum
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import click
from rich.console import Console

from ..daemon import auth
from ..daemon.paths import LOCK_PATH as _PATHS_LOCK_PATH
from ..daemon.paths import LOG_PATH as _PATHS_LOG_PATH
from ..daemon.paths import PID_PATH as _PATHS_PID_PATH
from ..daemon.paths import SOCKET_PATH as _PATHS_SOCKET_PATH

console = Console()

# Re-export under the names the rest of this module already uses. Tests
# monkey-patch these attributes on cli_main, so keep them as module-level names.
PID_PATH: Path = _PATHS_PID_PATH
SOCKET_PATH: Path = _PATHS_SOCKET_PATH
LOCK_PATH: Path = _PATHS_LOCK_PATH


# ---------- daemon lifecycle inspection ----------


class DaemonStatus(enum.Enum):
    NOT_RUNNING = "not_running"          # no pid file, no socket
    RUNNING = "running"                  # /health responds and its pid matches our pid file (or no pid file)
    STALE = "stale"                      # pid/socket left behind, no live process
    UNREACHABLE = "unreachable"          # recorded pid is alive but /health doesn't respond
    FOREIGN_OWNED = "foreign_owned"      # socket served by a daemon whose pid != our pid file


@dataclass
class DaemonInfo:
    status: DaemonStatus
    pid: Optional[int]
    health: Optional[dict]


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness check that doesn't require ownership.

    PermissionError means the pid exists but belongs to another user — we
    treat that as "alive" because the kernel hasn't reaped it yet.
    """
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _read_pid_file() -> Optional[int]:
    if not PID_PATH.exists():
        return None
    try:
        return int(PID_PATH.read_text().strip())
    except (ValueError, OSError):
        return None


def inspect_daemon() -> DaemonInfo:
    """Single source of truth for "is the daemon running, and where?".

      - RUNNING: socket alive AND health.pid matches pid file (or pid file absent)
      - FOREIGN_OWNED: socket alive but health.pid disagrees with pid file
      - UNREACHABLE: lock is held by a live daemon, but /health doesn't respond
                     (could be: hung, just-spawned not-yet-ready, foreground
                     mode that hasn't written pid file)
      - STALE: pid/socket files exist but no live daemon (lock free)
      - NOT_RUNNING: nothing exists

    The flock is the *authoritative* liveness check: the kernel auto-releases
    the lock when the holding process dies, so a held lock means the daemon
    process is genuinely alive — independent of pid files (which can be
    missing in foreground mode or during the startup window) and independent
    of pid liveness (which can be a recycled foreign pid).
    """
    from ..daemon.server import is_daemon_locked

    pid_in_file = _read_pid_file()
    health = _probe_existing_daemon()

    if health is not None:
        socket_pid = health.get("pid")
        if socket_pid is None:
            # /health didn't tell us the daemon's pid (older schema). Do NOT
            # substitute pid_in_file — it could be stale (e.g. left over from
            # a previous daemon that crashed without cleanup) and would
            # mislead callers like daemon_stop into a false "different
            # daemon" abort. Treat the pid as genuinely unknown.
            return DaemonInfo(DaemonStatus.RUNNING, None, health)
        if pid_in_file is None or socket_pid == pid_in_file:
            return DaemonInfo(DaemonStatus.RUNNING, socket_pid, health)
        return DaemonInfo(DaemonStatus.FOREIGN_OWNED, socket_pid, health)

    # /health did not respond. Use the flock to disambiguate live-but-silent
    # from genuinely-dead.
    locked, lock_pid = is_daemon_locked()
    if locked:
        # A daemon process IS alive (holds the flock) but isn't answering. Could
        # be hung, mid-startup, or foreground mode pre-pid-file. Refuse to
        # touch any artifacts; cleanup would orphan or break it.
        return DaemonInfo(DaemonStatus.UNREACHABLE, lock_pid or pid_in_file, None)

    # Lock is free → no daemon process. Whatever pid/socket files exist are
    # leftovers from a previous run.
    if PID_PATH.exists() or SOCKET_PATH.exists():
        return DaemonInfo(DaemonStatus.STALE, pid_in_file, None)

    return DaemonInfo(DaemonStatus.NOT_RUNNING, None, None)


def _unlink_artifacts() -> None:
    PID_PATH.unlink(missing_ok=True)
    if SOCKET_PATH.exists():
        try:
            SOCKET_PATH.unlink()
        except OSError:
            pass


def _clean_stale_artifacts() -> None:
    """Race-free cleanup: hold the daemon flock during unlink.

    Without this, `inspect_daemon()` could observe lock-free, then a new
    daemon could acquire the lock and create a fresh socket, and our
    subsequent unlink would delete the new daemon's live artifacts. Holding
    the same flock during cleanup serializes us with the daemon's
    `_acquire_singleton_lock()` so the unlink is atomic w.r.t. start.

    If we can't take the lock (a daemon is alive), abort silently — those
    artifacts aren't ours to remove.
    """
    if sys.platform == "win32":
        # No flock primitive; we already declare POSIX-only at startup, so
        # this path shouldn't execute, but be conservative.
        _unlink_artifacts()
        return

    try:
        import fcntl  # POSIX-only
    except ImportError:
        _unlink_artifacts()
        return

    import errno

    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        # Can't even open the lock file — skip cleanup rather than racing.
        return

    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                # A daemon grabbed the lock between our state inspection and
                # this call. Its artifacts are now live — leave them alone.
                console.print(
                    "[yellow]Skipping cleanup: a daemon is now holding the lock.[/yellow]"
                )
                return
            # Real fault: leave artifacts and surface the error.
            console.print(f"[red]Cleanup aborted: flock failed: {e!r}[/red]")
            return
        try:
            _unlink_artifacts()
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


@click.group()
def cli() -> None:
    """slim-tg-mcp control CLI."""


# ---------- init ----------


def _collect_passphrase(
    use_passphrase: bool, passphrase_stdin: bool, *, confirm: bool = True
) -> Optional[str]:
    """Get a passphrase without putting it on argv or in shell history.

    --passphrase is a flag that triggers a hidden interactive prompt.
    --passphrase-stdin reads a single line from stdin (for automation).
    Neither places the secret on the command line.
    """
    if passphrase_stdin:
        return sys.stdin.readline().rstrip("\n")
    if use_passphrase:
        return click.prompt(
            "Encryption passphrase",
            hide_input=True,
            confirmation_prompt=confirm,
        )
    return None


@cli.command()
@click.option("--label", default="main", help="Account label (default: main)")
@click.option(
    "--passphrase",
    "use_passphrase",
    is_flag=True,
    help="Encrypt with a passphrase (hidden prompt) instead of OS keychain",
)
@click.option(
    "--passphrase-stdin",
    is_flag=True,
    help="Read passphrase from stdin (for automation)",
)
def init(label: str, use_passphrase: bool, passphrase_stdin: bool) -> None:
    """Interactive first-time Telegram login. Stores an encrypted session."""
    api_id_str = os.environ.get("TG_API_ID") or click.prompt(
        "Telegram api_id (from https://my.telegram.org)", type=str
    )
    api_hash = os.environ.get("TG_API_HASH") or click.prompt(
        "Telegram api_hash", type=str, hide_input=True
    )
    try:
        api_id = int(api_id_str)
    except ValueError:
        console.print("[red]api_id must be an integer[/red]")
        sys.exit(1)

    pass_value = _collect_passphrase(use_passphrase, passphrase_stdin)

    # Run the interactive Telethon login synchronously (it prompts the user).
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    client = TelegramClient(StringSession(), api_id, api_hash)
    with client:
        console.print("[green]Logged in.[/green]")
        session_string = client.session.save()

    try:
        auth.save_session(label, session_string, passphrase=pass_value)
    except auth.KeychainUnavailable as e:
        console.print(f"[yellow]Keychain unavailable: {e}[/yellow]")
        console.print("[yellow]Falling back to passphrase encryption.[/yellow]")
        pass_value = click.prompt(
            "Encryption passphrase", hide_input=True, confirmation_prompt=True
        )
        auth.save_session(label, session_string, passphrase=pass_value)

    console.print(f"[green]Session encrypted and saved as label={label!r}.[/green]")
    if pass_value is not None:
        console.print(
            "[yellow]Remember: you'll need this passphrase every time the daemon "
            "starts. Use `tgmcp daemon start --passphrase` (interactive) or "
            "`--passphrase-stdin` (automation).[/yellow]"
        )
    console.print(
        f"[dim]Add to your shell:\n"
        f"  export TG_API_ID={api_id}\n"
        f"  export TG_API_HASH={api_hash}\n"
        f"  export TGMCP_ACCOUNT={label}[/dim]"
    )


# ---------- account ----------


@cli.group()
def account() -> None:
    """Manage saved Telegram accounts."""


@account.command("list")
def account_list() -> None:
    accounts = auth.list_accounts()
    if not accounts:
        console.print("[yellow]No accounts. Run `tgmcp init` first.[/yellow]")
        return
    for a in accounts:
        console.print(f"  - {a}")


@account.command("add")
@click.argument("label")
@click.option("--passphrase", "use_passphrase", is_flag=True)
@click.option("--passphrase-stdin", is_flag=True)
def account_add(label: str, use_passphrase: bool, passphrase_stdin: bool) -> None:
    ctx = click.get_current_context()
    ctx.invoke(
        init,
        label=label,
        use_passphrase=use_passphrase,
        passphrase_stdin=passphrase_stdin,
    )


@account.command("remove")
@click.argument("label")
def account_remove(label: str) -> None:
    if auth.delete_account(label):
        console.print(f"[green]Removed {label}[/green]")
    else:
        console.print(f"[yellow]No such account: {label}[/yellow]")


# ---------- daemon ----------


@cli.group()
def daemon() -> None:
    """Control the long-running Telegram daemon."""


LOG_PATH: Path = _PATHS_LOG_PATH


def _probe_existing_daemon() -> Optional[dict]:
    """If the socket is already serving, return its /health payload. Otherwise None.

    Used to detect a still-running daemon whose pid file is missing or stale —
    if we tried to spawn over it, the new child would fight the old one for
    the socket.
    """
    if not SOCKET_PATH.exists():
        return None
    try:
        from ..client import DaemonClient

        with DaemonClient(timeout=1.0) as c:
            payload = c.health()
            if payload.get("ok"):
                return payload
    except Exception:
        pass
    return None


@daemon.command("start")
@click.option("--account", "label", default="main")
@click.option("--foreground", is_flag=True)
@click.option(
    "--passphrase",
    is_flag=True,
    help="Prompt for the session passphrase (required for accounts encrypted "
    "with --passphrase at init time).",
)
@click.option(
    "--passphrase-stdin",
    is_flag=True,
    help="Read passphrase from stdin (one line). Useful for automation.",
)
def daemon_start(label: str, foreground: bool, passphrase: bool, passphrase_stdin: bool) -> None:
    info = inspect_daemon()

    if info.status == DaemonStatus.RUNNING:
        if info.pid is not None:
            console.print(f"[yellow]Daemon already running (pid={info.pid})[/yellow]")
            PID_PATH.parent.mkdir(parents=True, exist_ok=True)
            PID_PATH.write_text(str(info.pid))
        else:
            console.print(
                "[yellow]Daemon already running (pid unknown — older /health "
                "schema). Refusing to spawn a duplicate.[/yellow]"
            )
        return

    if info.status == DaemonStatus.FOREIGN_OWNED:
        if info.pid is not None:
            console.print(
                f"[red]Another daemon owns {SOCKET_PATH} (pid={info.pid}). "
                f"Refusing to spawn a second one.\n"
                f"Investigate manually: ps -p {info.pid}[/red]"
            )
        else:
            console.print(
                f"[red]Another daemon owns {SOCKET_PATH} (pid unknown). "
                "Refusing to spawn a second one.[/red]"
            )
        return

    if info.status == DaemonStatus.UNREACHABLE:
        if info.pid is not None:
            console.print(
                f"[red]Recorded daemon (pid={info.pid}) is alive but not responding "
                f"on {SOCKET_PATH}. Refusing to spawn over it.\n"
                f"If it's hung, stop it manually: kill {info.pid}[/red]"
            )
        else:
            console.print(
                f"[red]A daemon is holding the lock but its pid is unknown "
                f"(racing with startup or lock-file write). Refusing to spawn "
                f"over it. Try again or inspect {LOCK_PATH} manually.[/red]"
            )
        return

    if info.status == DaemonStatus.STALE:
        console.print("[dim]Cleaning stale pid/socket left by a previous run...[/dim]")
        _clean_stale_artifacts()

    pass_value: Optional[str] = _collect_passphrase(passphrase, passphrase_stdin, confirm=False)

    env = os.environ.copy()
    env["TGMCP_ACCOUNT"] = label
    # Note: we deliberately do NOT put the passphrase in env. Same-user
    # processes can read /proc/<pid>/environ; on Linux that file reflects the
    # exec-time env even after os.environ.pop. Pass via FD instead.

    if foreground:
        os.environ["TGMCP_ACCOUNT"] = label
        from ..daemon import server as daemon_server

        daemon_server.set_passphrase_override(pass_value)
        daemon_server.main()
        return

    # Detach. Set up resources inside try/finally so an early Popen failure
    # never leaks the pipe FDs or the log file handle.
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    pass_fds: tuple[int, ...] = ()
    log_fp = None
    proc: Optional[subprocess.Popen] = None
    try:
        if pass_value is not None:
            r, w = os.pipe()
            try:
                os.write(w, pass_value.encode("utf-8"))
            finally:
                os.close(w)
            env["TGMCP_PASSPHRASE_FD"] = str(r)
            pass_fds = (r,)

        log_fp = LOG_PATH.open("a", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, "-m", "tgmcp.daemon.server"],
            env=env,
            stdout=log_fp,
            stderr=log_fp,
            pass_fds=pass_fds,
            start_new_session=True,
        )
    except Exception:
        # Popen failed — close everything we created. Re-raise after cleanup.
        if log_fp is not None:
            log_fp.close()
        for fd in pass_fds:
            try:
                os.close(fd)
            except OSError:
                pass
        raise
    finally:
        # On the success path, the child inherited duplicates of pass_fds and
        # log_fp; the parent no longer needs them.
        if proc is not None:
            for fd in pass_fds:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if log_fp is not None:
                log_fp.close()

    # Wait for the daemon to become healthy. Verify the responder is OUR child
    # (matching pid), not a pre-existing daemon that snuck in.
    deadline = time.time() + 10.0
    while time.time() < deadline:
        if proc.poll() is not None:
            console.print(
                f"[red]Daemon died on startup (exit code {proc.returncode}). "
                f"See log: {LOG_PATH}[/red]"
            )
            return
        if SOCKET_PATH.exists():
            try:
                from ..client import DaemonClient

                with DaemonClient(timeout=2.0) as c:
                    payload = c.health()
                if payload.get("ok") and payload.get("pid") == proc.pid:
                    PID_PATH.parent.mkdir(parents=True, exist_ok=True)
                    PID_PATH.write_text(str(proc.pid))
                    console.print(
                        f"[green]Daemon started (pid={proc.pid}, account={label})[/green]"
                    )
                    return
                if payload.get("pid") not in (None, proc.pid):
                    console.print(
                        f"[red]Socket is owned by another daemon (pid={payload.get('pid')}). "
                        f"Killing our orphan child {proc.pid}.[/red]"
                    )
                    try:
                        proc.terminate()
                    except ProcessLookupError:
                        pass
                    return
            except Exception:
                pass
        time.sleep(0.2)

    console.print(
        f"[red]Daemon did not become healthy within 10s. "
        f"Check {LOG_PATH}. Killing pid={proc.pid}.[/red]"
    )
    try:
        proc.terminate()
    except ProcessLookupError:
        pass


@daemon.command("status")
def daemon_status() -> None:
    info = inspect_daemon()
    if info.status == DaemonStatus.NOT_RUNNING:
        console.print("[yellow]Daemon not running[/yellow]")
        return
    if info.status == DaemonStatus.RUNNING:
        pid_str = info.pid if info.pid is not None else "unknown"
        console.print(
            f"[green]Daemon running (pid={pid_str}, "
            f"account={info.health.get('account') if info.health else 'unknown'}), "
            f"socket={SOCKET_PATH}[/green]"
        )
        return
    if info.status == DaemonStatus.UNREACHABLE:
        if info.pid is not None:
            console.print(
                f"[red]Recorded daemon (pid={info.pid}) is alive but not responding. "
                f"Likely hung — check {LOG_PATH}.[/red]"
            )
        else:
            console.print(
                f"[red]A daemon is holding the lock but its pid is unknown. "
                f"Likely hung or mid-startup — check {LOG_PATH}.[/red]"
            )
        return
    if info.status == DaemonStatus.FOREIGN_OWNED:
        if info.pid is not None:
            console.print(
                f"[red]Socket owned by foreign daemon (pid={info.pid}). "
                f"Inspect: ps -p {info.pid}[/red]"
            )
        else:
            console.print(
                "[red]Socket owned by an unidentified daemon (pid unknown). "
                "Inspect manually.[/red]"
            )
        return
    # STALE
    console.print("[yellow]Stale pid/socket files; daemon is not running.[/yellow]")


@daemon.command("stop")
def daemon_stop() -> None:
    info = inspect_daemon()

    if info.status == DaemonStatus.NOT_RUNNING:
        console.print("[yellow]Daemon not running[/yellow]")
        return

    if info.status == DaemonStatus.STALE:
        console.print("[yellow]Stale pid/socket; cleaning up without signaling.[/yellow]")
        _clean_stale_artifacts()
        return

    if info.status == DaemonStatus.UNREACHABLE:
        # A daemon is alive (holds the flock) but /health doesn't respond. We
        # don't know whether it's a hung daemon or an unrelated process.
        # Refuse to act blindly.
        if info.pid is not None:
            console.print(
                f"[red]Recorded pid={info.pid} is alive but socket /health does not "
                f"respond. Refusing to SIGTERM — could be a hung daemon OR an unrelated "
                f"process that inherited the recycled pid.\n"
                f"Investigate manually:\n"
                f"  ps -p {info.pid}\n"
                f"If it's our daemon, kill it yourself:\n"
                f"  kill {info.pid} && rm {PID_PATH} {SOCKET_PATH}[/red]"
            )
        else:
            console.print(
                f"[red]A daemon is holding the lock but its pid is unknown "
                f"(read race or empty lock file). Refusing to act.\n"
                f"Investigate manually: cat {LOCK_PATH}[/red]"
            )
        return

    if info.status == DaemonStatus.FOREIGN_OWNED:
        if info.pid is not None:
            console.print(
                f"[red]Socket owned by foreign daemon (pid={info.pid}). "
                f"Refusing to signal. Inspect: ps -p {info.pid}[/red]"
            )
        else:
            console.print(
                "[red]Socket owned by a foreign daemon (pid unknown). "
                "Refusing to signal.[/red]"
            )
        return

    # RUNNING: prefer a daemon-side shutdown RPC over signaling a pid.
    # Pid-based shutdown has an unavoidable race: the daemon can exit and
    # the kernel can recycle the pid between our recheck and os.kill, so
    # we'd signal an unrelated process. The /shutdown endpoint asks the
    # daemon to stop itself; the request flows through the same Unix
    # socket that authenticated us as same-user.
    #
    # `pid` may be None when /health responded but did not include a pid
    # (very old daemon). That's fine for the instance-bound RPC path
    # because /shutdown doesn't need pid; we only require it for the
    # signal-based fallback.
    pid = info.pid

    from ..client import DaemonClient
    from ..daemon.server import is_daemon_locked

    import httpx

    rpc_succeeded = False
    rpc_terminal = False  # any non-success outcome of an instance-bound RPC
    # Bind the RPC to the SPECIFIC daemon instance we just inspected. If a
    # successor daemon has taken over the socket between inspect_daemon()
    # and this RPC, it will reject the request with 409 — preventing
    # collateral shutdown of an unrelated daemon.
    instance_id = (info.health or {}).get("instance_id")

    if not instance_id and pid is None:
        # No actionable info: can't bind RPC, can't signal a pid. Refuse
        # rather than guessing.
        console.print(
            "[red]Daemon /health gave us neither pid nor instance_id; "
            "refusing to act blind. Inspect with `tgmcp daemon status`.[/red]"
        )
        return

    if instance_id:
        try:
            with DaemonClient(timeout=3.0) as c:
                res = c.shutdown(instance_id=instance_id)
            if res.get("ok"):
                pid_part = (
                    f"pid={res['pid']}, " if res.get("pid") is not None else ""
                )
                console.print(
                    f"[green]Requested graceful shutdown "
                    f"({pid_part}instance={res.get('instance_id', '')[:8]}…)[/green]"
                )
                rpc_succeeded = True
            else:
                console.print(
                    f"[red]Shutdown RPC returned unexpected response: {res}. "
                    "Refusing pid-based fallback — instance binding failed.[/red]"
                )
                rpc_terminal = True
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 409:
                console.print(
                    "[red]Shutdown refused by daemon: instance_id mismatch "
                    "(409). A different daemon now owns the socket. "
                    "Refusing all fallbacks — run `tgmcp daemon status` to "
                    "see who is in charge.[/red]"
                )
            else:
                # Any other HTTP error from a daemon that DID give us an
                # instance_id is also terminal: we cannot prove the daemon
                # we want to stop still exists, so SIGTERM-with-captured-pid
                # would risk killing a successor that recycled the same pid.
                console.print(
                    f"[red]Instance-bound shutdown RPC failed with HTTP "
                    f"{e.response.status_code}: {e}. Refusing pid-based "
                    "fallback — investigate manually.[/red]"
                )
            rpc_terminal = True
        except Exception as e:
            # Transport / timeout / read error AFTER we committed to the
            # instance-bound path. Same reasoning: the daemon may have died
            # and been replaced; pid-based SIGTERM is no longer safe.
            console.print(
                f"[red]Instance-bound shutdown failed ({type(e).__name__}: {e}). "
                "Refusing pid-based fallback — daemon may have been replaced. "
                "Run `tgmcp daemon status` and stop manually if needed.[/red]"
            )
            rpc_terminal = True

        if rpc_terminal:
            return  # Hard stop on any instance-bound failure mode.
    else:
        # Backwards-compat path: an older daemon that doesn't publish
        # instance_id. We accept the small residual risk of pid recycling
        # since we have no better signal — but we still apply the strict
        # lock-holder recheck below.
        console.print(
            "[yellow]Daemon /health did not return instance_id (older daemon?). "
            "Falling back to signal-based stop with strict re-check.[/yellow]"
        )

    if not rpc_succeeded:
        if pid is None:
            # /health gave us no pid and we never tried (or already
            # terminally failed) instance-bound RPC. Nothing safe to do.
            console.print(
                "[red]No pid available for signal fallback (older /health "
                "schema). Stop the daemon manually.[/red]"
            )
            return
        # Fallback: SIGTERM, but only after the strictest possible re-check.
        # Refuse if (a) lock is free → daemon already exited (recycled pid risk),
        # (b) holder is unknown (lock file race), or (c) holder differs.
        locked, holder = is_daemon_locked()
        if not locked:
            console.print(
                f"[yellow]Lock is free — daemon (pid={pid}) already exited. "
                "Cleaning artifacts.[/yellow]"
            )
            _clean_stale_artifacts()
            return
        if holder is None:
            console.print(
                "[red]Lock is held but holder pid unreadable (race). "
                "Refusing to send SIGTERM blind. Try again or stop manually.[/red]"
            )
            return
        if holder != pid:
            console.print(
                f"[red]Lock holder changed from {pid} to {holder} between probe and "
                f"signal. Refusing to SIGTERM — a different daemon now owns the "
                "lock.[/red]"
            )
            return
        try:
            os.kill(pid, signal.SIGTERM)
            console.print(f"[green]Sent SIGTERM to {pid} (RPC fallback)[/green]")
        except ProcessLookupError:
            console.print("[yellow]Process already gone[/yellow]")

    # Wait for the daemon to actually exit before unlinking artifacts. The
    # flock auto-releases when the process exits, so a free lock is the
    # cleanest exit signal. We must not clean while the lock is still held —
    # that would yank the socket out from under a slow-shutdown daemon, or
    # worse, from a freshly-restarted replacement that took our place.
    from ..daemon.server import is_daemon_locked

    deadline = time.time() + 10.0
    while time.time() < deadline:
        locked, holder = is_daemon_locked()
        if not locked:
            break
        # Lock is held. If we know our target pid, detect a fast-replacement
        # daemon (different holder) and abort cleanup. If pid is unknown
        # (older /health schema), we can't make that distinction here — just
        # wait for the lock to release and rely on the final inspect_daemon
        # check below to keep us safe.
        if pid is not None and holder is not None and holder != pid:
            console.print(
                f"[yellow]Lock now held by pid={holder} (a different daemon). "
                "Leaving its artifacts alone.[/yellow]"
            )
            return
        time.sleep(0.2)

    # Re-inspect before any cleanup. If something else now owns the daemon
    # state, do nothing.
    final = inspect_daemon()
    if final.status == DaemonStatus.STALE or final.status == DaemonStatus.NOT_RUNNING:
        _clean_stale_artifacts()
    elif final.status == DaemonStatus.UNREACHABLE:
        original = f"pid={pid}" if pid is not None else "original daemon"
        holder = f"pid={final.pid}" if final.pid is not None else "an unidentified daemon"
        console.print(
            f"[yellow]{original} did not release lock within 10s. "
            f"{holder} still holds the lock; refusing to clean its artifacts. "
            "Investigate manually.[/yellow]"
        )
    elif final.status == DaemonStatus.RUNNING:
        successor = f"pid={final.pid}" if final.pid is not None else "an unidentified daemon"
        console.print(
            f"[yellow]A new daemon ({successor}) has taken over the socket. "
            "Leaving its artifacts alone.[/yellow]"
        )
    else:
        console.print(
            f"[yellow]Final state {final.status.value}; not auto-cleaning.[/yellow]"
        )


if __name__ == "__main__":
    cli()
