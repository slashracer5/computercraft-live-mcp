"""Shared client code for the CC bridge relay: HTTP calls and file transfer.

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
