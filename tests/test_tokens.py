import json
import logging
import os
import stat
from contextlib import asynccontextmanager

import pytest
from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from computercraft_mcp import bridge_client, relay
from computercraft_mcp.config import get_config, reset_config
from computercraft_mcp.formatting import format_result

API, SHARED, TOKEN_3, TOKEN_5 = "API-SECRET-A", "SHARED-SECRET-S", "COMPUTER-3-SECRET", "COMPUTER-5-SECRET"
TOKENS = {"api": API, "computer": SHARED, "computers": {"3": TOKEN_3, "5": TOKEN_5}}


@pytest.fixture(autouse=True)
def home(monkeypatch, tmp_path, caplog):
    for name in ("CC_MCP_HOME", "CC_MCP_URL", "CC_MCP_API_TOKEN", "CC_MCP_COMPUTER_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CC_MCP_HOME", str(tmp_path / "home"))
    reset_config()
    caplog.set_level(logging.DEBUG, logger="computercraft_mcp")
    yield tmp_path / "home"
    reset_config()


def stored(home):
    return json.loads((home / "tokens.json").read_text(encoding="utf-8"))


def relay_messages(caplog, level, contains=""):
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == relay.log.name and r.levelno == level and contains in r.getMessage()
    ]


def assert_no_secrets_logged(caplog, *token_sets):
    formatter = logging.Formatter(relay.LOG_FORMAT)
    for record in caplog.records:
        for token in (s for tokens in token_sets for s in relay.secret_values(tokens)):
            assert token not in formatter.format(record)


@asynccontextmanager
async def relay_client(tokens=TOKENS, reload=None):
    async with TestClient(TestServer(relay.make_app(tokens, reload), port=0)) as client:
        yield client


async def hello(client, cid, token):
    """Connect as computer `cid`; returns the open websocket, or the close code if refused."""
    ws = await client.ws_connect("/ws")
    await ws.send_json({"type": "hello", "id": cid, "token": token, "label": "lbl", "host": "test"})
    message = await ws.receive(timeout=1)
    if message.type == WSMsgType.TEXT and json.loads(message.data)["type"] == "welcome":
        return ws
    assert message.type == WSMsgType.CLOSE
    await ws.close()
    return message.data


async def computers(client, api=API):
    async with client.get("/api/computers", headers={"Authorization": "Bearer " + api}) as response:
        assert response.status == 200
        return {c["id"]: c for c in (await response.json())["computers"]}


# ---------------------------------------------------------------------------------------------- the id binding


def test_computer_id():
    for good, want in [(0, 0), (3, 3), (3.0, 3), (2**31 - 1, 2**31 - 1)]:
        assert relay.computer_id(good) == want
    for bad in [None, "3", True, False, 3.5, -1, 2**31, float("inf"), float("nan"), [3], {"id": 3}]:
        assert relay.computer_id(bad) is None


@pytest.mark.parametrize(
    ("cid", "token", "auth", "reason"),
    [
        (3, TOKEN_3, "own_token", None),
        (4, TOKEN_3, None, "token not valid for this id (it is computer #3's token)"),
        (5, TOKEN_3, None, "token not valid for this id (it is computer #3's token)"),
        (3, SHARED, None, "shared token not valid for this id (it has its own token)"),
        (4, SHARED, "shared_token", None),
        (4, API, None, "wrong token"),
        (3, API, None, "wrong token"),
        (3, "", None, "wrong token"),
        (3, None, None, "wrong token"),
        (3, 12345, None, "wrong token"),
        (3, "é-not-ascii", None, "wrong token"),
        (3, "\ud800", None, "wrong token"),
    ],
)
def test_check_token(cid, token, auth, reason):
    assert relay.check_token(TOKENS, cid, token) == (auth, reason)


def test_empty_tokens_never_match():
    assert relay.check_token({"computer": "", "computers": {"3": ""}}, 3, "") == (None, "wrong token")
    assert relay.check_token({"computer": ""}, 4, "") == (None, "wrong token")
    assert not relay.same_secret("", "")
    assert relay.same_secret("é", "é") and not relay.same_secret("é", "e")


