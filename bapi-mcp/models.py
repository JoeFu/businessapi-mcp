"""
Pydantic request models for the Business API lodgement endpoints.

Field names are the API's own camelCase names, so the JSON schema the LLM sees matches the vendor
docs one-to-one (https://businessapi.com.au/developers/api/<name>). Code tables come from
/developers/references/asic-table-references and /developers/references/ato-table-references.

The validators encode the "Mandatory: IF ..." rules from the docs, so a bad payload fails locally with a
readable message instead of a 400 (or worse, a rejected lodgement) from the registry.
"""
from __future__ import annotations

from datetime import date as Date, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ======================================================================================
# Shared helpers
# ======================================================================================

TODAY = Date.today


def digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def abn_is_valid(abn: str) -> bool:
    d = digits(abn)
    if len(d) != 11:
        return False
    weights = (10, 1, 3, 5, 7, 9, 11, 13, 15, 17, 19)
    nums = [int(c) for c in d]
    nums[0] -= 1
    return sum(n * w for n, w in zip(nums, weights)) % 89 == 0


def acn_is_valid(acn: str) -> bool:
    d = digits(acn)
    if len(d) != 9:
        return False
    weights = (8, 7, 6, 5, 4, 3, 2, 1)
    total = sum(int(c) * w for c, w in zip(d[:8], weights))
    return (10 - total % 10) % 10 == int(d[8])


def tfn_is_valid(tfn: str) -> bool:
    d = digits(tfn)
    if len(d) == 9:
        weights = (1, 4, 3, 7, 5, 8, 6, 9, 10)
    elif len(d) == 8:
        weights = (10, 7, 8, 4, 6, 3, 5, 1)
    else:
        return False
    return sum(int(c) * w for c, w in zip(d, weights)) % 11 == 0


def _clean_abn(v: str | None) -> str | None:
    if v in (None, ""):
        return None
    d = digits(v)
    if not abn_is_valid(d):
        raise ValueError(f"'{v}' is not a valid ABN (11 digits, checksum)")
    return d


def _clean_acn(v: str | None) -> str | None:
    if v in (None, ""):
        return None
    d = digits(v)
    if not acn_is_valid(d):
        raise ValueError(f"'{v}' is not a valid ACN/ARBN (9 digits, checksum)")
    return d


def _clean_tfn(v: str | None) -> str | None:
    if v in (None, ""):
        return None
    if not v.isdigit():
        raise ValueError("TFN must be digits only")
    if not tfn_is_valid(v):
        raise ValueError("TFN fails the checksum")
    return v


def _require_acn(v: str | None) -> str:
    d = _clean_acn(v)
    if not d:
        raise ValueError("acn is required")
    return d


_PO_BOX_PREFIXES = (
    "PO BOX", "P.O. BOX", "GPO", "LOCKED BAG", "PRIVATE BAG", "C/", "CARE OF", "ATTN", "RMB", "RSD", "CNR",
)


def _looks_like_po_box(line: str | None) -> bool:
    return bool(line) and line.strip().upper().startswith(_PO_BOX_PREFIXES)


def _prune(value: Any) -> Any:
    """Drop "", null, {} and [] recursively. The vendor's samples pad unused optional blocks with empty
    strings / empty objects; after pruning, 'optional' really means 'absent' and required-field errors are clear."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            pv = _prune(v)
            if pv is None or pv == "" or pv == {} or pv == []:
                continue
            out[k] = pv
        return out
    if isinstance(value, list):
        items = [_prune(v) for v in value]
        return [v for v in items if not (v is None or v == "" or v == {} or v == [])]
    return value


class _Pruned(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def _prune_empty(cls, data: Any) -> Any:
        return _prune(data) if isinstance(data, dict) else data


class _Strict(_Pruned):
    """Unknown keys are rejected: catches typos before the registry does."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class _Loose(_Pruned):
    """Blocks whose full field list the vendor does not publish: unknown keys pass through."""

    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)


# ======================================================================================
# ASIC code tables (Company registration, Form 201)
# ======================================================================================

Jurisdiction = Literal["ACT", "CCK", "CXR", "JBT", "NSW", "NFK", "NT", "QLD", "SA", "TAS", "VIC", "WA"]
"""ASIC Table 0035 — states, territories and external territories."""

StateCode = Literal["ACT", "NSW", "NT", "QLD", "SA", "TAS", "VIC", "WA"]
"""ASIC Table 0004 — states and territories only."""

CompanyType = Literal["APUB", "APTY", "FNOS", "RACN"]
"""ASIC Table 0013: APUB public, APTY proprietary, FNOS foreign company, RACN registered Australian body."""

CompanyClass = Literal["LMGT", "LMSG", "LMSH", "NLIA", "UNLM"]
"""ASIC Table 0001: LMGT limited by guarantee, LMSG by shares & guarantee, LMSH by shares, NLIA no liability, UNLM unlimited."""

CompanySubClass = Literal["PNPC", "PSTC", "HUNT", "PROP", "LISN", "LISS", "LIST", "STFI", "NLTD", "ULSN", "ULSS", "ULST"]
"""ASIC Table 0002. Ordinary Pty Ltd = PROP. PNPC non-profit pty, PSTC super-trustee pty, HUNT home-unit pty,
LIST listed public, ULST unlisted public, ULSN unlisted non-profit, ULSS unlisted super-trustee,
LISN/LISS s.383 licence to omit 'Limited', NLTD s.150 company, STFI small transferring financial institution."""

LegalElement = Literal["PTY", "PTY.", "PROPRIETARY", "LTD", "LTD.", "LIMITED", "NL", "N.L.", "NO LIABILITY"]
OfficeholderRole = Literal["DIR", "SEC"]
SignatoryRole = Literal["DIR", "SEC", "ALT", "AGT"]

# ASIC Table 0003: sub-class -> {class -> type}
COMPANY_COMBINATIONS: dict[str, dict[str, str]] = {
    "PNPC": {"LMSH": "APTY", "LMSG": "APTY"},
    "PSTC": {"LMSH": "APTY", "LMSG": "APTY", "UNLM": "APTY"},
    "PROP": {"LMSH": "APTY", "LMSG": "APTY", "UNLM": "APTY"},
    "HUNT": {"LMSH": "APTY", "LMSG": "APTY", "UNLM": "APTY"},
    "LISN": {"LMSH": "APUB", "LMGT": "APUB", "LMSG": "APUB"},
    "LISS": {"LMGT": "APUB"},
    "LIST": {"LMSH": "APUB", "LMSG": "APUB", "NLIA": "APUB", "UNLM": "APUB"},
    "NLTD": {"LMSH": "APUB", "LMGT": "APUB"},
    "STFI": {"LMSH": "APUB", "LMGT": "APUB", "LMSG": "APUB"},
    "ULSS": {"LMSH": "APUB", "LMGT": "APUB", "LMSG": "APUB", "UNLM": "APUB"},
    "ULST": {"LMSH": "APUB", "LMGT": "APUB", "LMSG": "APUB", "NLIA": "APUB", "UNLM": "APUB"},
    "ULSN": {"LMSH": "APUB", "LMGT": "APUB", "LMSG": "APUB"},
}

_UNACCEPTABLE_NAME_WORDS = {"&", "COMPANY", "LIMITED", "LTD", "LTD.", "P/L", "PROPRIETARY", "PTY", "PTY."}


class PersonName(_Strict):
    """A natural person's name as ASIC wants it (no company words such as PTY or LTD)."""

    familyName: str = Field(..., max_length=30)
    givenName1: str = Field(..., max_length=20, description="First given name")
    givenName2: str | None = Field(None, max_length=20)
    givenName3: str | None = Field(None, max_length=20)

    @model_validator(mode="after")
    def _no_company_words(self):
        for part in (self.familyName, self.givenName1, self.givenName2 or "", self.givenName3 or ""):
            for word in part.upper().split():
                if word in _UNACCEPTABLE_NAME_WORDS:
                    raise ValueError(f"'{word}' is not allowed in a personal name (ASIC Table 0023)")
        return self


class OrganisationRef(_Strict):
    """A company / body named in the application."""

    name: str = Field(..., max_length=200)
    acn: str | None = Field(None, description="9-digit ACN or ARBN (spaces allowed; checksum validated)")

    _acn = field_validator("acn")(_clean_acn)


class AuStreetAddress(_Strict):
    """Australian street address for the registered office / principal place of business (no PO boxes)."""

    careOf: str | None = Field(None, max_length=50, description="C/- line, if any")
    line2: str | None = Field(None, max_length=50, description="Unit / floor / building")
    street: str = Field(..., max_length=52, description='Street number and name, e.g. "9 KENT STREET"')
    locality: str = Field(..., max_length=30, description="Suburb / town")
    state: Jurisdiction
    postcode: str = Field(..., pattern=r"^\d{4}$")


class Address(_Strict):
    """Address of a person or organisation; may be overseas. Australian addresses need state + postcode."""

    careOf: str | None = Field(None, max_length=50)
    line2: str | None = Field(None, max_length=50, description="Unit / floor / building")
    street: str = Field(..., max_length=52)
    locality: str = Field(..., max_length=30, description="Suburb / town / city")
    state: Jurisdiction | None = Field(None, description="Required when country is AU")
    postcode: str | None = Field(None, pattern=r"^\d{4}$", description="Required when country is AU")
    country: str = Field("AU", min_length=2, max_length=2, description="ISO 3166-1 alpha-2 code, e.g. AU, NZ, GB")

    @field_validator("country")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    @model_validator(mode="after")
    def _au_needs_state_and_postcode(self):
        if self.country == "AU" and (not self.state or not self.postcode):
            raise ValueError("Australian addresses need both state and postcode")
        return self


