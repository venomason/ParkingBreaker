"""T30: exact persisted destination instructions at the final Lob create boundary."""

import json
from collections.abc import Sequence
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from mail_samples import RECIPIENT, seed_approval
from parkingbreaker.address_disclosure import DISCLOSURE_SHA256, DISCLOSURE_VERSION
from parkingbreaker.address_policy import (
    AddressProvenance,
    ConsentEvent,
    DestinationDecision,
    Disclosure,
    EvidenceKind,
    EvidenceRef,
    address_fingerprint,
    mailing_gate,
)
from parkingbreaker.address_store import _json
from parkingbreaker.agencies import MailingAddress
from parkingbreaker.api import schemas as s
from parkingbreaker.api.service import ServiceConflict
from parkingbreaker.appeal.drafting import Draft
from parkingbreaker.appeal.fakes import FakeDrafter
from parkingbreaker.case_state import CaseState, RemailRequested, ReturnConfirmed
from parkingbreaker.intake_store import IntakeConflict
from parkingbreaker.lob_fakes import FakeLobClient
from parkingbreaker.lob_submission import (
    Accepted,
    LetterEvidence,
    LetterRequest,
    LobResponse,
    MetadataLookup,
    NotSent,
    Rejected,
    Uncertain,
)
from parkingbreaker.mail_attempt_store import PreparedMailAttempt
from parkingbreaker.payment_state import CaptureSucceeded, PaymentEffect, PaymentState
from parkingbreaker.submission_coordinator import run_submission_step
from parkingbreaker.submission_records import SubmissionCursor
from parkingbreaker.transaction_records import CaseRecord
from parkingbreaker.transaction_store import SqliteTransactionStore
from test_case_service import NOW, approve_with_preview, drafting_case, fake_resolution, stripe
from test_effect_dispatch import approved, payment_fixture
from test_submission_coordinator import prepared, step
from test_transaction_lifecycle import letter

assert fake_resolution
REFUSAL = "ADDRESS_MAILING_GATE_REFUSED"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def destination(customer: bool = False) -> DestinationDecision:
    consent = ConsentEvent(
        "intake", "owner", address_fingerprint(RECIPIENT), "b" * 64, 1, True, NOW
    )
    return DestinationDecision(
        "intake",
        "owner",
        RECIPIENT,
        address_fingerprint(RECIPIENT),
        AddressProvenance.CUSTOMER_DIRECTED_UNVERIFIED
        if customer
        else AddressProvenance.SYSTEM_VERIFIED,
        "address-responsibility-v1",
        NOW,
        verification_evidence=()
        if customer
        else (EvidenceRef("official-quote", "a" * 64, EvidenceKind.OFFICIAL_SOURCE_QUOTE),),
        failed_verification_code="NO_OFFICIAL_DESTINATION" if customer else None,
        customer_address_revision=1 if customer else None,
        disclosure=Disclosure("v1", "b" * 64) if customer else None,
        consent=consent if customer else None,
    )


@pytest.mark.parametrize("customer", [False, True])
def test_pure_gate_requires_verified_destination_under_current_policy(customer: bool) -> None:
    decision = destination(customer)
    consents = () if decision.consent is None else (decision.consent,)
    result = mailing_gate(
        decision,
        consents,
        case_id="intake",
        owner="owner",
        mailed_address=RECIPIENT,
        latest_customer_address_revision=1 if customer else 2,
        current_disclosure_sha256="b" * 64,
        now=NOW,
    )
    assert result.allowed is (not customer)
    assert result.reason == ("ADDRESS_MAILING_GATE_REFUSED" if customer else "VERIFIED_BY_EVIDENCE")


