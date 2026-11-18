#!/usr/bin/env python3
"""ComputerCraft MCP relay.

Two endpoints on one port:
  GET  /ws            the in-game computer (bridge.lua) connects here
  POST /api/call      control API: forward one request to a computer, return its result
  GET  /api/computers connected computers
  GET  /healthz       liveness

Two separate secrets: the COMPUTER token (lives on the in-game computer's disk)
and the API token (used by ccctl / the MCP shim). Leaking the first must not
give anyone the control API.

Dependencies: aiohttp.
"""

import argparse
import asyncio
import json
import logging
import re
import secrets
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
log = logging.getLogger("computercraft_mcp.relay")
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
    logging.getLogger("computercraft_mcp").setLevel(level.upper())


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


def load_tokens() -> dict:
    config = get_config()
    path = config.home / "tokens.json"
    tokens = json.loads(path.read_text()) if path.exists() else {}
    changed = False
    for key, override in (("computer", config.computer_token), ("api", config.api_token)):
        if override:
            tokens[key] = override
        elif key not in tokens:
            tokens[key] = secrets.token_urlsafe(24)
            changed = True
    if changed:
        config.home.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(tokens, indent=2))
        try:
            path.chmod(0o600)
        except OSError:
            pass
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
    def __init__(self, ws, hello: dict, remote: str | None = None, tokens=()):
        self.ws = ws
        self.id = int(hello.get("id", 0))
        self.label = hello.get("label")
        self.host = hello.get("host")
        self.connected_at = time.time()
        self.opened_at = time.monotonic()
        self.remote = remote
        self.secrets = secret_values(tokens)  # redacted from anything echoed into the log
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


def make_app(tokens: dict) -> web.Application:
    computers: dict[int, Conn] = {}

    def authorized(request) -> bool:
        got = request.headers.get("Authorization", "")
        ok = secrets.compare_digest(got, "Bearer " + tokens["api"])
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
        if reason is None:
            if hello.get("type") != "hello":
                reason = "not a hello"
            elif not secrets.compare_digest(str(hello.get("token", "")), tokens["computer"]):
                reason = "wrong token"
        if reason:
            claimed_id = hello.get("id")
            # Only numeric ids belong in logs; never echo arbitrary handshake fields.
            try:
                claimed_id = int(claimed_id)
            except (TypeError, ValueError, OverflowError):
                claimed_id = "-"
            log.warning("rejected hello remote=%s reason=%s claimed_id=%s", request.remote, reason, claimed_id)
            await ws.close(code=4401, message=b"unauthorized")
            return ws
        conn = Conn(ws, hello, remote=request.remote, tokens=tokens)
        old = computers.get(conn.id)
        if old:
            log.warning(
                "computer #%s replaced connection old_remote=%s new_remote=%s open=%.2fs",
                conn.id,
                old.remote,
                conn.remote,
                time.monotonic() - old.opened_at,
            )
            await old.ws.close(code=4000, message=b"replaced")
        computers[conn.id] = conn
        await ws.send_json({"type": "welcome"})
        label = conn.snippet(conn.label) if isinstance(conn.label, str) else None
        log.info("computer #%s connected label=%s remote=%s", conn.id, label, conn.remote)
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
        if target is None:
            if len(computers) != 1:
                log.info(
                    "#- %s req=- queued=0.00s rtt=0.00s outcome=no_target req_bytes=- resp_bytes=-",
                    operation(body),
                )
                ids = ", ".join(f"#{c}" for c in computers) or "none"
                return web.json_response(
                    err("no_target", f"{len(computers)} computers connected ({ids}); pass 'computer'"),
                    status=409,
                )
            conn = next(iter(computers.values()))
        else:
            conn = computers.get(int(target))
            if conn is None:
                log.info(
                    "#%s %s req=- queued=0.00s rtt=0.00s outcome=not_connected req_bytes=- resp_bytes=-",
                    int(target),
                    operation(body),
                )
                return web.json_response(err("not_connected", f"computer {target} is not connected"), status=404)
        timeout = float(body.get("timeout", DEFAULT_TIMEOUT))
        result = await conn.call(body, timeout + RELAY_TIMEOUT_MARGIN)
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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--show-tokens", action="store_true", help="print both tokens and exit")
    p.add_argument("--setup", action="store_true", help="print the exact in-game commands and exit")
    p.add_argument(
        "--log-level",
        type=str.upper,
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
        help="relay log level (default INFO)",
    )
    p.add_argument("--log-file", help="append UTF-8 logs to this file as well as stderr")
    return p


def main():
    a = build_parser().parse_args()
    tokens = load_tokens()
    if a.show_tokens:
        print(json.dumps(tokens, indent=2))
        return
    if a.setup:
        base = f"{a.host}:{a.port}"
        print(f"""In-game, on the computer (once per computer):

    wget http://{base}/bridge.lua bridge.lua
    bridge ws://{base}/ws {tokens["computer"]}

To start it automatically whenever the computer boots, also run:

    edit startup
        (type this one line, then Ctrl, choose Save, Ctrl, choose Exit)
        shell.run("bridge")

The relay must be running (computercraft-mcp-relay), but the order does not matter: the bridge retries
until the relay is up. `exit` in the computer's shell stops the bridge.""")
        return
    setup_logging(a.log_level, a.log_file)
    log.info("listening on %s:%s; tokens in %s", a.host, a.port, get_config().home / "tokens.json")
    web.run_app(make_app(tokens), host=a.host, port=a.port, print=None)


if __name__ == "__main__":
    main()
