"""Read-only journey reconstruction. No transactions, retries or outcomes are decided here.

The store adapter reads a single bounded SQLite snapshot. The pure projection preserves
source sequence order, merges lanes by receipt time, and never interprets a partial
capture as abandonment. Pagination is over an immutable, fingerprinted projection;
a cursor from a changed snapshot is rejected instead of silently skipping late rows.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from parkingbreaker.journey_events import (
    JOURNEY_ID_PATTERN,
    JourneyEvent,
    JourneyEventKind,
    _occurrence_sha256,
    read_journey_events,
)
from parkingbreaker.mail_attempt_store import StorageCorruption, rollback_preserving_error
from parkingbreaker.ticket_observability import (
    OcrObservationCoverage,
    TicketObservabilitySnapshot,
    read_ticket_observability,
)

MAX_SNAPSHOT_ROWS = 10_000
MAX_PAGE_SIZE = 100


class CaptureCoverage(StrEnum):
    COMPLETE = "complete"
    LIMITED = "limited"
    LEGACY = "legacy"


class FactRoute(StrEnum):
    OCR = "ocr_review"
    MANUAL = "customer_supplied"
    UNKNOWN = "unknown"


class FundingRoute(StrEnum):
    PAID = "paid"
    FREE_BETA = "free_beta"
    UNKNOWN = "unknown"


class FindingCode(StrEnum):
    MISSING_PREDECESSOR = "missing_predecessor"
    LATE_RECORD = "late_record"
    INCOMPLETE_CAPTURE = "incomplete_capture"
    LEGACY_CAPTURE = "legacy_capture"
    LEGITIMATE_SKIP = "legitimate_skip"
    ROUTE_UNKNOWN = "route_unknown"
    CLOCK_SKEW = "clock_skew"


class TimelineSource(StrEnum):
    MILESTONE = "committed_milestone"
    OCR = "server_ocr_observation"
    ASSIGNMENT = "server_review_assignment"
    CLIENT = "client_observation"


@dataclass(frozen=True)
class TimelineFinding:
    code: FindingCode
    entry_id: str | None = None
    predecessor: JourneyEventKind | None = None


@dataclass(frozen=True)
class TimelineEntry:
    entry_id: str
    source: TimelineSource
    source_sequence: int | None
    occurred_at: datetime | None
    recorded_at: datetime
    details_json: str

    def to_dict(self) -> dict[str, object]:
        return {
            "entry_id": self.entry_id,
            "source": self.source.value,
            "source_sequence": self.source_sequence,
            "occurred_at": None if self.occurred_at is None else self.occurred_at.isoformat(),
            "recorded_at": self.recorded_at.isoformat(),
            "details": json.loads(self.details_json),
        }


@dataclass(frozen=True)
class TimelinePage:
    snapshot_id: str
    entries: tuple[TimelineEntry, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class JourneyTimeline:
    journey_id: str | None
    coverage: CaptureCoverage
    funding: FundingRoute
    entries: tuple[TimelineEntry, ...]
    findings: tuple[TimelineFinding, ...]
    pending_ocr_count: int
    missing_ocr_count: int
    snapshot_id: str

    def to_dict(self) -> dict[str, object]:
        return {
            "journey_id": self.journey_id,
            "coverage": self.coverage.value,
            "funding": self.funding.value,
            "entries": [entry.to_dict() for entry in self.entries],
            "findings": [
                {
                    "code": item.code.value,
                    "entry_id": item.entry_id,
                    "predecessor": item.predecessor.value if item.predecessor else None,
                }
                for item in self.findings
            ],
            "pending_ocr_count": self.pending_ocr_count,
            "missing_ocr_count": self.missing_ocr_count,
            "snapshot_id": self.snapshot_id,
        }

    def page(self, *, cursor: str | None = None, limit: int = MAX_PAGE_SIZE) -> TimelinePage:
        if type(limit) is not int or not 1 <= limit <= MAX_PAGE_SIZE:
            raise ValueError("invalid timeline page limit")
        offset = 0
        if cursor is not None:
            match = re.fullmatch(r"([0-9a-f]{64}):([1-9][0-9]{0,5})", cursor)
            if match is None or match[1] != self.snapshot_id:
                raise ValueError("timeline cursor snapshot mismatch")
            offset = int(match[2])
            if offset >= len(self.entries):
                raise ValueError("invalid timeline cursor offset")
        end = min(offset + limit, len(self.entries))
        return TimelinePage(
            self.snapshot_id,
            self.entries[offset:end],
            f"{self.snapshot_id}:{end}" if end < len(self.entries) else None,
        )


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _unique[T](items: Iterable[T], key: str) -> tuple[T, ...]:
    result: dict[object, T] = {}
    for item in items:
        identity = getattr(item, key)
        if identity in result and result[identity] != item:
            raise ValueError("conflicting timeline replay")
        result[identity] = item
    return tuple(result.values())


def _merge(lanes: Sequence[Sequence[TimelineEntry]]) -> tuple[TimelineEntry, ...]:
    """Receipt-time merge subject to each ledger/session's own sequence order."""
    heap = [
        (lane[0].recorded_at, lane[0].entry_id, index, 0)
        for index, lane in enumerate(lanes)
        if lane
    ]
    heapq.heapify(heap)
    merged = []
    while heap:
        _, _, index, offset = heapq.heappop(heap)
        lane = lanes[index]
        merged.append(lane[offset])
        offset += 1
        if offset < len(lane):
            heapq.heappush(heap, (lane[offset].recorded_at, lane[offset].entry_id, index, offset))
    return tuple(merged)


