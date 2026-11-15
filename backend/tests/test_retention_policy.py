from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

import pytest

from parkingbreaker.retention_inventory import TableClass, inventory_snapshot
from parkingbreaker.retention_policy import (
    ClosureAnchor,
    ClosureKind,
    DataScope,
    PolicyAdoption,
    PreservationHold,
    RetentionDecision,
    RetentionPolicy,
    RetentionRefused,
    UnresolvedBlockers,
    evaluate_service_retention,
)

UTC_ZONE = ZoneInfo("UTC")
LA_ZONE = ZoneInfo("America/Los_Angeles")
ADOPTED_UTC = RetentionPolicy(adoption=PolicyAdoption.ADOPTED, timezone=UTC_ZONE)
CLEAN = UnresolvedBlockers()


def evaluate(
    policy: RetentionPolicy | None,
    *,
    scope: DataScope = DataScope.SERVICE_DATA,
    evaluated_at: datetime,
    anchors: tuple[ClosureAnchor, ...] = (),
    holds: tuple[PreservationHold, ...] = (),
    blockers: UnresolvedBlockers | None = CLEAN,
) -> RetentionDecision:
    return evaluate_service_retention(
        policy,
        scope=scope,
        evaluated_at=evaluated_at,
        anchors=anchors,
        holds=holds,
        blockers=blockers,
    )


def closed_at(moment: datetime) -> tuple[ClosureAnchor, ...]:
    return (ClosureAnchor(kind=ClosureKind.CLOSED, timestamp=moment),)


def test_missing_policy_and_unadopted_policy_fail_closed() -> None:
    moment = datetime(2025, 1, 15, 12, 0, tzinfo=UTC)
    missing = evaluate(None, evaluated_at=moment, anchors=closed_at(moment))
    proposed = evaluate(
        RetentionPolicy(adoption=PolicyAdoption.PROPOSED, timezone=UTC_ZONE),
        evaluated_at=moment,
        anchors=closed_at(moment),
    )

    assert missing.eligible is False
    assert missing.refusals == (RetentionRefused.POLICY_MISSING,)
    assert proposed.eligible is False
    assert proposed.refusals == (RetentionRefused.POLICY_NOT_ADOPTED,)


@pytest.mark.parametrize(
    "policy",
    [
        RetentionPolicy(
            adoption=cast(PolicyAdoption, "adopted"),
            timezone=UTC_ZONE,
        ),
        RetentionPolicy(
            adoption=PolicyAdoption.ADOPTED,
            timezone=cast(ZoneInfo, "UTC"),
        ),
        RetentionPolicy(
            adoption=PolicyAdoption.ADOPTED,
            timezone=UTC_ZONE,
            retention_years=2,
        ),
        RetentionPolicy(
            adoption=PolicyAdoption.ADOPTED,
            timezone=UTC_ZONE,
            retention_years=cast(int, True),
        ),
    ],
)
def test_invalid_policy_fails_closed(policy: RetentionPolicy) -> None:
    moment = datetime(2025, 1, 15, 12, 0, tzinfo=UTC)
    decision = evaluate(policy, evaluated_at=moment, anchors=closed_at(moment))

    assert decision.eligible is False
    assert decision.refusals == (RetentionRefused.POLICY_INVALID,)


def test_wrong_policy_object_fails_closed() -> None:
    moment = datetime(2025, 1, 15, 12, 0, tzinfo=UTC)
    decision = evaluate_service_retention(
        cast(RetentionPolicy, object()),
        scope=DataScope.SERVICE_DATA,
        evaluated_at=moment,
        anchors=closed_at(moment),
    )

    assert decision.refusals == (RetentionRefused.POLICY_INVALID,)