class GeneralInformation(_Strict):
    """Company name, type/class/sub-class and jurisdiction."""

    companyName: str | None = Field(
        None, max_length=200,
        description='Full proposed name INCLUDING legal elements, e.g. "ACME TRADING PTY LTD". Required unless acnNameIndicator is true.',
    )
    acnNameIndicator: bool = Field(..., description="true = the company will be named by its ACN (e.g. 'A.C.N. 123 456 789 PTY LTD')")
    acnNameLegalElements: LegalElement | None = Field(None, description="Legal elements for an ACN-named company (required when acnNameIndicator is true)")
    companyType: CompanyType = Field(..., description="APTY for a Pty Ltd, APUB for a public company")
    companyClass: CompanyClass = Field(..., description="LMSH for an ordinary company limited by shares")
    companySubClass: CompanySubClass = Field(..., description="PROP for an ordinary proprietary company")
    jurisdiction: Jurisdiction = Field(..., description="State/territory of registration")
    governedByConstitution: bool | None = Field(None, description="Required for APUB companies of class NLIA/LMGT with sub-class ULSN/ULSS")
    nonCashSharesIndicator: bool | None = Field(None, description="Required for APUB + LMSH: will any shares be issued for non-cash consideration?")
    nameReservedIndicator: bool | None = Field(None, description="true if the name was reserved with a Form 410 (then supply nameReserved)")
    nameIdenticalIndicator: bool | None = Field(None, description="true if the name is identical to an existing registered business name")
    nameIdenticalAbn: str | None = Field(None, description="ABN of the business-name holder; required when nameIdenticalIndicator is true")
    residentialOfficeholders: bool = Field(..., description="Are all officeholder addresses their usual residential addresses?")

    _abn = field_validator("nameIdenticalAbn")(_clean_abn)

    @model_validator(mode="after")
    def _rules(self):
        if not self.acnNameIndicator and not self.companyName:
            raise ValueError("companyName is required when acnNameIndicator is false")
        if self.acnNameIndicator and not self.acnNameLegalElements:
            raise ValueError("acnNameLegalElements is required when acnNameIndicator is true")
        if self.nameIdenticalIndicator and not self.nameIdenticalAbn:
            raise ValueError("nameIdenticalAbn is required when nameIdenticalIndicator is true")
        if self.companyType in ("APTY", "APUB"):
            allowed = COMPANY_COMBINATIONS.get(self.companySubClass, {})
            if allowed.get(self.companyClass) != self.companyType:
                raise ValueError(
                    f"companyType/companyClass/companySubClass {self.companyType}/{self.companyClass}/{self.companySubClass} "
                    f"is not a valid ASIC combination (Table 0003). Valid classes for {self.companySubClass}: "
                    + ", ".join(f"{c} ({t})" for c, t in allowed.items())
                )
        if self.companyType == "APUB" and self.companyClass == "LMSH" and self.nonCashSharesIndicator is None:
            raise ValueError("nonCashSharesIndicator is required for APUB + LMSH companies")
        if (
            self.companyType == "APUB" and self.companyClass in ("NLIA", "LMGT")
            and self.companySubClass in ("ULSN", "ULSS") and self.governedByConstitution is None
        ):
            raise ValueError("governedByConstitution is required for this public company class/sub-class")
        return self


class NameReserved(_Strict):
    """Details of the Form 410 name reservation (who reserved it + reservation document number)."""

    individual: PersonName | None = None
    organisation: OrganisationRef | None = None
    documentNumber: str = Field(..., max_length=9, description="Form 410 reservation document number")

    @model_validator(mode="after")
    def _one_party(self):
        if bool(self.individual) == bool(self.organisation):
            raise ValueError("Give exactly one of individual or organisation")
        return self


class NameIdentical(_Strict):
    state: StateCode
    registrationNumber: str = Field(..., max_length=10, description="State registration number of the business name")


class OpenHours(_Strict):
    openHour: str = Field(..., pattern=r"^\d{4}$", description="HHMM 24h, e.g. 0900")
    closeHour: str = Field(..., pattern=r"^\d{4}$", description="HHMM 24h, e.g. 1700")


class RegisteredOffice(_Strict):
    """Registered office address, occupier and opening hours."""

    address: AuStreetAddress
    occupyIndicator: bool = Field(..., description="Will the new company itself occupy the premises?")
    occupierName: str | None = Field(None, max_length=200, description="Required when occupyIndicator is false")
    occupierConsent: bool | None = Field(None, description="Required when occupyIndicator is false; must be true")
    openStandardHoursIndicator: bool | None = Field(None, description="Required for APUB companies")
    openHours: list[OpenHours] | None = Field(None, description="Required when openStandardHoursIndicator is false")

    @model_validator(mode="after")
    def _rules(self):
        if not self.occupyIndicator and (not self.occupierName or self.occupierConsent is None):
            raise ValueError("occupierName and occupierConsent are required when occupyIndicator is false")
        if self.openStandardHoursIndicator is False and not self.openHours:
            raise ValueError("openHours is required when openStandardHoursIndicator is false")
        return self


class PrincipalPlaceOfBusiness(_Strict):
    address: AuStreetAddress


class UltimateHoldingCompany(_Strict):
    name: str = Field(..., max_length=200)
    acn: str | None = Field(None, description="Required when country is AU")
    abn: str | None = None
    country: str = Field(..., min_length=2, max_length=60, description="ISO alpha-2 code preferred (NZ); the vendor sample uses a name (NEW ZEALAND)")

    _acn = field_validator("acn")(_clean_acn)
    _abn = field_validator("abn")(_clean_abn)

    @model_validator(mode="after")
    def _au_needs_acn(self):
        if self.country.upper() in ("AU", "AUSTRALIA") and not self.acn:
            raise ValueError("acn is required for an Australian ultimate holding company")
        return self


class BirthDetails(_Strict):
    date: Date = Field(..., description="YYYY-MM-DD; officeholder must be 18+")
    locality: str = Field(..., max_length=30, description="Birth suburb (Australia) or city (overseas)")
    localityQualifier: str = Field(..., max_length=3, description="Birth state code (ACT, NSW, …) or 2-letter ISO country code when born overseas")
    intlIndicator: bool | None = Field(None, description="true when born overseas (localityQualifier is then a country code)")

    @field_validator("date")
    @classmethod
    def _adult(cls, v: Date) -> Date:
        if v > TODAY() - timedelta(days=18 * 365 + 5):
            raise ValueError("officeholder must be at least 18 years old")
        return v


class OfficeHolder(_Strict):
    """A director (DIR) or secretary (SEC). A proprietary company needs at least one director resident in Australia."""

    name: PersonName
    birthDetails: BirthDetails
    address: Address = Field(..., description="Usual residential address")
    formerName: PersonName | None = None
    roles: list[OfficeholderRole] = Field(..., min_length=1, description='e.g. ["DIR"] or ["DIR", "SEC"]')


class ShareStructure(_Strict):
    """One share class. Money fields are integers whose last two digits are cents (100 = $1.00) per the
    vendor's validation rules — the vendor's own sample payload is ambiguous on this, so confirm in the sandbox."""

    classCode: str = Field(..., max_length=4, description="e.g. ORD")
    classTitle: str = Field(..., max_length=60, description="e.g. ORDINARY")
    numberIssued: int = Field(..., ge=1)
    totalAmountPaid: int = Field(..., ge=0)
    totalAmountUnpaid: int = Field(..., ge=0)


class Party(_Strict):
    """An individual OR an organisation (exactly one) with an address — used for shareholders and members."""

    individual: PersonName | None = None
    organisation: OrganisationRef | None = None
    address: Address

    @model_validator(mode="after")
    def _one_party(self):
        if bool(self.individual) == bool(self.organisation):
            raise ValueError("Give exactly one of individual or organisation")
        return self


class ShareAllocation(_Strict):
    """Shares taken up by one holding (one or more joint holders) in one class."""

    classCode: str = Field(..., max_length=4, description="Must match a shareStructure classCode")
    numberIssued: int = Field(..., ge=1, description="Number of shares in this holding")
    fullyPaidIndicator: bool
    totalAmountPaid: int = Field(..., ge=0, description="Cents-style integer, see ShareStructure")
    totalAmountUnpaid: int = Field(..., ge=0)
    amountPaidPerShare: int = Field(..., ge=0)
    amountUnpaidPerShare: int = Field(..., ge=0)
    beneficialOwnerIndicator: bool = Field(..., description="Are the holders the beneficial owners?")
    beneficialOwner: str | None = Field(None, max_length=200, description="Required when beneficialOwnerIndicator is false")
    holdingOwners: list[Party] = Field(..., min_length=1)

    @model_validator(mode="after")
    def _rules(self):
        if self.beneficialOwnerIndicator is False and not self.beneficialOwner:
            raise ValueError("beneficialOwner is required when beneficialOwnerIndicator is false")
        return self


class Applicant(_Strict):
    """Who is lodging the application (an individual, or an organisation plus its signatory)."""

    individual: PersonName | None = None
    organisation: OrganisationRef | None = None
    organisationSignatoryIndividual: PersonName | None = Field(None, description="Required when the applicant is an organisation")
    organisationSignatoryRole: SignatoryRole | None = Field(None, description="DIR, SEC, ALT or AGT; required when the applicant is an organisation")
    address: Address
    dateSigned: Date = Field(..., description="YYYY-MM-DD, not in the future")
    declaresTrueAndCorrectIndicator: bool = Field(..., description="Must be true")

    @model_validator(mode="after")
    def _rules(self):
        if bool(self.individual) == bool(self.organisation):
            raise ValueError("Give exactly one of individual or organisation")
        if self.organisation and (not self.organisationSignatoryIndividual or not self.organisationSignatoryRole):
            raise ValueError("organisationSignatoryIndividual and organisationSignatoryRole are required for an organisation applicant")
        if self.dateSigned > TODAY():
            raise ValueError("dateSigned cannot be in the future")
        if not self.declaresTrueAndCorrectIndicator:
            raise ValueError("declaresTrueAndCorrectIndicator must be true")
        return self


class Admin(_Strict):
    manualReviewIndicator: bool = Field(..., description="Ask Business API staff to review before transmission")
    manualReviewText: str | None = Field(None, description="Required when manualReviewIndicator is true")

    @model_validator(mode="after")
    def _rules(self):
        if self.manualReviewIndicator and not self.manualReviewText:
            raise ValueError("manualReviewText is required when manualReviewIndicator is true")
        return self


