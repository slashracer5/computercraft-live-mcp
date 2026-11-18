"""ccctl (#16): a subcommand for every MCP tool, the same text, exit status, --json, chunked writes, update-bridge."""

import io
import json
import sys
from pathlib import Path

import pytest

from computercraft_mcp import bridge_client as bc
from computercraft_mcp import cc_lua, ccctl, mcp_server

pytestmark = pytest.mark.usefixtures("deterministic_ids")

REFERENCE = Path(__file__).resolve().parents[1] / "docs" / "REFERENCE.md"

# Every MCP tool and the ccctl subcommand that does the same. A new tool fails the parity test until it has a
# subcommand, a row here, and a line in `ccctl --help` and docs/REFERENCE.md.
TOOL_TO_SUBCOMMAND = {
    "cc_computers": "computers",
    "cc_ping": "ping",
    "cc_exec": "exec",
    "cc_shell": "shell",
    "cc_read": "read",
    "cc_write": "write",
    "cc_list": "ls",
    "cc_peripherals": "periph",
    "cc_log": "log",
    "cc_cancel": "cancel",
    "cc_network": "network",
    "cc_call": "call",
    "cc_batch": "batch",
    "cc_push": "push",
    "cc_pull": "pull",
}


class Stream(io.StringIO):
    def reconfigure(self, **kwargs):
        pass


@pytest.fixture
def cli(monkeypatch):
    def run(*argv, stdin=""):
        out = Stream()
        monkeypatch.setattr(sys, "stdout", out)
        monkeypatch.setattr(sys, "stdin", Stream(stdin))
        return ccctl.main(list(argv)), out.getvalue()

    return run


def exec_reply(doc) -> dict:
    return {"ok": True, "returns": [json.dumps(json.dumps(doc))]}


class Drive:
    """Stands in for bridge_client.call: files on the computer plus the cc_push check/swap steps (their snippet
    builders are patched to emit JSON tags; the Lua itself runs in tests/selftest_cc.py)."""

    def __init__(self):
        self.files = {}
        self.log = []

    def steps(self):
        return [entry[0] for entry in self.log]

    def __call__(self, op, computer=None, timeout=30, **args):
        if op == "write":
            path = args["path"]
            self.log.append(("write", path, args.get("append"), computer))
            self.files[path] = (self.files.get(path, "") if args.get("append") else "") + args["data"]
            return {"ok": True, "size": len(self.files[path].encode())}
        if op == "read":
            self.log.append(("read", args["path"], args.get("offset"), computer))
            data = self.files[args["path"]].encode()
            return {"ok": True, "data": data.decode(), "eof": True, "next_offset": len(data)}
        assert op == "exec"
        step, *params = json.loads(args["code"])
        self.log.append((step, *params, computer))
        if step == "finalise":
            target, part = params
            self.files[target] = self.files.pop(part)
            return exec_reply({"ok": True, "size": len(self.files[target].encode())})
        if step == "discard":
            self.files.pop(params[0], None)
            return exec_reply({"gone": True})
        return exec_reply({"ok": True})


@pytest.fixture
def drive(monkeypatch):
    fake = Drive()
    monkeypatch.setattr(cc_lua, "push_preflight_code", lambda target, part, need: json.dumps(["preflight", target]))
    monkeypatch.setattr(cc_lua, "push_finalise_code", lambda target, part: json.dumps(["finalise", target, part]))
    monkeypatch.setattr(cc_lua, "push_discard_code", lambda part: json.dumps(["discard", part]))
    monkeypatch.setattr(bc, "call", fake)
    monkeypatch.delenv("CC_MCP_FILES_ROOT", raising=False)
    return fake


# ---------------------------------------------------------------------------------- parity
@pytest.mark.asyncio
async def test_every_mcp_tool_has_a_ccctl_subcommand(capsys):
    tools = {tool.name for tool in await mcp_server.server.list_tools()}
    assert tools - TOOL_TO_SUBCOMMAND.keys() == set(), "new MCP tool: add a ccctl subcommand and a row above"
    assert TOOL_TO_SUBCOMMAND.keys() - tools == set(), "stale row: the tool no longer exists"
    parser = ccctl.build_parser()
    help_text = parser.format_help()
    reference = REFERENCE.read_text(encoding="utf-8").splitlines()
    for tool, cmd in TOOL_TO_SUBCOMMAND.items():
        with pytest.raises(SystemExit) as exited:
            parser.parse_args([cmd, "--help"])
        assert exited.value.code == 0, cmd
        assert f"  ccctl {cmd}" in help_text and tool in help_text, f"`ccctl --help` must list {cmd} ({tool})"
        assert any(f"`{tool}`" in line and f"`ccctl {cmd}" in line for line in reference), (
            f"docs/REFERENCE.md needs a line pairing `{tool}` with `ccctl {cmd}`"
        )


