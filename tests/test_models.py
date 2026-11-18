"""Every sample payload from the vendor docs must validate; the documented cross-field rules must be enforced."""
from __future__ import annotations

import json
from datetime import date

import pytest
from pydantic import TypeAdapter, ValidationError

from businessapi_mcp import models as m

TODAY = date.today().isoformat()


def _fresh_dates(payload: dict, old: str = "2026-06-01") -> dict:
    """Vendor samples carry fixed dates that drift out of 'within six months' windows; move them to today."""
    return json.loads(json.dumps(payload).replace(old, TODAY))


@pytest.mark.parametrize(
    "key, model",
    [
        ("company_registration", m.CompanyRegistrationRequest),
        ("abn_registration_117_006", m.AbnRegistrationRequest),
        ("abn_registration_051_027", m.AbnRegistrationRequest),
        ("abn_registration_161_150", m.AbnRegistrationRequest),
        ("abn_registration_212_071", m.AbnRegistrationRequest),
        ("abn_registration_210_196", m.AbnRegistrationRequest),
        ("business_name_registration_IND", m.BusinessNameRegistrationRequest),
        ("business_name_registration_IB", m.BusinessNameRegistrationRequest),
        ("business_name_registration_PTSH", m.BusinessNameRegistrationRequest),
        ("business_name_registration_USTR", m.BusinessNameRegistrationRequest),
        ("business_name_registration_JV", m.BusinessNameRegistrationRequest),
        ("business_name_renewal", m.BusinessNameRenewalRequest),
        ("company_name_change", m.CompanyNameChangeRequest),
        ("asic_361_form", m.Asic361FormRequest),
        ("asic_362_form", m.Asic362FormRequest),
        ("asic_ra52", m.AsicRa52Request),
    ],
)
def test_vendor_samples_validate(samples, key, model):
    model.model_validate(samples[key])


def test_asic_484_sample_validates(samples):
    m.Asic484FormRequest.model_validate(_fresh_dates(samples["asic_484_form"]))


def test_deregistration_sample_validates(samples):
    m.CompanyDeregistrationRequest.model_validate(samples["company_deregistration"])


def test_monitoring_entities_validate(samples):
    TypeAdapter(list[m.MonitoredEntityInput]).validate_python(samples["monitoring_entities"])


def test_payloads_prune_empty_values_and_add_test_flag(samples):
    payload = m.AbnRegistrationRequest.model_validate(samples["abn_registration_117_006"]).to_payload(test_transmission=True)
    assert payload["testTransmissionIndicator"] is True
    assert "entitySubTypeAdditional" not in payload  # was null in the sample


def test_form_484_is_upper_cased(samples):
    sample = _fresh_dates(samples["asic_484_form"])
    sample["generalInformation"]["companyName"] = "my company pty ltd"
    payload = m.Asic484FormRequest.model_validate(sample).to_payload(test_transmission=True)
    assert payload["generalInformation"]["companyName"] == "MY COMPANY PTY LTD"


@pytest.mark.parametrize(
    "key, model, mutate, message",
    [
        ("company_registration", m.CompanyRegistrationRequest, lambda p: p["generalInformation"].__setitem__("companyType", "APUB"), "not a valid ASIC combination"),
        ("abn_registration_117_006", m.AbnRegistrationRequest, lambda p: p["applicationDetail"].__setitem__("tfnApplicationIndicator", True), "sole traders cannot apply for a TFN"),
        ("abn_registration_051_027", m.AbnRegistrationRequest, lambda p: p["associates"]["individuals"][0].__setitem__("positionHeld", ["TRT"]), "not allowed for sub-type 027"),
        ("business_name_registration_IB", m.BusinessNameRegistrationRequest, lambda p: p["entity"]["organisation"].pop("acn"), "IB needs entity.organisation.acn"),
        ("business_name_renewal", m.BusinessNameRenewalRequest, lambda p: p["generalInformation"].__setitem__("term", 2), "term must be 1 or 3"),
        ("company_name_change", m.CompanyNameChangeRequest, lambda p: p["generalInformation"].__setitem__("proposedName", None), "proposedName is required"),
        ("company_deregistration", m.CompanyDeregistrationRequest, lambda p: p["declarations"].__setitem__("noOutstandingLiabilitiesIndicator", False), "every declaration to be true"),
        ("asic_361_form", m.Asic361FormRequest, lambda p: p["signature"].__setitem__("agentAuthorisedIndicator", False), "must both be true"),
        ("asic_362_form", m.Asic362FormRequest, lambda p: p["generalInformation"].__setitem__("type", "X"), "'A' or 'C'"),
    ],
)
def test_documented_rules_are_enforced(samples, key, model, mutate, message):
    payload = json.loads(json.dumps(samples[key]))
    mutate(payload)
    with pytest.raises(ValidationError) as exc:
        model.model_validate(payload)
    assert message in str(exc.value)


def test_form_484_v455_share_totals(samples):
    sample = _fresh_dates(samples["asic_484_form"])
    sample["members"][0]["number"] = 99
    with pytest.raises(ValidationError, match="ASIC V455"):
        m.Asic484FormRequest.model_validate(sample)
    sample = _fresh_dates(samples["asic_484_form"])
    sample.pop("members")
    with pytest.raises(ValidationError, match="require both shareStructure and members"):
        m.Asic484FormRequest.model_validate(sample)


def test_checksums():
    assert m.abn_is_valid("51 824 753 556")
    assert not m.abn_is_valid("51824753557")
    assert m.acn_is_valid("004085616") and m.acn_is_valid("000000019")
    assert not m.acn_is_valid("123456789")
    assert m.tfn_is_valid("123456782") and not m.tfn_is_valid("123456789")


@pytest.mark.parametrize(
    "url, ok",
    [
        ("https://example.com/hooks", True),
        ("https://8.8.8.8/x", True),
        ("http://example.com/hooks", False),
        ("https://localhost/x", False),
        ("https://10.0.0.5/x", False),
        ("https://100.64.1.1/x", False),
    ],
)
def test_webhook_url_rules(url, ok):
    if ok:
        assert m.validate_webhook_url(url) == url
    else:
        with pytest.raises(ValueError):
            m.validate_webhook_url(url)
