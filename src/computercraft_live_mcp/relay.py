#!/usr/bin/env python3
"""ComputerCraft MCP relay.

Two endpoints on one port:
  GET  /ws            the in-game computer (bridge.lua) connects here
  POST /api/call      control API: forward one request to a computer, return its result
  GET  /api/computers connected computers
  GET  /healthz       liveness

Separate secrets in tokens.json: one COMPUTER token per computer id (it lives on
that computer's disk and only lets it connect as that id) and the API token (used
by ccctl / the MCP shim). Leaking a computer token must not give anyone the
control API, nor another computer's connection. The old shared computer token
still works, with a deprecation warning, for ids that have no token of their own.

Dependencies: aiohttp.
"""

import argparse
import asyncio
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import sys
import time
from pathlib import Path

from aiohttp import WSMsgType, web

from .config import DEFAULT_PORT, get_config
from .limits import DEFAULT_TIMEOUT, MAX_REQ_BYTES, RELAY_TIMEOUT_MARGIN, RELAY_WS_MAX_MSG_SIZE

NO_LOCK_OPS = {"ping", "cancel", "log"}  # must work while a long request is running
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LATE_REPLY_LIMIT = 128
SNIPPET_CHARS = 200
# Ops and error kinds are logged only when they look like protocol names, so arbitrary
# text (newlines, content, token-like strings) from a caller or a computer stays out of the log.
PROTOCOL_NAME = re.compile(r"[a-z][a-z_]{0,31}")
COMPUTER_KEY = re.compile(r"0|[1-9][0-9]{0,9}")  # keys of tokens.json "computers": canonical ids only
MAX_COMPUTER_ID = 2**31  # CC computer ids are Java ints
PRIVATE_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
log = logging.getLogger("computercraft_live_mcp.relay")
_log_handlers: list[logging.Handler] = []


def setup_logging(level: str = "INFO", log_file: str | None = None) -> None:
    """Relay process logging (main() only): stderr plus an optional file; third-party loggers stay at WARNING."""
    handlers = [logging.StreamHandler()]
    if log_file:
        handlers.append(logging.FileHandler(log_file, mode="a", encoding="utf-8"))
    root = logging.getLogger()
    for handler in _log_handlers:
        root.removeHandler(handler)
        handler.close()
    _log_handlers[:] = handlers
    for handler in handlers:
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        root.addHandler(handler)
    root.setLevel(logging.WARNING)
    logging.getLogger("computercraft_live_mcp").setLevel(level.upper())


def protocol_name(value) -> str:
    return value if isinstance(value, str) and PROTOCOL_NAME.fullmatch(value) else "unknown"


def operation(body: dict) -> str:
    return protocol_name(body.get("op"))


def outcome(result: dict) -> str:
    if result.get("ok"):
        return "ok"
    error = result.get("error")
    return protocol_name(error.get("kind") if isinstance(error, dict) else None)


def secret_values(tokens) -> tuple[str, ...]:
    """Every non-empty string in the token data, including nested per-computer tokens."""
    if isinstance(tokens, str):
        return (tokens,) if tokens else ()
    if isinstance(tokens, dict):
        tokens = list(tokens.values())
    if isinstance(tokens, (list, tuple)):
        return tuple(s for value in tokens for s in secret_values(value))
    return ()