@pytest.mark.asyncio
async def test_token_for_3_cannot_connect_as_4(caplog):
    async with relay_client() as client:
        assert await hello(client, 4, TOKEN_3) == 4401
        ws = await hello(client, 3, TOKEN_3)
        assert not isinstance(ws, int)
        # The holder of #3's token cannot take over #5's connection either.
        ws5 = await hello(client, 5, TOKEN_5)
        assert await hello(client, 5, TOKEN_3) == 4401
        listed = await computers(client)
        assert set(listed) == {3, 5}
        assert listed[3]["auth"] == listed[5]["auth"] == "own_token"
        assert listed[5]["replacements"] == 0
        rejected = relay_messages(caplog, logging.WARNING, "rejected hello")
        assert len(rejected) == 2
        assert all("reason=token not valid for this id (it is computer #3's token)" in line for line in rejected)
        assert "claimed_id=4" in rejected[0] and "claimed_id=5" in rejected[1]
        await ws.close()
        await ws5.close()
    assert_no_secrets_logged(caplog, TOKENS)


@pytest.mark.asyncio
async def test_shared_token_is_deprecated_and_refused_for_ids_with_their_own(caplog):
    async with relay_client() as client:
        ws = await hello(client, 7, SHARED)
        assert not isinstance(ws, int)
        warning = relay_messages(caplog, logging.WARNING, "shared computer token")
        assert len(warning) == 1
        assert "computer #7 connected" in warning[0] and "deprecated" in warning[0]
        assert "computercraft-mcp-relay --setup --computer 7" in warning[0]
        assert (await computers(client))[7]["auth"] == "shared_token"
        assert await hello(client, 3, SHARED) == 4401
        assert (
            "reason=shared token not valid for this id (it has its own token) claimed_id=3"
            in (relay_messages(caplog, logging.WARNING, "rejected hello")[0])
        )
        ws3 = await hello(client, 3, TOKEN_3)
        assert len(relay_messages(caplog, logging.WARNING, "shared computer token")) == 1
        await ws.close()
        await ws3.close()
    assert_no_secrets_logged(caplog, TOKENS)


@pytest.mark.asyncio
@pytest.mark.parametrize("cid", [None, "3", True, 3.5, -1, 2**31, [3]])
async def test_bad_ids_are_rejected(caplog, cid):
    async with relay_client() as client:
        assert await hello(client, cid, TOKEN_3) == 4401
        assert "reason=bad id claimed_id=-" in relay_messages(caplog, logging.WARNING, "rejected hello")[0]
        assert await computers(client) == {}


@pytest.mark.asyncio
async def test_integral_float_id_is_that_id():
    async with relay_client() as client:
        ws = await hello(client, 3.0, TOKEN_3)
        assert set(await computers(client)) == {3}
        await ws.close()


@pytest.mark.asyncio
async def test_non_ascii_tokens_are_refused_not_crashing(caplog):
    async with relay_client() as client:
        assert await hello(client, 3, "tökén") == 4401
        assert await hello(client, 3, "\ud800") == 4401
        async with client.get("/api/computers", headers={"Authorization": "Bearer tökén"}) as response:
            assert response.status == 401
        assert relay_messages(caplog, logging.WARNING, "unauthorized API request")


@pytest.mark.asyncio
async def test_token_revoked_while_replacing_is_refused(caplog, monkeypatch):
    revoked = {**TOKENS, "computers": {"3": "NEW-COMPUTER-3-SECRET", "5": TOKEN_5}}
    pending = []
    original_close = relay.web.WebSocketResponse.close

    async def close_with_reload(self, **kwargs):
        if kwargs.get("code") == 4000 and not pending:
            # tokens.json changes, and another request reloads it, while the old connection is closing.
            pending.append(revoked)
            assert await computers(client) == {}
        return await original_close(self, **kwargs)

    monkeypatch.setattr(relay.web.WebSocketResponse, "close", close_with_reload)
    async with relay_client(TOKENS, lambda: pending.pop() if pending else None) as client:
        first = await hello(client, 3, TOKEN_3)
        assert await hello(client, 3, TOKEN_3) == 4401
        assert (await first.receive(timeout=1)).type == WSMsgType.CLOSE
        assert await computers(client) == {}
        assert "reason=wrong token claimed_id=3" in relay_messages(caplog, logging.WARNING, "rejected hello")[-1]
    assert_no_secrets_logged(caplog, revoked)


