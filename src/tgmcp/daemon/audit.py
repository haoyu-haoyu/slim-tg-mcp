"""Append-only audit log for write operations.

Only writes are logged (sends, edits, deletes). Reads are skipped to keep
log size manageable.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .paths import AUDIT_LOG_PATH as LOG_PATH


def log(action: str, **fields: object) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "action": action,
        **fields,
    }
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
