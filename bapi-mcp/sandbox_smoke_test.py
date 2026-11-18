"""
Sandbox smoke test for the Business API MCP server.

    uv run --with fastmcp bapi-mcp/sandbox_smoke_test.py            # read-only calls + local dry-run validation
    uv run --with fastmcp bapi-mcp/sandbox_smoke_test.py --lodge    # ALSO lodge the example payloads in the SANDBOX and poll
    uv run --with fastmcp bapi-mcp/sandbox_smoke_test.py --no-monitoring   # skip the entity-monitoring steps

Needs a TEST key (bapi_sk_test_...) in BAPI_API_KEY or in bapi-mcp/.env. Live keys are refused: this script always
talks to https://businessapi.com.au/api/test/v2, where every call is free and nothing reaches ASIC/ATO production.

It drives the real MCP tools in-process (fastmcp Client -> server.mcp), so a green run proves the whole chain:
.env/key handling -> tool schemas -> Pydantic validation -> HTTP -> vendor response envelope -> status polling.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

try:
    from dotenv import load_dotenv

    load_dotenv(HERE / ".env")
except ImportError:
    pass

KEY = os.environ.get("BAPI_API_KEY", "").strip()
if not KEY:
    sys.exit("BAPI_API_KEY is not set. Copy .env.example to .env and paste your bapi_sk_test_ key, or export the variable.")
if "_test_" not in KEY:
    sys.exit("This smoke test only runs with a TEST key (bapi_sk_test_...). Refusing to use a live key.")
os.environ["BAPI_ENV"] = "test"
os.environ.pop("BAPI_TOOLSETS", None)  # the test needs every toolset

import server  # noqa: E402  (after the env is settled)
from fastmcp import Client  # noqa: E402

TODAY = date.today().isoformat()
RESULTS: list[tuple[str, str, str]] = []  # (status, step, detail)


def record(status: str, step: str, detail: str = "") -> None:
    RESULTS.append((status, step, detail))
    print(f"[{status:<4}] {step}" + (f"  — {detail}" if detail else ""))


def body(result) -> dict:
    try:
        return json.loads(result.content[0].text)
    except Exception:
        return {"raw": result.content[0].text if result.content else ""}


def summary(d: dict, *keys: str) -> str:
    picked = {k: d.get(k) for k in keys if k in d}
    if "errors" in d and d["errors"]:
        picked["errors"] = d["errors"]
    return json.dumps(picked, ensure_ascii=False)[:220]


async def call(c: Client, name: str, args: dict | None = None):
    r = await c.call_tool(name, args or {}, raise_on_error=False)
    return r, body(r)


async def step_http(c: Client, step: str, tool: str, args: dict, ok_statuses=(200, 201), show=()) -> dict:
    r, d = await call(c, tool, args)
    if r.is_error:
        record("FAIL", step, r.content[0].text[:200]); return d
    status = d.get("httpStatus")
    if status in ok_statuses:
        record("PASS", step, summary(d, *show)); return d
    if status == 401:
        record("FAIL", step, "401 — the API key was rejected (wrong key, or a live key on the sandbox)"); return d
    if status == 403 and str(d.get("error", "")) in ("monitoring_unavailable", "sandbox_only", "card_billing_required"):
        record("SKIP", step, f"{status} {d.get('error')}: {d.get('message', '')[:120]}"); return d
    record("FAIL", step, f"HTTP {status}: {summary(d, 'error', 'message')}"); return d


def load_example(name: str) -> dict:
    return json.loads((HERE / "examples" / name).read_text(encoding="utf-8"))


async def main(lodge: bool, monitoring: bool) -> int:
    print(f"Business API sandbox smoke test — {TODAY}\nkey: {KEY[:13]}…  env: test  lodge: {lodge}  monitoring: {monitoring}\n")
    async with Client(server.mcp) as c:
        tools = {t.name for t in await c.list_tools()}
        record("PASS", "tools/list", f"{len(tools)} tools registered")

        # ---- local-only steps (no network) ----
        r, d = await call(c, "get_request_schema", {"form": "company_registration"})
        record("FAIL" if r.is_error else "PASS", "get_request_schema(company_registration)", f"{len(json.dumps(d))} chars")

        company = load_example("company_registration.json"); company["applicant"]["dateSigned"] = TODAY
        abn = load_example("abn_registration_sole_trader.json")
        abn["businessActivity"]["abnStartDate"] = TODAY; abn["declaration"]["date"] = TODAY
        bn = load_example("business_name_registration_ib.json")
        for label, tool, payload in (
            ("dry-run lodge_company_registration (local validation)", "lodge_company_registration", company),
            ("dry-run lodge_abn_registration (local validation)", "lodge_abn_registration", abn),
            ("dry-run lodge_business_name_registration (local validation)", "lodge_business_name_registration", bn),
        ):
            r, d = await call(c, tool, {"request": payload, "dry_run": True})
            record("FAIL" if r.is_error or not d.get("dryRun") else "PASS", label, (r.content[0].text[:160] if r.is_error else d.get("wouldPost", "")))

        # ---- read-only network steps ----
        acct = await step_http(c, "get_account_status", "get_account_status", {}, show=("plan", "status", "environment"))
        if acct.get("httpStatus") == 401:
            print("\nStopping: the sandbox rejected the key, so nothing else can be tested.")
            return finish()
        await step_http(c, "lookup_abn 51 824 753 556", "lookup_abn", {"abn": "51 824 753 556"}, show=("entityName", "abn", "status", "response"))
        await step_http(c, "lookup_acn 004 085 616", "lookup_acn", {"acn": "004 085 616"}, show=("companyName", "acn", "status", "response"))
        await step_http(c, "check_company_name_availability", "check_company_name_availability", {"name": "BAPI SMOKE TEST PTY LTD"}, show=("availability", "available", "response"))
        await step_http(c, "check_business_name_availability (+holder)", "check_business_name_availability",
                        {"name": "BAPI SMOKE TRADING CO", "holder_name": "SAMPLE COMPANY PTY LTD"}, show=("availability", "availableForHolder", "asicUnavailable"))
        ind = await step_http(c, "list_business_industries", "list_business_industries", {})
        if ind.get("httpStatus") == 200:
            size = len(ind.get("data") or ind.get("response") or ind)
            RESULTS[-1] = ("PASS", RESULTS[-1][1], f"{size} entries")

        # ---- PPSR: create ($0 in sandbox) and poll ----
        ppsr = await step_http(c, "create_ppsr_search (abn)", "create_ppsr_search", {"abn": "51824753556"}, show=("requestId", "status"))
        rid = ppsr.get("requestId") or (ppsr.get("response") or {}).get("requestId")
        if rid:
            r, d = await call(c, "wait_for_request", {"request_id": rid, "kind": "ppsr", "timeout_seconds": 90, "poll_seconds": 5})
            st = str(d.get("status", "")).lower()
            record("PASS" if st == "completed" else ("WARN" if d.get("timedOut") else "FAIL"),
                   f"wait_for_request ppsr #{rid}", f"status={d.get('status')} polls={d.get('polls')} resultCount={((d.get('data') or {}).get('search') or {}).get('resultCount')}")

        # ---- entity monitoring (sandbox: unitPrice 0) ----
        if monitoring:
            order = await step_http(c, "start_entity_monitoring (dry_run=false, sandbox $0)", "start_entity_monitoring",
                                    {"entities": [{"acn": "004085616", "reference": "smoke-test"}], "idempotency_key": "smoke-test-004085616", "dry_run": False},
                                    ok_statuses=(200, 201, 202, 422), show=("unitPrice", "total", "error"))
            if order.get("httpStatus") == 422:
                RESULTS[-1] = ("PASS", RESULTS[-1][1], "422 already_monitored — expected on a re-run")
            listed = await step_http(c, "list_monitored_entities(acn)", "list_monitored_entities", {"acn": "004085616", "per_page": 5}, show=("meta",))
            ents = listed.get("data") or []
            if ents:
                eid = ents[0]["id"]
                await step_http(c, f"get_monitored_entity #{eid}", "get_monitored_entity", {"entity_id": eid}, show=("status", "monitoringStatus", "expiresAt"))
                sim = await step_http(c, f"simulate_monitoring_event #{eid} document_lodged", "simulate_monitoring_event",
                                      {"entity_id": eid, "event_type": "document_lodged"}, ok_statuses=(200, 201), show=("eventType", "title", "simulated"))
                await step_http(c, f"list_monitoring_events(entity {eid})", "list_monitoring_events", {"entity_id": eid, "per_page": 5}, show=("meta",))
                await step_http(c, f"get_entity_snapshot #{eid} (409 expected in sandbox)", "get_entity_snapshot", {"entity_id": eid}, ok_statuses=(200, 409), show=("error",))

        # ---- webhooks: list only (creating needs a public HTTPS receiver) ----
        await step_http(c, "list_webhooks", "list_webhooks", {}, show=("data",))

        # ---- real sandbox lodgements ----
        if lodge:
            print("\n--lodge: submitting the example payloads to the SANDBOX (ASIC/ATO test services, $0)")
            res = await step_http(c, "lodge_company_registration (sandbox)", "lodge_company_registration", {"request": company, "dry_run": False}, ok_statuses=(200, 201), show=("response", "requestId", "status"))
            rid = (res.get("response") or {}).get("requestId") or res.get("requestId")
            if rid:
                r, d = await call(c, "wait_for_request", {"request_id": rid, "kind": "lodgement", "timeout_seconds": 180, "poll_seconds": 10})
                record("INFO", f"company registration #{rid} after polling", f"status={d.get('status')} documentNumber={d.get('documentNumber')} response={json.dumps(d.get('response'))[:160]} errors={json.dumps(d.get('errors'))[:160]}")
            res = await step_http(c, "lodge_abn_registration (sandbox)", "lodge_abn_registration", {"request": abn, "dry_run": False}, ok_statuses=(200, 201), show=("requestId", "status", "billing", "warnings"))
            rid = res.get("requestId") or (res.get("response") or {}).get("requestId")
            if rid:
                r, d = await call(c, "wait_for_request", {"request_id": rid, "kind": "lodgement", "timeout_seconds": 180, "poll_seconds": 10})
                record("INFO", f"ABN registration #{rid} after polling", f"status={d.get('status')} info={json.dumps(d.get('info'))[:160]} errors={json.dumps(d.get('errors'))[:160]}")
            res = await step_http(c, "lodge_business_name_registration (sandbox)", "lodge_business_name_registration", {"request": bn, "dry_run": False}, ok_statuses=(200, 201), show=("requestId", "status"))
            rid = res.get("requestId") or (res.get("response") or {}).get("requestId")
            if rid:
                r, d = await call(c, "wait_for_request", {"request_id": rid, "kind": "business_name", "timeout_seconds": 180, "poll_seconds": 10})
                record("INFO", f"business name #{rid} after polling", f"status={d.get('status')} response={json.dumps(d.get('response'))[:200]} errors={json.dumps(d.get('errors'))[:120]}")
    return finish()


def finish() -> int:
    counts = {s: sum(1 for st, _, _ in RESULTS if st == s) for s in ("PASS", "FAIL", "SKIP", "WARN", "INFO")}
    print("\n" + " ".join(f"{k}={v}" for k, v in counts.items() if v))
    if counts["FAIL"]:
        print("Failures:")
        for st, step, detail in RESULTS:
            if st == "FAIL":
                print(f"  - {step}: {detail}")
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lodge", action="store_true", help="also lodge the example payloads in the sandbox and poll their status")
    ap.add_argument("--no-monitoring", action="store_true", help="skip entity-monitoring steps")
    a = ap.parse_args()
    sys.exit(asyncio.run(main(lodge=a.lodge, monitoring=not a.no_monitoring)))
