import json

import pytest

from computercraft_mcp import formatting
from computercraft_mcp.formatting import FORMATTERS, format_result

NETWORK_NOTE = (
    "Names can change after rewiring or rebuilding (ship_* zone names derive from the bow): "
    "find peripherals by name prefix at run time, do not hard-code them."
)
DEVICES = [
    {"name": "dev_b", "types": ["widget"], "methods": ["setY", "getX"]},
    {"name": "dev_a", "types": ["widget"], "methods": ["getX", "setY"]},
]
GROUP = "\ntype widget  x2\n  names: dev_a, dev_b\n  methods: getX, setY"
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
        'OK [0.12s]\nreturns: 42, "hello"\noutput:\nprinted',
    ),
    (
        "shell",
        {"ok": True, "output": "captured-tail", "truncated": True, "stderr": ["warning"], "duration": 1},
        "OK [1.00s]\nstderr:\n  warning\noutput: (truncated, tail kept)\ncaptured-tail",
    ),
    (
        "read",
        {"ok": True, "data": "héllo\n", "next_offset": 7, "eof": False, "duration": 0},
        "OK [0.00s]\n[more data: call again with offset=7]\nhéllo\n",
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
        "OK\nd rom/\n     7 small.lua\n  1234 large.lua",
    ),
    ("peripherals", {"ok": True, "peripherals": DEVICES}, "OK\n2 peripherals\n" + NETWORK_NOTE + "\n" + GROUP),
    (
        "log",
        {
            "ok": True,
            "lines": ["normal", "red"],
            "red": [2],
            "errors": [{"id": 3, "when": "12:34", "text": "bad\nvalue"}],
            "last_error_id": 3,
        },
        "OK\nlast_error_id: 3\nterminal (! = red text):\n  normal\n! red\nprintError log:\n  #3 [12:34] bad\nvalue",
    ),
    ("cancel", {"ok": True, "cancelled": True}, "OK\ncancelled: true"),
    (
        "ping",
        {"ok": True, "id": 0, "label": "my-label", "clock": 12.5, "day": 3, "host": "CC", "free": 100},
        "OK\nid=0, label=my-label, clock=12.5, day=3, host=CC, free=100",
    ),
    (
        "computers",
        {
            "computers": [
                {"id": 0, "label": "my-label", "host": "CC", "connected_for_s": 15},
                {"id": 1, "host": "CC", "connected_for_s": 0},
            ]
        },
        "OK\nid=0, label=my-label, host=CC, connected_for_s=15\nid=1, label=None, host=CC, connected_for_s=0",
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
        + "\nmodem: side=left, wireless=False, remote_count=2\n"
        + GROUP,
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
        'OK\n3 call(s) in 10 ms\ndev_a.setY(2, 40) -> 42, "forty-two"'
        "\ndev_b.boom() -> ERROR bad value\ndev_a.clear() -> (no value)",
    ),
    (
        "push",
        {"ok": True, "chars": 120001, "chunks": 3, "size": 120003, "duration": 0.25},
        "OK [0.25s]\nuploaded 120001 chars in 3 chunk(s); remote size 120003 bytes",
    ),
    ("pull", {"ok": True, "bytes": 120003, "path": "/tmp/pulled.txt"}, "OK\nsaved 120003 bytes to /tmp/pulled.txt"),
]


@pytest.mark.parametrize(("op", "res", "expected"), SNAPSHOTS, ids=[case[0] for case in SNAPSHOTS])
def test_formatter_snapshot(op, res, expected):
    assert format_result(op, res, calls=CALLS) == expected
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
    assert format_result(op, res) == expected + f"\n  {kind}: failed"


