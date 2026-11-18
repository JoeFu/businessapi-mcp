"""
Route B: generate the MCP server from openapi.yaml instead of hand-writing tools.

    uv run --group dev scripts/server_from_openapi.py

Every `operationId` in openapi.yaml becomes an MCP tool; its summary/description becomes the tool
description and its parameters/requestBody become the input schema. Add an endpoint to the YAML and
the model can use it — no Python changes.

Same env vars as server.py: BAPI_API_KEY, BAPI_ENV (test|live), BAPI_ALLOW_LIVE.
"""
from __future__ import annotations

import os
from pathlib import Path

import httpx2 as httpx  # fastmcp >= 4 ships the httpx2 fork; the API is the same as httpx
import yaml
from fastmcp import FastMCP

from businessapi_mcp.config import SETUP_HINT, load_settings

SETTINGS = load_settings()
if not SETTINGS.has_key:
    raise SystemExit("No Business API key configured. " + SETUP_HINT)
if SETTINGS.problems:
    raise SystemExit("; ".join(SETTINGS.problems))
API_KEY, ENV, ALLOW_LIVE = SETTINGS.api_key, SETTINGS.env, SETTINGS.allow_live

BASE_URL = "https://businessapi.com.au/api/v2" if ENV == "live" else "https://businessapi.com.au/api/test/v2"


async def _block_live_writes(request: httpx.Request) -> None:
    if ENV == "live" and request.method in ("POST", "PUT", "PATCH", "DELETE") and not ALLOW_LIVE:
        raise httpx.RequestError(
            "Refused: LIVE write would lodge a real form and be billed. Set BAPI_ALLOW_LIVE=true or use BAPI_ENV=test.",
            request=request,
        )


client = httpx.AsyncClient(
    base_url=BASE_URL,
    headers={"Authorization": f"Bearer {API_KEY}", "Accept": "application/json"},
    timeout=httpx.Timeout(30.0),
    event_hooks={"request": [_block_live_writes]},
)

spec = yaml.safe_load((Path(__file__).resolve().parent.parent / "openapi.yaml").read_text(encoding="utf-8"))

mcp = FastMCP.from_openapi(
    openapi_spec=spec,
    client=client,
    name="Business API (AU) — from OpenAPI",
)

if __name__ == "__main__":
    mcp.run()
