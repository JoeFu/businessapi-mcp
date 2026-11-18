"""
Merge the Pydantic request models into openapi.yaml so route B (server_from_openapi.py) gets the same typed
lodgement endpoints as the hand-written server.

    uv run --group dev scripts/export_openapi.py

Re-run after editing models.py. Hand-written parts of openapi.yaml are preserved; only components.schemas entries
and the paths listed in LODGEMENTS are (re)generated.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from businessapi_mcp.models import (
    AbnRegistrationRequest,
    Asic361FormRequest,
    Asic362FormRequest,
    Asic484FormRequest,
    AsicRa52Request,
    BusinessNameRegistrationRequest,
    BusinessNameRenewalRequest,
    CompanyDeregistrationRequest,
    CompanyNameChangeRequest,
    CompanyRegistrationRequest,
    MonitoredEntityInput,
)

SPEC = Path(__file__).resolve().parent.parent / "openapi.yaml"

LODGEMENTS = [
    # path, operationId, model, summary
    (
        "/company-registrations", "lodge_company_registration", CompanyRegistrationRequest,
        "Lodge ASIC Form 201 (new company). LIVE ≈ $50 + ASIC fee. Returns {response: {requestId}}; poll /status/{requestId}.",
    ),
    (
        "/abn-registrations", "lodge_abn_registration", AbnRegistrationRequest,
        "Apply for an ABN (ATO). LIVE ≈ $25. Returns {requestId, status: received, billing}; poll /status/{requestId}. "
        "Add testTransmissionIndicator=false only for a real lodgement.",
    ),
    (
        "/business-name-registrations", "lodge_business_name_registration", BusinessNameRegistrationRequest,
        "Register a business name (ASIC). LIVE ≈ $15 + ASIC fee. Returns {requestId, status: new}; "
        "poll /business-name-registrations/{requestId}/status for the payment link / certificate.",
    ),
    (
        "/business-name-renewals", "lodge_business_name_renewal", BusinessNameRenewalRequest,
        "Renew a business name (ASIC) for 1 or 3 years. LIVE ≈ $15 + ASIC fee. Returns {requestId, status: new}; "
        "poll /business-name-renewals/{requestId}/status.",
    ),
    (
        "/asic-484-forms", "lodge_asic_484_form", Asic484FormRequest,
        "Lodge ASIC Form 484 (changes to company details: addresses, officeholders, shares, UHC). Strings upper-case. "
        "Returns {response: {requestId, documentNumber}}; poll /asic-484-forms/{requestId}/status.",
    ),
    (
        "/company-name-changes", "lodge_company_name_change", CompanyNameChangeRequest,
        "Lodge ASIC Form 205A (change of company name). Returns {requestId, status: sent}; poll /company-name-changes/{requestId}/status; "
        "certificate PDF at /company-name-changes/{requestId}/certificate.",
    ),
    (
        "/company-deregistrations", "lodge_company_deregistration", CompanyDeregistrationRequest,
        "Lodge ASIC Form 6010 (voluntary deregistration; all six s.601AA declarations must be true). Returns {response: {requestId}}; "
        "poll /company-deregistrations/{requestId}/status.",
    ),
    (
        "/asic-361-forms", "lodge_asic_361_form", Asic361FormRequest,
        "Lodge ASIC Form 361 (registered agent ceases to act for a company). Returns {response: {requestId, documentNumber}}.",
    ),
    (
        "/asic-362-forms", "lodge_asic_362_form", Asic362FormRequest,
        "Lodge ASIC Form 362 (company appoints (A) or ceases (C) a registered agent). Returns {response: {requestId, documentNumber}}.",
    ),
    (
        "/asic-ra52-forms", "request_asic_account_transactions", AsicRa52Request,
        "Request ASIC Form RA52 (agent account transactions report for a period). Finished status carries records[].",
    ),
]

WEBHOOK_EVENT_TYPES = [
    "*", "company_registration.status_changed", "abn_registration.status_changed", "business_name_registration.status_changed",
    "business_name_renewal.status_changed", "ppsr.alert.grantor_registration_changed", "monitoring.change_detected",
    "monitoring.expiring", "monitoring.expired",
]

WEBHOOK_PATHS: dict[str, dict[str, Any]] = {
    "/webhooks": {
        "get": {"operationId": "list_webhooks", "summary": "List webhook endpoints for this API key (secrets omitted).",
                "responses": {"200": {"description": "endpoints"}}},
        "post": {
            "operationId": "create_webhook",
            "summary": "Register a webhook endpoint (HTTPS, public host; ≤10 per key). The signing secret is returned ONCE.",
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["url", "eventTypes"],
                "properties": {"url": {"type": "string", "format": "uri"},
                               "eventTypes": {"type": "array", "minItems": 1, "items": {"type": "string", "enum": WEBHOOK_EVENT_TYPES}}},
            }}}},
            "responses": {"201": {"description": "endpoint incl. secret (whsec_…)"}},
        },
    },
    "/webhooks/{id}": {
        "patch": {
            "operationId": "update_webhook", "summary": "Update URL, event types or enabled flag.",
            "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}],
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object",
                "properties": {"url": {"type": "string", "format": "uri"},
                               "eventTypes": {"type": "array", "minItems": 1, "items": {"type": "string", "enum": WEBHOOK_EVENT_TYPES}},
                               "enabled": {"type": "boolean"}},
            }}}},
            "responses": {"200": {"description": "endpoint"}},
        },
        "delete": {"operationId": "delete_webhook", "summary": "Remove a webhook endpoint.",
                   "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}],
                   "responses": {"200": {"description": "deleted"}}},
    },
    "/webhooks/{id}/rotate-secret": {
        "post": {"operationId": "rotate_webhook_secret", "summary": "Invalidate the signing secret and issue a new one (returned once).",
                 "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}],
                 "responses": {"200": {"description": "endpoint incl. new secret"}}},
    },
}

STATUS_PATHS = {
    "/business-name-registrations/{requestId}/status": (
        "get_business_name_registration_status",
        "Business-name status: 'wating ASIC CC payment' carries response.transactionPaymentUrl; 'cancelled' -> requestNewPaymentLink.",
    ),
    "/business-name-registrations/{requestId}/requestNewPaymentLink": (
        "refresh_business_name_payment_link",
        "Re-issue the ASIC card payment link for a cancelled/expired business-name payment (same requestId).",
    ),
    "/business-name-renewals/{requestId}/status": (
        "get_business_name_renewal_status",
        "Renewal status; finished -> response {transactionDocumentUrl, businessNameNumber, renewalTo}.",
    ),
    "/asic-484-forms/{requestId}/status": (
        "get_asic_484_form_status",
        "Form 484 status: {response: {requestId, status, documentNumber, info}}.",
    ),
    "/company-name-changes/{requestId}/status": (
        "get_company_name_change_status",
        "Name change status: {requestId, status, documentNumber, response {companyName, acn, proposedName}}.",
    ),
    "/company-deregistrations/{requestId}/status": (
        "get_company_deregistration_status",
        "Deregistration status: {response: {status, documentNumber, info {…, dateOfDeregistration}}}.",
    ),
    "/asic-361-forms/{requestId}/status": ("get_asic_361_form_status", "Form 361 status (nested under response)."),
    "/asic-362-forms/{requestId}/status": ("get_asic_362_form_status", "Form 362 status (nested under response)."),
    "/asic-ra52-forms/{requestId}/status": ("get_asic_ra52_form_status", "Form RA52 status; finished carries response.records[]."),
}

MONITORING_PATHS: dict[str, dict[str, Any]] = {
    "/monitoring/entities": {
        "post": {
            "operationId": "start_entity_monitoring",
            "summary": "Start monitoring up to 25 entities for 12 months ($18 each live). Send an Idempotency-Key header.",
            "parameters": [{"name": "Idempotency-Key", "in": "header", "required": False, "schema": {"type": "string", "maxLength": 255}}],
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["entities"],
                "properties": {"entities": {"type": "array", "minItems": 1, "maxItems": 25,
                                            "items": {"$ref": "#/components/schemas/MonitoredEntityInput"}}},
            }}}},
            "responses": {"201": {"description": "{unitPrice, total, entities[]}"}, "422": {"description": "validation_failed with per-row errors"}},
        },
        "get": {
            "operationId": "list_monitored_entities",
            "summary": "List monitored entities. Filters: status, acn, abn, expiresBefore, perPage (≤100), page.",
            "parameters": [
                {"name": "status", "in": "query", "schema": {"type": "string", "enum": ["processing", "active", "stopped", "expired"]}},
                {"name": "acn", "in": "query", "schema": {"type": "string"}},
                {"name": "abn", "in": "query", "schema": {"type": "string"}},
                {"name": "expiresBefore", "in": "query", "schema": {"type": "string", "format": "date"}},
                {"name": "perPage", "in": "query", "schema": {"type": "integer", "maximum": 100}},
                {"name": "page", "in": "query", "schema": {"type": "integer"}},
            ],
            "responses": {"200": {"description": "data[] + meta"}},
        },
    },
    "/monitoring/entities/renew": {
        "post": {
            "operationId": "renew_entity_monitoring",
            "summary": "Renew up to 25 entities for another 12 months ($18 each live). Idempotency-Key header REQUIRED.",
            "parameters": [{"name": "Idempotency-Key", "in": "header", "required": True, "schema": {"type": "string", "maxLength": 255}}],
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["ids"],
                "properties": {"ids": {"type": "array", "minItems": 1, "maxItems": 25, "items": {"type": "integer"}}},
            }}}},
            "responses": {"201": {"description": "{unitPrice, total, entities[]}"}},
        },
    },
    "/monitoring/entities/{id}": {
        "get": {"operationId": "get_monitored_entity", "summary": "One monitored entity.",
                "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}],
                "responses": {"200": {"description": "entity object"}}},
        "delete": {"operationId": "stop_entity_monitoring", "summary": "Stop monitoring now (no refund; resumable while the term runs).",
                   "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}],
                   "responses": {"200": {"description": "entity with status stopped"}}},
    },
    "/monitoring/entities/{id}/resume": {
        "post": {"operationId": "resume_entity_monitoring", "summary": "Resume a stopped entity whose paid term is still running (free).",
                 "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}],
                 "responses": {"200": {"description": "entity"}}},
    },
    "/monitoring/entities/{id}/snapshot": {
        "get": {"operationId": "get_entity_snapshot", "summary": "Current record from every monitored source (JSON). Active entities only.",
                "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}],
                "responses": {"200": {"description": "{entity, snapshot}"}}},
    },
    "/monitoring/entities/{id}/events": {
        "get": {"operationId": "list_entity_monitoring_events", "summary": "That entity's events, newest first.",
                "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}},
                               {"name": "perPage", "in": "query", "schema": {"type": "integer", "maximum": 100}},
                               {"name": "page", "in": "query", "schema": {"type": "integer"}}],
                "responses": {"200": {"description": "data[] + meta"}}},
    },
    "/monitoring/events": {
        "get": {"operationId": "list_monitoring_events", "summary": "All events oldest first; since = last monitoringEventId or ISO date-time.",
                "parameters": [{"name": "since", "in": "query", "schema": {"type": "string"}},
                               {"name": "perPage", "in": "query", "schema": {"type": "integer", "maximum": 100}},
                               {"name": "page", "in": "query", "schema": {"type": "integer"}}],
                "responses": {"200": {"description": "data[] + meta"}}},
    },
    "/monitoring/events/{id}": {
        "get": {"operationId": "get_monitoring_event", "summary": "One event in full (description, source, effectiveDate, metadata, entity).",
                "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}],
                "responses": {"200": {"description": "event"}}},
    },
}


HELPER_PATHS: dict[str, dict[str, Any]] = {
    "/helpers/business-industries": {
        "get": {
            "operationId": "search_business_industries",
            "summary": "Search ATO main-business-activity codes by keyword; returns activities[] {score, code, industryCode, fullName}.",
            "parameters": [{"name": "searchTerm", "in": "query", "required": True, "schema": {"type": "string"}}],
            "responses": {"200": {"description": "{searchTerm, activitiesCount, activities[]}"}},
        }
    },
    "/helpers/entitlement-questionnaire": {
        "get": {
            "operationId": "get_abn_entitlement_questionnaire",
            "summary": "ABN entitlement questionnaire for an ATO entity type / sub-type (answers go into entitlement.questionnaire).",
            "parameters": [
                {"name": "entityType", "in": "query", "required": True, "schema": {"type": "string"}},
                {"name": "entitySubType", "in": "query", "required": True, "schema": {"type": "string"}},
            ],
            "responses": {"200": {"description": "questionnaire"}},
        }
    },
}


def main() -> None:
    spec = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    schemas = spec.setdefault("components", {}).setdefault("schemas", {})
    paths = spec.setdefault("paths", {})

    for path, op_id, model, summary in LODGEMENTS:
        js = model.model_json_schema(ref_template="#/components/schemas/{model}")
        for name, sub in js.pop("$defs", {}).items():
            schemas[name] = sub
        schemas[model.__name__] = js
        paths.setdefault(path, {})["post"] = {
            "operationId": op_id,
            "summary": summary,
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{model.__name__}"}}},
            },
            "responses": {"201": {"description": "Lodged; requestId for status polling"}, "400": {"description": "Validation failed; nothing sent, nothing charged"}},
        }

    for path, (op_id, summary) in STATUS_PATHS.items():
        paths[path] = {
            "get": {
                "operationId": op_id,
                "summary": summary,
                "parameters": [{"name": "requestId", "in": "path", "required": True, "schema": {"type": "integer"}}],
                "responses": {"200": {"description": "Status"}},
            }
        }

    js = MonitoredEntityInput.model_json_schema(ref_template="#/components/schemas/{model}")
    for name, sub in js.pop("$defs", {}).items():
        schemas[name] = sub
    schemas["MonitoredEntityInput"] = js
    for path, ops in MONITORING_PATHS.items():
        paths[path] = ops
    for path, ops in WEBHOOK_PATHS.items():
        paths[path] = ops
    for path, ops in HELPER_PATHS.items():  # verified against the sandbox: these helpers need query parameters
        paths[path] = ops

    header = (
        "# Business API (businessapi.com.au) v2 — OpenAPI 3.1 for route B (server_from_openapi.py).\n"
        "# The vendor publishes no machine-readable spec; this file is hand-maintained EXCEPT the three lodgement\n"
        "# request schemas and their paths, which export_openapi.py regenerates from models.py (comments are not\n"
        "# preserved by the YAML dumper, so keep notes here in the header).\n"
        "# Paths are relative to the v2 root; the HTTP client supplies /api/v2 or /api/test/v2 from BAPI_ENV.\n"
        "# Covered: helpers, lookup, PPSR, status, company/ABN/business-name registration, business-name renewal, Forms 484/205A/\n"
        "# 6010/361/362/RA52, entity monitoring, webhooks. Not covered (no public docs page): name reservations, extracts,\n"
        "# tax/registrations, asic-370/492/902/ra17/ra50/ra51/ra61/ra63/ra65/ra67/ra71-forms, asic-inbox.\n"
    )
    SPEC.write_text(header + yaml.safe_dump(spec, sort_keys=False, allow_unicode=True, width=120), encoding="utf-8")
    print(f"openapi.yaml: {len(paths)} paths, {len(schemas)} schemas")


if __name__ == "__main__":
    main()