class CompanyRegistrationRequest(_Strict):
    """ASIC Form 201 — register a new Australian company. POST /company-registrations."""

    generalInformation: GeneralInformation
    nameReserved: NameReserved | None = Field(None, description="Required when generalInformation.nameReservedIndicator is true")
    nameIdentical: list[NameIdentical] | None = Field(None, description="State registrations of the identical business name (when no nameIdenticalAbn)")
    registeredOffice: RegisteredOffice
    principalPlaceOfBusiness: PrincipalPlaceOfBusiness
    ultimateHoldingCompany: UltimateHoldingCompany | None = None
    officeHolders: list[OfficeHolder] = Field(..., min_length=1)
    shareStructure: list[ShareStructure] | None = Field(None, description="Required unless companyClass is LMGT")
    shareAllocation: list[ShareAllocation] | None = Field(None, description="Required unless companyClass is LMGT")
    nonShareMembersAmount: int | None = Field(None, description="Members' guarantee amount (cents-style); required for LMGT")
    nonShareMembers: list[Party] | None = Field(None, description="Required for LMGT")
    applicant: Applicant
    admin: Admin

    @model_validator(mode="after")
    def _rules(self):
        gi = self.generalInformation
        if gi.nameReservedIndicator and not self.nameReserved:
            raise ValueError("nameReserved is required when generalInformation.nameReservedIndicator is true")
        if gi.companyClass == "LMGT":
            if self.nonShareMembersAmount is None or not self.nonShareMembers:
                raise ValueError("nonShareMembersAmount and nonShareMembers are required for a company limited by guarantee (LMGT)")
        else:
            if not self.shareStructure or not self.shareAllocation:
                raise ValueError("shareStructure and shareAllocation are required unless companyClass is LMGT")
            codes = [s.classCode for s in self.shareStructure]
            if len(set(codes)) != len(codes):
                raise ValueError("shareStructure classCodes must be unique")
            if codes != sorted(codes):
                raise ValueError("shareStructure must be in alphabetical classCode order")
            for alloc in self.shareAllocation:
                if alloc.classCode not in codes:
                    raise ValueError(f"shareAllocation classCode {alloc.classCode} has no matching shareStructure entry")
        if not any("DIR" in oh.roles for oh in self.officeHolders):
            raise ValueError("at least one officeholder must be a director (DIR)")
        if gi.companyType == "APUB" and self.registeredOffice.openStandardHoursIndicator is None:
            raise ValueError("registeredOffice.openStandardHoursIndicator is required for public companies")
        return self

    def to_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


# ======================================================================================
# ATO code tables (ABN registration)
# ======================================================================================

AtoEntityType = Literal["051", "117", "161", "210", "212"]
"""ATO Table 0001: 051 company, 117 individual/sole trader, 161 partnership, 210 superannuation fund, 212 trust."""

AtoEntitySubType = Literal[
    "006", "009", "020", "021", "022", "027", "032", "039", "045", "054", "059", "071", "072", "077", "087", "088",
    "089", "090", "112", "120", "138", "145", "150", "156", "168", "174", "179", "184", "196", "205", "221",
]
"""ATO Table 0002. Common: 006 sole trader · 027 private company · 032 public company · 087 family partnership ·
150 other partnership · 071 discretionary investment trust · 077 discretionary trading trust · 089 fixed unit trust ·
196 self-managed super fund · 120 limited partnership (NB: entityType 051, not 161)."""

AtoEntitySubTypeAdditional = Literal["UPC", "CLP", "CHT", "LBG"]
"""ATO Table 0003: UPC unlisted public company, CLP corporate limited partnership, CHT closely held trust, LBG limited by guarantee."""

SUBTYPES_BY_ENTITY_TYPE: dict[str, set[str]] = {
    "117": {"006"},
    "051": {"027", "032", "045", "145", "205", "174", "156", "120"},
    "161": {"087", "150"},
    "212": {"059", "039", "071", "072", "077", "089", "090", "112", "184", "179"},
    "210": {"196", "221", "138", "168", "009"},
}

ApplicationReasonCode = Literal["CBS", "CON", "INV", "NEW", "OTH", "PAY", "PUR", "RER", "RED"]
"""ATO Table 0004: NEW new business · CBS change in business structure · PUR purchased existing business ·
INV investment · CON contractor · PAY to receive payment for services · RER/RED re-commenced · OTH other."""

PositionCode = Literal["DIR", "DCT", "PTN", "MSF", "POF", "TRT", "TRB", "CSH", "CSY", "OFB"]
"""ATO Table 0013: DIR director · DCT director of corporate trustee · PTN partner · MSF SMSF member · POF public officer ·
TRT trustee · TRB beneficiary · CSH shareholder · CSY secretary · OFB office bearer."""

GenderCode = Literal["0", "1", "2", "3"]
"""ATO Table 0014: 1 male, 2 female, 3 intersex/indeterminate, 0 not stated."""

GstTurnoverRange = Literal[
    "$0 - $74,999", "$75,000 - $149,999", "$150,000 - $1,999,999", "$2,000,000 - $9,999,999",
    "$10,000,000 - $19,999,999", "$20 million and over",
]
GstFrequency = Literal["Annually", "HalfYearly", "Monthly", "Quarterly"]
GstReason = Literal["Legislative Requirement", "Voluntary"]
PaygwChannel = Literal["E", "EAS", "ECI", "ELS", "EML", "FDF", "INP", "MGV", "MIPS", "OTH", "P", "PE", "PH", "RDCC", "RO", "SBR", "SMS", "WEB"]
PaygwSummaryMethod = Literal["ATO", "Both", "Print"]
LctActivity = Literal["Importer", "Manufacturer", "Retailer", "Wholesaler"]
SuperBenefitCode = Literal["A", "D", "U", "E"]
"""ATO Table 0012: A accumulation, D defined benefit, U unfunded defined benefit, E hybrid."""
LanguageCode = Literal[
    "ARA", "ASS", "CHI", "CRO", "DAR", "DIN", "ENG", "FAR", "GRK", "HIN", "IND", "ITA", "JAP", "KAR", "KHM", "KOR",
    "MAC", "MLT", "BLK", "OTH", "POL", "POR", "RUS", "SRB", "SOM", "SPA", "TTY", "THA", "TUR", "VNM",
]


def allowed_positions(sub_type: str, additional: str | None) -> set[str]:
    """Associate roles the ATO accepts for an entity sub-type (vendor's 'Associate roles' table)."""
    cht = additional == "CHT"
    table: dict[str, set[str]] = {
        "006": set(),
        "087": {"PTN"}, "150": {"PTN"},
        "027": {"CSH", "CSY", "DIR", "POF"}, "032": {"CSH", "CSY", "DIR", "POF"},
        "045": {"OFB", "POF"}, "156": {"OFB", "POF"},
        "145": {"CSY", "DIR", "OFB", "POF", "DCT"},
        "205": {"DIR", "OFB", "POF"},
        "120": {"PTN", "DIR", "POF", "DCT"},
        "174": {"TRT", "POF", "DCT"} | ({"TRB"} if cht else set()),
        "039": {"TRT", "TRB", "POF"} if cht else {"TRT", "POF", "DCT"},
        "059": {"TRT", "TRB", "POF", "DCT"} if cht else {"TRT", "DCT", "POF"},
        "071": {"TRT", "TRB", "POF", "DCT"}, "072": {"TRT", "TRB", "POF", "DCT"}, "077": {"TRT", "TRB", "POF", "DCT"},
        "089": {"TRT", "TRB", "DCT"},
        "090": {"TRT", "POF", "DCT"} | ({"TRB"} if cht else set()),
        "112": {"TRT", "POF", "DCT"} | ({"TRB"} if cht else set()),
        "179": {"TRT", "DCT", "POF"}, "184": {"TRT", "DCT", "POF"},
        "196": {"TRT", "MSF", "DCT", "POF"},
        "009": {"TRT", "POF", "DCT"}, "138": {"TRT", "POF", "DCT"}, "168": {"TRT", "POF", "DCT"}, "221": {"TRT", "POF", "DCT"},
    }
    return table.get(sub_type, set())


class EntitlementAnswer(_Loose):
    questionId: int
    answerId: int
    nextQuestionId: int = Field(..., description="0 on the last answer")
    outcomeId: int | None = None


class Entitlement(_Loose):
    """The ABN entitlement questionnaire the applicant completed (get questions from GET /helpers/entitlement-questionnaire)."""

    firstQuestion: int
    outcomeId: int
    outcome: bool = Field(..., description="true = entitled to an ABN")
    questionnaire: list[EntitlementAnswer] = Field(..., min_length=1)


class AtoAddress(_Loose):
    """ATO address: street/line2 ≤ 38 chars, locality ≤ 46. Australian addresses need locality, state, postcode."""

    line2: str | None = Field(None, max_length=38, description="Unit / level / building")
    street: str = Field(..., max_length=38, description="Street number and name")
    locality: str | None = Field(None, max_length=46)
    state: StateCode | None = None
    postcode: str | None = Field(None, pattern=r"^\d{4}$")
    countryCode: str = Field("AU", min_length=2, max_length=2, description="ISO alpha-2, e.g. AU")

    @field_validator("countryCode")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    @model_validator(mode="after")
    def _au_fields(self):
        if self.countryCode == "AU" and not (self.locality and self.state and self.postcode):
            raise ValueError("Australian addresses need locality, state and postcode")
        return self

    def is_po_box(self) -> bool:
        return _looks_like_po_box(self.street) or _looks_like_po_box(self.line2)


class AbnIndividual(_Loose):
    """The sole-trader applicant. Residential address fields are checked by Business API but not sent to the ATO."""

    title: str | None = Field(None, description="Mr, Mrs, Ms, Dr …")
    familyName: str
    givenName: str
    otherGivenName: str | None = None
    dateOfBirth: Date
    genderCode: GenderCode | None = None
    line2: str | None = Field(None, max_length=38)
    street: str | None = Field(None, max_length=38, description="Residential street (required when no tfn)")
    locality: str | None = Field(None, max_length=46)
    state: StateCode | None = None
    postcode: str | None = Field(None, pattern=r"^\d{4}$")
    countryCode: str | None = Field(None, min_length=2, max_length=2)

    @field_validator("dateOfBirth")
    @classmethod
    def _past(cls, v: Date) -> Date:
        if v > TODAY():
            raise ValueError("dateOfBirth cannot be in the future")
        return v

    def has_address(self) -> bool:
        return bool(self.street and self.locality and self.state and self.postcode)


class AbnOrganisation(_Loose):
    name: str = Field(..., description="Legal name of the company / partnership / trust / fund")
    acn: str | None = Field(None, description="Required for sub-types 027 and 032 (the company must already exist)")

    _acn = field_validator("acn")(_clean_acn)