def reconstruct_journey(
    events: Sequence[JourneyEvent],
    *,
    journey_id: str | None,
    coverage: CaptureCoverage = CaptureCoverage.LIMITED,
    capture_complete: bool = True,
    fact_routes: Mapping[str, FactRoute] | None = None,
    funding: FundingRoute = FundingRoute.UNKNOWN,
    observations: TicketObservabilitySnapshot | None = None,
    late_after: timedelta = timedelta(minutes=1),
) -> JourneyTimeline:
    """Derive only supported history; completeness describes input, not customer behavior.

    COMPLETE/LEGACY coverage must come from independently recorded capture provenance.
    Version-one store rows do not contain that provenance; its adapter always uses LIMITED.
    Manual routing is per confirmation, never inferred from a browser observation.
    """
    if journey_id is not None and re.fullmatch(JOURNEY_ID_PATTERN, journey_id) is None:
        raise ValueError("invalid journey_id")
    if type(coverage) is not CaptureCoverage or type(funding) is not FundingRoute:
        raise ValueError("invalid timeline context")
    if type(capture_complete) is not bool or late_after < timedelta(0):
        raise ValueError("invalid timeline capture/timing policy")
    if len(events) > MAX_SNAPSHOT_ROWS:
        raise ValueError("timeline snapshot limit exceeded")
    ordered = sorted(_unique(events, "event_id"), key=lambda event: event.seq)
    if any(event.journey_id != journey_id for event in ordered):
        raise ValueError("mixed journey evidence")
    if len({event.seq for event in ordered}) != len(ordered):
        raise ValueError("conflicting timeline sequence")
    routes = dict(fact_routes or {})
    if any(type(route) is not FactRoute for route in routes.values()):
        raise ValueError("invalid fact route")
    confirmations = {
        event.event_id for event in ordered if event.kind is JourneyEventKind.FACTS_CONFIRMED
    }
    if not routes.keys() <= confirmations:
        raise ValueError("fact route has no confirmation evidence")
    contiguous = [event.seq for event in ordered] == list(range(1, len(ordered) + 1))
    findings: list[TimelineFinding] = []
    if coverage is CaptureCoverage.LEGACY:
        findings.append(TimelineFinding(FindingCode.LEGACY_CAPTURE))
    if coverage is CaptureCoverage.LIMITED or not capture_complete or not contiguous:
        findings.append(TimelineFinding(FindingCode.INCOMPLETE_CAPTURE))

    first_sequence: dict[JourneyEventKind, int] = {}
    for event in ordered:
        first_sequence.setdefault(event.kind, event.seq)

    def require(event: JourneyEvent, predecessor: JourneyEventKind) -> None:
        first = first_sequence.get(predecessor)
        if first is not None and first < event.seq:
            return
        if first is not None:
            code = FindingCode.LATE_RECORD
        elif not capture_complete or not contiguous or coverage is CaptureCoverage.LIMITED:
            code = FindingCode.INCOMPLETE_CAPTURE
        elif coverage is CaptureCoverage.LEGACY:
            code = FindingCode.LEGACY_CAPTURE
        else:
            code = FindingCode.MISSING_PREDECESSOR
        findings.append(TimelineFinding(code, event.event_id, predecessor))

    milestone_lane = []
    previous: JourneyEvent | None = None
    for event in ordered:
        if event.recorded_at < event.occurred_at or (
            previous is not None and event.recorded_at < previous.recorded_at
        ):
            findings.append(TimelineFinding(FindingCode.CLOCK_SKEW, event.event_id))
        if event.recorded_at - event.occurred_at > late_after:
            findings.append(TimelineFinding(FindingCode.LATE_RECORD, event.event_id))
        if event.kind is JourneyEventKind.FACTS_CONFIRMED:
            route = routes.get(event.event_id, FactRoute.UNKNOWN)
            if route is FactRoute.MANUAL:
                if first_sequence.get(JourneyEventKind.TICKET_READ, event.seq) >= event.seq:
                    findings.append(
                        TimelineFinding(
                            FindingCode.LEGITIMATE_SKIP,
                            event.event_id,
                            JourneyEventKind.TICKET_READ,
                        )
                    )
            elif route is FactRoute.OCR:
                require(event, JourneyEventKind.TICKET_READ)
            else:
                findings.append(
                    TimelineFinding(
                        FindingCode.ROUTE_UNKNOWN, event.event_id, JourneyEventKind.TICKET_READ
                    )
                )
        elif event.kind is JourneyEventKind.INTAKE_SUBMITTED:
            require(event, JourneyEventKind.FACTS_CONFIRMED)
        elif event.kind is JourneyEventKind.HOLD_AUTHORIZED:
            require(event, JourneyEventKind.INTAKE_SUBMITTED)
            if funding is FundingRoute.FREE_BETA:
                raise ValueError("paid hold conflicts with free beta evidence")
        elif event.kind is JourneyEventKind.PACKET_APPROVED:
            require(event, JourneyEventKind.INTAKE_SUBMITTED)
            if funding is FundingRoute.FREE_BETA:
                findings.append(
                    TimelineFinding(
                        FindingCode.LEGITIMATE_SKIP,
                        event.event_id,
                        JourneyEventKind.HOLD_AUTHORIZED,
                    )
                )
            elif funding is FundingRoute.PAID:
                require(event, JourneyEventKind.HOLD_AUTHORIZED)
            else:
                findings.append(
                    TimelineFinding(
                        FindingCode.ROUTE_UNKNOWN, event.event_id, JourneyEventKind.HOLD_AUTHORIZED
                    )
                )
        details = event.to_dict()
        if event.kind is JourneyEventKind.FACTS_CONFIRMED:
            details["fact_route"] = routes.get(event.event_id, FactRoute.UNKNOWN).value
        milestone_lane.append(
            TimelineEntry(
                event.event_id,
                TimelineSource.MILESTONE,
                event.seq,
                event.occurred_at,
                event.recorded_at,
                _json(details),
            )
        )
        previous = event

    lanes: list[Sequence[TimelineEntry]] = [milestone_lane]
    pending = missing = 0
    if observations is not None:
        if observations.journey_id != journey_id:
            raise ValueError("mixed journey observations")
        row_count = (
            len(events)
            + len(observations.ocr_observations)
            + len(observations.review_sessions)
            + len(observations.review_observations)
        )
        if row_count > MAX_SNAPSHOT_ROWS:
            raise ValueError("timeline snapshot limit exceeded")
        for read in sorted(
            _unique(observations.ocr_observations, "request_id"), key=lambda item: item.request_id
        ):
            pending += read.coverage is OcrObservationCoverage.PENDING
            missing += read.coverage is OcrObservationCoverage.MISSING
            obs = read.observation
            if obs is None:
                continue
            if obs.journey_id != journey_id:
                raise ValueError("mixed OCR evidence")
            details = {
                "operation_status": obs.operation_status,
                "routing_mode": obs.routing_mode.value,
                "primary_model": obs.primary_model,
                "secondary_model": obs.secondary_model,
                "primary_status": obs.primary_status.value,
                "escalation_status": obs.escalation_status.value,
                "decision_policy": obs.decision_policy,
                "schema_version": obs.schema_version,
                "timing_basis": "server_monotonic_offsets",
                "coordinator_duration_ms": obs.coordinator_duration_ms,
                "primary_duration_ms": obs.primary_duration_ms,
                "primary_start_offset_ms": obs.primary_start_offset_ms,
                "primary_end_offset_ms": obs.primary_end_offset_ms,
                "escalation_duration_ms": obs.escalation_duration_ms,
                "escalation_start_offset_ms": obs.escalation_start_offset_ms,
                "escalation_end_offset_ms": obs.escalation_end_offset_ms,
                "shown_suggestion_count": obs.shown_suggestion_count,
                "manual_entry_field_count": obs.manual_entry_field_count,
                "reason_counts": {
                    reason.value: count for reason, count in obs.reason_counts.items()
                },
            }
            lanes.append(
                (
                    TimelineEntry(
                        obs.observation_id,
                        TimelineSource.OCR,
                        None,
                        None,
                        obs.recorded_at,
                        _json(details),
                    ),
                )
            )
        sessions = _unique(observations.review_sessions, "session_id")
        reviews = _unique(observations.review_observations, "observation_id")
        session_ids = {session.session_id for session in sessions}
        if any(review.session_id not in session_ids for review in reviews):
            raise ValueError("review observation missing session")
        for session in sorted(sessions, key=lambda item: item.session_id):
            if session.journey_id not in (None, journey_id):
                raise ValueError("mixed review session evidence")
            assignment = TimelineEntry(
                session.session_id,
                TimelineSource.ASSIGNMENT,
                None,
                session.assigned_at,
                session.assigned_at,
                _json(
                    {
                        "policy_mode": session.policy_mode.value,
                        "variant": session.variant.value,
                        "effective_treatment_version": session.effective_treatment_version,
                        "canary_version": session.canary_version,
                        "canary_percentage": session.canary_percentage,
                        "exposure": "unknown_until_client_observation",
                    }
                ),
            )
            review_lane = [assignment]
            session_reviews = sorted(
                (item for item in reviews if item.session_id == session.session_id),
                key=lambda item: item.seq,
            )
            if len({item.seq for item in session_reviews}) != len(session_reviews):
                raise ValueError("conflicting review sequence")
            if [item.seq for item in session_reviews] != list(range(1, len(session_reviews) + 1)):
                findings.append(TimelineFinding(FindingCode.INCOMPLETE_CAPTURE, session.session_id))
            for review in session_reviews:
                details = {
                    "session_id": review.session_id,
                    "event_kind": review.event_kind.value,
                    "outcome": review.outcome.value,
                    "measurement_label": review.measurement_label,
                    "treatment_variant": review.treatment_variant.value,
                    "treatment_version": review.treatment_version,
                    "rendered_variant": review.rendered_variant.value,
                    "visible_elapsed_ms": review.visible_elapsed_ms,
                    "busy_elapsed_ms": review.busy_elapsed_ms,
                    "hidden_elapsed_ms": review.hidden_elapsed_ms,
                    "visibility_changes": review.visibility_changes,
                    "timing_state": review.timing_state.value,
                    "invalid_field_count": review.invalid_field_count,
                    "customer_supplied": review.customer_supplied,
                    "reading_status": review.reading_status,
                    "review_status": review.review_status,
                }
                review_lane.append(
                    TimelineEntry(
                        review.observation_id,
                        TimelineSource.CLIENT,
                        review.seq,
                        None,
                        review.recorded_at,
                        _json(details),
                    )
                )
            lanes.append(review_lane)
    if pending or missing:
        findings.append(TimelineFinding(FindingCode.INCOMPLETE_CAPTURE))
    entries = _merge(lanes)
    if len({item.entry_id for item in entries}) != len(entries):
        raise ValueError("conflicting timeline identity")
    result = JourneyTimeline(
        journey_id, coverage, funding, entries, tuple(dict.fromkeys(findings)), pending, missing, ""
    )
    # Include all derived context; a stale cursor cannot silently cross snapshots.
    digest = hashlib.sha256(_json(result.to_dict()).encode()).hexdigest()
    return JourneyTimeline(
        journey_id, coverage, funding, entries, result.findings, pending, missing, digest
    )


