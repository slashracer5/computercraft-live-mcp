#!/usr/bin/env python3
"""Command-line client for the ComputerCraft MCP relay: one subcommand per MCP tool, printing the same text.

  ccctl computers                                    cc_computers
  ccctl ping                                         cc_ping
  ccctl exec  "os.getComputerID()" [--reset]         cc_exec (expression or statements)
  ccctl shell "ls rom"                               cc_shell
  ccctl read  startup.lua [--offset N]               cc_read
  ccctl write startup.lua [--file local.lua] [--append]
                                                     cc_write (text on stdin without --file)
  ccctl ls    [path]                                 cc_list
  ccctl periph [--methods]                           cc_peripherals
  ccctl log   [--lines N] [--errors-only] [--since-error ID]
                                                     cc_log
  ccctl cancel                                       cc_cancel
  ccctl network [--methods]                          cc_network
  ccctl call  PERIPHERAL METHOD [ARG ...]            cc_call (each ARG is JSON: 5, true, '"5"'; else a string)
  ccctl batch [CALLS | --file calls.json] [--sequential]
                                                     cc_batch (CALLS: a JSON list as for cc_batch; or stdin)
  ccctl push  local.lua remote.lua [--append]        cc_push
  ccctl pull  remote.log local.log                   cc_pull
  ccctl find  "programs/*" [--max N]                 cc_find (fs.find glob: * stays within one path segment)
  ccctl grep  PATTERN [PATH] [--lua] [--max N] [--include-rom]
                                                     cc_grep (plain text unless --lua; PATH "" = everything)
  ccctl stat  PATH                                   cc_stat
  ccctl rm    PATH [--recursive]                     cc_delete (a non-empty directory needs --recursive)
  ccctl mv    SRC DST                                cc_move (DST is the new full path; never overwrites)
  ccctl cp    SRC DST                                cc_copy (never overwrites)
  ccctl mkdir PATH                                   cc_mkdir (parents included)
  ccctl update-bridge                                upload the packaged bridge.lua (then restart the bridge)

Shared flags (before or after the subcommand): --json  --computer ID  --timeout SECONDS
Env: CC_MCP_URL, CC_MCP_API_TOKEN, CC_MCP_HOME (see bridge_client.py).
write, push and update-bridge upload in chunks like cc_push: any size, and an overwrite is atomic.
Local paths are used as typed: CC_MCP_FILES_ROOT only limits the MCP tools.
Exit status is 1 when the relay or the operation fails (call/batch: when any call fails), else 0.
"""

import argparse
import json
import sys
from pathlib import Path

from . import bridge_client as bc
from . import cc_lua
from .formatting import format_result

