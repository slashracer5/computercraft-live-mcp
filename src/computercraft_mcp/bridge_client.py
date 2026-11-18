"""Shared client code for the CC bridge relay: HTTP calls and file transfer.

Used by ccctl.py (command line) and mcp_server.py (MCP tools).
Env: CC_MCP_URL (default http://127.0.0.1:8765), CC_MCP_API_TOKEN,
CC_MCP_HOME (default ~/.computercraft-mcp, holds tokens.json).
"""

import contextlib
import json
import os
import secrets
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import cc_lua

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
    """Upload a local UTF-8 text file (see push_text)."""
    try:
        with open(resolve_local(local), encoding="utf-8", newline="") as fh:  # newline="": keep CRLF/LF exactly
            text = fh.read()
    except (OSError, UnicodeDecodeError, PermissionError) as e:
        return {"ok": False, "error": {"kind": "local_file", "message": str(e)}}
    return push_text(text, remote, computer, timeout, append)


def push_text(text: str, remote: str, computer: int | None = None, timeout: float = 30, append: bool = False) -> dict:
    """Upload text in chunks that fit the relay's request limit.

    An overwrite is atomic: one check request (space, read-only, directory), the chunks into part_path(remote),
    then one request that swaps the part into place. On failure the part is deleted (best effort) and the result
    says how far the upload got (sent_chars, chunks) and what is on the computer now (remote_state).
    append=True checks the same things but then appends straight to `remote`: not atomic."""
    started = time.monotonic()
    part = None if append else part_path(remote)
    sent = chunks = 0

    def result(res: dict, **fields) -> dict:  # our fields always override whatever the last reply carried
        return {
            **res,
            "chars": len(text),
            "sent_chars": sent,
            "chunks": chunks,
            "part_left": False,
            "duration": time.monotonic() - started,
            **fields,
        }

    res = _push_preflight(remote, part, len(text.encode("utf-8", "surrogatepass")), computer, timeout)
    if not res.get("ok"):
        return result(res, ok=False, remote_state="unchanged")
    res, sent, chunks = _upload_chunks(text, part or remote, computer, timeout, append)
    if not res.get("ok"):
        if part is None:
            return result(res, ok=False, remote_state="append")
        return result(res, ok=False, remote_state="unchanged", part_left=not _discard_part(part, computer, timeout))
    if part is None:
        return result(res, ok=True)
    res, state = _push_finalise(remote, part, computer, timeout)
    if res.get("ok"):
        return result(res)
    if state == "part_kept":  # the old file is gone: the part holds the only copy of the new one
        return result(res, ok=False, remote_state=state)
    return result(res, ok=False, remote_state=state, part_left=not _discard_part(part, computer, timeout))


def part_path(remote: str) -> str:
    """The temporary file an overwriting push writes before it replaces `remote` (a stale one is overwritten)."""
    return remote.rstrip("/\\") + ".part"


def push_chunk(text: str, pos: int = 0) -> str:
    """The next chunk from `pos`, halved until its JSON-escaped form fits the relay's request limit
    (non-ASCII text escapes to 6 bytes per char)."""
    n = 60000
    while True:
        piece = text[pos : pos + n]
        if len(json.dumps(piece)) <= 75000 or n <= 500:
            return piece
        n //= 2


def _push_error(kind: str, message: str) -> dict:
    return {"ok": False, "error": {"kind": kind, "message": message}}


def _exec_json(code: str, computer: int | None, timeout: float) -> tuple[dict, dict | None]:
    """Run a cc_lua snippet; returns (reply, the JSON document it returned or None)."""
    res = call("exec", computer, timeout, code=code)
    doc = cc_lua.parse_returned_json(res) if res.get("ok") else None
    return res, doc if isinstance(doc, dict) else None


