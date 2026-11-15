"""Pure service-data retention eligibility for a proposed one-calendar-year policy.

This module is a local implementation primitive only. It records a proposed policy and
evaluates explicit caller-supplied facts; it does not adopt owner policy, authorize
deletion, schedule work, read files or touch a database. Every refusal is a fixed enum
member and no customer content, path or exception text is carried in a decision.

Closure is never inferred. A case is eligible only when the caller supplies exactly one
explicit CLOSED or ABANDONED anchor with a timezone-aware timestamp, the proposed policy
has been explicitly adopted, the evaluated instant is at or after the calendar anniversary
boundary in the policy timezone, no active preservation hold applies to that scope, and an
explicit blocker record confirms that no money, submission, original-mail, deadline,
incident or recovery side effect remains unresolved. A proposed policy is not an adopted
policy, and service-data expiry never authorizes restricted financial/provider schedules or
protected unknown data. Missing facts, including an omitted blocker record, fail closed.
"""

from __future__ import annotations

import calendar
import dataclasses
import enum
from datetime import UTC, datetime
from typing import Final
from zoneinfo import ZoneInfo

PROPOSED_RETENTION_YEARS: Final = 1


class PolicyAdoption(enum.StrEnum):
    """Whether the proposed policy is only proposed or has been explicitly adopted."""

    PROPOSED = "proposed"
    ADOPTED = "adopted"


class DataScope(enum.StrEnum):
    """The only scope this evaluator can expire is ordinary service data."""

    SERVICE_DATA = "service_data"
    FINANCIAL_PROVIDER_DATA = "financial_provider_data"
    UNKNOWN_PROTECTED_DATA = "unknown_protected_data"


class ClosureKind(enum.StrEnum):
    """Explicit closure evidence. Nothing else counts as closure."""

    CLOSED = "closed"
    ABANDONED = "abandoned"


class RetentionRefused(enum.StrEnum):
    """Stable fail-closed reasons. Values never contain customer content or paths."""

    POLICY_MISSING = "policy_missing"
    POLICY_NOT_ADOPTED = "policy_not_adopted"
    POLICY_INVALID = "policy_invalid"
    SCOPE_INVALID = "scope_invalid"
    FINANCIAL_PROVIDER_SCHEDULE_SEPARATE = "financial_provider_schedule_separate"
    UNKNOWN_DATA_PROTECTED = "unknown_data_protected"
    ANCHOR_MISSING = "anchor_missing"
    ANCHOR_AMBIGUOUS = "anchor_ambiguous"
    ANCHOR_INVALID = "anchor_invalid"
    ANCHOR_NAIVE_TIMESTAMP = "anchor_naive_timestamp"
    ANCHOR_FUTURE_TIMESTAMP = "anchor_future_timestamp"
    EVALUATION_TIME_INVALID = "evaluation_time_invalid"
    EVALUATION_TIME_NAIVE = "evaluation_time_naive"
    BOUNDARY_INVALID = "boundary_invalid"
    RETENTION_PERIOD_NOT_ELAPSED = "retention_period_not_elapsed"
    PRESERVATION_HOLD_ACTIVE = "preservation_hold_active"
    MONEY_UNRESOLVED = "money_unresolved"
    SUBMISSION_UNRESOLVED = "submission_unresolved"
    ORIGINAL_MAIL_UNRESOLVED = "original_mail_unresolved"
    DEADLINE_UNRESOLVED = "deadline_unresolved"
    INCIDENT_UNRESOLVED = "incident_unresolved"
    RECOVERY_UNRESOLVED = "recovery_unresolved"
    INPUT_INVALID = "input_invalid"


@dataclasses.dataclass(frozen=True)
class RetentionPolicy:
    """An explicit one-year policy. `timezone` selects the anniversary wall clock."""

    adoption: PolicyAdoption
    timezone: ZoneInfo
    retention_years: int = PROPOSED_RETENTION_YEARS


@dataclasses.dataclass(frozen=True)
class ClosureAnchor:
    """One explicit closure or abandonment record supplied by the caller."""

    kind: ClosureKind
    timestamp: datetime


@dataclasses.dataclass(frozen=True)
class PreservationHold:
    """A scoped hold with no expiry: only an explicit release removes it."""

    scope: DataScope
    released: bool = False


@dataclasses.dataclass(frozen=True)
class UnresolvedBlockers:
    """Side effects that must all be resolved before service data can expire."""

    money: bool = False
    submission: bool = False
    original_mail: bool = False
    deadline: bool = False
    incident: bool = False
    recovery: bool = False