RESTART_NOTE = (
    "The running bridge keeps its old code until it restarts: reboot the computer if startup.lua starts the bridge\n"
    '(`ccctl exec "os.reboot()"`, which reports `disconnected`), or type `exit` in its shell and run `bridge` again.'
)


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="print the raw result")
    common.add_argument("--computer", type=int, default=argparse.SUPPRESS, help="computer id (several connected)")
    common.add_argument("--timeout", type=float, default=argparse.SUPPRESS, help="seconds per request")
    p = argparse.ArgumentParser(
        prog="ccctl", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter, parents=[common]
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("computers", "ping", "cancel"):
        sub.add_parser(name, parents=[common])
    s = sub.add_parser("exec", parents=[common])
    s.add_argument("code")
    s.add_argument("--reset", action="store_true")
    s = sub.add_parser("shell", parents=[common])
    s.add_argument("command")
    s = sub.add_parser("read", parents=[common])
    s.add_argument("path")
    s.add_argument("--offset", type=int, default=0)
    s = sub.add_parser("write", parents=[common])
    s.add_argument("path")
    s.add_argument("--file", help="local file to upload (default: stdin)")
    s.add_argument("--append", action="store_true")
    s = sub.add_parser("ls", parents=[common])
    s.add_argument("path", nargs="?", default="")
    for name in ("periph", "network"):
        s = sub.add_parser(name, parents=[common])
        s.add_argument("--methods", action="store_true")
    s = sub.add_parser("log", parents=[common])
    s.add_argument("--lines", type=int, default=50)
    s.add_argument("--errors-only", action="store_true")
    s.add_argument("--since-error", type=int, default=0)
    s = sub.add_parser("call", parents=[common])
    s.add_argument("peripheral")
    s.add_argument("method")
    s.add_argument("args", nargs="*", metavar="ARG", help="JSON value (5, true, '\"text\"'), else a plain string")
    s = sub.add_parser("batch", parents=[common])
    source = s.add_mutually_exclusive_group()
    source.add_argument("calls", nargs="?", help='JSON list: [{"peripheral": ..., "method": ..., "args": [...]}]')
    source.add_argument("--file", help="read the JSON list from this file (default: stdin)")
    s.add_argument("--sequential", action="store_true", help="one call after another (cc_batch parallel=False)")
    s = sub.add_parser("push", parents=[common])
    s.add_argument("local")
    s.add_argument("remote")
    s.add_argument("--append", action="store_true")
    s = sub.add_parser("pull", parents=[common])
    s.add_argument("remote")
    s.add_argument("local")
    s = sub.add_parser("find", parents=[common])
    s.add_argument("pattern")
    s.add_argument("--max", type=int, default=200, dest="max_results", help="most paths to list (up to 1000)")
    s = sub.add_parser("grep", parents=[common])
    s.add_argument("pattern")
    s.add_argument("path", nargs="?", default="", help="directory (recursive) or file; default: everything")
    s.add_argument("--lua", action="store_true", help="PATTERN is a Lua pattern (default: plain text)")
    s.add_argument("--max", type=int, default=200, dest="max_matches", help="stop after N matches (up to 1000)")
    s.add_argument("--include-rom", action="store_true", help="also search rom/")
    for name in ("stat", "mkdir"):
        sub.add_parser(name, parents=[common]).add_argument("path")
    s = sub.add_parser("rm", parents=[common])
    s.add_argument("path")
    s.add_argument("-r", "--recursive", action="store_true", help="delete a non-empty directory with its contents")
    for name in ("mv", "cp"):
        s = sub.add_parser(name, parents=[common])
        s.add_argument("src")
        s.add_argument("dst", help="the new full path (must not exist)")
    sub.add_parser("update-bridge", parents=[common])
    return p


def _json_or_text(value: str):
    try:
        return json.loads(value)
    except ValueError:
        return value


def _stdin_text() -> str:
    sys.stdin.reconfigure(encoding="utf-8")
    return sys.stdin.read()


def _load_calls(a) -> tuple[object, dict | None]:
    """The batch's decoded JSON (checked by run_calls), or (None, error result)."""
    try:
        if a.file:
            text = Path(a.file).read_text(encoding="utf-8")
        elif a.calls is not None:
            text = a.calls
        else:
            text = _stdin_text()
    except (OSError, UnicodeDecodeError) as e:
        return None, {"ok": False, "error": {"kind": "local_file", "message": str(e)}}
    try:
        return json.loads(text), None
    except ValueError as e:
        return None, {"ok": False, "error": {"kind": "bad_request", "message": f"calls are not valid JSON: {e}"}}


def run(a, computer: int | None, timeout: float) -> tuple[str, dict, dict]:
    """Do one subcommand; returns (formatter op, raw result, formatter context)."""
    cmd = a.cmd
    if cmd == "computers":
        return "computers", bc.request("/api/computers"), {}
    if cmd in ("ping", "cancel"):
        return cmd, bc.call(cmd, computer, timeout), {}
    if cmd == "exec":
        return "exec", bc.call("exec", computer, timeout, code=a.code, reset=a.reset or None), {}
    if cmd == "shell":
        return "shell", bc.call("shell", computer, timeout, command=a.command), {}
    if cmd == "read":
        return "read", bc.call("read", computer, timeout, path=a.path, offset=a.offset), {}
    if cmd == "write":  # chunked like push, so large files work
        if a.file:
            return "push", bc.push_file(a.file, a.path, computer, timeout, a.append, check_roots=False), {}
        return "push", bc.push_text(_stdin_text(), a.path, computer, timeout, a.append), {}
    if cmd == "ls":
        return "list", bc.call("list", computer, timeout, path=a.path), {}
    if cmd == "periph":
        return "peripherals", bc.call("peripherals", computer, timeout, methods=a.methods or None), {}
    if cmd == "log":
        res = bc.call(
            "log", computer, timeout, lines=a.lines, errors_only=a.errors_only or None, since_error=a.since_error
        )
        return "log", res, {}
    if cmd == "network":
        return "network", bc.network(a.methods, computer, timeout), {}
    if cmd == "call":
        calls = [{"peripheral": a.peripheral, "method": a.method, "args": [_json_or_text(v) for v in a.args]}]
        return "calls", bc.run_calls(calls, False, computer, timeout), {"calls": calls}
    if cmd == "batch":
        calls, error = _load_calls(a)
        if error:
            return "calls", error, {}
        return "calls", bc.run_calls(calls, not a.sequential, computer, timeout), {"calls": calls}
    if cmd == "push":
        return "push", bc.push_file(a.local, a.remote, computer, timeout, a.append, check_roots=False), {}
    if cmd == "pull":
        return "pull", bc.pull_file(a.remote, a.local, computer, timeout, check_roots=False), {}
    if cmd == "update-bridge":
        return "push", bc.push_file(str(bc.BRIDGE_LUA), "bridge.lua", computer, timeout, check_roots=False), {}
    if cmd == "find":
        return "find", bc.fs_find(a.pattern, a.max_results, computer, timeout), {}
    if cmd == "grep":
        res = bc.fs_grep(a.pattern, a.path, not a.lua, a.max_matches, a.include_rom, computer, timeout)
        return "grep", res, {}
    if cmd == "stat":
        return "stat", bc.fs_stat(a.path, computer, timeout), {}
    if cmd == "rm":
        return "delete", bc.fs_delete(a.path, a.recursive, computer, timeout), {}
    if cmd == "mv":
        return "move", bc.fs_move(a.src, a.dst, computer, timeout), {}
    if cmd == "cp":
        return "copy", bc.fs_copy(a.src, a.dst, computer, timeout), {}
    if cmd == "mkdir":
        return "mkdir", bc.fs_mkdir(a.path, computer, timeout), {}
    raise AssertionError(cmd)


def succeeded(op: str, res: dict) -> bool:
    if "ok" not in res:  # GET /api/computers answers {"computers": [...]} without "ok"
        return isinstance(res.get("computers"), list) and not res.get("error")
    if res["ok"] is not True:
        return False
    if op == "calls":  # the exec ran; each call carries its own result
        doc = cc_lua.parse_returned_json(res)
        results = doc.get("results") if isinstance(doc, dict) else None
        if results is None:
            return False
        return all(isinstance(r, dict) and r.get("ok") is True for r in (results if isinstance(results, list) else []))
    return True


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    a = build_parser().parse_args(argv)
    as_json = getattr(a, "json", False)
    computer = getattr(a, "computer", None)
    timeout = getattr(a, "timeout", 60.0 if a.cmd == "network" else 30.0)  # as the MCP tools
    op, res, context = run(a, computer, timeout)
    ok = succeeded(op, res)
    print(json.dumps(res, indent=2) if as_json else format_result(op, res, **context))
    if a.cmd == "update-bridge" and ok and not as_json:
        print(RESTART_NOTE)
    return 0 if ok else 1


def cli() -> None:
    sys.exit(main())


if __name__ == "__main__":
    cli()
