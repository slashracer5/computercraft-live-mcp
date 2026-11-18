"""Shared client code for the CC bridge relay: HTTP calls and result formatting.

Used by ccctl.py (command line) and mcp_server.py (MCP tools).
Env: CC_MCP_URL (default http://127.0.0.1:8765), CC_MCP_API_TOKEN,
CC_MCP_HOME (default ~/.computercraft-mcp, holds tokens.json).
"""

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

HOME = Path(os.environ.get("CC_MCP_HOME", Path.home() / ".computercraft-mcp"))
URL = os.environ.get("CC_MCP_URL", "http://127.0.0.1:8765").rstrip("/")


def api_token() -> str:
    if os.environ.get("CC_MCP_API_TOKEN"):
        return os.environ["CC_MCP_API_TOKEN"]
    return json.loads((HOME / "tokens.json").read_text())["api"]


def request(path: str, body: dict | None = None) -> dict:
    """GET (no body) or POST (JSON body) to the relay. Never raises for relay-side errors."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        URL + path,
        data=data,
        method="POST" if data else "GET",
        headers={"Authorization": "Bearer " + api_token(), "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=float((body or {}).get("timeout", 30)) + 15) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return json.load(e)
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        return {"ok": False, "error": {"kind": "relay_unreachable", "message": f"{URL}: {e}"}}


def call(op: str, computer: int | None = None, timeout: float = 30, **args) -> dict:
    body = {"op": op, "timeout": timeout, **{k: v for k, v in args.items() if v is not None}}
    if computer is not None:
        body["computer"] = computer
    return request("/api/call", body)


HINTS = {
    "relay_unreachable": "The relay is not running. Start it with `computercraft-mcp-relay` (leave it running), "
    "then retry. The in-game computer reconnects by itself within seconds.",
    "no_target": "Several computers are connected: repeat the call with computer=<id> (cc_computers lists them).",
    "not_connected": "That computer is offline (world closed, chunk unloaded, or bridge stopped). "
    "cc_computers shows who is connected.",
    "disconnected": "The computer dropped mid-request (chunk unload, reboot or world exit). "
    "Check cc_computers, then retry.",
    "busy": "A previous request is still running on that computer. Call cc_cancel, then retry. If this keeps happening "
    "the game is probably paused or unfocused: yielding peripheral calls only progress while the game is ticking.",
    "relay_timeout": "The computer did not answer even its own timeout timer, which almost always means Minecraft "
    "is paused/unfocused or the computer's chunk is not loaded. A cancel was queued for it. Focus the game and retry; "
    "use cc_ping to see if it answers at all.",
    "timeout": "The request ran past its timeout and was abandoned on the computer. Raise `timeout`, split the work, "
    "or (for peripheral setters) make sure the game is focused and unpaused.",
}


def hint_for(res: dict) -> str | None:
    e = res.get("error")
    return HINTS.get(e.get("kind")) if isinstance(e, dict) else None


def format_result(res: dict) -> str:
    """Human/LLM-readable text for one result. Errors come first and are explicit."""
    out = []
    ok = res.get("ok", True)
    out.append("OK" if ok else "ERROR")
    e = res.get("error")
    if e:
        out.append(f"  {e.get('kind')}: {e.get('message')}")
        if e.get("traceback"):
            out.append("  " + e["traceback"].strip().replace("\n", "\n  "))
    hint = hint_for(res)
    if hint:
        out.append("  hint: " + hint)
    if res.get("returns"):
        out.append("returns: " + ", ".join(res["returns"]))
    if res.get("stderr"):
        out.append("stderr:")
        out.extend("  " + line for line in res["stderr"])
    if res.get("output"):
        out.append("output:" + (" (truncated, tail kept)" if res.get("truncated") else ""))
        out.append(res["output"])
    if "lines" in res or "errors" in res:
        red = set(res.get("red") or [])
        if res.get("lines"):
            out.append("terminal (! = red text):")
            out.extend(("! " if i in red else "  ") + line for i, line in enumerate(res["lines"], 1))
        if res.get("errors"):
            out.append("printError log:")
            out.extend(f"  #{x['id']} [{x['when']}] {x['text']}" for x in res["errors"])
        out.append(f"last_error_id: {res.get('last_error_id', 0)}")
    for key in ("data", "entries", "peripherals", "computers"):
        if key in res:
            out.append(res[key] if isinstance(res[key], str) else json.dumps(res[key], indent=2))
    if res.get("data") is not None and res.get("next_offset") is not None and not res.get("eof", True):
        out.append(f"[more data: call again with offset={res['next_offset']}]")
    if "duration" in res:
        out.append(f"[{res['duration']:.2f}s]")
    return "\n".join(out)


# ---------------------------------------------------------------------------------- file transfer
def files_roots() -> list[Path]:
    """Local folders push/pull may touch: only those listed in CC_MCP_FILES_ROOT (os.pathsep separated).
    There is no default: until the variable is set, push/pull refuse."""
    extra = os.environ.get("CC_MCP_FILES_ROOT", "")
    return [Path(p).expanduser().resolve() for p in extra.split(os.pathsep) if p.strip()]


def resolve_local(path: str) -> Path:
    """Resolve `path` (relative paths are taken from the first allowed folder).

    Refuses anything outside the allowed folders."""
    roots = files_roots()
    if not roots:
        raise PermissionError(
            "file transfer is disabled: set CC_MCP_FILES_ROOT to the local folder(s) "
            "cc_push/cc_pull may read and write (several folders separated by the OS path separator)"
        )
    p = Path(path).expanduser()
    p = (p if p.is_absolute() else roots[0] / p).resolve()
    if not any(p == r or r in p.parents for r in roots):
        raise PermissionError(
            f"{p} is outside the allowed folders ({', '.join(str(r) for r in roots)}); "
            "add the folder to CC_MCP_FILES_ROOT to allow it"
        )
    return p


def push_file(local: str, remote: str, computer: int | None = None, timeout: float = 30, append: bool = False) -> dict:
    """Upload a local UTF-8 text file in chunks that fit the relay's request limit."""
    try:
        with open(resolve_local(local), encoding="utf-8", newline="") as fh:  # newline="": keep CRLF/LF exactly
            text = fh.read()
    except (OSError, UnicodeDecodeError, PermissionError) as e:
        return {"ok": False, "error": {"kind": "local_file", "message": str(e)}}
    pos, chunks, res = 0, 0, {"ok": True, "size": 0}
    while True:
        n = 60000
        while True:  # shrink until the JSON-escaped chunk fits (non-ASCII text escapes to 6 bytes per char)
            piece = text[pos : pos + n]
            if len(json.dumps(piece)) <= 75000 or n <= 500:
                break
            n //= 2
        res = call("write", computer, timeout, path=remote, data=piece, append=(append or chunks > 0) or None)
        chunks += 1
        pos += len(piece)
        if not res.get("ok") or pos >= len(text):
            break
    res["chunks"] = chunks
    res["chars"] = len(text)
    return res


def pull_file(remote: str, local: str, computer: int | None = None, timeout: float = 30) -> dict:
    """Download a computer file to a local path. Intended for text (ASCII/UTF-8); chunk edges are byte offsets."""
    try:
        dest = resolve_local(local)
    except PermissionError as e:
        return {"ok": False, "error": {"kind": "local_file", "message": str(e)}}
    parts, offset = [], 0
    while True:
        res = call("read", computer, timeout, path=remote, offset=offset)
        if not res.get("ok"):
            return res
        parts.append(res.get("data") or "")
        offset = res.get("next_offset", offset)
        if res.get("eof", True):
            break
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("".join(parts), encoding="utf-8", newline="")
    except OSError as e:
        return {"ok": False, "error": {"kind": "local_file", "message": str(e)}}
    return {"ok": True, "bytes": offset, "path": str(dest)}
