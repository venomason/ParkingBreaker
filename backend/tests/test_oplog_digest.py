from __future__ import annotations

import dataclasses
import json
from datetime import date
from typing import Any

import pytest

from parkingbreaker.agencies import IssuerType, ResolvedAgency
from parkingbreaker.case_state import HardStop, StopFamily
from parkingbreaker.oplog.digest import (
    JurisdictionExclusion,
    LogDigest,
    PhaseReached,
    ResearchStatus,
    iso_week_of,
    issuer_token,
    make_digest,
)
from parkingbreaker.research.delegation import DelegationOutcome
from parkingbreaker.research.jurisdiction import MatchOutcome
from parkingbreaker.research.veto_codes import VetoCode


def _kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "issuer_token": "CA_sf_gov",
        "issuer_type": IssuerType.MUNICIPAL,
        "basis_key_token": "CA",
        "ticket_type": "street_cleaning",
        "stage_token": None,
        "phase_reached": PhaseReached.MAILED,
        "outcome": "SERVICEABLE",
        "hard_stop": None,
        "veto_codes": (),
        "match_outcome": None,
        "delegation_counts": {},
        "ai_cost_cents": 10,
        "mail_class": "first_class",
        "violation_category": "street_cleaning",
        "mode": "standard",
        "states_reached": ("AUTHORIZED", "MAILED"),
        "stage_durations_s": {"research": 42, "drafting": 30},
        "research_counts": {"pages_fetched": 4, "quotes_rejected": 1},
        "question_types": ("signage_photo",),
        "default_types": (),
        "redraft_count": 1,
        "flag_types": ("deadline_tight",),
        "feasibility_margin_days": 6,
        "lob_lag_days": 1,
        "usps_lag_days": 3,
        "payment_class": "captured",
        "remedy_class": None,
        "cost_cents": 1250,
        "iso_week": "2026-W41",
        "research_status": ResearchStatus.PENDING,
        "jurisdiction_exclusion": None,
    }
    base.update(overrides)
    return base


def test_fields_are_exactly_the_allowlist() -> None:
    names = {f.name for f in dataclasses.fields(LogDigest)}
    assert names == set(_kwargs())


def test_digest_is_frozen_and_hashable() -> None:
    digest = make_digest(**_kwargs())
    with pytest.raises(dataclasses.FrozenInstanceError):
        digest.issuer_token = "CA_la_gov"  # type: ignore[misc]
    assert hash(digest) == hash(make_digest(**_kwargs()))


def test_to_prompt_json_round_trips_and_is_deterministic() -> None:
    digest = make_digest(**_kwargs())
    text = digest.to_prompt_json()
    data = json.loads(text)
    assert data["stage_durations_s"] == {"drafting": 30, "research": 42}
    assert data["states_reached"] == ["AUTHORIZED", "MAILED"]
    assert data["iso_week"] == "2026-W41"
    assert data["remedy_class"] is None
    reordered = make_digest(**_kwargs(stage_durations_s={"drafting": 30, "research": 42}))
    assert text == reordered.to_prompt_json()


def test_mapping_order_does_not_matter() -> None:
    a = make_digest(**_kwargs(research_counts={"a": 1, "b": 2}))
    b = make_digest(**_kwargs(research_counts={"b": 2, "a": 1}))
    assert a == b


@pytest.mark.parametrize(
    "overrides",
    [
        {"issuer_token": "San Francisco"},
        {"violation_category": "parked at 123 Main St"},
        {"mode": ""},
        {"payment_class": "pi_1234567"},
        {"remedy_class": "refund to jo@x.com"},
        {"flag_types": ("free text here",)},
        {"question_types": ("7ABC12345",)},
        {"states_reached": ("MAILED 2026-10-11",)},
        {"stage_durations_s": {"research": -1}},
        {"research_counts": {"pages fetched": 2}},
        {"redraft_count": -1},
        {"cost_cents": -5},
        {"lob_lag_days": -1},
        {"iso_week": "2026-10-11"},
        {"iso_week": "2026-W54"},
    ],
)
def test_rejects_values_outside_the_allowlist(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match=":"):
        make_digest(**_kwargs(**overrides))


def test_negative_feasibility_margin_is_allowed() -> None:
    assert make_digest(**_kwargs(feasibility_margin_days=-2)).feasibility_margin_days == -2


def test_iso_week_of() -> None:
    assert iso_week_of(date(2026, 10, 11)) == "2026-W41"
    assert iso_week_of(date(2027, 1, 1)) == "2026-W53"
    assert iso_week_of(date(2026, 1, 5)) == "2026-W02"


def test_prompt_json_has_no_exact_dates_or_ids() -> None:
    text = make_digest(**_kwargs()).to_prompt_json()
    assert "2026-10" not in text
    assert "@" not in text


