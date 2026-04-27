"""Phase 2: tg-messaging extension — schema and route shape regressions.

We can't hit live Telegram in unit tests, but we CAN verify:
  - The pydantic schemas accept/reject the right shapes.
  - The daemon route handlers exist and dispatch into TGSession methods.
  - The DaemonClient wrappers exist with the right signatures.
  - The act.py skill dispatcher parses each subcommand.
"""

from __future__ import annotations

import inspect

import pytest

from tgmcp.daemon import server


def test_schemas_have_required_fields():
    cases = {
        server.EditReq: {"chat", "msg_id", "text"},
        server.DeleteReq: {"chat", "msg_ids"},  # revoke has default
        server.ForwardReq: {"from_chat", "to_chat", "msg_ids"},
        server.PinReq: {"chat", "msg_id"},
        server.UnpinReq: {"chat"},
        server.ReactReq: {"chat", "msg_id"},
        server.MarkReadReq: {"chat"},
    }
    for cls, expected_required in cases.items():
        actual = {n for n, f in cls.model_fields.items() if f.is_required()}
        assert actual == expected_required, (
            f"{cls.__name__}: required fields mismatch — got {actual}, expected {expected_required}"
        )


def test_schemas_default_revoke_true_for_delete():
    """Telegram-client-equivalent default: delete for everyone."""
    assert server.DeleteReq.model_fields["revoke"].default is True


def test_react_emoji_is_optional_for_clear():
    """Setting emoji=None must be valid (it clears the reaction)."""
    req = server.ReactReq(chat="@x", msg_id=1, emoji=None)
    assert req.emoji is None


def test_route_paths_registered():
    paths = {r.path for r in server.app.routes}
    for p in ("/edit", "/delete", "/forward", "/pin", "/unpin", "/react", "/mark_read"):
        assert p in paths, f"route {p} not registered"


def test_client_has_corresponding_methods():
    from tgmcp.client import DaemonClient

    for method in ("edit", "delete", "forward", "pin", "unpin", "react", "mark_read"):
        assert hasattr(DaemonClient, method), f"DaemonClient missing {method!r}"


def test_tgsession_has_corresponding_methods():
    from tgmcp.daemon.telegram import TGSession

    for method in (
        "edit_message",
        "delete_messages",
        "forward_messages",
        "pin_message",
        "unpin_message",
        "react",
        "mark_as_read",
    ):
        assert hasattr(TGSession, method), f"TGSession missing {method!r}"


def test_react_signature_accepts_optional_emoji():
    from tgmcp.daemon.telegram import TGSession

    sig = inspect.signature(TGSession.react)
    assert "emoji" in sig.parameters
    # Must allow None for clearing reactions.
    p = sig.parameters["emoji"]
    assert p.annotation is not inspect.Parameter.empty


def test_skill_dispatcher_parses_each_subcommand():
    """Every documented subcommand in SKILL.md must be wired in act.py."""
    import importlib.util
    from pathlib import Path

    skill = (
        Path(__file__).resolve().parents[1]
        / "skills"
        / "tg-messaging"
        / "act.py"
    )
    spec = importlib.util.spec_from_file_location("act", skill)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)

    parser = mod.build_parser()
    for sub in ("send", "edit", "delete", "forward", "pin", "unpin", "react", "read"):
        # Each subcommand must parse its minimum-args invocation without
        # errors (we just verify the parser knows about it).
        # We use --help via SystemExit catch since argparse calls exit(0).
        try:
            parser.parse_args([sub, "--help"])
        except SystemExit as e:
            assert e.code == 0, f"subcommand {sub} failed parser sanity check"
        else:
            pytest.fail(f"--help should have raised SystemExit for {sub}")


def test_delete_returns_requested_count_not_pts_count():
    """Phase-2 review caught that AffectedMessages.pts_count is the
    updates-state delta, NOT a per-message delete count. delete_messages
    must therefore return len(msg_ids) (what we asked for), not pts_count."""
    import asyncio
    from types import SimpleNamespace

    from tgmcp.daemon.telegram import TGSession, TGConfig

    s = TGSession(cfg=TGConfig(api_id=1, api_hash="x", session_string="y"))

    class FakeClient:
        async def get_entity(self, _e):
            return SimpleNamespace()

        async def delete_messages(self, _entity, _ids, revoke=True):
            # Telethon returns AffectedMessages with pts_count = updates count,
            # which can be smaller, equal, or larger than len(ids).
            return SimpleNamespace(pts_count=999)

    s.client = FakeClient()
    n = asyncio.run(s.delete_messages("@x", [1, 2, 3]))
    assert n == 3, "must report attempted count, not pts_count(=999)"


def test_delete_endpoint_response_shape():
    """The /delete HTTP response must NOT include 'affected' (the bogus
    pts-derived count). It should report ok + requested count instead."""
    import inspect

    src = inspect.getsource(server.delete)
    assert '"affected"' not in src, "must not return the bogus 'affected' field"
    assert '"requested"' in src
    assert '"ok"' in src


def test_skill_handlers_match_subcommand_names():
    import importlib.util
    from pathlib import Path

    skill = (
        Path(__file__).resolve().parents[1]
        / "skills"
        / "tg-messaging"
        / "act.py"
    )
    spec = importlib.util.spec_from_file_location("act", skill)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)

    expected = {"send", "edit", "delete", "forward", "pin", "unpin", "react", "read"}
    assert set(mod.HANDLERS.keys()) == expected
