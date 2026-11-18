"""Computer labels select connections without changing numeric id authentication."""

import asyncio
import io
import logging
import sys
from contextlib import asynccontextmanager

import pytest
from conftest import FakeComputer

from computercraft_live_mcp import bridge_client as bc
from computercraft_live_mcp import ccctl, formatting, mcp_server, relay


class LabelComputer(FakeComputer):
    def __init__(self, ws, cid, label):
        super().__init__(ws)
        self.reply = {"ok": True, "id": cid, "label": label}

    async def handle(self, request):
        if request["op"] != "ping":
            return await super().handle(request)
        self.seen.append(request)
        await self.ws.send_json({"type": "result", "req": request["req"], **self.reply})


@asynccontextmanager
async def connect(client, cid=7, label="reactor"):
    async with client.session.ws_connect(client.server.make_url("/ws")) as ws:
        await ws.send_json({"type": "hello", "token": client.tokens["computer"], "id": cid, "label": label})
        assert (await ws.receive_json(timeout=5))["type"] == "welcome"
        fake = LabelComputer(ws, cid, label)
        reader = asyncio.create_task(fake.run())
        try:
            yield fake
        finally:
            for task in (reader, *fake.tasks):
                task.cancel()
            results = await asyncio.gather(reader, *fake.tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                    raise result


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["reactor", "REACTOR", "7", 7, 7.0, None])
async def test_resolves_label_or_id(relay_client, target):
    async with connect(relay_client):
        status, result = await relay_client.call(op="ping", computer=target)
        assert status == 200 and result["ok"] and result["id"] == 7


@pytest.mark.asyncio
async def test_digit_label_wins_over_id_fallback(relay_client):
    async with connect(relay_client, 7), connect(relay_client, 3, "7"):
        assert (await relay_client.call(op="ping", computer="7"))[1]["id"] == 3
        assert (await relay_client.call(op="ping", computer=7))[1]["id"] == 7


@pytest.mark.asyncio
async def test_casefold_is_used(relay_client):
    async with connect(relay_client, label="Straße"):
        assert (await relay_client.call(op="ping", computer="STRASSE"))[1]["ok"]


@pytest.mark.asyncio
async def test_ambiguous_label(relay_client, caplog):
    async with connect(relay_client, 3), connect(relay_client, 7, "REACTOR"):
        caplog.clear()
        caplog.set_level(logging.INFO, logger=relay.log.name)
        status, result = await relay_client.call(op="ping", computer="reactor")
        assert status == 409 and result["error"]["kind"] == "ambiguous"
        assert "#3" in result["error"]["message"] and "#7" in result["error"]["message"]
        assert "#- ping" in caplog.text and "reactor" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["missing", " reactor", "reactor ", "react", "07", " 7", "2147483648"])
async def test_unknown_label_is_not_connected(relay_client, target):
    async with connect(relay_client):
        status, result = await relay_client.call(op="ping", computer=target)
        assert status == 404 and result["error"]["kind"] == "not_connected"
        assert "no connected computer is labelled" in result["error"]["message"]
        assert "cc_ping" in result["error"]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["", [], {}, True, False, 1.5, -1, 2**31, 10**100])
async def test_bad_targets(relay_client, target):
    status, result = await relay_client.call(op="ping", computer=target)
    assert status == 400
    assert result == relay.err("bad_request", "computer must be an id or a label")


@pytest.mark.asyncio
async def test_no_target_lists_labels(relay_client):
    async with connect(relay_client, 3), connect(relay_client, 5, None), connect(relay_client, 7, "storage"):
        status, result = await relay_client.call(op="ping")
        assert status == 409 and result["error"]["kind"] == "no_target"
        assert result["error"]["message"] == (
            "3 computers connected (#3 (reactor), #5 (no label), #7 (storage)); pass 'computer' (an id or a label)"
        )


@pytest.mark.asyncio
async def test_no_computers(relay_client):
    status, result = await relay_client.call(op="ping")
    assert status == 409
    assert result["error"]["message"] == "no computer is connected"


@pytest.mark.asyncio
async def test_labels_in_errors_are_escaped_and_truncated(relay_client):
    label = "reactor\n\t\x1b" + "x" * 100
    async with connect(relay_client, 3, label), connect(relay_client, 7, label):
        for target, kind in [(None, "no_target"), (label, "ambiguous"), (label + "missing", "not_connected")]:
            _, result = await relay_client.call(op="ping", computer=target)
            assert result["error"]["kind"] == kind
            message = result["error"]["message"]
            assert "reactor\\n\\t\\u001b" in message and "..." in message
            assert "\n" not in message and "\t" not in message and "\x1b" not in message
            assert "x" * 33 not in message