# Include every string leaf: scalar categories/enums/week, tuple elements, and map keys.
STRING_FIELDS = (
    "issuer_token",
    "issuer_type",
    "basis_key_token",
    "ticket_type",
    "violation_category",
    "stage_token",
    "phase_reached",
    "outcome",
    "hard_stop",
    "match_outcome",
    "mode",
    "payment_class",
    "remedy_class",
    "mail_class",
    "iso_week",
    "veto_codes",
    "states_reached",
    "question_types",
    "default_types",
    "flag_types",
    "stage_durations_s",
    "research_counts",
    "delegation_counts",
    "research_status",
    "jurisdiction_exclusion",
)


@pytest.mark.parametrize("field", STRING_FIELDS)
@pytest.mark.parametrize(
    "unsafe",
    [
        "https://sf.gov/contest",
        "sf.gov",
        '"Contest by mail"',
        "123 Main St",
        "AB12CD",
        "plate_AB12CD",
        "ticket_123456789",
    ],
)
@pytest.mark.parametrize("direct", [False, True])
def test_sensitive_strings_rejected_in_every_string_field(
    field: str,
    unsafe: str,
    direct: bool,
) -> None:
    digest = make_digest(**_kwargs())
    current = getattr(digest, field)
    value: Any = unsafe
    if isinstance(current, tuple):
        value = (
            ((unsafe, 1),)
            if field.endswith("_counts") or field == "stage_durations_s"
            else (unsafe,)
        )
    if not direct and field in {"stage_durations_s", "research_counts", "delegation_counts"}:
        value = {unsafe: 1}
    if direct:
        with pytest.raises(ValueError, match=":"):
            dataclasses.replace(digest, **{field: value})
    else:
        with pytest.raises(ValueError, match=":"):
            make_digest(**_kwargs(**{field: value}))


def test_v7_fields_round_trip_and_veto_codes_are_canonical() -> None:
    values = _kwargs(
        phase_reached=PhaseReached.PREHOLD,
        outcome=StopFamily.NOT_YET_VERIFIED,
        hard_stop=HardStop.S2,
        match_outcome=MatchOutcome.AMBIGUOUS,
        veto_codes=(VetoCode.UNSAFE_URL, VetoCode.NO_ASSESSMENT, VetoCode.UNSAFE_URL),
        delegation_counts={
            DelegationOutcome.NOT_YET_VERIFIED: 2,
            DelegationOutcome.DELEGATED_CONTEST: 1,
        },
    )
    digest = make_digest(**values)
    assert digest.veto_codes == (VetoCode.NO_ASSESSMENT, VetoCode.UNSAFE_URL)
    data = json.loads(digest.to_prompt_json())
    assert set(data) == set(values)
    assert "city_id" not in data
    assert data["phase_reached"] == "prehold"
    assert data["outcome"] == "NOT_YET_VERIFIED"
    assert data["hard_stop"] == "S2"
    assert data["veto_codes"] == ["NO_ASSESSMENT", "UNSAFE_URL"]
    assert data["match_outcome"] == "AMBIGUOUS"
    assert data["delegation_counts"] == {"NOT_YET_VERIFIED": 2, "DELEGATED_CONTEST": 1}
    assert data["issuer_type"] == "MUNICIPAL"
    assert data["ai_cost_cents"] == 10
    assert data["cost_cents"] == 1250
    assert data["mail_class"] == "first_class"
    assert data["ticket_type"] == data["violation_category"]
    reordered = make_digest(
        **{
            **values,
            "veto_codes": (VetoCode.NO_ASSESSMENT, VetoCode.UNSAFE_URL),
            "delegation_counts": dict(reversed(list(values["delegation_counts"].items()))),
        }
    )
    assert reordered == digest
    assert reordered.to_prompt_json() == digest.to_prompt_json()
    assert dataclasses.replace(digest, veto_codes=values["veto_codes"]) == digest


@pytest.mark.parametrize("phase", PhaseReached)
@pytest.mark.parametrize("outcome", ["SERVICEABLE", *StopFamily])
@pytest.mark.parametrize("hard_stop", [None, *HardStop])
def test_all_closed_phase_outcome_and_stop_values(
    phase: PhaseReached,
    outcome: Any,
    hard_stop: HardStop | None,
) -> None:
    assert make_digest(
        **_kwargs(
            phase_reached=phase,
            outcome=outcome,
            hard_stop=hard_stop,
        )
    ).to_prompt_json()


