# businessapi-mcp

[![CI](https://github.com/JoeFu/businessapi-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/JoeFu/businessapi-mcp/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/businessapi-mcp.svg)](https://pypi.org/project/businessapi-mcp/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

MCP server for [Business API](https://businessapi.com.au) — the Australian ASIC / ATO / ABRS / PPSR
digital-service-provider API. Give Claude Code, Claude Desktop, Cursor or any MCP client the ability to look up
ABNs and ACNs, check names, register companies, ABNs and business names, lodge ASIC agent forms, run PPSR searches,
and monitor entities — with the sandbox as the default environment and every lodgement dry-run first.

```text
You:    Register a company called ACME TRADING SANDBOX TEST PTY LTD in the sandbox
Claude: check_company_name_availability → Available
        get_request_schema("company_registration") → asks you for officeholders, addresses, shares
        lodge_company_registration(dry_run=true) → shows you the exact payload and fees
        lodge_company_registration(dry_run=false) → requestId 37363
        wait_for_request → finished · ACN 601510089 · document 0EACL6550
```

## Install in three steps

Requires [uv](https://docs.astral.sh/uv/) (it fetches and runs the package; nothing else to install).

1. **Get a key.** Register at https://businessapi.com.au/register and create a key at
   https://businessapi.com.au/user/api-keys. Sandbox keys start with `bapi_sk_test_`, are free and need no card.

2. **Store it** (prompted, hidden; saved to your user config directory, verified against the API):

   ```bash
   uvx businessapi-mcp setup
   ```

3. **Register the server** with your client:

   ```bash
   uvx businessapi-mcp install claude-code      # or: claude-desktop · cursor · vscode · mcp-json
   ```

   This is equivalent to `claude mcp add bapi -- uvx businessapi-mcp`; the key never appears in client configuration.

Then open a new session and ask: *"Which company has ABN 51 824 753 556?"* (it calls `lookup_abn`) or
*"Register a company in the sandbox"*. `uvx businessapi-mcp doctor` runs a self-test against the sandbox if anything
looks off, and `uvx businessapi-mcp status` shows which key and environment are active.

<details>
<summary>Manual client configuration</summary>

```json
{ "mcpServers": { "bapi": { "command": "uvx", "args": ["businessapi-mcp"] } } }
```

Claude Desktop: `claude_desktop_config.json`. Cursor: `~/.cursor/mcp.json`. VS Code: `.vscode/mcp.json` with
`"servers": { "bapi": { "type": "stdio", "command": "uvx", "args": ["businessapi-mcp"] } }`. The key is read from
the user config file written by `setup`, from `./.env`, or from `BAPI_API_KEY`.
</details>

## How the model knows what to do

The vendor publishes no OpenAPI specification, only HTML documentation. This project turns that documentation into
three things the model receives over MCP:

1. **Server instructions** — environment, asynchronous lodgement flow, pricing, dry-run workflow, where to start.
2. **Tools** — 46 of them, each with a description that says when to use it, what it costs and what to call next.
   Lodgement bodies are validated by Pydantic models whose field names match the vendor docs one-to-one, with the
   ASIC/ATO code tables as enums and the "Mandatory IF …" cross-field rules as validators.
3. **Resources** — 12 guides under `bapi://docs/*` (endpoint catalogue, lifecycle, per-form guides, code tables) and
   the request schema of every lodgement under `bapi://schemas/{form}`.

### Tools

| Group | Tools | Notes |
|---|---|---|
| `lookup` | `lookup_abn` `lookup_acn` `check_company_name_availability` `check_business_name_availability` `search_business_industries` | free lookups |
| `registrations` | `lodge_company_registration` `lodge_abn_registration` `lodge_business_name_registration` `lodge_business_name_renewal` + status / payment-link tools, `get_abn_entitlement_questionnaire`, `get_request_schema` | Form 201, ABNREG, BNREG, BNRENEW |
| `agent_forms` | `lodge_asic_484_form` `lodge_company_name_change` `lodge_company_deregistration` `lodge_asic_361_form` `lodge_asic_362_form` `request_asic_account_transactions` + status tools | registered-agent forms 484 / 205A / 6010 / 361 / 362 / RA52 |
| `monitoring` | `start_entity_monitoring` `renew_entity_monitoring` `list_monitored_entities` `get_monitored_entity` `stop_entity_monitoring` `resume_entity_monitoring` `get_entity_snapshot` `list_monitoring_events` `get_monitoring_event` `simulate_monitoring_event` | $18 per entity per year live |
| `webhooks` | `list_webhooks` `create_webhook` `update_webhook` `delete_webhook` `rotate_webhook_secret` `verify_webhook_signature` | signature check runs locally |
| `ppsr` | `create_ppsr_search` `get_ppsr_search` | $6 per search live |
| `core` | `get_request_status` `wait_for_request` `get_account_status` `call_endpoint` | polling, account, escape hatch |

Every lodgement tool defaults to `dry_run=true`: it validates locally and returns the exact JSON that would be sent.
The model has to pass `dry_run=false` to lodge — the natural "show the user the payload and fees, then confirm" step.

## Safety model

- **Sandbox by default.** The environment is inferred from the key prefix. `bapi_sk_test_…` only ever reaches
  `https://businessapi.com.au/api/test/v2`, where nothing is billed and nothing reaches production registries.
- **Live writes are blocked** unless the server runs with `BAPI_ALLOW_LIVE=true`. With a live key every
  POST / PATCH / DELETE is refused before any HTTP call. Turn it on only when you intend to lodge.
- `testTransmissionIndicator` is injected from the server environment and anything the model sends is ignored;
  `simulate_monitoring_event` refuses to run against live; monitoring orders and renewals need a caller-supplied
  idempotency key so retries never charge twice; TFNs are checksum-validated and never echoed; webhook secrets are
  returned once.
- Tools carry MCP `annotations` (`readOnlyHint`, `destructiveHint`) and a toolset tag.

See [SECURITY.md](SECURITY.md).

## Configuration

| Variable | Default | Effect |
|---|---|---|
| `BAPI_API_KEY` | — | the key; also read from `./.env` or the file written by `businessapi-mcp setup` |
| `BAPI_ENV` | inferred from the key | `test` / `live`; only a cross-check, a mismatch is reported |
| `BAPI_ALLOW_LIVE` | off | must be `true` before any live POST / PATCH / DELETE |
| `BAPI_INLINE_SCHEMAS` | off | `true` puts the full lodgement schemas into `tools/list` |
| `BAPI_TOOLSETS` | `all` | comma-separated groups to expose (see below) |

Lookup order: environment variables → `./.env` in the directory the server starts from → the user config file
(`%APPDATA%\businessapi-mcp\.env`, `~/Library/Application Support/businessapi-mcp/.env`, `~/.config/businessapi-mcp/.env`).

### Context size: lazy schemas and toolsets

Inlining the full schemas of the lodgement tools into `tools/list` costs about 147k characters (~37k tokens) on every
connection. By default the `request` parameter of each `lodge_*` tool is a plain JSON object and the model fetches the
exact schema on demand with `get_request_schema(form)` or `bapi://schemas/{form}`; validation is unchanged and errors
come back as a list of field paths.

| Configuration | Tools | Chars | ≈Tokens |
|---|---|---|---|
| `BAPI_INLINE_SCHEMAS=true`, all toolsets | 46 | 146,619 | 36,654 |
| default (lazy), all toolsets | 46 | 52,432 | 13,108 |
| `BAPI_TOOLSETS=lookup,registrations,core` | 18 | 20,409 | 5,102 |
| `BAPI_TOOLSETS=agent_forms,core` | 15 | 17,893 | 4,473 |
| `BAPI_TOOLSETS=monitoring,webhooks` | 16 | 17,012 | 4,253 |
| `BAPI_TOOLSETS=lookup,ppsr` | 7 | 7,494 | 1,873 |

`BAPI_TOOLSETS` lets one package run as several small servers — see `.mcp.json.example`. Leaving `core` off a server
also removes the `call_endpoint` escape hatch. Re-measure with `uv run scripts/tools_list_size.py`.

## Command line

```text
businessapi-mcp                      run the MCP server on stdio (what clients launch)
businessapi-mcp run --transport http --port 8000
businessapi-mcp setup [--local]      store the key (hidden prompt) and verify it
businessapi-mcp status               where the key comes from, which environment is active
businessapi-mcp doctor [--lodge]     sandbox self-test through the real tools; --lodge also lodges the examples
businessapi-mcp install <client>     claude-code | claude-desktop | cursor | vscode | mcp-json  (--dry-run, --dev REPO)
```

`doctor` refuses live keys. Its read-only run covers account status, lookups, name checks, industry search, the ABN
entitlement questionnaire, a PPSR search polled to completion, a $0 monitoring order with a simulated event and
`list_webhooks`; `--lodge` additionally submits the example company, sole-trader ABN and business-name payloads to the
sandbox and polls them (observed: company `finished` with a test ACN, ABN `validation ok`, business name
`wating ASIC CC payment` with an ASIC UAT payment link).

## What the vendor docs get wrong (verified in the sandbox)

- Two helpers need query parameters the docs omit: `/helpers/business-industries?searchTerm=` and
  `/helpers/entitlement-questionnaire?entityType=&entitySubType=`.
- Every `officeHolders[]` entry in a company registration needs `addressOverriddenIndicator` (400 without it); the
  model defaults it to `false`.
- Company registration POST answers `{requestId, status, billing}` at the top level, not `{response: {requestId}}`;
  the finished status uses `registrationDate`, not `dateOfRegistration`; ASIC rejections arrive as
  `errors[].details.text` (the full advice letter) with a snake_case `response`.
- ABN lookups return PascalCase keys (`EntityName`, `AbnStatus`, …).
- Share money fields: the rules say integers in cents, the vendor sample sends `100` for 100 × $1 shares, and the
  sandbox accepts either — agree the convention with the vendor before a live lodgement.
- Several vendor samples carry the placeholder ACN `123456789`, which fails the checksum their own docs require.
- Endpoints without a public docs page (name reservations, extracts, `tax/registrations`, most RA forms, ASIC inbox)
  are reachable only through `call_endpoint`.

## Development

```bash
git clone https://github.com/JoeFu/businessapi-mcp && cd businessapi-mcp
uv sync --group dev
uv run pytest                                   # offline: vendor samples, rules, server modes, CLI
uv run businessapi-mcp install claude-code --dev .   # point your client at this checkout
uv run businessapi-mcp doctor                   # needs a sandbox key in ./.env or via setup
uv run --group dev scripts/export_openapi.py    # refresh openapi.yaml after editing models.py
```

Layout: `src/businessapi_mcp/` (`server.py` tools and docs, `models.py` request models, `config.py` key discovery,
`cli.py`, `smoke.py` doctor, `examples/`), `tests/` with the vendor samples as fixtures, `scripts/` (OpenAPI export,
an OpenAPI-generated alternative server, tools/list size measurement), `openapi.yaml`.

Adding a lodgement: write the request model in `models.py`, a `_lodge_x(request, dry_run)` implementation in
`server.py`, and one row in the `LODGEMENTS` registry; lazy / inline mode, toolset filtering and `get_request_schema`
follow automatically. Other tools use `@tool("<group>", annotations=...)`.

Releases: push a `v*` tag; `publish.yml` builds and publishes to PyPI through trusted publishing.

## License

MIT — see [LICENSE](LICENSE). Not affiliated with Business API Pty Ltd, ASIC or the ATO.
