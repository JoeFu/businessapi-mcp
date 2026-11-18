"""
Sandbox self-test — what `businessapi-mcp doctor` runs.

    businessapi-mcp doctor                 read-only calls + local dry-run validation
    businessapi-mcp doctor --lodge         ALSO lodge the example payloads in the SANDBOX and poll them
    businessapi-mcp doctor --no-monitoring skip the entity-monitoring steps

Needs a TEST key (bapi_sk_test_…). Live keys are refused: the doctor only ever talks to
https://businessapi.com.au/api/test/v2, where every call is free and nothing reaches ASIC/ATO production.

It drives the real MCP tools in-process (fastmcp Client -> server.mcp), so a green run proves the whole chain:
key discovery -> tool schemas -> Pydantic validation -> HTTP -> vendor response envelope -> status polling.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import date
from pathlib import Path

from .config import SETUP_HINT, load_settings

EXAMPLES = Path(__file__).resolve().parent / "examples"
TODAY = date.today().isoformat()


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def record(self, status: str, step: str, detail: str = "") -> None:
        self.rows.append((status, step, detail))
        print(f"[{status:<4}] {step}" + (f"  -  {detail}" if detail else ""))

    def finish(self) -> int:
        counts = {s: sum(1 for st, _, _ in self.rows if st == s) for s in ("PASS", "FAIL", "SKIP", "WARN", "INFO")}
        print("\n" + " ".join(f"{k}={v}" for k, v in counts.items() if v))
        if counts["FAIL"]:
            print("Failures:")
            for st, step, detail in self.rows:
                if st == "FAIL":
                    print(f"  - {step}: {detail}")
        return 1 if counts["FAIL"] else 0


def _body(result) -> dict:
    try:
        return json.loads(result.content[0].text)
    except Exception:
        return {"raw": result.content[0].text if result.content else ""}


def _summary(d: dict, *keys: str) -> str:
    picked = {k: d.get(k) for k in keys if k in d}
    if d.get("errors"):
        picked["errors"] = d["errors"]
    return json.dumps(picked, ensure_ascii=False)[:220]


def load_example(name: str) -> dict:
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


def run(lodge: bool = False, monitoring: bool = True) -> int:
    settings = load_settings()
    if not settings.has_key:
        print("No API key configured. " + SETUP_HINT)
        return 2
    if settings.env != "test":
        print("The doctor only runs against the sandbox with a TEST key (bapi_sk_test_…). Refusing a live key.")
        return 2
    os.environ["BAPI_TOOLSETS"] = "all"  # the self-test needs every toolset, whatever the user configured
    from . import server
    from fastmcp import Client

    report = Report()
    record = report.record

    async def call(c: Client, name: str, args: dict | None = None):
        r = await c.call_tool(name, args or {}, raise_on_error=False)
        return r, _body(r)

    async def step_http(c: Client, step: str, tool: str, args: dict, ok_statuses=(200, 201), show=()) -> dict:
        r, d = await call(c, tool, args)
        if r.is_error:
            record("FAIL", step, r.content[0].text[:200])
            return d
        status = d.get("httpStatus")
        if status in ok_statuses:
            errs = d.get("errors")
            if errs and status < 400:  # the API sometimes answers 200 with an errors object (e.g. a missing query parameter)
                record("FAIL", step, f"HTTP {status} but errors present: {json.dumps(errs, ensure_ascii=False)[:160]}")
                return d
            record("PASS", step, _summary(d, *show))
            return d
        if status == 401:
            record("FAIL", step, "401 — the API key was rejected (wrong key, or a live key on the sandbox)")
            return d
        if status == 403 and str(d.get("error", "")) in ("monitoring_unavailable", "sandbox_only", "card_billing_required"):
            record("SKIP", step, f"{status} {d.get('error')}: {str(d.get('message', ''))[:120]}")
            return d
        record("FAIL", step, f"HTTP {status}: {_summary(d, 'error', 'message')}")
        return d

    async def main() -> int:
        print(f"businessapi-mcp doctor - {TODAY}\n{settings.describe()}\nlodge: {lodge}  monitoring: {monitoring}\n")
        async with Client(server.mcp) as c:
            tools = {t.name for t in await c.list_tools()}
            record("PASS", "tools/list", f"{len(tools)} tools registered")

            r, d = await call(c, "get_request_schema", {"form": "company_registration"})
            record("FAIL" if r.is_error else "PASS", "get_request_schema(company_registration)", f"{len(json.dumps(d))} chars")

            company = load_example("company_registration.json")
            company["applicant"]["dateSigned"] = TODAY
            abn = load_example("abn_registration_sole_trader.json")
            abn["businessActivity"]["abnStartDate"] = TODAY
            abn["declaration"]["date"] = TODAY
            bn = load_example("business_name_registration_ib.json")
            for label, tool, payload in (
                ("dry-run lodge_company_registration (local validation)", "lodge_company_registration", company),
                ("dry-run lodge_abn_registration (local validation)", "lodge_abn_registration", abn),
                ("dry-run lodge_business_name_registration (local validation)", "lodge_business_name_registration", bn),
            ):
                r, d = await call(c, tool, {"request": payload, "dry_run": True})
                ok = not r.is_error and d.get("dryRun")
                record("PASS" if ok else "FAIL", label, d.get("wouldPost", "") if ok else r.content[0].text[:160])

            acct = await step_http(c, "get_account_status", "get_account_status", {}, show=("planId", "planEndsAt"))
            if acct.get("httpStatus") == 401:
                print("\nStopping: the sandbox rejected the key, so nothing else can be tested.")
                return report.finish()
            await step_http(c, "lookup_abn 51 824 753 556", "lookup_abn", {"abn": "51 824 753 556"}, show=("EntityName", "AbnStatus", "EntityTypeName"))
            await step_http(c, "lookup_acn 004 085 616", "lookup_acn", {"acn": "004 085 616"}, show=("name", "status", "type"))
            await step_http(c, "check_company_name_availability", "check_company_name_availability", {"name": "BAPI SMOKE TEST PTY LTD"}, show=("availability",))
            await step_http(c, "check_business_name_availability (+holder)", "check_business_name_availability",
                            {"name": "BAPI SMOKE TRADING CO", "holder_name": "SAMPLE COMPANY PTY LTD"}, show=("availability", "availableForHolder", "asicUnavailable"))
            await step_http(c, "search_business_industries('cleaning')", "search_business_industries", {"search_term": "cleaning"}, show=("activitiesCount",))
            await step_http(c, "get_abn_entitlement_questionnaire(117/006)", "get_abn_entitlement_questionnaire", {"entity_type": "117", "entity_sub_type": "006"})

            ppsr = await step_http(c, "create_ppsr_search (abn)", "create_ppsr_search", {"abn": "51824753556"}, show=("requestId", "status"))
            rid = ppsr.get("requestId") or (ppsr.get("response") or {}).get("requestId")
            if rid:
                r, d = await call(c, "wait_for_request", {"request_id": rid, "kind": "ppsr", "timeout_seconds": 90, "poll_seconds": 5})
                st = str(d.get("status", "")).lower()
                record("PASS" if st == "completed" else ("WARN" if d.get("timedOut") else "FAIL"),
                       f"wait_for_request ppsr #{rid}", f"status={d.get('status')} polls={d.get('polls')} resultCount={((d.get('data') or {}).get('search') or {}).get('resultCount')}")

            if monitoring:
                order = await step_http(c, "start_entity_monitoring (dry_run=false, sandbox $0)", "start_entity_monitoring",
                                        {"entities": [{"acn": "004085616", "reference": "doctor"}], "idempotency_key": "doctor-004085616", "dry_run": False},
                                        ok_statuses=(200, 201, 202, 422), show=("unitPrice", "total", "error"))
                if order.get("httpStatus") == 422:
                    report.rows[-1] = ("PASS", report.rows[-1][1], "422 already_monitored — expected on a re-run")
                listed = await step_http(c, "list_monitored_entities(acn)", "list_monitored_entities", {"acn": "004085616", "per_page": 5}, show=("meta",))
                ents = listed.get("data") or []
                if ents:
                    eid = ents[0]["id"]
                    await step_http(c, f"get_monitored_entity #{eid}", "get_monitored_entity", {"entity_id": eid}, show=("status", "monitoringStatus", "expiresAt"))
                    await step_http(c, f"simulate_monitoring_event #{eid} document_lodged", "simulate_monitoring_event",
                                    {"entity_id": eid, "event_type": "document_lodged"}, ok_statuses=(200, 201), show=("eventType", "title", "simulated"))
                    await step_http(c, f"list_monitoring_events(entity {eid})", "list_monitoring_events", {"entity_id": eid, "per_page": 5}, show=("meta",))
                    await step_http(c, f"get_entity_snapshot #{eid} (409 expected in sandbox)", "get_entity_snapshot", {"entity_id": eid}, ok_statuses=(200, 409), show=("error",))

            await step_http(c, "list_webhooks", "list_webhooks", {}, show=("data",))

            if lodge:
                print("\n--lodge: submitting the example payloads to the SANDBOX (ASIC/ATO test services, $0)")
                res = await step_http(c, "lodge_company_registration (sandbox)", "lodge_company_registration", {"request": company, "dry_run": False}, show=("requestId", "status", "billing"))
                rid = res.get("requestId") or (res.get("response") or {}).get("requestId")
                if rid:
                    r, d = await call(c, "wait_for_request", {"request_id": rid, "kind": "lodgement", "timeout_seconds": 240, "poll_seconds": 10})
                    record("INFO", f"company registration #{rid} after polling", f"status={d.get('status')} documentNumber={d.get('documentNumber')} response={json.dumps(d.get('response'))[:160]}")
                res = await step_http(c, "lodge_abn_registration (sandbox)", "lodge_abn_registration", {"request": abn, "dry_run": False}, show=("requestId", "status", "billing", "warnings"))
                rid = res.get("requestId") or (res.get("response") or {}).get("requestId")
                if rid:
                    r, d = await call(c, "wait_for_request", {"request_id": rid, "kind": "lodgement", "timeout_seconds": 180, "poll_seconds": 10})
                    record("INFO", f"ABN registration #{rid} after polling", f"status={d.get('status')} info={json.dumps(d.get('info'))[:160]} errors={json.dumps(d.get('errors'))[:160]}")
                res = await step_http(c, "lodge_business_name_registration (sandbox)", "lodge_business_name_registration", {"request": bn, "dry_run": False}, show=("requestId", "status"))
                rid = res.get("requestId") or (res.get("response") or {}).get("requestId")
                if rid:
                    r, d = await call(c, "wait_for_request", {"request_id": rid, "kind": "business_name", "timeout_seconds": 180, "poll_seconds": 10})
                    record("INFO", f"business name #{rid} after polling", f"status={d.get('status')} response={json.dumps(d.get('response'))[:200]}")
        return report.finish()

    return asyncio.run(main())


if __name__ == "__main__":  # python -m businessapi_mcp.smoke [--lodge] [--no-monitoring]
    import sys

    raise SystemExit(run(lodge="--lodge" in sys.argv, monitoring="--no-monitoring" not in sys.argv))
