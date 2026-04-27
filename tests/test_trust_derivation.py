"""Trust-derivation tests for TGSession._trust_for.

The critical invariant: a message authored by someone OTHER than the user
must never be emitted as trust="high", even when forwarded by the user
themselves. Otherwise an attacker could reach the high-trust tier just
by getting the user to forward a malicious message.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from tgmcp.daemon.sanitizer import derive_trust
from tgmcp.daemon.telegram import TGSession, TGConfig


@dataclass
class FakeMsg:
    sender_id: Optional[int]
    chat_id: int
    fwd_from: object = None
    is_private: bool = False


def _session(me_id: int = 100, contacts: set[int] | None = None) -> TGSession:
    s = TGSession(cfg=TGConfig(api_id=1, api_hash="x", session_string="y"))
    s.me_id = me_id
    s.contact_ids = contacts or set()
    return s


def test_self_authored_message_is_high_trust():
    s = _session(me_id=100)
    m = FakeMsg(sender_id=100, chat_id=42, is_private=True)
    ctx = s._trust_for(m)
    assert ctx.is_self is True
    assert derive_trust(ctx) == "high"


def test_forwarded_message_from_self_is_NOT_high_trust():
    """Even if the user forwarded it (sender == me), the content author is
    someone else. Must downgrade to low."""
    s = _session(me_id=100)
    m = FakeMsg(sender_id=100, chat_id=42, fwd_from=object(), is_private=True)
    ctx = s._trust_for(m)
    assert ctx.is_self is False, "forwarded content must not be high-trust"
    assert derive_trust(ctx) == "low"


def test_contact_dm_is_medium():
    s = _session(me_id=100, contacts={200})
    m = FakeMsg(sender_id=200, chat_id=200, is_private=True)
    ctx = s._trust_for(m)
    assert derive_trust(ctx) == "medium"


def test_forwarded_from_contact_is_low():
    """Contact forwarded a stranger's message — content is from stranger."""
    s = _session(me_id=100, contacts={200})
    m = FakeMsg(sender_id=200, chat_id=200, fwd_from=object(), is_private=True)
    ctx = s._trust_for(m)
    assert derive_trust(ctx) == "low"


def test_stranger_in_group_is_low():
    s = _session(me_id=100)
    m = FakeMsg(sender_id=999, chat_id=-1001234567890, is_private=False)
    ctx = s._trust_for(m)
    assert derive_trust(ctx) == "low"


def test_anonymous_admin_is_low():
    s = _session(me_id=100)
    m = FakeMsg(sender_id=None, chat_id=-1001234567890, is_private=False)
    ctx = s._trust_for(m)
    assert derive_trust(ctx) == "low"


def test_chat_id_uses_marked_format():
    """TGMessage.chat_id must come from m.chat_id directly, not be reconstructed
    from peer_id (which would strip Telethon's -100<channel_id> marker)."""
    s = _session(me_id=100)
    m = FakeMsg(sender_id=100, chat_id=-1001234567890)
    ctx = s._trust_for(m)
    assert ctx.chat_id == -1001234567890
