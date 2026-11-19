import json
import os

import pytest

from computercraft_live_mcp import bridge_client as bc
from computercraft_live_mcp import cc_lua, formatting
from computercraft_live_mcp.formatting import format_result

pytestmark = pytest.mark.usefixtures("deterministic_ids")

BIG = "x" * 150_000  # three chunks: 60000 + 60000 + 30000
DISCONNECTED = {"ok": False, "error": {"kind": "disconnected", "message": "computer disconnected"}}


def exec_reply(doc: dict) -> dict:
    return {"ok": True, "returns": [json.dumps(json.dumps(doc))]}


class FakeComputer:
    """Stands in for bridge_client.call: an in-memory drive that answers `write` and the cc_push snippets.

    The snippet builders are patched to emit JSON tags, so this fake only checks the Python side; the Lua itself
    runs in tests/selftest_cc.py. `fail[(step, n)]` replaces the n-th reply of a step with a canned one."""

    def __init__(self, files=None, dirs=(), free=1_000_000):
        self.files = dict(files or {})
        self.dirs = set(dirs)
        self.free = free
        self.move_error = None
        self.fail = {}
        self.log = []
        self.counts = {}

    def __call__(self, op, computer=None, timeout=30, **args):
        if op == "write":
            step, params = "write", (args["path"], args.get("append"), args["data"])
            self.log.append(("write", args["path"], args.get("append"), len(args["data"])))
        elif op == "exec":
            step, *params = json.loads(args["code"])
            self.log.append((step, *params))
        else:
            raise AssertionError(f"unexpected op {op}")
        self.counts[step] = self.counts.get(step, 0) + 1
        canned = self.fail.get((step, self.counts[step]))
        if canned:
            return canned
        return self.write(*params) if step == "write" else exec_reply(getattr(self, step)(*params))

    def write(self, path, append, data):
        assert path not in self.dirs and not path.startswith("rom/")
        self.files[path] = (self.files.get(path, "") if append else "") + data
        return {"ok": True, "size": len(self.files[path].encode())}

    def preflight(self, target, part, need):
        if target.startswith("rom/"):
            return {"ok": False, "reason": "read_only"}
        if target in self.dirs:
            return {"ok": False, "reason": "is_directory"}
        if part in self.dirs:
            return {"ok": False, "reason": "part_is_directory"}
        free = self.free + len(self.files.get(part, ""))
        return {"ok": False, "reason": "no_space", "free": free, "need": need} if free < need else {"ok": True}

    def finalise(self, target, part):
        if part not in self.files:
            return {"ok": False, "reason": "part_missing"}
        existed = self.files.pop(target, None) is not None
        if self.move_error:
            return {"ok": False, "reason": "move_failed", "message": self.move_error, "target_deleted": existed}
        self.files[target] = self.files.pop(part)
        return {"ok": True, "size": len(self.files[target].encode())}

    def discard(self, part):
        self.files.pop(part, None)
        return {"gone": True}


@pytest.fixture
def computer(monkeypatch):
    fake = FakeComputer()
    monkeypatch.setattr(
        cc_lua, "push_preflight_code", lambda target, part, need: json.dumps(["preflight", target, part, need])
    )
    monkeypatch.setattr(cc_lua, "push_finalise_code", lambda target, part: json.dumps(["finalise", target, part]))
    monkeypatch.setattr(cc_lua, "push_discard_code", lambda part: json.dumps(["discard", part]))
    monkeypatch.setattr(bc, "call", fake)
    return fake


def steps(fake):
    return [entry[0] for entry in fake.log]


# ---------------------------------------------------------------------------------- push
def test_successful_push_writes_the_part_and_finalises_with_one_move(computer):
    computer.files = {"prog.lua": "old"}
    res = bc.push_text(BIG, "prog.lua")
    assert computer.log == [
        ("preflight", "prog.lua", "prog.lua.part", 150_000),
        ("write", "prog.lua.part", None, 60_000),
        ("write", "prog.lua.part", True, 60_000),
        ("write", "prog.lua.part", True, 30_000),
        ("finalise", "prog.lua", "prog.lua.part"),
    ]
    assert computer.files == {"prog.lua": BIG}
    assert res["ok"] is True
    assert (res["chars"], res["sent_chars"], res["chunks"], res["size"]) == (150_000, 150_000, 3, 150_000)
    assert format_result("push", res).splitlines()[1] == "uploaded 150000 chars in 3 chunk(s); remote size 150000 bytes"


