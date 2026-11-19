"""Keep the documented tool inventory aligned with the registered tools."""

from pathlib import Path

import pytest
from doc_tools import readme_tool_names

from computercraft_live_mcp import mcp_server

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_readme_tools_match_server():
    documented = readme_tool_names(ROOT / "README.md")
    registered = {tool.name for tool in await mcp_server.server.list_tools()}
    assert documented == registered, (
        f"missing from README: {sorted(registered - documented)}; extra in README: {sorted(documented - registered)}"
    )


@pytest.mark.parametrize("name", ["QUICKSTART.md", "REFERENCE.md"])
def test_docs_link_to_readme_tools(name):
    assert "(../README.md#tools)" in (ROOT / "docs" / name).read_text(encoding="utf-8")


def test_docs_have_no_stale_guidance():
    paths = sorted(ROOT.glob("*.md")) + sorted((ROOT / "docs").rglob("*.md"))
    for path in paths:
        text = path.read_text(encoding="utf-8").lower()
        for stale in ("site-packages", "json-ish"):
            assert stale not in text, f"{path.relative_to(ROOT)} still contains {stale!r}"


def test_readme_parser_limits_names_to_tool_table(tmp_path):
    readme = tmp_path / "README.md"
    readme.write_text(
        "## Tools\n\n| tool | what |\n|---|---|\n| `cc_read` `cc_write` | files |\n"
        "\nMention `cc_other` outside the table.\n\n## Other\n| `cc_extra` | other |\n",
        encoding="utf-8",
    )
    assert readme_tool_names(readme) == {"cc_read", "cc_write"}


@pytest.mark.parametrize(
    "text",
    ["# No tools\n", "## Tools\nNo table.\n", "## Tools\n| tool | what |\n|---|---|\n| none | none |\n"],
)
def test_readme_parser_rejects_missing_inventory(tmp_path, text):
    readme = tmp_path / "README.md"
    readme.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="missing|no backticked"):
        readme_tool_names(readme)