class AbnApplicant(_Loose):
    """Exactly one of individual (sole trader) or organisation (everything else)."""

    individual: AbnIndividual | None = None
    organisation: AbnOrganisation | None = None
    tfn: str | None = Field(None, description="Applicant's existing TFN, digits only; send it when applicationDetail.tfnIndicator is true")

    _tfn = field_validator("tfn")(_clean_tfn)

    @model_validator(mode="after")
    def _one(self):
        if bool(self.individual) == bool(self.organisation):
            raise ValueError("Give exactly one of individual or organisation")
        return self


class ApplicationDetail(_Loose):
    tfnIndicator: bool = Field(..., description="Does the applicant already have a TFN? (then send applicant.tfn)")
    abnApplicationIndicator: bool = Field(True, description="Applying for an ABN (always true here)")
    tfnApplicationIndicator: bool = Field(False, description="Also issue a TFN (companies, partnerships, trusts only — never sole traders)")


class ApplicationReason(_Loose):
    code: ApplicationReasonCode
    firstTimeDirectorIndicator: bool | None = None
    firstTimeSelfEmployedIndicator: bool | None = None


class BusinessActivity(_Loose):
    code: str | None = Field(None, description="Main business activity (ANZSIC-style) code, e.g. 7749; use GET /helpers/business-industries")
    description: str | None = Field(None, description="Description matching the code")
    abnStartDate: Date = Field(..., description="After 1999-01-01, at most 6 months ahead; not in the future if the applicant has held an ABN before")
    abnEndDate: Date | None = None
    governmentIndicator: bool | None = Field(None, description="Government entity? Required for every entity except sole traders")
    agriculturalIndicator: bool | None = None

    @field_validator("abnStartDate")
    @classmethod
    def _window(cls, v: Date) -> Date:
        if v <= Date(1999, 1, 1):
            raise ValueError("abnStartDate must be after 1999-01-01")
        if v > TODAY() + timedelta(days=183):
            raise ValueError("abnStartDate can be at most 6 months ahead")
        return v


class MainAddresses(_Loose):
    """Business (physical, no PO box) and postal addresses plus contact details."""

    business: AtoAddress = Field(..., description="Physical business address; a sole trader working from home gives their home")
    postal: AtoAddress | None = Field(None, description="May be a PO box")
    businessEmail: str | None = None
    correspondenceEmail: str | None = None
    businessPhone: str | None = None
    sameMainBusinessActivityIndicator: bool | None = None
    businessActivityCode: str | None = None
    businessActivityDescription: str | None = None
    additionalBusinessLocationsIndicator: bool | None = None

    @model_validator(mode="after")
    def _no_po_box(self):
        if self.business.is_po_box():
            raise ValueError("mainAddresses.business cannot be a PO box / care-of address")
        if self.business.postcode in ("2001", "3001", "4001"):
            raise ValueError("mainAddresses.business postcode cannot be a GPO postcode (2001/3001/4001)")
        return self


class TaxationInformation(_Loose):
    nonprofitIndicator: bool
    incomeTaxExemptionIndicator: bool
    australianResidentIndicator: bool = Field(..., description="false puts an applicant without a TFN on a 43-day proof-of-identity hold")
    taxAgentIdentifier: str | None = Field(None, description="Registered tax agent number, if lodged by an agent")
    superBenefitMembers: int | None = Field(None, description="SMSF: number of members")
    superCode: SuperBenefitCode | None = Field(None, description="SMSF benefit structure (A accumulation …)")


class IndividualAssociate(_Loose):
    """A person associated with the entity (director, partner, trustee, member …). Needs a tfn and/or a street address."""

    title: str | None = None
    familyName: str
    givenName: str
    otherGivenName: str | None = None
    dateOfBirth: Date
    genderCode: GenderCode | None = None
    positionHeld: list[PositionCode] = Field(..., min_length=1)
    tfn: str | None = None
    line2: str | None = Field(None, max_length=38)
    street: str | None = Field(None, max_length=38)
    locality: str | None = Field(None, max_length=46)
    state: StateCode | None = None
    postcode: str | None = Field(None, pattern=r"^\d{4}$")
    countryCode: str | None = Field(None, min_length=2, max_length=2)

    _tfn = field_validator("tfn")(_clean_tfn)

    @model_validator(mode="after")
    def _identity(self):
        if self.dateOfBirth > TODAY():
            raise ValueError("dateOfBirth cannot be in the future")
        if not self.tfn and not self.street:
            raise ValueError(f"associate {self.givenName} {self.familyName} needs a tfn or a street address")
        if _looks_like_po_box(self.street) or _looks_like_po_box(self.line2):
            raise ValueError("associate addresses cannot be PO boxes")
        return self


class OrganisationAssociate(_Loose):
    """A company associated with the entity (corporate trustee, corporate partner, shareholder …)."""

    name: str
    acn: str | None = None
    arbn: str | None = None
    abn: str | None = None
    incorporationDate: Date | None = Field(None, description="Required when no tfn is given")
    positionHeld: list[PositionCode] = Field(..., min_length=1)
    tfn: str | None = None
    line2: str | None = Field(None, max_length=38)
    street: str | None = Field(None, max_length=38)
    locality: str | None = Field(None, max_length=46)
    state: StateCode | None = None
    postcode: str | None = Field(None, pattern=r"^\d{4}$")
    countryCode: str | None = Field(None, min_length=2, max_length=2)

    _tfn = field_validator("tfn")(_clean_tfn)
    _acn = field_validator("acn")(_clean_acn)
    _abn = field_validator("abn")(_clean_abn)

    @model_validator(mode="after")
    def _identity(self):
        if not self.tfn and not (self.street and self.incorporationDate):
            raise ValueError(f"organisation associate {self.name} needs a tfn, or both a street address and incorporationDate")
        if self.incorporationDate and self.incorporationDate > TODAY():
            raise ValueError("incorporationDate cannot be in the future")
        return self


class AssociatesOther(_Loose):
    legalRepresentativeIndicator: bool | None = None


class Associates(_Loose):
    individuals: list[IndividualAssociate] | None = None
    organisations: list[OrganisationAssociate] | None = None
    other: AssociatesOther | None = None

    def all(self) -> list[IndividualAssociate | OrganisationAssociate]:
        return [*(self.individuals or []), *(self.organisations or [])]


class GstRegistration(_Loose):
    registrationStartDate: Date = Field(..., description="On/after abnStartDate, at most 6 months ahead")
    turnoverRangeCode: GstTurnoverRange
    lodgementFrequencyCode: GstFrequency
    reasonCode: GstReason
    cashAccountingIndicator: bool | None = None
    importIndicator: bool | None = None
    accountName: str | None = Field(None, max_length=32)


class PaygwRegistration(_Loose):
    lodgementChannelCode: PaygwChannel | None = None
    paymentSummaryProvisionMethodCode: PaygwSummaryMethod | None = None


class LctRegistration(_Loose):
    liabilityRangeCode: str | None = Field(None, description="$0 to $49,999 · $50,000 to $99,999 · $100,000 to $499,999 · $500,000 to $5 million · More than $5 million")
    activityCode: LctActivity | None = None


class TaxRegistrations(_Strict):
    """Only gst, ftc, lct, wet, paygw and fbt are accepted (PAYG withholding is paygw, not payg)."""

    gst: GstRegistration | None = None
    paygw: PaygwRegistration | None = None
    fbt: dict[str, Any] | None = None
    ftc: dict[str, Any] | None = None
    lct: LctRegistration | None = None
    wet: dict[str, Any] | None = None


class Declaration(_Loose):
    fullName: str
    positionHeld: str = Field(..., max_length=50, description="Free text, e.g. Owner, Director, Tax Agent")
    phone: str | None = None
    date: Date
    signatureAcceptanceIndicator: bool = Field(..., description="Must be true")
    signatureText: str

    @model_validator(mode="after")
    def _accepted(self):
        if not self.signatureAcceptanceIndicator:
            raise ValueError("signatureAcceptanceIndicator must be true")
        return self


class AuthorisedContact(_Loose):
    title: str | None = None
    familyName: str
    givenName: str
    otherGivenName: str | None = None
    positionHeld: str = Field(..., max_length=50, description="Free text")
    email: str
    phone: str
    preferredLanguageCode: LanguageCode | None = None
    preferredLanguageOther: str | None = None


class SuperFundNoticeOfElection(_Loose):
    governingRulesIndicator: bool
    regulatoryElectionDate: Date = Field(..., description="Not in the future")
    agePensionFundIndicator: bool


class SuperFundTrusteeDisclosure(_Loose):
    """Answer the set matching the trustee type (corporate vs individual); extra answers are harmless."""

    smsf12MonthsActivityIndicator: bool
    # corporate trustee
    liquidatorAppointedIndicator: bool | None = None
    disqualifiedOfficerIndicator: bool | None = None
    receiverAppointedIndicator: bool | None = None
    operationCeaseIndicator: bool | None = None
    underOfficialManagementIndicator: bool | None = None
    # individual trustees
    trusteesBankruptIndicator: bool | None = None
    civilPenaltyOrderIndicator: bool | None = None
    convictionIndicator: bool | None = None
    disqualifiedTrusteesOrDirectorsIndicator: bool | None = None


class SuperFundFinancialDetails(_Loose):
    electronicServiceAlias: str | None = None