def test_chunk_failure_never_touches_the_target_and_removes_the_part(computer):
    computer.files = {"startup.lua": "old"}
    computer.fail[("write", 2)] = DISCONNECTED
    res = bc.push_text(BIG, "startup.lua")
    assert steps(computer) == ["preflight", "write", "write", "discard"]
    assert computer.log[-1] == ("discard", "startup.lua.part")
    assert all(entry[1] == "startup.lua.part" for entry in computer.log if entry[0] == "write")
    assert computer.files == {"startup.lua": "old"}
    assert res["ok"] is False
    assert res["error"]["kind"] == "disconnected"
    assert (res["chars"], res["sent_chars"], res["chunks"]) == (150_000, 60_000, 1)
    assert res["remote_state"] == "unchanged"
    assert res["part_left"] is False
    text = format_result("push", res)
    assert "  error: disconnected" in text
    assert text.splitlines()[-1] == "sent 60000 of 150000 chars in 1 chunk(s); the remote file is unchanged"


def test_cut_connection_leaves_target_and_reports_a_possible_leftover_part(computer):
    computer.files = {"startup.lua": "old"}
    cut = {"ok": False, "error": {"kind": "relay_unreachable", "message": "connection refused"}}
    computer.fail[("write", 2)] = cut
    computer.fail[("discard", 1)] = cut
    res = bc.push_text(BIG, "startup.lua")
    assert "finalise" not in steps(computer)
    assert computer.files["startup.lua"] == "old"
    assert res["part_left"] is True
    assert format_result("push", res).splitlines()[-2:] == [
        "sent 60000 of 150000 chars in 1 chunk(s); the remote file is unchanged",
        "the temporary .part file may remain; the next push to this path overwrites it",
    ]


@pytest.mark.parametrize(
    ("setup", "remote", "kind"),
    [
        ({"free": 10}, "prog.lua", "no_space"),
        ({}, "rom/prog.lua", "read_only"),
        ({"dirs": {"progs"}}, "progs", "is_directory"),
        ({"dirs": {"prog.lua.part"}}, "prog.lua", "is_directory"),
    ],
    ids=["no_space", "read_only", "is_directory", "part_is_directory"],
)
def test_preflight_failure_stops_before_any_write(computer, setup, remote, kind):
    computer.files = {"prog.lua": "old"}
    for key, value in setup.items():
        setattr(computer, key, value)
    res = bc.push_text(BIG, remote)
    assert steps(computer) == ["preflight"]
    assert computer.files == {"prog.lua": "old"}
    assert res["ok"] is False
    assert res["error"]["kind"] == kind
    assert (res["sent_chars"], res["chunks"], res["remote_state"]) == (0, 0, "unchanged")
    lines = format_result("push", res).splitlines()
    assert lines[1] == "  hint: " + formatting.HINTS[kind]
    assert lines[2] == "  error: " + kind
    assert lines[-1] == "sent 0 of 150000 chars in 0 chunk(s); the remote file is unchanged"


def test_no_space_message_has_the_numbers(computer):
    computer.free = 3
    res = bc.push_text("héllo", "a.txt")
    assert computer.log == [("preflight", "a.txt", "a.txt.part", 6)]  # UTF-8 bytes, not chars
    assert res["error"]["message"] == "a.txt: the upload needs 6 bytes, the drive has 3 free"


def test_preflight_counts_a_stale_part_as_free_and_overwrites_it(computer):
    computer.files = {"a.lua": "old", "a.lua.part": "y" * 100}
    computer.free = 50
    res = bc.push_text("z" * 120, "a.lua")
    assert res["ok"] is True
    assert computer.log[1] == ("write", "a.lua.part", None, 120)  # truncates the stale part
    assert computer.files == {"a.lua": "z" * 120}


def test_empty_file_is_one_chunk(computer):
    res = bc.push_text("", "empty.txt")
    assert res["ok"] is True
    assert steps(computer) == ["preflight", "write", "finalise"]
    assert computer.files == {"empty.txt": ""}


def test_trailing_slash_does_not_put_the_part_inside_a_folder(computer):
    bc.push_text("x", "dir/")
    assert computer.log[0] == ("preflight", "dir/", "dir.part", 1)