def test_exact_utc_anniversary_boundary_not_a_365_day_approximation() -> None:
    anchor = datetime(2024, 1, 15, 12, 0, tzinfo=UTC)
    boundary = datetime(2025, 1, 15, 12, 0, tzinfo=UTC)
    # 365 days after the anchor is one day early because 2024 is a leap year.
    approximation = anchor + timedelta(days=365)
    assert approximation == datetime(2025, 1, 14, 12, 0, tzinfo=UTC)

    before = evaluate(
        ADOPTED_UTC,
        evaluated_at=boundary - timedelta(microseconds=1),
        anchors=closed_at(anchor),
    )
    approximation_time = evaluate(
        ADOPTED_UTC,
        evaluated_at=approximation,
        anchors=closed_at(anchor),
    )
    at_boundary = evaluate(
        ADOPTED_UTC,
        evaluated_at=boundary,
        anchors=closed_at(anchor),
    )
    after = evaluate(
        ADOPTED_UTC,
        evaluated_at=boundary + timedelta(microseconds=1),
        anchors=closed_at(anchor),
    )

    assert before.refusals == (RetentionRefused.RETENTION_PERIOD_NOT_ELAPSED,)
    assert approximation_time.refusals == (RetentionRefused.RETENTION_PERIOD_NOT_ELAPSED,)
    assert at_boundary.eligible is True
    assert at_boundary.refusals == ()
    assert at_boundary.boundary == boundary
    assert after.eligible is True


def test_los_angeles_anniversary_preserves_wall_clock_across_dst() -> None:
    # 03:30 was PST on the anchor date and is PDT on the anniversary date.
    anchor = datetime(2024, 3, 9, 3, 30, tzinfo=LA_ZONE)
    boundary = datetime(2025, 3, 9, 10, 30, tzinfo=UTC)
    policy = RetentionPolicy(adoption=PolicyAdoption.ADOPTED, timezone=LA_ZONE)

    before = evaluate(
        policy,
        evaluated_at=boundary - timedelta(microseconds=1),
        anchors=closed_at(anchor),
    )
    at_boundary = evaluate(policy, evaluated_at=boundary, anchors=closed_at(anchor))

    assert before.refusals == (RetentionRefused.RETENTION_PERIOD_NOT_ELAPSED,)
    assert at_boundary.eligible is True
    assert at_boundary.boundary == boundary
    assert at_boundary.boundary is not None
    assert at_boundary.boundary.astimezone(LA_ZONE) == datetime(2025, 3, 9, 3, 30, tzinfo=LA_ZONE)
    assert at_boundary.boundary.astimezone(LA_ZONE).utcoffset() == timedelta(hours=-7)


def test_los_angeles_ambiguous_fall_back_anniversary_is_refused() -> None:
    # 01:30 on 2026-11-01 occurs twice in Los Angeles (fall-back fold), so there is no
    # single unambiguous anniversary instant. Both possible instants exist, yet the
    # evaluator must refuse rather than silently choose the earlier fold, even long after
    # both of them have passed.
    anchor = datetime(2025, 11, 1, 1, 30, tzinfo=LA_ZONE)
    policy = RetentionPolicy(adoption=PolicyAdoption.ADOPTED, timezone=LA_ZONE)
    wall_clock = datetime(2026, 11, 1, 1, 30)
    early = wall_clock.replace(tzinfo=LA_ZONE, fold=0).astimezone(UTC)
    late = wall_clock.replace(tzinfo=LA_ZONE, fold=1).astimezone(UTC)
    assert early != late

    decision = evaluate(
        policy,
        evaluated_at=datetime(2026, 11, 2, 12, 0, tzinfo=UTC),
        anchors=closed_at(anchor),
    )

    assert decision.eligible is False
    assert decision.refusals == (RetentionRefused.BOUNDARY_INVALID,)
    assert decision.boundary is None


def test_los_angeles_nonexistent_spring_forward_anniversary_is_refused() -> None:
    # 02:30 does not exist in Los Angeles on 2025-03-09 (spring-forward gap). Neither fold
    # variant round-trips to that wall clock, so the anniversary cannot be represented and
    # the evaluator must refuse rather than normalise the time forward.
    anchor = datetime(2024, 3, 9, 2, 30, tzinfo=LA_ZONE)
    policy = RetentionPolicy(adoption=PolicyAdoption.ADOPTED, timezone=LA_ZONE)
    wall_clock = datetime(2025, 3, 9, 2, 30)
    for fold in (0, 1):
        candidate = wall_clock.replace(tzinfo=LA_ZONE, fold=fold)
        assert candidate.astimezone(UTC).astimezone(LA_ZONE).replace(tzinfo=None) != wall_clock

    decision = evaluate(
        policy,
        evaluated_at=datetime(2025, 3, 10, 12, 0, tzinfo=UTC),
        anchors=closed_at(anchor),
    )

    assert decision.eligible is False
    assert decision.refusals == (RetentionRefused.BOUNDARY_INVALID,)
    assert decision.boundary is None


