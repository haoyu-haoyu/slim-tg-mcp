"""tg-polls: schema, route, validator, dispatcher regressions."""

from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path

import pytest

from tgmcp.daemon import server


def test_routes_registered():
    paths = {r.path for r in server.app.routes}
    for p in ("/poll/create", "/poll/close", "/poll/results"):
        assert p in paths, f"route {p} not registered"


def test_create_poll_schema_required_fields():
    fields = {n for n, f in server.CreatePollReq.model_fields.items() if f.is_required()}
    assert fields == {"chat", "question", "options"}


def test_create_poll_rejects_one_option():
    with pytest.raises(ValueError):
        server.CreatePollReq(chat="@x", question="Q", options=["only one"])


def test_create_poll_rejects_eleven_options():
    with pytest.raises(ValueError):
        server.CreatePollReq(
            chat="@x", question="Q", options=[f"opt{i}" for i in range(11)]
        )


def test_create_poll_rejects_empty_option():
    with pytest.raises(ValueError, match="empty"):
        server.CreatePollReq(chat="@x", question="Q", options=["A", "  "])


def test_create_poll_rejects_oversize_option():
    with pytest.raises(ValueError, match="100 chars"):
        server.CreatePollReq(chat="@x", question="Q", options=["A", "x" * 101])


def test_create_poll_rejects_oversize_question():
    with pytest.raises(ValueError):
        server.CreatePollReq(chat="@x", question="x" * 301, options=["A", "B"])


def test_create_poll_rejects_oversize_explanation():
    with pytest.raises(ValueError):
        server.CreatePollReq(
            chat="@x",
            question="Q",
            options=["A", "B"],
            quiz=True,
            correct_option=0,
            explanation="x" * 201,
        )


def test_session_has_poll_methods():
    from tgmcp.daemon.telegram import TGSession

    for name in ("create_poll", "close_poll", "poll_results"):
        assert hasattr(TGSession, name), f"TGSession missing {name!r}"


def test_create_poll_quiz_validation():
    """The TGSession helper enforces quiz invariants up front."""
    import asyncio
    from types import SimpleNamespace

    from tgmcp.daemon.telegram import TGConfig, TGSession

    s = TGSession(cfg=TGConfig(api_id=1, api_hash="x", session_string="y"))
    s.client = SimpleNamespace()

    # quiz + multiple_choice → ValueError
    with pytest.raises(ValueError, match="multiple_choice"):
        asyncio.run(
            s.create_poll(
                "@x", "Q", ["A", "B"], quiz=True, multiple_choice=True, correct_option=0
            )
        )
    # quiz without correct_option → ValueError
    with pytest.raises(ValueError, match="correct_option"):
        asyncio.run(s.create_poll("@x", "Q", ["A", "B"], quiz=True))
    # correct_option out of range → ValueError
    with pytest.raises(ValueError, match="out of range"):
        asyncio.run(
            s.create_poll("@x", "Q", ["A", "B"], quiz=True, correct_option=5)
        )


def test_client_has_poll_methods():
    from tgmcp.client import DaemonClient

    for name in ("poll_create", "poll_close", "poll_results"):
        assert hasattr(DaemonClient, name), f"DaemonClient missing {name!r}"


def _load_skill():
    skill = Path(__file__).resolve().parents[1] / "skills" / "tg-polls" / "poll.py"
    spec = importlib.util.spec_from_file_location("poll_skill", skill)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_skill_dispatcher_subcommands():
    mod = _load_skill()
    assert set(mod.HANDLERS.keys()) == {"create", "edit", "close", "results"}


def test_skill_split_options_simple():
    mod = _load_skill()
    assert mod._split_options("A,B,C") == ["A", "B", "C"]


def test_skill_split_options_handles_escaped_comma():
    mod = _load_skill()
    # An option containing a literal comma should be escapable.
    assert mod._split_options(r"Pizza,Salad\, mixed,Sushi") == [
        "Pizza",
        "Salad, mixed",
        "Sushi",
    ]


def test_skill_split_options_strips_whitespace_and_drops_empty():
    mod = _load_skill()
    assert mod._split_options("A , , B ") == ["A", "B"]


def test_skill_quiz_without_correct_option_rejected():
    mod = _load_skill()
    args = mod.build_parser().parse_args(
        ["create", "--chat", "@x", "--question", "Q", "--options", "A,B", "--quiz"]
    )
    with pytest.raises(SystemExit, match="correct-option"):
        mod.cmd_create(args, c=None)