def request_id(value) -> int | None:
    """Result ids as sent back by the bridge; an integral float (12.0) still matches request 12."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def remember(records: dict, rid: int, value) -> None:
    records[rid] = value
    if len(records) > LATE_REPLY_LIMIT:
        records.pop(next(iter(records)))


def computer_id(value) -> int | None:
    """A computer id as claimed in a hello: a non-negative integer below 2**31 (12.0 counts as 12)."""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < MAX_COMPUTER_ID:
        return value
    return None


def new_token() -> str:
    return secrets.token_urlsafe(24)


def same_secret(given, expected) -> bool:
    """Constant-time comparison that accepts any text (non-ASCII, lone surrogates) and never matches an empty token."""
    if not isinstance(given, str) or not isinstance(expected, str) or not expected:
        return False
    return secrets.compare_digest(given.encode("utf-8", "surrogatepass"), expected.encode("utf-8", "surrogatepass"))


def check_token(tokens: dict, cid: int, token) -> tuple[str | None, str | None]:
    """May `token` connect as computer `cid`? (auth, None) with auth "own_token" or "shared_token", else
    (None, reason). The shared token (deprecated) only works for ids that have no token of their own."""
    computers = tokens.get("computers") or {}
    own = computers.get(str(cid))
    if own is not None:
        if same_secret(token, own):
            return "own_token", None
        if same_secret(token, tokens.get("computer")):
            return None, "shared token not valid for this id (it has its own token)"
    elif same_secret(token, tokens.get("computer")):
        return "shared_token", None
    for other, value in computers.items():
        if same_secret(token, value):
            return None, f"token not valid for this id (it is computer #{other}'s token)"
    return None, "wrong token"


def token_file_problem(data) -> str | None:
    """Why stored token data is unusable, or None. Strict, so a hand edit cannot loosen the id binding
    (e.g. "03" would never match computer 3, letting the shared token through). Never quotes values."""
    if not isinstance(data, dict):
        return "expected a JSON object"
    for key in ("api", "computer"):
        if key in data and not (isinstance(data[key], str) and data[key]):
            return f'"{key}" must be a non-empty string'
    computers = data.get("computers", {})
    if not isinstance(computers, dict):
        return '"computers" must map computer ids to tokens'
    for key, value in computers.items():
        if not COMPUTER_KEY.fullmatch(key) or computer_id(int(key)) is None:
            return 'every key in "computers" must be a computer id such as "3"'
        if not (isinstance(value, str) and value):
            return f'"computers" entry {key} must be a non-empty string'
    return None


def read_tokens(path: Path) -> dict:
    """tokens.json as stored ({} if it does not exist); raises ValueError if it is unusable."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise ValueError(f"{path} is not valid JSON ({e})") from None
    problem = token_file_problem(data)
    if problem:
        raise ValueError(f"{path}: {problem}")
    return data


