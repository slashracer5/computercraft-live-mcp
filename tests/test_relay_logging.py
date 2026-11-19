import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from computercraft_live_mcp import relay

TOKENS = {
    "api": "API-TOKEN-SECRET-1",
    "computer": "COMPUTER-TOKEN-SECRET-2",
    "computers": {"4": "PER-COMPUTER-TOKEN-SECRET-4"},
}
HEADERS = {"Authorization": "Bearer " + TOKENS["api"]}


@pytest.fixture(autouse=True)
def capture_logs(caplog):
    caplog.set_level(logging.DEBUG, logger="computercraft_live_mcp")
    yield
    formatter = logging.Formatter(relay.LOG_FORMAT)
    for record in caplog.get_records("call"):
        for token in relay.secret_values(TOKENS):
            assert token not in record.getMessage()
            assert token not in formatter.format(record)


@asynccontextmanager
async def relay_client():
    async with TestClient(TestServer(relay.make_app(TOKENS), port=0)) as client:
        yield client


async def connect(client):
    ws = await client.ws_connect("/ws")
    await ws.send_json({"type": "hello", "id": 3, "token": TOKENS["computer"], "label": TOKENS["api"]})
    assert (await ws.receive_json(timeout=1))["type"] == "welcome"
    return ws


async def call(client, body):
    async with client.post("/api/call", json=body, headers=HEADERS) as response:
        assert response.status == 200
        return await response.json()


def messages(caplog, level, contains=""):
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == relay.log.name and record.levelno == level and contains in record.getMessage()
    ]


async def wait_for_log(caplog, level, contains):
    async def poll():
        while not messages(caplog, level, contains):
            await asyncio.sleep(0.01)
        return messages(caplog, level, contains)

    return await asyncio.wait_for(poll(), 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["exec", "ping", "cancel", "log"])
@pytest.mark.parametrize("kind", [None, "runtime", "timeout", "busy"])
async def test_request_summary(caplog, op, kind):
    async with relay_client() as client:
        ws = await connect(client)
        body = {"op": op, "code": "return 'private-code'", "timeout": 1}
        task = asyncio.create_task(call(client, body))
        request = await ws.receive_str(timeout=1)
        rid = json.loads(request)["req"]
        result = {"type": "result", "req": rid, "ok": kind is None, "output": "private-result-é", "stderr": {}}
        if kind:
            result["error"] = {"kind": kind, "message": "private-error"}
        reply = json.dumps(result, ensure_ascii=False)
        await ws.send_str(reply)
        response = await asyncio.wait_for(task, 1)
        assert response["stderr"] == []
        lines = messages(caplog, logging.INFO, "req=")
        assert len(lines) == 1
        assert re.fullmatch(
            rf"#3 {op} req={rid} queued=\d+\.\d{{2}}s rtt=\d+\.\d{{2}}s "
            rf"outcome={kind or 'ok'} req_bytes={len(request.encode())} resp_bytes={len(reply.encode())}",
            lines[0],
        )
        if op in relay.NO_LOCK_OPS:
            assert "queued=0.00s" in lines[0]
        for record in caplog.records:
            assert "private-result" not in record.getMessage()
            assert "private-error" not in record.getMessage()
            if record.levelno >= logging.INFO:
                assert "private-code" not in record.getMessage()