def _push_preflight(remote: str, part: str | None, need: int, computer: int | None, timeout: float) -> dict:
    """One request before the first chunk, so a full drive, read-only path or directory fails before any write."""
    res, doc = _exec_json(cc_lua.push_preflight_code(remote, part, need), computer, timeout)
    if not res.get("ok"):
        return res
    if doc is None:
        return _push_error("internal", "the upload check returned no result")
    if doc.get("ok") is True:
        return {"ok": True}
    reason = doc.get("reason")
    if reason == "no_space":
        free = doc.get("free")
        free = free if isinstance(free, (int, float)) and not isinstance(free, bool) else "?"
        return _push_error("no_space", f"{remote}: the upload needs {need} bytes, the drive has {free} free")
    if reason == "read_only":
        return _push_error("read_only", f"{remote} is read-only")
    if reason == "is_directory":
        return _push_error("is_directory", f"{remote} is a directory")
    if reason == "part_is_directory":
        return _push_error("is_directory", f"{part} (the temporary upload file) is a directory")
    return _push_error("internal", "unexpected answer from the upload check")


def _upload_chunks(text: str, path: str, computer: int | None, timeout: float, append: bool) -> tuple[dict, int, int]:
    """Write `text` to `path` chunk by chunk; the first chunk truncates unless append.

    Returns (last reply, chars written, chunks written); stops at the first failed chunk."""
    sent = chunks = 0
    while True:
        piece = push_chunk(text, sent)
        res = call("write", computer, timeout, path=path, data=piece, append=(append or chunks > 0) or None)
        if not res.get("ok"):
            return res, sent, chunks
        sent += len(piece)
        chunks += 1
        if sent >= len(text):
            return res, sent, chunks


def _push_finalise(remote: str, part: str, computer: int | None, timeout: float) -> tuple[dict, str]:
    """Swap the finished part into place with one request. Returns (result, remote_state on failure)."""
    res, doc = _exec_json(cc_lua.push_finalise_code(remote, part), computer, timeout)
    if not res.get("ok"):  # the swap may or may not have run
        return {**res, "ok": False}, "unknown"
    if doc is None:
        return _push_error("internal", "the final replace returned no result"), "unknown"
    if doc.get("ok") is True:
        return {"ok": True, "size": doc.get("size")}, ""
    reason, detail = doc.get("reason"), doc.get("message")
    if reason == "is_directory":
        return _push_error("is_directory", f"{remote} became a directory during the upload"), "unchanged"
    if reason == "part_missing":
        return _push_error("replace_failed", f"{part} disappeared before it could replace {remote}"), "unchanged"
    if reason == "delete_failed":
        return _push_error("replace_failed", f"could not delete {remote}: {detail}"), "unchanged"
    if reason == "move_failed" and doc.get("target_deleted") is True:
        return _push_error("replace_failed", f"could not move {part} to {remote}: {detail}"), "part_kept"
    if reason == "move_failed":
        return _push_error("replace_failed", f"could not move {part} to {remote}: {detail}"), "unchanged"
    return _push_error("internal", "unexpected answer from the final replace"), "unknown"


def _discard_part(part: str, computer: int | None, timeout: float) -> bool:
    """Best-effort delete of a part file; True when the computer confirms it is gone."""
    _, doc = _exec_json(cc_lua.push_discard_code(part), computer, timeout)
    return doc is not None and doc.get("gone") is True


def pull_file(remote: str, local: str, computer: int | None = None, timeout: float = 30) -> dict:
    """Download a computer file to a local path. Intended for text (ASCII/UTF-8); chunk edges are byte offsets.

    The local file is only replaced once everything arrived (temp file + os.replace): a failed pull leaves an
    existing file as it was."""
    try:
        dest = resolve_local(local)
    except PermissionError as e:
        return {"ok": False, "error": {"kind": "local_file", "message": str(e)}}
    if dest.is_dir():
        return {"ok": False, "error": {"kind": "local_file", "message": f"{dest} is a directory"}}
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
        write_local_atomic(dest, "".join(parts))
    except OSError as e:
        return {"ok": False, "error": {"kind": "local_file", "message": str(e)}}
    return {"ok": True, "bytes": offset, "path": str(dest)}


def write_local_atomic(dest: Path, text: str) -> None:
    """Write UTF-8 text to a new temp file next to `dest`, then rename it over `dest`.

    On failure the temp file is deleted and an existing `dest` is untouched."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.{secrets.token_hex(4)}.part")
    try:
        with open(tmp, "x", encoding="utf-8", newline="") as fh:  # "x": never follows or reuses an existing file
            fh.write(text)
        if os.name != "nt" and dest.exists():  # keep an existing file's permissions, as the in-place write did
            with contextlib.suppress(OSError):
                shutil.copymode(dest, tmp)
        os.replace(tmp, dest)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
