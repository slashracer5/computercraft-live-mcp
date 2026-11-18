import json

import pytest

from computercraft_mcp import formatting
from computercraft_mcp.formatting import FORMATTERS, format_result

pytestmark = pytest.mark.usefixtures("deterministic_ids")


def block(text, source, number=1, *, inline=False):
    block_id = f"{number:08x}"
    separator = "" if inline else "\n"
    return f'<untrusted source="{source}" id="{block_id}">{separator}{text}{separator}</untrusted id="{block_id}">'


NETWORK_NOTE = (
    "Names can change after rewiring or rebuilding (ship_* zone names derive from the bow): "
    "find peripherals by name prefix at run time, do not hard-code them."
)
DEVICES = [
    {"name": "dev_b", "types": ["widget"], "methods": ["setY", "getX"]},
    {"name": "dev_a", "types": ["widget"], "methods": ["getX", "setY"]},
]
GROUP = "type widget  x2\n  names: dev_a, dev_b\n  methods: getX, setY"
CALLS = [
    {"peripheral": "dev_a", "method": "setY", "args": [2, 40]},
    {"peripheral": "dev_b", "method": "boom"},
    {"peripheral": "dev_a", "method": "clear"},
]


def reply(doc, **fields):
    return {"ok": True, "returns": [json.dumps(json.dumps(doc))], **fields}


SNAPSHOTS = [
    (
        "exec",
        {"ok": True, "returns": ["42", '"hello"'], "output": "printed", "duration": 0.125},
        "OK [0.12s]\nreturns: "
        + block('42, "hello"', "returns", inline=True)
        + "\noutput:\n"
        + block("printed", "output", 2),
    ),
    (
        "shell",
        {"ok": True, "output": "captured-tail", "truncated": True, "stderr": ["warning"], "duration": 1},
        "OK [1.00s]\nstderr:\n"
        + block("warning", "stderr")
        + "\noutput: (truncated, tail kept)\n"
        + block("captured-tail", "output", 2),
    ),
    (
        "read",
        {"ok": True, "data": "héllo\n", "next_offset": 7, "eof": False, "duration": 0},
        "OK [0.00s]\n[more data: call again with offset=7]\n" + block("héllo\n", "file"),
    ),
    ("write", {"ok": True, "size": 1234, "duration": 0.5}, "OK [0.50s]\nremote size 1234 bytes"),
    (
        "list",
        {
            "ok": True,
            "entries": [
                {"name": "rom", "isDir": True, "size": 0},
                {"name": "small.lua", "isDir": False, "size": 7},
                {"name": "large.lua", "isDir": False, "size": 1234},
            ],
        },
        "OK\n" + block("d rom/\n     7 small.lua\n  1234 large.lua", "listing"),
    ),
    (
        "peripherals",
        {"ok": True, "peripherals": DEVICES},
        "OK\n2 peripherals\n" + NETWORK_NOTE + "\n" + block(GROUP, "peripherals"),
    ),
    (
        "log",
        {
            "ok": True,
            "lines": ["normal", "red"],
            "red": [2],
            "errors": [{"id": 3, "when": "12:34", "text": "bad\nvalue"}],
            "last_error_id": 3,
        },
        "OK\nlast_error_id: 3\nterminal (! = red text):\n"
        + block("  normal\n! red", "terminal")
        + "\nprintError log:\n"
        + block("  #3 [12:34] bad\nvalue", "printError", 2),
    ),
    ("cancel", {"ok": True, "cancelled": True}, "OK\ncancelled: true"),
    (
        "ping",
        {"ok": True, "id": 0, "label": "my-label", "clock": 12.5, "day": 3, "host": "CC", "free": 100},
        "OK\n" + block("id=0, label=my-label, clock=12.5, day=3, host=CC, free=100", "ping", inline=True),
    ),
    (
        "computers",
        {
            "computers": [
                {"id": 0, "label": "my-label", "host": "CC", "connected_for_s": 15},
                {"id": 1, "host": "CC", "connected_for_s": 0},
            ]
        },
        "OK\n"
        + block(
            "id=0, label=my-label, host=CC, connected_for_s=15\nid=1, label=None, host=CC, connected_for_s=0",
            "computers",
        ),
    ),
    (
        "network",
        reply(
            {
                "devices": DEVICES,
                "on_ship": True,
                "ship_mass": 120,
                "modems": [{"side": "left", "wireless": False, "remote_count": 2}],
            }
        ),
        "OK\n2 peripherals (on_ship=true, ship_mass=120)\n"
        + NETWORK_NOTE
        + "\n"
        + block("modem: side=left, wireless=False, remote_count=2\n\n" + GROUP, "peripherals"),
    ),
    (
        "calls",
        reply(
            {
                "n": 3,
                "elapsed_ms": 10,
                "results": [
                    {"ok": True, "values": [42, "forty-two"]},
                    {"ok": False, "error": "bad value"},
                    {"ok": True, "values": {}},
                ],
            }
        ),
        "OK\n3 call(s) in 10 ms\ndev_a.setY(2, 40) -> "
        + block('42, "forty-two"', "call", inline=True)
        + "\ndev_b.boom() -> ERROR "
        + block("bad value", "call_error", 2, inline=True)
        + "\ndev_a.clear() -> (no value)",
    ),
    (
        "push",
        {"ok": True, "chars": 120001, "chunks": 3, "size": 120003, "duration": 0.25},
        "OK [0.25s]\nuploaded 120001 chars in 3 chunk(s); remote size 120003 bytes",
    ),
    ("pull", {"ok": True, "bytes": 120003, "path": "/tmp/pulled.txt"}, "OK\nsaved 120003 bytes to /tmp/pulled.txt"),
]