def test_append_writes_straight_to_the_target_without_finalise(computer):
    computer.files = {"log.txt": "a"}
    res = bc.push_text("b" * 70_000, "log.txt", append=True)
    assert computer.log == [
        ("preflight", "log.txt", None, 70_000),
        ("write", "log.txt", True, 60_000),
        ("write", "log.txt", True, 10_000),
    ]
    assert computer.files == {"log.txt": "a" + "b" * 70_000}
    assert res["ok"] is True
    assert res["size"] == 70_001


def test_failed_append_is_reported_as_not_atomic(computer):
    computer.files = {"log.txt": "a"}
    computer.fail[("write", 2)] = DISCONNECTED
    res = bc.push_text("b" * 70_000, "log.txt", append=True)
    assert steps(computer) == ["preflight", "write", "write"]  # no part, nothing to discard
    assert res["remote_state"] == "append"
    assert res["part_left"] is False
    assert format_result("push", res).splitlines()[-1] == (
        "sent 60000 of 70000 chars in 1 chunk(s); append is not atomic: the remote file may end with part of the upload"
    )


def test_move_failure_after_delete_keeps_the_part(computer):
    computer.files = {"a.lua": "old"}
    computer.move_error = "Out of space"
    res = bc.push_text("new", "a.lua")
    assert steps(computer) == ["preflight", "write", "finalise"]  # no discard: the part is the only copy now
    assert computer.files == {"a.lua.part": "new"}
    assert res["error"]["kind"] == "replace_failed"
    assert "Out of space" in res["error"]["message"]
    assert res["remote_state"] == "part_kept"
    text = format_result("push", res)
    assert "  hint: " + formatting.HINTS["replace_failed"] in text
    assert text.splitlines()[-1].endswith(
        "the old remote file was deleted and the complete upload is in the .part file next to it"
    )


def test_move_failure_without_old_file_discards_the_part(computer):
    computer.move_error = "Access denied"
    res = bc.push_text("new", "a.lua")
    assert steps(computer) == ["preflight", "write", "finalise", "discard"]
    assert computer.files == {}
    assert res["remote_state"] == "unchanged"


def test_unconfirmed_finalise_is_unknown_and_cleans_up(computer):
    computer.files = {"a.lua": "old"}
    computer.fail[("finalise", 1)] = {"ok": False, "error": {"kind": "relay_timeout", "message": "no answer"}}
    res = bc.push_text("new", "a.lua")
    assert steps(computer) == ["preflight", "write", "finalise", "discard"]
    assert computer.files == {"a.lua": "old"}
    assert res["error"]["kind"] == "relay_timeout"
    assert res["remote_state"] == "unknown"


def test_a_reply_cannot_forge_the_push_fields(computer):
    forged = {**DISCONNECTED, "part_left": True, "remote_state": "part_kept", "sent_chars": 999, "chunks": 9}
    computer.fail[("write", 1)] = forged
    res = bc.push_text("new", "log.txt", append=True)
    assert (res["part_left"], res["remote_state"], res["sent_chars"], res["chunks"]) == (False, "append", 0, 0)


def test_preflight_transport_error_is_returned(computer):
    computer.fail[("preflight", 1)] = {"ok": False, "error": {"kind": "not_connected", "message": "offline"}}
    res = bc.push_text("new", "a.lua")
    assert steps(computer) == ["preflight"]
    assert res["error"]["kind"] == "not_connected"
    assert res["remote_state"] == "unchanged"


def test_unparseable_preflight_answer_fails_closed(computer):
    computer.fail[("preflight", 1)] = {"ok": True, "returns": ["nil"]}
    res = bc.push_text("new", "a.lua")
    assert steps(computer) == ["preflight"]
    assert res["error"]["kind"] == "internal"


def test_push_file_reads_the_local_file_exactly(computer, monkeypatch, tmp_path):
    monkeypatch.setenv("CC_MCP_FILES_ROOT", str(tmp_path))
    (tmp_path / "prog.lua").write_bytes("print('é')\r\n".encode())
    res = bc.push_file("prog.lua", "prog.lua")
    assert res["ok"] is True
    assert computer.files == {"prog.lua": "print('é')\r\n"}


# ---------------------------------------------------------------------------------- formatting
def test_new_kinds_are_known_and_hinted():
    for kind in ("no_space", "read_only", "is_directory", "replace_failed"):
        assert kind in formatting.KNOWN_KINDS
        assert kind in formatting.HINTS


