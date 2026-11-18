# Changelog

## 0.1.0 — 2026-11-18

First public release.

- MCP server (fastmcp 4) for businessapi.com.au: 46 tools, 12 documentation resources, request-schema resources.
- Typed, locally validated lodgements: company registration (Form 201), ABN application, business-name registration
  and renewal, ASIC Forms 484 / 205A / 6010 / 361 / 362 / RA52; PPSR searches; entity monitoring; webhook management.
- Zero-configuration install: `uvx businessapi-mcp`, `businessapi-mcp setup` (key stored in the user config directory,
  environment inferred from the key prefix), `businessapi-mcp install <client>`, `businessapi-mcp doctor`.
- Safety: sandbox by default, live writes blocked unless `BAPI_ALLOW_LIVE=true`, dry-run-first lodgement tools,
  caller-supplied idempotency keys for monitoring orders.
- Context-size controls: lazy request schemas (`get_request_schema`) and `BAPI_TOOLSETS` splitting.
- Verified end-to-end against the vendor sandbox (company registration finished with a test ACN; ABN application
  reached `validation ok`; business-name registration returned an ASIC payment link).