@pytest.mark.parametrize(
    "overrides",
    [
        {"issuer_type": "MUNICIPAL"},
        {"phase_reached": "prehold"},
        {"outcome": "unsupported"},
        {"hard_stop": "S8"},
        {"match_outcome": "single"},
        {"veto_codes": ("UNSAFE_URL",)},
        {"ai_cost_cents": -1},
        {"ai_cost_cents": 1251},
        {"ai_cost_cents": True},
        {"ai_cost_cents": 1.5},
        {"delegation_counts": {DelegationOutcome.UNRESOLVED: -1}},
        {"delegation_counts": {"OTHER": 1}},
        {"delegation_counts": {DelegationOutcome.UNRESOLVED: True}},
        {"mail_class": ""},
        {"ticket_type": ""},
        {"stage_token": ""},
        {"basis_key_token": "TRIBE:example"},
        {"feasibility_margin_days": "6"},
    ],
)
def test_new_field_validation(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match=":"):
        make_digest(**_kwargs(**overrides))


def agency_fixture() -> ResolvedAgency:
    return ResolvedAgency(
        id="never_log_this_id",
        name="Never log this name",
        state="CA",
        official_domains=("sf.gov", "other.gov"),
        issuer_type=IssuerType.MUNICIPAL,
        deciding_body="Never log this body",
        basis_key="CA",
    )


def test_issuer_token_uses_only_basis_and_first_official_domain() -> None:
    agency = agency_fixture()
    assert issuer_token(agency) == "CA_sf_gov"
    assert issuer_token(
        dataclasses.replace(
            agency,
            id="another_id",
            name="Another name",
            deciding_body="Another body",
        )
    ) == issuer_token(agency)
    assert (
        issuer_token(
            dataclasses.replace(
                agency,
                issuer_type=IssuerType.FEDERAL,
                basis_key="US-FED",
            )
        )
        == "US-FED_sf_gov"
    )
    assert (
        issuer_token(
            dataclasses.replace(
                agency,
                issuer_type=IssuerType.TRIBAL,
                basis_key="TRIBE:example.group",
            )
        )
        == "TRIBE_example_group_sf_gov"
    )


def test_long_issuer_token_is_shortened_stably_without_colliding() -> None:
    first = issuer_token(
        dataclasses.replace(agency_fixture(), official_domains=("a" * 38 + ".gov",))
    )
    second = issuer_token(
        dataclasses.replace(agency_fixture(), official_domains=("a" * 37 + "b.gov",))
    )
    assert len(first) == 40
    assert len(second) == 40
    assert first[:31] == second[:31]
    assert first != second
    assert first == issuer_token(
        dataclasses.replace(agency_fixture(), official_domains=("a" * 38 + ".gov",))
    )
    assert not any(c.isdigit() for c in first[32:])


def test_issuer_token_still_rejects_digit_heavy_identities() -> None:
    with pytest.raises(ValueError, match="issuer_token:"):
        issuer_token(dataclasses.replace(agency_fixture(), official_domains=("ticket123456.gov",)))


def test_direct_constructor_cannot_accept_mutable_or_noncanonical_fields() -> None:
    digest = make_digest(**_kwargs())
    for field in ("veto_codes", "states_reached", "delegation_counts"):
        invalid: dict[str, Any] = {field: []}
        with pytest.raises(ValueError, match=":"):
            dataclasses.replace(digest, **invalid)

    for pairs in (
        ((DelegationOutcome.UNRESOLVED, 1), (DelegationOutcome.ALREADY_OFFICIAL, 2)),
        ((DelegationOutcome.UNRESOLVED, 1), (DelegationOutcome.UNRESOLVED, 2)),
        (("UNRESOLVED", 1),),
    ):
        invalid = {"delegation_counts": pairs}
        with pytest.raises(ValueError, match=":"):
            dataclasses.replace(digest, **invalid)


@pytest.mark.parametrize("direct", [False, True])
@pytest.mark.parametrize("exclusion", JurisdictionExclusion)
@pytest.mark.parametrize("outcome", list(StopFamily))
def test_research_failure_can_never_be_recorded_as_unsupported(
    direct: bool, exclusion: JurisdictionExclusion, outcome: StopFamily
) -> None:
    values: dict[str, Any] = {
        "phase_reached": PhaseReached.RESEARCHING,
        "research_status": ResearchStatus.REQUIRED_FACTS_UNVERIFIED,
        "jurisdiction_exclusion": exclusion,
        "outcome": outcome,
    }
    if direct:
        with pytest.raises(ValueError, match=r"research_status:|jurisdiction_exclusion:"):
            dataclasses.replace(make_digest(**_kwargs()), **values)
    else:
        with pytest.raises(ValueError, match=r"research_status:|jurisdiction_exclusion:"):
            make_digest(**_kwargs(**values))


def test_research_measurement_fields_round_trip_without_customer_content() -> None:
    digest = make_digest(
        **_kwargs(
            phase_reached=PhaseReached.RESEARCHING,
            outcome=StopFamily.NOT_YET_VERIFIED,
            research_status=ResearchStatus.REQUIRED_FACTS_UNVERIFIED,
        )
    )
    data = json.loads(digest.to_prompt_json())
    assert data["research_status"] == "REQUIRED_FACTS_UNVERIFIED"
    assert data["jurisdiction_exclusion"] is None
