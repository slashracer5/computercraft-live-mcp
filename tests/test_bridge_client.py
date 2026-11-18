import asyncio
import json
import os
import socket

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer as AiohttpTestServer

from computercraft_mcp import bridge_client as bc
from computercraft_mcp import formatting


@pytest.fixture
def files_root(monkeypatch, tmp_path):
    monkeypatch.setenv("CC_MCP_FILES_ROOT", str(tmp_path))
    return tmp_path


@pytest.fixture
def http_environment(monkeypatch):
    monkeypatch.setenv("CC_MCP_API_TOKEN", "test-api-token")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")


def test_format_success_result():
    text = formatting.format_result(
        "exec",
        {"ok": True, "returns": ["42", '"hello"'], "stderr": ["warning"], "output": "hello world", "duration": 1.234},
    )
    assert text.splitlines()[0] == "OK [1.23s]"
    assert 'returns: 42, "hello"' in text
    assert "stderr:" in text and "warning" in text
    assert "output:" in text and "hello world" in text


def test_format_error_result_includes_hint_and_traceback():
    text = formatting.format_result(
        "exec",
        {
            "ok": False,
            "error": {"kind": "disconnected", "message": "gone", "traceback": "exec:1\nstack traceback"},
        },
    )
    assert text.splitlines()[0] == "ERROR"
    assert "disconnected" in text and "gone" in text
    assert "exec:1" in text and "stack traceback" in text
    assert formatting.HINTS["disconnected"] in text


def test_format_log_marks_red_lines_and_error_id():
    text = formatting.format_result(
        "log",
        {
            "ok": True,
            "lines": ["ordinary", "red warning"],
            "red": [2],
            "errors": [{"id": 7, "when": "12:00", "text": "boom"}],
            "last_error_id": 7,
        },
    )
    lines = text.splitlines()
    assert "! red warning" in lines
    assert "  ordinary" in lines
    assert "printError log:" in text
    assert "#7" in text and "12:00" in text and "boom" in text
    assert "last_error_id: 7" in text


@pytest.mark.parametrize("eof", [False, True])
def test_format_paging_hint_only_before_eof(eof):
    text = formatting.format_result("read", {"ok": True, "data": "page", "next_offset": 123, "eof": eof})
    assert ("[more data: call again with offset=123]" in text) is (not eof)


def test_resolve_local_requires_explicit_roots(monkeypatch):
    monkeypatch.delenv("CC_MCP_FILES_ROOT", raising=False)
    with pytest.raises(PermissionError, match="CC_MCP_FILES_ROOT"):
        bc.resolve_local("file.txt")


