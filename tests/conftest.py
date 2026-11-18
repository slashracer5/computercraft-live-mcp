import asyncio
import itertools
import json

import aiohttp
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestServer

from computercraft_mcp import formatting, relay
from computercraft_mcp.config import reset_config


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch, tmp_path):
    # The package reads its environment once (config.get_config); reset it around every test.
    monkeypatch.setenv("CC_MCP_HOME", str(tmp_path / "home"))
    for name in ("CC_MCP_URL", "CC_MCP_API_TOKEN", "CC_MCP_COMPUTER_TOKEN", "CC_MCP_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    reset_config()
    yield
    reset_config()


@pytest.fixture
def deterministic_ids(monkeypatch):
    def reset():
        ids = itertools.count(1)
        monkeypatch.setattr(formatting, "_new_id", lambda: f"{next(ids):08x}")

    reset()
    return reset


@pytest.fixture
def tokens():
    return relay.load_tokens()


class RelayClient:
    def __init__(self, server, session, tokens):
        self.server = server
        self.session = session
        self.tokens = tokens

    async def request(self, path, body=None, token=None):
        headers = {"Authorization": "Bearer " + (self.tokens["api"] if token is None else token)}
        async with self.session.request(
            "GET" if body is None else "POST", self.server.make_url(path), json=body, headers=headers
        ) as response:
            return response.status, await response.json()

    async def call(self, **body):
        return await self.request("/api/call", body)


@pytest_asyncio.fixture
async def relay_client(tokens):
    async with TestServer(relay.make_app(tokens), host="127.0.0.1", port=0) as server:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            yield RelayClient(server, session, tokens)


class FakeComputer:
    def __init__(self, ws):
        self.ws = ws
        self.seen = []
        self.exec_started = asyncio.Event()
        self.release_exec = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.tasks = []

    async def handle(self, request):
        self.seen.append(request)
        op = request["op"]
        if op == "cancel":
            self.cancelled.set()
            result = {"ok": True, "cancelled": True}
        elif op == "ping":
            result = {"ok": True, "id": 7}
        elif op == "exec":
            code = request["code"]
            if code == "hang":
                return
            if code == "slow":
                self.exec_started.set()
                await self.release_exec.wait()
            if code == "die":
                await self.ws.close()
                return
            if code == "error()":
                result = {
                    "ok": False,
                    "error": {"kind": "runtime", "message": "exec:1: boom"},
                    "stderr": [],
                    "output": "",
                }
            else:
                result = {"ok": True, "returns": [json.dumps(code)], "output": "echo"}
        else:
            raise AssertionError(f"unexpected operation: {op}")
        await self.ws.send_json({"type": "result", "req": request["req"], **result})

    async def run(self):
        async for message in self.ws:
            assert message.type == aiohttp.WSMsgType.TEXT
            self.tasks.append(asyncio.create_task(self.handle(json.loads(message.data))))


@pytest_asyncio.fixture
async def computer(relay_client):
    async with relay_client.session.ws_connect(relay_client.server.make_url("/ws")) as ws:
        await ws.send_json(
            {"type": "hello", "token": relay_client.tokens["computer"], "id": 7, "label": "fake", "host": "test"}
        )
        assert (await asyncio.wait_for(ws.receive_json(), 10))["type"] == "welcome"
        fake = FakeComputer(ws)
        reader = asyncio.create_task(fake.run())
        try:
            yield fake
        finally:
            for task in (reader, *fake.tasks):
                task.cancel()
            outcomes = await asyncio.gather(reader, *fake.tasks, return_exceptions=True)
            for outcome in outcomes:
                if isinstance(outcome, BaseException) and not isinstance(outcome, asyncio.CancelledError):
                    raise outcome