def write_private(path: Path, data: bytes) -> None:
    """Create `path` readable by its owner only from the first byte. O_EXCL: fails if it exists, even as a symlink.
    On Windows the mode only controls read-only; the file inherits the folder's permissions."""
    fd = os.open(path, PRIVATE_FILE_FLAGS, 0o600)
    try:
        with open(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def save_tokens(path: Path, tokens: dict, create: bool = False) -> None:
    """A new tokens.json is created in place with O_EXCL; an update is written to a private temp file in the
    same folder and swapped in with os.replace, so nobody sees a partial or briefly world-readable file."""
    data = (json.dumps(tokens, indent=2) + "\n").encode()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if create:
        write_private(path, data)
        return
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    write_private(tmp, data)
    try:
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:  # Windows: a reader has tokens.json open for a moment
                if os.name != "nt" or attempt == 4:
                    raise
                time.sleep(0.05)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def env_overrides() -> dict:
    config = get_config()
    return {key: value for key, value in (("api", config.api_token), ("computer", config.computer_token)) if value}


def stored_tokens() -> dict:
    """tokens.json as stored, after generating (and saving) a missing API or shared token not set by the env."""
    path = get_config().tokens_file
    existed = path.exists()
    stored = read_tokens(path)
    missing = [key for key in ("computer", "api") if key not in stored and key not in env_overrides()]
    for key in missing:
        stored[key] = new_token()
    if missing:
        save_tokens(path, stored, create=not existed)
    return stored


def load_tokens() -> dict:
    """The tokens the relay checks: tokens.json plus CC_MCP_API_TOKEN / CC_MCP_COMPUTER_TOKEN (never saved)."""
    return {**stored_tokens(), **env_overrides()}


def token_for_computer(cid: int) -> tuple[str, bool]:
    """The token for computer `cid`: (token, newly issued). Issued and saved if it has none yet."""
    stored = stored_tokens()
    computers = stored.get("computers", {})
    if str(cid) in computers:
        return computers[str(cid)], False
    stored["computers"] = {**computers, str(cid): new_token()}
    save_tokens(get_config().tokens_file, stored)
    return stored["computers"][str(cid)], True


def rotate_tokens(which: str, cid: int | None = None) -> dict:
    """Replace tokens in tokens.json. which: "api", "computer" (the shared token and every per-computer token,
    or only `cid`'s) or "all". Returns what changed: {"api": bool, "shared": bool, "computers": {id: token}}."""
    stored = stored_tokens()
    computers = dict(stored.get("computers", {}))
    changed = {"api": which in ("api", "all"), "shared": False, "computers": {}}
    if changed["api"]:
        stored["api"] = new_token()
    if which in ("computer", "all"):
        changed["shared"] = cid is None
        if changed["shared"]:
            stored["computer"] = new_token()
        for key in [str(cid)] if cid is not None else list(computers):
            computers[key] = changed["computers"][int(key)] = new_token()
    if computers:
        stored["computers"] = computers
    save_tokens(get_config().tokens_file, stored)
    return changed


def file_stamp(path: Path):
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size, st.st_ino


class TokenReloader:
    """Lets a running relay follow tokens.json, so --setup and --rotate take effect without a restart."""

    def __init__(self, path: Path):
        self.path = path
        self.stamp = file_stamp(path)

    def __call__(self) -> dict | None:
        """The new tokens if the file changed since the last look, else None (also when it is unusable)."""
        stamp = file_stamp(self.path)
        if stamp == self.stamp:
            return None
        self.stamp = stamp
        try:
            tokens = {**read_tokens(self.path), **env_overrides()}
            missing = [key for key in ("api", "computer") if key not in tokens]
            if missing:
                raise ValueError(f"{self.path} has no {' or '.join(missing)} token")
        except (OSError, ValueError) as e:
            log.warning("tokens.json changed but cannot be used (%s); keeping the previous tokens", e)
            return None
        return tokens


def bmp_only(obj):
    """CC turns \\uXXXX escapes into UTF-8 bytes (so text arrives exact) but mis-decodes
    surrogate pairs, so characters above U+FFFF (emoji) are replaced with '?'."""
    if isinstance(obj, str):
        return "".join(c if ord(c) <= 0xFFFF else "?" for c in obj)
    if isinstance(obj, list):
        return [bmp_only(x) for x in obj]
    if isinstance(obj, dict):
        return {k: bmp_only(v) for k, v in obj.items()}
    return obj


def encode_request(rid: int, body: dict) -> str:
    """Encode the complete request as sent to CC, including its BMP-only conversion."""
    return json.dumps(bmp_only({"type": "req", "req": rid, **body}))


def err(kind: str, message: str) -> dict:
    return {"ok": False, "error": {"kind": kind, "message": message}}


# CC's JSON writes an empty Lua table as {} - turn the list-typed fields back into [].
LIST_FIELDS = ("stderr", "returns", "entries", "peripherals", "lines", "red", "errors")
# CC teletext/box glyphs (0x80-0x9f) show up in its error boxes; make them readable.
GLYPHS = {i: "-" for i in range(0x80, 0xA0)}
GLYPHS[0x95] = "|"


def fix_bytes(s):
    """encoding == "bytes": every character is one raw byte. Prefer reading it as UTF-8;
    if it is not valid UTF-8 (CC's box glyphs, binary data) keep the bytes, glyphs as ASCII."""
    if not isinstance(s, str):
        return s
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s.translate(GLYPHS)


def normalise(res: dict) -> dict:
    for key in LIST_FIELDS:
        if res.get(key) == {}:
            res[key] = []
    for p in res.get("peripherals") or []:
        for key in ("types", "methods"):
            if p.get(key) == {}:
                p[key] = []
    if res.get("encoding") == "bytes":
        for key in ("output", "data"):
            res[key] = fix_bytes(res.get(key)) if key in res else None
            if res[key] is None:
                del res[key]
        for key in ("stderr", "returns", "lines"):
            if isinstance(res.get(key), list):
                res[key] = [fix_bytes(s) for s in res[key]]
        for e in res.get("errors") or []:
            if isinstance(e, dict) and "text" in e:
                e["text"] = fix_bytes(e["text"])
        if isinstance(res.get("error"), dict):
            for key in ("message", "traceback"):
                if key in res["error"]:
                    res["error"][key] = fix_bytes(res["error"][key])
    return res


class Conn:
    def __init__(self, ws, hello: dict, remote: str | None = None, tokens=(), auth: str | None = None):
        self.ws = ws
        self.id = int(hello.get("id", 0))
        self.label = hello.get("label") if isinstance(hello.get("label"), str) else None
        self.host = hello.get("host")
        self.connected_at = time.time()
        self.opened_at = time.monotonic()
        self.remote = remote
        self.secrets = secret_values(tokens)  # redacted from anything echoed into the log
        self.auth = auth  # "own_token", or "shared_token" (deprecated)
        self.token = hello.get("token")  # re-checked when tokens.json changes
        # Takeovers of this id, carried over from the connection this one replaced.
        self.replacements = 0
        self.replaced_at: float | None = None
        self.previous_remote: str | None = None
        self.pending: dict[int, asyncio.Future] = {}
        self.response_sizes: dict[int, int] = {}
        self.timed_out: dict[int, tuple[str, float]] = {}  # bounded: rid -> (op, sent at), to spot late replies
        self.queued_cancels: dict[int, bool] = {}  # bounded: -rid of cancels sent after a relay_timeout
        self.counter = 0
        self.lock = asyncio.Lock()

    def info(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "host": self.host,
            "connected_for_s": round(time.time() - self.connected_at),
            "remote": self.remote,
            "auth": self.auth,
            "replacements": self.replacements,
            "last_replaced_s_ago": None if self.replaced_at is None else round(time.time() - self.replaced_at),
            "previous_remote": self.previous_remote,
        }

    def snippet(self, text: str) -> str:
        """Token-free, ASCII-only (one line) and truncated: for DEBUG lines and player-written labels."""
        for secret in self.secrets:
            text = text.replace(secret, "[REDACTED]")
        return ascii(text[:SNIPPET_CHARS] + ("..." if len(text) > SNIPPET_CHARS else ""))

    async def _roundtrip(self, body: dict, timeout: float, queued: float = 0.0) -> dict:
        self.counter += 1
        rid = self.counter
        data = encode_request(rid, body)
        size = len(data.encode())
        op = operation(body)
        if log.isEnabledFor(logging.DEBUG):
            summary = [
                f"{key}={self.snippet(body[key])} {key}_len={len(body[key])}"
                for key in ("path", "code", "command")
                if isinstance(body.get(key), str)
            ]
            if isinstance(body.get("data"), str):
                summary.append(f"data_len={len(body['data'])}")
            log.debug("#%s %s req=%s %s", self.id, op, rid, " ".join(summary))
        if size > MAX_REQ_BYTES:
            log.warning("#%s %s req=%s too_large req_bytes=%s limit=%s", self.id, op, rid, size, MAX_REQ_BYTES)
            log.info(
                "#%s %s req=%s queued=%.2fs rtt=0.00s outcome=too_large req_bytes=%s resp_bytes=-",
                self.id,
                op,
                rid,
                queued,
                size,
            )
            return err("too_large", f"request over {MAX_REQ_BYTES} bytes; send big files in chunks with append")
        fut = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        started = time.monotonic()
        kind = "aborted"  # left through an unexpected exception or task cancellation
        try:
            await self.ws.send_str(data)
            result = normalise(await asyncio.wait_for(fut, timeout))
            kind = outcome(result)
            return result
        except asyncio.TimeoutError:
            kind = "relay_timeout"
            remember(self.timed_out, rid, (op, started))
            cancel_queued = False
            # The computer's own timer never fired, which in practice means the game is paused/unfocused
            # or the computer's chunk is not ticking. The job may still be queued on the computer: queue a
            # cancel behind it (websocket order is preserved) so the next request is not answered "busy".
            if body.get("op") not in NO_LOCK_OPS:
                try:
                    remember(self.queued_cancels, -rid, True)
                    await self.ws.send_str(json.dumps({"type": "req", "req": -rid, "op": "cancel"}))
                    cancel_queued = True
                    log.warning("#%s %s req=%s queued cancel cancel_req=%s", self.id, op, rid, -rid)
                except Exception:
                    self.queued_cancels.pop(-rid, None)
            log.warning(
                "#%s %s req=%s relay_timeout queued=%.2fs timeout=%gs wait=%gs cancel_queued=%s",
                self.id,
                op,
                rid,
                queued,
                float(body.get("timeout", DEFAULT_TIMEOUT)),
                timeout,
                str(cancel_queued).lower(),
            )
            return err("relay_timeout", f"no reply from computer within {timeout:.0f}s")
        except ConnectionError:
            kind = "disconnected"
            return err("disconnected", "computer disconnected before replying")
        finally:
            self.pending.pop(rid, None)
            log.info(
                "#%s %s req=%s queued=%.2fs rtt=%.2fs outcome=%s req_bytes=%s resp_bytes=%s",
                self.id,
                op,
                rid,
                queued,
                time.monotonic() - started,
                kind,
                size,
                self.response_sizes.pop(rid, "-"),
            )

    async def call(self, body: dict, timeout: float) -> dict:
        if body.get("op") in NO_LOCK_OPS:
            return await self._roundtrip(body, timeout)
        started = time.monotonic()
        async with self.lock:
            # Time queued is not part of the relay's wait (the timeout starts once the lock is held).
            return await self._roundtrip(body, timeout, queued=time.monotonic() - started)

    def receive_result(self, data: dict, size: int) -> None:
        rid = request_id(data.get("req"))
        fut = self.pending.get(rid) if rid is not None else None
        if fut is not None and not fut.done():
            self.response_sizes[rid] = size
            fut.set_result(data)
        elif rid in self.queued_cancels:
            self.queued_cancels.pop(rid)
            log.info(
                "computer #%s cancel ack req=%s outcome=%s cancelled=%s",
                self.id,
                rid,
                outcome(data),
                str(data.get("cancelled") is True).lower(),
            )
        elif rid in self.timed_out:
            op, started = self.timed_out.pop(rid)
            log.warning(
                "computer #%s answered late req=%s (%s) %.2fs after it was sent, after the relay gave up; outcome=%s",
                self.id,
                rid,
                op,
                time.monotonic() - started,
                outcome(data),
            )
        else:
            log.warning("computer #%s unexpected reply req=%s", self.id, rid if rid is not None else "invalid")


def label_text(label: str) -> str:
    """One-line, bounded label text for error messages (without surrounding quotes)."""
    return json.dumps(label[:32] + ("..." if len(label) > 32 else ""))[1:-1]


def resolve_target(computers: dict[int, Conn], target) -> Conn | tuple[str, str, int]:
    """Resolve an id or exact, case-insensitive label, or return (kind, message, status)."""
    if target is None:
        if len(computers) == 1:
            return next(iter(computers.values()))
        if not computers:
            return "no_target", "no computer is connected", 409
        pairs = ", ".join(
            f"#{c.id} ({label_text(c.label) if c.label is not None else 'no label'})" for c in computers.values()
        )
        return "no_target", f"{len(computers)} computers connected ({pairs}); pass 'computer' (an id or a label)", 409
    if isinstance(target, str) and target:
        matches = [c for c in computers.values() if c.label is not None and c.label.casefold() == target.casefold()]
        if len(matches) == 1:
            return matches[0]
        if matches:
            ids = ", ".join(f"#{c.id}" for c in matches)
            message = f'{len(matches)} computers are labelled "{label_text(target)}" ({ids}); pass the id'
            return "ambiguous", message, 409
        cid = computer_id(int(target)) if COMPUTER_KEY.fullmatch(target) else None
        if cid is None:
            return (
                "not_connected",
                f'no connected computer is labelled "{label_text(target)}" '
                "(labels are read on connect and refreshed by cc_ping; cc_computers lists them)",
                404,
            )
        target = cid
    cid = computer_id(target)
    if cid is None:
        return "bad_request", "computer must be an id or a label", 400
    if cid not in computers:
        return "not_connected", f"computer {cid} is not connected", 404
    return computers[cid]


def make_app(tokens: dict, reload=None) -> web.Application:
    """`reload` (a TokenReloader) is asked for changed tokens before each hello and API request."""
    computers: dict[int, Conn] = {}
    closing: set[asyncio.Task] = set()

    def current_tokens() -> dict:
        nonlocal tokens
        fresh = reload() if reload else None
        if fresh is not None:
            tokens = fresh
            log.info("tokens.json changed; tokens reloaded")
            for conn in list(computers.values()):
                conn.secrets = secret_values(tokens)
                conn.auth, _ = check_token(tokens, conn.id, conn.token)
                if conn.auth is None:
                    # Dropped from the table at once, so no request reaches it while the close completes.
                    del computers[conn.id]
                    log.warning("computer #%s token no longer valid; disconnecting remote=%s", conn.id, conn.remote)
                    task = asyncio.ensure_future(conn.ws.close(code=4401, message=b"unauthorized"))
                    closing.add(task)
                    task.add_done_callback(closing.discard)
        return tokens

    def authorized(request) -> bool:
        got = request.headers.get("Authorization", "")
        ok = same_secret(got, "Bearer " + current_tokens()["api"])
        if not ok:
            log.warning("unauthorized API request remote=%s", request.remote)
        return ok

    async def ws_handler(request):
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=RELAY_WS_MAX_MSG_SIZE)
        await ws.prepare(request)
        hello = {}
        reason = None
        try:
            first = await asyncio.wait_for(ws.receive(), 10)
            hello = json.loads(first.data) if first.type == WSMsgType.TEXT else {}
        except asyncio.TimeoutError:
            reason = "no hello within 10 s"
        except ValueError:
            reason = "not JSON"
        if not isinstance(hello, dict):
            hello = {}
        # Only valid numeric ids reach the log or the token check; never echo arbitrary handshake fields.
        cid = computer_id(hello.get("id"))
        auth = None
        if reason is None:
            if hello.get("type") != "hello":
                reason = "not a hello"
            elif cid is None:
                reason = "bad id"
            else:
                auth, reason = check_token(current_tokens(), cid, hello.get("token"))
        if reason:
            claimed_id = "-" if cid is None else cid
            log.warning("rejected hello remote=%s reason=%s claimed_id=%s", request.remote, reason, claimed_id)
            await ws.close(code=4401, message=b"unauthorized")
            return ws
        conn = Conn(ws, hello, remote=request.remote, tokens=tokens, auth=auth)
        old = computers.get(conn.id)
        if old:
            conn.replacements = old.replacements + 1
            conn.replaced_at = time.time()
            conn.previous_remote = old.remote
            log.warning(
                "computer #%s replaced connection old_remote=%s new_remote=%s open=%.2fs replacements=%s",
                conn.id,
                old.remote,
                conn.remote,
                time.monotonic() - old.opened_at,
                conn.replacements,
            )
            await old.ws.close(code=4000, message=b"replaced")
            # tokens.json may have been reloaded while the old connection was closing.
            conn.auth, reason = check_token(tokens, conn.id, conn.token)
            if reason:
                log.warning("rejected hello remote=%s reason=%s claimed_id=%s", request.remote, reason, conn.id)
                await ws.close(code=4401, message=b"unauthorized")
                return ws
        computers[conn.id] = conn
        await ws.send_json({"type": "welcome"})
        label = conn.snippet(conn.label) if isinstance(conn.label, str) else None
        log.info("computer #%s connected label=%s remote=%s", conn.id, label, conn.remote)
        if conn.auth == "shared_token":
            log.warning(
                "computer #%s connected with the shared computer token, which is deprecated and will stop working "
                "in a future release: run `computercraft-live-mcp-relay --setup --computer %s` and type the printed "
                "commands on that computer",
                conn.id,
                conn.id,
            )
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    continue
                if isinstance(data, dict) and data.get("type") == "result":
                    conn.receive_result(data, len(msg.data.encode("utf-8")))
        finally:
            if computers.get(conn.id) is conn:
                del computers[conn.id]
            for fut in conn.pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError())
            log.info(
                "computer #%s disconnected remote=%s open=%.2fs close_code=%s",
                conn.id,
                conn.remote,
                time.monotonic() - conn.opened_at,
                ws.close_code,
            )
        return ws

    async def api_computers(request):
        if not authorized(request):
            return web.json_response(err("unauthorized", "bad API token"), status=401)
        return web.json_response({"computers": [c.info() for c in computers.values()]})

    async def api_call(request):
        if not authorized(request):
            return web.json_response(err("unauthorized", "bad API token"), status=401)
        try:
            body = await request.json()
        except ValueError:
            log.info("#- unknown req=- queued=0.00s rtt=0.00s outcome=bad_request req_bytes=- resp_bytes=-")
            return web.json_response(err("bad_request", "body must be JSON"), status=400)
        target = body.pop("computer", None)
        conn = resolve_target(computers, target)
        if isinstance(conn, tuple):
            kind, message, status = conn
            cid = computer_id(target)
            if kind == "not_connected" and isinstance(target, str) and COMPUTER_KEY.fullmatch(target):
                cid = computer_id(int(target))
            log.info(
                "#%s %s req=- queued=0.00s rtt=0.00s outcome=%s req_bytes=- resp_bytes=-",
                cid if cid is not None else "-",
                operation(body),
                kind,
            )
            return web.json_response(err(kind, message), status=status)
        timeout = float(body.get("timeout", DEFAULT_TIMEOUT))
        result = await conn.call(body, timeout + RELAY_TIMEOUT_MARGIN)
        if body.get("op") == "ping" and result.get("ok") and computer_id(result.get("id")) == conn.id:
            label = result.get("label") if isinstance(result.get("label"), str) else None
            if label != conn.label:
                conn.label = label
                log.info(
                    "computer #%s label changed label=%s", conn.id, conn.snippet(label) if label is not None else None
                )
        return web.json_response(result)

    async def healthz(request):
        return web.json_response({"ok": True, "computers": len(computers)})

    async def serve_bridge(request):
        # public on purpose: the client script holds no secrets, and this makes
        # installing it a one-liner:  wget http://HOST:PORT/bridge.lua bridge.lua
        return web.Response(
            text=(Path(__file__).parent / "bridge.lua").read_text(encoding="utf-8"), content_type="text/plain"
        )

    app = web.Application()
    app.add_routes(
        [
            web.get("/ws", ws_handler),
            web.get("/api/computers", api_computers),
            web.post("/api/call", api_call),
            web.get("/healthz", healthz),
            web.get("/bridge.lua", serve_bridge),
        ]
    )
    return app


