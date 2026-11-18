"""
Route B: generate the MCP server from openapi.yaml instead of hand-writing tools.

    uv run --with fastmcp --with pyyaml server_from_openapi.py

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

API_KEY = os.environ.get("BAPI_API_KEY", "").strip()
ENV = os.environ.get("BAPI_ENV", "test").strip().lower()
ALLOW_LIVE = os.environ.get("BAPI_ALLOW_LIVE", "").strip().lower() in ("1", "true", "yes")

if not API_KEY:
    raise SystemExit("BAPI_API_KEY is not set (https://businessapi.com.au/user/api-keys)")
if ENV not in ("test", "live") or f"_{ENV}_" not in API_KEY:
    raise SystemExit(f"BAPI_ENV={ENV} does not match the key prefix (bapi_sk_{ENV}_...)")

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

spec = yaml.safe_load((Path(__file__).with_name("openapi.yaml")).read_text(encoding="utf-8"))

mcp = FastMCP.from_openapi(
    openapi_spec=spec,
    client=client,
    name="Business API (AU) — from OpenAPI",
)

if __name__ == "__main__":
    mcp.run()
