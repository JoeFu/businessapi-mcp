"""
Business API (businessapi.com.au) -> MCP server

Run:   uv run --with fastmcp server.py          (stdio transport, for Claude Code / Claude Desktop / Cursor)
Env:
  BAPI_API_KEY     bapi_sk_test_...  or  bapi_sk_live_...   (https://businessapi.com.au/user/api-keys)
  BAPI_ENV         test | live   default test; must match the key prefix
  BAPI_ALLOW_LIVE  true          required before ANY live POST/PATCH/DELETE: those lodge real
                                 forms with ASIC/ATO/PPSR and are billed (e.g. PPSR search $6,
                                 company registration $50 + government fee)

Design: the model "learns" the API from three places it can see over MCP:
  1. server `instructions`            -> one-paragraph orientation at connect time
  2. tool name + docstring + schema   -> how to call each operation, what it costs, what to do next;
                                         lodgement tools take typed Pydantic requests (models.py) whose
                                         schema mirrors the vendor docs field-for-field
  3. resources bapi://docs/*          -> endpoint catalogue, envelope/status lifecycle, code tables, per-endpoint rules
Everything else (long-tail endpoints) goes through the generic `call_endpoint` tool.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal

import httpx2 as httpx  # fastmcp >= 4 ships the httpx2 fork; the API is the same as httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ValidationError

try:  # optional: read bapi-mcp/.env so `uv run --with fastmcp server.py` finds the key without exporting it
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:  # pragma: no cover - python-dotenv ships with fastmcp, but stay importable without it
    pass

from models import (
    COMPANY_COMBINATIONS,
    SUBTYPES_BY_ENTITY_TYPE,
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
    WebhookEventType,
    allowed_positions,
    validate_webhook_url,
)

BASE_URL = "https://businessapi.com.au"
API_KEY = os.environ.get("BAPI_API_KEY", "").strip()
ENV = os.environ.get("BAPI_ENV", "test").strip().lower()
ALLOW_LIVE = os.environ.get("BAPI_ALLOW_LIVE", "").strip().lower() in ("1", "true", "yes")
PREFIX = "/api/v2" if ENV == "live" else "/api/test/v2"
TEST_TRANSMISSION = ENV != "live"  # ABN / business-name payload flag; sandbox always uses the test service

# Context-size controls -------------------------------------------------------------------
# BAPI_INLINE_SCHEMAS=true  -> lodge_* tools expose their full Pydantic request schema in tools/list (~120k chars).
# default (lazy)            -> lodge_* tools take `request` as a plain object; the model fetches the exact schema
#                              on demand with get_request_schema(form) / resource bapi://schemas/{form}.
# BAPI_TOOLSETS=a,b         -> expose only these groups, so one codebase can run as several small MCP servers.
INLINE_SCHEMAS = os.environ.get("BAPI_INLINE_SCHEMAS", "").strip().lower() in ("1", "true", "yes")
ALL_TOOLSETS = ("lookup", "registrations", "agent_forms", "monitoring", "webhooks", "ppsr", "core")
_toolsets_raw = os.environ.get("BAPI_TOOLSETS", "all").strip().lower()
ENABLED_TOOLSETS: set[str] = (
    set(ALL_TOOLSETS) if _toolsets_raw in ("", "all", "*") else {t.strip() for t in _toolsets_raw.split(",") if t.strip()}
)
_unknown_toolsets = ENABLED_TOOLSETS - set(ALL_TOOLSETS)
if _unknown_toolsets:
    raise SystemExit(f"BAPI_TOOLSETS has unknown toolset(s) {sorted(_unknown_toolsets)}; valid: {', '.join(ALL_TOOLSETS)}")

# --------------------------------------------------------------------------------------
# Docs the model can read (resources). Keep in sync with https://businessapi.com.au/developers/*
# --------------------------------------------------------------------------------------

OVERVIEW_DOC = f"""# Business API (AU) — how to use it
Business API is an Australian ASIC / ATO / ABRS / PPSR digital service provider. Everything is REST + JSON.

## Environments (this server is currently in **{ENV}**)
| env  | base URL                                   | key prefix      |
|------|--------------------------------------------|-----------------|
| test | https://businessapi.com.au/api/test/v2/... | bapi_sk_test_…  |
| live | https://businessapi.com.au/api/v2/...      | bapi_sk_live_…  |
Test = full simulated sandbox (ASIC test environment, ATO test service, PPSR Discovery), $0, no card.
Using a live key on a test URL (or vice versa) is an error.

## Auth
`Authorization: Bearer <key>`. `sk_` (secret) keys for everything; `pk_` (publishable) keys only for
Helper endpoints and need a whitelisted domain.
Rate limits: helper/search endpoints 1,200 req/min; lodgement/status endpoints 600 req/min.

## Request / response envelope
- POST (lodge) -> HTTP 201, body {{"response": {{"requestId": 12239}}, "errors": []}}
  (ABN / business-name endpoints return requestId top-level, plus a `billing` block {{price_cents, charged_cents}}).
- GET  (status) -> HTTP 200, body {{"requestId", "status", "documentNumber?", "response", "warnings", "errors"}}.
- 400 payload failed Business API validation (nothing sent, nothing charged; ABN: errors keyed by field path)
  · 401 bad/missing key · 403 missing scope / not your resource / wrong method · 404 unknown endpoint
  · 405 right path, wrong method.
- General errors: "errors": ["Unauthorised."]
  Lodgement errors: "errors": [{{"details": "V418 Country name not verified…", "target": "NEWZEALAND"}}]

## Lodgement lifecycle (asynchronous!)
new -> transmission ok -> validation ok | validation failed -> manual review -> finished | rejected
POST returns immediately with a requestId. Poll GET /status/{{requestId}} (tool: get_request_status, or
wait_for_request to block until a terminal state) or register a webhook. On 'validation failed' / 'rejected'
read errors[]; on 'manual review' read warnings; on 'finished' read response.
PPSR searches: new -> processing -> completed | failed.
Business names (card payment): new -> … -> "wating ASIC CC payment" (payment link in response) -> finished;
"cancelled" = the payment link died, call refresh_business_name_payment_link, do NOT re-lodge.

## Money (live only)
Company registration ≈ $50 service + ASIC fee, ABN/tax registration ≈ $25, business name ≈ $15 + ASIC fee
($39/1yr, $92/3yr approx.), PPSR search $6, entity monitoring $18/entity/year.
Never call a live POST without the user explicitly confirming. The lodge_* tools default to dry_run=true.

## Webhooks
Manage via /webhooks (or the dashboard). Events: company_registration.status_changed,
abn_registration.status_changed, business_name_registration.status_changed,
business_name_renewal.status_changed, ppsr.alert.grantor_registration_changed,
monitoring.change_detected / .expiring / .expired.
Header X-Bapi-Signature: t=<ts>,v1=<hex> where v1 = HMAC-SHA256(secret, "<t>.<raw_body>").
Reply 2xx within 15 s; retries up to 6 attempts.