def _default_route_address(bind_host: str) -> str:
    family, destination = {
        "0.0.0.0": (socket.AF_INET, "192.0.2.1"),
        "::": (socket.AF_INET6, "2001:db8::1"),
    }[bind_host]
    # A UDP connect selects an interface without sending a packet.
    with socket.socket(family, socket.SOCK_DGRAM) as route:
        route.connect((destination, 9))
        return route.getsockname()[0]


def _is_loopback(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host.lower() == "localhost"
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return address.ipv4_mapped.is_loopback
    return address.is_loopback


def computer_arg(text: str) -> int:
    cid = computer_id(int(text)) if re.fullmatch(r"[0-9]{1,10}", text) else None
    if cid is None:
        raise argparse.ArgumentTypeError(f"not a computer id: {text!r} (type `id` on the computer to see its id)")
    return cid


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    action = p.add_mutually_exclusive_group()
    action.add_argument("--show-tokens", action="store_true", help="print the tokens and exit")
    action.add_argument(
        "--setup",
        action="store_true",
        help="issue (or reuse) the token for --computer ID, print the exact in-game commands and exit",
    )
    action.add_argument(
        "--rotate",
        choices=("api", "computer", "all"),
        help="replace tokens, print what to update and exit: the API token, the computer tokens "
        "(every one, or only --computer ID's), or all of them",
    )
    p.add_argument(
        "--computer",
        type=computer_arg,
        metavar="ID",
        help="computer id for --setup and --rotate computer (type `id` on the computer to see it)",
    )
    p.add_argument(
        "--advertise-host",
        help="host or IP for --setup (defaults to the bind host or a detected local IP)",
    )
    p.add_argument(
        "--log-level",
        type=str.upper,
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
        help="relay log level (default INFO)",
    )
    p.add_argument("--log-file", help="append UTF-8 logs to this file as well as stderr")
    return p


def setup_text(host: str, port: int, cid: int, token: str, new: bool) -> str:
    state = "New token" if new else f"Existing token (`--rotate computer --computer {cid}` replaces it)"
    base = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    return f"""{state} for computer #{cid}. It only works for computer #{cid}: type `id` on the computer to check.

On a server you control, add this to the CC:Tweaked server config above
the existing $private rule and restart the world:

    [[http.rules]]
        host = {json.dumps(host, ensure_ascii=False)}
        port = {port}
        action = "allow"

In-game, on computer #{cid} (skip `wget` if bridge.lua is already there; if the bridge is running, type `exit` first):

    wget http://{base}/bridge.lua bridge.lua
    bridge ws://{base}/ws {token}

To start it automatically whenever the computer boots, also run:

    edit startup
        (type this one line, then Ctrl, choose Save, Ctrl, choose Exit)
        shell.run("bridge")

The relay must be running (computercraft-live-mcp-relay), but the order does not matter: the bridge retries
until the relay is up, and a running relay picks up new tokens by itself. `exit` in the computer's shell
stops the bridge."""


def rotate_text(base: str, changed: dict) -> str:
    config = get_config()
    parts = []
    if changed["api"]:
        text = (
            "New API token saved in tokens.json. ccctl and the MCP server read it from there on every call; if your\n"
            "MCP client config or shell sets CC_MCP_API_TOKEN, put the new value there (--show-tokens prints it)."
        )
        if config.api_token:
            text += "\nCC_MCP_API_TOKEN is set in this shell, so it still overrides tokens.json here."
        parts.append(text)
    if changed["shared"]:
        text = (
            "The shared computer token (deprecated) was replaced too. Computers that used it are refused until you\n"
            "run `computercraft-live-mcp-relay --setup --computer <id>` for each and type the printed commands there."
        )
        if config.computer_token:
            text += "\nCC_MCP_COMPUTER_TOKEN is set in this shell, so it still overrides the shared token here."
        parts.append(text)
    for cid, token in sorted(changed["computers"].items()):
        parts.append(
            f"On computer #{cid}, type `exit` if the bridge is running, then:\n\n    bridge ws://{base}/ws {token}"
        )
    parts.append(
        "A running relay reloads tokens.json on its next connection or API request and disconnects computers\n"
        "whose token was replaced."
    )
    return "\n\n".join(parts)


def main(argv=None):
    parser = build_parser()
    a = parser.parse_args(argv)
    if a.setup and a.computer is None:
        parser.error("--setup needs --computer ID: each computer gets its own token (type `id` on it to see its id)")
    if a.computer is not None and not (a.setup or a.rotate == "computer"):
        parser.error("--computer goes with --setup or --rotate computer")
    base = f"{a.host}:{a.port}"
    if a.setup:
        host = (a.advertise_host or a.host).removeprefix("[").removesuffix("]")
        if a.advertise_host is None and host in {"0.0.0.0", "::"}:
            try:
                host = _default_route_address(host)
            except OSError:
                parser.error("could not detect a local IP address; set --advertise-host")
            print(
                f"[relay] using detected address {host}; set --advertise-host if unreachable from the game",
                file=sys.stderr,
            )
        if host in {"0.0.0.0", "::"}:
            parser.error("--advertise-host must be an address that the game can connect to")
    try:
        if a.setup:
            print(setup_text(host, a.port, a.computer, *token_for_computer(a.computer)))
            return
        if a.rotate:
            print(rotate_text(base, rotate_tokens(a.rotate, a.computer)))
            return
        tokens = load_tokens()
    except (OSError, ValueError) as e:
        sys.exit(f"computercraft-live-mcp-relay: {e}")
    if a.show_tokens:
        print(json.dumps(tokens, indent=2))
        return
    if not _is_loopback(a.host):
        print(
            "[relay] warning: non-loopback listener uses plain HTTP/WebSocket and sends tokens "
            "without encryption. Use only on a trusted network. "
            "https://github.com/slashracer5/computercraft-live-mcp/blob/main/SECURITY.md",
            file=sys.stderr,
        )
    setup_logging(a.log_level, a.log_file)
    path = get_config().tokens_file
    log.info("listening on %s:%s; tokens in %s", a.host, a.port, path)
    web.run_app(make_app(tokens, TokenReloader(path)), host=a.host, port=a.port, print=None)


if __name__ == "__main__":
    main()