def test_anniversary_converts_anchor_offset_to_policy_timezone() -> None:
    # 17:30 at UTC+05:30 is 04:00 in Los Angeles on the same civil date.
    anchor = datetime(
        2024,
        1,
        15,
        17,
        30,
        tzinfo=timezone(timedelta(hours=5, minutes=30)),
    )
    policy = RetentionPolicy(adoption=PolicyAdoption.ADOPTED, timezone=LA_ZONE)
    decision = evaluate(
        policy,
        evaluated_at=datetime(2025, 1, 15, 12, 0, tzinfo=UTC),
        anchors=closed_at(anchor),
    )

    assert decision.eligible is True
    assert decision.boundary == datetime(2025, 1, 15, 12, 0, tzinfo=UTC)


def test_february_29_anniversary_clips_to_february_28() -> None:
    anchor = datetime(2024, 2, 29, 12, 0, tzinfo=UTC)
    boundary = datetime(2025, 2, 28, 12, 0, tzinfo=UTC)

    before = evaluate(
        ADOPTED_UTC,
        evaluated_at=boundary - timedelta(microseconds=1),
        anchors=closed_at(anchor),
    )
    at_boundary = evaluate(ADOPTED_UTC, evaluated_at=boundary, anchors=closed_at(anchor))
    after = evaluate(
        ADOPTED_UTC,
        evaluated_at=boundary + timedelta(microseconds=1),
        anchors=closed_at(anchor),
    )

    assert before.refusals == (RetentionRefused.RETENTION_PERIOD_NOT_ELAPSED,)
    assert at_boundary.eligible is True
    assert at_boundary.boundary == boundary
    assert after.eligible is True


@pytest.mark.parametrize("kind", [ClosureKind.CLOSED, ClosureKind.ABANDONED])
def test_explicit_closed_and_abandoned_anchors_are_both_valid(kind: ClosureKind) -> None:
    anchor = datetime(2024, 1, 15, 12, 0, tzinfo=UTC)
    decision = evaluate(
        ADOPTED_UTC,
        evaluated_at=datetime(2025, 1, 15, 12, 0, tzinfo=UTC),
        anchors=(ClosureAnchor(kind=kind, timestamp=anchor),),
    )

    assert decision.eligible is True


def test_missing_anchor_is_never_inferred_from_elapsed_age_or_activity() -> None:
    decision = evaluate(
        ADOPTED_UTC,
        evaluated_at=datetime(2035, 1, 15, 12, 0, tzinfo=UTC),
        anchors=(),
    )

    assert decision.eligible is False
    assert decision.refusals == (RetentionRefused.ANCHOR_MISSING,)
    assert decision.boundary is None


def test_ambiguous_anchor_fails_closed() -> None:
    moment = datetime(2025, 1, 15, 12, 0, tzinfo=UTC)
    decision = evaluate(
        ADOPTED_UTC,
        evaluated_at=moment,
        anchors=(
            ClosureAnchor(kind=ClosureKind.CLOSED, timestamp=moment - timedelta(days=2)),
            ClosureAnchor(kind=ClosureKind.ABANDONED, timestamp=moment - timedelta(days=1)),
        ),
    )

    assert decision.eligible is False
    assert decision.refusals == (RetentionRefused.ANCHOR_AMBIGUOUS,)


