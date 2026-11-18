import asyncio
import json

import aiohttp
import pytest

from computercraft_mcp import relay


def test_tokens_are_distinct_and_persisted(tokens):
    assert tokens["api"]
    assert tokens["computer"]
    assert tokens["api"] != tokens["computer"]
    assert json.loads((relay.HOME / "tokens.json").read_text()) == tokens
    assert relay.load_tokens() == tokens


@pytest.mark.asyncio
async def test_bad_api_token(relay_client):
    status, result = await relay_client.request("/api/computers", token="wrong")
    assert status == 401
    assert result["error"]["kind"] == "unauthorized"


@pytest.mark.asyncio
async def test_api_token_cannot_connect_a_computer(relay_client):
    async with relay_client.session.ws_connect(relay_client.server.make_url("/ws")) as ws:
        await ws.send_json({"type": "hello", "token": relay_client.tokens["api"], "id": 1})
        message = await asyncio.wait_for(ws.receive(), 10)
        assert message.type == aiohttp.WSMsgType.CLOSE
        assert ws.close_code == 4401


@pytest.mark.asyncio
async def test_no_computer(relay_client):
    status, result = await relay_client.call(op="ping")
    assert status == 409
    assert result["error"]["kind"] == "no_target"


@pytest.mark.asyncio
async def test_computer_listed(relay_client, computer):
    status, result = await relay_client.request("/api/computers")
    assert status == 200
    assert len(result["computers"]) == 1
    info = result["computers"][0]
    assert info["id"] == 7
    assert info["label"] == "fake"
    assert info["host"] == "test"


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["1+1", "é—✓", "emoji: 😀"])
async def test_exec_round_trip(relay_client, computer, code):
    status, result = await relay_client.call(op="exec", code=code)
    assert status == 200
    assert result["ok"] is True
    assert result["returns"] == [json.dumps(code.replace("😀", "?"))]
    assert result["output"] == "echo"


@pytest.mark.asyncio
async def test_error_passes_through(relay_client, computer):
    status, result = await relay_client.call(op="exec", code="error()")
    assert status == 200
    assert result["ok"] is False
    assert result["error"] == {"kind": "runtime", "message": "exec:1: boom"}
    assert result["stderr"] == []
    assert result["output"] == ""


@pytest.mark.asyncio
async def test_oversized_request(relay_client, computer):
    _, result = await relay_client.call(op="exec", code="x" * 150_000)
    assert result["ok"] is False
    assert result["error"]["kind"] == "too_large"
    assert computer.seen == []


@pytest.mark.asyncio
async def test_ping_while_exec_is_running(relay_client, computer):
    slow = asyncio.create_task(relay_client.call(op="exec", code="slow"))
    try:
        await asyncio.wait_for(computer.exec_started.wait(), 10)
        _, pong = await asyncio.wait_for(relay_client.call(op="ping"), 10)
        assert pong["ok"] is True
        assert not slow.done()
        computer.release_exec.set()
        _, result = await asyncio.wait_for(slow, 10)
        assert result["ok"] is True
    finally:
        computer.release_exec.set()
        slow.cancel()
        await asyncio.gather(slow, return_exceptions=True)


@pytest.mark.asyncio
async def test_silent_computer_times_out_and_recovers(relay_client, computer):
    _, result = await relay_client.call(op="exec", code="hang", timeout=1)
    assert result["ok"] is False
    assert result["error"]["kind"] == "relay_timeout"
    await asyncio.wait_for(computer.cancelled.wait(), 10)
    assert [request["op"] for request in computer.seen] == ["exec", "cancel"]
    _, result = await relay_client.call(op="exec", code="1+2")
    assert result["ok"] is True
    assert result["returns"] == ['"1+2"']


@pytest.mark.asyncio
async def test_disconnect_mid_call_and_gone_computer(relay_client, computer):
    _, result = await relay_client.call(op="exec", code="die")
    assert result["ok"] is False
    assert result["error"]["kind"] == "disconnected"
    status, result = await relay_client.call(op="ping", computer=7)
    assert status == 404
    assert result["error"]["kind"] == "not_connected"


@pytest.mark.asyncio
@pytest.mark.xfail(strict=True, reason="#5: malformed API bodies must return JSON bad_request errors")
@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"op": "ping", "computer": "abc"}, id="invalid-computer"),
        pytest.param([], id="array-body"),
        pytest.param({"op": "exec", "code": "1", "timeout": "abc"}, id="invalid-timeout"),
    ],
)
async def test_malformed_call_body(relay_client, computer, body):
    async with relay_client.session.post(
        relay_client.server.make_url("/api/call"),
        json=body,
        headers={"Authorization": "Bearer " + relay_client.tokens["api"]},
    ) as response:
        assert response.status == 400
        result = await response.json()
        assert result["ok"] is False
        assert result["error"]["kind"] == "bad_request"
