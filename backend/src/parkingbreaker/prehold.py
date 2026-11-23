"""Pre-hold segment (HANDOFF 1.2): issuer resolution, then the intake support check.

The one production composition of the steps taken before any card hold. Customer intake
and the bench both call run_prehold. Clock, searcher, fetcher and AI assessors are
injected; nothing here reads the environment.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import date

from parkingbreaker.agencies import ResolvedAgency
from parkingbreaker.llm import LlmBudgetExceeded, LlmProviderError
from parkingbreaker.research.issuer_assessment import IssuerAssessor
from parkingbreaker.research.issuer_resolution import IssuerResolutionRun, resolve_ticket_issuer
from parkingbreaker.research.jurisdiction import DotGovSnapshot, IssuerQuery
from parkingbreaker.research.link_assessment import LinkAssessor
from parkingbreaker.research.search import Searcher, SearchError
from parkingbreaker.research.sources import Clock, Fetcher, FetchError
from parkingbreaker.service_policy import (
    IntakeSupportResult,
    IssuerKind,
    ServiceControlEntry,
    StateBasis,
    held_state_before_resolution,
    intake_support_check,
    unrecorded_basis_stop,
)

# Provider failures before a hold become a verification stop, never an exclusion.
PROVIDER_ERRORS = (LlmBudgetExceeded, LlmProviderError, FetchError, SearchError)


@dataclass(frozen=True)
class PreholdRequest:
    query: IssuerQuery
    printed_stage: str | None
    issuer_kind: IssuerKind
    notice_date: date
    printed_deadline: date | None
    today: date


@dataclass(frozen=True)
class PreholdDeps:
    snapshot: DotGovSnapshot
    searcher: Searcher | None
    fetcher: Fetcher
    issuer_assessor: IssuerAssessor
    link_assessor: LinkAssessor
    clock: Clock
    budget_cents: int
    state_basis: Mapping[str, StateBasis]
    controls: Sequence[ServiceControlEntry]


@dataclass(frozen=True)
class PreholdResult:
    issuer: IssuerResolutionRun | None
    support: IntakeSupportResult
    # Set when a provider failed during issuer resolution; the exception is kept for the
    # caller's own trace and is never shown to a customer.
    provider_error: Exception | None = None

    @property
    def agency(self) -> ResolvedAgency | None:
        return None if self.issuer is None else self.issuer.agency


def run_prehold(
    request: PreholdRequest,
    deps: PreholdDeps,
    *,
    launch_allowlist: AbstractSet[str] | None,
) -> PreholdResult:
    """Resolve only issuers whose intake can use an official source."""
    issuer: IssuerResolutionRun | None = None
    error: Exception | None = None
    if request.issuer_kind is not IssuerKind.PRIVATE:
        # Query state is the customer-confirmed violation state, never a mailing address.
        if held_state_before_resolution(request.query.customer_state, deps.state_basis):
            if request.notice_date <= request.today:
                return PreholdResult(None, unrecorded_basis_stop())
        else:
            try:
                issuer = resolve_ticket_issuer(
                    request.query,
                    request.printed_stage,
                    deps.snapshot,
                    request.today,
                    deps.searcher,
                    deps.fetcher,
                    deps.issuer_assessor,
                    deps.link_assessor,
                    deps.clock,
                    budget_cents=deps.budget_cents,
                )
            except PROVIDER_ERRORS as exc:
                error = exc
    support = intake_support_check(
        None if issuer is None else issuer.agency,
        request.issuer_kind,
        deps.state_basis,
        deps.controls,
        request.notice_date,
        request.printed_deadline,
        request.today,
        launch_allowlist=launch_allowlist,
    )
    return PreholdResult(issuer, support, error)