class AbnRegistrationRequest(_Strict):
    """ATO ABNREG — apply for an Australian Business Number. POST /abn-registrations.
    testTransmissionIndicator is NOT a field here: the server sets it from its environment."""

    entityType: AtoEntityType
    entitySubType: AtoEntitySubType
    entitySubTypeAdditional: AtoEntitySubTypeAdditional | None = None
    entitlement: Entitlement
    applicant: AbnApplicant
    applicationDetail: ApplicationDetail
    applicationReason: ApplicationReason
    businessActivity: BusinessActivity
    mainAddresses: MainAddresses
    additionalAddresses: list[AtoAddress] | None = None
    taxationInformation: TaxationInformation
    associates: Associates | None = Field(None, description="Required for every entity except a sole trader")
    taxRegistrations: TaxRegistrations | None = None
    superFundNoticeOfElection: SuperFundNoticeOfElection | None = Field(None, description="Required for SMSF (sub-type 196)")
    superFundTrusteeDisclosure: SuperFundTrusteeDisclosure | None = Field(None, description="Required for SMSF (sub-type 196)")
    superFundFinancialDetails: SuperFundFinancialDetails | None = None
    authorisedContacts: list[AuthorisedContact] = Field(..., min_length=1)
    declaration: Declaration
    testTransmissionIndicator: bool | None = Field(None, exclude=True, description="Ignored: the server sets it from BAPI_ENV")

    @model_validator(mode="after")
    def _rules(self):
        et, st = self.entityType, self.entitySubType
        if st not in SUBTYPES_BY_ENTITY_TYPE[et]:
            raise ValueError(f"entitySubType {st} does not belong to entityType {et}; allowed: {sorted(SUBTYPES_BY_ENTITY_TYPE[et])}")
        assoc = self.associates.all() if self.associates else []

        if et == "117":
            if not self.applicant.individual:
                raise ValueError("a sole trader application needs applicant.individual")
            if self.applicationDetail.tfnApplicationIndicator:
                raise ValueError("sole traders cannot apply for a TFN through the API (tfnApplicationIndicator must be false)")
            if not self.applicant.tfn and not self.applicant.individual.has_address():
                raise ValueError("a sole trader must give applicant.tfn or a residential address (street, locality, state, postcode)")
            if assoc:
                raise ValueError("a sole trader has no associates")
        else:
            if not self.applicant.organisation:
                raise ValueError("applicant.organisation (with the legal name) is required for this entity type")
            if st in ("027", "032") and not self.applicant.organisation.acn:
                raise ValueError("applicant.organisation.acn is required for private (027) / public (032) companies")
            if self.businessActivity.governmentIndicator is None:
                raise ValueError("businessActivity.governmentIndicator is required for every entity except sole traders")
            minimum = 2 if st in ("087", "150") else 1
            if len(assoc) < minimum:
                raise ValueError(f"this entity needs at least {minimum} associate(s) in associates.individuals/organisations")

        if self.applicationDetail.tfnIndicator and not self.applicant.tfn:
            raise ValueError("applicationDetail.tfnIndicator is true but applicant.tfn is missing")

        allowed = allowed_positions(st, self.entitySubTypeAdditional)
        tfns: list[str] = []
        public_officers = 0
        for a in assoc:
            bad = set(a.positionHeld) - allowed
            if bad:
                raise ValueError(f"positionHeld {sorted(bad)} not allowed for sub-type {st}; allowed: {sorted(allowed) or 'none'}")
            if "POF" in a.positionHeld:
                public_officers += 1
            if a.tfn:
                tfns.append(a.tfn)
            if isinstance(a, OrganisationAssociate) and self.applicant.organisation and a.acn and a.acn == self.applicant.organisation.acn:
                raise ValueError("an associate organisation cannot use the applicant's own ACN")
        if public_officers > 1:
            raise ValueError("only one associate may be Public Officer (POF)")
        if len(tfns) != len(set(tfns)):
            raise ValueError("each associate TFN must be different")

        if st == "196" and (not self.superFundNoticeOfElection or not self.superFundTrusteeDisclosure):
            raise ValueError("SMSF (196) needs superFundNoticeOfElection and superFundTrusteeDisclosure")
        if self.taxRegistrations and self.taxRegistrations.gst:
            if self.taxRegistrations.gst.registrationStartDate < self.businessActivity.abnStartDate:
                raise ValueError("gst.registrationStartDate must be on or after abnStartDate")
        return self

    def to_payload(self, test_transmission: bool) -> dict[str, Any]:
        payload = self.model_dump(mode="json", exclude_none=True)
        payload["testTransmissionIndicator"] = test_transmission
        return payload


# ======================================================================================
# Business name registration (ASIC BNREG)
# ======================================================================================

BnEntityType = Literal["IND", "IB", "PTSH", "USTR", "JV"]
"""IND individual · IB incorporated body (use for companies) · PTSH partnership · USTR unincorporated trust · JV joint venture."""

BN_SUBTYPES: dict[str, str] = {
    "IB": "ABR entity-type code of the holder, e.g. PRV (private company) or PUB (public company)",
    "PTSH": "FPT (family partnership) or PTR (other partnership)",
    "USTR": "DTT (discretionary trading trust), DIT (discretionary investment trust), FUT (fixed unit trust) …",
}


class BnGeneralInformation(_Strict):
    businessName: str = Field(..., description="Proposed business (trading) name; check availability first")
    term: int = Field(..., description="Registration period in years: 1 or 3")
    paymentMethod: Literal["card", "invoice"] = Field(
        ..., description="card = ASIC online payment link returned via status (instant); invoice = ASIC emails an invoice (pay within 10 days)",
    )
    email: str = Field(..., description="Contact email for the application")

    @field_validator("term")
    @classmethod
    def _term(cls, v: int) -> int:
        if v not in (1, 3):
            raise ValueError("term must be 1 or 3 years")
        return v


class BnStreetAddress(_Strict):
    """Structured street address used by the business names register."""

    unitOrOfficeNumber: str | None = None
    streetNumber: str
    streetName: str = Field(..., description='Street name WITHOUT the type, e.g. "EXAMPLE"')
    streetType: str = Field(..., description='e.g. STREET, ST, ROAD, RD, AVENUE')
    locality: str
    state: StateCode
    postCode: str = Field(..., pattern=r"^\d{4}$")
    country: str | None = Field(None, min_length=2, max_length=2, description="ISO alpha-2, default AU")


class BnPostalAddress(_Strict):
    """PO box style address (allowed for serviceAddress only)."""

    postalType: str = Field(..., description='e.g. "PO BOX", "GPO BOX", "LOCKED BAG"')
    postalNumber: str
    locality: str
    state: StateCode
    postCode: str = Field(..., pattern=r"^\d{4}$")


class BnName(_Strict):
    familyName: str
    givenNames: str = Field(..., description="All given names, space separated")
    otherGivenNames: str | None = None


class BnBirthDetails(_Strict):
    date: Date
    cityTown: str
    state: StateCode | None = Field(None, description="Required when born in Australia")
    countryOfBirth: str = Field(..., min_length=2, max_length=2, description="ISO alpha-2")

    @model_validator(mode="after")
    def _au_state(self):
        if self.countryOfBirth.upper() == "AU" and not self.state:
            raise ValueError("state of birth is required when countryOfBirth is AU")
        if self.date > TODAY():
            raise ValueError("date of birth cannot be in the future")
        return self


class BnIndividual(_Strict):
    name: BnName
    birthDetails: BnBirthDetails
    address: BnStreetAddress
    abn: str | None = Field(None, description="Joint-venture associates need their own ABN")

    _abn = field_validator("abn")(_clean_abn)


class BnOrganisation(_Strict):
    name: str
    acn: str | None = Field(None, description="Required for the IB holder and for company partners/trustees")
    abn: str | None = Field(None, description="Joint-venture associates need their own ABN")

    _acn = field_validator("acn")(_clean_acn)
    _abn = field_validator("abn")(_clean_abn)


class BnParty(_Strict):
    """A partner / trustee / JV associate: exactly one of individual or organisation."""

    individual: BnIndividual | None = None
    organisation: BnOrganisation | None = None

    @model_validator(mode="after")
    def _one(self):
        if bool(self.individual) == bool(self.organisation):
            raise ValueError("Give exactly one of individual or organisation")
        return self


class BnEntity(_Strict):
    """The holder of the business name."""

    type: BnEntityType
    subType: str | None = Field(None, description="Holder's ABR entity-type code — required for IB, PTSH, USTR (PRV, PUB, FPT, PTR, DTT, DIT, FUT …)")
    abn: str | None = Field(None, description="Holder's ABN — required for every type except JV")
    individual: BnIndividual | None = Field(None, description="IND only")
    organisation: BnOrganisation | None = Field(None, description="IB / PTSH / USTR / JV: legal name (+ acn for IB)")
    partners: list[BnParty] | None = Field(None, description="PTSH only")
    trustees: list[BnParty] | None = Field(None, description="USTR only")
    associates: list[BnParty] | None = Field(None, description="JV only; each needs its own abn")

    _abn = field_validator("abn")(_clean_abn)

    @model_validator(mode="after")
    def _rules(self):
        t = self.type
        if t != "JV" and not self.abn:
            raise ValueError("entity.abn is required for every entity type except JV")
        if t in BN_SUBTYPES and not self.subType:
            raise ValueError(f"entity.subType is required for {t}: {BN_SUBTYPES[t]}")
        if t == "IND":
            if not self.individual:
                raise ValueError("IND needs entity.individual")
        else:
            if not self.organisation:
                raise ValueError(f"{t} needs entity.organisation.name")
        if t == "IB" and not (self.organisation and self.organisation.acn):
            raise ValueError("IB needs entity.organisation.acn")
        if t == "PTSH" and not self.partners:
            raise ValueError("PTSH needs entity.partners")
        if t == "USTR" and not self.trustees:
            raise ValueError("USTR needs entity.trustees")
        if t == "JV":
            if not self.associates:
                raise ValueError("JV needs entity.associates")
            for p in self.associates:
                party = p.individual or p.organisation
                if party is None or not party.abn:
                    raise ValueError("every JV associate needs its own abn")
        return self


class BnSignatory(_Strict):
    name: BnName
    declaresTrueAndCorrectIndicator: bool = Field(..., description="Must be true")

    @field_validator("declaresTrueAndCorrectIndicator")
    @classmethod
    def _true(cls, v: bool) -> bool:
        if not v:
            raise ValueError("declaresTrueAndCorrectIndicator must be true")
        return v


class BusinessNameRegistrationRequest(_Strict):
    """ASIC BNREG — register a business (trading) name. POST /business-name-registrations.
    testTransmissionIndicator is NOT a field here: the server sets it from its environment."""

    generalInformation: BnGeneralInformation
    businessAddress: BnStreetAddress = Field(..., description="Principal place of business (street address)")
    serviceAddress: BnStreetAddress | BnPostalAddress = Field(..., description="Address for service of documents; may be a PO box")
    entity: BnEntity
    signatory: BnSignatory
    testTransmissionIndicator: bool | None = Field(None, exclude=True, description="Ignored: the server sets it from BAPI_ENV")

    def to_payload(self, test_transmission: bool) -> dict[str, Any]:
        payload = self.model_dump(mode="json", exclude_none=True)
        payload["testTransmissionIndicator"] = test_transmission
        return payload


