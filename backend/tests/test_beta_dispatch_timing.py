"""Selected free-beta mail keeps its customer-work window, never a paid Stripe expiry.

Real SqliteTransactionStore, the real dispatcher and submission coordinator, and the
existing fake Lob seam. Payments is a dependency that fails on any touch: a NO_CHARGE case
has no authorization, capture, fallback wakeup or expiry-driven cancellation. The customer
window is the only timer, and it still stops every pre-submission state at 30 minutes.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from threading import Barrier
from typing import cast
from uuid import uuid4

import pytest

from mail_samples import make_request, seed_approval, seed_destination
from parkingbreaker import case_state as cs
from parkingbreaker.beta_store import BETA_DISCLOSURE_SHA256
from parkingbreaker.effect_dispatch import _AuthorizationGuard, dispatch_once
from parkingbreaker.lob_fakes import FakeLobClient
from parkingbreaker.lob_submission import (
    TIMING_REFUSAL_REASON,
    Accepted,
    LetterEvidence,
    LetterRequest,
    LobResponse,
    MetadataLookup,
    Rejected,
    Uncertain,
    classify_response,
    idempotency_key,
)
from parkingbreaker.payment_records import PaymentClient
from parkingbreaker.payment_state import PaymentState
from parkingbreaker.session_window import DEFAULT_WINDOW
from parkingbreaker.submission_coordinator import RETRY_REJECTION_REASON
from parkingbreaker.submission_records import SubmissionCursor
from parkingbreaker.transaction_records import CaseRecord
from parkingbreaker.transaction_store import SqliteTransactionStore
from test_beta_admission import NOW, campaign, candidate

WINDOW = DEFAULT_WINDOW
ORIGINAL = "ltr_BetaOriginal"
OTHER = "ltr_SomeOtherLetter"
TOKEN = "beta-approved-submission"


class Clock:
    """The one explicit provider/coordinator clock; tests move it, nothing sleeps."""

    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def set(self, now: datetime) -> None:
        self.now = now


class ForbiddenPayments:
    """Any payment touch proves a free case entered the paid money machine."""

    def __getattribute__(self, name: str) -> object:
        raise AssertionError("payment dependency touched: " + name)


class Ids:
    def __init__(self, prefix: str = "beta") -> None:
        self.prefix = prefix
        self.count = 0

    def __call__(self) -> str:
        self.count += 1
        return f"{self.prefix}-id-{self.count:04d}"


class Deadline:
    """Deterministic researched deadline feasibility, and how often it was consulted.

    Scripted answers are consumed in order; the last one stays in force. `Deadline(False)`
    models research that already says the send date is unsafe.
    """

    def __init__(self, *answers: bool) -> None:
        self.answers = list(answers)
        self.calls = 0

    def __call__(self, token: str, now: datetime) -> bool:
        assert token
        assert now.tzinfo is not None
        self.calls += 1
        if not self.answers:
            return True
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


class LostAnswer(FakeLobClient):
    """The provider created the letter but its HTTP answer never arrived.

    The letter and its evidence stay with the provider, and the same key replays it, like
    real provider idempotency. `hidden_metadata` makes the token search omit the known
    original so only the exact known-letter retrieval still sees it.
    """

    def __init__(
        self,
        *,
        evidence: LetterEvidence = LetterEvidence.CREATED,
        hidden_metadata: bool = False,
        letter_id: str = ORIGINAL,
    ) -> None:
        super().__init__()
        self.evidence = evidence
        self.hidden_metadata = hidden_metadata
        self.letter_id = letter_id
        self.retrievals: list[str] = []

    def create_letter(self, request: LetterRequest, idempotency_key: str) -> LobResponse:
        self.calls.append((idempotency_key, request))
        known = self.by_key.get(idempotency_key)
        if known is not None:
            return LobResponse(
                200,
                known,
                evidence=self.evidence_by_token.get(request.submission_token, self.evidence),
            )
        self.by_key[idempotency_key] = self.letter_id
        self.letters[request.submission_token] = self.letter_id
        self.evidence_by_token[request.submission_token] = self.evidence
        return LobResponse(None, error="connection lost")

    def find_by_metadata(self, submission_token: str) -> str | MetadataLookup | None:
        if self.hidden_metadata:
            self.lookups.append(submission_token)
            return MetadataLookup(None, authoritative=True)
        return super().find_by_metadata(submission_token)

    def lookup_letter(self, letter_id: str, submission_token: str) -> MetadataLookup:
        self.retrievals.append(letter_id)
        return super().lookup_letter(letter_id, submission_token)


class LostThenReject(LostAnswer):
    """The first answer is lost; the same-key retry is later refused as invalid.

    This is the provider-side 422/400/413 shape after the adapter has sanitized an
    allowlisted validation error. The letter created behind the lost answer remains with
    the provider, so the refusal cannot prove the earlier request was never sent.
    """

    def __init__(
        self,
        *,
        status: int = 422,
        refuse_retry: bool = True,
        evidence: LetterEvidence = LetterEvidence.CREATED,
        letter_id: str = ORIGINAL,
    ) -> None:
        super().__init__(evidence=evidence, letter_id=letter_id)
        self.status = status
        self.refuse_retry = refuse_retry

    def create_letter(self, request: LetterRequest, idempotency_key: str) -> LobResponse:
        known = self.by_key.get(idempotency_key)
        if known is not None and self.refuse_retry:
            self.calls.append((idempotency_key, request))
            return LobResponse(self.status, error="provider validation rejected")
        return super().create_letter(request, idempotency_key)


class LookupFake(FakeLobClient):
    """One scripted reconciliation lookup; it never creates a letter."""

    def __init__(
        self,
        found: str | MetadataLookup | None,
        retrieval: MetadataLookup | None = None,
    ) -> None:
        super().__init__()
        self.found = found
        self.retrieval = retrieval
        self.retrievals: list[str] = []

    def find_by_metadata(self, submission_token: str) -> str | MetadataLookup | None:
        self.lookups.append(submission_token)
        return self.found

    def lookup_letter(self, letter_id: str, submission_token: str) -> MetadataLookup:
        self.retrievals.append(letter_id)
        if self.retrieval is None:
            raise RuntimeError("unknown letter")
        return self.retrieval


class Beta:
    """A selected NO_CHARGE case with its approved original packet prepared.

    Nothing is pre-applied past the point under test: a fresh case keeps its natural
    `start_beta_case` timestamps, and the dispatcher itself must start submission.
    """

    def __init__(self, path: Path, *, approve: bool = True) -> None:
        self.path = path
        self.store = SqliteTransactionStore(path)
        campaign(self.store)
        candidate(self.store, 1)
        self.store.select_beta_batch("beta-1", "batch-1", "approval-1", NOW)
        case = self.store.start_beta_case("case_1", BETA_DISCLOSURE_SHA256, NOW)
        assert (case.amount_cents, case.payment_state) == (0, PaymentState.NO_CHARGE)
        seed_destination(self.store, case, NOW)
        self.token = case.case_token
        self.clock = Clock()
        self.ids = Ids()
        self.lob = FakeLobClient()
        self.deadline = Deadline()
        self.payments = cast(PaymentClient, ForbiddenPayments())
        case = self._post(case, cs.ResearchComplete(), cs.DraftReady())
        if approve:
            case = self._post(case, cs.CustomerApproved())
        expected = cs.CaseState.FROZEN if approve else cs.CaseState.AWAITING_ADOPTION
        assert case.letter_state is expected
        self.store.prepare(self.token, 1, make_request(token=TOKEN), NOW)
        if approve:
            seed_approval(self.store, self.token)

    def _post(self, case: CaseRecord, *events: cs.Event) -> CaseRecord:
        for event in events:
            case = self.store.apply_letter_event(
                case.case_token,
                event,
                case.revision,
                occurred_at=NOW,
                new_effect_id=lambda: uuid4().hex,
            )
        return case

    @property
    def case(self) -> CaseRecord:
        saved = self.store.get_case(self.token)
        assert saved is not None
        return saved

    @property
    def cursor(self) -> SubmissionCursor:
        saved = self.store.submission_cursor(self.token, 1)
        assert saved is not None
        return saved

    def dispatch(self, at: datetime | None = None, lob: FakeLobClient | None = None) -> int:
        if at is not None:
            self.clock.set(at)
        return dispatch_once(
            self.store,
            self.lob if lob is None else lob,
            self.payments,
            self.clock,
            self.ids,
            case_id=self.token,
            deadline_feasible=self.deadline,
        )

    def restart(self) -> None:
        self.store.close()
        self.store = SqliteTransactionStore(self.path)


def last_event(beta: Beta) -> str:
    return beta.store.transition_history(beta.token)[-1].event_name


def assert_no_money_or_second_attempt(beta: Beta) -> None:
    assert [row for row in beta.store.effects(beta.token) if row.machine == "payment"] == []
    assert beta.store.payment_cursor(beta.token) is None
    assert beta.store.get(beta.token, 2) is None
    assert beta.case.attempt == 1


def uncertain_known(beta: Beta, evidence: LetterEvidence = LetterEvidence.CREATED) -> None:
    """One create that reached the provider, recorded under the original letter id."""
    beta.lob.script.append(LobResponse(200, ORIGINAL, evidence=evidence))
    beta.dispatch(NOW + timedelta(minutes=1))
    cursor = beta.cursor
    assert isinstance(cursor.outcome, Uncertain)
    assert cursor.outcome.letter_id == ORIGINAL


def orphaned_intent(beta: Beta) -> datetime:
    """One create whose answer was lost and that recorded no letter id."""
    beta.lob.script.append(LobResponse(None, error="timeout"))
    beta.dispatch(NOW + timedelta(minutes=1))
    cursor = beta.cursor
    assert isinstance(cursor.outcome, Uncertain)
    assert cursor.outcome.letter_id is None
    return cursor.first_sent_at + timedelta(hours=23)


@pytest.mark.parametrize(
    "offset",
    [timedelta(minutes=1), WINDOW - timedelta(seconds=1)],
    ids=["plus-1-minute", "just-before-window-close"],
)
def test_beta_frozen_dispatch_creates_once_without_paid_expiry(
    tmp_path: Path, offset: timedelta
) -> None:
    beta = Beta(tmp_path / "pb.sqlite")
    beta.lob.script.append(LobResponse(200, ORIGINAL, evidence=LetterEvidence.PRODUCTION))
    at = NOW + offset

    processed = beta.dispatch(at)
    case = beta.case
    assert case.letter_state is cs.CaseState.IN_TRANSIT, (
        f"the dispatcher stopped a free FROZEN case at {at}: {last_event(beta)}"
    )
    assert processed > 0
    assert case.payment_state is PaymentState.NO_CHARGE
    # Exactly one original create, under the deterministic key of this one attempt.
    assert len(beta.lob.calls) == 1
    key, request = beta.lob.calls[0]
    assert key == idempotency_key(beta.token, 1)
    assert request.submission_token == TOKEN
    assert request == make_request(token=TOKEN)
    assert beta.lob.letters == {TOKEN: ORIGINAL}
    # The acceptance callback records that token and the dispatch instant, not a paid
    # expiry instant and not a second letter.
    history = beta.store.transition_history(beta.token)[-2:]
    assert [row.event_name for row in history] == ["SubmitRequested", "SubmitConfirmed"]
    assert {row.occurred_at for row in history} == {at}
    operation = (
        beta.store._require_open()
        .execute(
            "SELECT started_at, completed_at, outcome_json FROM pb_submission_operations "
            "WHERE case_token=? AND attempt=1 ORDER BY rowid DESC LIMIT 1",
            (beta.token,),
        )
        .fetchone()
    )
    assert operation is not None
    assert operation[:2] == (at.isoformat(), at.isoformat())
    assert ORIGINAL in operation[2]
    assert_no_money_or_second_attempt(beta)


@pytest.mark.parametrize(
    "offset", [WINDOW, WINDOW + timedelta(minutes=1)], ids=["at-thirty", "after-thirty"]
)
def test_beta_frozen_mail_still_stops_at_the_customer_window(
    tmp_path: Path, offset: timedelta
) -> None:
    beta = Beta(tmp_path / "pb.sqlite")

    assert beta.dispatch(NOW + offset) == 0

    assert beta.case.letter_state is cs.CaseState.STOPPED_NO_CHARGE
    assert last_event(beta) == "WindowExpired"
    assert beta.lob.calls == []
    assert beta.lob.lookups == []
    assert beta.lob.cancels == []
    assert beta.store.submission_cursor(beta.token, 1) is None
    assert_no_money_or_second_attempt(beta)


def test_beta_window_still_stops_a_non_frozen_windowed_state(tmp_path: Path) -> None:
    beta = Beta(tmp_path / "pb.sqlite", approve=False)

    assert beta.dispatch(NOW + WINDOW) == 0

    assert beta.case.letter_state is cs.CaseState.STOPPED_NO_CHARGE
    assert last_event(beta) == "WindowExpired"
    assert beta.lob.calls == []
    assert beta.store.get(beta.token, 2) is None


def test_beta_research_deadline_refusal_stops_without_a_create(tmp_path: Path) -> None:
    beta = Beta(tmp_path / "pb.sqlite")
    beta.deadline = Deadline(False)

    assert beta.dispatch(NOW + timedelta(minutes=1)) == 0

    assert beta.case.letter_state is cs.CaseState.STOPPED_NO_CHARGE
    assert last_event(beta) == "Stopped"
    assert beta.lob.calls == []
    assert beta.store.submission_cursor(beta.token, 1) is None
    assert_no_money_or_second_attempt(beta)


def test_deadline_crossing_refuses_the_first_create_locally(tmp_path: Path) -> None:
    beta = Beta(tmp_path / "pb.sqlite")
    # Feasible at the FROZEN precheck, unsafe when the create guard rechecks it.
    beta.deadline = Deadline(True, False)

    beta.dispatch(NOW + timedelta(minutes=1))

    assert beta.deadline.calls == 2
    assert beta.lob.calls == []
    assert beta.case.letter_state is cs.CaseState.STOPPED_NO_CHARGE
    assert last_event(beta) == "TimingRefused"
    assert_no_money_or_second_attempt(beta)


def test_possibly_sent_beta_intent_stays_uncertain_and_reconcilable(tmp_path: Path) -> None:
    beta = Beta(tmp_path / "pb.sqlite")
    beta.lob = LostAnswer(evidence=LetterEvidence.CREATED)
    beta.dispatch(NOW + timedelta(minutes=1))
    first = beta.cursor
    assert isinstance(first.outcome, Uncertain)
    assert first.outcome.letter_id is None

    # The researched deadline turns unsafe after the call intent was committed: the
    # create guard refuses locally and, because a letter may already exist, keeps the
    # answer uncertain instead of claiming a definite non-send.
    beta.deadline = Deadline(False)
    beta.dispatch(first.next_at)
    refused = beta.cursor
    assert isinstance(refused.outcome, Uncertain)
    assert refused.outcome.reason == TIMING_REFUSAL_REASON
    assert refused.outcome.letter_id is None
    assert beta.case.letter_state is cs.CaseState.SUBMITTING
    assert len(beta.lob.calls) == 1
    assert beta.lob.cancels == []
    assert_no_money_or_second_attempt(beta)

    # Later production evidence accepts that same original on a bounded same-key retry.
    beta.deadline = Deadline()
    beta.lob.evidence_by_token[TOKEN] = LetterEvidence.PRODUCTION
    beta.dispatch(refused.next_at)
    accepted = beta.case
    assert accepted.letter_state is cs.CaseState.IN_TRANSIT
    assert len(beta.lob.calls) == 2
    assert {call[0] for call in beta.lob.calls} == {idempotency_key(beta.token, 1)}
    assert beta.lob.letters == {TOKEN: ORIGINAL}
    assert beta.lob.cancels == []
    assert_no_money_or_second_attempt(beta)


@pytest.mark.parametrize("status", [400, 413, 422], ids=["400", "413", "422"])
def test_retry_validation_rejection_after_lost_answer_keeps_uncertainty(
    tmp_path: Path, status: int
) -> None:
    """A later same-key validation refusal cannot disprove the earlier lost attempt."""
    beta = Beta(tmp_path / "pb.sqlite")
    beta.lob = LostThenReject(status=status)
    beta.dispatch(NOW + timedelta(minutes=1))
    first = beta.cursor
    assert isinstance(first.outcome, Uncertain)
    assert first.outcome.letter_id is None
    # The first answer was lost, but the provider did create the original letter.
    assert beta.lob.letters == {TOKEN: ORIGINAL}

    beta.dispatch(first.next_at)
    refused = beta.cursor
    assert isinstance(refused.outcome, Uncertain)
    assert refused.outcome.reason == RETRY_REJECTION_REASON
    assert refused.outcome.letter_id is None
    refused_case = beta.case
    assert refused_case.letter_state is cs.CaseState.SUBMITTING
    assert len(beta.lob.calls) == 2
    assert len({call[0] for call in beta.lob.calls}) == 1
    assert beta.lob.calls[0][1] == beta.lob.calls[1][1] == make_request(token=TOKEN)
    assert beta.lob.cancels == []
    assert_no_money_or_second_attempt(beta)

    # The bounded lookup at the existing cutoff still recovers that same original.
    beta.restart()
    beta.lob.evidence_by_token[TOKEN] = LetterEvidence.PRODUCTION
    beta.dispatch(first.first_sent_at + timedelta(hours=23))
    recovered = beta.case
    assert recovered.letter_state is cs.CaseState.IN_TRANSIT
    accepted = beta.cursor
    assert isinstance(accepted.outcome, Accepted)
    assert accepted.outcome.letter_id == ORIGINAL
    assert len(beta.lob.calls) == 2
    assert beta.lob.letters == {TOKEN: ORIGINAL}
    assert beta.lob.cancels == []
    assert_no_money_or_second_attempt(beta)


def test_first_validation_rejection_without_prior_intent_still_stops(tmp_path: Path) -> None:
    """With no earlier possibly-sent intent, a first refusal remains a definite non-send."""
    beta = Beta(tmp_path / "pb.sqlite")
    beta.lob.script.append(LobResponse(422, error="provider validation rejected"))

    beta.dispatch(NOW + timedelta(minutes=1))

    cursor = beta.cursor
    assert isinstance(cursor.outcome, Rejected)
    assert cursor.outcome.status == 422
    assert beta.case.letter_state is cs.CaseState.STOPPED_NO_CHARGE
    assert beta.lob.letters == {}
    assert beta.lob.cancels == []
    assert_no_money_or_second_attempt(beta)


def test_transport_failure_before_provider_creation_retries_the_same_key(
    tmp_path: Path,
) -> None:
    """A create the provider never received may be replayed under its one original key."""
    beta = Beta(tmp_path / "pb.sqlite")
    # Transport failed before the provider created anything: no inventory, no letter id.
    beta.lob.script.append(LobResponse(None, error="connection refused"))
    beta.dispatch(NOW + timedelta(minutes=1))
    first = beta.cursor
    assert isinstance(first.outcome, Uncertain)
    assert first.outcome.letter_id is None
    assert beta.lob.letters == {}
    assert beta.lob.by_key == {}
    assert len(beta.lob.calls) == 1
    assert first.next_at == NOW + timedelta(minutes=1, seconds=30)

    beta.restart()
    assert beta.cursor.next_at == first.next_at
    # The retry is due exactly at next_at and the provider now accepts the creation.
    beta.lob.script.append(LobResponse(200, ORIGINAL, evidence=LetterEvidence.PRODUCTION))
    beta.dispatch(first.next_at)

    accepted = beta.case
    assert accepted.letter_state is cs.CaseState.IN_TRANSIT
    # One distinct mailing: two create HTTP calls, one key and one identical frozen request.
    assert len(beta.lob.calls) == 2
    assert len({call[0] for call in beta.lob.calls}) == 1
    assert beta.lob.calls[0][0] == idempotency_key(beta.token, 1)
    assert beta.lob.calls[0][1] == beta.lob.calls[1][1]
    assert beta.lob.calls[0][1] == make_request(token=TOKEN)
    assert beta.lob.letters == {TOKEN: ORIGINAL}
    assert beta.lob.lookups == []
    assert beta.lob.cancels == []
    assert_no_money_or_second_attempt(beta)


def test_committed_but_never_called_intent_creates_under_its_original_key(
    tmp_path: Path,
) -> None:
    """A committed call intent whose provider call never happened still sends once."""
    beta = Beta(tmp_path / "pb.sqlite")
    at = NOW + timedelta(minutes=1)
    case = beta.store.apply_letter_event(
        beta.token,
        cs.SubmitRequested(),
        beta.case.revision,
        occurred_at=at,
        new_effect_id=lambda: uuid4().hex,
    )
    # Process death after the durable call intent and before any provider request.
    with beta.store.submission_ownership():
        claimed, kind = beta.store.claim_submission(
            beta.token, 1, case.revision, None, now=at, operation_id=beta.ids()
        )
    assert kind == "create"
    assert beta.lob.calls == []
    assert beta.lob.letters == {}

    beta.restart()
    beta.lob.script.append(LobResponse(200, ORIGINAL, evidence=LetterEvidence.PRODUCTION))
    beta.dispatch(claimed.next_at)

    accepted = beta.case
    assert accepted.letter_state is cs.CaseState.IN_TRANSIT
    assert len(beta.lob.calls) == 1
    assert beta.lob.calls[0][0] == idempotency_key(beta.token, 1)
    assert beta.lob.calls[0][1] == make_request(token=TOKEN)
    assert beta.lob.letters == {TOKEN: ORIGINAL}
    assert beta.lob.cancels == []
    assert_no_money_or_second_attempt(beta)


def test_beta_create_guard_omits_only_the_financial_expiry() -> None:
    """No Stripe expiry is applied to a free case, but its own deadline guard stays."""
    late = NOW + timedelta(hours=2)
    lob = FakeLobClient([LobResponse(200, ORIGINAL, evidence=LetterEvidence.PRODUCTION)])
    free = _AuthorizationGuard(lob, None, Clock(late), False, lambda now: True)
    assert free.create_letter(make_request(token=TOKEN), "pb-fixture-key").status == 200
    assert len(lob.calls) == 1
    # The identical instant with a financial expiry keeps the paid refusal, no call.
    paid = _AuthorizationGuard(lob, late, Clock(late), False, lambda now: True)
    refusal = paid.create_letter(make_request(token=TOKEN), "pb-fixture-key")
    assert (refusal.status, refusal.error) == (422, "AUTHORIZATION_EXPIRED")
    assert len(lob.calls) == 1


def test_beta_guard_keeps_a_possibly_sent_intent_uncertain() -> None:
    """A committed create intent keeps uncertainty; it is never a definite non-send."""
    guard = _AuthorizationGuard(FakeLobClient(), None, Clock(), True, lambda now: False)
    response = guard.create_letter(make_request(token=TOKEN), "pb-fixture-key")
    assert response.status is None
    assert response.local_refusal is False
    assert response.error == TIMING_REFUSAL_REASON
    outcome = classify_response(response)
    assert isinstance(outcome, Uncertain)
    assert outcome.reason == TIMING_REFUSAL_REASON


def test_lost_create_answer_recovers_the_same_token_after_reopen(tmp_path: Path) -> None:
    beta = Beta(tmp_path / "pb.sqlite")
    beta.lob = LostAnswer(evidence=LetterEvidence.PRODUCTION)
    beta.dispatch(NOW + timedelta(minutes=1))
    claimed = beta.cursor
    assert isinstance(claimed.outcome, Uncertain)
    assert claimed.outcome.letter_id is None
    # The provider really holds the original letter; only the answer was lost.
    assert beta.lob.letters == {TOKEN: ORIGINAL}
    # Ordinary positive retry backoff, not the paid expiry clamp to "now".
    assert claimed.next_at == NOW + timedelta(minutes=1, seconds=30)

    beta.restart()
    reopened = beta.cursor
    assert (
        reopened.next_at,
        reopened.first_sent_at,
        reopened.sends,
        reopened.lookup_only,
    ) == (
        claimed.next_at,
        claimed.first_sent_at,
        claimed.sends,
        claimed.lookup_only,
    )

    # Before the due time the scheduler makes no provider call at all.
    assert beta.dispatch(claimed.next_at - timedelta(seconds=20)) == 0
    assert len(beta.lob.calls) == 1
    assert beta.lob.lookups == []
    assert beta.lob.cancels == []

    beta.dispatch(claimed.next_at)
    assert beta.case.letter_state is cs.CaseState.IN_TRANSIT
    # One distinct mailing: one key, one identical frozen request, one provider letter.
    # The replayed create is a bounded same-key retry, not a second submission, and the
    # metadata lookup is reserved for the idempotency cutoff below.
    assert len(beta.lob.calls) == 2
    assert len({call[0] for call in beta.lob.calls}) == 1
    assert beta.lob.calls[0] == beta.lob.calls[1]
    assert beta.lob.letters == {TOKEN: ORIGINAL}
    assert beta.lob.lookups == []
    assert beta.lob.cancels == []
    assert_no_money_or_second_attempt(beta)


@pytest.mark.parametrize(
    "evidence",
    [LetterEvidence.CREATED, LetterEvidence.RENDERED],
    ids=["created", "rendered"],
)
def test_proven_original_stays_uncertain_without_cancel_or_capture(
    tmp_path: Path, evidence: LetterEvidence
) -> None:
    beta = Beta(tmp_path / "pb.sqlite")
    beta.lob.script.append(LobResponse(200, ORIGINAL, evidence=evidence))
    beta.dispatch(NOW + timedelta(minutes=1))
    claimed = beta.cursor
    assert isinstance(claimed.outcome, Uncertain)
    assert claimed.outcome.letter_id == ORIGINAL

    # A due reconciliation lookup is still uncertain: never a paid cancel or capture.
    beta.dispatch(claimed.next_at)
    assert beta.case.letter_state is cs.CaseState.SUBMITTING
    assert isinstance(beta.cursor.outcome, Uncertain)
    # Past the customer window the case is already SUBMITTING, where it does not govern.
    beta.restart()
    beta.dispatch(NOW + timedelta(minutes=45))
    assert beta.case.letter_state is cs.CaseState.SUBMITTING
    assert isinstance(beta.cursor.outcome, Uncertain)
    assert beta.lob.cancels == []
    assert beta.lob.letters == {TOKEN: ORIGINAL}
    assert_no_money_or_second_attempt(beta)

    # Later authoritative production evidence accepts this same original letter.
    beta.lob.evidence_by_token[TOKEN] = LetterEvidence.PRODUCTION
    beta.dispatch(beta.cursor.next_at)
    accepted = beta.case
    assert accepted.letter_state is cs.CaseState.IN_TRANSIT
    assert len(beta.lob.calls) == 1
    assert beta.lob.cancels == []
    # The existing beta return closure still applies to the accepted original.
    returned = beta.store.apply_letter_event(
        beta.token,
        cs.ReturnConfirmed("OTHER"),
        beta.case.revision,
        occurred_at=beta.clock(),
        new_effect_id=lambda: uuid4().hex,
    )
    assert returned.letter_state is cs.CaseState.RETURNED
    assert_no_money_or_second_attempt(beta)


def test_lookup_cutoff_finds_the_original_without_another_create(tmp_path: Path) -> None:
    beta = Beta(tmp_path / "pb.sqlite")
    beta.lob = LostAnswer(evidence=LetterEvidence.PRODUCTION)
    beta.dispatch(NOW + timedelta(minutes=1))
    claimed = beta.cursor
    assert isinstance(claimed.outcome, Uncertain)
    assert claimed.outcome.letter_id is None

    cutoff = claimed.first_sent_at + timedelta(hours=23)
    beta.restart()
    beta.dispatch(cutoff)

    assert beta.case.letter_state is cs.CaseState.IN_TRANSIT
    assert len(beta.lob.calls) == 1
    assert beta.lob.lookups == [TOKEN]
    assert beta.lob.cancels == []
    assert_no_money_or_second_attempt(beta)


def test_lookup_cutoff_uses_the_exact_id_when_metadata_omits_it(tmp_path: Path) -> None:
    beta = Beta(tmp_path / "pb.sqlite")
    uncertain_known(beta)
    cutoff = beta.cursor.first_sent_at + timedelta(hours=23)
    fake = LookupFake(
        MetadataLookup(None, authoritative=True),
        retrieval=MetadataLookup(ORIGINAL, authoritative=False, evidence=LetterEvidence.PRODUCTION),
    )

    beta.dispatch(cutoff, lob=fake)

    assert beta.case.letter_state is cs.CaseState.IN_TRANSIT
    assert fake.calls == []
    assert fake.lookups == [TOKEN]
    assert fake.retrievals == [ORIGINAL]
    assert fake.cancels == []
    assert_no_money_or_second_attempt(beta)


def test_lookup_cutoff_keeps_uncertainty_for_unproven_retrieval(tmp_path: Path) -> None:
    beta = Beta(tmp_path / "pb.sqlite")
    uncertain_known(beta)
    cutoff = beta.cursor.first_sent_at + timedelta(hours=23)
    fake = LookupFake(
        MetadataLookup(None, authoritative=True),
        retrieval=MetadataLookup(ORIGINAL, authoritative=False, evidence=LetterEvidence.CREATED),
    )

    beta.dispatch(cutoff, lob=fake)

    cursor = beta.cursor
    assert isinstance(cursor.outcome, Uncertain)
    assert cursor.outcome.letter_id == ORIGINAL
    assert beta.case.letter_state is cs.CaseState.SUBMITTING
    assert fake.calls == []
    assert fake.retrievals == [ORIGINAL]
    assert fake.cancels == []
    # Ordinary positive lookup backoff, not an expiry-clamped "now".
    assert cursor.next_at == cutoff + timedelta(minutes=30)
    assert_no_money_or_second_attempt(beta)


def test_beta_lookup_rejects_a_different_letter(tmp_path: Path) -> None:
    beta = Beta(tmp_path / "pb.sqlite")
    uncertain_known(beta)
    fake = LookupFake(MetadataLookup(OTHER, authoritative=True, evidence=LetterEvidence.PRODUCTION))

    beta.dispatch(beta.cursor.next_at, lob=fake)

    cursor = beta.cursor
    assert isinstance(cursor.outcome, Uncertain)
    assert cursor.outcome.letter_id == ORIGINAL
    assert beta.case.letter_state is cs.CaseState.SUBMITTING
    assert fake.calls == []
    assert fake.cancels == []
    assert_no_money_or_second_attempt(beta)


def test_beta_incomplete_absence_stays_uncertain(tmp_path: Path) -> None:
    beta = Beta(tmp_path / "pb.sqlite")
    uncertain_known(beta)
    fake = LookupFake(
        MetadataLookup(None, authoritative=False),
        retrieval=MetadataLookup(ORIGINAL, authoritative=False, evidence=LetterEvidence.CREATED),
    )

    beta.dispatch(beta.cursor.next_at, lob=fake)

    cursor = beta.cursor
    assert isinstance(cursor.outcome, Uncertain)
    assert cursor.outcome.letter_id == ORIGINAL
    assert fake.retrievals == [ORIGINAL]
    assert fake.calls == []
    assert fake.cancels == []


def test_beta_legacy_absence_without_an_anchor_stays_uncertain(tmp_path: Path) -> None:
    beta = Beta(tmp_path / "pb.sqlite")
    cutoff = orphaned_intent(beta)
    fake = LookupFake("ltr_legacy_bare_string")

    beta.dispatch(cutoff, lob=fake)

    assert isinstance(beta.cursor.outcome, Uncertain)
    assert beta.case.letter_state is cs.CaseState.SUBMITTING
    assert fake.calls == []
    assert fake.cancels == []


def test_beta_authoritative_absence_stops_without_sending_again(tmp_path: Path) -> None:
    beta = Beta(tmp_path / "pb.sqlite")
    cutoff = orphaned_intent(beta)
    fake = LookupFake(MetadataLookup(None, authoritative=True))

    beta.dispatch(cutoff, lob=fake)

    assert beta.case.letter_state is cs.CaseState.STOPPED_NO_CHARGE
    assert last_event(beta) == "ConfirmedNotSent"
    assert fake.calls == []
    assert fake.cancels == []
    assert_no_money_or_second_attempt(beta)


def test_beta_matching_voided_evidence_is_canceled_without_new_requests(
    tmp_path: Path,
) -> None:
    beta = Beta(tmp_path / "pb.sqlite")
    uncertain_known(beta)
    fake = LookupFake(MetadataLookup(ORIGINAL, authoritative=True, evidence=LetterEvidence.VOIDED))

    beta.dispatch(beta.cursor.next_at, lob=fake)

    assert beta.case.letter_state is cs.CaseState.STOPPED_NO_CHARGE
    assert last_event(beta) == "ConfirmedNotSent"
    # Existing cancellation truth only: no new cancel request and no second submission.
    assert fake.cancels == []
    assert fake.calls == []
    assert_no_money_or_second_attempt(beta)


def test_competing_and_repeated_dispatchers_keep_one_identity(tmp_path: Path) -> None:
    beta = Beta(tmp_path / "pb.sqlite")
    beta.lob.script.append(LobResponse(200, ORIGINAL, evidence=LetterEvidence.PRODUCTION))
    beta.clock.set(NOW + timedelta(minutes=1))
    ready = Barrier(2)

    def call(prefix: str) -> int:
        store = SqliteTransactionStore(beta.path)
        try:
            ready.wait(timeout=10)
            return dispatch_once(
                store,
                beta.lob,
                beta.payments,
                beta.clock,
                Ids(prefix),
                case_id=beta.token,
                deadline_feasible=beta.deadline,
            )
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(call, ("one", "two")))

    assert 0 in results
    assert len(beta.lob.calls) == 1
    assert beta.case.letter_state is cs.CaseState.IN_TRANSIT
    assert beta.lob.letters == {TOKEN: ORIGINAL}
    assert_no_money_or_second_attempt(beta)

    # A repeat dispatch after the fact does no further provider work.
    assert beta.dispatch(NOW + timedelta(minutes=2)) == 0
    assert len(beta.lob.calls) == 1
    assert_no_money_or_second_attempt(beta)
