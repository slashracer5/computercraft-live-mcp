import io
import json
import sys

import pytest

from computercraft_mcp import bridge_client as bc
from computercraft_mcp import ccctl, mcp_server
from computercraft_mcp.formatting import format_result

pytestmark = pytest.mark.usefixtures("deterministic_ids")


class Stream(io.StringIO):
    def reconfigure(self, **kwargs):
        pass


CLI_CASES = [
    (["computers"], "computers", {"computers": [{"id": 0, "label": "lab", "host": "CC", "connected_for_s": 1}]}),
    (["ping"], "ping", {"ok": True, "id": 0, "label": "lab"}),
    (["cancel"], "cancel", {"ok": True, "cancelled": True}),
    (["exec", "return 42"], "exec", {"ok": True, "returns": ["42"]}),
    (["shell", "ls"], "shell", {"ok": True, "output": "rom"}),
    (["read", "test.txt"], "read", {"ok": True, "data": "hello", "eof": True}),
    (["ls"], "list", {"ok": True, "entries": [{"name": "rom", "isDir": True}]}),
    (["periph"], "peripherals", {"ok": True, "peripherals": [{"name": "left", "types": ["modem"]}]}),
    (["log"], "log", {"ok": True, "lines": ["hello"], "last_error_id": 0}),
]


@pytest.mark.parametrize(("argv", "op", "res"), CLI_CASES, ids=[case[1] for case in CLI_CASES])
@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("failed", [False, True])
def test_cli_formatting_and_exit_status(monkeypatch, argv, op, res, as_json, failed, deterministic_ids):
    if failed:
        res = {"ok": False, "error": {"kind": "not_connected", "message": "offline"}}
    output = Stream()
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setattr(sys, "stdin", Stream("hello"))
    monkeypatch.setattr(sys, "argv", ["ccctl", *argv, *(["--json"] if as_json else [])])
    requested = []

    def call(operation, *args, **kwargs):
        requested.append(operation)
        return res

    def request(path):
        assert path == "/api/computers"
        requested.append("computers")
        return res

    monkeypatch.setattr(bc, "call", call)
    monkeypatch.setattr(bc, "request", request)
    status = ccctl.main()
    assert requested == [op]
    assert status == (1 if failed else 0)
    deterministic_ids()
    expected = json.dumps(res, indent=2) if as_json else format_result(op, res)
    assert output.getvalue() == expected + "\n"


MCP_CASES = [
    ("cc_computers", {}, "computers"),
    ("cc_ping", {}, "ping"),
    ("cc_exec", {"code": "return 42"}, "exec"),
    ("cc_shell", {"command": "ls"}, "shell"),
    ("cc_read", {"path": "test.txt"}, "read"),
    ("cc_write", {"path": "test.txt", "content": "hello"}, "write"),
    ("cc_list", {}, "list"),
    ("cc_peripherals", {}, "peripherals"),
    ("cc_log", {}, "log"),
    ("cc_cancel", {}, "cancel"),
    ("cc_network", {}, "network"),
    ("cc_call", {"peripheral": "left", "method": "getX"}, "calls"),
    ("cc_batch", {"calls": [{"peripheral": "left", "method": "getX"}]}, "calls"),
    ("cc_push", {"local_path": "local.txt", "remote_path": "remote.txt"}, "push"),
    ("cc_pull", {"local_path": "local.txt", "remote_path": "remote.txt"}, "pull"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool", "args", "op"), MCP_CASES, ids=[case[0] for case in MCP_CASES])
@pytest.mark.parametrize("failed", [False, True])
async def test_mcp_tools_delegate_all_formatting(monkeypatch, tool, args, op, failed):
    res = {"ok": False, "error": {"kind": "local_file", "message": "denied"}} if failed else {"ok": True}
    for name in ("call", "request", "push_file", "pull_file"):
        monkeypatch.setattr(bc, name, lambda *args, **kwargs: res)
    formatted = []

    def formatter(operation, result, **context):
        assert operation == op
        assert result is res
        formatted.append(context)
        return "formatted"

    monkeypatch.setattr(mcp_server, "format_result", formatter)
    assert await getattr(mcp_server, tool)(**args) == "formatted"
    assert len(formatted) == 1
    if op == "calls":
        assert formatted[0]["calls"][0]["peripheral"] == "left"
        assert formatted[0]["calls"][0]["method"] == "getX"
    else:
        assert formatted == [{}]


@pytest.mark.asyncio
@pytest.mark.parametrize("calls", [[{}], [None], [{"peripheral": "left"}], [{"method": "getX"}]])
async def test_invalid_calls_use_formatter_without_relay(monkeypatch, calls):
    def no_call(*args, **kwargs):
        pytest.fail("invalid calls must not reach the relay")

    monkeypatch.setattr(bc, "call", no_call)
    formatted = []

    def formatter(op, res, **context):
        formatted.append((op, res))
        return format_result(op, res, **context)

    monkeypatch.setattr(mcp_server, "format_result", formatter)
    assert await mcp_server.cc_batch(calls) == (
        'ERROR\n  error: bad_request\n<untrusted source="error" id="00000001">\n'
        'each call needs {"peripheral": name, "method": name, "args": [...]}\n</untrusted id="00000001">'
    )
    assert len(formatted) == 1
    assert formatted[0][0] == "calls"
    assert formatted[0][1]["ok"] is False