@pytest.mark.asyncio
async def test_queued_timeout_late_reply_and_cancel_ack(caplog, monkeypatch):
    monkeypatch.setattr(relay, "RELAY_TIMEOUT_MARGIN", 0)
    queued = asyncio.Event()
    original_call = relay.Conn.call

    async def observe_call(conn, body, timeout):
        if body.get("timeout") == 0.08:
            queued.set()
        return await original_call(conn, body, timeout)

    monkeypatch.setattr(relay.Conn, "call", observe_call)
    async with relay_client() as client:
        ws = await connect(client)
        first = asyncio.create_task(call(client, {"op": "exec", "timeout": 1}))
        first_request = await ws.receive_json(timeout=1)
        second = asyncio.create_task(call(client, {"op": "exec", "timeout": 0.08}))
        await asyncio.wait_for(queued.wait(), 1)
        ping = asyncio.create_task(call(client, {"op": "ping", "timeout": 1}))
        ping_request = await ws.receive_json(timeout=1)
        assert ping_request["op"] == "ping"
        await ws.send_json({"type": "result", "req": ping_request["req"], "ok": True})
        assert (await asyncio.wait_for(ping, 1))["ok"]
        # Keep the queue longer than the second request's round-trip budget.
        await asyncio.sleep(0.12)
        await ws.send_json({"type": "result", "req": first_request["req"], "ok": True})
        assert (await asyncio.wait_for(first, 1))["ok"]
        request = await ws.receive_json(timeout=1)
        rid = request["req"]
        assert (await asyncio.wait_for(second, 1))["error"]["kind"] == "relay_timeout"
        cancel = await ws.receive_json(timeout=1)
        assert cancel == {"type": "req", "req": -rid, "op": "cancel"}
        warnings = messages(caplog, logging.WARNING, "relay_timeout")
        assert len(warnings) == 1
        assert f"#3 exec req={rid}" in warnings[0]
        assert float(re.search(r"queued=([\d.]+)s", warnings[0])[1]) > 0
        assert "timeout=0.08s wait=0.08s cancel_queued=true" in warnings[0]
        assert len(messages(caplog, logging.WARNING, "queued cancel")) == 1
        summaries = messages(caplog, logging.INFO, "outcome=")
        assert len(summaries) == 3
        summary = next(line for line in summaries if "outcome=relay_timeout" in line)
        assert float(re.search(r"queued=([\d.]+)s", summary)[1]) > 0
        assert "resp_bytes=-" in summary
        assert "queued=0.00s" in next(line for line in summaries if " ping " in line)
        await ws.send_json({"type": "result", "req": rid, **relay.err("cancelled", "private-late-error")})
        late = (await wait_for_log(caplog, logging.WARNING, "answered late"))[0]
        assert f"req={rid} (exec)" in late and "after the relay gave up; outcome=cancelled" in late
        await ws.send_json({"type": "result", "req": -rid, "ok": True, "cancelled": True})
        ack = (await wait_for_log(caplog, logging.INFO, "cancel ack"))[0]
        assert f"req={-rid} outcome=ok cancelled=true" in ack
        await ws.send_json({"type": "result", "req": 999, "ok": True})
        assert "req=999" in (await wait_for_log(caplog, logging.WARNING, "unexpected reply"))[0]


@pytest.mark.asyncio
async def test_lock_free_timeout_does_not_queue_cancel(caplog, monkeypatch):
    monkeypatch.setattr(relay, "RELAY_TIMEOUT_MARGIN", 0.02)
    async with relay_client() as client:
        ws = await connect(client)
        task = asyncio.create_task(call(client, {"op": "ping", "timeout": 0.02}))
        await ws.receive_json(timeout=1)
        assert (await asyncio.wait_for(task, 1))["error"]["kind"] == "relay_timeout"
        warning = messages(caplog, logging.WARNING, "relay_timeout")[0]
        assert "queued=0.00s timeout=0.02s wait=0.04s cancel_queued=false" in warning
        assert not messages(caplog, logging.WARNING, "queued cancel")


