# businessapi.com.au → MCP server

Wraps Business API (an Australian ASIC / ATO / ABRS / PPSR digital service provider) as an MCP server so an LLM client
such as Claude Code, Claude Desktop or Cursor knows how to use it the moment it connects — and can safely carry out
real work: company registration / name change / deregistration, ABN applications, business-name registration and
renewal, ASIC Forms 484 / 361 / 362 / RA52, PPSR searches, entity monitoring and webhook management.

## The short version

- The vendor publishes **no OpenAPI / Swagger**, only HTML docs (`https://businessapi.com.au/developers/api/<name>`,
  13 pages plus an endpoint summary, a webhooks page and the ASIC/ATO code tables). Nothing can be generated in one
  step, so this project translates the docs into Pydantic models, tool descriptions and resource documents.
- The API is REST + JSON with `Authorization: Bearer bapi_sk_{test|live}_...`. Sandbox base URL
  `https://businessapi.com.au/api/test/v2/`, live `https://businessapi.com.au/api/v2/`. **Almost every POST is an
  asynchronous lodgement**: it returns a `requestId`, and you poll a status endpoint or receive a webhook. Live calls
  are billed per lodgement.
- The model "knows how to use it" because of three things it receives in the MCP handshake:
  1. the server `instructions` — environment, async flow, pricing, dry-run workflow, which resource to read first,
     how to fetch a schema;
  2. each tool's **name + description + parameter schema** — lodgement bodies are validated by Pydantic models whose
     field names match the vendor docs one-to-one, with field descriptions, enums (ASIC/ATO code tables) and the
     "Mandatory IF …" cross-field rules built into the validators;
  3. **resources** — 12 guides under `bapi://docs/*` and 9 request schemas under `bapi://schemas/{form}`.

## Files

| File | Purpose |
|---|---|
| `server.py` | Route A: the hand-written MCP server. 46 tools, 12 doc resources, 1 schema resource template, 1 workflow prompt |
| `models.py` | Pydantic request models (Form 201 / ABNREG / BNREG / BNRENEW / 484 / 205A / 6010 / 361 / 362 / RA52 / monitored entity), ABN/ACN/TFN checksums, code tables, webhook URL validation |
| `sandbox_smoke_test.py` | End-to-end test against the sandbox with a test key (see **Testing with a sandbox key**) |
| `examples/*.json` | Example request payloads used by the smoke test (company, sole-trader ABN, business name for a company) |
| `tools_list_size.py` | Measures the context cost of `tools/list` under each configuration |
| `openapi.yaml` | Route B: OpenAPI 3.1; lodgement schemas generated from `models.py` by `export_openapi.py`, the rest hand-written |
| `server_from_openapi.py` | Route B: `FastMCP.from_openapi()` turns every `operationId` in the YAML into a tool |
| `export_openapi.py` | Merges the model JSON Schemas plus the monitoring and webhook paths into `openapi.yaml` (re-run after editing models) |
| `.env.example` | Template for the git-ignored `.env` that holds your key |
| `.mcp.json.example` | Client configuration for Claude Code / Claude Desktop / Cursor (single server and split-by-toolset variants) |

## Quick start

1. Register at https://businessapi.com.au/register and create a **test** key at https://businessapi.com.au/user/api-keys
   (prefix `bapi_sk_test_`; the sandbox is free and needs no card).
