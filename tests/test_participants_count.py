"""Regression: classic Chat groups must report a real participants_count.

Round-10 found the bug: GetFullChatRequest returns ChatFull whose `full_chat`
exposes `.participants` (a list), not `.participants_count`. The previous
code used getattr for `participants_count` and silently got None.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tgmcp.daemon.telegram import TGSession, TGConfig


def _session() -> TGSession:
    return TGSession(cfg=TGConfig(api_id=1, api_hash="x", session_string="y"))


@pytest.mark.asyncio
async def test_chat_branch_counts_participants_list():
    """Classic groups: count items in full_chat.participants.participants."""
    from telethon.tl.types import Chat

    s = _session()
    fake_entity = Chat.__new__(Chat)
    fake_entity.id = 42
    fake_entity.participants_count = None  # entity itself has nothing

    # Mock the GetFullChatRequest result.
    full = SimpleNamespace(
        full_chat=SimpleNamespace(
            participants=SimpleNamespace(
                participants=[object(), object(), object(), object(), object()]  # 5 members
            )
        )
    )

    class FakeClient:
        async def __call__(self, _request):
            return full

    s.client = FakeClient()

    count = await s._participants_count(fake_entity)
    assert count == 5


@pytest.mark.asyncio
async def test_chat_branch_falls_back_to_entity_when_participants_forbidden():
    """When permissions are restricted (ChatParticipantsForbidden), fall back
    to the entity's own participants_count if available."""
    from telethon.tl.types import Chat

    s = _session()
    fake_entity = Chat.__new__(Chat)
    fake_entity.id = 42
    fake_entity.participants_count = 17

    # Forbidden case: full_chat.participants has no .participants list.
    full = SimpleNamespace(
        full_chat=SimpleNamespace(participants=SimpleNamespace())  # no .participants
    )

    class FakeClient:
        async def __call__(self, _request):
            return full

    s.client = FakeClient()

    count = await s._participants_count(fake_entity)
    assert count == 17


@pytest.mark.asyncio
async def test_channel_branch_uses_full_chat_participants_count():
    """Channels/megagroups: full.full_chat.participants_count is the right field."""
    from telethon.tl.types import Channel

    s = _session()
    fake_entity = Channel.__new__(Channel)
    fake_entity.id = 100
    fake_entity.broadcast = True
    fake_entity.participants_count = None

    full = SimpleNamespace(full_chat=SimpleNamespace(participants_count=12345))

    class FakeClient:
        async def __call__(self, _request):
            return full

    s.client = FakeClient()

    count = await s._participants_count(fake_entity)
    assert count == 12345


@pytest.mark.asyncio
async def test_rpc_failure_falls_back_to_entity_count():
    """If GetFull*Request raises (privacy/permission), don't crash —
    fall back to the entity's lightweight count if it has one."""
    from telethon.tl.types import Chat

    s = _session()
    fake_entity = Chat.__new__(Chat)
    fake_entity.id = 42
    fake_entity.participants_count = 99

    class BoomClient:
        async def __call__(self, _request):
            raise RuntimeError("rpc denied")

    s.client = BoomClient()

    count = await s._participants_count(fake_entity)
    assert count == 99
