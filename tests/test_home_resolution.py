"""Round-14 MINOR: HOME must come from /etc/passwd, not $HOME.

`os.path.expanduser("~/.config/tgmcp")` consults `$HOME` first. An attacker
who can influence `$HOME` (poisoned shell init, sudo without -H) could
redirect every persistent path — sessions, audit log, daemon log — to a
directory they control. `pwd.getpwuid()` queries the OS user database and
ignores env vars.
"""

from __future__ import annotations

import os
import pwd
import sys

import pytest

if sys.platform == "win32":  # pragma: no cover
    pytest.skip("POSIX-only", allow_module_level=True)


def test_home_dir_comes_from_pwd_not_env():
    """Even with a malicious $HOME, our HOME_DIR must equal the pwd entry."""
    real_home = pwd.getpwuid(os.getuid()).pw_dir
    from tgmcp.daemon import paths as paths_mod

    assert str(paths_mod.HOME_DIR) == real_home


def test_config_dir_anchored_to_pwd_home(monkeypatch):
    """If we set $HOME to something bogus, CONFIG_DIR resolution should be
    unaffected because we use pwd, not expanduser."""
    monkeypatch.setenv("HOME", "/tmp/attacker-controlled-home")

    from tgmcp.daemon import paths as paths_mod

    # _resolve_home must NOT honor $HOME.
    resolved = paths_mod._resolve_home()
    assert str(resolved) != "/tmp/attacker-controlled-home"
    assert str(resolved) == pwd.getpwuid(os.getuid()).pw_dir