@pytest.mark.parametrize(("op", "res", "expected"), SNAPSHOTS, ids=[case[0] for case in SNAPSHOTS])
def test_formatter_snapshot(op, res, expected, deterministic_ids):
    assert format_result(op, res, calls=CALLS) == expected
    deterministic_ids()
    assert format_result(op, {**res, "future_field": "ignored"}, calls=CALLS) == expected


def test_every_formatter_has_snapshot():
    assert {case[0] for case in SNAPSHOTS} == set(FORMATTERS)


@pytest.mark.parametrize("op", FORMATTERS)
@pytest.mark.parametrize(
    "kind",
    [
        "relay_unreachable",
        "no_target",
        "not_connected",
        "relay_timeout",
        "disconnected",
        "too_large",
        "unauthorized",
        "bad_request",
        "local_file",
    ],
)
def test_relay_and_client_errors(op, kind):
    res = {"ok": False, "error": {"kind": kind, "message": "failed"}}
    expected = "ERROR"
    if kind in formatting.HINTS:
        expected += "\n  hint: " + formatting.HINTS[kind]
    assert format_result(op, res) == expected + f"\n  error: {kind}\n" + block("failed", "error")


def test_hint_snapshot():
    assert format_result("computers", {"ok": False, "error": {"kind": "no_target", "message": "choose"}}) == (
        "ERROR\n  hint: Several computers are connected: repeat the call with computer=<id> (cc_computers lists them)."
        "\n  error: no_target\n" + block("choose", "error")
    )


def test_duration_and_hint_header_order():
    res = {
        "ok": False,
        "duration": 1.25,
        "error": {"kind": "no_target", "message": "choose", "traceback": "frame"},
        "stderr": ["warning"],
    }
    assert format_result("exec", res) == (
        "ERROR [1.25s]\n"
        "  hint: Several computers are connected: repeat the call with computer=<id> (cc_computers lists them)."
        "\n  error: no_target\n" + block("choose\nframe", "error") + "\nstderr:\n" + block("warning", "stderr", 2)
    )


@pytest.mark.parametrize("op", ["exec", "shell", "network", "calls"])
def test_crash_preserves_returns_and_output(op):
    res = {
        "ok": False,
        "error": {"kind": "runtime", "message": "boom", "traceback": " frame1\nframe2 \n"},
        "stderr": ["printed error", "another error"],
        "duration": 1.25,
        "returns": ["42"],
        "output": "before crash",
        "truncated": True,
    }
    assert format_result(op, res) == (
        "ERROR [1.25s]\n  error: runtime\n"
        + block("boom\nframe1\nframe2", "error")
        + "\nstderr:\n"
        + block("printed error\nanother error", "stderr", 2)
        + "\nreturns: "
        + block("42", "returns", 3, inline=True)
        + "\noutput: (truncated, tail kept)\n"
        + block("before crash", "output", 4)
    )


