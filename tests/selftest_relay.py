#!/usr/bin/env python3
"""Relay-only self test with a fake in-game computer (no Minecraft / CraftOS needed).

Run:  python tests/selftest_relay.py   (after `pip install -e .`)
"""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import aiohttp

PORT = 18765
BASE = f"http://127.0.0.1:{PORT}"
HERE = Path(__file__).parent
results = []


def check(name, cond, detail=""):
    results.append(cond)
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))


def http(path, body=None, token=None):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        method="POST" if body is not None else "GET",
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


async def fake_computer(tokens, stop: asyncio.Event, cid=7, behaviour=None, seen=None):
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(f"{BASE}/ws".replace("http", "ws")) as ws:
            await ws.send_json(
                {"type": "hello", "token": tokens["computer"], "id": cid, "label": "fake", "host": "test"}
            )
            assert (await ws.receive_json())["type"] == "welcome"

            async def handle(req):
                if seen is not None:
                    seen.append(req["op"])
                if req["op"] == "cancel":
                    await ws.send_json({"type": "result", "req": req["req"], "ok": True, "cancelled": True})
                    return
                if req["op"] == "exec" and "hang" in req["code"]:
                    return  # never answers: like a paused game where the computer's timer cannot fire
                # like the real client: ping is answered inline even while a job runs
                if req["op"] == "ping":
                    await ws.send_json({"type": "result", "req": req["req"], "ok": True, "id": cid})
                elif req["op"] == "exec":
                    if "slow" in req["code"]:
                        await asyncio.sleep(1.5)
                    if "die" in req["code"]:
                        await ws.close()
                        return
                    if "error" in req["code"]:
                        r = {
                            "ok": False,
                            "error": {"kind": "runtime", "message": "exec:1: boom"},
                            "stderr": [],
                            "output": "",
                        }
                    else:
                        r = {"ok": True, "returns": [json.dumps(req["code"])], "output": "echo"}
                    await ws.send_json({"type": "result", "req": req["req"], **r})

            tasks = []
            async for msg in ws:
                tasks.append(asyncio.create_task(handle(json.loads(msg.data))))
                if stop.is_set():
                    return


async def main():
    home = tempfile.mkdtemp(prefix="ccmcp_test_")
    env = {**os.environ, "CC_MCP_HOME": home}
    relay = subprocess.Popen(
        [sys.executable, "-m", "computercraft_mcp.relay", "--port", str(PORT)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        await asyncio.sleep(1.5)
        tokens = json.loads((Path(home) / "tokens.json").read_text())
        api = tokens["api"]
        check("tokens differ", tokens["api"] != tokens["computer"])

        code, _ = await asyncio.to_thread(http, "/api/computers", None, "wrong")
        check("bad API token -> 401", code == 401, str(code))

        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(f"ws://127.0.0.1:{PORT}/ws") as ws:
                await ws.send_json({"type": "hello", "token": tokens["api"], "id": 1})
                m = await ws.receive()
                check(
                    "API token rejected as computer token",
                    ws.close_code == 4401 or m.type != aiohttp.WSMsgType.TEXT,
                    str(m),
                )

        code, res = await asyncio.to_thread(http, "/api/call", {"op": "ping"}, api)
        check("no computer -> 409", code == 409, f"{code} {res}")

        stop = asyncio.Event()
        seen = []
        comp = asyncio.create_task(fake_computer(tokens, stop, seen=seen))
        await asyncio.sleep(0.5)
        code, res = await asyncio.to_thread(http, "/api/computers", None, api)
        check("computer listed", code == 200 and res["computers"][0]["id"] == 7, str(res))

        code, res = await asyncio.to_thread(http, "/api/call", {"op": "exec", "code": "1+1"}, api)
        check("exec round trip", res.get("ok") and res["returns"] == ['"1+1"'], str(res))

        code, res = await asyncio.to_thread(http, "/api/call", {"op": "exec", "code": "error()"}, api)
        check(
            "error passes through intact",
            res.get("ok") is False and res["error"]["kind"] == "runtime" and "boom" in res["error"]["message"],
            str(res),
        )

        code, res = await asyncio.to_thread(http, "/api/call", {"op": "exec", "code": "x" * 150_000}, api)
        check("oversized request refused", res.get("error", {}).get("kind") == "too_large", str(res)[:200])

        code, res = await asyncio.to_thread(http, "/api/call", {"op": "exec", "code": "é"}, api)
        check("non-ASCII request survives (replaced)", res.get("ok"), str(res))

        # ping must not wait behind a slow exec holding the lock
        t0 = time.time()
        slow = asyncio.create_task(asyncio.to_thread(http, "/api/call", {"op": "exec", "code": "slow"}, api))
        await asyncio.sleep(0.2)
        _, pong = await asyncio.to_thread(http, "/api/call", {"op": "ping"}, api)
        ping_t = time.time() - t0
        await slow
        check("ping not blocked by running exec", pong.get("ok") and ping_t < 1.0, f"{ping_t:.2f}s")

        # a computer that never answers (paused game): relay times out, queues a cancel, and the next call works
        code, res = await asyncio.to_thread(http, "/api/call", {"op": "exec", "code": "hang", "timeout": 1}, api)
        check("silent computer -> relay_timeout", res.get("error", {}).get("kind") == "relay_timeout", str(res))
        await asyncio.sleep(0.3)
        check("relay queued a cancel behind the timed-out job", "cancel" in seen, str(seen))
        code, res = await asyncio.to_thread(http, "/api/call", {"op": "exec", "code": "1+2"}, api)
        check("next request works after a timeout", res.get("ok"), str(res))

        # disconnect mid-call
        code, res = await asyncio.to_thread(http, "/api/call", {"op": "exec", "code": "die"}, api)
        check("disconnect mid-call reported", res.get("error", {}).get("kind") == "disconnected", str(res))
        await asyncio.sleep(0.3)
        code, res = await asyncio.to_thread(http, "/api/call", {"op": "ping", "computer": 7}, api)
        check("gone computer -> 404", code == 404, f"{code} {res}")
        stop.set()
        comp.cancel()
    finally:
        relay.terminate()
        try:
            relay.wait(5)
        except subprocess.TimeoutExpired:
            relay.kill()
    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
