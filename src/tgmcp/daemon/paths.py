"""Shared filesystem paths.

Splits paths into two tiers:

* **Runtime** (socket, lock, pid): MUST live on a guaranteed-local filesystem.
  Unix domain sockets and `flock` are unreliable on NFS/SMB-mounted homes,
  which would silently break our "lock is authoritative" liveness invariant.
  We prefer `$XDG_RUNTIME_DIR` (typically `/run/user/$UID`, tmpfs on Linux)
  and fall back to `/tmp/tgmcp-<uid>` with mode 0700.

* **Persistent** (sessions, audit log, daemon log): live under
  `~/.config/tgmcp`. These are durable user data and home-on-NFS is fine
  for ordinary file I/O.

All consumers MUST import from this module rather than building paths
locally, so there is exactly one source of truth.
"""

from __future__ import annotations

import os
import pwd
import stat
from pathlib import Path


def _resolve_home() -> Path:
    """Authoritative home from /etc/passwd, NOT from $HOME.

    `$HOME` is just an env var; an attacker who can influence it (poisoned
    shell init, sudo without -H, etc.) could redirect every "persistent" path
    in this module to a directory they control. `pwd.getpwuid(getuid())`
    queries the OS user database, which they cannot rewrite without root.
    """
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


HOME_DIR = _resolve_home()
CONFIG_DIR = HOME_DIR / ".config" / "tgmcp"
SESSIONS_DIR = CONFIG_DIR / "sessions"
AUDIT_LOG_PATH = CONFIG_DIR / "audit.log"
LOG_PATH = CONFIG_DIR / "daemon.log"

# Fixed local path. We deliberately do NOT use `tempfile.gettempdir()` here
# because that honors $TMPDIR/$TEMP/$TMP, which on shared dev machines can
# point at a network-mounted directory — exactly the failure mode the
# runtime-dir split is meant to prevent. `/tmp` is hardcoded local on every
# POSIX system this daemon supports.
FALLBACK_RUNTIME_BASE = Path("/tmp")


class UnsafeRuntimeDir(RuntimeError):
    """Raised when the runtime dir cannot be made safe to use.

    This is fatal: rather than fall back to an attacker-controllable path,
    we refuse to start. The operator must remove or fix the offending dir.
    """


def _validate_runtime_dir(d: Path) -> None:
    """Reject a runtime dir that another user could exploit.

    Threats this defends against:
      1. Symlink → attacker pre-creates `/tmp/tgmcp-1000` as a symlink to
         their own directory; our socket/lock/pid land there.
      2. Wrong owner → attacker creates the dir first; we'd write secrets
         (audit pid, socket connection target) into their dir.
      3. Wide perms → another user could read/write our socket and
         impersonate clients to our daemon.
    """
    info = os.lstat(d)  # lstat: don't follow symlinks
    if stat.S_ISLNK(info.st_mode):
        raise UnsafeRuntimeDir(
            f"{d} is a symlink — refusing to use it. Remove and retry."
        )
    if not stat.S_ISDIR(info.st_mode):
        raise UnsafeRuntimeDir(f"{d} exists but is not a directory.")
    if info.st_uid != os.getuid():
        raise UnsafeRuntimeDir(
            f"{d} is owned by uid={info.st_uid}, not us ({os.getuid()}). "
            "This may be a squat attack — refusing to use it."
        )
    perms = info.st_mode & 0o777
    if perms & 0o077:
        raise UnsafeRuntimeDir(
            f"{d} mode is {oct(perms)} (group/world bits set). "
            "Required: 0700. Refusing to expose runtime artifacts."
        )


def _xdg_runtime_dir_safe() -> Path | None:
    """Return XDG_RUNTIME_DIR if set AND safe (per spec: owned by us, 0700,
    real directory, not a symlink). Otherwise None.

    The env var can be attacker-set in shared environments; we validate the
    parent before trusting it as the root for our socket/lock/pid.
    """
    raw = os.environ.get("XDG_RUNTIME_DIR")
    if not raw:
        return None
    p = Path(raw)
    try:
        info = os.lstat(p)
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return None
    if info.st_uid != os.getuid():
        return None
    if info.st_mode & 0o077:
        return None
    return p


def ensure_safe_subdir(parent: Path, name: str) -> Path:
    """Create or validate an app-owned subdirectory of `parent`.

    Same safety properties as the runtime root: no symlinks, owned by us, 0700.
    Raises UnsafeRuntimeDir on any violation.
    """
    d = parent / name
    try:
        os.makedirs(d, mode=0o700, exist_ok=False)
    except FileExistsError:
        _validate_runtime_dir(d)
    except OSError as e:
        raise UnsafeRuntimeDir(f"could not create {d}: {e}") from e
    return d


def _resolve_runtime_dir(fallback_base: Path | None = None) -> Path:
    """Pick the runtime dir. `fallback_base` is exposed for testing — pass
    a tmp_path to exercise the fallback branch with attack scenarios."""
    base = fallback_base if fallback_base is not None else FALLBACK_RUNTIME_BASE
    xdg = _xdg_runtime_dir_safe()
    if xdg is not None:
        d = xdg / "tgmcp"
    else:
        d = base / f"tgmcp-{os.getuid()}"

    try:
        os.makedirs(d, mode=0o700, exist_ok=False)
    except FileExistsError:
        _validate_runtime_dir(d)
    except OSError as e:
        raise UnsafeRuntimeDir(f"could not create runtime dir {d}: {e}") from e

    return d


RUNTIME_DIR = _resolve_runtime_dir()
DOWNLOADS_DIR = RUNTIME_DIR / "downloads"
SOCKET_PATH = RUNTIME_DIR / "daemon.sock"
LOCK_PATH = RUNTIME_DIR / "daemon.lock"
PID_PATH = RUNTIME_DIR / "daemon.pid"