@pytest.mark.asyncio
@pytest.mark.parametrize("label", [None, 42, [], {}])
async def test_hello_label_must_be_string(relay_client, label):
    async with connect(relay_client, label=label):
        _, result = await relay_client.request("/api/computers")
        assert result["computers"][0]["label"] is None


@pytest.mark.asyncio
async def test_ping_refreshes_label(relay_client, caplog):
    async with connect(relay_client) as fake:
        fake.reply["label"] = "storage\nroom"
        caplog.clear()
        caplog.set_level(logging.INFO, logger=relay.log.name)
        assert (await relay_client.call(op="ping", computer=7))[1]["ok"]
        assert "label changed" in caplog.text and "storage\\nroom" in caplog.text
        assert "storage\nroom" not in caplog.text
        assert (await relay_client.call(op="ping", computer="STORAGE\nROOM"))[1]["ok"]
        assert (await relay_client.call(op="ping", computer="reactor"))[0] == 404
        del fake.reply["label"]
        await relay_client.call(op="ping", computer=7)
        assert (await relay_client.call(op="ping", computer="storage\nroom"))[0] == 404
        assert (await relay_client.request("/api/computers"))[1]["computers"][0]["label"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [{"ok": True, "id": 3}, {"ok": False, "id": 7}, {"ok": True}])
async def test_ping_only_refreshes_success_with_matching_id(relay_client, reply):
    async with connect(relay_client) as fake:
        fake.reply = {**reply, "label": "changed"}
        await relay_client.call(op="ping", computer=7)
        assert (await relay_client.request("/api/computers"))[1]["computers"][0]["label"] == "reactor"


@pytest.mark.asyncio
@pytest.mark.parametrize("target, logged", [("private\nlabel", "-"), (42, "42"), ("42", "42")])
async def test_missing_target_logging(relay_client, caplog, target, logged):
    caplog.set_level(logging.INFO, logger=relay.log.name)
    status, result = await relay_client.call(op="exec", computer=target, code="return 1")
    assert status == 404 and result["error"]["kind"] == "not_connected"
    assert f"#{logged} exec req=-" in caplog.text and "outcome=not_connected" in caplog.text
    assert "private" not in caplog.text


@pytest.mark.asyncio
async def test_every_tool_accepts_id_and_label():
    checked = 0
    for tool in await mcp_server.server.list_tools():
        computer = tool.input_schema.get("properties", {}).get("computer")
        if computer is not None:
            types = {item.get("type") for item in computer.get("anyOf", [])}
            assert {"integer", "string"} <= types, tool.name
            checked += 1
    assert checked > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("target", [3, "3", "reactor"])
async def test_mcp_and_client_pass_target_unchanged(monkeypatch, target):
    sent = []

    def request(path, body):
        sent.append((path, body))
        return {"ok": True, "id": 3}

    monkeypatch.setattr(bc, "request", request)
    # Through the server's argument validation: an int stays an int and "3" stays a string (the relay resolves it).
    await mcp_server.server.call_tool("cc_ping", {"computer": target})
    assert sent[0][0] == "/api/call"
    assert sent[0][1]["computer"] == target
    assert type(sent[0][1]["computer"]) is type(target)


@pytest.mark.parametrize("argument, expected", [("reactor", "reactor"), ("3", 3), ("03", "03"), (" 3", " 3")])
def test_ccctl_computer(monkeypatch, argument, expected):
    seen = []

    class Stream(io.StringIO):
        def reconfigure(self, **kwargs):
            pass

    output = Stream()
    monkeypatch.setattr(sys, "stdout", output)

    def call(op, computer=None, timeout=30, **args):
        seen.append(computer)
        return {"ok": True, "id": 3}

    monkeypatch.setattr(bc, "call", call)
    assert ccctl.main(["ping", "--computer", argument]) == 0
    assert seen == [expected] and type(seen[0]) is type(expected)
    assert output.getvalue().startswith("OK")


def test_ccctl_empty_computer():
    with pytest.raises(SystemExit) as exited:
        ccctl.build_parser().parse_args(["ping", "--computer", ""])
    assert exited.value.code == 2


def test_ambiguous_formatting():
    assert "ambiguous" in formatting.KNOWN_KINDS
    assert "computer=<id>" in formatting.HINTS["ambiguous"]
    text = formatting.format_result("ping", relay.err("ambiguous", "duplicate label"))
    assert "  error: ambiguous\n" in text.split("<untrusted", 1)[0]