@pytest.mark.asyncio
async def test_replacements_are_visible_in_api_computers(caplog):
    async with relay_client() as client:
        first = await hello(client, 3, TOKEN_3)
        info = (await computers(client))[3]
        assert info["remote"] == "127.0.0.1"
        assert (info["replacements"], info["last_replaced_s_ago"], info["previous_remote"]) == (0, None, None)
        second = await hello(client, 3, TOKEN_3)
        assert (await first.receive(timeout=1)).type == WSMsgType.CLOSE
        assert first.close_code == 4000
        info = (await computers(client))[3]
        assert info["replacements"] == 1 and info["last_replaced_s_ago"] == 0
        assert info["previous_remote"] == "127.0.0.1" and info["auth"] == "own_token"
        third = await hello(client, 3, TOKEN_3)
        assert (await second.receive(timeout=1)).type == WSMsgType.CLOSE
        assert (await computers(client))[3]["replacements"] == 2
        warnings = relay_messages(caplog, logging.WARNING, "replaced connection")
        assert len(warnings) == 2 and warnings[1].endswith("replacements=2")
        assert "old_remote=127.0.0.1 new_remote=127.0.0.1" in warnings[1]
        await third.close()


# ------------------------------------------------------------------------------------------------ tokens.json


def test_fresh_tokens_file_is_created_exclusively_and_private(home, monkeypatch):
    calls = []
    real_open = os.open

    def spy_open(path, flags, mode=0o777, *args, **kwargs):
        calls.append((os.fspath(path), flags, mode))
        return real_open(path, flags, mode, *args, **kwargs)

    monkeypatch.setattr(relay.os, "open", spy_open)
    tokens = relay.load_tokens()
    assert tokens["api"] and tokens["computer"] and tokens["api"] != tokens["computer"]
    assert "computers" not in tokens
    path = home / "tokens.json"
    assert calls == [(str(path), relay.PRIVATE_FILE_FLAGS, 0o600)]
    assert relay.PRIVATE_FILE_FLAGS & os.O_EXCL and relay.PRIVATE_FILE_FLAGS & os.O_CREAT
    assert stored(home) == tokens
    # An update goes through a private temp file in the same folder, never through the target itself.
    relay.token_for_computer(3)
    temp, flags, mode = calls[1]
    assert os.path.dirname(temp) == str(home) and temp != str(path)
    assert (flags, mode) == (relay.PRIVATE_FILE_FLAGS, 0o600)
    assert sorted(p.name for p in home.iterdir()) == ["tokens.json"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions; on Windows the file inherits the folder's ACL")
def test_tokens_file_mode_is_0600(home):
    umask = os.umask(0o022)
    try:
        relay.load_tokens()
        path = home / "tokens.json"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(home.stat().st_mode) == 0o700
        relay.token_for_computer(3)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        relay.rotate_tokens("all")
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    finally:
        os.umask(umask)


def test_existing_file_is_never_opened_for_create(home):
    home.mkdir()
    path = home / "tokens.json"
    path.write_text("keep me")
    with pytest.raises(FileExistsError):
        relay.save_tokens(path, TOKENS, create=True)
    assert path.read_text() == "keep me"


def test_update_is_atomic(home, monkeypatch):
    relay.load_tokens()
    before = stored(home)
    real_replace = os.replace

    def broken_replace(src, dst):
        raise OSError("disk on fire")

    monkeypatch.setattr(relay.os, "replace", broken_replace)
    with pytest.raises(OSError, match="disk on fire"):
        relay.token_for_computer(3)
    assert stored(home) == before
    assert sorted(p.name for p in home.iterdir()) == ["tokens.json"]
    monkeypatch.setattr(relay.os, "replace", real_replace)

    def broken_fsync(fd):
        raise OSError("write failed")

    monkeypatch.setattr(relay.os, "fsync", broken_fsync)
    with pytest.raises(OSError, match="write failed"):
        relay.rotate_tokens("all")
    assert stored(home) == before
    assert sorted(p.name for p in home.iterdir()) == ["tokens.json"]


def test_replace_retries_while_a_reader_holds_the_file_on_windows(home, monkeypatch):
    relay.load_tokens()
    real_replace = os.replace
    failures = []

    def busy_replace(src, dst):
        if len(failures) < 2:
            failures.append(dst)
            raise PermissionError("in use")
        real_replace(src, dst)

    monkeypatch.setattr(relay.os, "replace", busy_replace)
    monkeypatch.setattr(relay.time, "sleep", lambda s: None)
    if os.name == "nt":
        token, new = relay.token_for_computer(3)
        assert new and stored(home)["computers"]["3"] == token
    else:
        with pytest.raises(PermissionError):
            relay.token_for_computer(3)
    assert sorted(p.name for p in home.iterdir()) == ["tokens.json"]


def test_old_files_load_unchanged(home):
    home.mkdir()
    path = home / "tokens.json"
    path.write_text(json.dumps({"api": "a", "computer": "c"}))
    stamp = relay.file_stamp(path)
    assert relay.load_tokens() == {"api": "a", "computer": "c"}
    assert relay.file_stamp(path) == stamp


@pytest.mark.parametrize(
    ("data", "problem"),
    [
        ([], "expected a JSON object"),
        ({"api": ""}, '"api" must be a non-empty string'),
        ({"computer": 5}, '"computer" must be a non-empty string'),
        ({"computers": ["x"]}, '"computers" must map computer ids to tokens'),
        ({"computers": {"03": "SECRET-VALUE"}}, "must be a computer id"),
        ({"computers": {"-1": "SECRET-VALUE"}}, "must be a computer id"),
        ({"computers": {"SECRET-VALUE": "3"}}, "must be a computer id"),
        ({"computers": {"2147483648": "SECRET-VALUE"}}, "must be a computer id"),
        ({"computers": {"3": ""}}, '"computers" entry 3 must be a non-empty string'),
        ({"computers": {"3": None}}, '"computers" entry 3 must be a non-empty string'),
    ],
)
def test_malformed_tokens_file_is_refused(home, data, problem, capsys):
    home.mkdir()
    (home / "tokens.json").write_text(json.dumps(data))
    with pytest.raises(ValueError, match=problem) as error:
        relay.load_tokens()
    assert "SECRET-VALUE" not in str(error.value)
    with pytest.raises(SystemExit) as exit_:
        relay.main(["--show-tokens"])
    assert problem in str(exit_.value) and "SECRET-VALUE" not in str(exit_.value)
    assert capsys.readouterr().out == ""


def test_invalid_json_is_refused(home):
    home.mkdir()
    (home / "tokens.json").write_text('{"api": "SECRET-VALUE",')
    with pytest.raises(ValueError, match="is not valid JSON") as error:
        relay.load_tokens()
    assert "SECRET-VALUE" not in str(error.value)


def test_env_overrides_keep_working_and_are_never_saved(home, monkeypatch):
    monkeypatch.setenv("CC_MCP_API_TOKEN", "ENV-API")
    monkeypatch.setenv("CC_MCP_COMPUTER_TOKEN", "ENV-SHARED")
    reset_config()
    assert relay.load_tokens() == {"api": "ENV-API", "computer": "ENV-SHARED"}
    assert not (home / "tokens.json").exists()
    token, _ = relay.token_for_computer(3)
    assert stored(home) == {"computers": {"3": token}}
    tokens = relay.load_tokens()
    assert tokens == {"api": "ENV-API", "computer": "ENV-SHARED", "computers": {"3": token}}
    assert relay.check_token(tokens, 4, "ENV-SHARED") == ("shared_token", None)
    assert relay.check_token(tokens, 3, "ENV-SHARED")[0] is None
    assert bridge_client.api_token() == "ENV-API"
    # With only one override, the other token is generated and saved; the override is not.
    monkeypatch.delenv("CC_MCP_API_TOKEN")
    reset_config()
    tokens = relay.load_tokens()
    assert tokens["computer"] == "ENV-SHARED" and tokens["api"] != "ENV-API"
    assert stored(home) == {"computers": {"3": token}, "api": tokens["api"]}
    assert bridge_client.api_token() == tokens["api"]


# ----------------------------------------------------------------------------------------- a running relay


@pytest.mark.asyncio
async def test_running_relay_follows_setup_and_rotate(home, caplog):
    tokens = relay.load_tokens()
    path = get_config().tokens_file
    async with relay_client(tokens, relay.TokenReloader(path)) as client:
        api = tokens["api"]
        # Not restarted: a token issued now works at once, for its own id only.
        old = await hello(client, 3, tokens["computer"])
        assert (await computers(client, api))[3]["auth"] == "shared_token"
        token_4, _ = relay.token_for_computer(4)
        ws4 = await hello(client, 4, token_4)
        assert not isinstance(ws4, int)
        assert "tokens reloaded" in relay_messages(caplog, logging.INFO, "tokens.json")[0]
        # Issuing #3 its own token cuts off #3's shared-token connection.
        token_3, _ = relay.token_for_computer(3)
        assert set(await computers(client, api)) == {4}
        message = await old.receive(timeout=1)
        assert message.type == WSMsgType.CLOSE and old.close_code == 4401
        assert "computer #3 token no longer valid; disconnecting" in relay_messages(caplog, logging.WARNING)[-1]
        assert await hello(client, 3, tokens["computer"]) == 4401
        ws3 = await hello(client, 3, token_3)
        # Rotating #3 drops it, leaves #4 alone, and the old token stops working.
        changed = relay.rotate_tokens("computer", 3)
        assert set(await computers(client, api)) == {4}
        assert (await ws3.receive(timeout=1)).type == WSMsgType.CLOSE
        assert await hello(client, 3, token_3) == 4401
        ws3 = await hello(client, 3, changed["computers"][3])
        assert not isinstance(ws3, int)
        # Rotating the API token: the old one is refused, the client reads the new one from tokens.json.
        relay.rotate_tokens("api")
        new_api = stored(home)["api"]
        async with client.get("/api/computers", headers={"Authorization": "Bearer " + api}) as response:
            assert response.status == 401
        assert set(await computers(client, bridge_client.api_token())) == {3, 4}
        assert new_api == bridge_client.api_token()
        # A broken edit is ignored with a warning; the relay keeps its tokens.
        path.write_text("{not json", encoding="utf-8")
        assert set(await computers(client, new_api)) == {3, 4}
        assert "keeping the previous tokens" in relay_messages(caplog, logging.WARNING, "tokens.json changed")[0]
        await ws3.close()
        await ws4.close()
    assert_no_secrets_logged(caplog, tokens, {"a": token_3, "b": token_4, "c": new_api}, changed["computers"])


# ------------------------------------------------------------------------------------------------- the CLI


def test_setup_requires_a_computer_id(capsys):
    with pytest.raises(SystemExit) as exit_:
        relay.main(["--setup"])
    assert exit_.value.code == 2
    assert "--setup needs --computer ID" in capsys.readouterr().err


@pytest.mark.parametrize("bad", ["-1", "abc", "3.5", "2147483648", "٣"])
def test_computer_argument_validation(bad, capsys):
    with pytest.raises(SystemExit):
        relay.main(["--setup", "--computer", bad])
    assert "not a computer id" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [["--computer", "3"], ["--rotate", "api", "--computer", "3"], ["--rotate", "all", "--computer", "3"]],
)
def test_computer_needs_setup_or_rotate_computer(argv, capsys):
    with pytest.raises(SystemExit):
        relay.main(argv)
    assert "--computer goes with --setup or --rotate computer" in capsys.readouterr().err