def test_failure_line_only_prints_known_states():
    res = {"ok": False, "error": {"kind": "timeout"}, "sent_chars": 1, "chars": 2, "chunks": 1}
    assert format_result("push", {**res, "remote_state": "PWN"}).splitlines()[-1] == "sent 1 of 2 chars in 1 chunk(s)"
    assert format_result("push", {**res, "remote_state": ["x"]}).splitlines()[-1] == "sent 1 of 2 chars in 1 chunk(s)"
    assert "may remain" not in format_result("push", {**res, "part_left": "yes"})


# ---------------------------------------------------------------------------------- Lua snippets
TRICKY = "a]]=]\"'\n.lua"


@pytest.mark.parametrize(
    "code",
    [
        cc_lua.push_preflight_code(TRICKY, TRICKY + ".part", 5),
        cc_lua.push_finalise_code(TRICKY, TRICKY + ".part"),
        cc_lua.push_discard_code(TRICKY + ".part"),
    ],
    ids=["preflight", "finalise", "discard"],
)
def test_snippets_embed_paths_as_json_literals(code):
    assert cc_lua.json_literal(TRICKY + ".part") in code
    assert TRICKY not in code  # never pasted raw into the Lua source
    assert "%(" not in code


def test_append_preflight_has_no_part():
    code = cc_lua.push_preflight_code("log.txt", None, 7)
    assert f"local target, part, need = {cc_lua.json_literal('log.txt')}, nil, 7" in code


# ---------------------------------------------------------------------------------- pull
def read_replies(monkeypatch, replies):
    calls = []

    def call(op, computer=None, timeout=30, **args):
        assert op == "read"
        calls.append(args["offset"])
        return replies[len(calls) - 1]

    monkeypatch.setattr(bc, "call", call)
    return calls


@pytest.fixture
def root(monkeypatch, tmp_path):
    monkeypatch.setenv("CC_MCP_FILES_ROOT", str(tmp_path))
    (tmp_path / "out.txt").write_text("old", encoding="utf-8")
    return tmp_path


def test_pull_replaces_the_file_and_leaves_no_temp_file(monkeypatch, root):
    calls = read_replies(
        monkeypatch,
        [
            {"ok": True, "data": "new\r\n", "eof": False, "next_offset": 5},
            {"ok": True, "data": "é", "eof": True, "next_offset": 7},
        ],
    )
    res = bc.pull_file("remote.txt", "out.txt")
    assert calls == [0, 5]
    assert res == {"ok": True, "bytes": 7, "path": str(root / "out.txt")}
    assert (root / "out.txt").read_bytes() == "new\r\né".encode()
    assert os.listdir(root) == ["out.txt"]


def test_pull_failure_leaves_the_existing_local_file(monkeypatch, root):
    read_replies(monkeypatch, [{"ok": True, "data": "new", "eof": False, "next_offset": 3}, DISCONNECTED])
    res = bc.pull_file("remote.txt", "out.txt")
    assert res["error"]["kind"] == "disconnected"
    assert (root / "out.txt").read_text(encoding="utf-8") == "old"
    assert os.listdir(root) == ["out.txt"]


def test_failed_local_replace_leaves_the_file_and_removes_the_temp_file(monkeypatch, root):
    read_replies(monkeypatch, [{"ok": True, "data": "new", "eof": True, "next_offset": 3}])

    def replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", replace)
    res = bc.pull_file("remote.txt", "out.txt")
    assert res["error"] == {"kind": "local_file", "message": "disk full"}
    assert (root / "out.txt").read_text(encoding="utf-8") == "old"
    assert os.listdir(root) == ["out.txt"]


def test_pull_creates_missing_folders(monkeypatch, root):
    read_replies(monkeypatch, [{"ok": True, "data": "log", "eof": True, "next_offset": 3}])
    res = bc.pull_file("remote.txt", "logs/today/out.txt")
    assert res["ok"] is True
    assert (root / "logs" / "today" / "out.txt").read_text(encoding="utf-8") == "log"
    assert os.listdir(root / "logs" / "today") == ["out.txt"]


def test_pull_into_a_directory_fails_before_downloading(monkeypatch, root):
    read_replies(monkeypatch, [])
    (root / "folder").mkdir()
    res = bc.pull_file("remote.txt", "folder")
    assert res["error"]["kind"] == "local_file"
    assert "is a directory" in res["error"]["message"]