@pytest.mark.parametrize("returned", ["same", "different", "unverified"])
def test_free_remail_needs_verified_destination_that_differs_from_returned_address(
    returned: str,
) -> None:
    customer = returned == "unverified"
    decision = destination(customer)
    returned_address = (
        replace(RECIPIENT, line1="1 Elsewhere Road") if returned != "same" else RECIPIENT
    )
    consents = () if decision.consent is None else (decision.consent,)
    result = mailing_gate(
        decision,
        consents,
        case_id="intake",
        owner="owner",
        mailed_address=RECIPIENT,
        latest_customer_address_revision=1 if customer else 2,
        current_disclosure_sha256="b" * 64,
        now=NOW,
        returned_address=returned_address,
    )
    assert result.allowed is (returned == "different")


@pytest.mark.parametrize(
    "problem",
    [
        "no_decision",
        "legacy_A12",
        "one_byte",
        "decision_case",
        "decision_owner",
        "future_decision",
        "ai_only",
        "no_evidence",
        "embedded_consent_only",
        "consent_case",
        "consent_owner",
        "consent_revision",
        "consent_disclosure",
        "consent_address",
        "consent_future",
    ],
)
def test_pure_gate_refuses_without_exact_current_instruction(problem: str) -> None:
    decision: DestinationDecision | None = destination(
        problem.startswith("consent_") or problem == "embedded_consent_only"
    )
    assert decision is not None
    consents = () if decision.consent is None else (decision.consent,)
    address = RECIPIENT
    if problem in {"no_decision", "legacy_A12"}:
        decision = None
    elif problem == "one_byte":
        address = replace(address, line1=address.line1 + " ")
    elif problem == "decision_case":
        decision = replace(decision, case_id="other")
    elif problem == "decision_owner":
        decision = replace(decision, owner="other")
    elif problem == "future_decision":
        decision = replace(decision, decided_at=NOW + timedelta(seconds=1))
    elif problem in {"ai_only", "no_evidence"}:
        # Constructors/codecs reject these already; prove the final gate also refuses.
        evidence = (
            ()
            if problem == "no_evidence"
            else (EvidenceRef("ai", "a" * 64, EvidenceKind.LLM_SUMMARY),)
        )
        object.__setattr__(decision, "verification_evidence", evidence)
    elif problem == "embedded_consent_only":
        consents = ()
    else:
        consent = decision.consent
        assert consent is not None
        consents = (
            {
                "consent_case": replace(consent, case_id="other"),
                "consent_owner": replace(consent, owner="other"),
                "consent_revision": replace(consent, address_revision=2),
                "consent_disclosure": replace(consent, disclosure_sha256="c" * 64),
                "consent_address": replace(consent, address_fingerprint="c" * 64),
                "consent_future": replace(consent, accepted_at=NOW + timedelta(seconds=1)),
            }[problem],
        )
        object.__setattr__(decision, "consent", consents[0])
    result = mailing_gate(
        decision,
        consents,
        case_id="intake",
        owner="owner",
        mailed_address=address,
        latest_customer_address_revision=1,
        now=NOW,
    )
    assert not result.allowed
    assert result.reason == REFUSAL


@pytest.mark.anyio
@pytest.mark.usefixtures("fake_resolution")
async def test_normal_case_persists_official_destination_and_reaches_in_transit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lob = FakeLobClient([LobResponse(200, "ltr_verified", evidence=LetterEvidence.PRODUCTION)])
    client, real, case_id = await approved(tmp_path, monkeypatch, lob)
    try:
        record = real._intake(case_id)
        saved = real._store.latest_destination_decision(case_id, record.owner)
        assert saved is not None
        assert saved.decision.provenance is AddressProvenance.SYSTEM_VERIFIED
        from parkingbreaker.research.fulfilment import verified_findings
        from parkingbreaker.research.questions import FactKey
        from parkingbreaker.research_records import decode_research

        research = real._store.get_research(case_id)
        assert research is not None
        result = decode_research(research.research_json)
        finding = next(
            f
            for f in verified_findings(result.findings, result.sources, result.ticket)
            if f.key is FactKey.MAILING_ADDRESS
        )
        assert {ref.sha256 for ref in saved.decision.verification_evidence} == set(
            finding.source_sha256s
        )
        assert all(
            ref.kind is EvidenceKind.OFFICIAL_SOURCE_QUOTE
            for ref in saved.decision.verification_evidence
        )
        real.dispatch(case_id)
        assert real.status(case_id).state == "IN_TRANSIT"
        assert len(lob.calls) == 1
        assert lob.calls[0][1].recipient == saved.decision.address
    finally:
        await client.aclose()