@pytest.mark.parametrize("op", ["exec", "shell", "read", "write", "list", "peripherals", "push"])
@pytest.mark.parametrize("ok", [True, False])
def test_all_job_ops_keep_capture(op, ok):
    res = {"ok": ok, "output": "captured", "stderr": ["warning"], "truncated": True, "duration": 0.25}
    text = format_result(op, res)
    assert text.startswith("OK [0.25s]\n" if ok else "ERROR [0.25s]\n")
    assert "[0.25s]\nstderr:\n" + block("warning", "stderr") in text
    assert text.endswith("output: (truncated, tail kept)\n" + block("captured", "output", 2))


@pytest.mark.parametrize(
    ("res", "expected"),
    [
        ({"ok": True, "data": "", "eof": True}, "OK\n" + block("", "file")),
        ({"ok": True, "data": "tail", "next_offset": 17, "eof": True}, "OK\n" + block("tail", "file")),
        (
            {"ok": True, "data": "", "next_offset": 0, "eof": False},
            "OK\n[more data: call again with offset=0]\n" + block("", "file"),
        ),
        ({"ok": True, "next_offset": 8, "eof": False}, "OK\n[more data: call again with offset=8]"),
        (
            {"ok": True, "data": "trimmed", "next_offset": 7, "size": 60000, "eof": False},
            "OK\n[more data: call again with offset=7]\n" + block("trimmed", "file"),
        ),
    ],
)
def test_read_paging(res, expected):
    assert format_result("read", res) == expected


@pytest.mark.parametrize(
    ("op", "res", "expected"),
    [
        ("list", {"ok": True, "entries": {}}, "OK"),
        ("peripherals", {"ok": True, "peripherals": {}}, "OK\n0 peripherals\n" + NETWORK_NOTE),
        ("network", reply({"devices": {}}), "OK\n0 peripherals\n" + NETWORK_NOTE),
        ("calls", reply({"results": {}, "n": 0, "elapsed_ms": 0}), "OK\n0 call(s) in 0 ms"),
        ("computers", {"computers": []}, "OK\nNo computers connected."),
        ("log", {"ok": True, "lines": {}, "errors": {}, "last_error_id": 4}, "OK\nlast_error_id: 4"),
        ("cancel", {"ok": True, "cancelled": False}, "OK\ncancelled: false"),
        (
            "ping",
            {"ok": True, "id": 1, "free": "unlimited"},
            "OK\n" + block("id=1, label=None, clock=None, day=None, host=None, free=unlimited", "ping", inline=True),
        ),
    ],
)
def test_empty_and_optional_fields(op, res, expected):
    assert format_result(op, res) == expected


@pytest.mark.parametrize("op", ["network", "calls"])
@pytest.mark.parametrize("returns", [None, [], {}, ["bad JSON"], ["42"], ["[]"], ["{}"], [json.dumps("bad JSON")]])
def test_snippet_falls_back_to_exec(op, returns, deterministic_ids):
    res = {"ok": True, "returns": returns, "output": "printed"}
    text = format_result(op, res)
    deterministic_ids()
    assert text == format_result("exec", res)


def test_snippet_headers_and_output(deterministic_ids):
    for op, doc in [("network", {"devices": []}), ("calls", {"results": []})]:
        deterministic_ids()
        text = format_result(op, reply(doc, duration=1, stderr=["warning"], output="printed"))
        assert text.startswith("OK [1.00s]\nstderr:\n" + block("warning", "stderr") + "\n")
        assert text.endswith("output:\n" + block("printed", "output", 2))


def test_peripheral_methods_errors():
    res = {
        "ok": True,
        "peripherals": [
            {"name": "left", "types": ["modem"], "methods_error": "offline"},
            {"name": "right", "types": ["widget"], "methods": {}},
        ],
    }
    assert format_result("peripherals", res) == (
        "OK\n2 peripherals\n"
        + NETWORK_NOTE
        + "\n"
        + block(
            "type modem  x1\n  names: left\n  left: methods error: offline"
            "\n\ntype widget  x1\n  names: right\n  methods: (none)",
            "peripherals",
        )
    )


