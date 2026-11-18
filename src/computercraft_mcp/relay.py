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
import secrets
import time
from pathlib import Path

from aiohttp import WSMsgType, web

from .config import DEFAULT_PORT, get_config
from .limits import DEFAULT_TIMEOUT, MAX_REQ_BYTES, RELAY_TIMEOUT_MARGIN, RELAY_WS_MAX_MSG_SIZE

NO_LOCK_OPS = {"ping", "cancel", "log"}  # must work while a long request is running


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
    def __init__(self, ws, hello: dict):
        self.ws = ws
        self.id = int(hello.get("id", 0))
        self.label = hello.get("label")
        self.host = hello.get("host")
        self.connected_at = time.time()
        self.pending: dict[int, asyncio.Future] = {}
        self.counter = 0
        self.lock = asyncio.Lock()

    def info(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "host": self.host,
            "connected_for_s": round(time.time() - self.connected_at),
        }

    async def _roundtrip(self, body: dict, timeout: float) -> dict:
        self.counter += 1
        rid = self.counter
        data = encode_request(rid, body)
        if len(data.encode()) > MAX_REQ_BYTES:
            return err("too_large", f"request over {MAX_REQ_BYTES} bytes; send big files in chunks with append")
        fut = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        try:
            await self.ws.send_str(data)
            return normalise(await asyncio.wait_for(fut, timeout))
        except asyncio.TimeoutError:
            # The computer's own timer never fired, which in practice means the game is paused/unfocused
            # or the computer's chunk is not ticking. The job may still be queued on the computer: queue a
            # cancel behind it (websocket order is preserved) so the next request is not answered "busy".
            if body.get("op") not in NO_LOCK_OPS:
                try:
                    await self.ws.send_str(json.dumps({"type": "req", "req": -rid, "op": "cancel"}))
                except Exception:
                    pass
            return err("relay_timeout", f"no reply from computer within {timeout:.0f}s")
        except ConnectionError:
            return err("disconnected", "computer disconnected before replying")
        finally:
            self.pending.pop(rid, None)

    async def call(self, body: dict, timeout: float) -> dict:
        if body.get("op") in NO_LOCK_OPS:
            return await self._roundtrip(body, timeout)
        async with self.lock:
            return await self._roundtrip(body, timeout)


def make_app(tokens: dict) -> web.Application:
    computers: dict[int, Conn] = {}

    def authorized(request) -> bool:
        got = request.headers.get("Authorization", "")
        return secrets.compare_digest(got, "Bearer " + tokens["api"])

    async def ws_handler(request):
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=RELAY_WS_MAX_MSG_SIZE)
        await ws.prepare(request)
        try:
            first = await asyncio.wait_for(ws.receive(), 10)
            hello = json.loads(first.data) if first.type == WSMsgType.TEXT else {}
        except (asyncio.TimeoutError, ValueError):
            hello = {}
        if hello.get("type") != "hello" or not secrets.compare_digest(str(hello.get("token", "")), tokens["computer"]):
            await ws.close(code=4401, message=b"unauthorized")
            return ws
        conn = Conn(ws, hello)
        old = computers.get(conn.id)
        if old:
            print(
                f"[relay {time.strftime('%H:%M:%S')}] computer #{conn.id}: replacing an older connection "
                f"(open {time.time() - old.connected_at:.0f}s)",
                flush=True,
            )
            await old.ws.close(code=4000, message=b"replaced")
        computers[conn.id] = conn
        await ws.send_json({"type": "welcome"})
        print(
            f"[relay {time.strftime('%H:%M:%S')}] computer #{conn.id} connected ({conn.label or 'no label'})",
            flush=True,
        )
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    continue
                if data.get("type") == "result":
                    fut = conn.pending.get(data.get("req"))
                    if fut and not fut.done():
                        fut.set_result(data)
        finally:
            if computers.get(conn.id) is conn:
                del computers[conn.id]
            for fut in conn.pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError())
            print(
                f"[relay {time.strftime('%H:%M:%S')}] computer #{conn.id} disconnected "
                f"(was open {time.time() - conn.connected_at:.0f}s)",
                flush=True,
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
            return web.json_response(err("bad_request", "body must be JSON"), status=400)
        target = body.pop("computer", None)
        if target is None:
            if len(computers) != 1:
                ids = ", ".join(f"#{c}" for c in computers) or "none"
                return web.json_response(
                    err("no_target", f"{len(computers)} computers connected ({ids}); pass 'computer'"),
                    status=409,
                )
            conn = next(iter(computers.values()))
        else:
            conn = computers.get(int(target))
            if conn is None:
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


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--show-tokens", action="store_true", help="print both tokens and exit")
    p.add_argument("--setup", action="store_true", help="print the exact in-game commands and exit")
    a = p.parse_args()
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
    print(f"[relay] listening on {a.host}:{a.port}; tokens in {get_config().home / 'tokens.json'}", flush=True)
    web.run_app(make_app(tokens), host=a.host, port=a.port, print=None)


if __name__ == "__main__":
    main()
