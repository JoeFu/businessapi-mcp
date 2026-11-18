"""The MCP surface: tool registration modes, schema delivery, dry runs, error formatting, live guard. No network."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time

import pytest
from fastmcp import Client


def run(coro):
    return asyncio.run(coro)


async def _tools(server):
    async with Client(server.mcp) as c:
        return {t.name: t for t in await c.list_tools()}


def test_lazy_mode_is_default(load_server):
    server = load_server()
    tools = run(_tools(server))
    assert len(tools) == 46
    schema = tools["lodge_company_registration"].input_schema
    assert schema["properties"]["request"] == {"type": "object", "additionalProperties": True}
    assert "get_request_schema" in tools
    assert tools["lookup_abn"].meta == {"fastmcp": {"tags": ["lookup"]}}


def test_inline_mode_exposes_full_schema(load_server):
    server = load_server(BAPI_INLINE_SCHEMAS="true")
    tools = run(_tools(server))
    props = tools["lodge_company_registration"].input_schema["properties"]["request"]["properties"]
    assert {"generalInformation", "registeredOffice", "officeHolders", "applicant"} <= set(props)


def test_toolsets_filter(load_server):
    server = load_server(BAPI_TOOLSETS="monitoring,webhooks")
    tools = run(_tools(server))
    assert len(tools) == 16
    assert "call_endpoint" not in tools and "get_request_schema" not in tools
    assert "start_entity_monitoring" in tools and "verify_webhook_signature" in tools


def test_unknown_toolset_fails_fast(load_server):
    with pytest.raises(SystemExit):
        load_server(BAPI_TOOLSETS="bogus")


def test_environment_inferred_from_key_prefix(load_server):
    assert load_server(BAPI_API_KEY="bapi_sk_live_dummy").ENV == "live"
    assert load_server(BAPI_API_KEY="bapi_sk_test_dummy").ENV == "test"


def test_resources_and_schema_resource(load_server):
    server = load_server()

    async def go():
        async with Client(server.mcp) as c:
            uris = {str(r.uri) for r in await c.list_resources()}
            assert len(uris) == 12 and "bapi://docs/overview" in uris
            text = (await c.read_resource("bapi://schemas/business_name_renewal"))[0].text
            assert "generalInformation" in json.loads(text)["properties"]
            r = await c.call_tool("get_request_schema", {"form": "asic_362_form"})
            assert json.loads(r.content[0].text)["tool"] == "lodge_asic_362_form"

    run(go())


def test_dry_run_returns_payload_without_network(load_server, samples):
    server = load_server()

    async def go():
        async with Client(server.mcp) as c:
            r = await c.call_tool("lodge_business_name_renewal", {"request": samples["business_name_renewal"]})
            d = json.loads(r.content[0].text)
            assert d["dryRun"] is True and d["wouldPost"] == "/api/test/v2/business-name-renewals"
            assert d["payload"]["testTransmissionIndicator"] is True

    run(go())


def test_lazy_validation_errors_are_listed(load_server):
    server = load_server()
    bad = {"generalInformation": {"businessName": "X", "abn": "123", "term": 2, "email": "a@b.c"},
           "applicant": {"individual": {"familyName": "D"}, "declaresTrueAndCorrectIndicator": False}}

    async def go():
        async with Client(server.mcp) as c:
            r = await c.call_tool("lodge_business_name_renewal", {"request": bad}, raise_on_error=False)
            assert r.is_error
            text = r.content[0].text
            assert "4 validation error(s)" in text and "generalInformation.abn" in text and "get_request_schema" in text

    run(go())


def test_live_writes_are_refused_before_any_http(load_server):
    server = load_server(BAPI_API_KEY="bapi_sk_live_dummy")

    async def go():
        async with Client(server.mcp) as c:
            r = await c.call_tool("stop_entity_monitoring", {"entity_id": 1}, raise_on_error=False)
            assert r.is_error and "Refused: this is a LIVE write" in r.content[0].text
            r = await c.call_tool("simulate_monitoring_event", {"entity_id": 1, "event_type": "document_lodged"}, raise_on_error=False)
            assert r.is_error and "sandbox-only" in r.content[0].text

    run(go())


def test_missing_key_is_explained(load_server):
    server = load_server(BAPI_API_KEY="")
    assert "NO API KEY CONFIGURED" in server.mcp.instructions

    async def go():
        async with Client(server.mcp) as c:
            r = await c.call_tool("lookup_abn", {"abn": "51824753556"}, raise_on_error=False)
            assert r.is_error and "businessapi-mcp setup" in r.content[0].text

    run(go())


def test_verify_webhook_signature(load_server):
    server = load_server()
    secret, body, t = "whsec_test", '{"event":"x"}', str(int(time.time()))
    good = f"t={t},v1=" + hmac.new(secret.encode(), f"{t}.{body}".encode(), hashlib.sha256).hexdigest()
    assert server.verify_webhook_signature(secret, good, body)["valid"] is True
    assert server.verify_webhook_signature(secret, good, body + " ")["reason"].startswith("signature mismatch")
    old_t = str(int(time.time()) - 900)
    stale = f"t={old_t},v1=" + hmac.new(secret.encode(), f"{old_t}.{body}".encode(), hashlib.sha256).hexdigest()
    assert "replay" in server.verify_webhook_signature(secret, stale, body)["reason"]