## Where the full field schemas live
https://businessapi.com.au/developers/api/<name>  e.g. company-registrations, abn-registrations,
business-name-registrations, asic-484-forms, ppsr-searches, entity-monitoring.
This server mirrors the first three as typed tools (lodge_company_registration, lodge_abn_registration,
lodge_business_name_registration); their input schemas ARE the field reference.
Code tables: resources bapi://docs/asic-codes and bapi://docs/ato-codes.
"""

ENDPOINTS_DOC = """# Business API v2 — endpoint catalogue
Path below is appended to https://businessapi.com.au/api/v2 (live) or https://businessapi.com.au/api/test/v2 (test).
POST,GET = POST lodges and returns requestId; GET /status/{requestId} (or the endpoint's own status path) reads status.
(typed tool) = this server has a dedicated tool; everything else via call_endpoint.

## Helpers (GET, free; pk_ or sk_ keys)
GET /helpers/company-name-availability?name=                  (typed tool)
GET /helpers/business-name-availability?name=
GET /helpers/entitlement-questionnaire                        ABN entitlement questions (answers go in entitlement.questionnaire)
GET /helpers/business-industries                              (typed tool)
GET /acn/helpers/payment-references

## Data validation & lookup (GET, sk_)
GET /lookup/abn/{abn}                                         (typed tool)
GET /lookup/acn/{acn}                                         (typed tool)

## Company collection (ASIC)
GET      /company-name-availability
POST,GET /company-registrations                  Form 201   (typed tool lodge_company_registration)
GET      /company-registrations/{requestId}/certificate      registration certificate PDF
GET      /company-registrations/{requestId}/documents        constitution / minutes / registers pack, one PDF
POST     /company-name-changes                   Form 205A  (typed tool lodge_company_name_change)
GET      /company-name-changes/{requestId}/status            (typed tool get_company_name_change_status)
GET      /company-name-changes/{requestId}/certificate       change-of-name certificate PDF
POST,GET /company-name-reservations              Form 410B  (no public docs page)
POST,GET /company-name-reservation-extensions    Form 410F  (no public docs page)
POST     /company-deregistrations                Form 6010  (typed tool lodge_company_deregistration)
GET      /company-deregistrations/{requestId}/status         (typed tool get_company_deregistration_status)
GET      /company-deregistrations/{requestId}/logs           transmission log
POST,GET /asic/extracts                          company extracts (no public docs page)

## ABN collection (ATO)
POST     /abn-registrations                      one-step lodgement   (typed tool lodge_abn_registration)
POST     /abn-registration-data                  staged: initiate (stores arbitrary JSON ≤15 KB)
GET      /abn-registration-data/{requestId}      staged: retrieve
POST     /abn-registration-data/{requestId}/update
GET      /abn-registration-data/{requestId}/lodge   staged: validate + lodge, returns a NEW requestId
POST,GET /tax/registrations                      TAXREG (GST/PAYG etc. for an existing ABN)

## Business name collection (ASIC)
GET      /business-name-availability?name=&holderName=       (typed tool check_business_name_availability)
POST     /business-name-registrations            BNREG      (typed tool lodge_business_name_registration)
GET      /business-name-registrations/{requestId}/status     (typed tool) payment link / certificate URL
GET      /business-name-registrations/{requestId}/requestNewPaymentLink   (typed tool)
POST     /business-name-renewals                 BNRENEW    (typed tool lodge_business_name_renewal)
GET      /business-name-renewals/{requestId}/status          (typed tool get_business_name_renewal_status)

## Registered agent collection (ASIC forms)
POST,GET /asic-361-forms   agent ceases to act               (typed tool lodge_asic_361_form; status via get_asic_form_status)
POST,GET /asic-362-forms   company appoints/ceases agent     (typed tool lodge_asic_362_form; status via get_asic_form_status)
POST,GET /asic-370-forms   resignation of officeholder       (no public docs page)
POST     /asic-484-forms   company details changes           (typed tool lodge_asic_484_form)
GET      /asic-484-forms/{requestId}/status                  (typed tool get_asic_484_form_status)
POST,GET /asic-492-forms   amendment to lodged document
POST,GET /asic-902-forms   supplementary document
POST,GET /asic-ra17-forms  data amendment report
POST,GET /asic-ra50-forms  withdraw pending Form 201
POST,GET /asic-ra51-forms  reprint registration certificate
POST,GET /asic-ra52-forms  ASIC account transactions         (typed tool request_asic_account_transactions; status via get_asic_form_status)
POST,GET /asic-ra61-forms  annual review date report
POST,GET /asic-ra63-forms  debt report (one company)
POST,GET /asic-ra65-forms  lodged documents report
POST,GET /asic-ra67-forms  debt report (many companies)
POST,GET /asic-ra71-forms  company details statement / raw data
GET      /asic-inbox                      inbound ASIC documents
GET      /asic-inbox/{id}/pdf             document PDF

## PPSR (docs: /developers/api/ppsr-searches) — $6 per search live
POST /ppsr/searches                                           (typed tool create_ppsr_search)
GET  /ppsr/searches/{requestId}                               (typed tool get_ppsr_search)
GET  /ppsr/searches/{requestId}/certificates/{certificateNumber}     (PDF, 202 while generating)

## Entity monitoring (docs: /developers/api/entity-monitoring, resource bapi://docs/entity-monitoring) — $18/entity/year live
POST       /monitoring/entities            order ≤25, Idempotency-Key   (typed tool start_entity_monitoring)
GET        /monitoring/entities            list, filters status/acn/abn/expiresBefore/perPage/page   (typed tool list_monitored_entities)
POST       /monitoring/entities/renew      ≤25 ids, Idempotency-Key REQUIRED   (typed tool renew_entity_monitoring)
GET        /monitoring/entities/{id}       (typed tool get_monitored_entity)
DELETE     /monitoring/entities/{id}       stop   (typed tool stop_entity_monitoring)
POST       /monitoring/entities/{id}/resume      (typed tool resume_entity_monitoring)
GET        /monitoring/entities/{id}/snapshot    (typed tool get_entity_snapshot)
GET        /monitoring/entities/{id}/report?includeRecent=true   (PDF, call outside MCP)
GET        /monitoring/entities/{id}/events      (typed tool list_monitoring_events with entity_id)
GET        /monitoring/events?since=             (typed tool list_monitoring_events)
GET        /monitoring/events/{id}               (typed tool get_monitoring_event)
POST       /monitoring/entities/{id}/simulate    TEST env only   (typed tool simulate_monitoring_event)
Monitoring rate limit: 60 requests/min per account; errors use {error, message, errors} not the shared envelope.

## General
GET /status/{requestId}                                       (typed tool get_request_status / wait_for_request)
GET /account-status                                           (typed tool)

## Webhooks (resource bapi://docs/webhooks)
GET    /webhooks                    (typed tool list_webhooks)
POST   /webhooks                    (typed tool create_webhook — secret returned once)
PATCH  /webhooks/{id}               (typed tool update_webhook)
DELETE /webhooks/{id}               (typed tool delete_webhook)
POST   /webhooks/{id}/rotate-secret (typed tool rotate_webhook_secret)
Signature check without network: verify_webhook_signature.
"""

PPSR_DOC = """# PPSR search request body (POST /ppsr/searches)
Send exactly ONE identifier set; the search type is inferred from the fields present.

| field              | type   | rule                                          | search type                  | certificate | NEVDIS |
|--------------------|--------|-----------------------------------------------|------------------------------|-------------|--------|
| acn                | string | 9 digits, checksum                            | grantor (organisation)       | yes         | no     |
| abn                | string | 11 digits, checksum                           | grantor (organisation)       | yes         | no     |
| arbn / arsn        | string | 9 digits                                      | grantor                      | yes         | no     |
| vin                | string | <=17 chars, ISO 3779                          | serial number + NEVDIS       | yes         | yes    |
| serialNumber       | string | alnum + hyphen, <=17; needs serialNumberType  | serial number + NEVDIS       | yes         | yes    |
| serialNumberType   | enum   | ChassisNumber / HIN / OfficialNumber          |                              |             |        |
| registrationNumber | string | digits, <=20                                  | registration lookup          | NO          | no     |
| familyName + givenNames + dateOfBirth (YYYY-MM-DD) + authorisedPurpose=true | individual grantor | yes | no |
| organisationName   | string | <=255                                         | organisation grantor by name | yes         | no     |

Create response: {"requestId": 5001, "status": "new"}
Poll GET /ppsr/searches/{requestId} until status is `completed` or `failed`.
Completed `data` has: entity, search{type, number, ppsrSearchNumber, resultCount, searchedAt},
registrations[] (registrationNumber, collateralType, collateralClass, serialNumber, grantorName,
securedPartyName, spgNumber, isPmsi, registrationStart/End…), nevdisData (VIN/serial searches only),
certificates[] {certificateNumber, status, downloadUrl}.
"""

COMPANY_DOC = """# Company registration (ASIC Form 201) — POST /company-registrations
Tool: lodge_company_registration(request, dry_run). Schema = https://businessapi.com.au/developers/api/company-registrations

## Workflow
1. check_company_name_availability(name) — the name must include legal elements ("… PTY LTD").
2. Collect: officeholders (name, DOB 18+, birth place, residential address, roles DIR/SEC), registered office
   (street address, who occupies it), principal place of business, share structure + who holds the shares,
   applicant (who lodges), optional ultimate holding company.
3. lodge_company_registration(request) with dry_run=true -> show the payload and fees to the user.
4. On explicit confirmation: dry_run=false -> requestId.
5. wait_for_request / get_request_status -> 'finished' gives acn, dateOfRegistration, documentNumber.
6. Certificate: GET /company-registrations/{requestId}/certificate (PDF); full document pack: …/documents.

## Typical Pty Ltd
generalInformation: companyType APTY, companyClass LMSH, companySubClass PROP, jurisdiction = state of registered office,
acnNameIndicator false, residentialOfficeholders true.  shareStructure: [{classCode ORD, classTitle ORDINARY, …}]
shareAllocation: one entry per holding (joint holders go in one entry's holdingOwners[]).

## Rules the validator enforces (from the vendor docs)
- Type/class/sub-class must be a valid ASIC combination (Table 0003), e.g. APTY only with PNPC/PSTC/PROP/HUNT.
- shareStructure + shareAllocation required unless LMGT (guarantee company), which needs nonShareMembers(+Amount) instead.
- shareStructure classCodes unique and alphabetical; every allocation's classCode must exist in shareStructure.
- registeredOffice: occupierName + occupierConsent when the company does not occupy; APUB needs openStandardHoursIndicator.
- Officeholder DOB 18+; personal names may not contain PTY, LTD, LIMITED, COMPANY, &, P/L.
- Applicant: individual OR organisation (+ signatory individual and role DIR/SEC/ALT/AGT); dateSigned ≤ today;
  declaresTrueAndCorrectIndicator true.
- Money fields (totalAmountPaid, amountPaidPerShare…) are integers whose last two digits are cents per the docs
  (100 = $1.00). The vendor's sample sends 100 for 100 x $1 shares, which contradicts that — verify in the sandbox.
- Addresses: street ≤52, locality ≤30, state from Table 0035, 4-digit postcode; country ISO alpha-2 (AU default).

## Status response (finished)
{"requestId", "status": "finished", "documentNumber", "response": {"companyName", "acn", "companyType",
 "companyClass", "companySubClass", "jurisdiction", "dateOfRegistration"}, "warnings", "errors"}
"""

ABN_DOC = """# ABN registration (ATO ABNREG) — POST /abn-registrations
Tool: lodge_abn_registration(request, dry_run). Schema = https://businessapi.com.au/developers/api/abn-registrations
Staged alternative (save partial form data, lodge later): /abn-registration-data … see bapi://docs/endpoints.

## Entity types -> sub-types (ATO tables)
117 individual/sole trader: 006
051 company: 027 private, 032 public, 045 co-operative, 145 other incorporated, 205 strata, 174 public trading trust,
    156 other unincorporated, 120 LIMITED PARTNERSHIP (yes, 051 not 161)
161 partnership: 087 family, 150 other
212 trust: 059 deceased estate, 039 cash mgmt, 071 discretionary investment, 072 discretionary services,
    077 discretionary trading, 089 fixed unit, 090 fixed, 112 hybrid, 184 public unit unlisted, 179 public unit listed
210 super fund: 196 SMSF, 221 APRA regulated, 138 non-regulated, 168 pooled super trust, 009 approved deposit fund
entitySubTypeAdditional: UPC unlisted public co · CLP corporate limited partnership · CHT closely held trust · LBG limited by guarantee

## Workflow
1. GET /helpers/entitlement-questionnaire (call_endpoint) -> walk the applicant through the questions; record
   {questionId, answerId, nextQuestionId} per step plus firstQuestion/outcomeId/outcome into `entitlement`.
2. GET /helpers/business-industries for businessActivity.code/description.
3. lodge_abn_registration(request, dry_run=true) -> review with the user (TFNs are sensitive: never echo them back).
4. dry_run=false -> {requestId, status: received, billing}. A duplicate in-progress application returns the existing
   requestId with a warning instead of lodging again.
5. wait_for_request -> finished: info.abn, abnStartDate, dateOfAbnRegistration, taxRegistrations dates.

## Rules the validator enforces (vendor + ATO rules)
- Sole trader (117): applicant.individual required, no associates, tfnApplicationIndicator false, needs applicant.tfn OR a
  residential address. The ATO matches identity on name + DOB + TFN; without a TFN an overseas address or non-resident
  status triggers a 43-day proof-of-identity hold.
- Everything else: applicant.organisation.name required; acn required for 027/032 (an ACN that already has an active ABN is
  rejected unless applicationReason.code is CBS or PUR); ≥1 associate (partnerships ≥2); governmentIndicator required.
- Associates: positionHeld must be allowed for the sub-type (see bapi://docs/ato-codes); one POF at most; TFNs unique;
  an individual needs tfn or street address; an organisation without tfn needs street + incorporationDate.
- Dates YYYY-MM-DD. abnStartDate after 1999-01-01 and ≤ 6 months ahead (not in the future if the applicant held an ABN
  before). Tax registration start dates ≥ abnStartDate.
- Addresses: street/line2 ≤38, locality ≤46; business address cannot be a PO box / GPO postcode 2001/3001/4001;
  postal address may be a PO box. countryCode ISO alpha-2 (AU).
- taxRegistrations keys: gst, ftc, lct, wet, paygw, fbt only. GST: turnoverRangeCode, lodgementFrequencyCode
  (Annually/HalfYearly/Monthly/Quarterly), reasonCode (Legislative Requirement/Voluntary).
- SMSF (196): superFundNoticeOfElection + superFundTrusteeDisclosure required; answer the trustee-type set.
- Booleans may be sent as true/false; the server sets testTransmissionIndicator from its environment.

## Errors
400 {"errors": {"applicationDetail.tfnIndicator": ["…required."]}} — nothing sent, nothing charged.
403 ABN registrations not enabled on the account. ATO rejections arrive via status, not HTTP errors.
"""

BUSINESS_NAME_DOC = """# Business name registration (ASIC BNREG) — POST /business-name-registrations
Tool: lodge_business_name_registration(request, dry_run). Schema = https://businessapi.com.au/developers/api/business-name-registrations

## Workflow
1. check_business_name_availability(name, holder_name=<legal name of the holder>) — lodge only when
   availableForHolder is true (a match on the holder's own company name is harmless; anything else means ASIC
   will reject with error 00133).
2. lodge_business_name_registration(request, dry_run=true) -> review; dry_run=false -> {requestId, status: new}.
3. get_business_name_registration_status(requestId) (or wait_for_request kind="business_name"):
   - paymentMethod "card": status "wating ASIC CC payment" with response.transactionPaymentUrl + transactionFee ->
     the payer opens the link; ASIC then registers instantly -> finished with certificateUrl + receiptNumber.
     If the link expires / is cancelled / card declined -> status "cancelled": call
     refresh_business_name_payment_link(requestId); do NOT lodge a new application (duplicate-name rejection risk).
   - paymentMethod "invoice": finished with transactionDocumentUrl; ASIC emails an invoice, pay within 10 days.

## Entity (holder) types
IND individual (entity.individual) · IB incorporated body = companies (entity.organisation.name + acn, subType PRV/PUB)
· PTSH partnership (entity.partners[]) · USTR unincorporated trust (entity.trustees[]) · JV joint venture (entity.associates[],
each with its own abn). entity.abn (the holder's ABN) is required for every type except JV. subType = the holder's
ABR entity-type code (PRV, PUB, FPT, PTR, DTT, DIT, FUT …), required for IB/PTSH/USTR.
Partners/trustees/associates: array of {individual: {...}} or {organisation: {name, acn}} — type inferred from the key.

## Fields
generalInformation: businessName, term (1 or 3 years), paymentMethod (card | invoice), email.
businessAddress: structured street address (unitOrOfficeNumber, streetNumber, streetName, streetType, locality, state, postCode).
serviceAddress: street address OR postal {postalType "PO BOX", postalNumber, locality, state, postCode}.
signatory: name {familyName, givenNames}, declaresTrueAndCorrectIndicator true.

## Renewal (POST /business-name-renewals, tool lodge_business_name_renewal)
generalInformation {businessName exactly as registered, abn of the holder, term 1|3, email} + applicant {individual {familyName,
givenNames}, declaresTrueAndCorrectIndicator true}. Response {requestId, status: new}. Status at
GET /business-name-renewals/{requestId}/status -> finished: response {transactionDocumentUrl, businessNameNumber, renewalTo}.
LIVE cost ≈ $15 service fee + ASIC renewal fee.
"""


ASIC484_DOC = """# ASIC Form 484 — Changes to company details — POST /asic-484-forms
Tool: lodge_asic_484_form(request, dry_run). Schema = https://businessapi.com.au/developers/api/asic-484-forms
Needs registered-agent onboarding (the company must be under your agent number). All string values are sent upper-case.
ASIC late fees apply when a change is notified more than 28 days after it took effect.

## Always required
generalInformation {companyName, acn}, signature {name {familyName, givenName1…}, role DIR|SEC|ALT, date (≤ today, ≥ 6 months ago),
declaresTrueAndCorrectIndicator true}.

## Change sections (include only what changed; several can be combined)
| change | sections |
|---|---|
| registered office address | registeredOffice {dateOfChange, address, occupyIndicator, occupierName/occupierConsent when not occupying} |
| principal place of business | principalPlaceOfBusiness {dateOfChange, address} |
| officeholders | officeHolders[] {status, dateOfChange, name, birthDetails, address, offices[] {office DIR|SEC|ALT, appointmentDate, cessationDate}} |
| share issue | shareStructure[] + shareIssue {earliestDateOfIssue, cash-only / written-contract indicators, shareClasses[]} + members[] |
| share cancellation | shareStructure[] + shareCancellation {earliestDateOfRegistrationOfCancellation, cancellationProvision, shareClasses[]} + members[] |
| ultimate holding company | ultimateHoldingCompany {dateOfChange, uhcStatus A|C|N, name, acn/abn, placeIncorporation} |
| concessional fees | concessionalFees {dateOfChange, special-purpose / home-unit / super-trustee / charitable indicators} |
| annexure | annexures[] {mark, type ALT, numberOfPages, text} |

## Codes
officeHolders.status (ASIC Table 0006): AP newly appointed · NO ceased · CA continuing, address change · CN continuing, name change ·
CX continuing, name and address change · AC newly appointed and ceased. (The vendor's 484 page glosses these differently; Table 0006
is ASIC's definition — confirm in the sandbox if a cessation is rejected.)
shareStructure.status (Table 0026): AP new class · DC details changed · NO whole class ceasing.
members.holdingStatus (Table 0028): AP new holding · CD change of details · CO continuing unchanged · NO ceased · TA/TR top-20 adjustments.
holdingOwners.memberStatus (Table 0027): AP new · CA address change · CN name change · CO unchanged · CX name+address · NO ceased.
cancellationProvision (Table 0021): S.254J-P, S.254J-I, S.256A-S.256E-S, S.256A-S.256E-M, SS.257H(3)-M, SS.257H(3)-O, S.258D, SS.258E(2), SS.258E(3), OTHER.
uhcStatus: A appointing · C ceasing · N notifying change.

## Rules the validator enforces
At least one change section · shareIssue/shareCancellation need shareStructure AND members · members.shareClass must exist in
shareStructure · ASIC V455: for every class not ceasing (status != NO) the members rows for that class must sum exactly to
numberIssued / totalAmountPaid / totalAmountUnpaid · dates not in the future · officeholders 18+ · Australian addresses need
state + postcode.

## Responses
POST -> {response: {requestId, documentNumber, documentUrl}, errors: []}
GET /asic-484-forms/{requestId}/status -> {response: {requestId, status, documentNumber, info {companyName, acn}}, errors}
"""


MONITORING_DOC = """# Entity monitoring — /monitoring/*
Watch companies (ACN) and ABN holders for public-register changes. Prepaid 12-month term, $18.00 per entity live (card billing
required; Pro/Enterprise volume discounts); $0 with test keys. Not an AML/CTF control; a notification that something changed —
check the register before acting. Monitoring a sole trader = handling personal information (Privacy Act).

## Tools
start_entity_monitoring(entities ≤25, idempotency_key) · renew_entity_monitoring(ids ≤25, idempotency_key) ·
list_monitored_entities(filters) · get_monitored_entity · stop_entity_monitoring (free, no refund, resumable) ·
resume_entity_monitoring (free while time left) · get_entity_snapshot · list_monitoring_events · get_monitoring_event ·
simulate_monitoring_event (sandbox only). Report PDF: GET /monitoring/entities/{id}/report (binary, outside MCP).

## What is checked
Daily: ASIC + ABR (deregistered/cancelled entities weekly). Events: newly lodged ASIC documents, name / address / status /
entity-type changes, GST registration changes, business names added/removed. Companies only: insolvency notices, PPSR
registrations. Every entity: IP Australia trade marks (matched on ABN/ACN, not name; renewal due 90/30/7 days), ASIC AFS /
credit licence registers and ACNC charity register (refreshed ~weekly). Existing holdings are baselined when added (no events).
Deregistration during the term sends status_change; the term is not refunded.

## Lifecycle
status: processing -> active -> stopped | expired. monitoringStatus: pending -> active (degraded = setup retrying; inactive when
stopped/expired). expiresAt is the first day NOT covered. Nothing auto-renews: monitoring.expiring webhooks fire 30 and 7 days
before expiry (also emailed for live), monitoring.expired at the end. Renew before expiry; renewing an active entity extends from
its current expiry; a stopped entity with time left must be resumed instead.

## Idempotency and charging
POST /monitoring/entities (recommended) and /monitoring/entities/renew (REQUIRED) take an Idempotency-Key header (≤255 chars):
the same key + same body never charges twice; a different body with the same key -> 409 idempotency_key_reused. One card charge per
batch: total = unitPrice × entities (cents). The whole request is validated before anything is charged; row errors come back as
422 validation_failed {errors: {"entities.0": [{code, message, entityId?}]}}. A 202 "processing" means payment may have gone
through — retry with the SAME key for the final answer.

## Limits
25 entities per request, 1,000 active live entities per account by default (1,000 sandbox entities). Global 60 requests/min per
account; registry lookups for live orders 60/min (Pro 600) -> 429 lookup_rate_limited with Retry-After. Snapshot/report 20/min.

## Error codes (JSON {error, message, errors?})
422 row: identifier_required, invalid_acn, invalid_abn, acn_not_found, acn_not_eligible, arbn_not_supported, abn_not_found,
abn_not_active, abn_acn_mismatch, duplicate_in_request, already_monitored (renew instead), stopped_with_time_remaining (resume instead),
renewal_in_progress, renewal_too_far_ahead · 422: entity_limit, not_resumable, test_entity_limit · 409: order_in_progress,
order_processing, entity_not_active, snapshot_not_ready, not_available_in_sandbox, request_in_progress, idempotency_key_reused ·
402: payment_method_required, payment_method_expired, payment_failed · 403: card_billing_required, monitoring_unavailable,
sandbox_only · 404 not_found · 400 idempotency_key_required · 503 registry_unavailable, payment_unavailable, snapshot_unavailable.

## Sandbox
Same validation, unitPrice 0, identifiers accepted on checksum, entities active immediately but never really monitored
(entityStatus stays unknown; snapshot/report -> 409 not_available_in_sandbox). Use simulate_monitoring_event to receive test
webhooks: {eventType: "document_lodged"} etc., or {simulate: expiring_30 | expiring_7 | expired | deregistered}.
"""


AGENT_FORMS_DOC = """# Company name change, deregistration and registered-agent forms
All of these need registered-agent onboarding: the company must be linked to your agent number (Form 362 type A does that).
Billed as registered-agent forms (Pro plan includes 2,500/month) plus ASIC fees where ASIC charges one. Status responses for
6010 / 361 / 362 / RA52 nest `status` under `response`; 205A returns it top-level.

## Form 205A — company name change — POST /company-name-changes (tool lodge_company_name_change)
generalInformation {companyName, acn, companyType, proposedName (with legal elements), acnNameIndicator, acnNameLegalElements?,
nameReservedIndicator?, nameIdenticalIndicator?, nameIdenticalAbn?, meetingDate, meetingResolutionText, paymentMethod DDEB|INV}
+ signatory {individual, role (Table 0011: DIR, SEC, AGT…), dateSigned, declaresTrueAndCorrectIndicator} + admin {manualReviewIndicator}.
POST -> {requestId, status: "sent"}. GET /company-name-changes/{requestId}/status -> {requestId, status, documentNumber,
response {companyName, acn, proposedName}}. Certificate PDF: GET /company-name-changes/{requestId}/certificate.
Check the new name first with check_company_name_availability; a name identical to a business name needs nameIdenticalAbn.

## Form 6010 — voluntary deregistration — POST /company-deregistrations (tool lodge_company_deregistration)
general {companyName, acn, paymentMethod DDEB|INV} + declarations (six s.601AA indicators, ALL true) + applicant {individual |
organisation (+ role), address?} + nominee {individual, address} + signatory {individual, role, dateSigned, declaration}.
POST -> {response: {requestId}}. GET /company-deregistrations/{requestId}/status -> {response: {status, documentNumber,
info {companyName, acn, companyType, companyClass, companySubClass, jurisdictionOfRegistration, dateOfDeregistration}}}.
Transmission log: GET /company-deregistrations/{requestId}/logs. Deregistration is final once ASIC processes it.

## Form 361 — agent ceases to act — POST /asic-361-forms (tool lodge_asic_361_form)
generalInformation {companyName, acn} + signature {name, role AGT, date, agentAuthorisedIndicator true, declaration true}.
## Form 362 — company appoints (A) / ceases (C) agent — POST /asic-362-forms (tool lodge_asic_362_form)
generalInformation {companyName, acn, type A|C} + signature {name, role DIR|SEC, date, declaration true}.
## Form RA52 — ASIC account transactions report — POST /asic-ra52-forms (tool request_asic_account_transactions)
general {startDate, endDate, includePriorOutstandingIndicator}. Finished status carries records[] {date, legend, status, value,
outstandingValue, reference, allocationReference, acn}; values are signed cents as strings ("+49500" = $495.00 owed).
Legends (Table 0019): Invoice, Applications, Annual Review Fee Invoice, Direct Debit, Direct Debit Rejection, Prescribed Fee,
Late Fee - Less than 1 Month, Late Fee - Greater than 1 Month, Credit Note, Refund Payment, B Pay, EFT, Fee Waiver …
Statuses (Table 0020): Outstanding, Fully Paid, Partially Paid, Fully Allocated, Partially Allocated, Unallocated, Memo …
361/362/RA52: POST -> {response: {requestId, documentNumber, documentUrl}}; GET /asic-{361|362|ra52}-forms/{requestId}/status.

## Codes
Signatory role (Table 0011): ACC accountant · AGT agent · APP applicant · DIR director · LEG legal representative · LP lodging
party · REP representative · SCP shelf company promoter · SEC secretary · SIG signatory · SUB subscriber.
Payment method (Table 0032): DDEB agent's direct-debit account · INV ASIC invoice.

## Endpoints WITHOUT a public docs page (use call_endpoint; shapes follow the Form 484 / 361 pattern — ask the vendor or use
their Postman collection): /company-name-reservations (410B), /company-name-reservation-extensions (410F), /asic/extracts,
/tax/registrations (TAXREG), /asic-370-forms, /asic-492-forms, /asic-902-forms, /asic-ra17|ra50|ra51|ra61|ra63|ra65|ra67|ra71-forms,
/asic-inbox, /asic-inbox/{id}/pdf.
"""

WEBHOOKS_DOC = """# Webhooks — status pushes instead of polling
Manage per API key (and therefore per environment): GET/POST /webhooks, PATCH/DELETE /webhooks/{id}, POST /webhooks/{id}/rotate-secret
(tools list_webhooks, create_webhook, update_webhook, delete_webhook, rotate_webhook_secret). Up to 10 endpoints per key.
url must be HTTPS and resolve to a public IP (private / loopback / link-local / CGNAT rejected at registration AND on every
delivery). eventTypes non-empty; ["*"] = everything. The signing secret (whsec_…) is returned only on create and rotate.

## Events and the statuses that trigger them
| event | interaction | notifiable statuses |
|---|---|---|
| company_registration.status_changed | company-registration | finished, rejected, validation failed, manual review, error |
| abn_registration.status_changed | abnreg.lodge | finished, rejected, manual review, error |
| business_name_registration.status_changed | business-name-registration | finished, rejected, manual review, error |
| business_name_renewal.status_changed | business-name-renewal | finished, rejected, manual review, error |
| ppsr.alert.grantor_registration_changed | PPSR mailbox | new PPSR registration against a watched organisation (payload has organisationNumberType, organisationNumber, registrationSummary) |
| monitoring.change_detected / .expiring / .expired | entity monitoring | see bapi://docs/entity-monitoring (payload carries an entity object) |
Each transition is delivered separately (manual review -> finished = two deliveries).

## Delivery payload (POST, application/json)
{"event": "company_registration.status_changed", "requestId": 30172, "interaction": "company-registration", "status": "rejected",
 "timestamp": "2026-07-31T11:40:11+10:00", "entityName": "EXAMPLE PTY LTD", "acn": "123456789", "abn": "12345678901",
 "deliveryId": "wd_9f2a1c7e..."}
entityName / acn / abn are omitted when not applicable. deliveryId is stable across retries -> dedupe on it. The payload is a
nudge: fetch GET /status/{requestId} (or the endpoint's status path) for the real data.

## Verifying X-Bapi-Signature: t=<unix>,v1=<hex>
expected = HMAC_SHA256(secret, f"{t}.{raw_body}"); constant-time compare with v1; optionally reject old t (replay).
Use the raw request bytes — re-serialising the JSON changes them. During rotation accept both secrets.
Python: hmac.compare_digest(hmac.new(secret.encode(), f"{t}.{raw}".encode(), hashlib.sha256).hexdigest(), v1)
Tool verify_webhook_signature does this check locally for debugging.

## Responding and retries
Reply 2xx within 15 s (body ignored); process asynchronously. Redirects are not followed (3xx = failure). Failures retry up to
6 attempts: immediate, +30 s, +5 min, +15 min, +1 h, +6 h (≈24 h total). 20 consecutive failures disable the endpoint and email
the account owner; re-enable via dashboard or update_webhook(enabled=true), which resets the counter. Webhooks are a
convenience layer — poll the status endpoint when you need the current state right now.
"""


def _asic_codes_doc() -> str:
    combos = "\n".join(
        f"| {sub} | " + ", ".join(f"{cls}→{typ}" for cls, typ in classes.items()) + " |"
        for sub, classes in COMPANY_COMBINATIONS.items()
    )
    return f"""# ASIC code tables (company registration)
## Company type (Table 0013)
APUB Australian public company · APTY Australian proprietary company · FNOS foreign company · RACN registered Australian body
## Company class (Table 0001)
LMGT limited by guarantee · LMSG limited by shares & guarantee · LMSH limited by shares · NLIA no liability · UNLM unlimited
## Company sub-class (Table 0002)
PNPC proprietary non-profit · PSTC proprietary superannuation trustee · HUNT proprietary home unit · PROP proprietary other (the
normal Pty Ltd) · LISN licensed to omit "Limited" (s.383) · LISS same, super trustee · LIST listed public · STFI small
transferring financial institution · NLTD not-limited public (s.150) · ULSN unlisted public non-profit · ULSS unlisted public
super trustee · ULST unlisted public
## Valid sub-class -> class -> type combinations (Table 0003)
| sub-class | class→type |
|---|---|
{combos}
## Jurisdiction / address state codes (Table 0035)
ACT, NSW, NT, QLD, SA, TAS, VIC, WA, plus external territories CCK Cocos (Keeling), CXR Christmas Island, JBT Jervis Bay, NFK Norfolk Island
(Table 0004, business-name states: ACT, NSW, NT, QLD, SA, TAS, VIC, WA only)
## Legal elements in company names (Table 0012)
APTY LMSH: one of PTY / PTY. / PROPRIETARY followed by one of LTD / LTD. / LIMITED (e.g. "X PTY LTD")
APTY UNLM: PTY / PTY. / PROPRIETARY only · APUB LMSH or LMGT: LTD / LTD. / LIMITED (may be omitted under s.150)
APUB NLIA: NL / N.L. / NO LIABILITY · APUB UNLM: no legal elements
## Roles
Officeholder roles: DIR director, SEC secretary. Applicant signatory roles (Table 0011 subset): DIR, SEC, ALT alternate director, AGT agent.
## Unacceptable words in personal names (Table 0023)
&, COMPANY, LIMITED, LTD, LTD., P/L, PROPRIETARY, PTY, PTY.
## ASIC advice types you may see in warnings (Table 0018)
201MANL manual review · 201PEND reserved pending applicant action · 201RJCT rejected · 201SUBJ temporarily reserved subject to
ASIC decision · 410RSVD name reserved · 410RJCT reservation rejected
"""


def _ato_codes_doc() -> str:
    subtypes = "\n".join(
        f"- {et}: {', '.join(sorted(subs))}" for et, subs in SUBTYPES_BY_ENTITY_TYPE.items()
    )
    roles_rows = []
    for et, subs in SUBTYPES_BY_ENTITY_TYPE.items():
        for st in sorted(subs):
            base = sorted(allowed_positions(st, None))
            cht = sorted(allowed_positions(st, "CHT"))
            extra = f" · with CHT: {', '.join(cht)}" if cht != base else ""
            roles_rows.append(f"| {et}/{st} | {', '.join(base) or 'none'}{extra} |")
    roles = "\n".join(roles_rows)
    return f"""# ATO code tables (ABN registration)
## Entity type (Table 0001) -> allowed sub-types (Table 0002)
051 company · 117 individual/sole trader · 161 partnership · 210 superannuation fund · 212 trust
{subtypes}
Sub-type names: 006 sole trader · 009 approved deposit fund · 027 Australian private company · 032 Australian public company ·
039 cash management trust · 045 co-operative · 059 deceased estate · 071 discretionary trust investment · 072 discretionary trust
services management · 077 discretionary trust trading · 087 family partnership · 089 fixed unit trust · 090 fixed trust ·
112 hybrid trust · 120 limited partnership · 138 non-regulated super fund · 145 other incorporated entity · 150 other
partnership · 156 other unincorporated entity · 168 pooled superannuation trust · 174 public trading trust · 179 public unit
trust listed · 184 public unit trust unlisted · 196 regulated self-managed super fund · 205 strata title · 221 APRA regulated fund
## Sub-type additional (Table 0003)
UPC unlisted public company · CLP corporate limited partnership · CHT closely held trust · LBG limited by guarantee
## Application reason (Table 0004)
NEW new business · CBS change in business structure · PUR purchased existing business · INV investment · CON contractor ·
PAY to receive payment for services · RER re-commenced (same structure) · RED re-commenced (different structure) · OTH other
## Associate position codes (Table 0013) and which sub-types accept them
DIR director · DCT director of a corporate trustee · PTN partner · MSF SMSF member · POF public officer · TRT trustee ·
TRB trust beneficiary · CSH company shareholder · CSY company secretary · OFB office bearer
| entityType/subType | allowed positionHeld |
|---|---|
{roles}
## GST (Tables 0007, 0008, 0018)
turnoverRangeCode: $0 - $74,999 · $75,000 - $149,999 · $150,000 - $1,999,999 · $2,000,000 - $9,999,999 · $10,000,000 - $19,999,999 · $20 million and over
lodgementFrequencyCode: Annually · HalfYearly · Monthly · Quarterly      reasonCode: Legislative Requirement · Voluntary
## PAYG withholding (Tables 0005, 0006)
lodgementChannelCode: E, EAS, ECI, ELS, EML, FDF, INP, MGV, MIPS, OTH, P, PE, PH, RDCC, RO, SBR, SMS, WEB
paymentSummaryProvisionMethodCode: ATO · Both · Print
## Luxury car tax (Tables 0011, 0019)
liabilityRangeCode: $0 to $49,999 · $50,000 to $99,999 · $100,000 to $499,999 · $500,000 to $5 million · More than $5 million
activityCode: Importer · Manufacturer · Retailer · Wholesaler
## SMSF benefit structure (Table 0012): A accumulation · D defined benefit · U unfunded defined benefit · E hybrid
## Gender (Table 0014): 1 male · 2 female · 3 intersex/indeterminate · 0 not stated
## Language (Table 0015): ARA ASS CHI CRO DAR DIN ENG FAR GRK HIN IND ITA JAP KAR KHM KOR MAC MLT BLK(n/a) OTH POL POR RUS SRB SOM SPA TTY THA TUR VNM
"""


mcp = FastMCP(
    name="Business API (AU)",
    instructions=(
        "Tools for businessapi.com.au — Australian ASIC/ATO/ABRS company, ABN, business-name, ASIC-form, PPSR and "
        f"entity-monitoring APIs. This server is in the **{ENV.upper()}** environment"
        + ("" if ENV == "test" else " — live POST calls lodge real forms and are billed")
        + ". Read resource bapi://docs/overview before the first call and bapi://docs/endpoints to find a path. "
        "Lodgements are asynchronous: a POST returns a requestId, then poll get_request_status (or wait_for_request) until "
        "the status is 'finished' or 'rejected' (PPSR: 'completed' or 'failed'). The lodge_* tools (company, ABN and "
        "business-name registration, business-name renewal, company name change, deregistration, ASIC Forms 484/361/362) "
        "take a fully typed request (the schema is the "
        "vendor's field reference) and default to dry_run=true, which validates and returns the payload without lodging: show "
        "it to the user, confirm fees, then call again with dry_run=false. Entity monitoring orders/renewals also default to "
        "dry_run=true and need an idempotency_key you choose. Use the typed tools when one exists; otherwise use call_endpoint "
        "with a path from the catalogue."
        + (
            ""
            if INLINE_SCHEMAS
            else " Here the lodge_* tools take `request` as a plain JSON object: first call get_request_schema(form) (or read "
            "bapi://schemas/{form}) for the exact field schema, build the object, then call the tool; invalid payloads come back "
            "as a readable list of field errors."
        )
        + (
            f" Only these toolsets are enabled on this server: {', '.join(sorted(ENABLED_TOOLSETS))}."
            if ENABLED_TOOLSETS != set(ALL_TOOLSETS)
            else ""
        )
    ),
)


def tool(toolset: str, **kwargs: Any):
    """Like @mcp.tool, but the tool is registered only when its toolset is enabled (BAPI_TOOLSETS) and the toolset is
    attached as a tag. Toolsets: lookup, registrations, agent_forms, monitoring, webhooks, ppsr, core."""
    kwargs["tags"] = set(kwargs.get("tags") or set()) | {toolset}
    if toolset not in ENABLED_TOOLSETS:
        return lambda fn: fn
    return mcp.tool(**kwargs)

# --------------------------------------------------------------------------------------
# HTTP plumbing
# --------------------------------------------------------------------------------------

_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if not API_KEY:
        raise ValueError("BAPI_API_KEY is not set. Create a key at https://businessapi.com.au/user/api-keys")
    if ENV not in ("test", "live"):
        raise ValueError("BAPI_ENV must be 'test' or 'live'")
    if f"_{ENV}_" not in API_KEY:
        raise ValueError(f"BAPI_ENV={ENV} but BAPI_API_KEY is not a {ENV} key (expected prefix bapi_sk_{ENV}_)")
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {API_KEY}", "Accept": "application/json"},
            timeout=httpx.Timeout(30.0),
        )
    return _client


def _guard_write(method: str) -> None:
    if ENV == "live" and method.upper() in ("POST", "PUT", "PATCH", "DELETE") and not ALLOW_LIVE:
        raise PermissionError(
            "Refused: this is a LIVE write. It would lodge a real form with ASIC/ATO/PPSR and be billed. "
            "Ask the user to confirm, then restart the server with BAPI_ALLOW_LIVE=true — or use BAPI_ENV=test."
        )


async def _call(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    body: Any = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    _guard_write(method)
    resp = await _http().request(method.upper(), f"{PREFIX}{path}", params=params, json=body, headers=headers)
    ctype = resp.headers.get("content-type", "")
    if "json" in ctype:
        try:
            data: Any = resp.json()
        except ValueError:
            data = {"raw": resp.text[:4000]}
    elif "pdf" in ctype:
        data = {"note": "Binary PDF returned; download it with an HTTP client, not through MCP.", "bytes": len(resp.content)}
    else:
        data = {"raw": resp.text[:4000]}
    out: dict[str, Any] = {"httpStatus": resp.status_code, "environment": ENV, "url": str(resp.request.url)}
    if isinstance(data, dict):
        out.update(data)
    else:
        out["data"] = data
    return out


def _dry_run(path: str, payload: dict[str, Any], note: str) -> dict[str, Any]:
    return {
        "dryRun": True,
        "environment": ENV,
        "wouldPost": f"{PREFIX}{path}",
        "note": note,
        "payload": payload,
    }


# --------------------------------------------------------------------------------------
# Typed tools — lookups and helpers (free, read-only)
# --------------------------------------------------------------------------------------

@tool("lookup", annotations={"readOnlyHint": True, "openWorldHint": True})
async def lookup_abn(abn: str) -> dict[str, Any]:
    """Validate an Australian Business Number (ABN) and look it up on the ABR.

    abn: 11 digits (spaces allowed). Free, read-only, no lodgement. Use this before any registration,
    PPSR or monitoring call that needs a confirmed ABN / entity name.
    """
    return await _call("GET", f"/lookup/abn/{abn.replace(' ', '')}")


@tool("lookup", annotations={"readOnlyHint": True, "openWorldHint": True})
async def lookup_acn(acn: str) -> dict[str, Any]:
    """Validate an Australian Company Number (ACN) and look the company up on ASIC.

    acn: 9 digits (spaces allowed). Free, read-only.
    """
    return await _call("GET", f"/lookup/acn/{acn.replace(' ', '')}")


@tool("lookup", annotations={"readOnlyHint": True, "openWorldHint": True})
async def check_company_name_availability(name: str) -> dict[str, Any]:
    """Check whether a proposed COMPANY name (including legal elements, e.g. "ACME PTY LTD") is available with ASIC.
    Free helper. Call this before lodge_company_registration or a name change / reservation."""
    return await _call("GET", "/helpers/company-name-availability", params={"name": name})


@tool("lookup", annotations={"readOnlyHint": True, "openWorldHint": True})
async def check_business_name_availability(name: str, holder_name: str | None = None) -> dict[str, Any]:
    """Check a proposed BUSINESS (trading) name against ASIC's name test. Free helper.

    holder_name: legal name of the entity that will hold the name (e.g. "ACME TRADING PTY LTD"). Strongly recommended:
    ASIC reports a match on the holder's own company name as "Identical", and with holder_name the response adds
    `availableForHolder` (lodge only when true) and marks such objections `ownName: true`.
    Response: availability (ASIC code, e.g. Available / Identical / ExistingBN), shortDescription, objections[]
    {code, level, description, message, matchedNames, ownName}. `asicUnavailable: true` means "could not check", not "available".
    """
    params: dict[str, Any] = {"name": name}
    if holder_name:
        params["holderName"] = holder_name
    return await _call("GET", "/business-name-availability", params=params)


@tool("lookup", annotations={"readOnlyHint": True, "openWorldHint": True})
async def list_business_industries() -> dict[str, Any]:
    """List the business-industry (main business activity) codes and descriptions used by ABN / business-name
    registrations (businessActivity.code / description). Free helper."""
    return await _call("GET", "/helpers/business-industries")


# --------------------------------------------------------------------------------------
# Typed tools — lodgements (POST, billed in live). Default dry_run=true.
# --------------------------------------------------------------------------------------

async def _lodge_company_registration(request: CompanyRegistrationRequest, dry_run: bool = True) -> dict[str, Any]:
    """Lodge ASIC Form 201 — register a new Australian company. LIVE cost ≈ $50 service fee + ASIC registration fee.

    `request` mirrors https://businessapi.com.au/developers/api/company-registrations field-for-field; read
    bapi://docs/company-registrations (workflow + rules) and bapi://docs/asic-codes (code tables) first.
    Typical Pty Ltd: companyType APTY, companyClass LMSH, companySubClass PROP, one ORD share class, ≥1 director.
    Before lodging: check_company_name_availability(name).

    dry_run=true (default): validate locally (all "Mandatory IF" rules from the docs) and return the exact JSON that
    would be posted — show it and the fees to the user. dry_run=false: lodge; returns {response: {requestId}}.
    Then wait_for_request(requestId) -> 'finished' gives acn / dateOfRegistration; certificate PDF at
    GET /company-registrations/{requestId}/certificate, document pack at …/documents.
    """
    payload = request.to_payload()
    if dry_run:
        return _dry_run("/company-registrations", payload, "Validated. Re-run with dry_run=false after the user confirms the payload and fees.")
    return await _call("POST", "/company-registrations", body=payload)


async def _lodge_abn_registration(request: AbnRegistrationRequest, dry_run: bool = True) -> dict[str, Any]:
    """Apply for an Australian Business Number (ATO ABNREG). LIVE cost ≈ $25.

    `request` mirrors https://businessapi.com.au/developers/api/abn-registrations; read bapi://docs/abn-registrations
    (entity types, workflow, rules) and bapi://docs/ato-codes first. Sole trader = entityType 117 / entitySubType 006;
    company = 051/027 (needs the ACN); partnership = 161/087|150; trust = 212/…; SMSF = 210/196.
    The entitlement questionnaire answers come from GET /helpers/entitlement-questionnaire (call_endpoint).
    TFNs are sensitive: collect them, validate (checksum is checked locally), never repeat them back to the user.

    dry_run=true (default): validate and return the payload (the server adds testTransmissionIndicator from its env).
    dry_run=false: lodge -> {requestId, status: "received", billing: {price_cents, charged_cents}}; a duplicate
    in-progress application returns the existing requestId with a warning. Then wait_for_request(requestId) ->
    'finished' gives info.abn. HTTP 400 = field errors keyed by path, nothing charged.
    """
    payload = request.to_payload(test_transmission=TEST_TRANSMISSION)
    if dry_run:
        return _dry_run("/abn-registrations", payload, "Validated. Re-run with dry_run=false after the user confirms.")
    return await _call("POST", "/abn-registrations", body=payload)


async def _lodge_business_name_registration(request: BusinessNameRegistrationRequest, dry_run: bool = True) -> dict[str, Any]:
    """Register a business (trading) name with ASIC (BNREG). LIVE cost ≈ $15 service fee + ASIC fee (1 or 3 years).

    `request` mirrors https://businessapi.com.au/developers/api/business-name-registrations; read
    bapi://docs/business-name-registrations first. Holder types: IND, IB (companies), PTSH, USTR, JV.
    Before lodging: check_business_name_availability(name, holder_name) and require availableForHolder == true.
    paymentMethod "card" -> after lodging, get_business_name_registration_status returns an ASIC payment link the
    payer must open; "invoice" -> ASIC emails an invoice (pay within 10 days).

    dry_run=true (default): validate and return the payload. dry_run=false: lodge -> {requestId, status: "new"}.
    Then get_business_name_registration_status / wait_for_request(kind="business_name").
    """
    payload = request.to_payload(test_transmission=TEST_TRANSMISSION)
    if dry_run:
        return _dry_run("/business-name-registrations", payload, "Validated. Re-run with dry_run=false after the user confirms.")
    return await _call("POST", "/business-name-registrations", body=payload)


@tool("registrations", annotations={"readOnlyHint": True, "openWorldHint": True})
async def get_business_name_registration_status(request_id: int) -> dict[str, Any]:
    """Status of a business-name registration. Statuses: new -> … -> "wating ASIC CC payment" (card: response has
    transactionPaymentUrl + transactionFee; the payer must open the link) -> finished (certificateUrl / receiptNumber, or
    transactionDocumentUrl for invoice payment). "cancelled" = payment link expired/declined: call
    refresh_business_name_payment_link, never re-lodge. "rejected" with errors["00133"] = name not available."""
    return await _call("GET", f"/business-name-registrations/{request_id}/status")


@tool("registrations", annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
async def refresh_business_name_payment_link(request_id: int) -> dict[str, Any]:
    """Re-issue the ASIC card-payment link for a business-name application whose status is "cancelled" (link expired,
    cancelled or card declined). Recovers the SAME application under the same requestId; fails if already paid/registered.
    Returns response.transactionPaymentUrl."""
    return await _call("GET", f"/business-name-registrations/{request_id}/requestNewPaymentLink")


# --------------------------------------------------------------------------------------
# Typed tools — business name renewal, ASIC Form 484
# --------------------------------------------------------------------------------------

async def _lodge_business_name_renewal(request: BusinessNameRenewalRequest, dry_run: bool = True) -> dict[str, Any]:
    """Renew an existing business name with ASIC (BNRENEW) for 1 or 3 years. LIVE cost ≈ $15 service fee + ASIC renewal fee.

    request: generalInformation {businessName exactly as registered, abn of the holder, term 1|3, email} and
    applicant {individual {familyName, givenNames}, declaresTrueAndCorrectIndicator true}.
    Check the current record first with lookup_abn (businessNames) if unsure of the exact name.
    dry_run=true (default) validates and returns the payload; dry_run=false lodges -> {requestId, status: "new"}.
    Then get_business_name_renewal_status / wait_for_request(kind="business_name_renewal") -> 'finished' gives
    response.businessNameNumber, renewalTo and transactionDocumentUrl.
    """
    payload = request.to_payload(test_transmission=TEST_TRANSMISSION)
    if dry_run:
        return _dry_run("/business-name-renewals", payload, "Validated. Re-run with dry_run=false after the user confirms.")
    return await _call("POST", "/business-name-renewals", body=payload)


@tool("registrations", annotations={"readOnlyHint": True, "openWorldHint": True})
async def get_business_name_renewal_status(request_id: int) -> dict[str, Any]:
    """Status of a business-name renewal: new -> … -> finished with response {transactionDocumentUrl, businessNameNumber, renewalTo}."""
    return await _call("GET", f"/business-name-renewals/{request_id}/status")


async def _lodge_asic_484_form(request: Asic484FormRequest, dry_run: bool = True) -> dict[str, Any]:
    """Lodge ASIC Form 484 — Changes to Company Details — for a company managed under your registered-agent account.

    One form can combine any of: registeredOffice / principalPlaceOfBusiness address change, officeHolders (appointments,
    cessations, name/address changes), shareIssue or shareCancellation (both need shareStructure + members), ultimateHoldingCompany,
    concessionalFees, annexures. Always needs generalInformation {companyName, acn} and signature {name, role DIR|SEC|ALT,
    date within the last 6 months, declaresTrueAndCorrectIndicator true}. Read bapi://docs/asic-484-forms first for the
    status / holdingStatus / cancellationProvision codes and the V455 share-totals rule (checked locally).
    The server upper-cases all string values (ASIC requirement). ASIC charges late fees when a change is notified more than
    28 days after it took effect. Billed as a registered-agent form (Pro plan includes 2,500 forms/month).

    dry_run=true (default) validates and returns the payload; dry_run=false lodges ->
    {response: {requestId, documentNumber, documentUrl}}. Then get_asic_484_form_status / wait_for_request(kind="asic_484").
    """
    payload = request.to_payload(test_transmission=TEST_TRANSMISSION)
    if dry_run:
        return _dry_run("/asic-484-forms", payload, "Validated (incl. ASIC V455 share totals). Re-run with dry_run=false after the user confirms.")
    return await _call("POST", "/asic-484-forms", body=payload)


@tool("agent_forms", annotations={"readOnlyHint": True, "openWorldHint": True})
async def get_asic_484_form_status(request_id: int) -> dict[str, Any]:
    """Status of a Form 484 lodgement: {response: {requestId, status, documentNumber, info: {companyName, acn}}, errors}."""
    return await _call("GET", f"/asic-484-forms/{request_id}/status")


# --------------------------------------------------------------------------------------
# Typed tools — entity monitoring ($18 per entity per year live; $0 in the sandbox)
# --------------------------------------------------------------------------------------

@tool("monitoring", annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
async def start_entity_monitoring(entities: list[MonitoredEntityInput], idempotency_key: str, dry_run: bool = True) -> dict[str, Any]:
    """Start monitoring up to 25 companies (acn) and/or ABN holders (abn) for public-register changes, prepaid 12 months each.

    LIVE cost $18.00 per entity (card billing required); sandbox $0. Sources: ASIC, ABR, insolvency notices and PPSR (companies),
    IP Australia trade marks, ASIC licence registers, ACNC. Changes arrive as monitoring.change_detected webhooks and through
    list_monitoring_events. Read bapi://docs/entity-monitoring first.
    entities: [{acn}, {abn}, {acn, abn}], each with an optional `reference` (≤100 chars) of your own. ARBNs are not accepted.
    idempotency_key: YOUR unique string for this order (≤255 chars, e.g. "order-2026-11-18-acme"). A retried call with the same
    key never charges twice — reuse it when retrying after a timeout or a 202 "processing" answer.
    Response 201 {unitPrice, total (cents), entities: [entity objects]}; 422 validation_failed carries per-row errors
    (already_monitored -> renew_entity_monitoring; stopped_with_time_remaining -> resume_entity_monitoring); nothing charged on errors.
    dry_run=true (default) validates locally and returns the payload.
    """
    if not entities:
        raise ValueError("entities must contain at least one entity")
    if len(entities) > 25:
        raise ValueError("at most 25 entities per request; split the batch")
    if not idempotency_key.strip() or len(idempotency_key) > 255:
        raise ValueError("idempotency_key must be 1-255 characters")
    body = {"entities": [e.model_dump(mode="json", exclude_none=True) for e in entities]}
    if dry_run:
        return _dry_run("/monitoring/entities", body, f"Validated. Re-run with dry_run=false (same idempotency_key '{idempotency_key}') after the user confirms the charge.")
    return await _call("POST", "/monitoring/entities", body=body, headers={"Idempotency-Key": idempotency_key})


@tool("monitoring", annotations={"readOnlyHint": True, "openWorldHint": True})
async def list_monitored_entities(
    status: Literal["processing", "active", "stopped", "expired"] | None = None,
    acn: str | None = None,
    abn: str | None = None,
    expires_before: str | None = None,
    per_page: int = 25,
    page: int = 1,
) -> dict[str, Any]:
    """List monitored entities (data[] of entity objects + meta {currentPage, perPage, total, lastPage}).
    Filters: status, acn, abn, expires_before (YYYY-MM-DD, for renewal runs). per_page max 100.
    Entity object: id, acn, abn, entityName, reference, status, monitoringStatus (pending|active|degraded|inactive), startsAt,
    expiresAt, daysRemaining, lastEventAt, entityStatus (registered|deregistered|cancelled|external_administration|
    strike_off_proposed|unknown), entityType, gstRegistered, gstDate, businessNames[]."""
    params: dict[str, Any] = {"perPage": max(1, min(per_page, 100)), "page": max(1, page)}
    if status:
        params["status"] = status
    if acn:
        params["acn"] = acn.replace(" ", "")
    if abn:
        params["abn"] = abn.replace(" ", "")
    if expires_before:
        params["expiresBefore"] = expires_before
    return await _call("GET", "/monitoring/entities", params=params)


@tool("monitoring", annotations={"readOnlyHint": True, "openWorldHint": True})
async def get_monitored_entity(entity_id: int) -> dict[str, Any]:
    """One monitored entity (see list_monitored_entities for the object). 404 not_found if it is not yours in this environment
    (ids are per account and per environment; a merged ABN entity's old id also returns 404 — re-read the list)."""
    return await _call("GET", f"/monitoring/entities/{entity_id}")


@tool("monitoring", annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": True})
async def stop_entity_monitoring(entity_id: int) -> dict[str, Any]:
    """Stop monitoring an entity now. Webhooks stop immediately; the paid term keeps running (no refund) and the entity can be
    resumed free until it ends; stopped time is not added back. Safe to repeat. 409 order_processing while an order/renewal
    for it is still processing — retry in a few minutes."""
    return await _call("DELETE", f"/monitoring/entities/{entity_id}")


@tool("monitoring", annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
async def resume_entity_monitoring(entity_id: int) -> dict[str, Any]:
    """Resume a stopped entity whose paid term is still running (free). Changes that happened while stopped are not sent.
    422 not_resumable = term already ended -> renew_entity_monitoring instead; 422 acn_not_eligible / abn_not_active = the
    company was deregistered / ABN cancelled meanwhile."""
    return await _call("POST", f"/monitoring/entities/{entity_id}/resume")


@tool("monitoring", annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
async def renew_entity_monitoring(entity_ids: list[int], idempotency_key: str, dry_run: bool = True) -> dict[str, Any]:
    """Renew monitoring for up to 25 entities for another 12 months. LIVE cost $18.00 per entity; sandbox $0.

    Monitoring never auto-renews: act on monitoring.expiring webhooks (30 and 7 days before expiry) or list_monitored_entities
    with expires_before. idempotency_key is REQUIRED by the API (400 without it): your unique string per renewal batch, reused on
    retries so a timed-out retry never buys a second year. Active entities: the new term starts at the current expiry; expired:
    today. Expiry can't go more than 2 years ahead (renewal_too_far_ahead). A stopped entity with time left must be resumed, not
    renewed. Deregistered companies / cancelled ABNs can't be renewed. Any id not yours -> whole request 404, nothing charged.
    Response has the order shape {unitPrice, total, entities}. dry_run=true (default) returns the payload only.
    """
    if not entity_ids:
        raise ValueError("entity_ids must contain at least one id")
    if len(entity_ids) > 25:
        raise ValueError("at most 25 ids per renewal; split the batch")
    if not idempotency_key.strip() or len(idempotency_key) > 255:
        raise ValueError("idempotency_key must be 1-255 characters")
    body = {"ids": entity_ids}
    if dry_run:
        return _dry_run("/monitoring/entities/renew", body, f"Validated. Re-run with dry_run=false (same idempotency_key '{idempotency_key}') after the user confirms the charge.")
    return await _call("POST", "/monitoring/entities/renew", body=body, headers={"Idempotency-Key": idempotency_key})


@tool("monitoring", annotations={"readOnlyHint": True, "openWorldHint": True})
async def get_entity_snapshot(entity_id: int) -> dict[str, Any]:
    """The entity's current record from every monitored source, as JSON (snake_case inside `snapshot`): entity_details (ASIC/ABR
    names, addresses, statuses, business_names), registrations (AFS/credit licences, ACNC), trade_marks, insolvency_notices, ppsr.
    Active entities only: 409 entity_not_active / snapshot_not_ready (new entity, wait a few minutes) / not_available_in_sandbox
    (sandbox entities have no live data); 503 snapshot_unavailable -> retry later. 20 requests/min.
    The same record as a dated PDF: GET /monitoring/entities/{id}/report?includeRecent=true (binary; fetch outside MCP)."""
    return await _call("GET", f"/monitoring/entities/{entity_id}/snapshot")


@tool("monitoring", annotations={"readOnlyHint": True, "openWorldHint": True})
async def list_monitoring_events(
    entity_id: int | None = None,
    since: str | None = None,
    per_page: int = 25,
    page: int = 1,
) -> dict[str, Any]:
    """List detected changes (summary form: monitoringEventId, entityId, eventType, title, detectedAt, simulated).

    Without entity_id: ALL your events, oldest first (GET /monitoring/events). `since` = the last monitoringEventId you processed
    (returns later events) or an ISO 8601 date-time — store the last id to catch up after webhook downtime.
    With entity_id: that entity's events, newest first. per_page max 100.
    eventType: document_lodged, name_change, address_change, status_change, type_change, gst_change, business_name_added,
    business_name_removed, insolvency_notice, ppsr_registration, trade_mark_filed / _status_change / _owner_change /
    _renewal_due / _renewed, register_added / _status_change / _removed / _conditions_change, other_change (treat unknown as other_change).
    Use get_monitoring_event for description, source, effectiveDate and metadata (oldValue/newValue, documentType …).
    """
    params: dict[str, Any] = {"perPage": max(1, min(per_page, 100)), "page": max(1, page)}
    if entity_id is not None:
        return await _call("GET", f"/monitoring/entities/{entity_id}/events", params=params)
    if since:
        params["since"] = since
    return await _call("GET", "/monitoring/events", params=params)


@tool("monitoring", annotations={"readOnlyHint": True, "openWorldHint": True})
async def get_monitoring_event(event_id: int) -> dict[str, Any]:
    """One monitoring event in full: description, source (asic|abr|insolvency|ppsr|trademark|register), effectiveDate, metadata
    (field/oldValue/newValue; documentType/documentNumber for document_lodged; businessName; previousName; noticeUrl; tradeMark*;
    registerLabel/registerStatus/licenseeNumber/registerUrl; gstCancelled) and the entity object."""
    return await _call("GET", f"/monitoring/events/{event_id}")


@tool("monitoring", annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True})
async def simulate_monitoring_event(
    entity_id: int,
    event_type: str | None = None,
    simulate: Literal["expiring_30", "expiring_7", "expired", "deregistered"] | None = None,
) -> dict[str, Any]:
    """SANDBOX ONLY: make a sandbox entity emit a test event + webhook. Give exactly one of:
    event_type — a change event such as document_lodged, address_change, status_change, gst_change, trade_mark_filed … (returns the event);
    simulate — expiring_30 | expiring_7 | expired (lifecycle webhooks) or deregistered (permanently marks the test entity deregistered;
    renew/resume are then refused). Refused in the live environment (the API returns 404 there too)."""
    if ENV == "live":
        raise PermissionError("simulate_monitoring_event is sandbox-only; switch to BAPI_ENV=test.")
    if bool(event_type) == bool(simulate):
        raise ValueError("Give exactly one of event_type or simulate.")
    body = {"eventType": event_type} if event_type else {"simulate": simulate}
    return await _call("POST", f"/monitoring/entities/{entity_id}/simulate", body=body)


# --------------------------------------------------------------------------------------
# Typed tools — company name change, deregistration, registered-agent forms 361 / 362 / RA52
# --------------------------------------------------------------------------------------

async def _lodge_company_name_change(request: CompanyNameChangeRequest, dry_run: bool = True) -> dict[str, Any]:
    """Lodge ASIC Form 205A — change a registered company's name. LIVE: service fee + ASIC fee (paymentMethod DDEB or INV).

    Before lodging: check_company_name_availability(proposedName). `request` = generalInformation {companyName, acn, companyType,
    proposedName (with legal elements), meetingDate, meetingResolutionText (the special resolution), paymentMethod DDEB|INV,
    optional acnName*/nameReserved*/nameIdentical* flags}, signatory {individual, role DIR|SEC…, dateSigned, declaration true},
    admin {manualReviewIndicator}. Read bapi://docs/asic-agent-forms. Requires registered-agent onboarding for the company.
    dry_run=true (default) validates and returns the payload; dry_run=false lodges -> {requestId, status: "sent"}.
    Then get_company_name_change_status / wait_for_request(kind="company_name_change") -> finished; certificate PDF at
    GET /company-name-changes/{requestId}/certificate (binary, outside MCP).
    """
    payload = request.to_payload(test_transmission=TEST_TRANSMISSION)
    if dry_run:
        return _dry_run("/company-name-changes", payload, "Validated. Re-run with dry_run=false after the user confirms.")
    return await _call("POST", "/company-name-changes", body=payload)


@tool("agent_forms", annotations={"readOnlyHint": True, "openWorldHint": True})
async def get_company_name_change_status(request_id: int) -> dict[str, Any]:
    """Status of a name change: {requestId, status, documentNumber, response {companyName, acn, proposedName}, errors}.
    A request that failed local validation (never reached ASIC) returns only requestId, status and errors."""
    return await _call("GET", f"/company-name-changes/{request_id}/status")


async def _lodge_company_deregistration(request: CompanyDeregistrationRequest, dry_run: bool = True) -> dict[str, Any]:
    """Lodge ASIC Form 6010 — VOLUNTARY DEREGISTRATION of a company (irreversible once ASIC deregisters). LIVE: service fee + ASIC fee.

    All six s.601AA declarations must be true (members agree, not trading, assets < $1,000, fees paid, no liabilities, no legal
    proceedings) — the validator refuses otherwise. `request` = general {companyName, acn, paymentMethod DDEB|INV}, declarations,
    applicant (the company as organisation + role, or an individual), nominee {individual, address} to receive ASIC correspondence,
    signatory {individual, role, dateSigned, declaration true}. Read bapi://docs/asic-agent-forms.
    dry_run=true (default) validates and returns the payload; dry_run=false lodges -> {response: {requestId}}.
    Then get_company_deregistration_status / wait_for_request(kind="company_deregistration") -> finished with info.dateOfDeregistration.
    Transmission log: GET /company-deregistrations/{requestId}/logs (call_endpoint).
    """
    payload = request.to_payload(test_transmission=TEST_TRANSMISSION)
    if dry_run:
        return _dry_run("/company-deregistrations", payload, "Validated. This deregisters a company — get explicit confirmation, then re-run with dry_run=false.")
    return await _call("POST", "/company-deregistrations", body=payload)


@tool("agent_forms", annotations={"readOnlyHint": True, "openWorldHint": True})
async def get_company_deregistration_status(request_id: int) -> dict[str, Any]:
    """Status of a deregistration: {response: {requestId, status, documentNumber, info {companyName, acn, companyType, companyClass,
    companySubClass, jurisdictionOfRegistration, dateOfDeregistration}}, errors}."""
    return await _call("GET", f"/company-deregistrations/{request_id}/status")


async def _lodge_asic_361_form(request: Asic361FormRequest, dry_run: bool = True) -> dict[str, Any]:
    """Lodge ASIC Form 361 — YOUR registered-agent account ceases to act for a company (agent-initiated).

    request = generalInformation {companyName, acn}, signature {name, role AGT, date, agentAuthorisedIndicator true, declaration true}.
    Billed as a registered-agent form. dry_run=true (default) returns the payload; dry_run=false lodges ->
    {response: {requestId, documentNumber, documentUrl}}. Then get_asic_form_status(form="361") / wait_for_request(kind="asic_361").
    """
    payload = request.to_payload(test_transmission=TEST_TRANSMISSION)
    if dry_run:
        return _dry_run("/asic-361-forms", payload, "Validated. Re-run with dry_run=false after the user confirms.")
    return await _call("POST", "/asic-361-forms", body=payload)


async def _lodge_asic_362_form(request: Asic362FormRequest, dry_run: bool = True) -> dict[str, Any]:
    """Lodge ASIC Form 362 — a company appoints (type A) or ceases (type C) your registered agent number (company-initiated).

    Appointing links the company to your agent account, which the other ASIC form tools (484, 205A, 6010 …) need.
    request = generalInformation {companyName, acn, type A|C}, signature {name, role DIR|SEC, date, declaration true}.
    dry_run=true (default) returns the payload; dry_run=false lodges -> {response: {requestId, documentNumber, documentUrl}}.
    Then get_asic_form_status(form="362") / wait_for_request(kind="asic_362").
    """
    payload = request.to_payload(test_transmission=TEST_TRANSMISSION)
    if dry_run:
        return _dry_run("/asic-362-forms", payload, "Validated. Re-run with dry_run=false after the user confirms.")
    return await _call("POST", "/asic-362-forms", body=payload)


@tool("agent_forms", annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
async def request_asic_account_transactions(
    start_date: str,
    end_date: str,
    include_prior_outstanding: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Request ASIC Form RA52 — a report of all transactions on your registered-agent ASIC account for a period (invoices, fees,
    direct debits, late fees, payments). Dates YYYY-MM-DD, end_date ≤ today. Billed as a registered-agent form (no legal effect).
    Returns {response: {requestId, documentNumber}}; then get_asic_form_status(form="ra52") / wait_for_request(kind="asic_ra52") ->
    finished with response.records[] {date, legend (Table 0019, e.g. Invoice, Direct Debit, Late Fee), status (Table 0020, e.g.
    Outstanding, Fully Allocated), value / outstandingValue (signed cents as strings, "+49500" = $495.00), reference, acn}.
    """
    req = AsicRa52Request(general={"startDate": start_date, "endDate": end_date, "includePriorOutstandingIndicator": include_prior_outstanding})
    payload = req.to_payload(test_transmission=TEST_TRANSMISSION)
    if dry_run:
        return _dry_run("/asic-ra52-forms", payload, "Validated.")
    return await _call("POST", "/asic-ra52-forms", body=payload)


@tool("agent_forms", annotations={"readOnlyHint": True, "openWorldHint": True})
async def get_asic_form_status(form: Literal["361", "362", "ra52"], request_id: int) -> dict[str, Any]:
    """Status of a Form 361 / 362 / RA52 lodgement: {response: {requestId, status, documentNumber, info {companyName, acn, type?},
    records? (RA52)}, errors}."""
    return await _call("GET", f"/asic-{form}-forms/{request_id}/status")


# --------------------------------------------------------------------------------------
# Typed tools — webhooks (manage endpoints; verify signatures locally)
# --------------------------------------------------------------------------------------

@tool("webhooks", annotations={"readOnlyHint": True, "openWorldHint": True})
async def list_webhooks() -> dict[str, Any]:
    """List the webhook endpoints registered for THIS API key / environment (signing secrets are never returned)."""
    return await _call("GET", "/webhooks")


@tool("webhooks", annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True})
async def create_webhook(url: str, event_types: list[WebhookEventType]) -> dict[str, Any]:
    """Register a webhook endpoint for this API key (max 10 per key). url must be HTTPS on a public host.

    event_types: one or more of company_registration.status_changed, abn_registration.status_changed,
    business_name_registration.status_changed, business_name_renewal.status_changed, ppsr.alert.grantor_registration_changed,
    monitoring.change_detected, monitoring.expiring, monitoring.expired — or ["*"] for everything.
    The response includes the signing `secret` (whsec_…) ONCE: give it to the user to store immediately; GET never returns it
    (rotate_webhook_secret issues a new one). Read bapi://docs/webhooks for the delivery payload, signature check and retries.
    """
    url = validate_webhook_url(url)
    if not event_types:
        raise ValueError("event_types must not be empty (use ['*'] for all events)")
    return await _call("POST", "/webhooks", body={"url": url, "eventTypes": list(dict.fromkeys(event_types))})


@tool("webhooks", annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
async def update_webhook(
    webhook_id: str,
    url: str | None = None,
    event_types: list[WebhookEventType] | None = None,
    enabled: bool | None = None,
) -> dict[str, Any]:
    """Update a webhook endpoint's URL, event types and/or enabled flag (PATCH). Re-enabling (enabled=true) after an automatic
    disable (20 consecutive failures) resets its failure count. Give at least one field."""
    body: dict[str, Any] = {}
    if url is not None:
        body["url"] = validate_webhook_url(url)
    if event_types is not None:
        if not event_types:
            raise ValueError("event_types must not be empty")
        body["eventTypes"] = list(dict.fromkeys(event_types))
    if enabled is not None:
        body["enabled"] = enabled
    if not body:
        raise ValueError("Give at least one of url, event_types, enabled")
    return await _call("PATCH", f"/webhooks/{webhook_id}", body=body)


@tool("webhooks", annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": True})
async def delete_webhook(webhook_id: str) -> dict[str, Any]:
    """Remove a webhook endpoint. Deliveries stop immediately. Prefer update_webhook(enabled=false) to pause instead."""
    return await _call("DELETE", f"/webhooks/{webhook_id}")


@tool("webhooks", annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
async def rotate_webhook_secret(webhook_id: str) -> dict[str, Any]:
    """Invalidate the endpoint's current signing secret and issue a new one (returned ONCE). The receiver must accept both the old
    and new secret until the new one is deployed everywhere."""
    return await _call("POST", f"/webhooks/{webhook_id}/rotate-secret")


@tool("webhooks", annotations={"readOnlyHint": True, "openWorldHint": False})
def verify_webhook_signature(secret: str, signature_header: str, raw_body: str, tolerance_seconds: int = 300) -> dict[str, Any]:
    """Verify a Business API webhook delivery LOCALLY (no network): recompute HMAC-SHA256 over "{t}.{raw_body}" with the endpoint's
    whsec_ secret and compare it to v1 from the X-Bapi-Signature header ("t=1785552011,v1=<hex>") in constant time.
    raw_body must be the exact bytes received (re-serialised JSON breaks the hash). Returns {valid, reason, ageSeconds}.
    Use it to debug a receiver; in production implement the same check in the receiver itself."""
    parts: dict[str, str] = {}
    for item in signature_header.replace(" ", "").split(","):
        if "=" in item:
            k, v = item.split("=", 1)
            parts[k] = v
    t, v1 = parts.get("t"), parts.get("v1")
    if not t or not v1:
        return {"valid": False, "reason": "header must look like 't=<unix>,v1=<hex>'", "ageSeconds": None}
    expected = hmac.new(secret.encode("utf-8"), f"{t}.{raw_body}".encode("utf-8"), hashlib.sha256).hexdigest()
    sig_ok = hmac.compare_digest(expected, v1.lower())
    try:
        age = int(time.time()) - int(t)
    except ValueError:
        return {"valid": False, "reason": "t is not a unix timestamp", "ageSeconds": None}
    if not sig_ok:
        return {"valid": False, "reason": "signature mismatch (wrong secret, altered body, or re-serialised JSON)", "ageSeconds": age}
    if tolerance_seconds and abs(age) > tolerance_seconds:
        return {"valid": False, "reason": f"timestamp outside ±{tolerance_seconds}s (possible replay)", "ageSeconds": age}
    return {"valid": True, "reason": "ok", "ageSeconds": age}


# --------------------------------------------------------------------------------------
# Typed tools — PPSR
# --------------------------------------------------------------------------------------

@tool("ppsr", annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True})
async def create_ppsr_search(
    abn: str | None = None,
    acn: str | None = None,
    arbn: str | None = None,
    arsn: str | None = None,
    vin: str | None = None,
    serial_number: str | None = None,
    serial_number_type: Literal["ChassisNumber", "HIN", "OfficialNumber"] | None = None,
    registration_number: str | None = None,
    organisation_name: str | None = None,
    family_name: str | None = None,
    given_names: str | None = None,
    date_of_birth: str | None = None,
    authorised_purpose: bool = False,
) -> dict[str, Any]:
    """Create a PPSR (Personal Property Securities Register) search. LIVE cost: $6.00 per search.

    Provide exactly ONE identifier set:
      - abn | acn | arbn | arsn            -> grantor search by number (certificate yes)
      - vin                                -> vehicle serial search + NEVDIS vehicle data
      - serial_number + serial_number_type -> non-VIN serial search (ChassisNumber | HIN | OfficialNumber)
      - registration_number                -> look up one PPSR registration (no certificate)
      - organisation_name                  -> grantor search by organisation name
      - family_name + given_names + date_of_birth (YYYY-MM-DD) + authorised_purpose=True
                                           -> individual grantor search; the user must confirm they have an
                                              authorised purpose under the PPS Act before you set it to True.
    Returns {requestId, status: "new"}. Then poll get_ppsr_search(request_id) until status is "completed" or "failed".
    """
    groups = {
        "number": [abn, acn, arbn, arsn],
        "vin": [vin],
        "serial": [serial_number],
        "registration": [registration_number],
        "organisation": [organisation_name],
        "individual": [family_name, given_names, date_of_birth],
    }
    present = [g for g, vals in groups.items() if any(vals)]
    if len(present) != 1:
        raise ValueError(
            f"Provide exactly one identifier set; got {present or 'none'}. See resource bapi://docs/ppsr-searches."
        )
    kind = present[0]
    if kind == "number" and sum(bool(x) for x in (abn, acn, arbn, arsn)) != 1:
        raise ValueError("Provide only one of abn / acn / arbn / arsn.")
    if kind == "serial" and not serial_number_type:
        raise ValueError("serial_number requires serial_number_type (ChassisNumber | HIN | OfficialNumber).")
    if kind == "individual":
        if not (family_name and given_names and date_of_birth):
            raise ValueError("Individual search needs family_name, given_names and date_of_birth (YYYY-MM-DD).")
        if not authorised_purpose:
            raise ValueError(
                "Individual searches require authorised_purpose=True after the user confirms an authorised purpose."
            )

    body: dict[str, Any] = {}
    for key, val in (
        ("abn", abn), ("acn", acn), ("arbn", arbn), ("arsn", arsn), ("vin", vin),
        ("serialNumber", serial_number), ("serialNumberType", serial_number_type),
        ("registrationNumber", registration_number), ("organisationName", organisation_name),
        ("familyName", family_name), ("givenNames", given_names), ("dateOfBirth", date_of_birth),
    ):
        if val not in (None, ""):
            body[key] = val.replace(" ", "") if key in ("abn", "acn", "arbn", "arsn") else val
    if kind == "individual":
        body["authorisedPurpose"] = True
    return await _call("POST", "/ppsr/searches", body=body)


@tool("ppsr", annotations={"readOnlyHint": True, "openWorldHint": True})
async def get_ppsr_search(request_id: int) -> dict[str, Any]:
    """Get the status / results of a PPSR search created with create_ppsr_search.
    status: new -> processing -> completed | failed. Results are under `data` (registrations[], nevdisData, certificates[]).
    Certificate PDFs: GET /ppsr/searches/{requestId}/certificates/{certificateNumber} (returns 202 while still generating)."""
    return await _call("GET", f"/ppsr/searches/{request_id}")


# --------------------------------------------------------------------------------------
# Typed tools — status, waiting, account, generic escape hatch
# --------------------------------------------------------------------------------------

@tool("core", annotations={"readOnlyHint": True, "openWorldHint": True})
async def get_request_status(request_id: int) -> dict[str, Any]:
    """Poll any lodgement (company / ABN / tax / business-name registration, ASIC forms, deregistration…) by requestId.

    Lifecycle: new -> transmission ok -> validation ok | validation failed -> manual review -> finished | rejected.
    On 'validation failed' / 'rejected' read errors[] ({details, target}); on 'manual review' read warnings;
    on 'finished' the result (e.g. acn, abn, dateOfRegistration, documentNumber) is in `response` (ABN: `info`).
    Use wait_for_request to block until a terminal state; use webhooks in production.
    """
    return await _call("GET", f"/status/{request_id}")


_TERMINAL = {
    "finished", "rejected", "validation failed", "completed", "failed", "cancelled",
    "wating asic cc payment", "waiting asic cc payment", "payment link requested",
}


@tool("core", annotations={"readOnlyHint": True, "openWorldHint": True})
async def wait_for_request(
    request_id: int,
    kind: Literal[
        "lodgement", "ppsr", "business_name", "business_name_renewal", "asic_484",
        "company_name_change", "company_deregistration", "asic_361", "asic_362", "asic_ra52",
    ] = "lodgement",
    timeout_seconds: int = 120,
    poll_seconds: int = 10,
) -> dict[str, Any]:
    """Poll a request until it reaches a terminal (or action-needed) state, then return that status response.

    kind: "lodgement" -> GET /status/{id} (company registration, ABN); "ppsr" -> GET /ppsr/searches/{id};
    "business_name" / "business_name_renewal" / "asic_484" / "company_name_change" / "company_deregistration" /
    "asic_361" / "asic_362" / "asic_ra52" -> that endpoint's own /{id}/status path.
    Stops on: finished, rejected, validation failed, completed, failed, cancelled, "wating ASIC CC payment"
    (business name needs the payer to open the link), or any HTTP error. timeout_seconds max 600.
    Returns the last status plus {"timedOut": bool, "polls": n}. Registrations in the sandbox usually finish in seconds
    to a few minutes; live ASIC/ATO processing can take longer — prefer webhooks for production.
    """
    path = {
        "lodgement": f"/status/{request_id}",
        "ppsr": f"/ppsr/searches/{request_id}",
        "business_name": f"/business-name-registrations/{request_id}/status",
        "business_name_renewal": f"/business-name-renewals/{request_id}/status",
        "asic_484": f"/asic-484-forms/{request_id}/status",
        "company_name_change": f"/company-name-changes/{request_id}/status",
        "company_deregistration": f"/company-deregistrations/{request_id}/status",
        "asic_361": f"/asic-361-forms/{request_id}/status",
        "asic_362": f"/asic-362-forms/{request_id}/status",
        "asic_ra52": f"/asic-ra52-forms/{request_id}/status",
    }[kind]
    timeout_seconds = max(5, min(timeout_seconds, 600))
    poll_seconds = max(3, min(poll_seconds, 60))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    polls = 0
    while True:
        polls += 1
        result = await _call("GET", path)
        raw_status = result.get("status")
        if raw_status is None and isinstance(result.get("response"), dict):  # Form 484 nests status under response
            raw_status = result["response"].get("status")
        status = str(raw_status or "").strip().lower()
        if result.get("httpStatus", 500) >= 400 or status in _TERMINAL:
            return {**result, "timedOut": False, "polls": polls}
        if loop.time() + poll_seconds > deadline:
            return {**result, "timedOut": True, "polls": polls}
        await asyncio.sleep(poll_seconds)


@tool("core", annotations={"readOnlyHint": True, "openWorldHint": True})
async def get_account_status() -> dict[str, Any]:
    """Show the account / plan state for the current API key (plan, billing readiness, scopes). Use it to explain 403s."""
    return await _call("GET", "/account-status")


@tool("core", annotations={"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True})
async def call_endpoint(
    method: Literal["GET", "POST", "PATCH", "DELETE"],
    path: str,
    query: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call any Business API v2 endpoint that has no dedicated tool (ASIC forms, entity monitoring, webhooks, staged ABN data…).

    path is RELATIVE to the v2 root, e.g. "/asic-484-forms", "/monitoring/entities/123", "/helpers/entitlement-questionnaire".
    Never include /api/v2 or /api/test/v2 — the server adds the right prefix for its environment.
    Find paths in resource bapi://docs/endpoints; field schemas are on https://businessapi.com.au/developers/api/<name>.
    POST = lodge (returns requestId; follow with get_request_status). Live writes are blocked unless BAPI_ALLOW_LIVE=true.
    Prefer the typed lodge_* tools for company / ABN / business-name registrations: they validate before sending.
    """
    if not path.startswith("/"):
        path = "/" + path
    if path.startswith("/api/"):
        raise ValueError(
            "path must be relative to the v2 root, e.g. '/company-registrations' (do not include /api/v2 or /api/test/v2)."
        )
    if method == "GET" and body:
        raise ValueError("GET requests take `query`, not `body`.")
    return await _call(method, path, params=query, body=body)


# --------------------------------------------------------------------------------------
# Lodgement registry: register the nine model-based lodge_* tools either with their full request schema
# (BAPI_INLINE_SCHEMAS=true) or, by default, as "lazy" tools whose `request` is a plain object validated
# server-side — the model fetches the schema on demand (get_request_schema / bapi://schemas/{form}).
# --------------------------------------------------------------------------------------

LodgementKey = Literal[
    "company_registration", "abn_registration", "business_name_registration", "business_name_renewal",
    "asic_484_form", "company_name_change", "company_deregistration", "asic_361_form", "asic_362_form",
]


@dataclass(frozen=True)
class LodgementSpec:
    key: str
    tool_name: str
    toolset: str
    model: type[BaseModel]
    impl: Callable[..., Awaitable[dict[str, Any]]]
    destructive: bool = False
    idempotent: bool = False


LODGEMENTS: tuple[LodgementSpec, ...] = (
    LodgementSpec("company_registration", "lodge_company_registration", "registrations", CompanyRegistrationRequest, _lodge_company_registration),
    LodgementSpec("abn_registration", "lodge_abn_registration", "registrations", AbnRegistrationRequest, _lodge_abn_registration, idempotent=True),
    LodgementSpec("business_name_registration", "lodge_business_name_registration", "registrations", BusinessNameRegistrationRequest, _lodge_business_name_registration),
    LodgementSpec("business_name_renewal", "lodge_business_name_renewal", "registrations", BusinessNameRenewalRequest, _lodge_business_name_renewal),
    LodgementSpec("asic_484_form", "lodge_asic_484_form", "agent_forms", Asic484FormRequest, _lodge_asic_484_form),
    LodgementSpec("company_name_change", "lodge_company_name_change", "agent_forms", CompanyNameChangeRequest, _lodge_company_name_change),
    LodgementSpec("company_deregistration", "lodge_company_deregistration", "agent_forms", CompanyDeregistrationRequest, _lodge_company_deregistration, destructive=True),
    LodgementSpec("asic_361_form", "lodge_asic_361_form", "agent_forms", Asic361FormRequest, _lodge_asic_361_form),
    LodgementSpec("asic_362_form", "lodge_asic_362_form", "agent_forms", Asic362FormRequest, _lodge_asic_362_form),
)
_SPEC_BY_KEY: dict[str, LodgementSpec] = {s.key: s for s in LODGEMENTS}


def _schema_for(spec: LodgementSpec) -> dict[str, Any]:
    return spec.model.model_json_schema()


def _format_validation_error(key: str, exc: ValidationError) -> str:
    lines = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err["loc"]) or "(request)"
        lines.append(f"{loc}: {err['msg']}")
    return (
        f"{len(lines)} validation error(s) in the {key} request — fix them and call again "
        f"(exact schema: get_request_schema(\"{key}\")):\n- " + "\n- ".join(lines)
    )


def _make_lazy(spec: LodgementSpec) -> Callable[..., Awaitable[dict[str, Any]]]:
    async def lazy(request: dict[str, Any], dry_run: bool = True) -> dict[str, Any]:
        try:
            obj = spec.model.model_validate(request)
        except ValidationError as exc:
            raise ToolError(_format_validation_error(spec.key, exc)) from None
        return await spec.impl(obj, dry_run)

    lazy.__name__ = lazy.__qualname__ = spec.tool_name
    lazy.__doc__ = (spec.impl.__doc__ or "").rstrip() + (
        f"\n\n`request` is a JSON object. Fetch its exact schema first with get_request_schema(\"{spec.key}\") "
        f"(or read resource bapi://schemas/{spec.key}), build the object, then call this tool; "
        "field errors come back as a readable list."
    )
    return lazy


for _spec in LODGEMENTS:
    _spec.impl.__name__ = _spec.impl.__qualname__ = _spec.tool_name  # so inline-mode validation errors name the tool, not _impl
    tool(
        _spec.toolset,
        name=_spec.tool_name,
        annotations={"readOnlyHint": False, "destructiveHint": _spec.destructive, "idempotentHint": _spec.idempotent, "openWorldHint": True},
    )(_spec.impl if INLINE_SCHEMAS else _make_lazy(_spec))


def get_request_schema(form: LodgementKey) -> dict[str, Any]:
    """Return the JSON Schema (with $defs) for a lodge_* tool's `request` object. Call it before building a lodgement.

    form -> tool: company_registration -> lodge_company_registration · abn_registration -> lodge_abn_registration ·
    business_name_registration -> lodge_business_name_registration · business_name_renewal -> lodge_business_name_renewal ·
    asic_484_form -> lodge_asic_484_form · company_name_change -> lodge_company_name_change ·
    company_deregistration -> lodge_company_deregistration · asic_361_form -> lodge_asic_361_form · asic_362_form -> lodge_asic_362_form.
    Field descriptions carry the vendor's rules and enums are the ASIC/ATO code tables; cross-field "Mandatory IF" rules are
    enforced when you call the tool and reported as field errors. The same schema is readable at bapi://schemas/{form}.
    """
    spec = _SPEC_BY_KEY[form]
    return {"form": form, "tool": spec.tool_name, "schema": _schema_for(spec)}


if {"registrations", "agent_forms"} & ENABLED_TOOLSETS:
    mcp.tool(
        name="get_request_schema",
        tags={"registrations", "agent_forms"},
        annotations={"readOnlyHint": True, "openWorldHint": False, "idempotentHint": True},
    )(get_request_schema)


@mcp.resource("bapi://schemas/{key}", name="Lodgement request schema", mime_type="application/json")
def schema_resource(key: str) -> str:
    """JSON Schema of a lodge_* request object; key = company_registration, abn_registration, business_name_registration,
    business_name_renewal, asic_484_form, company_name_change, company_deregistration, asic_361_form, asic_362_form."""
    spec = _SPEC_BY_KEY.get(key)
    if spec is None:
        raise ValueError(f"unknown form '{key}'; one of: {', '.join(_SPEC_BY_KEY)}")
    return json.dumps(_schema_for(spec), indent=2)


# --------------------------------------------------------------------------------------
# Resources (documentation the model can read) and a workflow prompt
# --------------------------------------------------------------------------------------

@mcp.resource("bapi://docs/overview", name="Business API overview", mime_type="text/markdown")
def docs_overview() -> str:
    """Auth, environments, response envelope, lodgement lifecycle, pricing, webhooks."""
    return OVERVIEW_DOC


@mcp.resource("bapi://docs/endpoints", name="Business API endpoint catalogue", mime_type="text/markdown")
def docs_endpoints() -> str:
    """Every v2 endpoint grouped by collection, with method, path, docs link and which tool covers it."""
    return ENDPOINTS_DOC


@mcp.resource("bapi://docs/company-registrations", name="Company registration (Form 201) guide", mime_type="text/markdown")
def docs_company() -> str:
    """Workflow, typical Pty Ltd values and the rules lodge_company_registration enforces."""
    return COMPANY_DOC


@mcp.resource("bapi://docs/abn-registrations", name="ABN registration guide", mime_type="text/markdown")
def docs_abn() -> str:
    """Entity types, workflow and the ATO/vendor rules lodge_abn_registration enforces."""
    return ABN_DOC


@mcp.resource("bapi://docs/business-name-registrations", name="Business name registration guide", mime_type="text/markdown")
def docs_business_name() -> str:
    """Holder types, payment flow (card link / invoice), cancelled-link recovery, name test guidance."""
    return BUSINESS_NAME_DOC


@mcp.resource("bapi://docs/ppsr-searches", name="PPSR search field reference", mime_type="text/markdown")
def docs_ppsr() -> str:
    """Request-body fields and result shape for /ppsr/searches."""
    return PPSR_DOC


@mcp.resource("bapi://docs/asic-484-forms", name="ASIC Form 484 guide", mime_type="text/markdown")
def docs_asic_484() -> str:
    """Change sections, status/holding codes, V455 share-totals rule and responses for Form 484."""
    return ASIC484_DOC


@mcp.resource("bapi://docs/entity-monitoring", name="Entity monitoring guide", mime_type="text/markdown")
def docs_monitoring() -> str:
    """What is monitored, lifecycle, idempotency/charging, limits, error codes, sandbox simulation."""
    return MONITORING_DOC


@mcp.resource("bapi://docs/asic-agent-forms", name="Name change, deregistration and agent forms guide", mime_type="text/markdown")
def docs_agent_forms() -> str:
    """Form 205A, 6010, 361, 362, RA52 shapes, statuses, code tables, and which endpoints have no public docs."""
    return AGENT_FORMS_DOC


@mcp.resource("bapi://docs/webhooks", name="Webhooks guide", mime_type="text/markdown")
def docs_webhooks() -> str:
    """Endpoint management, event table, delivery payload, signature verification, retries."""
    return WEBHOOKS_DOC


@mcp.resource("bapi://docs/asic-codes", name="ASIC code tables", mime_type="text/markdown")
def docs_asic_codes() -> str:
    """Company type/class/sub-class codes and valid combinations, jurisdictions, legal elements, roles."""
    return _asic_codes_doc()


@mcp.resource("bapi://docs/ato-codes", name="ATO code tables", mime_type="text/markdown")
def docs_ato_codes() -> str:
    """ABN entity types/sub-types, application reasons, associate position codes per sub-type, GST/PAYG/LCT codes."""
    return _ato_codes_doc()


@mcp.prompt(name="lodgement_workflow")
def lodgement_workflow(what: str = "a company registration") -> str:
    """Step-by-step plan for running a lodgement end-to-end through Business API."""
    return (
        f"You are helping with {what} via Business API (environment: {ENV}).\n"
        "1. Read bapi://docs/overview, then the guide for this lodgement (bapi://docs/company-registrations, "
        "bapi://docs/abn-registrations or bapi://docs/business-name-registrations) and the matching code tables.\n"
        "2. Run the free checks first (lookup_abn / lookup_acn / check_company_name_availability / "
        "check_business_name_availability with holder_name).\n"
        "3. Collect every required field from the user; call the lodge_* tool with dry_run=true. Fix validation errors "
        "with the user; show them the final payload and all fees; never echo TFNs back.\n"
        "4. Only after an explicit yes: call again with dry_run=false and keep the requestId.\n"
        "5. wait_for_request (or get_request_status / get_business_name_registration_status) until finished / rejected; "
        "explain errors[] and warnings in plain language; for card-paid business names hand the payer the payment link.\n"
    )


if __name__ == "__main__":
    mcp.run()