@pytest.mark.parametrize(
    "anchor",
    [
        ClosureAnchor(kind=cast(ClosureKind, "closed"), timestamp=datetime(2024, 1, 1)),
        ClosureAnchor(
            kind=ClosureKind.CLOSED,
            timestamp=cast(datetime, "2024-01-01T00:00:00+00:00"),
        ),
    ],
)
def test_malformed_anchor_fields_fail_closed(anchor: ClosureAnchor) -> None:
    decision = evaluate(
        ADOPTED_UTC,
        evaluated_at=datetime(2035, 1, 15, 12, 0, tzinfo=UTC),
        anchors=(anchor,),
    )

    assert decision.eligible is False
    assert decision.refusals == (RetentionRefused.ANCHOR_INVALID,)


def test_naive_anchor_timestamp_fails_closed() -> None:
    decision = evaluate(
        ADOPTED_UTC,
        evaluated_at=datetime(2025, 1, 15, 12, 0, tzinfo=UTC),
        anchors=(ClosureAnchor(kind=ClosureKind.CLOSED, timestamp=datetime(2024, 1, 15, 12)),),
    )

    assert decision.refusals == (RetentionRefused.ANCHOR_NAIVE_TIMESTAMP,)


def test_future_anchor_timestamp_fails_closed() -> None:
    evaluated_at = datetime(2025, 1, 15, 12, 0, tzinfo=UTC)
    decision = evaluate(
        ADOPTED_UTC,
        evaluated_at=evaluated_at,
        anchors=closed_at(evaluated_at + timedelta(microseconds=1)),
    )

    assert decision.refusals == (RetentionRefused.ANCHOR_FUTURE_TIMESTAMP,)
    assert decision.boundary is None


def test_wrong_anchor_container_fails_closed() -> None:
    decision = evaluate(
        ADOPTED_UTC,
        evaluated_at=datetime(2025, 1, 15, 12, 0, tzinfo=UTC),
        anchors=cast(tuple[ClosureAnchor, ...], []),
    )

    assert decision.refusals == (RetentionRefused.ANCHOR_INVALID,)


def test_naive_evaluation_time_fails_closed() -> None:
    decision = evaluate(
        ADOPTED_UTC,
        evaluated_at=datetime(2025, 1, 15, 12, 0),
        anchors=closed_at(datetime(2024, 1, 15, 12, 0, tzinfo=UTC)),
    )

    assert decision.refusals == (RetentionRefused.EVALUATION_TIME_NAIVE,)


def test_wrong_evaluation_time_type_fails_closed() -> None:
    decision = evaluate(
        ADOPTED_UTC,
        evaluated_at=cast(datetime, 0),
        anchors=closed_at(datetime(2024, 1, 15, 12, 0, tzinfo=UTC)),
    )

    assert decision.refusals == (RetentionRefused.EVALUATION_TIME_INVALID,)


@pytest.mark.parametrize(
    "instant",
    [
        # datetime.min with a positive offset underflows when converted to UTC.
        datetime.min.replace(tzinfo=timezone(timedelta(hours=1))),
        # datetime.max with a negative offset overflows when converted to UTC.
        datetime.max.replace(tzinfo=timezone(timedelta(hours=-1))),
    ],
)
def test_extreme_aware_evaluation_time_fails_closed(instant: datetime) -> None:
    # An ordinary aware datetime can still be unrepresentable in UTC. That is invalid input,
    # never a clean or elapsed decision, and it must not raise.
    decision = evaluate(
        ADOPTED_UTC,
        evaluated_at=instant,
        anchors=closed_at(datetime(2024, 1, 15, 12, 0, tzinfo=UTC)),
    )

    assert decision.eligible is False
    assert decision.refusals == (RetentionRefused.EVALUATION_TIME_INVALID,)
    assert decision.evaluated_at is None
    assert decision.boundary is None


def test_extreme_aware_anchor_at_lower_bound_fails_closed() -> None:
    # Converting the anchor into the policy timezone underflows, so no anniversary exists
    # and the evaluator fails closed with BOUNDARY_INVALID instead of raising.
    anchor = datetime.min.replace(tzinfo=timezone(timedelta(hours=1)))
    decision = evaluate(
        ADOPTED_UTC,
        evaluated_at=datetime(2025, 1, 15, 12, 0, tzinfo=UTC),
        anchors=closed_at(anchor),
    )

    assert decision.eligible is False
    assert decision.refusals == (RetentionRefused.BOUNDARY_INVALID,)
    assert decision.boundary is None