# ======================================================================================
# Business name renewal (ASIC BNRENEW)
# ======================================================================================

class BnRenewalGeneralInformation(_Strict):
    businessName: str = Field(..., description="The registered business name, exactly as it appears on the register")
    abn: str = Field(..., description="ABN of the business-name holder")
    term: int = Field(..., description="Renewal period in years: 1 or 3")
    email: str = Field(..., description="Contact email for the renewal")

    @field_validator("abn")
    @classmethod
    def _abn_required(cls, v: str) -> str:
        d = _clean_abn(v)
        if not d:
            raise ValueError("abn is required")
        return d

    @field_validator("term")
    @classmethod
    def _term(cls, v: int) -> int:
        if v not in (1, 3):
            raise ValueError("term must be 1 or 3 years")
        return v


class BnRenewalApplicant(_Strict):
    individual: BnName = Field(..., description="Person lodging the renewal (familyName, givenNames)")
    declaresTrueAndCorrectIndicator: bool = Field(..., description="Must be true")

    @field_validator("declaresTrueAndCorrectIndicator")
    @classmethod
    def _true(cls, v: bool) -> bool:
        if not v:
            raise ValueError("declaresTrueAndCorrectIndicator must be true")
        return v


class BusinessNameRenewalRequest(_Strict):
    """ASIC BNRENEW — renew an existing business name. POST /business-name-renewals."""

    generalInformation: BnRenewalGeneralInformation
    applicant: BnRenewalApplicant
    testTransmissionIndicator: bool | None = Field(None, exclude=True, description="Ignored: the server sets it from BAPI_ENV")

    def to_payload(self, test_transmission: bool) -> dict[str, Any]:
        payload = self.model_dump(mode="json", exclude_none=True)
        payload["testTransmissionIndicator"] = test_transmission
        return payload


# ======================================================================================
# ASIC Form 484 — Changes to company details
# ======================================================================================

Asic484SignatoryRole = Literal["DIR", "SEC", "ALT"]
OfficeholderStatus = Literal["AP", "NO", "CA", "CN", "CX", "AC"]
"""ASIC Table 0006: AP newly appointed · NO ceased · CA continuing, address change · CN continuing, name change ·
CX continuing, name and address change · AC newly appointed and ceased."""
OfficeCode = Literal["DIR", "SEC", "ALT"]
ShareClassStatus = Literal["AP", "DC", "NO"]
"""ASIC Table 0026: AP new class · DC details changed · NO whole class ceasing."""
HoldingStatus = Literal["AP", "CD", "CO", "NO", "TA", "TR"]
"""ASIC Table 0028: AP newly reported holding · CD change of details of existing holding · CO continuing unchanged ·
NO ceased · TA add unchanged holding to top 20 · TR remove unchanged holding from top 20."""
MemberStatus = Literal["AP", "CA", "CN", "CO", "CX", "NO"]
"""ASIC Table 0027: AP new member · CA address change · CN name change · CO continuing unchanged · CX name and address change · NO ceased."""
UhcStatus = Literal["A", "C", "N"]
"""Form 484 endpoint: A appointing · C ceasing · N notifying a change of details."""

_SIX_MONTHS = timedelta(days=183)


def _not_future(v: Date) -> Date:
    if v > TODAY():
        raise ValueError("date cannot be in the future")
    return v


