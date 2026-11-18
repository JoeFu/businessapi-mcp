# businessapi-mcp

MCP server for [businessapi.com.au](https://businessapi.com.au) — the Australian ASIC / ATO / ABRS / PPSR
digital-service-provider API — so an LLM client (Claude Code, Claude Desktop, Cursor …) can look up ABNs / ACNs, check
names, lodge company / ABN / business-name registrations and ASIC agent forms, run PPSR searches and manage entity
monitoring and webhooks, with dry-run-first safety and the sandbox as the default environment.

Everything lives in [`bapi-mcp/`](bapi-mcp/). Start with [`bapi-mcp/README.md`](bapi-mcp/README.md).

## Quick start

```bash
cp bapi-mcp/.env.example bapi-mcp/.env     # then paste your bapi_sk_test_ key into it
```

```bash
uv run --with fastmcp bapi-mcp/sandbox_smoke_test.py
```

```bash
claude mcp add bapi -e BAPI_ENV=test -- uv run --with fastmcp <absolute path>/bapi-mcp/server.py
```

The smoke test exercises the real tools against the sandbox (free, nothing reaches ASIC / ATO production); add `--lodge`
to also submit the example company, ABN and business-name payloads and poll their status.
