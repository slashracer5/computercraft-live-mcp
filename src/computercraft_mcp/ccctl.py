#!/usr/bin/env python3
"""Command-line client for the ComputerCraft MCP relay (the MCP server exposes the same calls as tools).

  ccctl computers
  ccctl ping
  ccctl exec  "os.getComputerID()"        # expression or statements
  ccctl shell "ls rom"
  ccctl read  startup.lua [--offset N]
  ccctl write startup.lua --file local.lua [--append]     # or text on stdin
  ccctl ls    [path]
  ccctl periph [--methods]
  ccctl log   [--lines N] [--errors-only] [--since-error ID]
  ccctl cancel

Shared flags (before or after the subcommand): --json  --computer ID  --timeout SECONDS
Env: CC_MCP_URL, CC_MCP_API_TOKEN, CC_MCP_HOME (see bridge_client.py).
Exit status is 1 on failure, except `computers`, which always exits 0.
"""

import argparse
import json
import sys
from pathlib import Path

from . import bridge_client as bc
from .formatting import format_result


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    common.add_argument("--computer", type=int, default=argparse.SUPPRESS)
    common.add_argument("--timeout", type=float, default=argparse.SUPPRESS)
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter, parents=[common]
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
    s.add_argument("--file")
    s.add_argument("--append", action="store_true")
    s = sub.add_parser("ls", parents=[common])
    s.add_argument("path", nargs="?", default="")
    s = sub.add_parser("periph", parents=[common])
    s.add_argument("--methods", action="store_true")
    s = sub.add_parser("log", parents=[common])
    s.add_argument("--lines", type=int, default=50)
    s.add_argument("--errors-only", action="store_true")
    s.add_argument("--since-error", type=int, default=0)
    a = p.parse_args()
    as_json = getattr(a, "json", False)
    computer = getattr(a, "computer", None)
    timeout = getattr(a, "timeout", 30.0)

    if a.cmd == "computers":
        res = bc.request("/api/computers")
    elif a.cmd in ("ping", "cancel"):
        res = bc.call(a.cmd, computer, timeout)
    elif a.cmd == "exec":
        res = bc.call("exec", computer, timeout, code=a.code, reset=a.reset or None)
    elif a.cmd == "shell":
        res = bc.call("shell", computer, timeout, command=a.command)
    elif a.cmd == "read":
        res = bc.call("read", computer, timeout, path=a.path, offset=a.offset)
    elif a.cmd == "write":
        sys.stdin.reconfigure(encoding="utf-8")
        data = Path(a.file).read_text(encoding="utf-8") if a.file else sys.stdin.read()
        res = bc.call("write", computer, timeout, path=a.path, data=data, append=a.append)
    elif a.cmd == "ls":
        res = bc.call("list", computer, timeout, path=a.path)
    elif a.cmd == "periph":
        res = bc.call("peripherals", computer, timeout, methods=a.methods or None)
    elif a.cmd == "log":
        res = bc.call(
            "log", computer, timeout, lines=a.lines, errors_only=a.errors_only or None, since_error=a.since_error
        )
    op = {"ls": "list", "periph": "peripherals"}.get(a.cmd, a.cmd)
    print(json.dumps(res, indent=2) if as_json else format_result(op, res))
    if a.cmd == "computers":
        return 0
    return 0 if res.get("ok", False) else 1


def cli() -> None:
    sys.exit(main())


if __name__ == "__main__":
    cli()
