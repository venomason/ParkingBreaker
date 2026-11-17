"""Focused tests for the append-only SQLite journey ledger and five atomic hooks."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import pytest

from parkingbreaker import case_service, prehold
from parkingbreaker.api import schemas as s
from parkingbreaker.appeal.drafting import Draft, DraftSentence
from parkingbreaker.appeal.fakes import FakeDrafter
from parkingbreaker.beta_store import BETA_DISCLOSURE_SHA256
from parkingbreaker.blob_store import FakeBlobStore
from parkingbreaker.case_service import CaseService, CaseServiceDeps, ResearchDeps
from parkingbreaker.case_state import CaseState, CustomerApproved, DraftReady, ResearchComplete
from parkingbreaker.config_fakes import AcceptingLobClient
from parkingbreaker.journey_events import (
    COMPONENT,
    CostStatus,
    JourneyEvent,
    JourneyEventAccessError,
    JourneyEventConflict,
    JourneyEventInput,
    JourneyEventKind,
    JourneyEventOutcome,
    append_journey_event_in_transaction,
    initialize_journey_events,
    read_journey_events,
    record_journey_event,
)
from parkingbreaker.journey_timeline import FundingRoute, TimelineSource
from parkingbreaker.llm import LlmBudgetExceeded, LlmProviderError, Usage
from parkingbreaker.mail_attempt_store import StorageCorruption
from parkingbreaker.payment_records import mutation_key
from parkingbreaker.prehold import PreholdDeps
from parkingbreaker.research.resolution import VehicleRelationship
from parkingbreaker.research.run import ResearchResult
from parkingbreaker.service_policy import SERVICE_CONTROL, STATE_BASIS
from parkingbreaker.stripe_payment import STRIPE_API_VERSION, StripePaymentClient
from parkingbreaker.ticket_intake_service import TicketIntakeCoordinator, TicketIntakeDeps
from parkingbreaker.ticket_intake_store import (
    ClaimStatus,
    DiscrepancyStatus,
    IntakeOperation,
    TicketIntakeStore,
    claim_ticket_intake_operation,
    complete_ticket_intake_operation,
    latest_ticket_intake_confirmation,
    record_ticket_intake_confirmation,
    reserve_ticket_intake,
    review_confirmation_binding,
)
from parkingbreaker.ticket_vision import TicketVisionReader
from parkingbreaker.transaction_store import SqliteTransactionStore
from parkingbreaker.workspace import Workspace
from prepared_packet_support import fetched_at
from samples import SAN_FRANCISCO
from test_beta_admission import NOW as BETA_NOW
from test_beta_admission import campaign, candidate
from test_case_service import approve_with_preview
from test_stripe_payment import Transport, fixture
from test_ticket_intake_service import (
    ALICE,
    BACK,
    FRONT,
    FailingVisionClient,
    ScriptedVisionClient,
    binding_for,
    photo,
    reading_payload,
    review_payload,
    submit_body,
)
from test_ticket_observability import assert_review_does_not_change_commitments
from test_tier_acceptance import extract_case, model_answers

NOW = datetime(2026, 10, 15, 21, 0, tzinfo=UTC)
LATER = NOW + timedelta(minutes=5)
BINDING = "a" * 64
IMAGES = "c" * 64
FIELDS = "d" * 64


class _Resolved:
    def __init__(self, agency: object) -> None:
        self.agency = agency
        self.stop = None
        self.code = None


class _MutableClock:
    def __init__(self, start: datetime = NOW) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> datetime:
        self.now = self.now + delta
        return self.now


class _HookedConnection:
    """Proxy connection to inject faults before COMMIT or on targeted SQL statements."""

    def __init__(self, target: sqlite3.Connection) -> None:
        self._target = target
        self.raise_before_commit: Exception | None = None
        self.raise_on_statement: tuple[str, Exception] | None = None

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
        if self.raise_on_statement is not None:
            pattern, exc = self.raise_on_statement
            if pattern in sql:
                self.raise_on_statement = None
                raise exc
        if sql.strip().upper() == "COMMIT" and self.raise_before_commit is not None:
            exc = self.raise_before_commit
            self.raise_before_commit = None
            raise exc
        return self._target.execute(sql, parameters)

    @property
    def in_transaction(self) -> bool:
        return self._target.in_transaction

    def close(self) -> None:
        self._target.close()


@pytest.fixture(autouse=True)
def _fake_issuer_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    def resolve(_query: Any, *_args: object, **_kwargs: object) -> _Resolved:
        return _Resolved(SAN_FRANCISCO)

    monkeypatch.setattr(prehold, "resolve_ticket_issuer", resolve)


def _full_service(
    path: Path,
    *,
    vision_client: ScriptedVisionClient | FailingVisionClient | None = None,
    clock: Callable[[], datetime] = lambda: NOW,
    stripe_responses: tuple[object, ...] = (),
) -> tuple[CaseService, SqliteTransactionStore, TicketIntakeCoordinator, FakeBlobStore]:
    store = SqliteTransactionStore(path)
    photos = FakeBlobStore()
    intake_store = TicketIntakeStore(store)
    counter = iter(range(1000))
    client = (
        ScriptedVisionClient(
            (reading_payload(), Usage(input_tokens=100, output_tokens=50, cost_cents=3)),
            (
                review_payload(binding_for().review_fields()),
                Usage(input_tokens=80, output_tokens=40, cost_cents=2),
            ),
        )
        if vision_client is None
        else vision_client
    )

    def new_id() -> str:
        return f"{next(counter):032x}"

    coordinator = TicketIntakeCoordinator(
        TicketIntakeDeps(
            store=store,
            intake=intake_store,
            photos=photos,
            clock=clock,
            new_id=new_id,
            reader_for_subject=lambda _subject: TicketVisionReader(client),
            required=True,
        )
    )
    unused: Any = object()
    prehold_deps = PreholdDeps(
        unused, None, unused, unused, unused, clock, 25, STATE_BASIS, SERVICE_CONTROL
    )
    transport = Transport(*(stripe_responses or (fixture(),)))
    payments = StripePaymentClient(
        secret_key="sk_test_offline",
        api_version=STRIPE_API_VERSION,
        transport=transport,
    )
    service = CaseService(
        CaseServiceDeps(
            store,
            prehold_deps,
            clock,
            payments,
            photos,
            new_id=new_id,
            lob=cast(Any, AcceptingLobClient()),
            ticket_intake=coordinator,
        )
    )
    return service, store, coordinator, photos


def _prepare_ready_workspace(
    service: CaseService,
) -> tuple[str, Any, str, str | None]:
    workspace = service.create_workspace(ALICE, s.CreateWorkspace(notes="")).workspace_id
    front_ref = service.upload_photo(workspace, photo(FRONT)).photo_ref
    back_ref = service.upload_photo(workspace, photo(BACK)).photo_ref
    binding = binding_for()
    form = s.IntakeForm(
        values={"story": "my exact words"},
        ticket_photo_ref=front_ref,
        ticket_back_photo_ref=back_ref,
        ticket_fact_binding=s.TicketFactBindingPayload(
            literal=dict(binding.literal),
            adopted=dict(binding.adopted),
        ),
    )
    service.save_intake_form(workspace, s.SaveIntakeForm(expected_revision=1, form=form))
    return workspace, binding, front_ref, back_ref


def _seed_store(path: Path, token: str = "ws_1", owner: str = "alice") -> SqliteTransactionStore:
    store = SqliteTransactionStore(path)
    store.save_workspace(token, Workspace(notes="synthetic"), NOW)
    store.set_workspace_owner(token, owner, NOW)
    return store


def test_full_five_hook_funnel_replays_and_privacy_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _MutableClock(NOW)
    binding = binding_for()
    client = ScriptedVisionClient(
        (reading_payload(), Usage(input_tokens=120, output_tokens=60, cost_cents=4)),
        (
            review_payload(binding.review_fields()),
            Usage(input_tokens=80, output_tokens=30, cost_cents=2),
        ),
    )
    service, store, coordinator, _photos = _full_service(
        tmp_path / "funnel.sqlite", vision_client=client, clock=clock
    )
    try:
        workspace, binding, front_ref, back_ref = _prepare_ready_workspace(service)
        owner = store.workspace_owner(workspace)
        assert owner is not None

        # 1. ticket_read (EXTRACT) plus replay no-op
        clock.advance(timedelta(seconds=2))
        reading = coordinator.request_reading(workspace, "req-extract-1", 2)
        assert reading.status == "success"
        assert coordinator.request_reading(workspace, "req-extract-1", 2) == reading

        # Review operation must NOT emit ticket_read
        clock.advance(timedelta(seconds=1))
        review = coordinator.request_review(workspace, "req-review-1", 2)
        assert review.status == "success"
        assert [e.kind for e in store.journey_events(workspace_token=workspace).events] == [
            JourneyEventKind.TICKET_READ
        ]

        # 2. facts_confirmed plus replay no-op
        clock.advance(timedelta(seconds=1))
        cmd = confirm_command_for(2, "req-review-1")
        confirmed = coordinator.confirm(workspace, cmd)
        assert confirmed.generation == 1
        assert coordinator.confirm(workspace, cmd) == confirmed

        # 3. intake_submitted plus replay no-op
        clock.advance(timedelta(seconds=3))
        body = submit_body(binding, front_ref, back_ref, command_id="cmd-confirm-1")
        receipt = service.submit(workspace, body)
        case_id = receipt.case_id
        assert service.submit(workspace, body) == receipt

        # Tier selection + begin_authorization_attempt must NOT emit hold_authorized
        service.choose(case_id, s.ChooseTier(tier=s.MailTier.CERTIFIED))
        key = mutation_key("authorization", case_id, "CERTIFIED:2999", "pm_card_visa")
        req_json = json.dumps(
            {
                "amount_cents": 2999,
                "currency": "usd",
                "metadata": {
                    "case": case_id,
                    "tier": "CERTIFIED",
                    "service_amount_cents": "2999",
                },
                "payment_method_id": "pm_card_visa",
                "tier": "CERTIFIED",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        store.begin_authorization_attempt(
            case_id, key, "CERTIFIED", 2999, "pm_card_visa", req_json, clock()
        )
        kinds_before_hold = [e.kind for e in store.journey_events(workspace_token=workspace).events]
        assert JourneyEventKind.HOLD_AUTHORIZED not in kinds_before_hold

        # 4. hold_authorized plus replay no-op
        clock.advance(timedelta(seconds=4))
        auth = service.authorize(
            case_id,
            s.StartAuthorization(terms_accepted=True, payment_method_id="pm_card_visa"),
        )
        assert auth.status == "authorized"
        assert (
            service.authorize(
                case_id,
                s.StartAuthorization(terms_accepted=True, payment_method_id="pm_card_visa"),
            )
            == auth
        )

        # Research + Draft + Adopt + Preview must NOT emit packet_approved
        result, _llm, _searcher = extract_case(model_answers())

        def run_fake_research(*_args: object, **_kwargs: object) -> ResearchResult:
            return fetched_at(result, clock())

        monkeypatch.setattr(case_service, "run_research", run_fake_research)
        unused: Any = object()
        object.__setattr__(
            service._deps,
            "research",
            ResearchDeps(unused, unused, None, lambda _s: None),
        )
        statement = body.customer_position.statement
        object.__setattr__(
            service._deps,
            "drafter",
            FakeDrafter(
                lambda *_: Draft((DraftSentence(statement, (f"note:0-{len(statement)}",)),))
            ),
        )
        clock.advance(timedelta(seconds=5))
        status = service.step(case_id, s.WorkerStep(step_id="research"))
        if status.state is CaseState.NEEDS_CUSTOMER:
            status = service.answer_question(
                case_id,
                s.AnswerQuestion(
                    question_id="VEHICLE_RELATIONSHIP",
                    answer=VehicleRelationship.REGISTERED_OWNER,
                ),
            )
        assert status.state is CaseState.DRAFTING
        clock.advance(timedelta(seconds=2))
        status = service.step(case_id, s.WorkerStep(step_id="draft"))
        assert status.state is CaseState.AWAITING_ADOPTION

        draft = service.draft(case_id)
        service.adopt_text(case_id, s.AdoptText(text=draft.text, text_sha256=draft.text_sha256))
        approval = approve_with_preview(service, case_id, draft.text_sha256)
        kinds_before_approve = [
            e.kind for e in store.journey_events(workspace_token=workspace).events
        ]
        assert JourneyEventKind.PACKET_APPROVED not in kinds_before_approve

        # 5. packet_approved plus replay no-op
        clock.advance(timedelta(seconds=3))
        approved_status = service.approve(case_id, approval)
        assert approved_status.state is CaseState.FROZEN
        assert service.approve(case_id, approval) == approved_status
        assert_review_does_not_change_commitments(store, coordinator, workspace)
        assert service.status(case_id) == approved_status

        page = store.journey_events(workspace_token=workspace, owner=owner)
        assert page.journey_id is not None
        assert page.journey_id.startswith("jrn_")
        assert page.next_after_seq is None
        assert [e.kind for e in page.events] == [
            JourneyEventKind.TICKET_READ,
            JourneyEventKind.FACTS_CONFIRMED,
            JourneyEventKind.INTAKE_SUBMITTED,
            JourneyEventKind.HOLD_AUTHORIZED,
            JourneyEventKind.PACKET_APPROVED,
        ]
        assert [e.seq for e in page.events] == [1, 2, 3, 4, 5]
        assert all(e.outcome is JourneyEventOutcome.SUCCEEDED for e in page.events)
        assert all(e.schema_version == 1 for e in page.events)
        assert all(e.journey_id == page.journey_id for e in page.events)
        assert all(e.occurred_at.date() == NOW.date() for e in page.events)
        assert all(e.recorded_at != e.occurred_at for e in page.events)
        assert page.events[0].cost_cents == 4
        assert page.events[0].cost_status is CostStatus.KNOWN
        assert page.events[0].duration_ms == 0
        for non_cost_event in page.events[1:]:
            assert non_cost_event.cost_cents is None
            assert non_cost_event.cost_status is CostStatus.NOT_APPLICABLE
            assert non_cost_event.duration_ms is None

        # The timeline reads a truly frozen paid case without modifying any business row.
        database = store._require_open()
        before_timeline = tuple(database.iterdump())
        timeline = store.journey_timeline(workspace_token=workspace, owner=owner)
        assert tuple(database.iterdump()) == before_timeline
        assert timeline.funding is FundingRoute.PAID
        assert [
            entry.entry_id for entry in timeline.entries if entry.source is TimelineSource.MILESTONE
        ] == [e.event_id for e in page.events]
        assert service.status(case_id) == approved_status

        # Public projection contains no restricted linkage, dedup keys, or customer PII.
        serialized = json.dumps(
            {"events": [e.to_dict() for e in page.events], "timeline": timeline.to_dict()},
            sort_keys=True,
        )
        for forbidden in (
            workspace,
            owner,
            case_id,
            "req-extract-1",
            "cmd-confirm-1",
            "SYN-0001",
            "SYN123",
            "Alex Example",
            "alex@example.test",
            "my exact words",
            "occurrence",
        ):
            assert forbidden not in serialized
    finally:
        store.close()


def confirm_command_for(revision: int, review_id: str) -> s.TicketConfirmationRequest:
    return s.TicketConfirmationRequest(
        command_id="cmd-confirm-1",
        expected_revision=revision,
        review_request_id=review_id,
        expected_confirmation_generation=0,
        accuracy_acknowledged=True,
        resolved_fields={},
    )


def test_ticket_read_refused_unknown_and_unknown_cost(tmp_path: Path) -> None:
    # Provider failure -> TICKET_READ_UNKNOWN with cost_cents=None and CostStatus.UNKNOWN
    failing = FailingVisionClient(LlmProviderError("upstream 503"))
    service_u, store_u, coord_u, _ = _full_service(
        tmp_path / "unknown.sqlite", vision_client=failing
    )
    try:
        ws_u, _, _, _ = _prepare_ready_workspace(service_u)
        with pytest.raises(Exception, match="ticket_reading_provider_unavailable"):
            coord_u.request_reading(ws_u, "req-u", 2)
        events_u = store_u.journey_events(workspace_token=ws_u).events
        assert len(events_u) == 1
        assert events_u[0].kind is JourneyEventKind.TICKET_READ_UNKNOWN
        assert events_u[0].outcome is JourneyEventOutcome.UNKNOWN
        assert events_u[0].cost_cents is None
        assert events_u[0].cost_status is CostStatus.UNKNOWN
    finally:
        store_u.close()

    # Budget exhaustion -> TICKET_READ_REFUSED with measured 0 cost
    budget_fail = FailingVisionClient(LlmBudgetExceeded("cap"))
    service_r, store_r, coord_r, _ = _full_service(
        tmp_path / "refused.sqlite", vision_client=budget_fail
    )
    try:
        ws_r, _, _, _ = _prepare_ready_workspace(service_r)
        with pytest.raises(Exception, match="ticket_reading_budget_exhausted"):
            coord_r.request_reading(ws_r, "req-r", 2)
        events_r = store_r.journey_events(workspace_token=ws_r).events
        assert len(events_r) == 1
        assert events_r[0].kind is JourneyEventKind.TICKET_READ_REFUSED
        assert events_r[0].outcome is JourneyEventOutcome.REFUSED
        assert events_r[0].cost_cents == 0
        assert events_r[0].cost_status is CostStatus.KNOWN
    finally:
        store_r.close()

    # Direct store completion with missing usage_json or missing cost_cents -> null + unknown
    store_d = _seed_store(tmp_path / "direct.sqlite")
    try:
        db = store_d._require_open()
        res = reserve_ticket_intake(db, "intake_1", "ws_1", "alice", NOW)
        claim = claim_ticket_intake_operation(
            db, "req_no_cost", res.intake_id, IntakeOperation.EXTRACT, BINDING, NOW
        )
        assert claim.claim_token is not None
        complete_ticket_intake_operation(
            db,
            "req_no_cost",
            ClaimStatus.SUCCESS,
            '{"synthetic":true}',
            '{"tokens":7}',
            NOW + timedelta(milliseconds=250),
            claim_token=claim.claim_token,
        )
        events_d = store_d.journey_events(workspace_token="ws_1").events
        assert len(events_d) == 1
        assert events_d[0].kind is JourneyEventKind.TICKET_READ
        assert events_d[0].duration_ms == 250
        assert events_d[0].cost_cents is None
        assert events_d[0].cost_status is CostStatus.UNKNOWN
    finally:
        store_d.close()


def test_distinct_confirmation_generations_remain_separate(tmp_path: Path) -> None:
    store = _seed_store(tmp_path / "gens.sqlite")
    try:
        db = store._require_open()
        res = reserve_ticket_intake(db, "intake_1", "ws_1", "alice", NOW)
        binding = review_confirmation_binding(IMAGES, FIELDS)
        review = claim_ticket_intake_operation(
            db, "rev_1", res.intake_id, IntakeOperation.REVIEW, binding, NOW
        )
        assert review.claim_token is not None
        complete_ticket_intake_operation(
            db,
            "rev_1",
            ClaimStatus.SUCCESS,
            '{"review":"ok"}',
            '{"cost_cents":1}',
            NOW,
            claim_token=review.claim_token,
        )
        # REVIEW completion itself emits no event
        assert store.journey_events(workspace_token="ws_1").events == ()

        first = record_ticket_intake_confirmation(
            db,
            "cmd_1",
            res.intake_id,
            IMAGES,
            FIELDS,
            "rev_1",
            1,
            1,
            DiscrepancyStatus.NONE,
            NOW,
        )
        # Exact replay of cmd_1 is a no-op
        assert (
            record_ticket_intake_confirmation(
                db,
                "cmd_1",
                res.intake_id,
                IMAGES,
                FIELDS,
                "rev_1",
                1,
                1,
                DiscrepancyStatus.NONE,
                NOW,
            )
            == first
        )
        # Distinct generation 2 records a second separate confirmation event
        record_ticket_intake_confirmation(
            db,
            "cmd_2",
            res.intake_id,
            IMAGES,
            FIELDS,
            "rev_1",
            1,
            2,
            DiscrepancyStatus.RESOLVED,
            LATER,
        )
        events = store.journey_events(workspace_token="ws_1").events
        assert len(events) == 2
        assert [e.seq for e in events] == [1, 2]
        assert all(e.kind is JourneyEventKind.FACTS_CONFIRMED for e in events)
    finally:
        store.close()


def test_occurrence_replay_noop_and_conflict_refusal(tmp_path: Path) -> None:
    store = _seed_store(tmp_path / "replay.sqlite")
    try:
        db = store._require_open()
        event = JourneyEventInput(
            kind=JourneyEventKind.INTAKE_SUBMITTED,
            outcome=JourneyEventOutcome.SUCCEEDED,
            occurred_at=NOW,
            recorded_at=NOW,
            cost_status=CostStatus.NOT_APPLICABLE,
        )
        first = record_journey_event(
            db, workspace_token="ws_1", occurrence_key="occ:1", event=event
        )
        replay = record_journey_event(
            db,
            workspace_token="ws_1",
            occurrence_key="occ:1",
            event=JourneyEventInput(
                kind=JourneyEventKind.INTAKE_SUBMITTED,
                outcome=JourneyEventOutcome.SUCCEEDED,
                occurred_at=NOW,
                recorded_at=LATER,
                cost_status=CostStatus.NOT_APPLICABLE,
            ),
        )
        assert replay == first
        assert len(store.journey_events(workspace_token="ws_1").events) == 1

        # Conflicting content on same occurrence_key is refused
        with pytest.raises(JourneyEventConflict, match="journey_event_occurrence_conflict"):
            record_journey_event(
                db,
                workspace_token="ws_1",
                occurrence_key="occ:1",
                event=JourneyEventInput(
                    kind=JourneyEventKind.HOLD_AUTHORIZED,
                    outcome=JourneyEventOutcome.SUCCEEDED,
                    occurred_at=NOW,
                    recorded_at=NOW,
                    cost_status=CostStatus.NOT_APPLICABLE,
                ),
            )

        # Same occurrence_key for a different workspace is also refused
        store.save_workspace("ws_2", Workspace(notes="two"), NOW)
        with pytest.raises(JourneyEventConflict, match="journey_event_occurrence_conflict"):
            record_journey_event(db, workspace_token="ws_2", occurrence_key="occ:1", event=event)
        assert store.journey_events(workspace_token="ws_2").events == ()
    finally:
        store.close()


def test_delayed_recording_replay_and_clock_skew(tmp_path: Path) -> None:
    store = _seed_store(tmp_path / "clock_skew.sqlite")
    try:
        db = store._require_open()

        # 1. Default recorded_at stays None on JourneyEventInput; insertion resolves real UTC once
        delayed_input = JourneyEventInput(
            kind=JourneyEventKind.INTAKE_SUBMITTED,
            outcome=JourneyEventOutcome.SUCCEEDED,
            occurred_at=NOW,
            cost_status=CostStatus.NOT_APPLICABLE,
        )
        assert delayed_input.recorded_at is None
        before = datetime.now(UTC)
        real_event = record_journey_event(
            db, workspace_token="ws_1", occurrence_key="delayed:1", event=delayed_input
        )
        after = datetime.now(UTC)
        assert real_event.occurred_at == NOW
        assert real_event.recorded_at != NOW
        assert before <= real_event.recorded_at <= after

        # 2. Injected clock resolves once per new row and is not called on replay
        store.save_workspace("ws_2", Workspace(notes="two"), NOW)
        clock_calls: list[datetime] = []
        tick = _MutableClock(NOW + timedelta(days=2))

        def counting_clock() -> datetime:
            value = tick.advance(timedelta(seconds=10))
            clock_calls.append(value)
            return value

        first = record_journey_event(
            db,
            workspace_token="ws_2",
            occurrence_key="delayed:2",
            event=delayed_input,
            clock=counting_clock,
        )
        assert len(clock_calls) == 1
        assert first.occurred_at == NOW
        assert first.recorded_at == clock_calls[0]
        link_row = db.execute(
            "SELECT created_at FROM pb_journey_links WHERE workspace_token=?", ("ws_2",)
        ).fetchone()
        assert link_row is not None
        assert datetime.fromisoformat(str(link_row[0])) == first.recorded_at

        # Replay with a later clock and a different recorded_at preserves original identity
        # and original recording time; semantic dedupe ignores new recording time.
        tick.advance(timedelta(hours=1))
        replay_default = record_journey_event(
            db,
            workspace_token="ws_2",
            occurrence_key="delayed:2",
            event=delayed_input,
            clock=counting_clock,
        )
        replay_explicit = record_journey_event(
            db,
            workspace_token="ws_2",
            occurrence_key="delayed:2",
            event=JourneyEventInput(
                kind=JourneyEventKind.INTAKE_SUBMITTED,
                outcome=JourneyEventOutcome.SUCCEEDED,
                occurred_at=NOW,
                recorded_at=LATER + timedelta(hours=3),
                cost_status=CostStatus.NOT_APPLICABLE,
            ),
            clock=counting_clock,
        )
        assert len(clock_calls) == 1
        assert replay_default == first
        assert replay_explicit == first

        # Changing occurred_at on the same occurrence_key is still a semantic conflict
        with pytest.raises(JourneyEventConflict, match="journey_event_occurrence_conflict"):
            record_journey_event(
                db,
                workspace_token="ws_2",
                occurrence_key="delayed:2",
                event=JourneyEventInput(
                    kind=JourneyEventKind.INTAKE_SUBMITTED,
                    outcome=JourneyEventOutcome.SUCCEEDED,
                    occurred_at=LATER,
                    cost_status=CostStatus.NOT_APPLICABLE,
                ),
                clock=counting_clock,
            )

        # 3. Clock skew: recorded_at < occurred_at is accepted without clamping or failure
        skew_occurred = datetime.now(UTC) + timedelta(hours=3)
        skew_recorded = skew_occurred - timedelta(hours=5)
        skew_input = JourneyEventInput(
            kind=JourneyEventKind.HOLD_AUTHORIZED,
            outcome=JourneyEventOutcome.SUCCEEDED,
            occurred_at=skew_occurred,
            recorded_at=skew_recorded,
            cost_status=CostStatus.NOT_APPLICABLE,
        )
        skew_event = record_journey_event(
            db, workspace_token="ws_2", occurrence_key="skew:direct", event=skew_input
        )
        assert skew_event.occurred_at == skew_occurred
        assert skew_event.recorded_at == skew_recorded
        assert skew_event.recorded_at < skew_event.occurred_at

        # Real business hook with a skewed future occurred_at also succeeds without clamping
        res = reserve_ticket_intake(db, "intake_skew", "ws_1", "alice", NOW)
        claim = claim_ticket_intake_operation(
            db, "req_skew", res.intake_id, IntakeOperation.EXTRACT, BINDING, skew_occurred
        )
        assert claim.claim_token is not None
        completed = complete_ticket_intake_operation(
            db,
            "req_skew",
            ClaimStatus.SUCCESS,
            '{"ok":true}',
            '{"cost_cents":2}',
            skew_occurred + timedelta(milliseconds=150),
            claim_token=claim.claim_token,
        )
        ws1_events = store.journey_events(workspace_token="ws_1").events
        assert ws1_events[-1].occurred_at == completed.completed_at
        assert ws1_events[-1].recorded_at < ws1_events[-1].occurred_at
    finally:
        store.close()


def test_rollback_fault_injection_removes_business_and_ledger_writes(tmp_path: Path) -> None:
    # 1. intake_submitted rollback
    intake_path = tmp_path / "rb_intake.sqlite"
    store = _seed_store(intake_path)
    try:
        hooked = _HookedConnection(store._require_open())
        store._database = hooked  # type: ignore[assignment]
        hooked.raise_before_commit = RuntimeError("fault before commit")
        with pytest.raises(RuntimeError, match="fault before commit"):
            store.record_intake_with_workspace_snapshot(
                "intake_1", "ws_1", '{"i":1}', '{"p":1}', "pos", (), frozenset(), NOW
            )
    finally:
        store.close()
    with SqliteTransactionStore(intake_path) as reopened:
        assert reopened.get_intake("intake_1") is None
        assert reopened.journey_events(workspace_token="ws_1").events == ()

    # 2. ticket_read (EXTRACT) rollback
    op_path = tmp_path / "rb_op.sqlite"
    op_store = _seed_store(op_path)
    try:
        db = op_store._require_open()
        res = reserve_ticket_intake(db, "intake_1", "ws_1", "alice", NOW)
        claim = claim_ticket_intake_operation(
            db, "req_1", res.intake_id, IntakeOperation.EXTRACT, BINDING, NOW
        )
        assert claim.claim_token is not None
        hooked_op = _HookedConnection(db)
        hooked_op.raise_before_commit = RuntimeError("fault on complete")
        with pytest.raises(RuntimeError, match="fault on complete"):
            complete_ticket_intake_operation(
                hooked_op,  # type: ignore[arg-type]
                "req_1",
                ClaimStatus.SUCCESS,
                '{"ok":true}',
                '{"cost_cents":5}',
                LATER,
                claim_token=claim.claim_token,
            )
    finally:
        op_store.close()
    with SqliteTransactionStore(op_path) as reopened:
        stored_claim = claim_ticket_intake_operation(
            reopened._require_open(), "req_1", "intake_1", IntakeOperation.EXTRACT, BINDING, NOW
        )
        assert stored_claim.status is ClaimStatus.PENDING
        assert reopened.journey_events(workspace_token="ws_1").events == ()

    # 2b. facts_confirmed rollback
    confirm_path = tmp_path / "rb_confirm.sqlite"
    confirm_store = _seed_store(confirm_path)
    try:
        db = confirm_store._require_open()
        res = reserve_ticket_intake(db, "intake_1", "ws_1", "alice", NOW)
        review_binding = review_confirmation_binding(IMAGES, FIELDS)
        review_claim = claim_ticket_intake_operation(
            db, "rev_1", res.intake_id, IntakeOperation.REVIEW, review_binding, NOW
        )
        assert review_claim.claim_token is not None
        complete_ticket_intake_operation(
            db,
            "rev_1",
            ClaimStatus.SUCCESS,
            '{"review":"ok"}',
            '{"cost_cents":1}',
            NOW,
            claim_token=review_claim.claim_token,
        )
        assert confirm_store.journey_events(workspace_token="ws_1").events == ()
        hooked_confirm = _HookedConnection(db)
        hooked_confirm.raise_before_commit = RuntimeError("fault on confirm commit")
        with pytest.raises(RuntimeError, match="fault on confirm commit"):
            record_ticket_intake_confirmation(
                cast(Any, hooked_confirm),
                "cmd_1",
                res.intake_id,
                IMAGES,
                FIELDS,
                "rev_1",
                1,
                1,
                DiscrepancyStatus.NONE,
                LATER,
            )
    finally:
        confirm_store.close()
    with SqliteTransactionStore(confirm_path) as reopened:
        assert latest_ticket_intake_confirmation(reopened._require_open(), "intake_1") is None
        assert reopened.journey_events(workspace_token="ws_1").events == ()

    # 3. hold_authorized & packet_approved rollback
    case_path = tmp_path / "rb_case.sqlite"
    case_store = _seed_store(case_path)
    try:
        case_store.record_intake("intake_1", "ws_1", '{"i":1}', '{"p":1}', NOW)
        case_store.update_intake("intake_1", 1, selected_tier="CERTIFIED", case_token=None)
        key = mutation_key("authorization", "intake_1", "CERTIFIED:2999", "pm_card_visa")
        req_json = json.dumps(
            {
                "amount_cents": 2999,
                "currency": "usd",
                "metadata": {
                    "case": "intake_1",
                    "tier": "CERTIFIED",
                    "service_amount_cents": "2999",
                },
                "payment_method_id": "pm_card_visa",
                "tier": "CERTIFIED",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        case_store.begin_authorization_attempt(
            "intake_1", key, "CERTIFIED", 2999, "pm_card_visa", req_json, NOW
        )
        hooked_auth = _HookedConnection(case_store._require_open())
        case_store._database = hooked_auth  # type: ignore[assignment]
        hooked_auth.raise_before_commit = RuntimeError("fault on auth commit")
        with pytest.raises(RuntimeError, match="fault on auth commit"):
            case_store.complete_authorization_attempt(
                "intake_1", key, "pi_auth_1", LATER, LATER + timedelta(days=5)
            )
    finally:
        case_store.close()
    with SqliteTransactionStore(case_path) as reopened:
        intake_row = reopened.get_intake("intake_1")
        assert intake_row is not None
        assert intake_row.case_token is None
        assert reopened.journey_events(workspace_token="ws_1").events == ()

        # Now commit hold_authorized cleanly, advance to AWAITING_ADOPTION, then fault FROZEN
        started = reopened.complete_authorization_attempt(
            "intake_1", key, "pi_auth_1", LATER, LATER + timedelta(days=5)
        )
        reopened.apply_letter_event(
            started.case_token, ResearchComplete(), started.revision, occurred_at=LATER
        )
        draft_case = reopened.apply_letter_event(
            started.case_token, DraftReady(), started.revision + 1, occurred_at=LATER
        )
        hooked_freeze = _HookedConnection(reopened._require_open())
        reopened._database = hooked_freeze  # type: ignore[assignment]
        hooked_freeze.raise_before_commit = RuntimeError("fault on freeze commit")
        with pytest.raises(RuntimeError, match="fault on freeze commit"):
            reopened.apply_letter_event(
                started.case_token,
                CustomerApproved(),
                draft_case.revision,
                occurred_at=LATER,
            )
    with SqliteTransactionStore(case_path) as final_store:
        final_case = final_store.get_case(started.case_token)
        assert final_case is not None
        assert final_case.letter_state is CaseState.AWAITING_ADOPTION
        events = final_store.journey_events(workspace_token="ws_1").events
        assert [e.kind for e in events] == [JourneyEventKind.HOLD_AUTHORIZED]


def test_restart_ordering_and_existing_db_without_invented_history(tmp_path: Path) -> None:
    path = tmp_path / "restart.sqlite"
    with _seed_store(path) as first:
        e1 = record_journey_event(
            first._require_open(),
            workspace_token="ws_1",
            occurrence_key="k1",
            event=JourneyEventInput(
                kind=JourneyEventKind.TICKET_READ,
                outcome=JourneyEventOutcome.SUCCEEDED,
                occurred_at=NOW,
                cost_cents=1,
                cost_status=CostStatus.KNOWN,
            ),
        )
    with SqliteTransactionStore(path) as second:
        e2 = record_journey_event(
            second._require_open(),
            workspace_token="ws_1",
            occurrence_key="k2",
            event=JourneyEventInput(
                kind=JourneyEventKind.INTAKE_SUBMITTED,
                outcome=JourneyEventOutcome.SUCCEEDED,
                occurred_at=LATER,
                cost_status=CostStatus.NOT_APPLICABLE,
            ),
        )
        page = second.journey_events(workspace_token="ws_1")
        assert page.journey_id == e1.journey_id == e2.journey_id
        assert [e.seq for e in page.events] == [1, 2]
        assert page.events == (e1, e2)

    # Simulate an existing DB created before journey_events existed: drop the component
    # and tables, keep existing workspace/case rows, then reopen.
    db = sqlite3.connect(path)
    try:
        db.execute("DROP TABLE pb_journey_events")
        db.execute("DROP TABLE pb_journey_links")
        db.execute("DELETE FROM pb_store_versions WHERE component=?", (COMPONENT,))
        db.commit()
    finally:
        db.close()

    with SqliteTransactionStore(path) as upgraded:
        assert upgraded.get_workspace("ws_1") is not None
        assert upgraded.journey_events(workspace_token="ws_1").events == ()
        new_event = record_journey_event(
            upgraded._require_open(),
            workspace_token="ws_1",
            occurrence_key="k3",
            event=JourneyEventInput(
                kind=JourneyEventKind.PACKET_APPROVED,
                outcome=JourneyEventOutcome.SUCCEEDED,
                occurred_at=LATER,
                cost_status=CostStatus.NOT_APPLICABLE,
            ),
        )
        assert new_event.seq == 1


def test_separate_journeys_pagination_and_authorization_boundary(tmp_path: Path) -> None:
    store = _seed_store(tmp_path / "bounds.sqlite", token="ws_1", owner="alice")
    try:
        store.save_workspace("ws_2", Workspace(notes="bob ws"), NOW)
        store.set_workspace_owner("ws_2", "bob", NOW)
        db = store._require_open()
        for idx, kind in enumerate(
            (
                JourneyEventKind.TICKET_READ,
                JourneyEventKind.FACTS_CONFIRMED,
                JourneyEventKind.INTAKE_SUBMITTED,
            ),
            start=1,
        ):
            record_journey_event(
                db,
                workspace_token="ws_1",
                occurrence_key=f"ws1:{idx}",
                event=JourneyEventInput(
                    kind=kind,
                    outcome=JourneyEventOutcome.SUCCEEDED,
                    occurred_at=NOW + timedelta(seconds=idx),
                    cost_status=CostStatus.NOT_APPLICABLE,
                ),
            )
        bob_event = record_journey_event(
            db,
            workspace_token="ws_2",
            occurrence_key="ws2:1",
            event=JourneyEventInput(
                kind=JourneyEventKind.TICKET_READ,
                outcome=JourneyEventOutcome.SUCCEEDED,
                occurred_at=NOW,
                cost_status=CostStatus.NOT_APPLICABLE,
            ),
        )
        p1 = store.journey_events(workspace_token="ws_1", owner="alice", limit=2)
        assert len(p1.events) == 2
        assert p1.next_after_seq == 2
        assert p1.journey_id != bob_event.journey_id
        p2 = store.journey_events(
            journey_id=p1.journey_id, owner="alice", after_seq=p1.next_after_seq or 0, limit=2
        )
        assert len(p2.events) == 1
        assert p2.events[0].seq == 3
        assert p2.next_after_seq is None

        # Cross-owner authorization fails closed
        with pytest.raises(JourneyEventAccessError, match="workspace_owner_mismatch"):
            store.journey_events(workspace_token="ws_1", owner="bob")
        with pytest.raises(JourneyEventAccessError, match="workspace_owner_mismatch"):
            store.journey_events(journey_id=p1.journey_id, owner="bob")
        with pytest.raises(JourneyEventAccessError, match="workspace_owner_mismatch"):
            store.journey_events(workspace_token="ws_unknown", owner="alice")

        # Bounds & argument validation
        with pytest.raises(ValueError, match="exactly one"):
            store.journey_events()
        with pytest.raises(ValueError, match="exactly one"):
            store.journey_events(journey_id=p1.journey_id, workspace_token="ws_1")
        for bad_limit in (0, -1, 101, True):
            with pytest.raises(ValueError, match="limit must be an integer"):
                store.journey_events(workspace_token="ws_1", limit=bad_limit)
        for bad_seq in (-1, True):
            with pytest.raises(ValueError, match="after_seq must be an integer"):
                store.journey_events(workspace_token="ws_1", after_seq=bad_seq)
    finally:
        store.close()


def test_contract_rejects_invalid_enums_scalars_pii_and_append_only_mutations(
    tmp_path: Path,
) -> None:
    # Invalid enums, scalars, naive datetimes, and cost invariants
    with pytest.raises(ValueError, match="kind must be a JourneyEventKind"):
        JourneyEventInput(
            kind=cast(Any, "ticket_read"),
            outcome=JourneyEventOutcome.SUCCEEDED,
            occurred_at=NOW,
        )
    with pytest.raises(ValueError, match="outcome is not allowed"):
        JourneyEventInput(
            kind=JourneyEventKind.TICKET_READ,
            outcome=JourneyEventOutcome.REFUSED,
            occurred_at=NOW,
        )
    with pytest.raises(ValueError, match="timezone-aware datetime"):
        JourneyEventInput(
            kind=JourneyEventKind.TICKET_READ,
            outcome=JourneyEventOutcome.SUCCEEDED,
            occurred_at=datetime(2026, 10, 15, 21, 0),
        )
    with pytest.raises(ValueError, match="timezone-aware datetime"):
        JourneyEventInput(
            kind=JourneyEventKind.TICKET_READ,
            outcome=JourneyEventOutcome.SUCCEEDED,
            occurred_at=NOW,
            recorded_at=datetime(2026, 10, 15, 21, 0),
        )
    # Clock skew (recorded_at earlier than occurred_at) is accepted unchanged
    skewed = JourneyEventInput(
        kind=JourneyEventKind.TICKET_READ,
        outcome=JourneyEventOutcome.SUCCEEDED,
        occurred_at=LATER,
        recorded_at=NOW,
    )
    assert skewed.occurred_at == LATER
    assert skewed.recorded_at == NOW
    with pytest.raises(ValueError, match="duration_ms must be an integer"):
        JourneyEventInput(
            kind=JourneyEventKind.TICKET_READ,
            outcome=JourneyEventOutcome.SUCCEEDED,
            occurred_at=NOW,
            duration_ms=-1,
        )
    with pytest.raises(ValueError, match="duration_ms must be an integer"):
        JourneyEventInput(
            kind=JourneyEventKind.TICKET_READ,
            outcome=JourneyEventOutcome.SUCCEEDED,
            occurred_at=NOW,
            duration_ms=cast(Any, True),
        )
    with pytest.raises(ValueError, match="cost_cents must be None"):
        JourneyEventInput(
            kind=JourneyEventKind.TICKET_READ,
            outcome=JourneyEventOutcome.SUCCEEDED,
            occurred_at=NOW,
            cost_cents=0,
            cost_status=CostStatus.UNKNOWN,
        )
    with pytest.raises(ValueError, match="cost_cents must be an integer"):
        JourneyEventInput(
            kind=JourneyEventKind.TICKET_READ,
            outcome=JourneyEventOutcome.SUCCEEDED,
            occurred_at=NOW,
            cost_cents=None,
            cost_status=CostStatus.KNOWN,
        )

    # Non-UTC aware datetime normalizes cleanly to UTC
    pst = timezone(timedelta(hours=-8))
    normalized = JourneyEventInput(
        kind=JourneyEventKind.INTAKE_SUBMITTED,
        outcome=JourneyEventOutcome.SUCCEEDED,
        occurred_at=datetime(2026, 10, 15, 13, 0, tzinfo=pst),
    )
    assert normalized.occurred_at.utcoffset() == timedelta(0)
    assert normalized.occurred_at == NOW

    # Reject PII-like extra metadata / unknown keys in closed mappings
    for bad_mapping in (
        {
            "kind": "ticket_read",
            "outcome": "succeeded",
            "occurred_at": NOW.isoformat(),
            "metadata": {"email": "alice@example.test"},
        },
        {
            "kind": "ticket_read",
            "outcome": "succeeded",
            "occurred_at": NOW.isoformat(),
            "provider_id": "pi_123",
        },
        {
            "kind": "invented",
            "outcome": "succeeded",
            "occurred_at": NOW.isoformat(),
        },
    ):
        with pytest.raises(ValueError, match=r"unknown journey event fields|invalid kind"):
            JourneyEventInput.from_mapping(bad_mapping)

    with pytest.raises(ValueError, match="closed schema"):
        JourneyEvent.from_mapping(
            {
                "event_id": "evt_" + "a" * 32,
                "journey_id": "jrn_" + "b" * 32,
                "seq": 1,
                "kind": "ticket_read",
                "outcome": "succeeded",
                "schema_version": 1,
                "occurred_at": NOW.isoformat(),
                "recorded_at": NOW.isoformat(),
                "duration_ms": None,
                "cost_cents": None,
                "cost_status": "unknown",
                "workspace_token": "ws_1",
            }
        )

    # Reject PII-like tokens and enforce append-only SQLite triggers
    store = _seed_store(tmp_path / "triggers.sqlite")
    try:
        db = store._require_open()
        with pytest.raises(ValueError, match="invalid workspace token"):
            record_journey_event(
                db, workspace_token="alice@example.com", occurrence_key="k", event=normalized
            )
        with pytest.raises(ValueError, match="invalid occurrence key"):
            record_journey_event(
                db, workspace_token="ws_1", occurrence_key="alice@example.com", event=normalized
            )
        recorded = record_journey_event(
            db, workspace_token="ws_1", occurrence_key="k_ok", event=normalized
        )
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            db.execute(
                "UPDATE pb_journey_events SET cost_cents=99 WHERE event_id=?",
                (recorded.event_id,),
            )
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            db.execute(
                "DELETE FROM pb_journey_events WHERE event_id=?",
                (recorded.event_id,),
            )
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            db.execute("DELETE FROM pb_journey_links WHERE workspace_token='ws_1'")
        with pytest.raises(StorageCorruption):
            append_journey_event_in_transaction(
                db, workspace_token="ws_1", occurrence_key="no_tx", event=normalized
            )
        # Missing journey_events component raises StorageCorruption on init check
        raw = sqlite3.connect(":memory:")
        with pytest.raises(StorageCorruption):
            initialize_journey_events(raw)
        raw.close()
        with pytest.raises(StorageCorruption):
            read_journey_events(sqlite3.connect(":memory:"), workspace_token="ws_1")
    finally:
        store.close()


def test_beta_no_charge_case_never_emits_hold_authorized(tmp_path: Path) -> None:
    store = SqliteTransactionStore(tmp_path / "beta.sqlite")
    try:
        campaign(store)
        candidate(store, 1)
        row = store.get_intake("case_1")
        assert row is not None
        store.update_intake("case_1", row.revision, selected_tier="CERTIFIED", case_token=None)
        store.select_beta_batch("beta-1", "batch-1", "approval-1", BETA_NOW)
        beta_case = store.start_beta_case("case_1", BETA_DISCLOSURE_SHA256, BETA_NOW)
        assert beta_case.amount_cents == 0

        # Advance beta case through approval -> FROZEN
        store.apply_letter_event(
            beta_case.case_token, ResearchComplete(), beta_case.revision, occurred_at=BETA_NOW
        )
        draft_case = store.apply_letter_event(
            beta_case.case_token, DraftReady(), beta_case.revision + 1, occurred_at=BETA_NOW
        )
        store.apply_letter_event(
            beta_case.case_token,
            CustomerApproved(),
            draft_case.revision,
            occurred_at=BETA_NOW,
        )
        events = store.journey_events(workspace_token=beta_case.workspace_token).events
        assert JourneyEventKind.HOLD_AUTHORIZED not in [e.kind for e in events]
        assert [e.kind for e in events] == [JourneyEventKind.PACKET_APPROVED]
    finally:
        store.close()