def test_extreme_aware_anchor_at_upper_bound_fails_closed() -> None:
    # datetime.max with a negative offset is later than any representable UTC instant, so
    # the existing future-anchor guard refuses it without needing an anniversary conversion.
    anchor = datetime.max.replace(tzinfo=timezone(timedelta(hours=-1)))
    decision = evaluate(
        ADOPTED_UTC,
        evaluated_at=datetime(2025, 1, 15, 12, 0, tzinfo=UTC),
        anchors=closed_at(anchor),
    )

    assert decision.eligible is False
    assert decision.refusals == (RetentionRefused.ANCHOR_FUTURE_TIMESTAMP,)
    assert decision.boundary is None


def test_unrepresentable_anniversary_fails_closed() -> None:
    decision = evaluate(
        ADOPTED_UTC,
        evaluated_at=datetime(9999, 12, 31, 23, 59, tzinfo=UTC),
        anchors=closed_at(datetime(9999, 1, 15, 12, 0, tzinfo=UTC)),
    )

    assert decision.refusals == (RetentionRefused.BOUNDARY_INVALID,)


def test_active_service_scope_hold_blocks_eligibility() -> None:
    moment = datetime(2025, 1, 15, 12, 0, tzinfo=UTC)
    decision = evaluate(
        ADOPTED_UTC,
        evaluated_at=moment,
        anchors=closed_at(moment - timedelta(days=400)),
        holds=(PreservationHold(scope=DataScope.SERVICE_DATA),),
    )

    assert decision.eligible is False
    assert decision.refusals == (RetentionRefused.PRESERVATION_HOLD_ACTIVE,)


def test_explicit_release_removes_only_its_hold() -> None:
    moment = datetime(2025, 1, 15, 12, 0, tzinfo=UTC)
    decision = evaluate(
        ADOPTED_UTC,
        evaluated_at=moment,
        anchors=closed_at(moment - timedelta(days=400)),
        holds=(PreservationHold(scope=DataScope.SERVICE_DATA, released=True),),
    )

    assert decision.eligible is True


def test_hold_does_not_auto_expire() -> None:
    anchor = datetime(2024, 1, 15, 12, 0, tzinfo=UTC)
    decision = evaluate(
        ADOPTED_UTC,
        evaluated_at=datetime(2125, 1, 15, 12, 0, tzinfo=UTC),
        anchors=closed_at(anchor),
        holds=(PreservationHold(scope=DataScope.SERVICE_DATA),),
    )

    assert decision.refusals == (RetentionRefused.PRESERVATION_HOLD_ACTIVE,)


@pytest.mark.parametrize(
    "scope",
    [DataScope.FINANCIAL_PROVIDER_DATA, DataScope.UNKNOWN_PROTECTED_DATA],
)
def test_non_service_holds_do_not_block_service_scope(scope: DataScope) -> None:
    moment = datetime(2025, 1, 15, 12, 0, tzinfo=UTC)
    decision = evaluate(
        ADOPTED_UTC,
        evaluated_at=moment,
        anchors=closed_at(moment - timedelta(days=400)),
        holds=(PreservationHold(scope=scope),),
    )

    assert decision.eligible is True


def test_holds_apply_only_to_their_declared_scope() -> None:
    moment = datetime(2025, 1, 15, 12, 0, tzinfo=UTC)
    decision = evaluate(
        ADOPTED_UTC,
        evaluated_at=moment,
        anchors=closed_at(moment - timedelta(days=400)),
        holds=(
            PreservationHold(scope=DataScope.FINANCIAL_PROVIDER_DATA),
            PreservationHold(scope=DataScope.UNKNOWN_PROTECTED_DATA),
        ),
    )

    assert decision.eligible is True


