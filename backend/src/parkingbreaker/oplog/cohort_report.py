"""Cohort report: per-appeal telemetry grouped for export, with small groups hidden. Pure.

Distinct customers are counted inside the restricted boundary and never exported. A cohort
appears only when it has at least ``min_cohort`` distinct customers (default ten); an
unset or invalid minimum suppresses all detail.

Comparing two exports must not reveal one customer, so figures are released in batches: a
cohort's published row changes only once at least ``min_cohort`` customers have joined it
since its last published row; until then the previous row is published again unchanged.
The caller keeps the returned release state inside the restricted boundary and passes it
back for the next export.

State (US state codes and DC) is the only export dimension: two overlapping breakdowns of
the same appeals could be subtracted to expose a hidden customer, so no second dimension is
offered. Any outcome or returned-mail cell below the minimum is withheld (None), and when
exactly one outcome cell is withheld the next smallest non-zero one is withheld too, so it
cannot be recovered from the appeal total. Carried releases are re-checked against the
current minimum and state list on every export.
Inputs are appeal telemetry from the event log only; the AI journal is not an input.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from parkingbreaker.oplog.appeal_events import AppealOutcome, AppealTelemetry
from parkingbreaker.policy_record import US_STATES_AND_DC

DEFAULT_MIN_COHORT = 10
DEFAULT_COHORTS = US_STATES_AND_DC


@dataclass(frozen=True)
class CohortRow:
    cohort: str
    appeals: int
    outcomes: tuple[tuple[AppealOutcome, int | None], ...]
    returned: int | None


@dataclass(frozen=True)
class Release:
    """Restricted state: the customers behind a cohort's last published row."""

    customers: frozenset[str]
    row: CohortRow  # exact counts; never exported as is


def _row(cohort: str, items: list[AppealTelemetry]) -> CohortRow:
    outcomes = Counter(item.outcome for item in items)
    return CohortRow(
        cohort,
        len(items),
        tuple((outcome, outcomes[outcome]) for outcome in AppealOutcome),
        sum(item.returned for item in items),
    )


def cohort_report(
    rows: Iterable[tuple[str, str, AppealTelemetry]],
    min_cohort: object = DEFAULT_MIN_COHORT,
    previous: Mapping[str, Release] | None = None,
    allowed_cohorts: frozenset[str] = DEFAULT_COHORTS,
) -> tuple[tuple[CohortRow, ...], dict[str, Release]]:
    """Rows are (cohort, restricted customer key, telemetry). Returns (export, new state)."""
    if type(min_cohort) is not int or min_cohort < DEFAULT_MIN_COHORT:
        return (), dict(previous or {})
    customers: dict[str, set[str]] = defaultdict(set)
    grouped: dict[str, list[AppealTelemetry]] = defaultdict(list)
    for cohort, customer, telemetry in rows:
        if cohort not in allowed_cohorts:
            raise ValueError("cohort is not an allowed category")
        if not isinstance(customer, str) or not customer.strip():
            raise ValueError("customer key required")
        if type(telemetry) is not AppealTelemetry:
            raise ValueError("telemetry required")
        customers[cohort].add(customer)
        grouped[cohort].append(telemetry)
    state = dict(previous or {})
    for cohort in grouped:
        current = frozenset(customers[cohort])
        released = state.get(cohort)
        joined = len(current - released.customers) if released else len(current)
        if joined >= min_cohort:
            state[cohort] = Release(current, _row(cohort, grouped[cohort]))
    export = tuple(
        _published(state[name].row, min_cohort)
        for name in sorted(state)
        if name in allowed_cohorts and len(state[name].customers) >= min_cohort
    )
    return export, state


def _published(row: CohortRow, minimum: int) -> CohortRow:
    cells = [(outcome, count) for outcome, count in row.outcomes]
    hidden = {i for i, (_, count) in enumerate(cells) if count and count < minimum}
    if len(hidden) == 1:
        # Secondary suppression: one withheld cell would equal total minus the rest.
        visible = [i for i, (_, count) in enumerate(cells) if count and i not in hidden]
        if visible:
            hidden.add(min(visible, key=lambda i: cells[i][1] or 0))
    returned = row.returned if not row.returned or row.returned >= minimum else None
    return CohortRow(
        row.cohort,
        row.appeals,
        tuple(
            (outcome, None if i in hidden else count) for i, (outcome, count) in enumerate(cells)
        ),
        returned,
    )
