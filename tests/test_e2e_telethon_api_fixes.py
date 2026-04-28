"""Regression tests pinning fixes discovered by the first real-account
e2e run on 2026-04-28.

Telethon ≥1.43 changed several TL types to use the structured
`TextWithEntities` field instead of bare `str`, dropped the
`scheduled=True` kwarg on `delete_messages`, restructured
`SaveDraftRequest` and `DraftMessage` to use a nested `reply_to` of
type `InputReplyTo`, and added a required `hash:int` field to `Poll`.

Each test below pins one of those fixes so a future Telethon upgrade
or a regression in our wrappers gets caught at unit-test time.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest


# ---------- Poll (TextWithEntities + hash) ----------


def test_create_poll_emits_hash_and_text_with_entities():
    """e2e finding: Poll(...) without hash crashed with TypeError;
    Poll.question and PollAnswer.text need TextWithEntities now."""
    from tgmcp.daemon.telegram import TGConfig, TGSession

    s = TGSession(cfg=TGConfig(api_id=1, api_hash="x", session_string="y"))

    captured: dict[str, Any] = {}

    class FakeClient:
        async def get_entity(self, _q):
            return SimpleNamespace()

        async def send_file(self, _e, media, **_kw):
            captured["media"] = media
            return SimpleNamespace(id=42)

    s.client = FakeClient()
    asyncio.run(s.create_poll("@x", "Q?", ["A", "B"]))

    poll = captured["media"].poll
    assert poll.hash == 0
    assert poll.question.__class__.__name__ == "TextWithEntities"
    assert poll.question.text == "Q?"
    for ans in poll.answers:
        assert ans.text.__class__.__name__ == "TextWithEntities"


def test_edit_poll_wraps_text_with_entities():
    """e2e finding: edit_poll's reconstructed PollAnswers also need
    TextWithEntities for the text field."""
    from tgmcp.daemon.telegram import TGConfig, TGSession

    s = TGSession(cfg=TGConfig(api_id=1, api_hash="x", session_string="y"))

    fake_poll = SimpleNamespace(
        question=SimpleNamespace(text="orig", entities=[]),
        answers=[
            SimpleNamespace(text=SimpleNamespace(text="A"), option=b"\x00"),
            SimpleNamespace(text=SimpleNamespace(text="B"), option=b"\x01"),
        ],
        closed=False,
        public_voters=False,
        multiple_choice=False,
        quiz=False,
    )
    fake_msg = SimpleNamespace(poll=SimpleNamespace(poll=fake_poll, results=None))

    captured: dict[str, Any] = {}

    class FakeClient:
        async def get_entity(self, _q):
            return SimpleNamespace()

        async def get_messages(self, _e, ids):
            return fake_msg

        def __call__(self, req):
            captured["req"] = req

            async def _coro():
                return SimpleNamespace()

            return _coro()

    s.client = FakeClient()
    asyncio.run(
        s.edit_poll("@x", 1, question="new?", options=["X", "Y"])
    )

    edited = captured["req"].media.poll
    assert edited.question.__class__.__name__ == "TextWithEntities"
    assert edited.question.text == "new?"
    for ans, expected in zip(edited.answers, ["X", "Y"]):
        assert ans.text.__class__.__name__ == "TextWithEntities"
        assert ans.text.text == expected


def test_poll_results_extracts_text_from_text_with_entities():
    """e2e finding: poll_results returned a TextWithEntities object
    in the 'question' field, which FastAPI's JSON encoder can't
    serialize. Must extract `.text` before returning."""
    from tgmcp.daemon.telegram import TGConfig, TGSession

    s = TGSession(cfg=TGConfig(api_id=1, api_hash="x", session_string="y"))

    fake_poll = SimpleNamespace(
        question=SimpleNamespace(text="my question?"),
        answers=[
            SimpleNamespace(
                text=SimpleNamespace(text="A"),
                option=b"\x00",
            ),
            SimpleNamespace(
                text=SimpleNamespace(text="B"),
                option=b"\x01",
            ),
        ],
        closed=False,
        public_voters=False,
        multiple_choice=False,
        quiz=False,
    )
    fake_msg = SimpleNamespace(
        poll=SimpleNamespace(
            poll=fake_poll,
            results=SimpleNamespace(total_voters=5, results=[]),
        )
    )

    class FakeClient:
        async def get_entity(self, _q):
            return SimpleNamespace()

        async def get_messages(self, _e, ids):
            return fake_msg

    s.client = FakeClient()
    out = asyncio.run(s.poll_results("@x", 1))

    # Must be plain strings, not TextWithEntities — else JSON encode fails
    assert isinstance(out["question"], str) and out["question"] == "my question?"
    for opt in out["options"]:
        assert isinstance(opt["text"], str)


# ---------- Drafts (reply_to InputReplyTo) ----------


def test_save_draft_uses_input_reply_to_message():
    """e2e finding: SaveDraftRequest's flat reply_to_msg_id was
    replaced with a structured reply_to: InputReplyTo object."""
    from tgmcp.daemon.telegram import TGConfig, TGSession

    s = TGSession(cfg=TGConfig(api_id=1, api_hash="x", session_string="y"))

    captured: dict[str, Any] = {}

    class FakeClient:
        async def get_entity(self, _q):
            return SimpleNamespace()

        def __call__(self, req):
            captured["req"] = req

            async def _coro():
                return SimpleNamespace()

            return _coro()

    s.client = FakeClient()
    asyncio.run(s.save_draft("@x", "draft", reply_to=42))

    req = captured["req"]
    assert req.reply_to.__class__.__name__ == "InputReplyToMessage"
    assert req.reply_to.reply_to_msg_id == 42


def test_get_draft_extracts_from_nested_reply_to():
    """e2e finding: DraftMessage.reply_to_msg_id was moved to
    reply_to.reply_to_msg_id (nested under InputReplyTo). Read-side
    extraction must dig into the nested object."""
    from tgmcp.daemon.telegram import TGConfig, TGSession

    s = TGSession(cfg=TGConfig(api_id=1, api_hash="x", session_string="y"))

    target_entity = SimpleNamespace()

    async def fake_iter():
        yield SimpleNamespace(
            entity=target_entity,
            text="real draft",
            date=None,
            reply_to=SimpleNamespace(reply_to_msg_id=999),
        )

    class FakeClient:
        async def get_entity(self, _q):
            return target_entity

        async def get_peer_id(self, e):
            return 42 if e is target_entity else 99

        def iter_drafts(self):
            return fake_iter()

    s.client = FakeClient()
    res = asyncio.run(s.get_draft("@x"))
    assert res is not None
    assert res["reply_to_msg_id"] == 999


# ---------- delete_scheduled (raw RPC) ----------


def test_delete_scheduled_uses_raw_rpc_not_helper_kwarg():
    """e2e finding: client.delete_messages(scheduled=True) no longer
    exists; we now use the raw DeleteScheduledMessagesRequest."""
    from tgmcp.daemon.telegram import TGConfig, TGSession

    s = TGSession(cfg=TGConfig(api_id=1, api_hash="x", session_string="y"))

    captured: dict[str, Any] = {}

    class FakeClient:
        async def get_input_entity(self, _q):
            return SimpleNamespace()

        def __call__(self, req):
            captured["req"] = req

            async def _coro():
                return SimpleNamespace()

            return _coro()

        # If the old code path were still in use, it would call this:
        async def delete_messages(self, *a, **kw):
            captured["bad"] = (a, kw)

    s.client = FakeClient()
    asyncio.run(s.delete_scheduled("@x", [1, 2, 3]))

    assert "bad" not in captured, "delete_scheduled must not call client.delete_messages"
    assert captured["req"].__class__.__name__ == "DeleteScheduledMessagesRequest"
    assert captured["req"].id == [1, 2, 3]


# ---------- Folders (TextWithEntities title) ----------


def test_update_folder_wraps_title_in_text_with_entities():
    """e2e finding: DialogFilter.title is now TypeTextWithEntities,
    not str. Caller-supplied string must be wrapped before the RPC."""
    from tgmcp.daemon.telegram import TGConfig, TGSession

    s = TGSession(cfg=TGConfig(api_id=1, api_hash="x", session_string="y"))

    captured: dict[str, Any] = {}

    class FakeClient:
        async def get_input_entity(self, _q):
            return SimpleNamespace()

        def __call__(self, req):
            captured["req"] = req

            async def _coro():
                return SimpleNamespace()

            return _coro()

    s.client = FakeClient()
    asyncio.run(
        s.update_folder(
            42,
            title="my folder",
            include_peers=["@somechat"],
        )
    )

    f = captured["req"].filter
    assert f.title.__class__.__name__ == "TextWithEntities"
    assert f.title.text == "my folder"


def test_list_folders_extracts_title_from_text_with_entities():
    """e2e finding: GetDialogFiltersRequest now returns DialogFilter
    with title=TextWithEntities; list_folders must extract .text for
    JSON serialization."""
    from tgmcp.daemon.telegram import TGConfig, TGSession

    s = TGSession(cfg=TGConfig(api_id=1, api_hash="x", session_string="y"))

    fake_filter = SimpleNamespace(
        id=42,
        title=SimpleNamespace(text="My Folder"),
        include_peers=["a", "b"],
        exclude_peers=[],
        pinned_peers=[],
        contacts=False,
        non_contacts=False,
        groups=True,
        broadcasts=False,
        bots=False,
    )

    class FakeClient:
        def __call__(self, req):
            async def _coro():
                return SimpleNamespace(filters=[fake_filter])

            return _coro()

    s.client = FakeClient()
    out = asyncio.run(s.list_folders())
    assert len(out) == 1
    assert isinstance(out[0]["title"], str)
    assert out[0]["title"] == "My Folder"


def test_extract_text_empty_string_round_trips():
    """Round-2 MINOR: an empty TextWithEntities('') must surface as ''
    in the JSON response, NOT as str(obj) which would emit the TL repr."""
    import asyncio

    from tgmcp.daemon.telegram import TGConfig, TGSession

    s = TGSession(cfg=TGConfig(api_id=1, api_hash="x", session_string="y"))

    fake_poll = SimpleNamespace(
        question=SimpleNamespace(text=""),  # deliberately empty
        answers=[
            SimpleNamespace(
                text=SimpleNamespace(text=""),
                option=b"\x00",
            )
        ],
        closed=True,
        public_voters=False,
        multiple_choice=False,
        quiz=False,
    )
    fake_msg = SimpleNamespace(
        poll=SimpleNamespace(poll=fake_poll, results=None)
    )

    class FakeClient:
        async def get_entity(self, _q):
            return SimpleNamespace()

        async def get_messages(self, _e, ids):
            return fake_msg

    s.client = FakeClient()
    out = asyncio.run(s.poll_results("@x", 1))
    assert out["question"] == ""
    assert out["options"][0]["text"] == ""


def test_list_folders_empty_title_round_trips():
    """Round-2 MINOR: same fix on the folder side — empty title must
    return as '', not as str(TextWithEntities)."""
    import asyncio

    from tgmcp.daemon.telegram import TGConfig, TGSession

    s = TGSession(cfg=TGConfig(api_id=1, api_hash="x", session_string="y"))

    fake_filter = SimpleNamespace(
        id=42,
        title=SimpleNamespace(text=""),
        include_peers=[],
        exclude_peers=[],
        pinned_peers=[],
        contacts=False,
        non_contacts=False,
        groups=True,
        broadcasts=False,
        bots=False,
    )

    class FakeClient:
        def __call__(self, req):
            async def _coro():
                return SimpleNamespace(filters=[fake_filter])

            return _coro()

    s.client = FakeClient()
    out = asyncio.run(s.list_folders())
    assert out[0]["title"] == ""


def test_get_draft_recognizes_non_message_reply_to_variant():
    """Round-2 MINOR: a draft with empty text but a non-message reply
    target (e.g. InputReplyToStory) must NOT be discarded as a
    placeholder — that would lose the user's actual draft state."""
    import asyncio

    from tgmcp.daemon.telegram import TGConfig, TGSession

    s = TGSession(cfg=TGConfig(api_id=1, api_hash="x", session_string="y"))

    target = SimpleNamespace()

    # InputReplyToStory shape: no reply_to_msg_id, but the fact that
    # reply_to is non-None means the user opened a story-reply UI.
    class InputReplyToStory:
        pass

    story_reply = InputReplyToStory()

    async def fake_iter():
        yield SimpleNamespace(
            entity=target,
            text="",  # no body yet
            date=None,
            reply_to=story_reply,
        )

    class FakeClient:
        async def get_entity(self, _q):
            return target

        async def get_peer_id(self, e):
            return 42 if e is target else 99

        def iter_drafts(self):
            return fake_iter()

    s.client = FakeClient()
    res = asyncio.run(s.get_draft("@x"))
    assert res is not None, "story-reply draft must not be treated as placeholder"
    assert res["text"] == ""
    assert res["reply_to_kind"] == "InputReplyToStory"
    # No reply_to_msg_id on this variant — stays None but draft surfaces
    assert res["reply_to_msg_id"] is None


def test_list_folders_falls_back_to_str_for_legacy():
    """If a future Telethon downgrade ever returns title as a bare
    string again, list_folders must still work."""
    from tgmcp.daemon.telegram import TGConfig, TGSession

    s = TGSession(cfg=TGConfig(api_id=1, api_hash="x", session_string="y"))

    legacy_filter = SimpleNamespace(
        id=7,
        title="Legacy Folder",  # bare string
        include_peers=[],
        exclude_peers=[],
        pinned_peers=[],
        contacts=True,
        non_contacts=False,
        groups=False,
        broadcasts=False,
        bots=False,
    )

    class FakeClient:
        def __call__(self, req):
            async def _coro():
                return SimpleNamespace(filters=[legacy_filter])

            return _coro()

    s.client = FakeClient()
    out = asyncio.run(s.list_folders())
    assert out[0]["title"] == "Legacy Folder"