def test_resolve_local_uses_first_root_and_allows_second(monkeypatch, tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.setenv("CC_MCP_FILES_ROOT", os.pathsep.join([str(first), str(second)]))
    assert bc.resolve_local("nested/file.txt") == (first / "nested" / "file.txt").resolve()
    assert bc.resolve_local(str(second / "file.txt")) == (second / "file.txt").resolve()


def test_resolve_local_refuses_outside_path(monkeypatch, tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    monkeypatch.setenv("CC_MCP_FILES_ROOT", str(root))
    with pytest.raises(PermissionError, match="outside the allowed folders"):
        bc.resolve_local(str(tmp_path / "elsewhere.txt"))
    with pytest.raises(PermissionError, match="outside the allowed folders"):
        bc.resolve_local("../escaped.txt")


@pytest.mark.parametrize("append", [False, True])
def test_push_file_chunks_fit_json_limit_and_preserve_text(monkeypatch, files_root, append):
    text = "ASCII é—✓\x00\x01\x1f\r\n" * 20000
    source = files_root / "source.txt"
    source.write_bytes(text.encode("utf-8"))
    calls = []

    def call(op, computer, timeout, **kwargs):
        assert (op, computer, timeout) == ("write", 7, 12)
        calls.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr(bc, "call", call)
    result = bc.push_file("source.txt", "remote.txt", computer=7, timeout=12, append=append)
    assert result["ok"] is True
    assert result["chunks"] == len(calls) > 1
    assert result["chars"] == len(text)
    assert "".join(chunk["data"] for chunk in calls) == text
    assert all(len(json.dumps(chunk["data"])) <= 75000 for chunk in calls)
    assert all(chunk["path"] == "remote.txt" for chunk in calls)
    assert calls[0].get("append") is (True if append else None)
    assert all(chunk["append"] is True for chunk in calls[1:])


def test_push_file_stops_at_failing_chunk(monkeypatch, files_root):
    (files_root / "source.txt").write_bytes(b"x" * 150000)
    calls = []
    error = {"ok": False, "error": {"kind": "runtime", "message": "disk full"}}

    def call(op, computer, timeout, **kwargs):
        calls.append(kwargs)
        return {"ok": True} if len(calls) == 1 else error.copy()

    monkeypatch.setattr(bc, "call", call)
    result = bc.push_file("source.txt", "remote.txt")
    assert result["ok"] is False
    assert result["error"] == error["error"]
    assert result["chunks"] == len(calls) == 2


def test_push_missing_file_returns_local_error(monkeypatch, files_root):
    def unexpected_call(*args, **kwargs):
        pytest.fail("missing files must not contact the relay")

    monkeypatch.setattr(bc, "call", unexpected_call)
    result = bc.push_file("missing.txt", "remote.txt")
    assert result["ok"] is False
    assert result["error"]["kind"] == "local_file"


def test_pull_file_joins_pages_and_uses_next_offset(monkeypatch, files_root):
    calls = []
    pages = {
        0: {"ok": True, "data": "hé\r\n", "next_offset": 5, "eof": False},
        5: {"ok": True, "data": "✓\r\n", "next_offset": 10, "eof": True},
    }

    def call(op, computer, timeout, **kwargs):
        assert (op, computer, timeout) == ("read", 7, 12)
        assert kwargs["path"] == "remote.txt"
        calls.append(kwargs["offset"])
        return pages[kwargs["offset"]]

    monkeypatch.setattr(bc, "call", call)
    result = bc.pull_file("remote.txt", "nested/result.txt", computer=7, timeout=12)
    dest = files_root / "nested" / "result.txt"
    assert dest.read_bytes() == "hé\r\n✓\r\n".encode()
    assert calls == [0, 5]
    assert result == {"ok": True, "bytes": 10, "path": str(dest.resolve())}


def test_pull_file_passes_error_through(monkeypatch, files_root):
    error = {"ok": False, "error": {"kind": "not_connected", "message": "offline"}}
    monkeypatch.setattr(bc, "call", lambda *args, **kwargs: error)
    assert bc.pull_file("remote.txt", "result.txt") is error
    assert not (files_root / "result.txt").exists()


@pytest.mark.xfail(strict=True, reason="#1: pull_file must write byte-encoded data as raw bytes, not UTF-8")
def test_pull_file_preserves_raw_bytes(monkeypatch, files_root):
    blob = b"\x00\x81\xa0\xff"
    page = {"ok": True, "encoding": "bytes", "data": blob.decode("latin-1"), "next_offset": 4, "eof": True}
    monkeypatch.setattr(bc, "call", lambda *args, **kwargs: page)
    result = bc.pull_file("remote.bin", "result.bin")
    assert result["ok"] is True
    assert (files_root / "result.bin").read_bytes() == blob


def test_request_reports_unreachable_relay(monkeypatch, http_environment):
    # Reserve a port without listening, so another process cannot claim it during the request.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        monkeypatch.setattr(bc, "URL", f"http://127.0.0.1:{sock.getsockname()[1]}")
        result = bc.request("/api/computers")
    assert result["ok"] is False
    assert result["error"]["kind"] == "relay_unreachable"


@pytest.mark.asyncio
@pytest.mark.xfail(strict=True, reason="#5: non-JSON HTTP responses must return an error dict instead of raising")
@pytest.mark.parametrize("status", [404, 200], ids=["html-404", "non-json-200"])
async def test_request_handles_non_json_response(monkeypatch, http_environment, status):
    async def handler(request):
        return web.Response(status=status, text="<html>not a relay</html>", content_type="text/html")

    app = web.Application()
    app.router.add_get("/api/computers", handler)
    async with AiohttpTestServer(app, host="127.0.0.1", port=0) as server:
        monkeypatch.setattr(bc, "URL", str(server.make_url("/")).rstrip("/"))
        result = await asyncio.to_thread(bc.request, "/api/computers")
    assert result["ok"] is False
    assert isinstance(result["error"], dict)
    assert result["error"]["kind"]
    assert result["error"]["message"]
