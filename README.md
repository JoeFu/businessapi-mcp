# businessapi-mcp

MCP server for [businessapi.com.au](https://businessapi.com.au) — the Australian ASIC / ATO / ABRS / PPSR
digital-service-provider API — so an LLM client (Claude Code, Claude Desktop, Cursor …) can look up ABNs/ACNs,
check names, lodge company / ABN / business-name registrations, ASIC agent forms, run PPSR searches and manage
entity monitoring and webhooks, with dry-run-first safety and sandbox-by-default.

Everything lives in [`bapi-mcp/`](bapi-mcp/) — start with [`bapi-mcp/README.md`](bapi-mcp/README.md) (中文).

Quick start:

```bash
set BAPI_API_KEY=bapi_sk_test_xxx          # PowerShell: $env:BAPI_API_KEY="bapi_sk_test_xxx"
uv run --with fastmcp bapi-mcp/server.py
```

Register with Claude Code:

```bash
claude mcp add bapi -e BAPI_API_KEY=bapi_sk_test_xxx -e BAPI_ENV=test -- uv run --with fastmcp <abs path>/bapi-mcp/server.py
```
