"""Timeline reconstruction is deterministic, read-only, and honest about absent evidence."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import test_ticket_intake_service as h
from parkingbreaker.api import schemas as s
from parkingbreaker.beta_store import BETA_DISCLOSURE_SHA256
from parkingbreaker.case_state import CustomerApproved, DraftReady, ResearchComplete
from parkingbreaker.journey_events import (
    CostStatus,
    JourneyEvent,
    JourneyEventAccessError,
    JourneyEventInput,
    JourneyEventOutcome,
    record_journey_event,
)
from parkingbreaker.journey_events import (
    JourneyEventKind as Kind,
)
from parkingbreaker.journey_timeline import (
    CaptureCoverage,
    FactRoute,
    FundingRoute,
    JourneyTimeline,
    TimelineEntry,
    TimelineSource,
    reconstruct_journey,
)
from parkingbreaker.journey_timeline import (
    FindingCode as Code,
)
from parkingbreaker.llm import Usage
from parkingbreaker.ticket_observability import (
    OcrObservationCoverage,
    OcrObservationRead,
    TicketObservabilitySnapshot,
)
from parkingbreaker.transaction_store import SqliteTransactionStore
from test_beta_admission import NOW as BETA_NOW
from test_beta_admission import campaign, candidate
from test_ticket_observability import observation

NOW = datetime(2026, 11, 17, 12, tzinfo=UTC)
JOURNEY = "jrn_" + "a" * 32


def event(seq: int, kind: Kind, *, seconds: int | None = None) -> JourneyEvent:
    at = NOW + timedelta(seconds=seq if seconds is None else seconds)
    outcome = {
        Kind.TICKET_READ_REFUSED: JourneyEventOutcome.REFUSED,
        Kind.TICKET_READ_UNKNOWN: JourneyEventOutcome.UNKNOWN,
    }.get(kind, JourneyEventOutcome.SUCCEEDED)
    return JourneyEvent(
        f"evt_{seq:032x}", JOURNEY, seq, kind, outcome, 1, at, at, None, None, CostStatus.UNKNOWN
    )


def timeline(*events: JourneyEvent) -> JourneyTimeline:
    return reconstruct_journey(events, journey_id=JOURNEY, coverage=CaptureCoverage.COMPLETE)


def codes(result: JourneyTimeline) -> set[Code]:
    return {finding.code for finding in result.findings}


def all_rows(store: SqliteTransactionStore) -> dict[str, list[tuple[object, ...]]]:
    db = store._require_open()
    tables = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    return {
        name: db.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall() for (name,) in tables
    }


def test_complete_funnel_replay_shuffle_and_pagination() -> None:
    events = tuple(
        event(index, kind)
        for index, kind in enumerate(
            (
                Kind.TICKET_READ,
                Kind.FACTS_CONFIRMED,
                Kind.INTAKE_SUBMITTED,
                Kind.HOLD_AUTHORIZED,
                Kind.PACKET_APPROVED,
            ),
            1,
        )
    )

    def build(rows: tuple[JourneyEvent, ...]) -> JourneyTimeline:
        return reconstruct_journey(
            rows,
            journey_id=JOURNEY,
            coverage=CaptureCoverage.COMPLETE,
            funding=FundingRoute.PAID,
            fact_routes={events[1].event_id: FactRoute.OCR},
        )

    result = build(events)
    assert result == build(tuple(reversed(events)) + events)
    assert result.findings == ()
    cursor = None
    collected: list[TimelineEntry] = []
    while True:
        page = result.page(cursor=cursor, limit=2)
        collected.extend(page.entries)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    assert tuple(collected) == result.entries
    assert len(collected) == 5
    assert result.page().next_cursor is None


@pytest.mark.parametrize(
    ("coverage", "complete", "expected"),
    [
        (CaptureCoverage.COMPLETE, True, Code.MISSING_PREDECESSOR),
        (CaptureCoverage.COMPLETE, False, Code.INCOMPLETE_CAPTURE),
        (CaptureCoverage.LIMITED, True, Code.INCOMPLETE_CAPTURE),
        (CaptureCoverage.LEGACY, True, Code.LEGACY_CAPTURE),
    ],
)
def test_absence_classification_requires_capture_evidence(
    coverage: CaptureCoverage, complete: bool, expected: Code
) -> None:
    submitted = event(1, Kind.INTAKE_SUBMITTED)
    result = reconstruct_journey(
        (submitted,),
        journey_id=JOURNEY,
        coverage=coverage,
        capture_complete=complete,
    )
    assert {item.code for item in result.findings if item.predecessor} == {expected}
    assert all(
        item.predecessor is Kind.FACTS_CONFIRMED for item in result.findings if item.predecessor
    )
    assert "abandon" not in json.dumps(result.to_dict())


def test_missing_sequence_is_incomplete_capture_even_with_complete_claim() -> None:
    result = timeline(event(3, Kind.INTAKE_SUBMITTED))
    assert Code.INCOMPLETE_CAPTURE in codes(result)
    assert Code.MISSING_PREDECESSOR not in codes(result)


@pytest.mark.parametrize("read_kind", [None, Kind.TICKET_READ_REFUSED, Kind.TICKET_READ_UNKNOWN])
def test_manual_route_and_free_beta_legitimately_skip_successful_ocr_and_paid_hold(
    read_kind: Kind | None,
) -> None:
    rows = [] if read_kind is None else [event(1, read_kind)]
    confirmed = event(len(rows) + 1, Kind.FACTS_CONFIRMED)
    rows.extend(
        (
            confirmed,
            event(len(rows) + 2, Kind.INTAKE_SUBMITTED),
            event(len(rows) + 3, Kind.PACKET_APPROVED),
        )
    )
    result = reconstruct_journey(
        rows,
        journey_id=JOURNEY,
        coverage=CaptureCoverage.COMPLETE,
        fact_routes={confirmed.event_id: FactRoute.MANUAL},
        funding=FundingRoute.FREE_BETA,
    )
    assert codes(result) == {Code.LEGITIMATE_SKIP}
    assert {item.predecessor for item in result.findings} == {
        Kind.TICKET_READ,
        Kind.HOLD_AUTHORIZED,
    }
    assert len(result.entries) == len(rows)


def test_unknown_routes_remain_unknown_and_paid_route_requires_hold() -> None:
    confirmed = event(1, Kind.FACTS_CONFIRMED)
    submitted = event(2, Kind.INTAKE_SUBMITTED)
    approved = event(3, Kind.PACKET_APPROVED)
    result = timeline(confirmed, submitted, approved)
    assert codes(result) == {Code.ROUTE_UNKNOWN}
    paid = reconstruct_journey(
        (confirmed, submitted, approved),
        journey_id=JOURNEY,
        coverage=CaptureCoverage.COMPLETE,
        funding=FundingRoute.PAID,
        fact_routes={confirmed.event_id: FactRoute.MANUAL},
    )
    assert any(
        item.code is Code.MISSING_PREDECESSOR and item.predecessor is Kind.HOLD_AUTHORIZED
        for item in paid.findings
    )


def test_late_predecessor_clock_skew_and_delayed_recording_do_not_reorder_ledger() -> None:
    submitted = event(1, Kind.INTAKE_SUBMITTED, seconds=30)
    confirmed = replace(
        event(2, Kind.FACTS_CONFIRMED, seconds=10),
        occurred_at=NOW - timedelta(minutes=5),
    )
    future = replace(event(3, Kind.TICKET_READ), occurred_at=NOW + timedelta(hours=1))
    result = timeline(submitted, confirmed, future)
    assert [entry.source_sequence for entry in result.entries] == [1, 2, 3]
    assert any(
        item.code is Code.LATE_RECORD and item.predecessor is Kind.FACTS_CONFIRMED
        for item in result.findings
    )
    assert {Code.LATE_RECORD, Code.CLOCK_SKEW} <= codes(result)


@pytest.mark.parametrize("alteration", ["id_content", "sequence", "journey"])
def test_conflicting_or_cross_journey_replays_are_rejected(alteration: str) -> None:
    original = event(1, Kind.TICKET_READ)
    changed = {
        "id_content": replace(original, duration_ms=10),
        "sequence": replace(original, event_id="evt_" + "b" * 32),
        "journey": replace(original, journey_id="jrn_" + "b" * 32),
    }[alteration]
    with pytest.raises(ValueError, match=r"conflicting|mixed"):
        timeline(original, changed)


def test_cursor_is_bound_to_whole_snapshot_including_late_arrivals_and_context() -> None:
    result = timeline(event(1, Kind.TICKET_READ), event(2, Kind.TICKET_READ_REFUSED))
    cursor = result.page(limit=1).next_cursor
    assert cursor is not None
    changed = timeline(
        event(1, Kind.TICKET_READ),
        event(2, Kind.TICKET_READ_REFUSED),
        event(3, Kind.TICKET_READ, seconds=-5),
    )
    with pytest.raises(ValueError, match="snapshot mismatch"):
        changed.page(cursor=cursor)
    changed_context = reconstruct_journey(
        (event(1, Kind.TICKET_READ), event(2, Kind.TICKET_READ_REFUSED)),
        journey_id=JOURNEY,
    )
    with pytest.raises(ValueError, match="snapshot mismatch"):
        changed_context.page(cursor=cursor)
    for invalid in (cursor + "0", "bad", result.snapshot_id + ":0", result.snapshot_id + ":999999"):
        with pytest.raises(ValueError, match="timeline cursor"):
            result.page(cursor=invalid)


@pytest.mark.parametrize("limit", [0, -1, True, 101])
def test_page_bounds(limit: int) -> None:
    with pytest.raises(ValueError, match="limit"):
        timeline().page(limit=limit)


def test_pending_and_uncaptured_operations_are_counts_not_invented_transitions() -> None:
    reads = (
        OcrObservationRead("private-request-1", "pending", OcrObservationCoverage.PENDING, None),
        OcrObservationRead("private-request-2", "unknown", OcrObservationCoverage.MISSING, None),
    )
    result = reconstruct_journey(
        (),
        journey_id=None,
        observations=TicketObservabilitySnapshot(
            None,
            reads + reads,
            (),
            (),
        ),
    )
    assert result.entries == ()
    assert result.pending_ocr_count == result.missing_ocr_count == 1
    assert codes(result) == {Code.INCOMPLETE_CAPTURE}
    assert "private-request" not in json.dumps(result.to_dict())
    assert result.page().next_cursor is None


def test_store_real_manual_confirmation_route_ignores_browser_claims(tmp_path: Path) -> None:
    fx = h.harness(tmp_path / "manual.sqlite", h.ScriptedVisionClient(({}, Usage())))
    ws, _, _, _ = h.ready(fx, binding=h.binding_for(dict.fromkeys(h.literal_values())))
    try:
        fx.coordinator.request_reading(ws, "refused-reading", 2)
        review = fx.coordinator.request_review(ws, "manual-review", 2, customer_supplied=True)
        assert review.status == "success"
        fx.coordinator.confirm(ws, h.confirm_command(fx, ws, review_id="manual-review"))
        before = all_rows(fx.store)
        result = fx.store.journey_timeline(workspace_token=ws, owner=h.ALICE)
        assert all_rows(fx.store) == before
        assert any(
            item.code is Code.LEGITIMATE_SKIP and item.predecessor is Kind.TICKET_READ
            for item in result.findings
        )
        assert Code.MISSING_PREDECESSOR not in codes(result)
    finally:
        fx.store.close()


def test_store_combines_safe_ocr_and_client_evidence_without_business_writes(
    tmp_path: Path,
) -> None:
    fx = h.harness(
        tmp_path / "observations.sqlite", h.ScriptedVisionClient((h.reading_payload(), Usage()))
    )
    ws, _, _, _ = h.ready(fx)
    try:
        fx.coordinator.request_reading(ws, "secret-reading-request", 2)
        session = fx.coordinator.start_review_session(
            ws, s.TicketReviewSessionRequest(client_session_id="a" * 32)
        )
        fx.coordinator.record_review_observation(
            ws,
            session.session_id,
            observation(session).model_copy(
                update={
                    "event_kind": "confirm_attempt",
                    "outcome": "client_reported_success",
                    "review_status": "success",
                    "customer_supplied": True,
                }
            ),
        )
        before = all_rows(fx.store)
        db = fx.store._require_open()
        db.execute("PRAGMA query_only=ON")
        result = fx.store.journey_timeline(workspace_token=ws)
        assert not db.in_transaction
        assert all_rows(fx.store) == before
        assert {entry.source for entry in result.entries} == set(TimelineSource)
        assert [
            json.loads(entry.details_json)["kind"]
            for entry in result.entries
            if entry.source is TimelineSource.MILESTONE
        ] == ["ticket_read"]
        client = next(entry for entry in result.entries if entry.source is TimelineSource.CLIENT)
        assert client.occurred_at is None
        assert json.loads(client.details_json)["visible_elapsed_ms"] == 20
        serialized = json.dumps(result.to_dict())
        for secret in (ws, "secret-reading-request", "SYN-0001", "SYN123", "Alex Example"):
            assert secret not in serialized
        assert "not_human_attention" in serialized
        assert fx.store.journey_timeline(workspace_token=ws) == result
        with pytest.raises(JourneyEventAccessError):
            fx.store.journey_timeline(workspace_token=ws, owner="wrong-owner")
        assert not db.in_transaction
    finally:
        fx.store.close()


def test_store_reads_all_event_pages_and_preserves_existing_transaction(tmp_path: Path) -> None:
    fx = h.harness(tmp_path / "pages.sqlite", h.ScriptedVisionClient())
    ws, _, _, _ = h.ready(fx)
    try:
        db = fx.store._require_open()
        for index in range(105):
            record_journey_event(
                db,
                workspace_token=ws,
                occurrence_key=f"synthetic-{index}",
                event=JourneyEventInput(
                    kind=Kind.TICKET_READ,
                    outcome=JourneyEventOutcome.SUCCEEDED,
                    occurred_at=NOW,
                    recorded_at=NOW,
                ),
            )
        before = all_rows(fx.store)
        db.execute("BEGIN")
        result = fx.store.journey_timeline(workspace_token=ws)
        assert db.in_transaction
        assert len(result.entries) == 105
        assert all_rows(fx.store) == before
        with pytest.raises(JourneyEventAccessError):
            fx.store.journey_timeline(workspace_token=ws, owner="wrong-owner")
        assert db.in_transaction
        db.execute("ROLLBACK")
    finally:
        fx.store.close()


def test_frozen_beta_funding_is_read_from_case_evidence(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "beta.sqlite") as store:
        campaign(store)
        candidate(store, 1)
        row = store.get_intake("case_1")
        assert row is not None
        store.update_intake("case_1", row.revision, selected_tier="CERTIFIED", case_token=None)
        store.select_beta_batch("beta-1", "batch-1", "approval-1", BETA_NOW)
        case = store.start_beta_case("case_1", BETA_DISCLOSURE_SHA256, BETA_NOW)
        case = store.apply_letter_event(
            case.case_token, ResearchComplete(), case.revision, occurred_at=BETA_NOW
        )
        case = store.apply_letter_event(
            case.case_token, DraftReady(), case.revision, occurred_at=BETA_NOW
        )
        store.apply_letter_event(
            case.case_token, CustomerApproved(), case.revision, occurred_at=BETA_NOW
        )
        before = all_rows(store)
        result = store.journey_timeline(workspace_token=case.workspace_token)
        assert all_rows(store) == before
        assert result.funding is FundingRoute.FREE_BETA
        assert any(
            item.code is Code.LEGITIMATE_SKIP and item.predecessor is Kind.HOLD_AUTHORIZED
            for item in result.findings
        )
        assert not any(
            entry.source is TimelineSource.MILESTONE
            and json.loads(entry.details_json)["kind"] == "hold_authorized"
            for entry in result.entries
        )


def test_existing_successful_read_is_not_reported_as_skipped() -> None:
    read = event(1, Kind.TICKET_READ)
    confirmed = event(2, Kind.FACTS_CONFIRMED)
    result = reconstruct_journey(
        (read, confirmed),
        journey_id=JOURNEY,
        coverage=CaptureCoverage.COMPLETE,
        fact_routes={confirmed.event_id: FactRoute.MANUAL},
    )
    assert result.findings == ()
    assert json.loads(result.entries[-1].details_json)["fact_route"] == "customer_supplied"


def test_conflicting_free_funding_and_hold_are_refused() -> None:
    with pytest.raises(ValueError, match="paid hold conflicts"):
        reconstruct_journey(
            (event(1, Kind.HOLD_AUTHORIZED),),
            journey_id=JOURNEY,
            funding=FundingRoute.FREE_BETA,
        )


def test_store_snapshot_limit_fails_closed_without_partial_diagnosis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import parkingbreaker.journey_timeline as module

    fx = h.harness(tmp_path / "bounded.sqlite", h.ScriptedVisionClient())
    ws, _, _, _ = h.ready(fx)
    try:
        for index in range(3):
            record_journey_event(
                fx.store._require_open(),
                workspace_token=ws,
                occurrence_key=f"bound-{index}",
                event=JourneyEventInput(
                    kind=Kind.TICKET_READ, outcome=JourneyEventOutcome.SUCCEEDED, occurred_at=NOW
                ),
            )
        monkeypatch.setattr(module, "MAX_SNAPSHOT_ROWS", 2)
        before = all_rows(fx.store)
        with pytest.raises(ValueError, match="snapshot limit"):
            fx.store.journey_timeline(workspace_token=ws)
        assert all_rows(fx.store) == before
        assert not fx.store._require_open().in_transaction
    finally:
        fx.store.close()


def test_restart_and_provider_replay_preserve_timeline_fingerprint(tmp_path: Path) -> None:
    path = tmp_path / "restart.sqlite"
    fx = h.harness(path, h.ScriptedVisionClient((h.reading_payload(), Usage())))
    ws, _, _, _ = h.ready(fx)
    try:
        fx.coordinator.request_reading(ws, "extract-restart", 2)
        before = fx.store.journey_timeline(workspace_token=ws)
    finally:
        fx.store.close()
    restarted = h.harness(path, h.ScriptedVisionClient(), photos=fx.photos)
    try:
        restarted.coordinator.request_reading(ws, "extract-restart", 2)
        assert restarted.store.journey_timeline(workspace_token=ws) == before
        assert restarted.client.calls == []
    finally:
        restarted.store.close()
