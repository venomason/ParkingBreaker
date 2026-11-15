from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from parkingbreaker.case_state import HardStop, StopFamily
from parkingbreaker.oplog.counters import CounterKey, counter_keys, decode_counter_key
from parkingbreaker.oplog.digest import (
    CounterMilestone,
    JurisdictionExclusion,
    PhaseReached,
    ResearchStatus,
    make_digest,
)
from test_oplog_digest import _kwargs


@pytest.mark.parametrize("stage", [None, "initial_review", "hearing"])
@pytest.mark.parametrize("outcome", ["SERVICEABLE", *StopFamily])
@pytest.mark.parametrize("stop", [None, *HardStop])
def test_one_count_key_for_each_attempt(
    stage: str | None,
    outcome: Any,
    stop: HardStop | None,
) -> None:
    digest = make_digest(
        **_kwargs(
            stage_token=stage,
            outcome=outcome,
            hard_stop=stop,
            phase_reached=PhaseReached.PREHOLD,
        )
    )
    keys = counter_keys(digest)
    assert keys[0] == CounterKey(
        "CA_sf_gov", "CA", "street_cleaning", stage or "unknown", outcome, stop
    )
    assert len(keys) == (1 if outcome == "SERVICEABLE" else 2)
    assert {f.name for f in dataclasses.fields(CounterKey)} == {
        "issuer_token",
        "basis_key_token",
        "ticket_type",
        "stage_token",
        "outcome",
        "hard_stop",
        "milestone",
        "jurisdiction_exclusion",
    }
    assert hash(keys) == hash(counter_keys(digest))
    with pytest.raises(dataclasses.FrozenInstanceError):
        keys[0].stage_token = "changed"  # type: ignore[misc]


def test_counts_do_not_split_by_cost_week_or_veto_details() -> None:
    digest = make_digest(**_kwargs())
    other = dataclasses.replace(digest, cost_cents=2000, ai_cost_cents=20, iso_week="2026-W42")
    assert counter_keys(digest) == counter_keys(other)


def test_unknown_city_failed_verification_stays_in_research_denominator() -> None:
    digest = make_digest(
        **_kwargs(
            issuer_token="unknown",
            phase_reached=PhaseReached.RESEARCHING,
            outcome=StopFamily.NOT_YET_VERIFIED,
            research_status=ResearchStatus.REQUIRED_FACTS_UNVERIFIED,
        )
    )
    assert tuple(key.milestone for key in counter_keys(digest)) == (
        CounterMilestone.REQUEST_RECEIVED,
        CounterMilestone.REQUIRED_FACTS_UNVERIFIED,
    )


@pytest.mark.parametrize("exclusion", JurisdictionExclusion)
def test_only_explicit_jurisdiction_exclusions_are_unsupported(
    exclusion: JurisdictionExclusion,
) -> None:
    digest = make_digest(
        **_kwargs(
            phase_reached=PhaseReached.PREHOLD,
            outcome=StopFamily.NOT_YET_VERIFIED
            if exclusion is JurisdictionExclusion.UNRECORDED_BASIS
            else StopFamily.BUSINESS_EXCLUSION,
            jurisdiction_exclusion=exclusion,
        )
    )
    received, unsupported = counter_keys(digest)
    assert received.milestone is CounterMilestone.REQUEST_RECEIVED
    assert unsupported.milestone is CounterMilestone.GENUINELY_UNSUPPORTED
    assert unsupported.jurisdiction_exclusion is exclusion
    assert decode_counter_key(unsupported.to_json()) == unsupported


def test_private_operator_scope_exit_is_not_an_unsupported_jurisdiction() -> None:
    digest = make_digest(
        **_kwargs(phase_reached=PhaseReached.PREHOLD, outcome=StopFamily.BUSINESS_EXCLUSION)
    )
    assert tuple(key.milestone for key in counter_keys(digest)) == (
        CounterMilestone.REQUEST_RECEIVED,
        CounterMilestone.OTHER_EXIT,
    )


def test_tier1_pass_and_mailed_completion_are_separate_milestones() -> None:
    digest = make_digest(
        **_kwargs(phase_reached=PhaseReached.DRAFTING, research_status=ResearchStatus.TIER1_PASSED)
    )
    assert tuple(key.milestone for key in counter_keys(digest)) == (
        CounterMilestone.REQUEST_RECEIVED,
        CounterMilestone.TIER1_PASSED,
    )
    mailed = dataclasses.replace(digest, phase_reached=PhaseReached.MAILED)
    assert tuple(key.milestone for key in counter_keys(mailed)) == (
        CounterMilestone.REQUEST_RECEIVED,
        CounterMilestone.TIER1_PASSED,
        CounterMilestone.APPEAL_COMPLETED,
    )


def test_serviceable_alone_does_not_claim_a_tier1_pass() -> None:
    keys = counter_keys(make_digest(**_kwargs(phase_reached=PhaseReached.RESEARCHING)))
    assert len(keys) == 1
    assert keys[0].milestone is CounterMilestone.REQUEST_RECEIVED


@pytest.mark.parametrize("field", [field.name for field in dataclasses.fields(CounterKey)])
@pytest.mark.parametrize(
    "unsafe", ["123 Main St", "https://sf.gov/contest", "AB12CD", "2026-10-14"]
)
def test_counter_validation_rejects_customer_content(field: str, unsafe: str) -> None:
    values: dict[str, Any] = {field: unsafe}
    with pytest.raises(ValueError, match=":"):
        dataclasses.replace(CounterKey(), **values)


def test_unsupported_counter_requires_an_explicit_policy_basis() -> None:
    with pytest.raises(ValueError, match="explicit service-policy exclusion"):
        CounterKey(
            milestone=CounterMilestone.GENUINELY_UNSUPPORTED,
            outcome=StopFamily.NOT_YET_VERIFIED,
        )
