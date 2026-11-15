from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest

from parkingbreaker.agencies import ApprovedCapability, IssuerType, ServiceMode
from parkingbreaker.case_state import HardStop, StopFamily
from parkingbreaker.service_policy import (
    SERVICE_CONTROL,
    STATE_BASIS,
    IntakeSupport,
    IssuerKind,
    ServiceControlCategory,
    ServiceControlEntry,
    StateBasis,
    intake_support_check,
    recorded_basis,
    service_control_entry,
)
from samples import SAN_FRANCISCO, TEST_BASIS

TODAY = date(2026, 10, 13)
ISSUED = date(2026, 10, 1)
CONTROL = ServiceControlEntry(
    "CA", ServiceControlCategory.ECONOMICS, "Test economics", "owner", TODAY
)
US_STATES = frozenset(
    [
        "AL",
        "AK",
        "AZ",
        "AR",
        "CA",
        "CO",
        "CT",
        "DE",
        "FL",
        "GA",
        "HI",
        "ID",
        "IL",
        "IN",
        "IA",
        "KS",
        "KY",
        "LA",
        "ME",
        "MD",
        "MA",
        "MI",
        "MN",
        "MS",
        "MO",
        "MT",
        "NE",
        "NV",
        "NH",
        "NJ",
        "NM",
        "NY",
        "NC",
        "ND",
        "OH",
        "OK",
        "OR",
        "PA",
        "RI",
        "SC",
        "SD",
        "TN",
        "TX",
        "UT",
        "VT",
        "VA",
        "WA",
        "WV",
        "WI",
        "WY",
    ]
)
RECORDED_KEYS = (US_STATES | {"DC"}) - {"AZ", "NV"}


def test_production_basis_records_exact_state_and_dc_set() -> None:
    assert len(US_STATES) == 50
    assert len(STATE_BASIS) == 49  # 50 states + DC - AZ - NV; CA is already included.
    assert set(STATE_BASIS) == RECORDED_KEYS


@pytest.mark.parametrize("key", sorted(RECORDED_KEYS))
def test_production_basis_is_recorded_restricted_and_supports_intake(key: str) -> None:
    basis = STATE_BASIS[key]
    assert basis.key == key
    assert basis.is_recorded
    assert basis.service_mode is ServiceMode.RESTRICTED_CLERICAL
    assert basis.approved_capabilities == ()
    agency = replace(SAN_FRANCISCO, state=key, basis_key=key)
    assert recorded_basis(agency, STATE_BASIS) is basis
    result = intake_support_check(
        agency,
        IssuerKind.GOVERNMENT,
        STATE_BASIS,
        SERVICE_CONTROL,
        ISSUED,
        None,
        TODAY,
        launch_allowlist=None,
    )
    assert result.status is IntakeSupport.SUPPORTED
    assert result.family is None
    assert result.hard_stop is None


@pytest.mark.parametrize("key", sorted(RECORDED_KEYS - {"CA"}))
def test_delegated_basis_records_owner_stance_and_ca_disclaimers(key: str) -> None:
    basis = STATE_BASIS[key]
    assert basis.rules_checked == (
        "Owner stance 2026-10-13 applied nationally under the owner's delegation "
        "(docs/business-strategy.md §6): ParkingBreaker is a restricted scribe and formatting "
        "service. It organises and formats the customer's own facts and position, and gives "
        "no legal advice, strategy, merits assessment, representation or outcome promise. "
        "No state-specific document-preparer finding has been made.",
    )
    assert basis.how_met == STATE_BASIS["CA"].how_met
    assert basis.links == STATE_BASIS["CA"].links
    assert basis.date_checked == date(2026, 10, 14)
    assert basis.checked_by == "Supervisor (Claude) under owner delegation 2026-10-13"


@pytest.mark.parametrize(
    ("issuer_type", "state", "key"),
    [
        (IssuerType.MUNICIPAL, "AZ", "AZ"),
        (IssuerType.MUNICIPAL, "NV", "NV"),
        (IssuerType.FEDERAL, "CA", "US-FED"),
        (IssuerType.TRIBAL, "CA", "TRIBE:123"),
        (IssuerType.TRIBAL, "CA", "TRIBE:Ab.c_0-9"),
    ],
)
def test_unrecorded_jurisdictions_remain_not_yet_verified_s6(
    issuer_type: IssuerType, state: str, key: str
) -> None:
    assert key not in STATE_BASIS
    agency = replace(SAN_FRANCISCO, issuer_type=issuer_type, state=state, basis_key=key)
    assert recorded_basis(agency, STATE_BASIS) is None
    result = intake_support_check(
        agency,
        IssuerKind.GOVERNMENT,
        STATE_BASIS,
        SERVICE_CONTROL,
        ISSUED,
        None,
        TODAY,
        launch_allowlist=None,
    )
    assert result.status is IntakeSupport.NOT_YET_VERIFIED
    assert result.family is StopFamily.NOT_YET_VERIFIED
    assert result.hard_stop is HardStop.S6