@dataclasses.dataclass(frozen=True)
class RetentionDecision:
    """A pure decision. Timestamps are UTC and no customer content is included."""

    eligible: bool
    refusals: tuple[RetentionRefused, ...]
    scope: DataScope | None
    boundary: datetime | None
    evaluated_at: datetime | None

    def to_dict(self) -> dict[str, object]:
        """Serialise fixed enum values and synthetic timestamps only."""

        return {
            "eligible": self.eligible,
            "refusals": [reason.value for reason in self.refusals],
            "scope": None if self.scope is None else self.scope.value,
            "boundary": None if self.boundary is None else self.boundary.isoformat(),
            "evaluated_at": (None if self.evaluated_at is None else self.evaluated_at.isoformat()),
            # This evaluator never activates the proposed policy or authorizes deletion.
            "retention_policy_activated": False,
            "deletion_authorized": False,
        }


def _is_aware(value: datetime) -> bool:
    try:
        return value.utcoffset() is not None
    except (OverflowError, TypeError, ValueError):
        return False


def _to_utc(value: datetime) -> datetime | None:
    """Convert an aware timestamp to UTC, or None when that instant is unrepresentable.

    `datetime.astimezone` raises `OverflowError` for an ordinary aware value close enough to
    `datetime.min`/`datetime.max` that subtracting its UTC offset leaves the representable
    range. Such a value is unusable input, not a date far in the past or future.
    """

    try:
        return value.astimezone(UTC)
    except (OSError, OverflowError, ValueError):
        return None


def _anniversary(anchor: datetime, policy_timezone: ZoneInfo, years: int) -> datetime | None:
    """Return the exact, unambiguous local-wall-clock anniversary converted to UTC.

    The local time of day is preserved. Feb 29 clips to Feb 28 in a non-leap target year.
    This is an exact boundary, never a 365-day approximation. The anniversary must be a
    real, unambiguous wall-clock instant in the policy timezone: both PEP 495 fold variants
    are round-tripped through UTC, and either a DST gap (no variant represents that wall
    clock) or a DST fold (two distinct instants represent it) yields no boundary at all.
    Nothing is normalised, and no fold is silently preferred.
    """

    try:
        local = anchor.astimezone(policy_timezone)
        target_year = local.year + years
        day = local.day
        if local.month == 2 and day == 29 and not calendar.isleap(target_year):
            day = 28
        wall_clock = local.replace(year=target_year, day=day).replace(tzinfo=None)
        instants: list[datetime] = []
        for fold in (0, 1):
            candidate = wall_clock.replace(tzinfo=policy_timezone, fold=fold)
            instant = candidate.astimezone(UTC)
            if instant.astimezone(policy_timezone).replace(tzinfo=None) == wall_clock:
                instants.append(instant)
        if not instants or any(instant != instants[0] for instant in instants):
            return None
        return instants[0]
    except (OverflowError, ValueError):
        return None


def _decision(
    refusals: list[RetentionRefused],
    *,
    scope: DataScope | None,
    boundary: datetime | None,
    evaluated_at: datetime | None,
) -> RetentionDecision:
    return RetentionDecision(
        eligible=not refusals,
        refusals=tuple(refusals),
        scope=scope,
        boundary=boundary,
        evaluated_at=evaluated_at,
    )


