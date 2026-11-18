import copy

import pytest

from computercraft_mcp import relay


def test_normalise_empty_tables():
    fields = ("stderr", "returns", "entries", "peripherals", "lines", "red", "errors")
    result = relay.normalise(dict.fromkeys(fields, {}))
    assert result == dict.fromkeys(fields, [])
    assert relay.normalise({"peripherals": [{"name": "left", "types": {}, "methods": {}}]}) == {
        "peripherals": [{"name": "left", "types": [], "methods": []}]
    }


def test_normalise_decodes_utf8_in_all_text_fields():
    text = "héllo — ✓"
    raw = text.encode("utf-8").decode("latin-1")
    result = relay.normalise(
        {
            "encoding": "bytes",
            "output": raw,
            "data": raw,
            "stderr": [raw],
            "returns": [raw],
            "lines": [raw],
            "errors": [{"id": 1, "text": raw}],
            "error": {"kind": "runtime", "message": raw, "traceback": raw},
        }
    )
    assert result["output"] == result["data"] == text
    assert result["stderr"] == result["returns"] == result["lines"] == [text]
    assert result["errors"] == [{"id": 1, "text": text}]
    assert result["error"] == {"kind": "runtime", "message": text, "traceback": text}


@pytest.mark.parametrize("encoding", [None, "utf8"])
def test_normalise_leaves_non_bytes_text_untouched(encoding):
    result = {
        "output": "\x81é",
        "data": "\x95",
        "stderr": ["\x81"],
        "returns": ["é"],
        "lines": ["\x95"],
        "errors": [{"text": "\x81"}],
        "error": {"message": "\x95", "traceback": "é"},
    }
    if encoding is not None:
        result["encoding"] = encoding
    original = copy.deepcopy(result)
    assert relay.normalise(result) == original


@pytest.mark.parametrize(
    ("value", "expected"),
    [("é—✓".encode().decode("latin-1"), "é—✓"), ("\x80\x81\x95\x9f", "--|-"), (None, None), (42, 42)],
)
def test_fix_bytes(value, expected):
    assert relay.fix_bytes(value) == expected


def test_normalise_translates_terminal_glyphs():
    glyphs = "\x81\x95\x9f"
    result = relay.normalise(
        {
            "encoding": "bytes",
            "output": glyphs,
            "stderr": [glyphs],
            "lines": [glyphs],
            "errors": [{"text": glyphs}],
            "error": {"message": glyphs, "traceback": glyphs},
        }
    )
    assert result["output"] == "-|-"
    assert result["stderr"] == result["lines"] == ["-|-"]
    assert result["errors"] == [{"text": "-|-"}]
    assert result["error"] == {"message": "-|-", "traceback": "-|-"}


@pytest.mark.xfail(strict=True, reason="#1: byte-encoded file data must not undergo terminal glyph translation")
def test_normalise_preserves_raw_file_bytes():
    blob = bytes([0x00, 0x81, 0x95, 0x9F, 0xA0, 0xFF]).decode("latin-1")
    result = relay.normalise({"ok": True, "encoding": "bytes", "data": blob})
    assert result["data"] == blob


def test_bmp_only_handles_nested_values():
    value = {"text": "é—✓\uffff", "nested": ["😀", {"value": "a𐀀b"}, 42, None, True]}
    assert relay.bmp_only(value) == {
        "text": "é—✓\uffff",
        "nested": ["?", {"value": "a?b"}, 42, None, True],
    }
    assert value["nested"][0] == "😀"


def test_err():
    assert relay.err("runtime", "boom") == {"ok": False, "error": {"kind": "runtime", "message": "boom"}}