def test_production_ca_basis_is_recorded_restricted_and_read_only() -> None:
    basis = STATE_BASIS["CA"]
    assert basis.key == "CA"
    assert basis.is_recorded
    assert basis.service_mode is ServiceMode.RESTRICTED_CLERICAL
    assert basis.approved_capabilities == ()
    assert basis.date_checked == TODAY
    assert basis.checked_by == "Amir (owner)"
    assert basis.rules_checked == (
        "Owner determination 2026-10-13: in California ParkingBreaker is a restricted scribe "
        "and formatting service. It organises and formats the customer's own facts and "
        "position, and gives no legal advice, strategy, merits assessment, representation "
        "or outcome promise. No separate Legal Document Assistant finding is made.",
    )
    assert basis.links == (
        "parkingbreaker 2.x frontend/components/LegalDisclaimer.tsx:41-54",
        "parkingbreaker 2.x frontend/app/[locale]/terms/page.tsx:75-90",
        "parkingbreaker 2.x frontend/app/[locale]/what-we-are/page.tsx:53-62",
        "parkingbreaker 2.x frontend/.copy-snapshot.json:665",
    )
    assert basis.how_met == (
        "The customer supplies the facts and position and adopts the exact text; "
        "no legal arguments or citations are added. Disclaimers are shown as in the "
        "2.x legally reviewed copy: LegalDisclaimer.tsx:41-54, terms/page.tsx:75-90, "
        "what-we-are/page.tsx:53-62 and .copy-snapshot.json:665 (see links)."
    )
    assert recorded_basis(SAN_FRANCISCO, STATE_BASIS) is basis
    assert SERVICE_CONTROL == ()
    with pytest.raises(TypeError):
        STATE_BASIS["CA"] = TEST_BASIS  # type: ignore[index]


@pytest.mark.parametrize(
    "field", ["rules_checked", "links", "date_checked", "how_met", "checked_by"]
)
def test_basis_requires_every_accountability_field(field: str) -> None:
    empty: object = (
        None if field == "date_checked" else () if field in {"rules_checked", "links"} else ""
    )
    assert TEST_BASIS.is_recorded
    assert not replace(TEST_BASIS, **{field: empty}).is_recorded  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["rules_checked", "links", "how_met", "checked_by"])
def test_blank_basis_content_does_not_permit_service(field: str) -> None:
    blank: object = (" ",) if field in {"rules_checked", "links"} else " "
    assert not replace(TEST_BASIS, **{field: blank}).is_recorded  # type: ignore[arg-type]


def test_capabilities_require_expanded_mode_and_unique_recorded_basis() -> None:
    cap = ApprovedCapability("rank_provisions", "Test basis")
    with pytest.raises(ValueError, match="EXPANDED"):
        replace(TEST_BASIS, approved_capabilities=(cap,))
    expanded = replace(TEST_BASIS, service_mode=ServiceMode.EXPANDED, approved_capabilities=(cap,))
    assert expanded.approved_capabilities == (cap,)
    with pytest.raises(ValueError, match="duplicate"):
        replace(expanded, approved_capabilities=(cap, cap))
    with pytest.raises(ValueError, match="basis"):
        ApprovedCapability("rank_provisions", " ")
    with pytest.raises(ValueError, match="name"):
        ApprovedCapability(" ", "test")
    with pytest.raises(ValueError, match="basis key"):
        StateBasis("California")


@pytest.mark.parametrize("key", ["CA", "DC", "PR", "US-FED", "TRIBE:123", "TRIBE:Ab.c_0-9"])
def test_basis_accepts_all_key_shapes(key: str) -> None:
    assert replace(TEST_BASIS, key=key).key == key


@pytest.mark.parametrize(
    "key", ["", "ca", "C1", "CA\n", "US-fed", "US-FED:x", "TRIBE:", "TRIBE:a/b", "TRIBE:é"]
)
def test_basis_rejects_invalid_key_shapes(key: str) -> None:
    with pytest.raises(ValueError, match="basis key"):
        replace(TEST_BASIS, key=key)


@pytest.mark.parametrize(
    ("issuer_type", "key"),
    [
        (IssuerType.MUNICIPAL, "CA"),
        (IssuerType.FEDERAL, "US-FED"),
        (IssuerType.TRIBAL, "TRIBE:123"),
    ],
)
def test_basis_lookup_uses_key_and_checks_record_identity(
    issuer_type: IssuerType, key: str
) -> None:
    agency = replace(SAN_FRANCISCO, issuer_type=issuer_type, basis_key=key)
    basis = replace(TEST_BASIS, key=key)
    assert recorded_basis(agency, {key: basis}) is basis
    assert recorded_basis(agency, {}) is None
    assert recorded_basis(agency, {key: replace(basis, key="NV")}) is None
    assert recorded_basis(agency, {key: replace(basis, checked_by="")}) is None
    if key != agency.state:
        assert recorded_basis(agency, {agency.state: TEST_BASIS}) is None


