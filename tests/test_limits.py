import json
import re
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from computercraft_live_mcp import bridge_client, cc_lua, limits, relay
from computercraft_live_mcp.bridge_client import push_chunk
from computercraft_live_mcp.relay import Conn, encode_request


def test_limit_chain():
    assert limits.PUSH_CHUNK_JSON_BYTES + limits.REQUEST_ENVELOPE_ALLOWANCE < limits.MAX_REQ_BYTES
    assert limits.MAX_REQ_BYTES < limits.CC_WEBSOCKET_CAP
    assert limits.MAX_MESSAGE < limits.CC_WEBSOCKET_CAP
    assert limits.CC_WEBSOCKET_CAP <= limits.RELAY_WS_MAX_MSG_SIZE
    assert 0 < limits.OUTPUT_LIMIT < limits.MAX_MESSAGE
    assert 0 < limits.READ_LIMIT < limits.MAX_MESSAGE
    assert 0 < limits.RELAY_TIMEOUT_MARGIN < limits.CLIENT_TIMEOUT_MARGIN
    assert 0 < limits.FS_REPLY_LIMIT < limits.MAX_MESSAGE
    assert 0 < limits.DEFAULT_TIMEOUT <= limits.NETWORK_TIMEOUT


@pytest.mark.parametrize("name", ["MAX_MESSAGE", "OUTPUT_LIMIT", "READ_LIMIT", "DEFAULT_TIMEOUT"])
def test_lua_limits_match_python(name):
    source = (Path(relay.__file__).parent / "bridge.lua").read_text(encoding="utf-8")
    matches = re.findall(rf"^local {name} = (\d+)\b", source, re.MULTILINE)
    assert len(matches) == 1
    assert int(matches[0]) == getattr(limits, name)


def test_fs_snippets_use_the_reply_limit():
    # The file-system tool snippets trim their own replies (the bridge cannot shorten return values).
    assert f"\nlocal REPLY_LIMIT = {limits.FS_REPLY_LIMIT}\n" in cc_lua.FS_PRELUDE
    assert "%(" not in cc_lua.FS_PRELUDE


@pytest.mark.parametrize(
    ("character", "expected_chars"),
    [("a", 60_000), ("\u00e9", 7_500), ("\x00", 7_500), ('"', 30_000), ("\\", 30_000), ("\U0001f600", 3_750)],
)
def test_chunk_halving(character, expected_chars):
    text = "skip" + character * limits.PUSH_CHUNK_CHARS
    piece = push_chunk(text, 4)
    assert piece == character * expected_chars
    assert len(json.dumps(piece).encode()) <= limits.PUSH_CHUNK_JSON_BYTES


@pytest.mark.parametrize("pattern", ["\u00e9", "\x00\x01\x1f", '"\\', "\U0001f600", '\u00e9\x00"\\\U0001f600'])
def test_worst_case_chunk_and_full_envelope_fit(pattern):
    text = pattern * limits.PUSH_CHUNK_CHARS
    piece = push_chunk(text)
    body = {
        "op": "write",
        "timeout": 123456.789,
        "path": "/" + "\u00e9" * 3000,
        "data": piece,
        "append": True,
    }
    encoded = encode_request(10**20, body)
    wire = json.loads(encoded)
    assert set(wire) == {"type", "req", "op", "timeout", "path", "data", "append"}
    assert wire["data"] == piece.replace("\U0001f600", "?")
    chunk_bytes = len(json.dumps(piece).encode())
    envelope_bytes = len(encoded.encode()) - len(json.dumps(wire["data"]).encode())
    assert chunk_bytes <= limits.PUSH_CHUNK_JSON_BYTES
    assert envelope_bytes <= limits.REQUEST_ENVELOPE_ALLOWANCE
    assert len(encoded.encode()) < limits.MAX_REQ_BYTES


@pytest.mark.parametrize(
    "text", ["", "short", "x" * 150_001, "\u00e9\x00\U0001f600" * 30_000], ids=["empty", "short", "ascii", "escaped"]
)
@pytest.mark.parametrize("append", [False, True])
def test_push_file_uses_chunks_without_losing_text(monkeypatch, tmp_path, text, append):
    monkeypatch.setenv("CC_MCP_FILES_ROOT", str(tmp_path))
    source = tmp_path / "input.txt"
    source.write_text(text, encoding="utf-8", newline="")
    writes = []

    def call(op, computer, timeout, **args):
        assert computer == 7
        assert timeout == limits.DEFAULT_TIMEOUT
        if op == "exec":  # cc_push's check before the first chunk and, for an overwrite, the final swap
            return {"ok": True, "returns": [json.dumps(json.dumps({"ok": True, "size": len(text.encode())}))]}
        assert op == "write"
        writes.append(args)
        return {"ok": True}

    monkeypatch.setattr(bridge_client, "call", call)
    result = bridge_client.push_file(str(source), "remote.txt", computer=7, append=append)
    assert result["ok"]
    assert result["chars"] == len(text)
    assert result["chunks"] == len(writes) >= 1
    assert "".join(write["data"] for write in writes) == text
    pos = 0
    for i, write in enumerate(writes):
        assert write["data"] == push_chunk(text, pos)
        assert write["path"] == ("remote.txt" if append else "remote.txt.part")  # an overwrite goes via .part
        assert write["append"] == ((append or i > 0) or None)
        pos += len(write["data"])


@pytest.mark.asyncio
async def test_roundtrip_uses_encoder_and_enforces_exact_limit():
    ws = AsyncMock()
    conn = Conn(ws, {"id": 7})
    body = {"op": "write", "path": "test", "data": "", "timeout": limits.DEFAULT_TIMEOUT, "append": True}
    body["data"] = "x" * (limits.MAX_REQ_BYTES - len(encode_request(1, body).encode()))

    async def reply(data):
        rid = json.loads(data)["req"]
        conn.pending[rid].set_result({"ok": True})

    ws.send_str.side_effect = reply
    assert await conn._roundtrip(body, 1) == {"ok": True}
    ws.send_str.assert_awaited_once_with(encode_request(1, body))
    assert len(ws.send_str.call_args.args[0].encode()) == limits.MAX_REQ_BYTES
    body["data"] += "x"
    result = await conn._roundtrip(body, 1)
    assert result["error"]["kind"] == "too_large"
    assert ws.send_str.await_count == 1
    assert not conn.pending


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [None, 2.5])
async def test_relay_timeout_margin(monkeypatch, timeout):
    call = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(Conn, "call", call)
    app = relay.make_app({"api": "api-token", "computer": "computer-token"})
    async with TestClient(TestServer(app, port=0)) as client:
        async with client.ws_connect("/ws") as ws:
            await ws.send_json({"type": "hello", "id": 7, "token": "computer-token"})
            assert (await ws.receive_json())["type"] == "welcome"
            body = {"op": "ping"}
            if timeout is not None:
                body["timeout"] = timeout
            async with client.post("/api/call", json=body, headers={"Authorization": "Bearer api-token"}) as response:
                assert response.status == 200
                assert await response.json() == {"ok": True}
            expected = limits.DEFAULT_TIMEOUT if timeout is None else timeout
            call.assert_awaited_once_with(body, expected + limits.RELAY_TIMEOUT_MARGIN)