def read_journey_timeline(
    database: sqlite3.Connection, *, workspace_token: str, owner: str | None = None
) -> JourneyTimeline:
    """Internal read-only snapshot; owner checking matches the existing event read API.

    No raw customer JSON, request IDs, case IDs or bearer workspace tokens are exported.
    Source growth is bounded before calling the existing observation snapshot reader.
    """
    owns_tx = not database.in_transaction
    if owns_tx:
        database.execute("BEGIN")
    try:
        page = read_journey_events(database, workspace_token=workspace_token, owner=owner)
        events = list(page.events)
        for table in (
            "pb_journey_events",
            "pb_ticket_intake_operations",
            "pb_ticket_review_sessions",
            "pb_ticket_review_observations",
            "pb_ticket_intake_confirmations",
        ):
            scope = "journey_id" if table == "pb_journey_events" else "workspace_token"
            value = page.journey_id if scope == "journey_id" else workspace_token
            count = database.execute(
                f"SELECT COUNT(*) FROM (SELECT 1 FROM {table} WHERE {scope}=? LIMIT ?)",
                (value, MAX_SNAPSHOT_ROWS + 1),
            ).fetchone()[0]
            if count > MAX_SNAPSHOT_ROWS:
                raise ValueError("timeline snapshot limit exceeded")
        while page.next_after_seq is not None:
            page = read_journey_events(
                database,
                workspace_token=workspace_token,
                owner=owner,
                after_seq=page.next_after_seq,
            )
            events.extend(page.events)
        observations = read_ticket_observability(
            database, workspace_token=workspace_token, owner=owner
        )
        # Match the exact immutable confirmation generation, never the latest browser claim.
        confirmations = database.execute(
            "SELECT c.intake_id, c.generation, o.result_json FROM pb_ticket_intake_confirmations c "
            "JOIN pb_ticket_intake_operations o ON o.request_id=c.review_request_id "
            "WHERE c.workspace_token=? AND o.operation='review' AND o.status='success'",
            (workspace_token,),
        ).fetchall()
        routes: dict[str, FactRoute] = {}
        for intake_id, generation, raw in confirmations:
            occurrence = _occurrence_sha256(f"facts_confirmed:{intake_id}:{generation}")
            row = database.execute(
                "SELECT event_id FROM pb_journey_events WHERE journey_id=? AND occurrence_sha256=?",
                (page.journey_id, occurrence),
            ).fetchone()
            if row is None:
                continue
            try:
                payload = json.loads(raw)
                supplied = payload.get("customer_supplied", False)
                if type(supplied) is not bool or (
                    supplied and (payload.get("fields") != {} or payload.get("confirmed") != {})
                ):
                    raise ValueError
            except (ValueError, TypeError, AttributeError):
                raise StorageCorruption("malformed review provenance") from None
            routes[row[0]] = FactRoute.MANUAL if supplied else FactRoute.OCR
        case_rows = database.execute(
            "SELECT amount_cents, payment_state FROM pb_cases WHERE workspace_token=?",
            (workspace_token,),
        ).fetchall()
        funding = FundingRoute.UNKNOWN
        if case_rows:
            if len(case_rows) != 1:
                raise StorageCorruption("ambiguous journey funding")
            amount, state = case_rows[0]
            if type(amount) is not int or amount < 0 or (amount == 0) != (state == "NO_CHARGE"):
                raise StorageCorruption("invalid journey funding")
            funding = FundingRoute.FREE_BETA if amount == 0 else FundingRoute.PAID
        result = reconstruct_journey(
            events,
            journey_id=page.journey_id,
            observations=observations,
            fact_routes=routes,
            funding=funding,
        )
        if owns_tx:
            database.execute("COMMIT")
        return result
    except Exception:
        if owns_tx and database.in_transaction:
            rollback_preserving_error(database)
        raise


class _ConnectionOwner(Protocol):
    def _require_open(self) -> sqlite3.Connection: ...


class JourneyTimelineStoreMixin:
    def journey_timeline(
        self: _ConnectionOwner, *, workspace_token: str, owner: str | None = None
    ) -> JourneyTimeline:
        return read_journey_timeline(
            self._require_open(), workspace_token=workspace_token, owner=owner
        )