def test_hint_snapshot():
    assert format_result("computers", {"ok": False, "error": {"kind": "no_target", "message": "choose"}}) == (
        "ERROR\n  hint: Several computers are connected: repeat the call with computer=<id> (cc_computers lists them)."
        "\n  no_target: choose"
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
        "\n  no_target: choose\n  frame\nstderr:\n  warning"
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
        "ERROR [1.25s]\n  runtime: boom\n  frame1\n  frame2\nstderr:\n  printed error\n  another error"
        "\nreturns: 42\noutput: (truncated, tail kept)\nbefore crash"
    )


@pytest.mark.parametrize("op", ["exec", "shell", "read", "write", "list", "peripherals", "push"])
@pytest.mark.parametrize("ok", [True, False])
def test_all_job_ops_keep_capture(op, ok):
    res = {"ok": ok, "output": "captured", "stderr": ["warning"], "truncated": True, "duration": 0.25}
    text = format_result(op, res)
    assert text.startswith("OK [0.25s]\n" if ok else "ERROR [0.25s]\n")
    assert "[0.25s]\nstderr:\n  warning" in text
    assert text.endswith("output: (truncated, tail kept)\ncaptured")


@pytest.mark.parametrize(
    ("res", "expected"),
    [
        ({"ok": True, "data": "", "eof": True}, "OK\n"),
        ({"ok": True, "data": "tail", "next_offset": 17, "eof": True}, "OK\ntail"),
        ({"ok": True, "data": "", "next_offset": 0, "eof": False}, "OK\n[more data: call again with offset=0]\n"),
        ({"ok": True, "next_offset": 8, "eof": False}, "OK\n[more data: call again with offset=8]"),
        (
            {"ok": True, "data": "trimmed", "next_offset": 7, "size": 60000, "eof": False},
            "OK\n[more data: call again with offset=7]\ntrimmed",
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
            "OK\nid=1, label=None, clock=None, day=None, host=None, free=unlimited",
        ),
    ],
)
def test_empty_and_optional_fields(op, res, expected):
    assert format_result(op, res) == expected


@pytest.mark.parametrize("op", ["network", "calls"])
@pytest.mark.parametrize("returns", [None, [], {}, ["bad JSON"], ["42"], ["[]"], ["{}"], [json.dumps("bad JSON")]])
def test_snippet_falls_back_to_exec(op, returns):
    res = {"ok": True, "returns": returns, "output": "printed"}
    assert format_result(op, res) == format_result("exec", res)


def test_snippet_headers_and_output():
    for op, doc in [("network", {"devices": []}), ("calls", {"results": []})]:
        text = format_result(op, reply(doc, duration=1, stderr=["warning"], output="printed"))
        assert text.startswith("OK [1.00s]\nstderr:\n  warning\n")
        assert text.endswith("output:\nprinted")


def test_peripheral_methods_errors():
    res = {
        "ok": True,
        "peripherals": [
            {"name": "left", "types": ["modem"], "methods_error": "offline"},
            {"name": "right", "types": ["widget"], "methods": {}},
        ],
    }
    assert format_result("peripherals", res) == (
        "OK\n2 peripherals\n" + NETWORK_NOTE + "\n\ntype modem  x1\n  names: left\n  left: methods error: offline"
        "\n\ntype widget  x1\n  names: right\n  methods: (none)"
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
        + "\n\ntype minecraft:chest/inventory  x2\n  names: chest_a, chest_b\n  methods: list, size"
    )


def test_calls_without_request_context():
    assert format_result("calls", reply({"results": [{"ok": True, "values": [42]}]})) == (
        "OK\n0 call(s) in ? ms\n?.?() -> 42"
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
    ) == ('ERROR [2.00s]\n  new: bad\n{\n  "payload": {\n    "value": "hello"\n  }\n}')


@pytest.mark.parametrize(("op", "res", "expected"), SNAPSHOTS, ids=[case[0] for case in SNAPSHOTS])
def test_computer_strings_use_emission_helper(monkeypatch, op, res, expected):
    seen = []

    def mark(text, source):
        seen.append((text, source))
        return "<data>"

    monkeypatch.setattr(formatting, "_untrusted", mark)
    text = format_result(op, res, calls=CALLS)
    for value, _source in seen:
        if value and not value.isnumeric():
            assert value not in text
    assert text.startswith("OK")


