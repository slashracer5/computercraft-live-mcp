"""Keep the protocol's error catalogue (docs/PROTOCOL.md, formatting.ERROR_KINDS) tied to the actual error producers."""

import re
from pathlib import Path

from computercraft_mcp import relay
from computercraft_mcp.formatting import ERROR_KINDS, HINTS, KNOWN_KINDS

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "computercraft_mcp"


def documented_kinds() -> dict[str, tuple[str, str]]:
    """PROTOCOL.md's error-kind table: kind -> (origin, HTTP status cell)."""
    doc = (ROOT / "docs" / "PROTOCOL.md").read_text(encoding="utf-8")
    table = doc.split("## Error kinds\n", 1)[1]
    rows = re.findall(r"^\| `([a-z_]+)` \| (bridge|relay|client) \| ([^|]*) \|", table, re.MULTILINE)
    assert len(rows) == len({kind for kind, _, _ in rows}), "duplicate error-kind rows"
    return {kind: (origin, status) for kind, origin, status in rows}


def test_documented_error_kinds_and_origins():
    assert {kind: origin for kind, (origin, _) in documented_kinds().items()} == ERROR_KINDS


def test_reference_summary_lists_every_kind():
    reference = (ROOT / "docs" / "REFERENCE.md").read_text(encoding="utf-8")
    row = re.search(r"^\| `error\.kind` \|([^\n]*)$", reference, re.MULTILINE)
    assert row is not None
    named = set(re.findall(r"`([a-z_]+)`", row.group(1))) - {"cc_push"}  # "for `cc_push`: no_space, ..."
    assert named == set(ERROR_KINDS)


def test_error_registry_and_hints():
    assert set(HINTS) <= set(ERROR_KINDS)
    assert KNOWN_KINDS == frozenset(ERROR_KINDS)
    assert set(ERROR_KINDS.values()) == {"bridge", "relay", "client"}


def test_documented_http_statuses():
    statuses = {kind: status for kind, (_, status) in documented_kinds().items()}
    source = (SOURCE / "relay.py").read_text(encoding="utf-8")
    fixed = re.findall(r'err\(\s*"([a-z_]+)",[^\n]*?\),\s*status=(\d{3})', source)
    assert {kind for kind, _ in fixed} >= {"unauthorized", "bad_request"}

    class Conn:  # what resolve_target reads of relay.Conn
        def __init__(self, cid: int, label: str | None):
            self.id, self.label = cid, label

    computers = {3: Conn(3, "Reactor"), 5: Conn(5, "reactor")}
    for target in (None, "REACTOR", "nobody", 7, True):
        kind, _, status = relay.resolve_target(computers, target)
        fixed.append((kind, str(status)))
    assert {kind for kind, _ in fixed} >= {"no_target", "ambiguous", "not_connected", "bad_request"}
    for kind, status in fixed:
        assert status in statuses[kind], (kind, status, statuses[kind])


def produced_kinds():
    # Each pattern has a distinct-kind floor: renamed helpers must not silently disable the scan.
    patterns = [
        ("bridge.lua", r'\bfail\(\s*"([a-z_]+)"', 8),
        ("bridge.lua", r'\bkind\s*=\s*"([a-z_]+)"', 2),
        ("relay.py", r'\berr\(\s*"([a-z_]+)"', 5),
        ("bridge_client.py", r'"kind"\s*:\s*"([a-z_]+)"', 4),
        ("ccctl.py", r'"kind"\s*:\s*"([a-z_]+)"', 2),
        ("bridge_client.py", r'\b_push_error\(\s*"([a-z_]+)"', 5),
        ("bridge_client.py", r'\breturn\s+"([a-z_]+)",\s*f"', 7),
    ]
    found = set()
    for filename, pattern, minimum in patterns:
        source = (SOURCE / filename).read_text(encoding="utf-8")
        matches = set(re.findall(pattern, source))
        assert len(matches) >= minimum, (filename, pattern, matches)
        found.update(matches)

    # Kinds returned as values by a helper: scan only that helper, not unrelated returns elsewhere in the file.
    helpers = [
        ("bridge.lua", r"local function errorKind\(message\)(.*?)\nend", r'\breturn\s+"([a-z_]+)"', 2),
        ("relay.py", r"\ndef resolve_target\(.*?(?=\n\S)", r'\breturn\s*\(?\s*"([a-z_]+)",', 4),
    ]
    for filename, body, pattern, minimum in helpers:
        helper = re.search(body, (SOURCE / filename).read_text(encoding="utf-8"), re.DOTALL)
        assert helper is not None, (filename, body)
        matches = set(re.findall(pattern, helper.group(0)))
        assert len(matches) >= minimum, (filename, pattern, matches)
        found.update(matches)
    assert len(found) >= 25
    return found


def test_produced_error_kinds_are_registered():
    # Logging outcomes (relay's "aborted") and snippet reason codes are not scanned: cc_lua reasons become public
    # kinds only through bridge_client's mappings, which are.
    assert produced_kinds() <= set(ERROR_KINDS)


def test_registered_error_kinds_are_produced():
    assert set(ERROR_KINDS) <= produced_kinds()