def test_malformed_hold_fails_closed() -> None:
    moment = datetime(2025, 1, 15, 12, 0, tzinfo=UTC)
    decision = evaluate(
        ADOPTED_UTC,
        evaluated_at=moment,
        anchors=closed_at(moment - timedelta(days=400)),
        holds=(
            PreservationHold(
                scope=DataScope.SERVICE_DATA,
                released=cast(bool, 0),
            ),
        ),
    )

    assert decision.refusals == (RetentionRefused.INPUT_INVALID,)


def test_wrong_hold_container_fails_closed() -> None:
    moment = datetime(2025, 1, 15, 12, 0, tzinfo=UTC)
    decision = evaluate(
        ADOPTED_UTC,
        evaluated_at=moment,
        anchors=closed_at(moment - timedelta(days=400)),
        holds=cast(tuple[PreservationHold, ...], []),
    )

    assert decision.refusals == (RetentionRefused.INPUT_INVALID,)


@pytest.mark.parametrize(
    ("blockers", "reason"),
    [
        (UnresolvedBlockers(money=True), RetentionRefused.MONEY_UNRESOLVED),
        (UnresolvedBlockers(submission=True), RetentionRefused.SUBMISSION_UNRESOLVED),
        (
            UnresolvedBlockers(original_mail=True),
            RetentionRefused.ORIGINAL_MAIL_UNRESOLVED,
        ),
        (UnresolvedBlockers(deadline=True), RetentionRefused.DEADLINE_UNRESOLVED),
        (UnresolvedBlockers(incident=True), RetentionRefused.INCIDENT_UNRESOLVED),
        (UnresolvedBlockers(recovery=True), RetentionRefused.RECOVERY_UNRESOLVED),
    ],
)
def test_each_unresolved_side_effect_fails_closed(
    blockers: UnresolvedBlockers,
    reason: RetentionRefused,
) -> None:
    moment = datetime(2025, 1, 15, 12, 0, tzinfo=UTC)
    decision = evaluate(
        ADOPTED_UTC,
        evaluated_at=moment,
        anchors=closed_at(moment - timedelta(days=400)),
        blockers=blockers,
    )

    assert decision.eligible is False
    assert decision.refusals == (reason,)


def test_all_unresolved_side_effects_are_reported_together() -> None:
    moment = datetime(2025, 1, 15, 12, 0, tzinfo=UTC)
    decision = evaluate(
        ADOPTED_UTC,
        evaluated_at=moment,
        anchors=closed_at(moment - timedelta(days=400)),
        blockers=UnresolvedBlockers(
            money=True,
            submission=True,
            original_mail=True,
            deadline=True,
            incident=True,
            recovery=True,
        ),
    )

    assert decision.refusals == (
        RetentionRefused.MONEY_UNRESOLVED,
        RetentionRefused.SUBMISSION_UNRESOLVED,
        RetentionRefused.ORIGINAL_MAIL_UNRESOLVED,
        RetentionRefused.DEADLINE_UNRESOLVED,
        RetentionRefused.INCIDENT_UNRESOLVED,
        RetentionRefused.RECOVERY_UNRESOLVED,
    )


def test_malformed_blocker_fails_closed() -> None:
    moment = datetime(2025, 1, 15, 12, 0, tzinfo=UTC)
    decision = evaluate(
        ADOPTED_UTC,
        evaluated_at=moment,
        anchors=closed_at(moment - timedelta(days=400)),
        blockers=UnresolvedBlockers(money=cast(bool, "yes")),
    )

    assert decision.refusals == (RetentionRefused.INPUT_INVALID,)


def test_wrong_blocker_object_fails_closed() -> None:
    moment = datetime(2025, 1, 15, 12, 0, tzinfo=UTC)
    decision = evaluate(
        ADOPTED_UTC,
        evaluated_at=moment,
        anchors=closed_at(moment - timedelta(days=400)),
        blockers=cast(UnresolvedBlockers, object()),
    )

    assert decision.refusals == (RetentionRefused.INPUT_INVALID,)