def _upper_strings(value: Any) -> Any:
    """Form 484: 'All string values must be uppercase'. Dates and codes are unaffected."""
    if isinstance(value, dict):
        return {k: _upper_strings(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_upper_strings(v) for v in value]
    if isinstance(value, str):
        return value.upper()
    return value


class Asic484GeneralInformation(_Strict):
    companyName: str = Field(..., max_length=200, description="Current registered company name")
    acn: str = Field(..., description="9-digit ACN of the company (checksum validated)")
    annualStatementResponseIndicator: bool | None = Field(None, description="true when lodged in response to the annual statement")

    @field_validator("acn")
    @classmethod
    def _acn_required(cls, v: str) -> str:
        d = _clean_acn(v)
        if not d:
            raise ValueError("acn is required")
        return d


class Asic484Signature(_Strict):
    name: PersonName
    role: Asic484SignatoryRole = Field(..., description="DIR, SEC or ALT (alternate director)")
    date: Date = Field(..., description="YYYY-MM-DD; not in the future and not more than 6 months ago")
    declaresTrueAndCorrectIndicator: bool = Field(..., description="Must be true")

    @model_validator(mode="after")
    def _rules(self):
        if self.date > TODAY():
            raise ValueError("signature.date cannot be in the future")
        if self.date < TODAY() - _SIX_MONTHS:
            raise ValueError("signature.date cannot be more than 6 months in the past")
        if not self.declaresTrueAndCorrectIndicator:
            raise ValueError("signature.declaresTrueAndCorrectIndicator must be true")
        return self


class Asic484AuAddress(_Strict):
    """Australian street address (registered office / principal place of business)."""

    careOf: str | None = Field(None, max_length=50)
    line2: str | None = Field(None, max_length=50)
    street: str = Field(..., max_length=52)
    locality: str = Field(..., max_length=30)
    state: StateCode
    postcode: str = Field(..., pattern=r"^\d{4}$")


class Asic484Address(_Strict):
    """Officeholder / member address; may be overseas (then omit state and postcode)."""

    careOf: str | None = Field(None, max_length=50)
    line2: str | None = Field(None, max_length=50)
    street: str = Field(..., max_length=52)
    locality: str = Field(..., max_length=30)
    state: StateCode | None = Field(None, description="Required when country is AU (or omitted)")
    postcode: str | None = Field(None, pattern=r"^\d{4}$", description="Required when country is AU (or omitted)")
    country: str | None = Field(None, min_length=2, max_length=2, description="ISO 3166-1 alpha-2; omit or AU for Australia")

    @model_validator(mode="after")
    def _au(self):
        if (self.country or "AU").upper() == "AU" and (not self.state or not self.postcode):
            raise ValueError("Australian addresses need state and postcode")
        return self


class Asic484RegisteredOffice(_Strict):
    dateOfChange: Date = Field(..., description="Date the change took effect (not in the future)")
    address: Asic484AuAddress
    occupyIndicator: bool = Field(..., description="Does the company occupy the premises?")
    occupierName: str | None = Field(None, max_length=200, description="Required when occupyIndicator is false")
    occupierConsent: bool | None = Field(None, description="Required when occupyIndicator is false")
    addressOverriddenIndicator: bool | None = None

    _date = field_validator("dateOfChange")(_not_future)

    @model_validator(mode="after")
    def _occupier(self):
        if not self.occupyIndicator and (not self.occupierName or self.occupierConsent is None):
            raise ValueError("occupierName and occupierConsent are required when occupyIndicator is false")
        return self


class Asic484PrincipalPlaceOfBusiness(_Strict):
    dateOfChange: Date
    address: Asic484AuAddress
    addressOverriddenIndicator: bool | None = None

    _date = field_validator("dateOfChange")(_not_future)


class Asic484UltimateHoldingCompany(_Strict):
    dateOfChange: Date
    uhcStatus: UhcStatus = Field(..., description="A appointing · C ceasing · N notifying change")
    name: str | None = Field(None, max_length=200)
    acn: str | None = None
    abn: str | None = None
    placeIncorporation: str | None = Field(None, max_length=50, description="Place of incorporation for a foreign holding company")

    _date = field_validator("dateOfChange")(_not_future)
    _acn = field_validator("acn")(_clean_acn)
    _abn = field_validator("abn")(_clean_abn)


class Asic484BirthDetails(_Strict):
    date: Date = Field(..., description="YYYY-MM-DD; officer must be 18+")
    locality: str = Field(..., max_length=30, description="City/town of birth")
    localityQualifier: str = Field(..., max_length=3, description="Australian state code (VIC) or ISO alpha-2 country (GB) for overseas births")

    @field_validator("date")
    @classmethod
    def _adult(cls, v: Date) -> Date:
        if v > TODAY() - timedelta(days=18 * 365 + 5):
            raise ValueError("officeholder must be at least 18 years old")
        return v


class Asic484Office(_Strict):
    office: OfficeCode = Field(..., description="DIR, SEC or ALT")
    appointmentDate: Date | None = None
    cessationDate: Date | None = None
    nameOfDirector: PersonName | None = Field(None, description="ALT only: the director this person is alternate for")

    _a = field_validator("appointmentDate")(lambda v: _not_future(v) if v else v)
    _c = field_validator("cessationDate")(lambda v: _not_future(v) if v else v)


class Asic484OfficeHolder(_Strict):
    status: OfficeholderStatus = Field(..., description="AP appointed · NO ceased · CA/CN/CX continuing with address/name change · AC appointed and ceased")
    dateOfChange: Date
    name: PersonName
    formerName: PersonName | None = None
    birthDetails: Asic484BirthDetails
    organisationName: str | None = Field(None, max_length=200, description="When the officer is a body corporate")
    organisationAcn: str | None = None
    address: Asic484Address = Field(..., description="Residential address")
    addressOverriddenIndicator: bool | None = None
    offices: list[Asic484Office] = Field(..., min_length=1)

    _date = field_validator("dateOfChange")(_not_future)
    _oacn = field_validator("organisationAcn")(_clean_acn)


class Asic484ShareStructure(_Strict):
    """A share class as it stands AFTER the change. Totals must equal the sum of the members rows for the class (ASIC V455)."""

    classCode: str = Field(..., max_length=4, description="e.g. ORD")
    classTitle: str = Field(..., max_length=60, description="e.g. ORDINARY")
    numberIssued: int = Field(..., ge=0)
    totalAmountPaid: int = Field(..., ge=0)
    totalAmountUnpaid: int = Field(..., ge=0)
    status: ShareClassStatus = Field(..., description="AP new class · DC details changed · NO whole class ceasing")
    earliestDateOfChange: Date

    _date = field_validator("earliestDateOfChange")(_not_future)


class Asic484ShareIssueClass(_Strict):
    classCode: str = Field(..., max_length=4)
    classTitle: str = Field(..., max_length=60)
    totalNumberInIssue: int = Field(..., ge=0, description="Number of shares issued in this transaction")
    amountPaid: int = Field(..., ge=0)
    amountDue: int = Field(..., ge=0)


class Asic484ShareIssue(_Strict):
    earliestDateOfIssue: Date
    sharesIssuedForCashOnlyIndicator: bool
    sharesIssuedUnderWrittenContractIndicator: bool
    descriptionOfConsideration: str | None = Field(None, description="Required in practice when not cash-only; printable ASCII")
    shareClasses: list[Asic484ShareIssueClass] = Field(..., min_length=1)

    _date = field_validator("earliestDateOfIssue")(_not_future)


class Asic484ShareCancellationClass(_Strict):
    classCode: str = Field(..., max_length=4)
    classTitle: str = Field(..., max_length=60)
    totalNumberCancelled: int = Field(..., ge=0)
    totalConsiderationPaid: int = Field(..., ge=0)


class Asic484ShareCancellation(_Strict):
    earliestDateOfRegistrationOfCancellation: Date
    cancellationProvision: str = Field(..., max_length=60, description="ASIC Table 0021 code, e.g. S.254J-P, S.256A-S.256E-S, SS.257H(3)-M, S.258D, OTHER")
    sectionReference: str | None = Field(None, max_length=60)
    description: str | None = None
    shareClasses: list[Asic484ShareCancellationClass] = Field(..., min_length=1)

    _date = field_validator("earliestDateOfRegistrationOfCancellation")(_not_future)


class Asic484HoldingOwner(_Strict):
    individual: PersonName | None = None
    organisation: OrganisationRef | None = None
    formerName: PersonName | None = None
    formerNameOrganisation: str | None = Field(None, max_length=200)
    address: Asic484Address
    memberHasAcnIndicator: bool | None = None
    memberStatus: MemberStatus | None = Field(None, description="AP new · CA address change · CN name change · CO unchanged · CX name+address change · NO ceased")
    addressOverriddenIndicator: bool | None = None

    @model_validator(mode="after")
    def _one(self):
        if bool(self.individual) == bool(self.organisation):
            raise ValueError("Give exactly one of individual or organisation")
        return self


class Asic484Member(_Strict):
    """One holding (one or more joint holders) in one share class, as it stands after the change."""

    shareClass: str = Field(..., max_length=4, description="Must match a shareStructure classCode")
    number: int = Field(..., ge=0)
    sharesFullyPaidIndicator: bool
    beneficialOwnerIndicator: bool
    totalPaid: int = Field(..., ge=0)
    totalUnpaid: int = Field(..., ge=0)
    increaseDecreaseInHolding: int | None = Field(None, description="Signed: + increase, - decrease")
    holdingStatus: HoldingStatus = Field(..., description="AP new holding · CD details changed · CO unchanged · NO ceased · TA/TR top-20 list adjustments")
    dateOfChangeToRegister: Date
    dateMemberNameAddedToRegister: Date | None = None
    holdingOwners: list[Asic484HoldingOwner] = Field(..., min_length=1)

    _date = field_validator("dateOfChangeToRegister")(_not_future)


class Asic484ConcessionalFees(_Strict):
    dateOfChange: Date
    hasCommencedSpecialPurposeCompanyIndicator: bool | None = None
    hasCeasedToBeSpecialPurposeCompanyIndicator: bool | None = None
    isHomeUnitCompanyIndicator: bool | None = None
    isSuperannuationTrusteeCompanyIndicator: bool | None = None
    isForCharitablePurposesIndicator: bool | None = None

    _date = field_validator("dateOfChange")(_not_future)


class Asic484Annexure(_Strict):
    mark: str = Field(..., max_length=2, description='Annexure mark, e.g. "A"')
    type: Literal["ALT"] = Field(..., description="ALT = terms of appointment of alternate directors")
    numberOfPages: int = Field(..., ge=1)
    text: str = Field(..., description="Printable ASCII plus line breaks")


class Asic484FormRequest(_Strict):
    """ASIC Form 484 — Changes to Company Details. POST /asic-484-forms. Include only the sections that changed.
    testTransmissionIndicator is NOT a field here: the server sets it from its environment."""

    generalInformation: Asic484GeneralInformation
    signature: Asic484Signature
    registeredOffice: Asic484RegisteredOffice | None = Field(None, description="Registered office address change")
    principalPlaceOfBusiness: Asic484PrincipalPlaceOfBusiness | None = Field(None, description="Principal place of business change")
    ultimateHoldingCompany: Asic484UltimateHoldingCompany | None = None
    officeHolders: list[Asic484OfficeHolder] | None = Field(None, description="Appointments, cessations, name/address changes")
    shareStructure: list[Asic484ShareStructure] | None = Field(None, description="Required with shareIssue / shareCancellation")
    shareIssue: Asic484ShareIssue | None = None
    shareCancellation: Asic484ShareCancellation | None = None
    members: list[Asic484Member] | None = Field(None, description="Required with shareIssue / shareCancellation")
    concessionalFees: Asic484ConcessionalFees | None = None
    annexures: list[Asic484Annexure] | None = None
    testTransmissionIndicator: bool | None = Field(None, exclude=True, description="Ignored: the server sets it from BAPI_ENV")

    @model_validator(mode="after")
    def _rules(self):
        sections = (
            self.registeredOffice, self.principalPlaceOfBusiness, self.ultimateHoldingCompany, self.officeHolders,
            self.shareStructure, self.shareIssue, self.shareCancellation, self.members, self.concessionalFees, self.annexures,
        )
        if not any(sections):
            raise ValueError("a Form 484 needs at least one change section (registeredOffice, officeHolders, shareIssue, …)")
        if (self.shareIssue or self.shareCancellation) and not (self.shareStructure and self.members):
            raise ValueError("shareIssue / shareCancellation require both shareStructure and members")
        if self.members and not self.shareStructure:
            raise ValueError("members requires shareStructure")
        if self.shareStructure:
            codes = [s.classCode for s in self.shareStructure]
            if len(set(codes)) != len(codes):
                raise ValueError("shareStructure classCodes must be unique")
            for m in self.members or []:
                if m.shareClass not in codes:
                    raise ValueError(f"members.shareClass {m.shareClass} has no matching shareStructure classCode")
            # ASIC V455: class totals must equal the sum of the member rows (classes ceasing entirely are exempt)
            for s in self.shareStructure:
                if s.status == "NO":
                    continue
                rows = [m for m in (self.members or []) if m.shareClass == s.classCode]
                if not rows:
                    continue
                got = (sum(m.number for m in rows), sum(m.totalPaid for m in rows), sum(m.totalUnpaid for m in rows))
                want = (s.numberIssued, s.totalAmountPaid, s.totalAmountUnpaid)
                if got != want:
                    raise ValueError(
                        f"ASIC V455: share class {s.classCode} totals (numberIssued, totalAmountPaid, totalAmountUnpaid) = {want} "
                        f"but the members rows sum to {got}"
                    )
            for sc in (self.shareIssue.shareClasses if self.shareIssue else []) + (
                self.shareCancellation.shareClasses if self.shareCancellation else []
            ):
                if sc.classCode not in codes:
                    raise ValueError(f"share class {sc.classCode} in shareIssue/shareCancellation has no shareStructure entry")
        return self

    def to_payload(self, test_transmission: bool) -> dict[str, Any]:
        payload = _upper_strings(self.model_dump(mode="json", exclude_none=True))
        payload["testTransmissionIndicator"] = test_transmission
        return payload


# ======================================================================================
# Entity monitoring
# ======================================================================================

class MonitoredEntityInput(_Strict):
    """One entity to monitor: a company by ACN, an ABN holder by ABN, or both (the ABR must confirm they match)."""

    acn: str | None = Field(None, description="9-digit ACN (companies are always keyed on their ACN)")
    abn: str | None = Field(None, description="11-digit ABN")
    reference: str | None = Field(None, max_length=100, description="Your own reference for this entity, e.g. a client id")

    _acn = field_validator("acn")(_clean_acn)
    _abn = field_validator("abn")(_clean_abn)

    @model_validator(mode="after")
    def _identifier(self):
        if not self.acn and not self.abn:
            raise ValueError("each entity needs an acn and/or an abn (ARBNs are not accepted)")
        return self


# ======================================================================================
# Company name change (ASIC Form 205A) and voluntary deregistration (Form 6010)
# ======================================================================================

AsicSignatoryCode = Literal["ACC", "AGT", "APP", "DIR", "LEG", "LP", "REP", "SCP", "SEC", "SIG", "SUB"]
"""ASIC Table 0011: ACC accountant · AGT agent · APP applicant · DIR director · LEG legal representative · LP lodging party ·
REP representative · SCP shelf company promoter · SEC secretary · SIG signatory · SUB subscriber."""
AsicPaymentMethod = Literal["DDEB", "INV"]
"""ASIC Table 0032: DDEB payment from the registered agent's direct-debit account · INV ASIC invoice issued for payment."""


class CompanyNameChangeGeneralInformation(_Loose):
    companyName: str = Field(..., max_length=200, description="Current registered company name")
    acn: str = Field(..., description="9-digit ACN (checksum validated)")
    companyType: CompanyType = Field(..., description="APTY for a Pty Ltd, APUB for a public company")
    proposedName: str | None = Field(None, max_length=200, description='New name INCLUDING legal elements, e.g. "NEW NAME PTY LTD"; required unless acnNameIndicator is true')
    acnNameIndicator: bool = Field(False, description="true = the new name is the ACN plus legal elements")
    acnNameLegalElements: LegalElement | None = Field(None, description="Required when acnNameIndicator is true")
    nameReservedIndicator: bool | None = Field(None, description="true if the new name was reserved with a Form 410 (then supply nameReserved)")
    nameIdenticalIndicator: bool | None = Field(None, description="true if the new name is identical to an existing registered business name")
    nameIdenticalAbn: str | None = Field(None, description="ABN of the business-name holder when nameIdenticalIndicator is true")
    meetingDate: Date = Field(..., description="Date of the members' meeting / special resolution (not in the future)")
    meetingResolutionText: str = Field(..., description='Text of the special resolution, e.g. "THAT THE NAME OF THE COMPANY BE CHANGED TO NEW NAME PTY LTD"')
    paymentMethod: AsicPaymentMethod = Field(..., description="DDEB (agent's direct debit account) or INV (ASIC invoice)")

    _acn = field_validator("acn")(_require_acn)
    _abn = field_validator("nameIdenticalAbn")(_clean_abn)
    _md = field_validator("meetingDate")(_not_future)

    @model_validator(mode="after")
    def _rules(self):
        if not self.acnNameIndicator and not self.proposedName:
            raise ValueError("proposedName is required when acnNameIndicator is false")
        if self.acnNameIndicator and not self.acnNameLegalElements:
            raise ValueError("acnNameLegalElements is required when acnNameIndicator is true")
        if self.nameIdenticalIndicator and not self.nameIdenticalAbn:
            raise ValueError("nameIdenticalAbn is required when nameIdenticalIndicator is true")
        return self


class AsicSignatory(_Loose):
    """Who signs the form: an individual with an ASIC role code, the signing date and the declaration."""

    individual: PersonName
    role: AsicSignatoryCode = Field(..., description="Usually DIR or SEC for a company officer, AGT for the registered agent (Table 0011)")
    dateSigned: Date = Field(..., description="YYYY-MM-DD, not in the future")
    declaresTrueAndCorrectIndicator: bool = Field(..., description="Must be true")

    @model_validator(mode="after")
    def _rules(self):
        if self.dateSigned > TODAY():
            raise ValueError("dateSigned cannot be in the future")
        if not self.declaresTrueAndCorrectIndicator:
            raise ValueError("declaresTrueAndCorrectIndicator must be true")
        return self


class CompanyNameChangeRequest(_Strict):
    """ASIC Form 205A — change a registered company's name. POST /company-name-changes.
    testTransmissionIndicator is NOT a field here: the server sets it from its environment."""

    generalInformation: CompanyNameChangeGeneralInformation
    nameReserved: NameReserved | None = Field(None, description="Form 410 reservation details when nameReservedIndicator is true")
    signatory: AsicSignatory
    admin: Admin
    testTransmissionIndicator: bool | None = Field(None, exclude=True, description="Ignored: the server sets it from BAPI_ENV")

    @model_validator(mode="after")
    def _rules(self):
        if self.generalInformation.nameReservedIndicator and not self.nameReserved:
            raise ValueError("nameReserved is required when generalInformation.nameReservedIndicator is true")
        return self

    def to_payload(self, test_transmission: bool) -> dict[str, Any]:
        payload = self.model_dump(mode="json", exclude_none=True)
        payload["testTransmissionIndicator"] = test_transmission
        return payload


class DeregistrationGeneral(_Loose):
    companyName: str = Field(..., max_length=200)
    acn: str = Field(..., description="9-digit ACN (checksum validated)")
    paymentMethod: AsicPaymentMethod = Field(..., description="DDEB (agent's direct debit) or INV (ASIC invoice) for the deregistration fee")

    _acn = field_validator("acn")(_require_acn)


class DeregistrationDeclarations(_Strict):
    """The six s.601AA conditions for voluntary deregistration. ASIC accepts the application only if ALL are true."""

    allMembersAgreeIndicator: bool = Field(..., description="All members of the company agree to the deregistration")
    notCarryingOnBusinessIndicator: bool = Field(..., description="The company is not carrying on business")
    assetsLessThan1000Indicator: bool = Field(..., description="The company's assets are worth less than $1,000")
    allFeesPaidIndicator: bool = Field(..., description="All fees and penalties payable under the Corporations Act have been paid")
    noOutstandingLiabilitiesIndicator: bool = Field(..., description="The company has no outstanding liabilities")
    notPartyToLegalProceedingsIndicator: bool = Field(..., description="The company is not a party to any legal proceedings")

    @model_validator(mode="after")
    def _all_true(self):
        missing = [k for k, v in self.model_dump().items() if v is not True]
        if missing:
            raise ValueError(f"voluntary deregistration requires every declaration to be true; false: {missing}")
        return self


class DeregistrationApplicant(_Loose):
    """The applicant: the company itself (organisation + role of the officer applying) or an individual such as a director or member."""

    individual: PersonName | None = None
    organisation: OrganisationRef | None = None
    role: AsicSignatoryCode | None = Field(None, description="Applicant's role, e.g. DIR")
    address: Address | None = None

    @model_validator(mode="after")
    def _one(self):
        if bool(self.individual) == bool(self.organisation):
            raise ValueError("Give exactly one of individual or organisation")
        return self


class DeregistrationNominee(_Strict):
    """Person nominated to receive ASIC correspondence about the deregistration."""

    individual: PersonName
    address: Address


class CompanyDeregistrationRequest(_Strict):
    """ASIC Form 6010 — voluntary deregistration of a company. POST /company-deregistrations.
    testTransmissionIndicator is NOT a field here: the server sets it from its environment."""

    general: DeregistrationGeneral
    declarations: DeregistrationDeclarations
    applicant: DeregistrationApplicant
    nominee: DeregistrationNominee
    signatory: AsicSignatory
    testTransmissionIndicator: bool | None = Field(None, exclude=True, description="Ignored: the server sets it from BAPI_ENV")

    def to_payload(self, test_transmission: bool) -> dict[str, Any]:
        payload = self.model_dump(mode="json", exclude_none=True)
        payload["testTransmissionIndicator"] = test_transmission
        return payload


# ======================================================================================
# Registered-agent forms: ASIC 361 (agent ceases), 362 (company appoints/ceases agent), RA52 (account transactions)
# ======================================================================================

class AsicFormGeneralInformation(_Strict):
    companyName: str = Field(..., max_length=200)
    acn: str = Field(..., description="9-digit ACN (checksum validated)")

    _acn = field_validator("acn")(_require_acn)


class Asic361Signature(_Strict):
    name: PersonName
    role: Literal["AGT"] = Field("AGT", description="Signed by the registered agent (AGT)")
    date: Date = Field(..., description="YYYY-MM-DD, not in the future")
    agentAuthorisedIndicator: bool = Field(..., description="The agent is authorised to lodge; must be true")
    declaration: bool = Field(..., description="Must be true")

    _d = field_validator("date")(_not_future)

    @model_validator(mode="after")
    def _true(self):
        if not (self.agentAuthorisedIndicator and self.declaration):
            raise ValueError("agentAuthorisedIndicator and declaration must both be true")
        return self


class Asic361FormRequest(_Strict):
    """ASIC Form 361 — notification by a registered agent that it ceases to act for a company. POST /asic-361-forms."""

    generalInformation: AsicFormGeneralInformation
    signature: Asic361Signature
    testTransmissionIndicator: bool | None = Field(None, exclude=True, description="Ignored: the server sets it from BAPI_ENV")

    def to_payload(self, test_transmission: bool) -> dict[str, Any]:
        payload = self.model_dump(mode="json", exclude_none=True)
        payload["testTransmissionIndicator"] = test_transmission
        return payload


class Asic362GeneralInformation(_Strict):
    companyName: str = Field(..., max_length=200)
    acn: str = Field(..., description="9-digit ACN (checksum validated)")
    type: Literal["A", "C"] = Field(..., description="A = appoint your agent number for this company · C = cease the appointment")

    _acn = field_validator("acn")(_require_acn)


class Asic362Signature(_Strict):
    name: PersonName
    role: Literal["DIR", "SEC"] = Field(..., description="Signed by an officer of the company (DIR or SEC)")
    date: Date = Field(..., description="YYYY-MM-DD, not in the future")
    declaration: bool = Field(..., description="Must be true")

    _d = field_validator("date")(_not_future)

    @field_validator("declaration")
    @classmethod
    def _true(cls, v: bool) -> bool:
        if not v:
            raise ValueError("declaration must be true")
        return v


class Asic362FormRequest(_Strict):
    """ASIC Form 362 — a company appoints (A) or ceases (C) a registered agent. POST /asic-362-forms."""

    generalInformation: Asic362GeneralInformation
    signature: Asic362Signature
    testTransmissionIndicator: bool | None = Field(None, exclude=True, description="Ignored: the server sets it from BAPI_ENV")

    def to_payload(self, test_transmission: bool) -> dict[str, Any]:
        payload = self.model_dump(mode="json", exclude_none=True)
        payload["testTransmissionIndicator"] = test_transmission
        return payload


class AsicRa52General(_Strict):
    startDate: Date
    endDate: Date
    includePriorOutstandingIndicator: bool = Field(False, description="Also list transactions from before startDate that are still outstanding")

    @model_validator(mode="after")
    def _window(self):
        if self.endDate < self.startDate:
            raise ValueError("endDate must be on or after startDate")
        if self.endDate > TODAY():
            raise ValueError("endDate cannot be in the future")
        return self


class AsicRa52Request(_Strict):
    """ASIC Form RA52 — report of all ASIC account transactions for the registered agent over a period. POST /asic-ra52-forms."""

    general: AsicRa52General

    def to_payload(self, test_transmission: bool) -> dict[str, Any]:
        payload = self.model_dump(mode="json", exclude_none=True)
        payload["testTransmissionIndicator"] = test_transmission
        return payload


# ======================================================================================
# Webhooks
# ======================================================================================

WebhookEventType = Literal[
    "*",
    "company_registration.status_changed",
    "abn_registration.status_changed",
    "business_name_registration.status_changed",
    "business_name_renewal.status_changed",
    "ppsr.alert.grantor_registration_changed",
    "monitoring.change_detected",
    "monitoring.expiring",
    "monitoring.expired",
]


def validate_webhook_url(url: str) -> str:
    """Business API rejects non-HTTPS URLs and private / loopback / link-local hosts; fail fast locally."""
    import ipaddress
    from urllib.parse import urlsplit

    parts = urlsplit(url.strip())
    if parts.scheme != "https":
        raise ValueError("webhook url must use https://")
    host = (parts.hostname or "").lower()
    if not host:
        raise ValueError("webhook url has no host")
    if host in ("localhost",) or host.endswith((".local", ".localhost", ".internal")):
        raise ValueError("webhook url must resolve to a public host (localhost / .local are rejected)")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return url.strip()
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip in ipaddress.ip_network("100.64.0.0/10"):
        raise ValueError("webhook url must point at a public IP (private, loopback, link-local and CGNAT ranges are rejected)")
    return url.strip()