CALLS = [{"peripheral": "left", "method": "getX"}, {"peripheral": "right", "method": "setY", "args": [1, "on"]}]
CALLS_DOC = {"results": [{"ok": True, "values": [5]}, {"ok": True, "values": []}], "elapsed_ms": 3, "n": 2}
NETWORK_DOC = {"devices": [{"name": "left", "types": ["modem"], "methods": ["isWireless"]}], "modems": []}
SAME_CASES = [
    ("cc_network", {"methods": True}, ["network", "--methods"], NETWORK_DOC),
    ("cc_call", {"peripheral": "right", "method": "setY", "args": [1, "on"]}, ["call", "right", "setY", "1", "on"], {}),
    ("cc_batch", {"calls": CALLS}, ["batch", json.dumps(CALLS)], CALLS_DOC),
    ("cc_batch", {"calls": CALLS, "parallel": False}, ["batch", json.dumps(CALLS), "--sequential"], CALLS_DOC),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool", "args", "argv", "doc"), SAME_CASES)
async def test_ccctl_sends_and_prints_what_the_mcp_tool_does(
    monkeypatch, cli, deterministic_ids, tool, args, argv, doc
):
    doc = doc or {"results": [{"ok": True, "values": [42]}], "elapsed_ms": 1, "n": 1}
    sent = []

    def call(op, computer=None, timeout=30, **kw):
        sent.append((op, computer, timeout, kw))
        return {**exec_reply(doc), "duration": 0.25}

    monkeypatch.setattr(bc, "call", call)
    expected = await getattr(mcp_server, tool)(**args)
    deterministic_ids()
    status, out = cli(*argv)
    assert status == 0
    assert out == expected + "\n"
    assert len(sent) == 2 and sent[0] == sent[1]


# ---------------------------------------------------------------------------------- exit status and --json
COMPUTERS_CASES = [
    ({"computers": [{"id": 0, "label": "lab", "host": "CC", "connected_for_s": 1}]}, 0),
    ({"computers": []}, 0),
    ({"ok": False, "error": {"kind": "relay_unreachable", "message": "http://127.0.0.1:8765: refused"}}, 1),
    ({"ok": False, "error": {"kind": "unauthorized", "message": "bad API token"}}, 1),
]


@pytest.mark.parametrize(("res", "status"), COMPUTERS_CASES)
@pytest.mark.parametrize("as_json", [False, True])
def test_computers_fails_when_the_relay_does_and_honours_json(monkeypatch, cli, res, status, as_json):
    monkeypatch.setattr(bc, "request", lambda path: res)
    code, out = cli("computers", *(["--json"] if as_json else []))
    assert code == status
    if as_json:
        assert json.loads(out) == res
    else:
        assert out.startswith("ERROR" if status else "OK")
        assert "<untrusted" in out or "No computers connected." in out


@pytest.mark.parametrize(
    ("doc", "status"),
    [
        ({"results": [{"ok": True, "values": [1]}], "n": 1}, 0),
        ({"results": {}, "n": 0}, 0),  # CC serialises an empty Lua array as {}
        ({"results": [{"ok": True, "values": []}, {"ok": False, "error": "bad argument"}], "n": 2}, 1),
        ({"something": "else"}, 1),
    ],
)
def test_call_and_batch_fail_when_any_call_fails(monkeypatch, cli, doc, status):
    monkeypatch.setattr(bc, "call", lambda *args, **kwargs: exec_reply(doc))
    assert cli("call", "left", "getX")[0] == status
    assert cli("batch", stdin=json.dumps(CALLS))[0] == status


def test_call_parses_each_argument_as_json_or_keeps_the_string(monkeypatch, cli):
    seen = []
    monkeypatch.setattr(bc, "run_calls", lambda calls, parallel, computer, timeout: seen.append(calls) or {"ok": 0})
    cli("call", "mon", "write", "5", "-2.5", "true", "null", '"7"', "[1, 2]", "plain text", "--computer", "3")
    assert seen == [
        [{"peripheral": "mon", "method": "write", "args": [5, -2.5, True, None, "7", [1, 2], "plain text"]}]
    ]


def test_batch_reads_the_list_from_an_argument_a_file_or_stdin(monkeypatch, cli, tmp_path):
    seen = []
    monkeypatch.setattr(bc, "run_calls", lambda *args: seen.append(args) or exec_reply(CALLS_DOC))
    path = tmp_path / "calls.json"
    path.write_text(json.dumps(CALLS), encoding="utf-8")
    assert cli("batch", json.dumps(CALLS))[0] == 0
    assert cli("batch", "--file", str(path), "--sequential", "--computer", "2", "--timeout", "5")[0] == 0
    assert cli("batch", stdin=json.dumps(CALLS))[0] == 0
    assert seen == [(CALLS, True, None, 30.0), (CALLS, False, 2, 5.0), (CALLS, True, None, 30.0)]


@pytest.mark.parametrize(
    "calls",
    ["not json", "{}", "[{}]", '[{"peripheral": "left"}]', '[{"peripheral": "left", "method": "m", "args": "x"}]'],
)
def test_malformed_batch_fails_before_reaching_the_relay(monkeypatch, cli, calls):
    monkeypatch.setattr(bc, "call", lambda *args, **kwargs: pytest.fail("a malformed batch must not be sent"))
    status, out = cli("batch", calls)
    assert status == 1
    assert out.startswith("ERROR\n  error: bad_request\n")


@pytest.mark.parametrize(
    "calls",
    [{}, "x", [None], [{"method": "m"}], [{"peripheral": "", "method": "m"}], [{"peripheral": 5, "method": "m"}]],
)
def test_run_calls_validates_before_reaching_the_relay(monkeypatch, calls):
    monkeypatch.setattr(bc, "call", lambda *args, **kwargs: pytest.fail("malformed calls must not be sent"))
    assert bc.run_calls(calls) == {"ok": False, "error": {"kind": "bad_request", "message": bc.CALL_SHAPE}}


def test_network_defaults_to_the_mcp_tools_timeout(monkeypatch, cli):
    seen = []
    monkeypatch.setattr(bc, "call", lambda op, computer, timeout, **kw: seen.append((op, timeout, kw)) or {"ok": 1})
    cli("network")
    cli("network", "--methods", "--timeout", "9")
    assert [(op, timeout) for op, timeout, _ in seen] == [("exec", 60.0), ("exec", 9.0)]
    assert seen[0][2]["code"] == cc_lua.network_code(False)
    assert seen[1][2]["code"] == cc_lua.network_code(True)


# ---------------------------------------------------------------------------------- write / push / pull
def test_write_file_uploads_a_large_file_in_chunks_without_files_root(cli, drive, tmp_path):
    big = tmp_path / "big.lua"
    big.write_text("x" * 150_000, encoding="utf-8")
    drive.files["big.lua"] = "old"
    status, out = cli("write", "big.lua", "--file", str(big))
    assert status == 0
    assert drive.files == {"big.lua": "x" * 150_000}
    assert drive.steps() == ["preflight", "write", "write", "write", "finalise"]
    assert {entry[1] for entry in drive.log if entry[0] == "write"} == {"big.lua.part"}
    assert out.splitlines()[1] == "uploaded 150000 chars in 3 chunk(s); remote size 150000 bytes"


def test_write_from_stdin_and_append(cli, drive):
    assert cli("write", "notes.txt", stdin="héllo\n")[0] == 0
    assert drive.files == {"notes.txt": "héllo\n"}
    drive.log.clear()
    status, out = cli("write", "notes.txt", "--append", "--json", stdin="more")
    assert status == 0
    assert drive.files["notes.txt"] == "héllo\nmore"
    assert drive.steps() == ["preflight", "write"]  # append writes straight to the file
    assert json.loads(out)["sent_chars"] == 4


def test_write_reports_a_missing_local_file(cli, drive, tmp_path):
    status, out = cli("write", "x.lua", "--file", str(tmp_path / "missing.lua"))
    assert status == 1
    assert "  error: local_file" in out
    assert drive.log == []


def test_push_and_pull_use_the_typed_paths_but_the_mcp_side_still_checks(monkeypatch, cli, drive, tmp_path):
    src = tmp_path / "prog.lua"
    src.write_bytes(b"print('hi')\r\n")
    status, out = cli("push", str(src), "prog.lua", "--computer", "4")
    assert status == 0
    assert drive.files == {"prog.lua": "print('hi')\r\n"}  # exact newlines
    assert {entry[-1] for entry in drive.log} == {4}
    monkeypatch.chdir(tmp_path)
    status, out = cli("pull", "prog.lua", "back/prog.lua")
    dest = (tmp_path / "back" / "prog.lua").resolve()
    assert status == 0
    assert dest.read_bytes() == b"print('hi')\r\n"
    assert out == f"OK\nsaved 13 bytes to {dest}\n"
    # The MCP tools call the same functions without check_roots=False: still refused without CC_MCP_FILES_ROOT.
    assert bc.push_file(str(src), "prog.lua")["error"]["kind"] == "local_file"
    assert bc.pull_file("prog.lua", str(dest))["error"]["kind"] == "local_file"


# ---------------------------------------------------------------------------------- update-bridge
def test_update_bridge_uploads_the_packaged_bridge_atomically(cli, drive):
    with open(bc.BRIDGE_LUA, encoding="utf-8", newline="") as fh:
        packaged = fh.read()
    assert "bridge" in packaged
    drive.files["bridge.lua"] = "-- old bridge"
    status, out = cli("update-bridge", "--computer", "7")
    assert status == 0
    assert drive.files == {"bridge.lua": packaged}
    assert drive.steps()[0] == "preflight" and drive.steps()[-1] == "finalise"
    assert {entry[-1] for entry in drive.log} == {7}
    assert out.endswith("\n" + ccctl.RESTART_NOTE + "\n")
    status, out = cli("update-bridge", "--json")
    assert status == 0
    assert json.loads(out)["ok"] is True  # --json prints only the result


def test_update_bridge_failure_exits_1_without_the_restart_note(monkeypatch, cli):
    offline = {"ok": False, "error": {"kind": "not_connected", "message": "computer 7 is not connected"}}
    monkeypatch.setattr(bc, "call", lambda *args, **kwargs: offline)
    status, out = cli("update-bridge")
    assert status == 1
    assert "  error: not_connected" in out
    assert ccctl.RESTART_NOTE not in out