2. Put the key in `bapi-mcp/.env` (copy `.env.example`; the file is git-ignored) or export `BAPI_API_KEY`.
3. Run the server (needs [uv](https://docs.astral.sh/uv/); fastmcp is installed on first run):

```bash
uv run --with fastmcp bapi-mcp/server.py
```

4. Register it with Claude Code (adjust the path):

```bash
claude mcp add bapi -e BAPI_ENV=test -- uv run --with fastmcp C:/path/to/businessapi-mcp/bapi-mcp/server.py
```

   With a `.env` in place you do not need `-e BAPI_API_KEY=...`. Alternatively drop a project `.mcp.json` based on
   `.mcp.json.example`, or paste the `mcpServers.bapi` block into Claude Desktop's `claude_desktop_config.json`.

5. Try it in a conversation:
   - "Which company has ABN 51 824 753 556?" → `lookup_abn`
   - "Is the company name ACME TRADING PTY LTD available?" → `check_company_name_availability`
   - "Register a company in the sandbox for me" → the model reads `bapi://docs/company-registrations`, calls
     `get_request_schema("company_registration")`, collects the fields from you, calls `lodge_company_registration`
     with `dry_run=true` to show you the payload, and only lodges after you confirm; then `wait_for_request` polls the result.

Dependency note: the code targets fastmcp 4.x, which ships the `httpx2` fork (hence `import httpx2 as httpx`).
Route B additionally needs `pyyaml`: `uv run --with fastmcp --with pyyaml bapi-mcp/server_from_openapi.py`.

## Testing with a sandbox key

`sandbox_smoke_test.py` drives the real MCP tools in-process against the sandbox. It refuses live keys.

```bash
uv run --with fastmcp bapi-mcp/sandbox_smoke_test.py
```

The default run is read-only plus local validation: tools/list, `get_request_schema`, dry-run validation of the three
example payloads, `get_account_status`, ABN/ACN lookups, name-availability checks, a business-industry search, the ABN
entitlement questionnaire, a PPSR search created and polled to completion, an entity-monitoring order (sandbox price $0;
re-runs are idempotent thanks to the fixed idempotency key), a simulated monitoring event, and `list_webhooks`.
A `200` whose body carries a non-empty `errors` object counts as a failure.

```bash
uv run --with fastmcp bapi-mcp/sandbox_smoke_test.py --lodge
```

`--lodge` additionally submits the example company registration, sole-trader ABN application and business-name
registration to the sandbox (ASIC and ATO **test** services, nothing is billed) and polls each for up to three minutes,
printing the final status, document number, warnings and errors. Observed on 2026-11-18: the company reached `finished`
with a test ACN within two polls; the ABN application reached `validation ok` (the ATO test service finishes later —
re-check with `get_request_status`); the card-paid business name reached `wating ASIC CC payment` with an ASIC UAT
payment link. `--no-monitoring` skips the monitoring steps on accounts where monitoring is not enabled.

Exit code 1 means at least one FAIL; a 401 on `get_account_status` means the key was rejected and the run stops there.

Things worth confirming with your own sandbox run, because the vendor docs contradict themselves:
- Share money fields: the validation rules say integers whose last two digits are cents (100 = $1.00), the vendor's
  own sample sends `100` for 100 × $1 shares. The example payload follows the sample, and the sandbox accepted it
  unchanged — the units are not validated server-side, so agree the convention with the vendor before a live lodgement.
- Address `country`: the rules say a 2-character code, the sample writes `NEW ZEALAND`.
- Form 484 `officeHolders.status` / `members.holdingStatus`: the 484 page glosses the codes differently from ASIC
  Tables 0006 / 0028; the models document the ASIC tables.
- The authentication page says V2 ignores `testTransmissionIndicator`, the 484 / ABN / business-name pages call it
  required. The server always injects it from the environment, which is safe either way.

## Context size: lazy schemas and toolsets

Inlining the full schemas of the ten lodgement tools into `tools/list` costs about 147k characters (~37k tokens) on
every connection. The default is therefore a **lazy** mode: the `request` parameter of each `lodge_*` tool is declared
as a plain JSON object, and the model fetches the exact schema when it needs it via `get_request_schema(form)` or the
resource `bapi://schemas/{form}`. Validation is unchanged — the same Pydantic model runs server-side and errors come
back as a readable list (e.g. `generalInformation.abn: '123' is not a valid ABN`).

| Configuration | Tools | Chars | ≈Tokens |
|---|---|---|---|
| `BAPI_INLINE_SCHEMAS=true`, all toolsets | 46 | 146,619 | 36,654 |
| default (lazy), all toolsets | 46 | 52,432 | 13,108 |
| `BAPI_TOOLSETS=lookup,registrations,core` | 18 | 20,409 | 5,102 |
| `BAPI_TOOLSETS=agent_forms,core` | 15 | 17,893 | 4,473 |
| `BAPI_TOOLSETS=monitoring,webhooks` | 16 | 17,012 | 4,253 |
| `BAPI_TOOLSETS=lookup,ppsr` | 7 | 7,494 | 1,873 |

(Re-run with `uv run --with fastmcp bapi-mcp/tools_list_size.py`; tokens estimated at 4 characters each.)

- `BAPI_INLINE_SCHEMAS=true` restores inline schemas — useful for clients that render forms from the schema, or when
  only one or two lodgement tools are exposed.
- `BAPI_TOOLSETS=a,b` registers only those groups, so one codebase can run as several small servers. Groups: `lookup`
  (free lookups), `registrations` (company / ABN / business-name registration and renewal), `agent_forms` (484 / 205A /
  6010 / 361 / 362 / RA52), `monitoring`, `webhooks`, `ppsr`, `core` (status polling, account, `call_endpoint`).
  `get_request_schema` appears with `registrations` or `agent_forms`. Unknown group names fail at startup. Every tool also
  carries its group as a tag (`_meta.fastmcp.tags`) for clients that filter on tags.
- Documentation resources are not affected by toolsets (the resource list is only URIs and names; contents load on demand).

## Tools (server.py, 46)

| Group | Tools | Notes |
|---|---|---|
| `lookup` | `lookup_abn` `lookup_acn` | ABN / ACN validation and lookup |
| | `check_company_name_availability` | company name availability |
| | `check_business_name_availability(name, holder_name)` | business-name availability; `holder_name` unlocks `availableForHolder` |
| | `search_business_industries(search_term)` | ATO main-business-activity codes by keyword (`code` → businessActivity.code) |
| `registrations` | `lodge_company_registration(request, dry_run=True)` | ASIC Form 201, about 140 fields |
| | `lodge_abn_registration(request, dry_run=True)` | ATO ABN application, 5 entity types |
| | `lodge_business_name_registration(request, dry_run=True)` | ASIC business name, 5 holder types |
| | `lodge_business_name_renewal` `get_business_name_renewal_status` | renew for 1 or 3 years |
| | `get_business_name_registration_status` `refresh_business_name_payment_link` | payment link / re-issue link |
| | `get_abn_entitlement_questionnaire(entity_type, entity_sub_type)` | ABN entitlement questions whose answers go into `entitlement.questionnaire` |
| | `get_request_schema(form)` | schema of any `lodge_*` request (also present with `agent_forms`) |
| `agent_forms` | `lodge_asic_484_form` `get_asic_484_form_status` | company changes: addresses, officeholders, share issues / cancellations, UHC, concessional fees, annexures |
| | `lodge_company_name_change` `get_company_name_change_status` | Form 205A |
| | `lodge_company_deregistration` `get_company_deregistration_status` | Form 6010 voluntary deregistration (all six s.601AA declarations must be true) |
| | `lodge_asic_361_form` `lodge_asic_362_form` `get_asic_form_status(form, id)` | agent ceases to act / company appoints or ceases an agent |
| | `request_asic_account_transactions(start, end, …)` | RA52 account transactions report |
| `monitoring` | `start_entity_monitoring(entities, idempotency_key, dry_run=True)` `renew_entity_monitoring` | order / renew up to 25 entities, $18 per entity per year |
| | `list_monitored_entities` `get_monitored_entity` `stop_entity_monitoring` `resume_entity_monitoring` | list / show / stop / resume |
| | `get_entity_snapshot` `list_monitoring_events` `get_monitoring_event` `simulate_monitoring_event` | snapshot, change events, sandbox simulation |
| `webhooks` | `list_webhooks` `create_webhook` `update_webhook` `delete_webhook` `rotate_webhook_secret` | endpoint management (the secret is returned once, on create and rotate) |
| | `verify_webhook_signature(secret, header, raw_body)` | local HMAC-SHA256 check of `X-Bapi-Signature`, no network |
| `ppsr` | `create_ppsr_search` `get_ppsr_search` | $6 per search live |
| `core` | `get_request_status` `wait_for_request(kind, timeout)` | poll to a terminal state; `kind` covers 10 status endpoints |
| | `get_account_status` `call_endpoint(method, path, …)` | account state; escape hatch for undocumented endpoints |

Resources: `bapi://docs/overview` `endpoints` `company-registrations` `abn-registrations` `business-name-registrations`
(incl. renewal) `asic-484-forms` `asic-agent-forms` (205A / 6010 / 361 / 362 / RA52) `entity-monitoring` `webhooks`
`ppsr-searches` `asic-codes` `ato-codes`; `bapi://schemas/{form}` with form ∈ company_registration, abn_registration,
business_name_registration, business_name_renewal, asic_484_form, company_name_change, company_deregistration,
asic_361_form, asic_362_form.

## Environment variables

| Variable | Default | Effect |
|---|---|---|
| `BAPI_API_KEY` | required | `bapi_sk_test_…` or `bapi_sk_live_…` (also read from `bapi-mcp/.env`) |
| `BAPI_ENV` | `test` | `test` / `live`; must match the key prefix |
| `BAPI_ALLOW_LIVE` | off | must be `true` before any live POST / PATCH / DELETE is allowed |
| `BAPI_INLINE_SCHEMAS` | off | `true` inlines the full lodgement schemas into `tools/list` |
| `BAPI_TOOLSETS` | `all` | comma-separated group names to register |

## Safety design

- Sandbox by default (`BAPI_ENV=test`); a key whose prefix does not match the environment is rejected at startup.
- Every live POST / PATCH / DELETE is refused before any HTTP call unless `BAPI_ALLOW_LIVE=true`, because those lodge real
  forms with ASIC / ATO and are billed (company registration ≈ $50 + ASIC fee, ABN ≈ $25, business name ≈ $15 + ASIC
  fee, PPSR search $6, entity monitoring $18 per entity per year, registered-agent forms per plan).
- All `lodge_*` tools and the monitoring order / renew tools default to **`dry_run=true`**: they validate locally and
  return the exact JSON that would be sent; the model must pass `dry_run=false` explicitly to lodge. That is the natural
  "show the user the payload and fees, then confirm" checkpoint. Deregistration also carries `destructiveHint`.
- Lazy mode does not weaken validation: the same Pydantic models run server-side and report field paths.
- Monitoring orders / renewals require a caller-supplied `idempotency_key` (the vendor requires it for renewals);
  retrying with the same key never charges twice.
- `testTransmissionIndicator` is injected by the server from its environment; anything the model sends is ignored, so a
  sandbox server cannot be tricked into a live lodgement flag.
- `simulate_monitoring_event` refuses to run in the live environment; webhook URLs are checked locally for HTTPS and a
  public host before the API sees them.
- Individual PPSR searches require the user to confirm an authorised purpose under the PPS Act (`authorised_purpose`).
- TFNs are checksum-validated locally only, and tool descriptions tell the model never to repeat a TFN back to the user;
  webhook secrets appear once and the tool description tells the model to hand them to the user immediately.
- Every tool carries MCP `annotations` (`readOnlyHint` / `destructiveHint`) and a toolset tag; clients that honour them
  can decide when to ask for confirmation.
- When splitting into several servers, leaving `core` off the monitoring / webhook server removes the `call_endpoint`
  escape hatch and shrinks its permission surface.

## What the local validation covers (models.py)

- Company registration: valid type / class / sub-class combinations (ASIC Table 0003), LMGT vs share structure,
  unique alphabetically ordered share classes, registered-office occupier rules, public-company opening hours,
  directors 18+, forbidden words in personal names, applicant individual-or-organisation, signing date not in the future.
- ABN: entity type ↔ sub-type pairing, sole-trader rules (no TFN application, TFN or residential address, no associates),
  ACN for companies (027 / 032), minimum associates, `positionHeld` whitelist per sub-type, at most one POF, unique TFNs,
  date windows (after 1999, at most six months ahead), no PO box for the business address, `taxRegistrations` key
  whitelist, both SMSF blocks.
- Business name: required blocks per holder type (IND / IB with ACN / PTSH partners / USTR trustees / JV associates with
  their own ABN), ABN required except JV, subType for IB / PTSH / USTR, term 1 or 3. Renewal: ABN, term 1 or 3, declaration.
- Form 484: at least one change section; share issue / cancellation need `shareStructure` and `members`;
  `members.shareClass` must exist; ASIC V455 totals; signature date within six months; no future dates; officers 18+;
  strings upper-cased before sending.
- Name change: `proposedName` vs `acnNameIndicator`, reservation block, meeting date, payment method DDEB / INV.
- Deregistration: all six s.601AA declarations true; applicant individual-or-organisation; nominee address; signing date.
- 361 / 362 / RA52: ACN checksum, 362 type A / C only, 361 `agentAuthorisedIndicator` and `declaration` true, RA52 date range.
- Monitoring: each entity needs an ACN and/or ABN (checksum), reference ≤ 100 chars, ≤ 25 per batch, idempotency key 1–255 chars.
- Webhooks: HTTPS and not localhost / private / link-local / CGNAT (Python also treats the TEST-NET documentation ranges
  as private); event types de-duplicated.
- Everywhere: ABN / ACN / TFN checksums; empty strings, nulls and empty objects / arrays are pruned (the vendor samples
  pad unused blocks with `""`).

## Verification log (2026-11-18)

- Every parseable sample payload in the vendor docs validates: company registration 1, ABN 5 entity types, business
  name 5 holder types, business-name renewal 1, Form 484 1, name change 1, deregistration 1, 361 1, 362 1, monitoring
  order 1 (the RA52 sample is not valid JSON, so a hand-written one is used). Three placeholder mistakes in the vendor
  samples were corrected for the tests: ACN `123456789` (fails the checksum the docs require) in 484 / 6010 / 361 / 362,
  `dateSigned` written as `20213006` in 6010, and `includePriorOutstandingIndicator` written as `"123456789"` in RA52.
- 14 deliberately broken payloads are all rejected with readable errors; in lazy mode a single request reports every
  offending field path at once.
- The three regression suites give identical results in lazy and inline mode; `BAPI_TOOLSETS=monitoring,webhooks` leaves
  16 tools and no `call_endpoint`; an unknown group name fails at startup; `get_request_schema` and `bapi://schemas/{form}`
  return JSON Schema with `$defs`.
- Real requests to the sandbox with a dummy key (including a monitoring order with the `Idempotency-Key` header, webhook
  creation and 484 / RA52 / deregistration status polling) all return `401 Invalid API key.`, confirming URLs,
  authorization header, custom headers and response envelope; `wait_for_request` stops on HTTP errors.
- Started with `BAPI_ENV=live` and a dummy live key: stop / renew / simulate monitoring, business-name renewal, webhook
  create / delete / rotate and RA52 are all refused before any HTTP call.
- Real sandbox run with a test key (`sandbox_smoke_test.py`, read-only mode): all 21 steps passed — account status,
  ABN/ACN lookups (the ABR response uses PascalCase keys: `EntityName`, `AbnStatus`, …), both name checks, industry
  search, entitlement questionnaire, a PPSR search that completed after two polls with one registration and a
  certificate, a $0 monitoring order, a simulated `document_lodged` event, `list_webhooks`. Two helper endpoints turned out
  to need query parameters the docs do not mention (`searchTerm`; `entityType` + `entitySubType`) and were fixed.
- `--lodge` sandbox run with the example payloads: the company registration first came back `400` because every
  officeholder needs an `addressOverriddenIndicator` the vendor's Form 201 page never mentions (now defaulted to `false`
  in the model); re-lodged, it reached `finished` after two polls with a test ACN and `documentNumber`. The POST answers
  `{requestId, status, billing}` at the top level and the finished status uses `registrationDate`, both unlike the docs.
  The sole-trader ABN application was accepted (`received`, `billing.price_cents: 0`) and reached `validation ok` on the
  ATO test service. The card-paid business name reached `wating ASIC CC payment` with an ASIC UAT payment link and a
  `transactionFee` of 108.00 for three years.

## Endpoints without a public docs page (reachable only through `call_endpoint`)

`/company-name-reservations` (410B), `/company-name-reservation-extensions` (410F), `/asic/extracts`,
`/tax/registrations` (TAXREG), `/asic-370|492|902|ra17|ra50|ra51|ra61|ra63|ra65|ra67|ra71-forms`, `/asic-inbox`,
`/asic-inbox/{id}/pdf`. The list is also in `bapi://docs/asic-agent-forms`; request shapes have to come from the vendor
or their Postman collection.

## Extending

- New lodgement: add a request model in `models.py`, write a `_lodge_xxx(request: Model, dry_run)` implementation in
  `server.py`, and add one row to the `LODGEMENTS` registry (key, tool name, group, model, implementation). Lazy / inline
  mode, toolset filtering and `get_request_schema` follow automatically.
- New plain tool: decorate with `@tool("<group>", annotations=...)` instead of `@mcp.tool`.
- Route B: after editing `models.py`, run `uv run --with fastmcp --with pyyaml bapi-mcp/export_openapi.py` to refresh
  `openapi.yaml`; other endpoints go straight into the YAML and `server_from_openapi.py` needs no change. Route B has no
  environment injection, live-write guard or lazy mode — use it for the sandbox or read-only work.
- Production: switch to HTTP transport (`mcp.run(transport="http", port=8000)`) so the API key stays on the server and is
  never handed to clients; use webhooks (port `verify_webhook_signature`'s logic into the receiver) instead of polling.
