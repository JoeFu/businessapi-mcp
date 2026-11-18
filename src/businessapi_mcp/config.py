"""
Where the API key comes from and what it implies - the "zero configuration" layer.

Lookup order (first hit wins for each variable):
  1. environment variables            BAPI_API_KEY, BAPI_ENV, BAPI_ALLOW_LIVE, BAPI_INLINE_SCHEMAS, BAPI_TOOLSETS
  2. ./.env in the current directory  (project-local override)
  3. the user config file             written by `businessapi-mcp setup`:
       Windows  %APPDATA%\\businessapi-mcp\\.env
       macOS    ~/Library/Application Support/businessapi-mcp/.env
       Linux    $XDG_CONFIG_HOME/businessapi-mcp/.env  (default ~/.config/businessapi-mcp/.env)

The environment (sandbox vs live) is inferred from the key prefix - bapi_sk_test_... is the sandbox, bapi_sk_live_... is
live - so BAPI_ENV is optional and only acts as a cross-check. With no key at all the server still starts; every API call
then fails with a message that says how to configure one.
"""
from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "businessapi-mcp"
ENV_FILE_NAME = ".env"
SETUP_HINT = "Run `businessapi-mcp setup` to store your key (sandbox keys are free: https://businessapi.com.au/user/api-keys), or set BAPI_API_KEY."


def user_config_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / APP_NAME


def user_env_file() -> Path:
    return user_config_dir() / ENV_FILE_NAME


def parse_env_file(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE parser (comments, `export`, quotes) - no python-dotenv dependency needed."""
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if value and value[0] in "\"'":  # quoted value, possibly followed by a comment
            quote = value[0]
            end = value.find(quote, 1)
            value = value[1:end] if end > 0 else value[1:]
        else:
            value = value.split(" #", 1)[0].strip()
        if key:
            values[key] = value
    return values


def key_environment(key: str) -> str | None:
    """'test' or 'live' from the key prefix (bapi_sk_test_... / bapi_sk_live_...), else None."""
    if not key.startswith("bapi_"):
        return None
    if "_live_" in key:
        return "live"
    if "_test_" in key:
        return "test"
    return None


@dataclass(frozen=True)
class Settings:
    api_key: str
    env: str  # "test" | "live"
    allow_live: bool
    inline_schemas: bool
    toolsets_raw: str
    key_source: str  # "environment variable BAPI_API_KEY", a file path, or "none"
    env_source: str  # "BAPI_ENV", "key prefix" or "default"
    problems: tuple[str, ...]  # configuration errors worth surfacing (e.g. BAPI_ENV contradicts the key)

    @property
    def has_key(self) -> bool:
        return bool(self.api_key)

    @property
    def base_url(self) -> str:
        return "https://businessapi.com.au/api/v2" if self.env == "live" else "https://businessapi.com.au/api/test/v2"

    def describe(self) -> str:
        lines = [
            f"API key: {'configured (' + self.api_key[:13] + '...)' if self.has_key else 'NOT configured'}  [source: {self.key_source}]",
            f"Environment: {self.env}  [from {self.env_source}]  ->  {self.base_url}",
            f"Live writes allowed: {self.allow_live}",
            f"Inline schemas: {self.inline_schemas}   Toolsets: {self.toolsets_raw or 'all'}",
            f"User config file: {user_env_file()}",
        ]
        lines.extend(f"PROBLEM: {p}" for p in self.problems)
        if not self.has_key:
            lines.append(SETUP_HINT)
        return "\n".join(lines)


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def load_settings(environ: Mapping[str, str] | None = None, cwd: Path | None = None) -> Settings:
    environ = dict(os.environ if environ is None else environ)
    cwd = cwd or Path.cwd()
    merged: dict[str, str] = {}
    key_source = "none"
    for path in (user_env_file(), cwd / ENV_FILE_NAME):  # lowest precedence first
        values = parse_env_file(path)
        for name, value in values.items():
            if name.startswith("BAPI_"):
                merged[name] = value
        if values.get("BAPI_API_KEY", "").strip():
            key_source = str(path)
    for name, value in environ.items():
        if name.startswith("BAPI_"):
            merged[name] = value
    if environ.get("BAPI_API_KEY", "").strip():
        key_source = "environment variable BAPI_API_KEY"

    api_key = merged.get("BAPI_API_KEY", "").strip()
    inferred = key_environment(api_key) if api_key else None
    problems: list[str] = []
    env_raw = merged.get("BAPI_ENV", "").strip().lower()
    if env_raw:
        env, env_source = env_raw, "BAPI_ENV"
        if env not in ("test", "live"):
            problems.append(f"BAPI_ENV must be 'test' or 'live', not '{env_raw}'")
            env = "test"
        elif inferred and inferred != env:
            problems.append(f"BAPI_ENV={env} but the API key is a {inferred} key (bapi_sk_{inferred}_...)")
    elif inferred:
        env, env_source = inferred, "key prefix"
    else:
        env, env_source = "test", "default"
    if api_key and not inferred:
        problems.append("BAPI_API_KEY does not look like a Business API key (expected bapi_sk_test_... or bapi_sk_live_...)")

    return Settings(
        api_key=api_key,
        env=env,
        allow_live=_truthy(merged.get("BAPI_ALLOW_LIVE")),
        inline_schemas=_truthy(merged.get("BAPI_INLINE_SCHEMAS")),
        toolsets_raw=merged.get("BAPI_TOOLSETS", "all").strip().lower(),
        key_source=key_source,
        env_source=env_source,
        problems=tuple(problems),
    )


def write_key(key: str, env_file: Path | None = None) -> Path:
    """Store the key in the user config file (or the given .env), owner-readable only where the OS supports it."""
    path = env_file or user_env_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = parse_env_file(path) if path.exists() else {}
    existing["BAPI_API_KEY"] = key.strip()
    existing.pop("BAPI_ENV", None)  # the environment is inferred from the key prefix
    body = "\n".join(f"{k}={v}" for k, v in existing.items())
    path.write_text(f"# Business API configuration (written by `{APP_NAME} setup`). Keep this file private.\n{body}\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:  # Windows: ACLs, not mode bits
        pass
    return path
