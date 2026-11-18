import json

import pytest

from computercraft_mcp import cc_lua


@pytest.mark.parametrize(
    ("text", "level"),
    [("plain", 1), ("]]", 1), ("]=]", 2), ("]==]", 1), ("]] ]=] ]==] trailing]", 3), ("trailing]", 1)],
)
def test_long_string(text, level):
    literal = cc_lua.long_string(text)
    opening = "[" + "=" * level + "["
    closing = "]" + "=" * level + "]"
    assert literal.startswith(opening)
    assert literal.endswith(closing)
    assert literal[len(opening) : -len(closing)] == text
    assert closing not in text
    assert literal.find(closing) == len(opening) + len(text)


def test_json_literal_is_ascii_and_decodes_original_value():
    value = {"text": "é—✓ 😀 ]=]", "args": [1, None, True]}
    literal = cc_lua.json_literal(value)
    assert literal.isascii()
    assert literal == f"textutils.unserialiseJSON({cc_lua.long_string(json.dumps(value))})"
    payload = literal.removeprefix("textutils.unserialiseJSON([==[").removesuffix("]==])")
    assert json.loads(payload) == value


@pytest.mark.parametrize("value", [{"answer": 42}, [1, "two", None]])
def test_parse_double_encoded_json(value):
    assert cc_lua.parse_returned_json({"returns": [json.dumps(json.dumps(value)), "ignored"]}) == value


@pytest.mark.parametrize(
    "result",
    [{}, {"returns": []}, {"returns": {}}, {"returns": ["not JSON"]}, {"returns": ["42"]}, {"returns": ["null"]}],
)
def test_parse_missing_or_non_document_json(result):
    assert cc_lua.parse_returned_json(result) is None


@pytest.mark.parametrize("parallel", [False, True])
def test_calls_code_packs_argument_count(parallel):
    calls = [
        {"peripheral": "left", "method": "set", "args": [None, 42, None]},
        {"peripheral": "right", "method": "get"},
    ]
    code = cc_lua.calls_code(calls, parallel)
    assert "%(" not in code
    assert "local n = 2" in code
    assert f"local parallel_mode = {str(parallel).lower()}" in code
    packed = [
        {"p": "left", "m": "set", "args": [None, 42, None], "n": 3},
        {"p": "right", "m": "get", "args": [], "n": 0},
    ]
    assert cc_lua.json_literal(packed) in code
    assert "table.unpack(c.args or {}, 1, nargs)" in code


@pytest.mark.parametrize("methods", [False, True])
def test_network_code_fills_placeholders(methods):
    code = cc_lua.network_code(methods)
    assert "%(" not in code
    assert f"local want_methods = {str(methods).lower()}" in code
    assert "peripheral.getNames()" in code
    assert "return textutils.serialiseJSON(out)" in code


def test_format_calls():
    calls = [
        {"peripheral": "left", "method": "set", "args": [42]},
        {"peripheral": "right", "method": "get"},
        {"peripheral": "top", "method": "reset"},
    ]
    doc = {
        "n": 3,
        "elapsed_ms": 12,
        "results": [{"ok": True, "values": [42, "yes"]}, {"ok": False, "error": "boom"}, {"ok": True}],
    }
    text = cc_lua.format_calls(doc, calls)
    assert "3 call(s)" in text
    assert "12 ms" in text
    assert 'left.set(42) -> 42, "yes"' in text
    assert "right.get() -> ERROR boom" in text
    assert "top.reset() -> (no value)" in text


def test_format_network_groups_devices():
    doc = {
        "on_ship": True,
        "ship_mass": 123,
        "modems": [{"side": "left", "local_name": "computer_7"}],
        "devices": [
            {"name": "dev_a", "types": ["widget"], "methods": ["get", "set"]},
            {"name": "dev_b", "types": ["widget"], "methods": ["get", "set"]},
        ],
    }
    text = cc_lua.format_network(doc)
    assert "2 peripherals" in text
    assert "on_ship=True" in text
    assert "ship_mass=123" in text
    assert "computer_7" in text
    assert "type widget  x2" in text
    assert "dev_a, dev_b" in text
    assert text.count("methods:") == 1
    assert "get, set" in text
    assert "0 peripherals" in cc_lua.format_network({})
