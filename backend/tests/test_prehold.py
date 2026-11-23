"""Pre-hold composition: issuer resolution then the intake check (HANDOFF 1.2)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Any

import pytest

from parkingbreaker import prehold
from parkingbreaker.case_state import HardStop, StopFamily
from parkingbreaker.prehold import PreholdDeps, PreholdRequest, run_prehold
from parkingbreaker.research.jurisdiction import IssuerQuery
from parkingbreaker.research.search import SearchError
from parkingbreaker.service_policy import (
    LAUNCH_ALLOWLIST,
    LAUNCH_EXCLUSION_DETAIL,
    SERVICE_CONTROL,
    STATE_BASIS,
    IntakeSupport,
    IssuerKind,
)
from samples import SAN_FRANCISCO

TODAY = date(2026, 10, 15)


class Resolved:
    def __init__(self, agency: object) -> None:
        self.agency = agency


def request(kind: IssuerKind = IssuerKind.GOVERNMENT) -> PreholdRequest:
    return PreholdRequest(
        IssuerQuery("San Francisco Municipal Transportation Agency", "San Francisco", "CA"),
        "first-level protest",
        kind,
        date(2026, 10, 10),
        None,
        TODAY,
    )


def deps() -> PreholdDeps:
    unused: Any = object()
    return PreholdDeps(
        unused,
        None,
        unused,
        unused,
        unused,
        lambda: datetime(2026, 10, 15, tzinfo=UTC),
        25,
        STATE_BASIS,
        SERVICE_CONTROL,
    )


def test_resolved_sfmta_is_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def resolve(*args: object, budget_cents: int, **kwargs: object) -> Resolved:
        calls.append(budget_cents)
        return Resolved(SAN_FRANCISCO)

    monkeypatch.setattr(prehold, "resolve_ticket_issuer", resolve)
    result = run_prehold(request(), deps(), launch_allowlist=LAUNCH_ALLOWLIST)
    assert result.support.status is IntakeSupport.SUPPORTED
    assert result.agency is SAN_FRANCISCO
    assert result.provider_error is None
    assert calls == [25]


def test_private_notice_is_excluded_without_any_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def resolve(*_args: object, **_kwargs: object) -> None:
        pytest.fail("no AI or search spend on excluded scope")

    monkeypatch.setattr(prehold, "resolve_ticket_issuer", resolve)
    result = run_prehold(request(IssuerKind.PRIVATE), deps(), launch_allowlist=LAUNCH_ALLOWLIST)
    assert result.support.family is StopFamily.BUSINESS_EXCLUSION
    assert result.issuer is None


def test_provider_failure_is_a_verification_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    def resolve(*_args: object, **_kwargs: object) -> None:
        raise SearchError("rate limited")

    monkeypatch.setattr(prehold, "resolve_ticket_issuer", resolve)
    result = run_prehold(request(), deps(), launch_allowlist=LAUNCH_ALLOWLIST)
    assert result.support.status is IntakeSupport.NOT_YET_VERIFIED
    assert isinstance(result.provider_error, SearchError)
    assert result.support.hard_stop is not HardStop.S4


def test_launch_allowlist_applies_to_resolved_agencies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prehold, "resolve_ticket_issuer", lambda *a, **k: Resolved(SAN_FRANCISCO))
    result = run_prehold(request(), deps(), launch_allowlist=frozenset({"lacity.gov"}))
    assert result.support.detail == LAUNCH_EXCLUSION_DETAIL


@pytest.mark.parametrize("state", ["AZ", "NV"])
@pytest.mark.parametrize("kind", [IssuerKind.GOVERNMENT, IssuerKind.PRIVATE])
def test_held_state_uses_no_issuer_providers(
    state: str, kind: IssuerKind, monkeypatch: pytest.MonkeyPatch
) -> None:
    def resolve(*_args: object, **_kwargs: object) -> None:
        pytest.fail("held or private intake must not enter search, fetch or issuer assessment")

    monkeypatch.setattr(prehold, "resolve_ticket_issuer", resolve)
    inquiry = request(kind)
    result = run_prehold(
        replace(inquiry, query=replace(inquiry.query, customer_state=state)),
        deps(),
        launch_allowlist=None,
    )
    assert result.issuer is None
    assert result.provider_error is None
    if kind is IssuerKind.PRIVATE:
        assert result.support.status is IntakeSupport.BUSINESS_EXCLUSION
        assert result.support.family is StopFamily.BUSINESS_EXCLUSION
        assert result.support.hard_stop is None
        assert result.support.detail == "Private parking notices are excluded by product scope."
    else:
        assert result.support.status is IntakeSupport.NOT_YET_VERIFIED
        assert result.support.family is StopFamily.NOT_YET_VERIFIED
        assert result.support.hard_stop is HardStop.S6
        assert result.support.detail == "Service basis is not yet recorded."
