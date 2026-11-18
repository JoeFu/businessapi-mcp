"""
`businessapi-mcp` command line.

    businessapi-mcp                      run the MCP server on stdio (what MCP clients launch)
    businessapi-mcp run --transport http --port 8000
    businessapi-mcp setup                store your API key (prompted, hidden) and verify it against the API
    businessapi-mcp status               show where the key comes from and which environment is active
    businessapi-mcp doctor [--lodge]     sandbox self-test through the real MCP tools (test keys only)
    businessapi-mcp install <client>     register the server with claude-code | claude-desktop | cursor | vscode | mcp-json
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__
from . import config
from .config import SETUP_HINT, key_environment, load_settings, write_key

PUBLISHED_COMMAND = {"command": "uvx", "args": ["businessapi-mcp"]}
GIT_COMMAND = {"command": "uvx", "args": ["--from", "git+https://github.com/JoeFu/businessapi-mcp", "businessapi-mcp"]}


def _dev_command(repo: Path) -> dict[str, object]:
    return {"command": "uv", "args": ["run", "--directory", str(repo), "businessapi-mcp"]}


# ---------------------------------------------------------------------------------------------
# run / status
# ---------------------------------------------------------------------------------------------

def cmd_run(args: argparse.Namespace) -> None:
    from .server import mcp  # imported late: loading the server builds 40+ tools

    if args.transport == "http":
        mcp.run(transport="http", host=args.host, port=args.port)
    else:
        mcp.run()


def cmd_status(args: argparse.Namespace) -> None:
    print(load_settings().describe())


# ---------------------------------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------------------------------

def _prompt_key() -> str:
    import getpass

    print("Paste your Business API key (input is hidden). Sandbox keys start with bapi_sk_test_ and are free:")
    print("  https://businessapi.com.au/user/api-keys")
    return getpass.getpass("Key: ")


def _verify_key(key: str, env: str) -> bool:
    import httpx2 as httpx

    base = "https://businessapi.com.au/api/v2" if env == "live" else "https://businessapi.com.au/api/test/v2"
    try:
        r = httpx.get(f"{base}/account-status", headers={"Authorization": f"Bearer {key}", "Accept": "application/json"}, timeout=20)
    except Exception as exc:  # network down, proxy, ...
        print(f"Could not reach the API to verify the key ({exc}). The key was saved anyway.")
        return False
    if r.status_code == 200:
        data = r.json()
        print(f"Key verified against the {env} environment (account: {data.get('accountName') or data.get('companyName') or 'ok'}).")
        return True
    if r.status_code == 401:
        print("The API rejected this key (401). Check it in the dashboard and run `businessapi-mcp setup` again.")
        return False
    print(f"Unexpected answer while verifying ({r.status_code}): {r.text[:200]}")
    return False


def cmd_setup(args: argparse.Namespace) -> None:
    key = (args.key or _prompt_key()).strip()
    env = key_environment(key)
    if env is None:
        sys.exit("That does not look like a Business API key (expected bapi_sk_test_... or bapi_sk_live_...). Nothing saved.")
    target = Path.cwd() / ".env" if args.local else config.user_env_file()
    path = write_key(key, env_file=target)
    print(f"Saved to {path}  ->  {env} environment (inferred from the key prefix).")
    if env == "live":
        print("Live key: lodgements stay blocked until the server runs with BAPI_ALLOW_LIVE=true. Keep it off until you need it.")
    if not args.no_verify:
        _verify_key(key, env)
    print("Next: `businessapi-mcp install claude-code` (or claude-desktop / cursor), then `businessapi-mcp doctor`.")


# ---------------------------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------------------------

def cmd_doctor(args: argparse.Namespace) -> None:
    from .smoke import run

    sys.exit(run(lodge=args.lodge, monitoring=not args.no_monitoring))


# ---------------------------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------------------------

def _claude_desktop_config() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "Claude" / "claude_desktop_config.json"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def _merge_json(path: Path, section: str, name: str, entry: dict[str, object], dry_run: bool) -> None:
    data: dict[str, object] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            sys.exit(f"{path} is not valid JSON - fix it by hand first.")
    servers = data.setdefault(section, {})
    assert isinstance(servers, dict)
    servers[name] = entry
    text = json.dumps(data, indent=2) + "\n"
    if dry_run:
        print(f"Would write {path}:\n{text}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_name(path.name + ".bak")
        shutil.copy2(path, backup)
        print(f"Backed up the previous file to {backup}")
    path.write_text(text, encoding="utf-8")
    print(f"Wrote {path}")


def cmd_install(args: argparse.Namespace) -> None:
    entry = _dev_command(Path(args.dev).resolve()) if args.dev else dict(GIT_COMMAND if args.git else PUBLISHED_COMMAND)
    name = args.name
    if args.client == "claude-code":
        cmd = ["claude", "mcp", "add", "--scope", args.scope, name, "--", str(entry["command"]), *map(str, entry["args"])]  # type: ignore[arg-type]
        if args.dry_run or not shutil.which("claude"):
            print(("Claude Code CLI not found on PATH. " if not args.dry_run else "") + "Run:\n  " + " ".join(cmd))
        else:
            subprocess.run(cmd, check=False)
            print("Registered. Open a new Claude Code session and type /mcp to confirm the 'bapi' server is connected.")
    elif args.client == "claude-desktop":
        _merge_json(_claude_desktop_config(), "mcpServers", name, entry, args.dry_run)
        print("Restart Claude Desktop to load the server.")
    elif args.client == "cursor":
        _merge_json(Path.home() / ".cursor" / "mcp.json", "mcpServers", name, entry, args.dry_run)
        print("Cursor picks the server up from Settings -> MCP (reload the window if needed).")
    elif args.client == "vscode":
        print("Add this to .vscode/mcp.json in your workspace (or your user-level mcp.json):")
        print(json.dumps({"servers": {name: {"type": "stdio", **entry}}}, indent=2))
    else:  # mcp-json
        print(json.dumps({"mcpServers": {name: entry}}, indent=2))
    if not args.dev and not shutil.which("uvx"):
        print("Note: `uvx` is not on PATH. Install uv (https://docs.astral.sh/uv/) so the client can start the server.")
    if not load_settings().has_key:
        print("No API key configured yet. " + SETUP_HINT)


# ---------------------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="businessapi-mcp", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"businessapi-mcp {__version__}")
    sub = p.add_subparsers(dest="cmd")

    run = sub.add_parser("run", help="run the MCP server (default when no subcommand is given)")
    run.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    run.add_argument("--host", default="127.0.0.1")
    run.add_argument("--port", type=int, default=8000)

    sub.add_parser("status", help="show the active key source, environment and options")

    setup = sub.add_parser("setup", help="store your API key (prompted, hidden) and verify it")
    setup.add_argument("--key", help="pass the key instead of being prompted (prefer the prompt: it keeps the key out of shell history)")
    setup.add_argument("--local", action="store_true", help="write ./.env in the current directory instead of the user config file")
    setup.add_argument("--no-verify", action="store_true", help="do not call the API to verify the key")

    doctor = sub.add_parser("doctor", help="sandbox self-test through the real MCP tools (refuses live keys)")
    doctor.add_argument("--lodge", action="store_true", help="also lodge the example payloads in the sandbox and poll them")
    doctor.add_argument("--no-monitoring", action="store_true", help="skip entity-monitoring steps")

    install = sub.add_parser("install", help="register the server with an MCP client")
    install.add_argument("client", choices=["claude-code", "claude-desktop", "cursor", "vscode", "mcp-json"])
    install.add_argument("--name", default="bapi", help="server name inside the client (default: bapi)")
    install.add_argument("--scope", default="user", choices=["user", "project", "local"], help="claude-code only (default: user)")
    install.add_argument("--dev", metavar="REPO", help="use `uv run --directory REPO businessapi-mcp` (local checkout) instead of `uvx businessapi-mcp`")
    install.add_argument("--git", action="store_true", help="run straight from GitHub (uvx --from git+https://...) instead of the PyPI package")
    install.add_argument("--dry-run", action="store_true", help="print what would be written without touching any file")
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.cmd is None:
        args.transport, args.host, args.port = "stdio", "127.0.0.1", 8000
        cmd_run(args)
        return
    {"run": cmd_run, "status": cmd_status, "setup": cmd_setup, "doctor": cmd_doctor, "install": cmd_install}[args.cmd](args)
