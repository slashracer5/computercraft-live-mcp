"""Read the canonical tool inventory without importing pytest."""

import re
from pathlib import Path


def readme_tool_names(readme_path: Path) -> set[str]:
    text = readme_path.read_text(encoding="utf-8")
    section = re.search(r"^## Tools\s*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    if section is None:
        raise ValueError(f"{readme_path}: missing ## Tools section")
    table = re.search(
        r"^\|[^\n]+\|\s*\n\|[ :|\-]+\|\s*\n((?:\|[^\n]+\|[^\S\n]*\n?)+)",
        section.group(1),
        re.M,
    )
    if table is None:
        raise ValueError(f"{readme_path}: missing tool table under ## Tools")
    names = set(re.findall(r"`(cc_[a-z_]+)`", table.group(1)))
    if not names:
        raise ValueError(f"{readme_path}: tool table contains no backticked cc_* names")
    return names
