"""Shared environment configuration, read on first use.

CC_MCP_HOME holds tokens.json; CC_MCP_URL selects the relay. CC_MCP_API_TOKEN and
CC_MCP_COMPUTER_TOKEN override the API token and the shared (deprecated) computer token.
CC_MCP_PROFILE is a comma list of opt-in mod profiles (see cc_lua.PROFILES).
Token files are handled by the relay/client.
CC_MCP_FILES_ROOT is read separately per file-transfer call in bridge_client.py.
"""

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

DEFAULT_PORT = 8765


@dataclass(frozen=True)
class Config:
    home: Path
    url: str
    api_token: str | None = field(repr=False)
    computer_token: str | None = field(repr=False)
    profiles: tuple[str, ...] = ()

    @property
    def tokens_file(self) -> Path:
        return self.home / "tokens.json"


@lru_cache(maxsize=1)
def get_config() -> Config:
    """Read the environment once per process, without touching token files."""
    return Config(
        home=Path(os.environ.get("CC_MCP_HOME", Path.home() / ".computercraft-mcp")),
        url=os.environ.get("CC_MCP_URL", f"http://127.0.0.1:{DEFAULT_PORT}").rstrip("/"),
        api_token=os.environ.get("CC_MCP_API_TOKEN"),
        computer_token=os.environ.get("CC_MCP_COMPUTER_TOKEN"),
        profiles=_names(os.environ.get("CC_MCP_PROFILE", "")),
    )


def _names(value: str) -> tuple[str, ...]:
    """'Sable, foo,,sable' -> ('sable', 'foo'): lower-cased, blanks and repeats dropped, order kept."""
    return tuple(dict.fromkeys(name.strip().lower() for name in value.split(",") if name.strip()))


def reset_config() -> None:
    """Clear the cached environment configuration (for tests or embedded use)."""
    get_config.cache_clear()