def test_actions_are_exclusive():
    with pytest.raises(SystemExit):
        relay.main(["--setup", "--computer", "3", "--show-tokens"])
    with pytest.raises(SystemExit):
        relay.main(["--rotate", "api", "--setup", "--computer", "3"])


def test_setup_issues_then_reuses_a_token(home, capsys):
    relay.main(["--setup", "--computer", "3", "--port", "9999"])
    out = capsys.readouterr().out
    token = stored(home)["computers"]["3"]
    assert out.startswith("New token for computer #3. It only works for computer #3")
    assert f"    bridge ws://127.0.0.1:9999/ws {token}\n" in out
    assert "wget http://127.0.0.1:9999/bridge.lua bridge.lua" in out
    assert stored(home)["computer"] not in out and stored(home)["api"] not in out
    relay.main(["--setup", "--computer", "3"])
    out = capsys.readouterr().out
    assert out.startswith("Existing token (`--rotate computer --computer 3` replaces it) for computer #3")
    assert token in out and stored(home)["computers"] == {"3": token}
    relay.main(["--setup", "--computer", "0"])
    assert set(stored(home)["computers"]) == {"0", "3"}


@pytest.mark.parametrize(
    ("argv", "api", "shared", "ids"),
    [
        (["--rotate", "api"], True, False, set()),
        (["--rotate", "computer"], False, True, {"3", "5"}),
        (["--rotate", "computer", "--computer", "5"], False, False, {"5"}),
        (["--rotate", "computer", "--computer", "9"], False, False, {"9"}),
        (["--rotate", "all"], True, True, {"3", "5"}),
    ],
)
def test_rotate(home, capsys, argv, api, shared, ids):
    relay.token_for_computer(3)
    relay.token_for_computer(5)
    before = stored(home)
    relay.main([*argv, "--port", "9999"])
    out = capsys.readouterr().out
    after = stored(home)
    assert (after["api"] != before["api"]) == api
    assert (after["computer"] != before["computer"]) == shared
    changed = {key for key, value in after["computers"].items() if before["computers"].get(key) != value}
    assert changed == ids
    assert set(after["computers"]) == {"3", "5"} | ids
    assert ("CC_MCP_API_TOKEN" in out) == api
    assert ("--setup --computer <id>" in out) == shared
    for key in ids:
        assert f"On computer #{key}, type `exit` if the bridge is running, then:" in out
        assert f"    bridge ws://127.0.0.1:9999/ws {after['computers'][key]}" in out
    assert after["api"] not in out and after["computer"] not in out
    assert "A running relay reloads tokens.json" in out
    assert sorted(p.name for p in home.iterdir()) == ["tokens.json"]