def evaluate_service_retention(
    policy: RetentionPolicy | None,
    *,
    scope: DataScope,
    evaluated_at: datetime,
    anchors: tuple[ClosureAnchor, ...] = (),
    holds: tuple[PreservationHold, ...] = (),
    blockers: UnresolvedBlockers | None = None,
) -> RetentionDecision:
    """Evaluate proposed service-data retention eligibility without any side effect.

    The caller supplies the policy, the scope, the actual evaluation time, every explicit
    closure anchor, every preservation hold and an explicit record of every unresolved side
    effect. Missing or contradictory facts fail closed: an omitted or None `blockers`
    argument is missing input, never evidence that all side effects are resolved. The
    anniversary wall clock must also be a real, unambiguous local instant in the policy
    timezone. The function never raises for malformed input and never consults a clock, a
    filesystem, a database, a network or a provider.
    """

    if policy is None:
        return _decision(
            [RetentionRefused.POLICY_MISSING],
            scope=None,
            boundary=None,
            evaluated_at=None,
        )
    if not isinstance(policy, RetentionPolicy):
        return _decision(
            [RetentionRefused.POLICY_INVALID],
            scope=None,
            boundary=None,
            evaluated_at=None,
        )
    if not isinstance(policy.adoption, PolicyAdoption):
        return _decision(
            [RetentionRefused.POLICY_INVALID],
            scope=None,
            boundary=None,
            evaluated_at=None,
        )
    if policy.adoption is not PolicyAdoption.ADOPTED:
        return _decision(
            [RetentionRefused.POLICY_NOT_ADOPTED],
            scope=None,
            boundary=None,
            evaluated_at=None,
        )
    if not isinstance(policy.timezone, ZoneInfo):
        return _decision(
            [RetentionRefused.POLICY_INVALID],
            scope=None,
            boundary=None,
            evaluated_at=None,
        )
    if (
        type(policy.retention_years) is not int
        or policy.retention_years != PROPOSED_RETENTION_YEARS
    ):
        return _decision(
            [RetentionRefused.POLICY_INVALID],
            scope=None,
            boundary=None,
            evaluated_at=None,
        )

    if not isinstance(scope, DataScope):
        return _decision(
            [RetentionRefused.SCOPE_INVALID],
            scope=None,
            boundary=None,
            evaluated_at=None,
        )
    if scope is DataScope.FINANCIAL_PROVIDER_DATA:
        return _decision(
            [RetentionRefused.FINANCIAL_PROVIDER_SCHEDULE_SEPARATE],
            scope=scope,
            boundary=None,
            evaluated_at=None,
        )
    if scope is DataScope.UNKNOWN_PROTECTED_DATA:
        return _decision(
            [RetentionRefused.UNKNOWN_DATA_PROTECTED],
            scope=scope,
            boundary=None,
            evaluated_at=None,
        )

    refusals: list[RetentionRefused] = []
    normalized_evaluated_at: datetime | None = None
    if not isinstance(evaluated_at, datetime):
        refusals.append(RetentionRefused.EVALUATION_TIME_INVALID)
    elif not _is_aware(evaluated_at):
        refusals.append(RetentionRefused.EVALUATION_TIME_NAIVE)
    else:
        normalized_evaluated_at = _to_utc(evaluated_at)
        if normalized_evaluated_at is None:
            # An aware timestamp whose UTC conversion falls outside the representable
            # range is invalid input, never a clean or elapsed decision.
            refusals.append(RetentionRefused.EVALUATION_TIME_INVALID)

    boundary: datetime | None = None
    if not isinstance(anchors, tuple):
        refusals.append(RetentionRefused.ANCHOR_INVALID)
    elif not anchors:
        refusals.append(RetentionRefused.ANCHOR_MISSING)
    elif len(anchors) > 1:
        refusals.append(RetentionRefused.ANCHOR_AMBIGUOUS)
    else:
        anchor = anchors[0]
        if (
            not isinstance(anchor, ClosureAnchor)
            or not isinstance(anchor.kind, ClosureKind)
            or not isinstance(anchor.timestamp, datetime)
        ):
            refusals.append(RetentionRefused.ANCHOR_INVALID)
        elif not _is_aware(anchor.timestamp):
            refusals.append(RetentionRefused.ANCHOR_NAIVE_TIMESTAMP)
        elif normalized_evaluated_at is None:
            # The evaluation-time refusal above already decides this evaluation.
            pass
        elif anchor.timestamp > normalized_evaluated_at:
            refusals.append(RetentionRefused.ANCHOR_FUTURE_TIMESTAMP)
        else:
            boundary = _anniversary(
                anchor.timestamp,
                policy.timezone,
                policy.retention_years,
            )
            if boundary is None:
                refusals.append(RetentionRefused.BOUNDARY_INVALID)

    if not isinstance(holds, tuple) or any(
        not isinstance(hold, PreservationHold)
        or not isinstance(hold.scope, DataScope)
        or type(hold.released) is not bool
        for hold in holds
    ):
        refusals.append(RetentionRefused.INPUT_INVALID)
    elif any(hold.scope is DataScope.SERVICE_DATA and not hold.released for hold in holds):
        refusals.append(RetentionRefused.PRESERVATION_HOLD_ACTIVE)

    if not isinstance(blockers, UnresolvedBlockers):
        # Omitted or None blocker facts are missing input, not an absence of side effects.
        refusals.append(RetentionRefused.INPUT_INVALID)
    else:
        blockers_by_field = (
            (blockers.money, RetentionRefused.MONEY_UNRESOLVED),
            (blockers.submission, RetentionRefused.SUBMISSION_UNRESOLVED),
            (blockers.original_mail, RetentionRefused.ORIGINAL_MAIL_UNRESOLVED),
            (blockers.deadline, RetentionRefused.DEADLINE_UNRESOLVED),
            (blockers.incident, RetentionRefused.INCIDENT_UNRESOLVED),
            (blockers.recovery, RetentionRefused.RECOVERY_UNRESOLVED),
        )
        if any(type(value) is not bool for value, _reason in blockers_by_field):
            refusals.append(RetentionRefused.INPUT_INVALID)
        else:
            refusals.extend(reason for value, reason in blockers_by_field if value)

    if (
        boundary is not None
        and normalized_evaluated_at is not None
        and normalized_evaluated_at < boundary
    ):
        refusals.append(RetentionRefused.RETENTION_PERIOD_NOT_ELAPSED)

    return _decision(
        refusals,
        scope=scope,
        boundary=boundary,
        evaluated_at=normalized_evaluated_at,
    )


__all__ = [
    "PROPOSED_RETENTION_YEARS",
    "ClosureAnchor",
    "ClosureKind",
    "DataScope",
    "PolicyAdoption",
    "PreservationHold",
    "RetentionDecision",
    "RetentionPolicy",
    "RetentionRefused",
    "UnresolvedBlockers",
    "evaluate_service_retention",
]