@pytest.mark.asyncio
@pytest.mark.parametrize(("field", "op"), [("code", "exec"), ("command", "shell")])
async def test_debug_redaction_truncation_and_oversize(caplog, field, op):
    async with relay_client() as client:
        ws = await connect(client)
        code = "return '" + " ".join(relay.secret_values(TOKENS)) + "' " + "x" * 250
        task = asyncio.create_task(call(client, {"op": op, field: code, "timeout": 1}))
        request = await ws.receive_json(timeout=1)
        assert request[field] == code
        await ws.send_json({"type": "result", "req": request["req"], "ok": True, "output": TOKENS["api"]})
        assert (await asyncio.wait_for(task, 1))["output"] == TOKENS["api"]
        debug = messages(caplog, logging.DEBUG, f"{field}=")[0]
        assert "[REDACTED] [REDACTED]" in debug and debug.endswith(f'..." {field}_len={len(code)}')
        assert "x" * 201 not in debug
        body = {
            "op": "write",
            "path": TOKENS["computer"],
            "data": "private-file" * (relay.MAX_REQ_BYTES // len("private-file") + 1),
        }
        result = await call(client, body)
        assert result["error"]["kind"] == "too_large"
        warning = messages(caplog, logging.WARNING, "too_large")[0]
        assert "#3 write req=2" in warning
        assert f"req_bytes={len(relay.encode_request(2, body).encode())}" in warning
        assert f"limit={relay.MAX_REQ_BYTES}" in warning
        assert len(messages(caplog, logging.INFO, "outcome=too_large")) == 1
        assert f"data_len={len(body['data'])}" in messages(caplog, logging.DEBUG, "data_len=")[0]
        assert all("private-file" not in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_untrusted_protocol_names_are_not_logged(caplog):
    async with relay_client() as client:
        ws = await connect(client)
        task = asyncio.create_task(call(client, {"op": TOKENS["api"], "timeout": 1}))
        request = await ws.receive_json(timeout=1)
        await ws.send_json({"type": "result", "req": request["req"], **relay.err(TOKENS["computer"], "private")})
        assert (await asyncio.wait_for(task, 1))["error"]["kind"] == TOKENS["computer"]
        summary = messages(caplog, logging.INFO, "req=")[0]
        assert "#3 unknown req=" in summary and "outcome=unknown" in summary


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("hello", "reason"),
    [
        ("not-json", "not JSON"),
        (json.dumps({"type": "wrong", "id": 3, "token": TOKENS["api"]}), "not a hello"),
        (json.dumps({"type": "hello", "id": 3, "token": "WRONG-TOKEN-SECRET"}), "wrong token"),
        (json.dumps({"type": "hello", "id": 3, "token": TOKENS["api"]}), "wrong token"),
        (json.dumps({"type": "hello", "id": TOKENS["api"], "token": "wrong"}), "bad id"),
        (json.dumps({"type": "hello", "id": 3, "token": TOKENS["computers"]["4"]}), "token not valid for this id"),
        (json.dumps({"type": "hello", "id": 4, "token": TOKENS["computer"]}), "shared token not valid for this id"),
    ],
)
async def test_rejected_hello(caplog, hello, reason):
    async with relay_client() as client:
        async with client.ws_connect("/ws") as ws:
            await ws.send_str(hello)
            message = await ws.receive(timeout=1)
            assert message.type == WSMsgType.CLOSE and message.data == 4401
        warning = messages(caplog, logging.WARNING, "rejected hello")[0]
        assert "remote=127.0.0.1" in warning and f"reason={reason}" in warning
        if '"id": 3' in hello:
            assert "claimed_id=3" in warning
        assert "WRONG-TOKEN-SECRET" not in warning


@pytest.mark.asyncio
async def test_hello_timeout(caplog, monkeypatch):
    original_wait = asyncio.wait_for

    async def short_hello_wait(awaitable, timeout):
        return await original_wait(awaitable, 0.02 if timeout == 10 else timeout)

    monkeypatch.setattr(relay.asyncio, "wait_for", short_hello_wait)
    async with relay_client() as client:
        async with client.ws_connect("/ws") as ws:
            message = await ws.receive(timeout=1)
            assert message.type == WSMsgType.CLOSE and message.data == 4401
        assert "reason=no hello within 10 s" in messages(caplog, logging.WARNING, "rejected hello")[0]


@pytest.mark.asyncio
async def test_unauthorized_and_pre_target_rejections(caplog):
    async with relay_client() as client:
        async with client.post(
            "/api/call", json={"op": "exec"}, headers={"Authorization": "Bearer " + TOKENS["computer"]}
        ) as response:
            assert response.status == 401
        assert "remote=127.0.0.1" in messages(caplog, logging.WARNING, "unauthorized API request")[0]
        for body, kind, status in [
            ({"op": "ping"}, "no_target", 409),
            ({"op": "exec", "computer": 42}, "not_connected", 404),
        ]:
            async with client.post("/api/call", json=body, headers=HEADERS) as response:
                assert response.status == status
                assert (await response.json())["error"]["kind"] == kind
        async with client.post("/api/call", data="not-json " + TOKENS["api"], headers=HEADERS) as response:
            assert response.status == 400
            assert (await response.json())["error"]["kind"] == "bad_request"
        lines = messages(caplog, logging.INFO, "outcome=")
        assert len(lines) == 3
        assert "#- ping" in lines[0] and "outcome=no_target" in lines[0]
        assert "#42 exec" in lines[1] and "outcome=not_connected" in lines[1]
        assert "outcome=bad_request" in lines[2]


@pytest.mark.asyncio
async def test_replaced_connection_and_disconnect(caplog):
    async with relay_client() as client:
        ws = await connect(client)
        task = asyncio.create_task(call(client, {"op": "exec", "timeout": 1}))
        await ws.receive_json(timeout=1)
        replacement = asyncio.create_task(connect(client))
        assert (await ws.receive(timeout=1)).type == WSMsgType.CLOSE
        new_ws = await asyncio.wait_for(replacement, 1)
        assert (await asyncio.wait_for(task, 1))["error"]["kind"] == "disconnected"
        warning = messages(caplog, logging.WARNING, "replaced connection")[0]
        assert "computer #3" in warning
        assert "old_remote=127.0.0.1 new_remote=127.0.0.1 open=" in warning
        # connect() uses the API token as the (player-written) label: shown, but redacted.
        connected = messages(caplog, logging.INFO, " connected ")
        assert len(connected) == 2
        assert all(line == "computer #3 connected label='[REDACTED]' remote=127.0.0.1" for line in connected)
        assert len(messages(caplog, logging.INFO, "outcome=disconnected")) == 1
        await new_ws.close()
        await wait_for_log(caplog, logging.INFO, "disconnected remote=")
        for line in messages(caplog, logging.INFO, "disconnected remote="):
            assert "remote=127.0.0.1 open=" in line and "close_code=" in line


def test_late_reply_records_are_bounded(caplog):
    conn = relay.Conn(AsyncMock(), {"id": 3})
    for rid in range(relay.LATE_REPLY_LIMIT + 5):
        relay.remember(conn.timed_out, rid, ("exec", 0))
        relay.remember(conn.queued_cancels, -rid, True)
    assert len(conn.timed_out) == len(conn.queued_cancels) == relay.LATE_REPLY_LIMIT
    assert 0 not in conn.timed_out and 0 not in conn.queued_cancels
    conn.receive_result({"req": 0, "ok": True}, 10)
    assert messages(caplog, logging.WARNING, "unexpected reply req=0")


@pytest.mark.asyncio
async def test_reply_ids_match_like_before():
    # Replies used to be matched with pending.get(req), so an integral float id still matches.
    conn = relay.Conn(AsyncMock(), {"id": 3})
    loop = asyncio.get_running_loop()
    conn.pending = {12: loop.create_future(), 1: loop.create_future()}
    conn.receive_result({"req": 12.0, "ok": True}, 10)
    assert conn.pending[12].result() == {"req": 12.0, "ok": True}
    assert conn.response_sizes == {12: 10}
    for bad in (True, 1.5, "1", None):
        conn.receive_result({"req": bad, "ok": True}, 10)
    assert not conn.pending[1].done()
    assert relay.request_id(-4.0) == -4 and relay.request_id(False) is None


@pytest.mark.asyncio
async def test_nested_per_computer_tokens_are_redacted(caplog):
    # tokens.json may grow nested per-computer tokens; they must be redacted too, without crashing.
    tokens = {**TOKENS, "computers": {"3": "PER-COMPUTER-SECRET-3"}}
    assert set(relay.secret_values(tokens)) == {TOKENS["api"], TOKENS["computer"], "PER-COMPUTER-SECRET-3"}
    ws = AsyncMock()
    conn = relay.Conn(ws, {"id": 3}, tokens=tokens)

    async def reply(data):
        conn.pending[json.loads(data)["req"]].set_result({"ok": True})

    ws.send_str.side_effect = reply
    assert await conn._roundtrip({"op": "exec", "code": "x = 'PER-COMPUTER-SECRET-3' -- é"}, 1) == {"ok": True}
    debug = messages(caplog, logging.DEBUG, "code=")[0]
    assert "PER-COMPUTER-SECRET-3" not in debug
    assert "code=\"x = '[REDACTED]' -- \\xe9\" code_len=32" in debug and debug.isascii()


@pytest.fixture
def restore_logging():
    root = logging.getLogger()
    package = logging.getLogger("computercraft_live_mcp")
    handlers, level, package_level = root.handlers[:], root.level, package.level
    owned = relay._log_handlers[:]
    yield
    for handler in relay._log_handlers:
        if handler not in owned:
            handler.close()
    root.handlers[:] = handlers
    root.setLevel(level)
    package.setLevel(package_level)
    relay._log_handlers[:] = owned


def test_setup_logging_file_and_levels(tmp_path, capsys, restore_logging):
    path = tmp_path / "relay.log"
    path.write_text("existing\n", encoding="utf-8")
    root = logging.getLogger()
    handlers = root.handlers[:]
    relay.setup_logging("debug", str(path))
    first_handlers = relay._log_handlers[:]
    relay.setup_logging("DEBUG", str(path))
    assert len(root.handlers) == len(handlers) + 2
    assert all(handler not in root.handlers for handler in first_handlers)
    assert root.level == logging.WARNING
    assert logging.getLogger("computercraft_live_mcp").level == logging.DEBUG
    relay.log.debug("debug snippet é")
    relay.log.info("one request")
    logging.getLogger("aiohttp.access").info("access noise")
    logging.getLogger("asyncio").debug("debug noise")
    logging.getLogger("aiohttp.server").warning("server warning")
    contents = path.read_text(encoding="utf-8")
    assert contents.startswith("existing\n")
    assert contents.count("one request") == 1
    assert "debug snippet é" in contents and "server warning" in contents
    assert "noise" not in contents
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} INFO computercraft_live_mcp.relay:", contents)
    console = capsys.readouterr()
    assert "one request" in console.err and console.out == ""
    relay.setup_logging("ERROR", str(path))
    relay.log.info("hidden info")
    relay.log.error("visible error")
    assert "hidden info" not in path.read_text(encoding="utf-8")
    assert "visible error" in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("level", ["debug", "INFO", "Warning", "eRrOr"])
def test_log_level_parser(level):
    args = relay.build_parser().parse_args(["--log-level", level, "--log-file", "relay.log"])
    assert args.log_level == level.upper()
    assert args.log_file == "relay.log"


def test_log_level_parser_default_and_invalid():
    assert relay.build_parser().parse_args([]).log_level == "INFO"
    assert relay.build_parser().parse_args([]).log_file is None
    with pytest.raises(SystemExit):
        relay.build_parser().parse_args(["--log-level", "trace"])