@pytest.mark.parametrize(
    ("issuer_type", "key"),
    [
        (IssuerType.MUNICIPAL, "CA"),
        (IssuerType.FEDERAL, "US-FED"),
        (IssuerType.TRIBAL, "TRIBE:123"),
    ],
)
@pytest.mark.parametrize("scope_kind", ["id", "basis_key"])
def test_control_matches_id_or_basis_key(
    issuer_type: IssuerType, key: str, scope_kind: str
) -> None:
    agency = replace(SAN_FRANCISCO, issuer_type=issuer_type, basis_key=key)
    entry = replace(CONTROL, scope=agency.id if scope_kind == "id" else key)
    assert service_control_entry(agency, (entry,)) is entry
    result = intake_support_check(
        agency, IssuerKind.GOVERNMENT, {}, (entry,), ISSUED, None, TODAY, launch_allowlist=None
    )
    assert result.status is IntakeSupport.BUSINESS_EXCLUSION
    assert result.control_entry is entry
    if key != agency.state:
        assert service_control_entry(agency, (CONTROL,)) is None
        basis = replace(TEST_BASIS, key=key)
        result = intake_support_check(
            agency,
            IssuerKind.GOVERNMENT,
            {key: basis},
            (CONTROL,),
            ISSUED,
            None,
            TODAY,
            launch_allowlist=None,
        )
        assert result.status is IntakeSupport.SUPPORTED


@pytest.mark.parametrize("field", ["scope", "detail", "author"])
def test_control_requires_nonblank_accountability(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        replace(CONTROL, **{field: " "})  # type: ignore[arg-type]


def test_control_requires_category_and_date() -> None:
    with pytest.raises(ValueError, match="category"):
        replace(CONTROL, category="LEGAL")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="entered_on"):
        replace(CONTROL, entered_on=None)  # type: ignore[arg-type]


@pytest.mark.parametrize("deadline", [None, TODAY, TODAY + timedelta(days=1)])
def test_unfamiliar_resolved_agency_supported_without_activation(deadline: date | None) -> None:
    agency = replace(SAN_FRANCISCO, id="previously-unseen-agency")
    result = intake_support_check(
        agency,
        IssuerKind.GOVERNMENT,
        {"CA": TEST_BASIS},
        (),
        ISSUED,
        deadline,
        TODAY,
        launch_allowlist=None,
    )
    assert result.status is IntakeSupport.SUPPORTED
    assert result.family is None
    assert result.hard_stop is None
    assert result.control_entry is None


def test_explicit_launch_allowlist_remains_an_operator_pause() -> None:
    agency = replace(SAN_FRANCISCO, id="CA:lacity.gov", official_domains=("lacity.gov",))
    unrestricted = intake_support_check(
        agency,
        IssuerKind.GOVERNMENT,
        STATE_BASIS,
        (),
        ISSUED,
        None,
        TODAY,
        launch_allowlist=None,
    )
    paused = intake_support_check(
        agency,
        IssuerKind.GOVERNMENT,
        STATE_BASIS,
        (),
        ISSUED,
        None,
        TODAY,
        launch_allowlist={"sfmta.com"},
    )
    assert unrestricted.status is IntakeSupport.SUPPORTED
    assert paused.status is IntakeSupport.BUSINESS_EXCLUSION
    assert paused.family is StopFamily.BUSINESS_EXCLUSION


def test_private_scope_wins_even_when_the_agency_is_unresolved() -> None:
    # A private issuer rarely resolves to an official source; say why we refuse.
    result = intake_support_check(
        None, IssuerKind.PRIVATE, {}, (CONTROL,), ISSUED, ISSUED, TODAY, launch_allowlist=None
    )
    assert result.status is IntakeSupport.BUSINESS_EXCLUSION
    assert result.family is StopFamily.BUSINESS_EXCLUSION


def test_unresolved_government_agency_wins_over_other_refusals() -> None:
    result = intake_support_check(
        None, IssuerKind.GOVERNMENT, {}, (CONTROL,), ISSUED, ISSUED, TODAY, launch_allowlist=None
    )
    assert result.status is IntakeSupport.NOT_YET_VERIFIED
    assert result.family is StopFamily.NOT_YET_VERIFIED


