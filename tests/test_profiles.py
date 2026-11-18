"""Mod profiles (CC_MCP_PROFILE): without one, nothing the model sees names a specific mod."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from computercraft_live_mcp import cc_lua
from computercraft_live_mcp.config import get_config, reset_config
from computercraft_live_mcp.formatting import format_result

MOD_WORDS = re.compile(r"\bsable\b|avionics|actuator|sublevel|\bship|on_ship|ship_mass", re.IGNORECASE)
SRC = Path(cc_lua.__file__).resolve().parents[1]

# A cc_network document as the computer returns it; on_ship / ship_mass are what the sable probe adds.
DOC = {
    "devices": [
        {"name": "minecraft:chest_0", "types": ["minecraft:chest", "inventory"]},
        {"name": "top", "types": ["modem", "peripheral_hub"]},
    ],
    "modems": [{"side": "top", "wireless": False, "remote_count": 1}],
    "on_ship": True,
    "ship_mass": 1234.5,
}


def network_text(doc: dict) -> str:
    """cc_network's text for a reply carrying `doc`, without the status line."""
    text = format_result("network", {"ok": True, "returns": [json.dumps(json.dumps(doc))]})
    assert text.startswith("OK\n")
    return text[3:]


SERVER_INFO = """
import asyncio, json
from computercraft_live_mcp import mcp_server
tools = asyncio.run(mcp_server.server.list_tools())
print(json.dumps({"instructions": mcp_server.server.instructions, "tools": {t.name: t.description for t in tools}}))
"""


@pytest.fixture(autouse=True)
def no_profile(monkeypatch):
    monkeypatch.delenv("CC_MCP_PROFILE", raising=False)
    reset_config()
    yield
    reset_config()


def use_profile(monkeypatch, value: str) -> None:
    monkeypatch.setenv("CC_MCP_PROFILE", value)
    reset_config()


def server_info(tmp_path, profile: str | None) -> tuple[dict, str]:
    """Start the MCP server module in a fresh interpreter; return its instructions/tool descriptions and stderr."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("CC_MCP_")}
    env["CC_MCP_HOME"] = str(tmp_path)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(SRC), env.get("PYTHONPATH")) if p)
    if profile is not None:
        env["CC_MCP_PROFILE"] = profile
    r = subprocess.run([sys.executable, "-c", SERVER_INFO], env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout), r.stderr


def test_profile_names_from_config(monkeypatch):
    assert get_config().profiles == ()
    assert cc_lua.active_profiles() == ()
    use_profile(monkeypatch, " Sable, ,other,sable ")
    assert get_config().profiles == ("sable", "other")


@pytest.mark.parametrize("methods", [False, True])
def test_network_without_profile_is_mod_independent(methods):
    code = cc_lua.network_code(methods)
    assert not MOD_WORDS.search(code), code
    assert code.rstrip().endswith("return textutils.serialiseJSON(out)")
    text = network_text(DOC)
    assert not MOD_WORDS.search(text), text
    assert text.startswith("2 peripherals\n")
    assert "find peripherals by name prefix at run time" in text


def test_instructions_without_profile_are_mod_independent():
    from computercraft_live_mcp import mcp_server

    assert cc_lua.profile_instructions() == ""
    assert not MOD_WORDS.search(mcp_server.INSTRUCTIONS)
    assert "verify with a one-variable" in mcp_server.INSTRUCTIONS


def test_sable_profile_adds_probe_fields_notes_and_instructions(monkeypatch):
    use_profile(monkeypatch, "sable")
    assert cc_lua.active_profiles() == (cc_lua.SABLE,)
    code = cc_lua.network_code(False)
    assert "sublevel.isInPlotGrid()" in code and "sublevel.getMass()" in code
    assert "out.on_ship = on_ship" in code and "out.ship_mass = mass" in code
    assert code.index("sublevel.") > code.index("for _, n in ipairs(names)")  # after the inventory, before return
    assert code.rstrip().endswith("return textutils.serialiseJSON(out)")
    text = network_text(DOC)
    assert text.startswith("2 peripherals (on_ship=true, ship_mass=1234.5)\n")
    assert text.endswith("\nship_* zone names derive from the bow, so rebuilding the ship can rename them.")
    # cc_peripherals shares the device listing but not the profile's notes
    listing = format_result("peripherals", {"ok": True, "peripherals": DOC["devices"]})
    assert not MOD_WORDS.search(listing), listing
    extra = cc_lua.profile_instructions()
    assert extra.startswith('\nProfile "sable"')
    for word in ("on_ship", "ship_mass", "Create Avionics", "actuator signs or frames"):
        assert word in extra


def test_sable_fields_are_optional(monkeypatch):
    # A computer without the `sublevel` API: the probe sets nothing, the header stays plain.
    use_profile(monkeypatch, "sable")
    text = network_text({"devices": [], "modems": []})
    assert text.startswith("0 peripherals\n")
    assert "on_ship" not in text
    assert "derive from the bow" in text


def test_explicit_profiles_override_the_environment(monkeypatch):
    assert "sublevel" in cc_lua.network_code(True, profiles=[cc_lua.SABLE])
    assert cc_lua.network_extras(DOC, profiles=[cc_lua.SABLE])[0] == [("on_ship", True), ("ship_mass", 1234.5)]
    assert cc_lua.profile_instructions([cc_lua.SABLE]) != ""
    use_profile(monkeypatch, "sable")
    assert not MOD_WORDS.search(cc_lua.network_code(True, profiles=()))
    assert cc_lua.network_extras(DOC, profiles=()) == ([], [])


def test_unknown_profile_is_ignored_with_one_warning(monkeypatch, capsys):
    use_profile(monkeypatch, "no-such-mod-23,sable")
    assert cc_lua.active_profiles() == (cc_lua.SABLE,)
    assert "sublevel" in cc_lua.network_code(False)
    err = capsys.readouterr().err
    assert err.count("'no-such-mod-23'") == 1
    assert "known: sable" in err


def test_server_without_profile_is_mod_independent(tmp_path):
    info, _ = server_info(tmp_path, None)
    assert "cc_network" in info["tools"] and "cc_batch" in info["tools"]
    assert not MOD_WORDS.search(info["instructions"]), info["instructions"]
    for name, description in info["tools"].items():
        assert not MOD_WORDS.search(description or ""), (name, description)


def test_server_with_sable_profile(tmp_path):
    info, _ = server_info(tmp_path, "sable")
    assert 'Profile "sable"' in info["instructions"]
    assert info["instructions"].startswith("Tools for a real ComputerCraft computer")


def test_server_ignores_unknown_profile(tmp_path):
    plain, _ = server_info(tmp_path, None)
    info, err = server_info(tmp_path, "nonsense")
    assert info == plain
    assert "ignoring unknown CC_MCP_PROFILE entry 'nonsense'" in err
