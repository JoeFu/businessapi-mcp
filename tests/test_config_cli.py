"""Key discovery, environment inference and the CLI's non-network commands."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from businessapi_mcp import cli, config


def test_env_inference_and_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "user_env_file", lambda: tmp_path / "none.env")
    s = config.load_settings(environ={"BAPI_API_KEY": "bapi_sk_live_abc"}, cwd=tmp_path)
    assert (s.env, s.env_source, s.problems) == ("live", "key prefix", ())
    s = config.load_settings(environ={"BAPI_API_KEY": "bapi_sk_live_abc", "BAPI_ENV": "test"}, cwd=tmp_path)
    assert s.env == "test" and "live key" in s.problems[0]
    s = config.load_settings(environ={}, cwd=tmp_path)
    assert not s.has_key and s.env == "test" and s.env_source == "default"


def test_lookup_order_env_over_local_over_user(tmp_path, monkeypatch):
    user = tmp_path / "user" / ".env"
    monkeypatch.setattr(config, "user_env_file", lambda: user)
    config.write_key("bapi_sk_test_user", env_file=user)
    assert config.load_settings(environ={}, cwd=tmp_path).key_source == str(user)
    (tmp_path / ".env").write_text('export BAPI_API_KEY="bapi_sk_test_local"  # comment\nBAPI_ALLOW_LIVE=true\n', encoding="utf-8")
    s = config.load_settings(environ={}, cwd=tmp_path)
    assert s.api_key == "bapi_sk_test_local" and s.allow_live is True
    s = config.load_settings(environ={"BAPI_API_KEY": "bapi_sk_test_env"}, cwd=tmp_path)
    assert s.api_key == "bapi_sk_test_env" and s.key_source == "environment variable BAPI_API_KEY"


def test_write_key_drops_env_and_keeps_other_values(tmp_path):
    path = tmp_path / ".env"
    path.write_text("BAPI_ENV=live\nBAPI_TOOLSETS=lookup\n", encoding="utf-8")
    config.write_key("bapi_sk_test_new", env_file=path)
    values = config.parse_env_file(path)
    assert values == {"BAPI_API_KEY": "bapi_sk_test_new", "BAPI_TOOLSETS": "lookup"}


def test_cli_install_mcp_json_and_dry_runs(capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "user_env_file", lambda: tmp_path / "none.env")
    monkeypatch.setenv("BAPI_API_KEY", "bapi_sk_test_dummy")
    cli.main(["install", "mcp-json", "--name", "demo"])
    out = capsys.readouterr().out
    snippet = json.loads(out[out.index("{"): out.rindex("}") + 1])
    assert snippet["mcpServers"]["demo"] == {"command": "uvx", "args": ["businessapi-mcp"]}

    monkeypatch.setattr(cli, "_claude_desktop_config", lambda: tmp_path / "Claude" / "claude_desktop_config.json")
    cli.main(["install", "claude-desktop", "--dry-run"])
    assert "Would write" in capsys.readouterr().out and not (tmp_path / "Claude").exists()

    cli.main(["install", "claude-desktop", "--dev", str(tmp_path)])
    data = json.loads((tmp_path / "Claude" / "claude_desktop_config.json").read_text(encoding="utf-8"))
    assert data["mcpServers"]["bapi"]["command"] == "uv" and "--directory" in data["mcpServers"]["bapi"]["args"]

    cli.main(["install", "claude-code", "--dry-run"])
    assert "claude mcp add --scope user bapi -- uvx businessapi-mcp" in capsys.readouterr().out


def test_cli_setup_writes_key_without_verifying(tmp_path, monkeypatch, capsys):
    target = tmp_path / "cfg" / ".env"
    monkeypatch.setattr(config, "user_env_file", lambda: target)
    cli.main(["setup", "--key", "bapi_sk_test_abc123", "--no-verify"])
    assert config.parse_env_file(target)["BAPI_API_KEY"] == "bapi_sk_test_abc123"
    assert "test environment" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["setup", "--key", "not-a-key", "--no-verify"])


def test_cli_status(capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "user_env_file", lambda: tmp_path / "none.env")
    monkeypatch.delenv("BAPI_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    cli.main(["status"])
    out = capsys.readouterr().out
    assert "NOT configured" in out and "businessapi-mcp setup" in out