@pytest.mark.anyio
@pytest.mark.usefixtures("fake_resolution")
async def test_destination_is_persisted_before_paid_drafter_is_called(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, real, case_id = await drafting_case(tmp_path, monkeypatch)
    try:
        drafter = real._deps.drafter
        assert isinstance(drafter, FakeDrafter)
        produce = drafter.produce
        observed: list[DestinationDecision] = []

        def checked_produce(attempt: int, problems: Sequence[str]) -> Draft:
            record = real._intake(case_id)
            # A separate connection proves persistence before the paid call.
            with SqliteTransactionStore(real._store.path) as reader:
                saved = reader.latest_destination_decision(case_id, record.owner)
                assert saved is not None
                assert saved.decision.provenance is AddressProvenance.SYSTEM_VERIFIED
                observed.append(saved.decision)
            return produce(attempt, problems)

        monkeypatch.setattr(drafter, "produce", checked_produce)
        real.step(case_id, s.WorkerStep(step_id="draft"))
        assert len(observed) == 1
        assert real.status(case_id).state is CaseState.AWAITING_ADOPTION
    finally:
        await client.aclose()


@pytest.mark.anyio
@pytest.mark.usefixtures("fake_resolution")
@pytest.mark.parametrize("damage", ["request_address", "deleted_decision"])
async def test_final_gate_stops_without_lob_or_capture_and_releases_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    lob = FakeLobClient([LobResponse(200, "must_not_send", evidence=LetterEvidence.PRODUCTION)])
    client, real, case_id = await approved(tmp_path, monkeypatch, lob)
    payments, transport = stripe(payment_fixture(), payment_fixture("canceled"))
    object.__setattr__(real._deps, "payments", payments)
    try:
        token = real._intake(case_id).case_token
        assert token is not None
        original_get = real._store.get
        if damage == "request_address":

            def tampered_get(case_token: str, attempt: int) -> PreparedMailAttempt | None:
                frozen = original_get(case_token, attempt)
                if frozen is None:
                    return None
                return replace(
                    frozen,
                    request=replace(
                        frozen.request,
                        recipient=replace(
                            frozen.request.recipient, line1=frozen.request.recipient.line1 + " "
                        ),
                    ),
                )

            monkeypatch.setattr(real._store, "get", tampered_get)
        else:
            real._store._require_open().execute(
                "DELETE FROM pb_address_decisions WHERE case_id=?", (case_id,)
            )
        assert real._store.submission_cursor(token, 1) is None
        real.dispatch(case_id)
        cursor = real._store.submission_cursor(token, 1)
        assert cursor is not None
        expected_reason = "LOCAL_REQUEST_REFUSED" if damage == "request_address" else REFUSAL
        assert cursor.outcome == Rejected(422, expected_reason)
        assert not lob.calls
        assert real.status(case_id).state == "STOPPED_NO_CHARGE"
        assert real.status(case_id).payment_state is PaymentState.RELEASED
        real.dispatch(case_id)
        assert real.status(case_id).payment_state is PaymentState.RELEASED
        assert not any(call[1].endswith("/capture") for call in transport.calls)
        assert any(call[1].endswith("/cancel") for call in transport.calls)
        assert not lob.calls
    finally:
        await client.aclose()


def test_gate_refusal_literal_matches_the_pure_gate() -> None:
    from parkingbreaker import transaction_store

    refused = mailing_gate(
        None,
        (),
        case_id="intake",
        owner="owner",
        mailed_address=RECIPIENT,
        latest_customer_address_revision=None,
        current_disclosure_sha256="b" * 64,
        now=NOW,
    )
    assert refused.reason == transaction_store.GATE_REFUSED_REASON == REFUSAL


@pytest.mark.anyio
@pytest.mark.usefixtures("fake_resolution")
@pytest.mark.parametrize("provider", ["gate", "lob_422", "local_refusal"])
async def test_first_send_stop_copy_is_true_to_whether_lob_was_called(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    gate = provider == "gate"
    local = provider == "local_refusal"
    lob = FakeLobClient(
        [LobResponse(200, "must_not_send", evidence=LetterEvidence.PRODUCTION)]
        if gate
        else [LobResponse(None, error="local request validation rejected", local_refusal=True)]
        if local
        else [LobResponse(422, error="provider validation rejected")]
    )
    client, real, case_id = await approved(tmp_path, monkeypatch, lob)
    payments, transport = stripe(payment_fixture(), payment_fixture("canceled"))
    object.__setattr__(real._deps, "payments", payments)
    try:
        if gate:
            real._store._require_open().execute(
                "DELETE FROM pb_address_decisions WHERE case_id=?", (case_id,)
            )
        real.dispatch(case_id)
        real.dispatch(case_id)
        status = real.status(case_id)
        assert status.state == "STOPPED_NO_CHARGE"
        assert status.payment_state is PaymentState.RELEASED
        assert not any(call[1].endswith("/capture") for call in transport.calls)
        assert any(call[1].endswith("/cancel") for call in transport.calls)
        assert status.stop is not None
        if gate:
            assert not lob.calls
            assert status.stop.reason_key == "stop.transaction_failure.destination_not_confirmed"
            assert status.stop.message.startswith(
                "We stopped before mailing because the mailing address could not be "
                "confirmed for this letter. You were not charged."
            )
            assert "did not accept" not in status.stop.message
        elif local:
            assert not lob.letters
            assert status.stop.reason_key == "stop.transaction_failure.letter_not_submittable"
            assert status.stop.message.startswith(
                "We stopped before mailing because this letter could not be prepared for "
                "mailing. You were not charged."
            )
            assert "did not accept" not in status.stop.message
        else:
            assert len(lob.calls) == 1
            assert status.stop.reason_key == "stop.transaction_failure.lob_rejected"
            assert "did not accept this letter" in status.stop.message
    finally:
        await client.aclose()


@pytest.mark.anyio
@pytest.mark.usefixtures("fake_resolution")
async def test_approval_refuses_persisted_destination_different_from_frozen_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, real, case_id = await approved(tmp_path, monkeypatch, FakeLobClient())
    try:
        record = real._intake(case_id)
        saved = real._store.latest_destination_decision(case_id, record.owner)
        draft = real._store.get_draft(case_id)
        assert saved is not None
        assert draft is not None
        assert draft.adopted_sha256 is not None
        changed = replace(saved.decision.address, line1=saved.decision.address.line1 + " ")
        changed_decision = replace(
            saved.decision, address=changed, fingerprint=address_fingerprint(changed)
        )
        with pytest.raises(IntakeConflict, match=r"^address_revision_stale$"):
            real._store.save_destination_decision(
                case_id, record.owner, changed_decision, saved.revision
            )
        # Inject historical drift to retain the downstream frozen-envelope proof.
        real._store._require_open().execute(
            "INSERT INTO pb_address_decisions VALUES (?, ?, ?, ?)",
            (case_id, record.owner, saved.revision + 1, _json(asdict(changed_decision))),
        )
        with pytest.raises(ServiceConflict, match="destination_decision_mismatch"):
            real.approve(
                case_id,
                s.Approve(
                    adopted_sha256=draft.adopted_sha256,
                    packet_sha256="0" * 64,
                    envelope_sha256="0" * 64,
                ),
            )
    finally:
        await client.aclose()


def test_legacy_unlinked_case_has_no_implicit_instruction(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = prepared(store)
        store._require_open().execute("DELETE FROM pb_address_decisions")
        store._require_open().execute("DELETE FROM pb_intake_records")
        lob = FakeLobClient([LobResponse(200, "must_not_send", evidence=LetterEvidence.PRODUCTION)])
        assert store.submission_cursor(case.case_token, 1) is None
        cursor = step(store, lob, case, now=NOW, effects=("stop", "release"))
        assert cursor.outcome == Rejected(422, REFUSAL)
        assert not lob.calls
        stopped = store.get_case(case.case_token)
        assert stopped is not None
        assert stopped.letter_state is CaseState.STOPPED_NO_CHARGE
        assert stopped.payment_state is PaymentState.RELEASED


def damage_destination(store: SqliteTransactionStore, damage: str) -> None:
    if damage == "deleted":
        store._require_open().execute("DELETE FROM pb_address_decisions")
    else:
        store._require_open().execute("UPDATE pb_address_decisions SET payload_json='{}'")


def retry_refused_gate(
    store: SqliteTransactionStore,
    lob: FakeLobClient,
    case: CaseRecord,
    previous: SubmissionCursor,
) -> SubmissionCursor:
    return run_submission_step(
        store,
        lob,
        case.case_token,
        case.attempt,
        expected_case_revision=case.revision,
        expected_cursor_revision=previous.revision,
        operation_id="refused-retry",
        clock=lambda: previous.next_at,
        new_effect_id=lambda: uuid4().hex,
    )


def assert_no_release(store: SqliteTransactionStore, case: CaseRecord) -> None:
    current = store.get_case(case.case_token)
    assert current is not None
    assert current.letter_state is not CaseState.STOPPED_NO_CHARGE
    assert current.payment_state is not PaymentState.RELEASED
    assert not any(
        event.event_name in {"SubmitRejected", "ConfirmedNotSent", "ReleaseRequested"}
        for event in store.transition_history(case.case_token)
    )
    assert not any(
        effect.effect == PaymentEffect.RELEASE_HOLD for effect in store.effects(case.case_token)
    )


@pytest.mark.parametrize("damage", ["deleted", "corrupted"])
@pytest.mark.parametrize("lookup", ["found", "absent", "incomplete", "failed"])
def test_refused_uncertain_retry_reconciles_without_releasing_hold(
    tmp_path: Path, damage: str, lookup: str
) -> None:
    class LookupLob(FakeLobClient):
        def find_by_metadata(self, submission_token: str) -> str | MetadataLookup | None:
            if lookup in {"found", "absent"}:
                return super().find_by_metadata(submission_token)
            self.lookups.append(submission_token)
            if lookup == "failed":
                raise OSError("lookup unavailable")
            return MetadataLookup()

    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = prepared(store, expires_at=NOW + timedelta(days=5))
        lob = LookupLob([LobResponse(None)])
        previous = step(store, lob, case, now=NOW)
        assert isinstance(previous.outcome, Uncertain)
        assert previous.next_at < previous.first_sent_at + timedelta(hours=23)
        frozen = store.get(case.case_token, 1)
        assert frozen is not None
        if lookup == "found":
            lob.letters[frozen.request.submission_token] = "ltr_existing"
        before_effects = store.effects(case.case_token)
        damage_destination(store, damage)
        cursor = retry_refused_gate(store, lob, case, previous)
        assert not isinstance(cursor.outcome, Rejected | NotSent)
        assert len(lob.calls) == 1
        assert lob.lookups == [frozen.request.submission_token]
        assert_no_release(store, case)
        current = store.get_case(case.case_token)
        assert current is not None
        if lookup == "found":
            assert cursor.outcome == Accepted("ltr_existing")
            assert current.letter_state is CaseState.IN_TRANSIT
            assert current.payment_state is PaymentState.CAPTURE_PENDING
            assert any(
                effect.effect == PaymentEffect.CAPTURE_PAYMENT
                for effect in store.effects(case.case_token)
            )
        else:
            assert isinstance(cursor.outcome, Uncertain)
            assert current.letter_state is CaseState.SUBMITTING
            assert current.payment_state is PaymentState.AUTHORIZED
            assert store.effects(case.case_token) == before_effects
            # Only the normal post-cutoff lookup can definitively establish absence.
            if lookup == "absent":
                terminal = step(
                    store, lob, case, cursor, NOW + timedelta(hours=23), ("stop", "release")
                )
                assert terminal.outcome == NotSent()
                assert len(lob.calls) == 1


@pytest.mark.parametrize("damage", ["deleted", "corrupted"])
@pytest.mark.parametrize("visible", [False, True])
def test_refused_retry_after_provider_create_crash_reconciles_on_restart(
    tmp_path: Path, damage: str, visible: bool
) -> None:
    class CrashAfterCreate(FakeLobClient):
        def create_letter(self, request: LetterRequest, idempotency_key: str) -> LobResponse:
            super().create_letter(request, idempotency_key)
            assert self.letters[request.submission_token] == "ltr_before_crash"
            raise SystemExit("process died after provider create")

        def find_by_metadata(self, submission_token: str) -> str | MetadataLookup | None:
            if visible:
                return super().find_by_metadata(submission_token)
            self.lookups.append(submission_token)
            # Model an existing letter that is not yet visible in metadata search.
            return MetadataLookup()

    path = tmp_path / "cases.sqlite"
    lob = CrashAfterCreate(
        [LobResponse(200, "ltr_before_crash", evidence=LetterEvidence.PRODUCTION)]
    )
    with SqliteTransactionStore(path) as store:
        case = prepared(store, expires_at=NOW + timedelta(days=5))
        with pytest.raises(SystemExit, match="process died"):
            step(store, lob, case, now=NOW)
    with SqliteTransactionStore(path) as store:
        previous = store.submission_cursor(case.case_token, 1)
        assert previous is not None
        assert previous.owner is not None
        assert isinstance(previous.outcome, Uncertain)
        before_effects = store.effects(case.case_token)
        damage_destination(store, damage)
        cursor = retry_refused_gate(store, lob, case, previous)
        assert not isinstance(cursor.outcome, Rejected | NotSent)
        assert len(lob.calls) == 1
        assert lob.lookups == [lob.calls[0][1].submission_token]
        assert_no_release(store, case)
        current = store.get_case(case.case_token)
        assert current is not None
        if visible:
            assert cursor.outcome == Accepted("ltr_before_crash")
            assert current.letter_state is CaseState.IN_TRANSIT
            assert current.payment_state is PaymentState.CAPTURE_PENDING
        else:
            assert isinstance(cursor.outcome, Uncertain)
            assert current.letter_state is CaseState.SUBMITTING
            assert current.payment_state is PaymentState.AUTHORIZED
            assert store.effects(case.case_token) == before_effects


def save_customer_instruction(
    store: SqliteTransactionStore, case_id: str, owner: str
) -> DestinationDecision:
    saved = store.latest_destination_decision(case_id, owner)
    assert saved is not None
    address = saved.decision.address
    decided_at = saved.decision.decided_at
    consent = ConsentEvent(
        case_id, owner, address_fingerprint(address), DISCLOSURE_SHA256, 1, True, decided_at
    )
    decision = replace(
        destination(True),
        case_id=case_id,
        owner=owner,
        address=address,
        fingerprint=address_fingerprint(address),
        decided_at=decided_at,
        disclosure=Disclosure(DISCLOSURE_VERSION, DISCLOSURE_SHA256),
        consent=consent,
    )
    # Seed a historical customer instruction: public customer writers are closed
    # for these already-drafted/frozen low-level fixtures.
    database = store._require_open()
    database.execute(
        "INSERT INTO pb_address_revisions VALUES (?, ?, ?, ?)",
        (case_id, owner, 1, _json({"address": asdict(address), "created_at": decided_at})),
    )
    database.execute(
        "INSERT INTO pb_address_consents VALUES (?, ?, ?, ?)",
        (case_id, owner, 1, _json(asdict(consent))),
    )
    database.execute(
        "INSERT INTO pb_address_decisions VALUES (?, ?, ?, ?)",
        (case_id, owner, saved.revision + 1, _json(asdict(decision))),
    )
    # This synthetic fixture represents approval of the customer-directed instruction.
    # Keep its frozen binding consistent; runtime must never backfill a legacy binding.
    database.execute(
        "UPDATE pb_mail_destination_bindings SET decision_revision=? WHERE intake_id=?",
        (saved.revision + 1, case_id),
    )
    return decision


def inject_customer_address_revision(
    store: SqliteTransactionStore,
    case_id: str,
    owner: str,
    address: MailingAddress,
    created_at: datetime,
) -> None:
    # Deliberate historical/corrupt snapshot injection keeps downstream gates tested
    # even though the public writer now refuses edits after drafting.
    store._require_open().execute(
        "INSERT INTO pb_address_revisions VALUES (?, ?, ?, ?)",
        (case_id, owner, 2, _json({"address": asdict(address), "created_at": created_at})),
    )


def test_new_customer_address_revision_invalidates_mailing_instruction(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = prepared(store)
        intake = store.intake_for_workspace(case.workspace_token)
        assert intake is not None
        decision = save_customer_instruction(store, intake.intake_id, intake.owner)
        changed = replace(decision.address, line1="999 New Address")
        with pytest.raises(IntakeConflict, match=r"^destination_locked_after_draft$"):
            store.save_customer_address(
                intake.intake_id, intake.owner, changed, NOW + timedelta(seconds=1), 1
            )
        inject_customer_address_revision(
            store,
            intake.intake_id,
            intake.owner,
            changed,
            NOW + timedelta(seconds=1),
        )
        lob = FakeLobClient([LobResponse(200, "must_not_send", evidence=LetterEvidence.PRODUCTION)])
        cursor = step(store, lob, case, now=NOW + timedelta(seconds=2), effects=("stop", "release"))
        assert cursor.outcome == Rejected(422, REFUSAL)
        assert not lob.calls
        current = store.get_case(case.case_token)
        assert current is not None
        assert current.letter_state is CaseState.STOPPED_NO_CHARGE
        assert current.payment_state is PaymentState.RELEASED


@pytest.mark.anyio
@pytest.mark.usefixtures("fake_resolution")
@pytest.mark.parametrize("frozen", [False, True])
async def test_approval_refuses_stale_customer_address_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, frozen: bool
) -> None:
    client, real, case_id = await drafting_case(tmp_path, monkeypatch)
    try:
        real.step(case_id, s.WorkerStep(step_id="draft"))
        draft = real.draft(case_id)
        adoption = real.adopt_text(
            case_id, s.AdoptText(text=draft.text, text_sha256=draft.text_sha256)
        )
        record = real._intake(case_id)
        body = approve_with_preview(real, case_id, adoption.adopted_sha256)
        if frozen:
            assert real.approve(case_id, body).state is CaseState.FROZEN
        # Simulate a persisted pre-policy instruction without enabling old production behavior.
        decision = save_customer_instruction(real._store, case_id, record.owner)
        changed = replace(decision.address, line1="999 New Address")
        with pytest.raises(IntakeConflict, match=r"^destination_locked_after_draft$"):
            real._store.save_customer_address(case_id, record.owner, changed, NOW, 1)
        inject_customer_address_revision(real._store, case_id, record.owner, changed, NOW)
        before = real._store.get_case(record.case_token or "")
        with pytest.raises(ServiceConflict, match=r"^destination_verification_required$"):
            real.approve(case_id, body)
        assert real._store.get_case(record.case_token or "") == before
    finally:
        await client.aclose()


def test_ai_only_persisted_claim_definitively_stops_before_provider(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = prepared(store)
        database = store._require_open()
        payload = json.loads(
            database.execute("SELECT payload_json FROM pb_address_decisions").fetchone()[0]
        )
        payload["verification_evidence"][0]["kind"] = EvidenceKind.LLM_SUMMARY
        database.execute(
            "UPDATE pb_address_decisions SET payload_json=?",
            (json.dumps(payload, sort_keys=True, separators=(",", ":")),),
        )
        lob = FakeLobClient([LobResponse(200, "must_not_send", evidence=LetterEvidence.PRODUCTION)])
        cursor = step(store, lob, case, now=NOW, effects=("stop", "release"))
        assert cursor.outcome == Rejected(422, REFUSAL)
        assert not lob.calls
        stopped = store.get_case(case.case_token)
        assert stopped is not None
        assert stopped.letter_state is CaseState.STOPPED_NO_CHARGE
        assert stopped.payment_state is PaymentState.RELEASED


def test_remail_attempt_requires_destination_too(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = prepared(store)
        lob = FakeLobClient(
            [
                LobResponse(200, "ltr_first", evidence=LetterEvidence.PRODUCTION),
                LobResponse(200, "must_not_send", evidence=LetterEvidence.PRODUCTION),
            ]
        )
        step(store, lob, case, now=NOW, effects=("route", "capture"))
        current = store.get_case(case.case_token)
        assert current is not None
        current = store.apply_payment_event(
            current.case_token, CaptureSucceeded("charge"), current.revision, occurred_at=NOW
        )
        current = letter(store, current, ReturnConfirmed("returned"))
        current = letter(store, current, RemailRequested())
        first = store.get(case.case_token, 1)
        assert first is not None
        store.prepare(
            case.case_token,
            2,
            replace(
                first.request,
                submission_token="remail",
                recipient=replace(first.request.recipient, line1="987 Corrected Avenue"),
                pdf=b"%PDF-1.4\ncorrected synthetic packet",
            ),
            NOW,
        )
        seed_approval(store, case.case_token, 2)
        store._require_open().execute("DELETE FROM pb_address_decisions")
        cursor = step(store, lob, current, now=NOW)
        assert cursor.outcome == Rejected(422, REFUSAL)
        assert len(lob.calls) == 1
        stopped = store.get_case(case.case_token)
        assert stopped is not None
        assert stopped.letter_state is CaseState.CLOSED_RETURNED
        assert stopped.payment_state is PaymentState.CAPTURED


def test_lookup_reconciles_existing_letter_without_destination(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = prepared(store, expires_at=NOW + timedelta(days=5))
        lob = FakeLobClient([LobResponse(None)])
        cursor = step(store, lob, case, now=NOW)
        frozen = store.get(case.case_token, 1)
        assert frozen is not None
        lob.letters[frozen.request.submission_token] = "ltr_already_created"
        store._require_open().execute("DELETE FROM pb_address_decisions")
        cursor = step(store, lob, case, cursor, NOW + timedelta(hours=23), ("route", "capture"))
        assert cursor.outcome == Accepted("ltr_already_created")
        assert len(lob.calls) == 1
        assert lob.lookups == [frozen.request.submission_token]


def test_persisted_customer_instruction_survives_restart_and_uses_same_coordinator(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        case = prepared(store)
        intake = store.intake_for_workspace(case.workspace_token)
        assert intake is not None
        decision = save_customer_instruction(store, intake.intake_id, intake.owner)
        consent = decision.consent
        assert consent is not None
    with SqliteTransactionStore(path) as store:
        before = store.destination_decision_history(intake.intake_id, intake.owner)
        lob = FakeLobClient(
            [LobResponse(200, "ltr_customer_chosen", evidence=LetterEvidence.PRODUCTION)]
        )
        cursor = step(store, lob, case, now=NOW, effects=("route", "capture"))
        assert cursor.outcome == Rejected(422, REFUSAL)
        assert lob.calls == []
        assert store.destination_decision_history(intake.intake_id, intake.owner) == before
        assert store.address_consent_events(intake.intake_id, intake.owner) == (consent,)
