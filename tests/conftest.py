from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_user_profile(monkeypatch, tmp_path):
    """Tests must never read or write the developer's real config files."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))


FIXTURES = Path(__file__).parent / "fixtures" / "vendor_samples.json"


@pytest.fixture(scope="session")
def samples() -> dict:
    """Request payloads taken from the vendor's documentation pages (placeholder ACNs replaced by checksum-valid ones)."""
    return json.loads(FIXTURES.read_text(encoding="utf-8"))


@pytest.fixture
def load_server(monkeypatch, tmp_path):
    """Reload businessapi_mcp.server under a controlled configuration (no user config file, no ./.env)."""
    from businessapi_mcp import config

    def _load(**env: str):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(config, "user_env_file", lambda: tmp_path / "no-user-config.env")
        for name in ("BAPI_API_KEY", "BAPI_ENV", "BAPI_ALLOW_LIVE", "BAPI_INLINE_SCHEMAS", "BAPI_TOOLSETS"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("BAPI_API_KEY", env.pop("BAPI_API_KEY", "bapi_sk_test_dummy"))
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        import businessapi_mcp.server as server

        return importlib.reload(server)

    return _load
