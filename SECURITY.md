# Security

## What this server can do

It lodges real forms with Australian regulators (ASIC, ATO, PPSR) on your Business API account and spends money when
run against the live environment. The design keeps that behind several explicit steps:

- **Sandbox by default.** The environment is inferred from the key prefix: `bapi_sk_test_…` keys only ever reach
  `https://businessapi.com.au/api/test/v2`, where nothing is billed and nothing reaches production registries.
- **Live writes are blocked unless `BAPI_ALLOW_LIVE=true`.** With a live key, every POST / PATCH / DELETE is refused
  before any HTTP request unless that variable is set for the server process. Keep it off until you intend to lodge.
- **Dry run first.** Every lodgement tool and the monitoring order / renewal tools default to `dry_run=true`: they
  validate and return the exact payload without sending it. The model has to pass `dry_run=false` explicitly.
- **No hidden flags.** `testTransmissionIndicator` is injected from the server's environment; anything a model sends
  is ignored. The sandbox-only `simulate_monitoring_event` tool refuses to run against live.
- **Idempotency.** Monitoring orders and renewals require a caller-supplied `Idempotency-Key`, so retries never
  charge twice.

## Secrets

- The API key lives in your user config file (`businessapi-mcp setup`), a project `.env`, or the `BAPI_API_KEY`
  environment variable. Client configurations written by `businessapi-mcp install` never contain the key.
- `.env`, `.mcp.json`, downloads and run logs are git-ignored. Do not commit keys; rotate any key that leaks at
  https://businessapi.com.au/user/api-keys.
- TFNs (tax file numbers) are checksum-validated locally and never logged; tool descriptions instruct the model not to
  repeat them back. Webhook signing secrets are returned once by the API and should be stored immediately.

## Reporting a vulnerability

Please open a private security advisory on GitHub (Security → Report a vulnerability) rather than a public issue.
Include the version (`businessapi-mcp --version`), the client you used, and steps to reproduce. Issues in Business API
itself should go to the vendor: https://businessapi.com.au/contact.