@pytest.mark.parametrize("op", ["peripherals", "network"])
def test_primary_peripheral_type_keeps_reported_order(op):
    devices = [
        {"name": "chest_b", "types": ["minecraft:chest", "inventory"], "methods": ["size", "list"]},
        {"name": "chest_a", "types": ["minecraft:chest", "inventory"], "methods": ["list", "size"]},
    ]
    res = {"ok": True, "peripherals": devices} if op == "peripherals" else reply({"devices": devices})
    assert format_result(op, res) == (
        "OK\n2 peripherals\n"
        + NETWORK_NOTE
        + "\n"
        + block("type minecraft:chest/inventory  x2\n  names: chest_a, chest_b\n  methods: list, size", "peripherals")
    )


def test_calls_without_request_context():
    assert format_result("calls", reply({"results": [{"ok": True, "values": [42]}]})) == (
        "OK\n0 call(s) in ? ms\n?.?() -> " + block("42", "call", inline=True)
    )


@pytest.mark.parametrize("op", FORMATTERS)
@pytest.mark.parametrize(
    "res",
    [
        {},
        {"ok": False},
        {"ok": False, "error": {"kind": [], "message": None, "traceback": 123}, "duration": "bad"},
        {
            "ok": True,
            "entries": [None, {}],
            "peripherals": [None, {"types": [None, {}], "methods": {}}],
            "computers": [None, {}],
            "lines": [None],
            "red": [{}],
            "errors": [None, {}],
            "returns": {"bad": 1},
        },
        reply({"devices": [None, {}], "modems": [None, {}], "results": [None, {}]}),
    ],
)
def test_missing_or_unexpected_fields_do_not_raise(op, res):
    assert isinstance(format_result(op, res), str)


def test_fields_from_other_operations_do_not_leak():
    res = {
        "ok": True,
        "data": "not exec data",
        "entries": [{"name": "not exec entry"}],
        "lines": ["not exec terminal"],
        "errors": [{"text": "not exec error"}],
    }
    assert format_result("exec", res) == "OK"


def test_unknown_op_generic_snapshot():
    assert format_result(
        "future",
        {"ok": False, "error": {"kind": "new", "message": "bad"}, "duration": 2, "payload": {"value": "hello"}},
    ) == (
        "ERROR [2.00s]\n"
        + block("kind: new\nbad", "error")
        + "\n"
        + block('{\n  "payload": {\n    "value": "hello"\n  }\n}', "result", 2)
    )


def test_error_and_log_blocks():
    res = {
        "ok": False,
        "error": {"kind": "runtime", "message": "boom", "traceback": "frame"},
        "stderr": ["warning"],
        "lines": ["red"],
        "red": [1],
        "errors": [{"id": 1, "when": "noon", "text": "problem"}],
        "last_error_id": 1,
    }
    assert format_result("log", res) == (
        "ERROR\n  error: runtime\n"
        + block("boom\nframe", "error")
        + "\nstderr:\n"
        + block("warning", "stderr", 2)
        + "\nlast_error_id: 1\nterminal (! = red text):\n"
        + block("! red", "terminal", 3)
        + "\nprintError log:\n"
        + block("  #1 [noon] problem", "printError", 4)
    )


def test_server_metadata_stays_outside_blocks():
    assert format_result("push", {"ok": True, "chars": 12, "chunks": 1, "size": 14, "duration": 0.5}) == (
        "OK [0.50s]\nuploaded 12 chars in 1 chunk(s); remote size 14 bytes"
    )
    assert format_result("read", {"ok": True, "data": "hello", "eof": False, "next_offset": 5}) == (
        "OK\n[more data: call again with offset=5]\n" + block("hello", "file")
    )


def test_local_path_stays_outside_blocks():
    assert format_result("pull", {"ok": True, "bytes": 12, "path": "/tmp/pulled.txt"}) == (
        "OK\nsaved 12 bytes to /tmp/pulled.txt"
    )


def test_call_request_stays_outside_but_results_do_not():
    calls = [{"peripheral": "chest", "method": "list", "args": ["slot"]}, {"peripheral": "chest", "method": "size"}]
    res = reply(
        {
            "n": 2,
            "elapsed_ms": 10,
            "results": [
                {"ok": True, "values": ["items"]},
                {"ok": False, "error": "offline"},
            ],
        }
    )
    assert format_result("calls", res, calls=calls) == (
        'OK\n2 call(s) in 10 ms\nchest.list("slot") -> '
        + block('"items"', "call", inline=True)
        + "\nchest.size() -> ERROR "
        + block("offline", "call_error", 2, inline=True)
    )