def test_error_and_log_emission_sources(monkeypatch):
    sources = set()

    def mark(text, source):
        sources.add(source)
        return "<data>"

    monkeypatch.setattr(formatting, "_untrusted", mark)
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
        "ERROR\n  <data>\n  <data>\nstderr:\n<data>\nlast_error_id: 1\nterminal (! = red text):"
        "\n<data>\nprintError log:\n  #1 [<data>] <data>"
    )
    assert sources == {"error", "traceback", "stderr", "terminal", "printError.when", "printError.text"}


def test_server_metadata_bypasses_emission_helper(monkeypatch):
    monkeypatch.setattr(formatting, "_untrusted", lambda text, source: "<data>")
    assert format_result("push", {"ok": True, "chars": 12, "chunks": 1, "size": 14, "duration": 0.5}) == (
        "OK [0.50s]\nuploaded 12 chars in 1 chunk(s); remote size 14 bytes"
    )
    assert format_result("read", {"ok": True, "data": "hello", "eof": False, "next_offset": 5}) == (
        "OK\n[more data: call again with offset=5]\n<data>"
    )


def test_local_path_bypasses_emission_helper(monkeypatch):
    def unexpected(text, source):
        pytest.fail(f"local path must not be treated as computer data: {source}")

    monkeypatch.setattr(formatting, "_untrusted", unexpected)
    assert format_result("pull", {"ok": True, "bytes": 12, "path": "/tmp/pulled.txt"}) == (
        "OK\nsaved 12 bytes to /tmp/pulled.txt"
    )


def test_call_request_bypasses_helper_but_results_do_not(monkeypatch):
    seen = []

    def mark(text, source):
        seen.append((text, source))
        return "<data>"

    monkeypatch.setattr(formatting, "_untrusted", mark)
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
        'OK\n2 call(s) in 10 ms\nchest.list("slot") -> <data>\nchest.size() -> ERROR <data>'
    )
    assert seen == [('"items"', "call.values"), ("offline", "call.error")]


@pytest.mark.parametrize(
    ("op", "res"),
    [
        ("exec", {"returns": ["COMPUTER_TEXT"], "output": "COMPUTER_TEXT", "stderr": ["COMPUTER_TEXT"]}),
        ("read", {"data": "COMPUTER_TEXT"}),
        ("list", {"entries": [{"name": "COMPUTER_TEXT", "isDir": True}]}),
        (
            "peripherals",
            {
                "peripherals": [
                    {
                        "name": "COMPUTER_TEXT",
                        "types": ["COMPUTER_TEXT"],
                        "methods": ["COMPUTER_TEXT"],
                        "methods_error": "COMPUTER_TEXT",
                    }
                ]
            },
        ),
        ("ping", {"label": "COMPUTER_TEXT", "host": "COMPUTER_TEXT"}),
        ("computers", {"computers": [{"label": "COMPUTER_TEXT", "host": "COMPUTER_TEXT"}]}),
        ("network", reply({"devices": [], "modems": [{"side": "COMPUTER_TEXT", "local_name": "COMPUTER_TEXT"}]})),
        (
            "calls",
            reply(
                {
                    "results": [
                        {"ok": True, "values": [{"name": "COMPUTER_TEXT"}]},
                        {"ok": False, "error": "COMPUTER_TEXT"},
                    ]
                }
            ),
        ),
        ("future", {"new_field": "COMPUTER_TEXT"}),
    ],
)
def test_no_computer_text_bypasses_helper(monkeypatch, op, res):
    seen = []

    def mark(text, source):
        seen.append(text)
        return "<data>"

    monkeypatch.setattr(formatting, "_untrusted", mark)
    assert "COMPUTER_TEXT" not in format_result(op, res)
    assert any("COMPUTER_TEXT" in text for text in seen)