def test_omitted_blockers_fail_closed() -> None:
    # Directly bypasses the convenience helper so no CLEAN default can fill the gap.
    moment = datetime(2025, 1, 15, 12, 0, tzinfo=UTC)
    decision = evaluate_service_retention(
        ADOPTED_UTC,
        scope=DataScope.SERVICE_DATA,
        evaluated_at=moment,
        anchors=closed_at(moment - timedelta(days=400)),
    )

    assert decision.eligible is False
    assert decision.refusals == (RetentionRefused.INPUT_INVALID,)


def test_none_blockers_fail_closed() -> None:
    # Explicit None is missing input, not a statement that every side effect is resolved.
    moment = datetime(2025, 1, 15, 12, 0, tzinfo=UTC)
    decision = evaluate_service_retention(
        ADOPTED_UTC,
        scope=DataScope.SERVICE_DATA,
        evaluated_at=moment,
        anchors=closed_at(moment - timedelta(days=400)),
        holds=(),
        blockers=None,
    )

    assert decision.eligible is False
    assert decision.refusals == (RetentionRefused.INPUT_INVALID,)


def test_wrong_scope_type_fails_closed() -> None:
    moment = datetime(2025, 1, 15, 12, 0, tzinfo=UTC)
    decision = evaluate(
        ADOPTED_UTC,
        scope=cast(DataScope, "service_data"),
        evaluated_at=moment,
        anchors=closed_at(moment - timedelta(days=400)),
    )

    assert decision.refusals == (RetentionRefused.SCOPE_INVALID,)


@pytest.mark.parametrize(
    ("scope", "reason"),
    [
        (
            DataScope.FINANCIAL_PROVIDER_DATA,
            RetentionRefused.FINANCIAL_PROVIDER_SCHEDULE_SEPARATE,
        ),
        (DataScope.UNKNOWN_PROTECTED_DATA, RetentionRefused.UNKNOWN_DATA_PROTECTED),
    ],
)
def test_service_expiry_never_authorizes_other_scopes(
    scope: DataScope,
    reason: RetentionRefused,
) -> None:
    moment = datetime(2025, 1, 15, 12, 0, tzinfo=UTC)
    decision = evaluate_service_retention(
        ADOPTED_UTC,
        scope=scope,
        evaluated_at=moment,
        anchors=closed_at(moment - timedelta(days=400)),
        holds=(),
        blockers=CLEAN,
    )

    assert decision.eligible is False
    assert decision.refusals == (reason,)
    assert decision.scope is scope
    assert decision.boundary is None


def test_eligible_decision_serialises_only_synthetic_metadata() -> None:
    anchor = datetime(2024, 1, 15, 12, 0, tzinfo=UTC)
    decision = evaluate(
        ADOPTED_UTC,
        evaluated_at=datetime(2025, 1, 15, 12, 0, tzinfo=UTC),
        anchors=closed_at(anchor),
    )

    assert decision.to_dict() == {
        "eligible": True,
        "refusals": [],
        "scope": "service_data",
        "boundary": "2025-01-15T12:00:00+00:00",
        "evaluated_at": "2025-01-15T12:00:00+00:00",
        "retention_policy_activated": False,
        "deletion_authorized": False,
    }
    assert decision.boundary is not None
    assert decision.boundary.tzinfo is UTC


def test_inventory_classifies_beta_feedback_notices_as_customer_payload(
    tmp_path: Path,
) -> None:
    root = tmp_path / "snapshot"
    root.mkdir()
    database = root / "parkingbreaker.sqlite"
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE pb_beta_feedback_notices (notice_id TEXT)")
        connection.execute(
            "INSERT INTO pb_beta_feedback_notices (notice_id) VALUES ('synthetic-notice')"
        )
        connection.commit()
    finally:
        connection.close()

    report = inventory_snapshot(root, database_paths=("parkingbreaker.sqlite",))

    assert report.row_count_for(TableClass.PROTECTED_CUSTOMER_CONTENT) == 1
    assert report.row_count_for(TableClass.RESTRICTED_FINANCIAL_PROVIDER_EVIDENCE) == 0
    assert report.row_count_for(TableClass.PROTECTED_UNKNOWN) == 0
    assert report.unknown_table_count == 0
    assert report.retention_policy_activated is False
    assert report.purge_eligible is False
