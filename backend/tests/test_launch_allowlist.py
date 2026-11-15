"""SF launch allowlist at intake (docs/specs/lane-c-minimum-spec.md L1)."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from parkingbreaker.agencies import ResolvedAgency, SourceScope
from parkingbreaker.case_state import HardStop, StopFamily
from parkingbreaker.service_policy import (
    LAUNCH_ALLOWLIST,
    LAUNCH_EXCLUSION_DETAIL,
    SERVICE_CONTROL,
    STATE_BASIS,
    IntakeSupport,
    IntakeSupportResult,
    IssuerKind,
    intake_support_check,
    on_launch_allowlist,
)
from samples import SAN_FRANCISCO

TODAY = date(2026, 10, 15)
ISSUED = date(2026, 10, 10)
LADOT = replace(
    SAN_FRANCISCO,
    id="CA:ladot.lacity.gov",
    name="Los Angeles Department of Transportation",
    official_domains=("lacity.gov",),
    deciding_body="LADOT Parking Violations Bureau",
)
SEATTLE = replace(
    SAN_FRANCISCO,
    id="WA:seattle.gov",
    name="Seattle Municipal Court",
    state="WA",
    official_domains=("seattle.gov",),
    deciding_body="Seattle Municipal Court",
    basis_key="WA",
)


def check(
    agency: ResolvedAgency | None,
    kind: IssuerKind = IssuerKind.GOVERNMENT,
    allowlist: frozenset[str] = LAUNCH_ALLOWLIST,
) -> IntakeSupportResult:
    return intake_support_check(
        agency, kind, STATE_BASIS, SERVICE_CONTROL, ISSUED, None, TODAY, launch_allowlist=allowlist
    )


def test_sfmta_is_served_at_launch() -> None:
    assert check(SAN_FRANCISCO).status is IntakeSupport.SUPPORTED


@pytest.mark.parametrize("agency", [LADOT, SEATTLE], ids=["ladot", "seattle"])
def test_other_agencies_get_the_plain_launch_exclusion(agency: ResolvedAgency) -> None:
    result = check(agency)
    assert result.status is IntakeSupport.BUSINESS_EXCLUSION
    assert result.family is StopFamily.BUSINESS_EXCLUSION
    assert result.detail == LAUNCH_EXCLUSION_DETAIL
    assert result.hard_stop is None


def test_unresolved_agency_keeps_its_not_yet_verified_reason() -> None:
    result = check(None)
    assert result.status is IntakeSupport.NOT_YET_VERIFIED
    assert result.detail != LAUNCH_EXCLUSION_DETAIL


def test_private_lot_keeps_its_private_reason() -> None:
    result = check(None, IssuerKind.PRIVATE)
    assert result.family is StopFamily.BUSINESS_EXCLUSION
    assert result.detail == "Private parking notices are excluded by product scope."


def test_allowlist_runs_before_the_state_basis_check() -> None:
    # With no recorded basis at all, the launch reason must still win over S6.
    result = intake_support_check(
        LADOT,
        IssuerKind.GOVERNMENT,
        {},
        SERVICE_CONTROL,
        ISSUED,
        None,
        TODAY,
        launch_allowlist=LAUNCH_ALLOWLIST,
    )
    assert result.detail == LAUNCH_EXCLUSION_DETAIL
    assert result.hard_stop is not HardStop.S6


def test_empty_allowlist_serves_nothing() -> None:
    assert check(SAN_FRANCISCO, allowlist=frozenset()).detail == LAUNCH_EXCLUSION_DETAIL


def test_delegated_scope_host_matches() -> None:
    delegated = replace(
        SAN_FRANCISCO,
        id="CA:sf.gov",
        official_domains=("sf.gov",),
        delegated_scopes=(SourceScope("sfmta.com", "/getting-around"),),
    )
    assert on_launch_allowlist(delegated, LAUNCH_ALLOWLIST)
    assert not on_launch_allowlist(LADOT, LAUNCH_ALLOWLIST)


def test_launch_allowlist_default_is_sfmta_only() -> None:
    assert frozenset({"sfmta.com"}) == LAUNCH_ALLOWLIST