def test_rotate_notes_env_overrides(home, monkeypatch, capsys):
    monkeypatch.setenv("CC_MCP_API_TOKEN", "ENV-API")
    monkeypatch.setenv("CC_MCP_COMPUTER_TOKEN", "ENV-SHARED")
    reset_config()
    relay.main(["--rotate", "all"])
    out = capsys.readouterr().out
    assert "CC_MCP_API_TOKEN is set in this shell" in out
    assert "CC_MCP_COMPUTER_TOKEN is set in this shell" in out
    assert "ENV-API" not in json.dumps(stored(home)) and "ENV-SHARED" not in json.dumps(stored(home))


def test_cc_computers_text_shows_the_connection_fields():
    # cc_computers' description asks the model to watch these, so its text must carry them.
    res = {
        "computers": [
            {
                "id": 3,
                "label": "base",
                "host": "CC",
                "connected_for_s": 9,
                "remote": "10.0.0.2",
                "auth": "own_token",
                "replacements": 0,
                "last_replaced_s_ago": None,
                "previous_remote": None,
            },
            {
                "id": 4,
                "label": None,
                "host": "CC",
                "connected_for_s": 1,
                "remote": "10.0.0.9",
                "auth": "shared_token",
                "replacements": 2,
                "last_replaced_s_ago": 5,
                "previous_remote": "10.0.0.3",
            },
        ]
    }
    lines = format_result("computers", res).splitlines()
    assert lines[2] == "id=3, label=base, host=CC, connected_for_s=9, remote=10.0.0.2, auth=own_token, replacements=0"
    assert lines[3] == (
        "id=4, label=None, host=CC, connected_for_s=1, remote=10.0.0.9, auth=shared_token, replacements=2, "
        "last_replaced_s_ago=5, previous_remote=10.0.0.3"
    )