def test_private_is_product_scope_before_control_basis_or_deadline() -> None:
    result = intake_support_check(
        SAN_FRANCISCO,
        IssuerKind.PRIVATE,
        {},
        (CONTROL,),
        ISSUED,
        ISSUED,
        TODAY,
        launch_allowlist=None,
    )
    assert result.status is IntakeSupport.BUSINESS_EXCLUSION
    assert result.family is StopFamily.BUSINESS_EXCLUSION
    assert "product scope" in result.detail
    assert result.control_entry is None


@pytest.mark.parametrize("scope", ["CA", SAN_FRANCISCO.id])
@pytest.mark.parametrize("category", list(ServiceControlCategory))
def test_control_refusal_preserves_owner_entry_before_basis_or_deadline(
    scope: str, category: ServiceControlCategory
) -> None:
    entry = replace(CONTROL, scope=scope, category=category)
    result = intake_support_check(
        SAN_FRANCISCO,
        IssuerKind.GOVERNMENT,
        {},
        (entry,),
        ISSUED,
        ISSUED,
        TODAY,
        launch_allowlist=None,
    )
    assert result.status is IntakeSupport.BUSINESS_EXCLUSION
    assert result.family is StopFamily.BUSINESS_EXCLUSION
    assert result.control_entry is entry
    assert result.detail == entry.detail


def test_missing_state_basis_is_s6_before_deadline() -> None:
    result = intake_support_check(
        SAN_FRANCISCO, IssuerKind.GOVERNMENT, {}, (), ISSUED, ISSUED, TODAY, launch_allowlist=None
    )
    assert result.status is IntakeSupport.NOT_YET_VERIFIED
    assert result.hard_stop is HardStop.S6
    assert result.detail == "Service basis is not yet recorded."


def test_past_deadline_is_s4_not_yet_verified() -> None:
    result = intake_support_check(
        SAN_FRANCISCO,
        IssuerKind.GOVERNMENT,
        {"CA": TEST_BASIS},
        (),
        ISSUED,
        TODAY - timedelta(days=1),
        TODAY,
        launch_allowlist=None,
    )
    assert result.status is IntakeSupport.NOT_YET_VERIFIED
    assert result.family is StopFamily.NOT_YET_VERIFIED
    assert result.hard_stop is HardStop.S4
    assert "printed deadline has passed" in result.detail
    assert "applicable rule and any later stage still need verification" in result.detail


def test_unrelated_control_does_not_block() -> None:
    result = intake_support_check(
        SAN_FRANCISCO,
        IssuerKind.GOVERNMENT,
        {"CA": TEST_BASIS},
        (replace(CONTROL, scope="NV"),),
        ISSUED,
        None,
        TODAY,
        launch_allowlist=None,
    )
    assert result.status is IntakeSupport.SUPPORTED


@pytest.mark.parametrize("issuer_kind", list(IssuerKind))
def test_future_notice_date_is_s4_before_policy(issuer_kind: IssuerKind) -> None:
    result = intake_support_check(
        None,
        issuer_kind,
        {},
        (CONTROL,),
        TODAY + timedelta(days=1),
        ISSUED,
        TODAY,
        launch_allowlist=None,
    )
    assert result.status is IntakeSupport.NOT_YET_VERIFIED
    assert result.family is StopFamily.NOT_YET_VERIFIED
    assert result.hard_stop is HardStop.S4
    assert "notice date" in result.detail
    assert "must be corrected by the customer" in result.detail
    assert result.control_entry is None


def test_notice_date_today_is_supported() -> None:
    result = intake_support_check(
        SAN_FRANCISCO,
        IssuerKind.GOVERNMENT,
        {"CA": TEST_BASIS},
        (),
        TODAY,
        None,
        TODAY,
        launch_allowlist=None,
    )
    assert result.status is IntakeSupport.SUPPORTED


def test_dynamic_ticket_age_reachability() -> None:
    from parkingbreaker.mail_timing import DeadlineRule
    from parkingbreaker.service_policy import (
        IntakeStop,
        intake_customer_stop,
        is_ticket_age_serviceable,
    )

    issue_date = date(2026, 11, 1)
    today = date(2026, 11, 15)  # 14 days old

    # Without verified parameters, falls back to static 10-day limit (rejected)
    assert not is_ticket_age_serviceable(issue_date=issue_date, today=today)
    assert intake_customer_stop(issue_date, today, False) is IntakeStop.TOO_OLD

    # With 21-day postmark rule (e.g. SFMTA), day 14 is safely reachable (allowed)
    assert is_ticket_age_serviceable(
        issue_date=issue_date,
        today=today,
        deadline_days=21,
        deadline_rule=DeadlineRule.POSTMARK,
    )
    assert (
        intake_customer_stop(
            issue_date,
            today,
            False,
            deadline_days=21,
            deadline_rule=DeadlineRule.POSTMARK,
        )
        is None
    )
