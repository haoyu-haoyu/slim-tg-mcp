"""Round-12 MAJOR: tg_download_media must not accept caller-controlled out_dir.

A prompt-injected model would otherwise have a filesystem-write primitive
with attacker-controlled Telegram content. The MCP tool / daemon route now
forces downloads into the app-owned DOWNLOADS_DIR with server-generated
filenames.
"""

from __future__ import annotations



from tgmcp.mcp_server import server as mcp_server


def test_mcp_tool_schema_does_not_expose_out_dir():
    """Regression: the MCP tool description must not advertise an `out_dir`
    parameter. If it did, prompt injection could redirect the file write."""
    # The tool list is async — we just need to inspect the source schema.
    import inspect

    source = inspect.getsource(mcp_server)
    # The schema for tg_download_media must not mention out_dir.
    assert "tg_download_media" in source
    download_block_start = source.index("tg_download_media")
    download_block = source[download_block_start : download_block_start + 1000]
    assert "out_dir" not in download_block, (
        "tg_download_media schema must not expose out_dir to callers — "
        "that would let a prompt-injected model write to arbitrary paths"
    )


def test_daemon_request_schema_rejects_out_dir():
    """The DownloadReq pydantic model must not have an out_dir field.
    If it did, the HTTP endpoint would still accept it from skills/CLI."""
    from tgmcp.daemon.server import DownloadReq

    # Pydantic v2: model_fields exposes the schema.
    fields = set(DownloadReq.model_fields.keys())
    assert "out_dir" not in fields, "DownloadReq must not accept out_dir"
    assert fields == {"chat", "msg_id"}


def test_telegram_download_media_no_out_dir_param():
    """The TGSession.download_media method must not take an out_dir parameter."""
    import inspect
    from tgmcp.daemon.telegram import TGSession

    sig = inspect.signature(TGSession.download_media)
    assert "out_dir" not in sig.parameters, (
        "download_media must NOT take a caller-controlled out_dir"
    )


def test_client_download_no_out_dir_param():
    """The shared DaemonClient wrapper must mirror the safer schema."""
    import inspect
    from tgmcp.client import DaemonClient

    sig = inspect.signature(DaemonClient.download)
    assert "out_dir" not in sig.parameters