def test_skill_quiz_and_multiple_rejected():
    mod = _load_skill()
    args = mod.build_parser().parse_args(
        [
            "create",
            "--chat", "@x",
            "--question", "Q",
            "--options", "A,B",
            "--quiz", "--multiple",
            "--correct-option", "0",
        ]
    )
    with pytest.raises(SystemExit, match="mutually exclusive"):
        mod.cmd_create(args, c=None)


def test_poll_results_aligns_by_exact_option_bytes_not_index_byte():
    """Round-12 MAJOR: poll_results was reading r.option[0] as the answer
    index, which only works for polls *we* created. A poll authored by the
    official client (or any other tool) uses arbitrary opaque option bytes,
    and reading the first byte produces wrong indices and miscounted votes.

    Pin the fix: build an explicit (option_bytes → answer_index) map from
    the poll's own answers, and look up r.option by exact bytes value."""
    import asyncio
    from types import SimpleNamespace

    from tgmcp.daemon.telegram import TGConfig, TGSession

    s = TGSession(cfg=TGConfig(api_id=1, api_hash="x", session_string="y"))

    # Poll with non-trivial opaque bytes — neither matches bytes([i]).
    answers = [
        SimpleNamespace(text="A", option=b"\xff\x00first"),
        SimpleNamespace(text="B", option=b"\xab\xcd\xefsecond"),
        SimpleNamespace(text="C", option=b"third-bytes"),
    ]
    poll = SimpleNamespace(
        question="Q",
        answers=answers,
        closed=False,
        public_voters=False,
        multiple_choice=False,
        quiz=False,
    )
    # Results in a different order; index lookup via [0] would map both to 0xab/0xff.
    fake_results = SimpleNamespace(
        total_voters=12,
        results=[
            SimpleNamespace(option=b"third-bytes", voters=10),  # → answer index 2
            SimpleNamespace(option=b"\xab\xcd\xefsecond", voters=2),  # → answer index 1
        ],
    )
    fake_msg = SimpleNamespace(poll=SimpleNamespace(poll=poll, results=fake_results))

    class FakeClient:
        async def get_entity(self, _q):
            return SimpleNamespace()

        async def get_messages(self, _entity, ids):
            return fake_msg

    s.client = FakeClient()

    out = asyncio.run(s.poll_results("@x", 1))
    votes = {opt["index"]: opt["votes"] for opt in out["options"]}
    assert votes == {0: 0, 1: 2, 2: 10}, (
        f"expected exact-bytes alignment {{0:0,1:2,2:10}}, got {votes} — "
        "results decoder must NOT use option[0] as the index"
    )


def test_close_poll_preserves_original_poll_attributes():
    """Round-12 MINOR: close_poll used to reconstruct Poll from a subset of
    fields, dropping optional metadata like close_period / close_date.
    Verify the helper now passes a shallow-copied Poll with `closed=True`,
    preserving every other attribute on the original."""
    import asyncio
    from types import SimpleNamespace

    from tgmcp.daemon.telegram import TGConfig, TGSession

    s = TGSession(cfg=TGConfig(api_id=1, api_hash="x", session_string="y"))

    original_poll = SimpleNamespace(
        id=999,
        question="?",
        answers=[],
        closed=False,
        public_voters=True,
        multiple_choice=True,
        quiz=False,
        close_period=600,         # extra field that the old code dropped
        close_date="2030-01-01",  # extra field
    )
    fake_msg = SimpleNamespace(
        poll=SimpleNamespace(poll=original_poll, results=None)
    )

    sent: list = []

    class FakeClient:
        async def get_entity(self, _q):
            return SimpleNamespace()

        async def get_messages(self, _entity, ids):
            return fake_msg

        def __call__(self, request):
            sent.append(request)

            async def _coro():
                return SimpleNamespace()

            return _coro()

    s.client = FakeClient()
    asyncio.run(s.close_poll("@x", 999))

    assert len(sent) == 1
    closed_poll = sent[0].media.poll
    assert closed_poll.closed is True
    # All preserved fields:
    assert closed_poll.id == 999
    assert closed_poll.public_voters is True
    assert closed_poll.multiple_choice is True
    assert closed_poll.close_period == 600
    assert closed_poll.close_date == "2030-01-01"


def test_audit_logs_no_poll_question_or_option_text():
    """Polls can carry sensitive content (e.g. internal team votes). The
    audit log should record the operation but not the question text or
    option strings — only metadata like counts and ids."""
    src = inspect.getsource(server.poll_create)
    audit_idx = src.find("audit.log(")
    assert audit_idx >= 0
    block = src[audit_idx:]
    # `len(req.options)` is fine (count, not content). The leakage forms
    # we forbid:
    assert "question=req.question" not in block
    assert "options=req.options" not in block
    # Defensive: outright passing the strings somehow.
    assert "req.question" not in block, (
        "audit must not include the poll question text"
    )
