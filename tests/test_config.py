import io
import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from computercraft_live_mcp import bridge_client, relay
from computercraft_live_mcp.config import DEFAULT_PORT, get_config, reset_config
from computercraft_live_mcp.limits import CLIENT_TIMEOUT_MARGIN, DEFAULT_TIMEOUT


@pytest.fixture(autouse=True)
def clean_config(monkeypatch):
    for name in ("CC_MCP_HOME", "CC_MCP_URL", "CC_MCP_API_TOKEN", "CC_MCP_COMPUTER_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    reset_config()
    yield
    reset_config()


def test_defaults(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    config = get_config()
    assert config.home == tmp_path / ".computercraft-live-mcp"
    assert config.url == f"http://127.0.0.1:{DEFAULT_PORT}"
    assert config.api_token is None
    assert config.computer_token is None
    assert not config.home.exists()


def test_legacy_home_is_kept_until_the_new_one_exists(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ".computercraft-mcp").mkdir()
    assert get_config().home == tmp_path / ".computercraft-mcp"
    reset_config()
    (tmp_path / ".computercraft-live-mcp").mkdir()
    assert get_config().home == tmp_path / ".computercraft-live-mcp"


def test_environment_overrides_are_lazy_and_frozen(monkeypatch, tmp_path):
    # The package is already imported; configuration is read only now.
    home = tmp_path / "configured"
    monkeypatch.setenv("CC_MCP_HOME", str(home))
    monkeypatch.setenv("CC_MCP_URL", "https://relay.example/base///")
    monkeypatch.setenv("CC_MCP_API_TOKEN", "secret-api-value")
    monkeypatch.setenv("CC_MCP_COMPUTER_TOKEN", "secret-computer-value")
    config = get_config()
    assert config.home == home
    assert config.url == "https://relay.example/base"
    assert config.api_token == "secret-api-value"
    assert config.computer_token == "secret-computer-value"
    assert "secret-api-value" not in repr(config)
    assert "secret-computer-value" not in repr(config)
    assert not home.exists()
    with pytest.raises(FrozenInstanceError):
        config.url = "http://changed.example"


def test_cache_and_reset(monkeypatch, tmp_path):
    first = get_config()
    monkeypatch.setenv("CC_MCP_HOME", str(tmp_path))
    monkeypatch.setenv("CC_MCP_URL", "http://changed.example/")
    monkeypatch.setenv("CC_MCP_API_TOKEN", "changed-api")
    monkeypatch.setenv("CC_MCP_COMPUTER_TOKEN", "changed-computer")
    assert get_config() is first
    reset_config()
    second = get_config()
    assert second is not first
    assert second.home == tmp_path
    assert second.url == "http://changed.example"
    assert second.api_token == "changed-api"
    assert second.computer_token == "changed-computer"


def test_token_files_stay_with_relay_and_client(monkeypatch, tmp_path):
    home = tmp_path / "tokens"
    monkeypatch.setenv("CC_MCP_HOME", str(home))
    config = get_config()
    assert not home.exists()
    tokens = relay.load_tokens()
    assert tokens["api"] and tokens["computer"]
    assert tokens["api"] != tokens["computer"]
    assert json.loads((home / "tokens.json").read_text()) == tokens
    assert relay.load_tokens() == tokens
    assert bridge_client.api_token() == tokens["api"]
    tokens["api"] = "updated-on-disk"
    (home / "tokens.json").write_text(json.dumps(tokens))
    assert bridge_client.api_token() == "updated-on-disk"
    assert get_config() is config
    assert config.api_token is None


def test_token_overrides_and_empty_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("CC_MCP_HOME", str(tmp_path))
    stored = {"api": "disk-api", "computer": "disk-computer"}
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps(stored))
    monkeypatch.setenv("CC_MCP_API_TOKEN", "env-api")
    monkeypatch.setenv("CC_MCP_COMPUTER_TOKEN", "env-computer")
    assert relay.load_tokens() == {"api": "env-api", "computer": "env-computer"}
    assert bridge_client.api_token() == "env-api"
    assert json.loads(path.read_text()) == stored
    monkeypatch.setenv("CC_MCP_API_TOKEN", "")
    monkeypatch.setenv("CC_MCP_COMPUTER_TOKEN", "")
    reset_config()
    assert relay.load_tokens() == stored
    assert bridge_client.api_token() == "disk-api"


@pytest.mark.parametrize("body", [None, {"op": "ping", "timeout": 2.5}])
def test_request_uses_shared_config_and_timeout(monkeypatch, body):
    monkeypatch.setenv("CC_MCP_URL", "http://relay.example///")
    monkeypatch.setenv("CC_MCP_API_TOKEN", "api-override")
    seen = []

    def urlopen(req, timeout):
        seen.append((req, timeout))
        return io.BytesIO(b'{"ok": true}')

    monkeypatch.setattr(bridge_client.urllib.request, "urlopen", urlopen)
    assert bridge_client.request("/api/call", body) == {"ok": True}
    req, timeout = seen[0]
    assert req.full_url == "http://relay.example/api/call"
    assert req.get_header("Authorization") == "Bearer api-override"
    assert timeout == (DEFAULT_TIMEOUT if body is None else body["timeout"]) + CLIENT_TIMEOUT_MARGIN


def test_files_root_is_read_per_call(monkeypatch, tmp_path):
    get_config()
    monkeypatch.delenv("CC_MCP_FILES_ROOT", raising=False)
    assert bridge_client.files_roots() == []
    monkeypatch.setenv("CC_MCP_FILES_ROOT", str(tmp_path))
    assert bridge_client.files_roots() == [tmp_path.resolve()]
