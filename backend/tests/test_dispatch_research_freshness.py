"""Current evidence permits create; aged evidence never erases a prior send intent."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest

from parkingbreaker.api import schemas as s
from parkingbreaker.case_service import CaseService
from parkingbreaker.case_state import CaseState
from parkingbreaker.lob_fakes import FakeLobClient
from parkingbreaker.lob_submission import LetterEvidence, LobResponse, MetadataLookup, Uncertain
from parkingbreaker.mail_attempt_store import PreparedMailAttempt
from parkingbreaker.payment_records import PaymentClient
from parkingbreaker.payment_state import PaymentState
from parkingbreaker.research_records import decode_research, research_json
from parkingbreaker.transaction_store import SqliteTransactionStore
from test_beta_dispatch_timing import ForbiddenPayments
from test_case_service import Clock, approve_with_preview, drafting_case, fake_resolution, stripe
from test_effect_dispatch import payment_fixture
from test_tier_acceptance import extract_case, model_answers

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("fake_resolution")]
assert fake_resolution


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.parametrize(
    "damage",
    [None, "old_run", "old_source", "future_run", "future_source", "reversed", "empty", "naive"],
)
def test_evidence_freshness(damage: str | None) -> None:
    result, _, _ = extract_case(model_answers())
    now = result.finished_at
    if damage == "old_run":
        result = replace(result, started_at=now - timedelta(hours=24))
    elif damage == "old_source":
        result = replace(
            result,
            sources=(
                replace(result.sources[0], retrieved_at=now - timedelta(hours=24)),
                *result.sources[1:],
            ),
        )
    elif damage == "future_run":
        result = replace(result, finished_at=now + timedelta(seconds=1))
    elif damage == "future_source":
        result = replace(
            result,
            sources=(
                replace(result.sources[0], retrieved_at=now + timedelta(seconds=1)),
                *result.sources[1:],
            ),
        )
    elif damage == "reversed":
        result = replace(result, started_at=now + timedelta(seconds=1))
    elif damage == "empty":
        result = replace(result, sources=())
    elif damage == "naive":
        now = now.replace(tzinfo=None)
    assert result.evidence_current_at(now) is (damage is None)
    if damage is None:
        assert result.evidence_current_at(now + timedelta(hours=24, microseconds=-1))
        assert not result.evidence_current_at(now + timedelta(hours=24))


@pytest.mark.parametrize("prior_intent", [False, True])
@pytest.mark.parametrize("free_beta", [False, True])
async def test_stale_research_prevents_create_but_preserves_original_lookup_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prior_intent: bool, free_beta: bool
) -> None:
    client, real, case = await drafting_case(tmp_path, monkeypatch, free_beta=free_beta)
    try:
        real.step(case, s.WorkerStep(step_id="draft"))
        draft = real.draft(case)
        adoption = real.adopt_text(
            case, s.AdoptText(text=draft.text, text_sha256=draft.text_sha256)
        )
        real.approve(case, approve_with_preview(real, case, adoption.adopted_sha256))
        record = real._intake(case)
        assert record.case_token is not None
        token = record.case_token
        prepared = real._store.get(token, 1)
        manifest = real._store.get_approval_manifest(token, 1)
        assert prepared is not None
        payments, transport = stripe(payment_fixture(), payment_fixture("canceled"))
        lob = FakeLobClient(
            [
                LobResponse(None)
                if prior_intent
                else LobResponse(200, "must_not_create", evidence=LetterEvidence.PRODUCTION)
            ]
        )
        clock = Clock()
        object.__setattr__(
            real._deps,
            "payments",
            cast(PaymentClient, ForbiddenPayments()) if free_beta else payments,
        )
        object.__setattr__(real._deps, "lob", lob)
        object.__setattr__(real._deps, "clock", clock)
        if prior_intent:
            real.dispatch(case)
            assert len(lob.calls) == 1
            cursor = real._store.submission_cursor(token, 1)
            assert cursor is not None
            assert isinstance(cursor.outcome, Uncertain)
            assert cursor.next_at is not None
            clock.now = cursor.next_at
        saved = real._store.get_research(case)
        assert saved is not None
        result = decode_research(saved.research_json)
        old = clock() - timedelta(days=2)
        result = replace(
            result,
            started_at=old,
            finished_at=old,
            sources=tuple(replace(source, retrieved_at=old) for source in result.sources),
        )
        real._store.save_research(
            case, research_json(result), saved.answers_json, clock(), saved.revision
        )
        # A new service/store must enforce freshness too, independently of process memory.
        real._store.close()
        real = CaseService(
            replace(real._deps, store=SqliteTransactionStore(tmp_path / "pb.sqlite"))
        )
        original_lookup = lob.find_by_metadata

        def unavailable(submission_token: str) -> MetadataLookup:
            lob.lookups.append(submission_token)
            return MetadataLookup(authoritative=False)

        monkeypatch.setattr(lob, "find_by_metadata", unavailable)
        for _ in range(5):
            real.dispatch(case)
            cursor = real._store.submission_cursor(token, 1)
            if cursor is not None and cursor.next_at is not None:
                clock.now = cursor.next_at
        assert real._store.get(token, 1) == prepared
        assert real._store.get_approval_manifest(token, 1) == manifest
        assert len(lob.calls) == int(prior_intent)
        assert not any(call[1].endswith("/capture") for call in transport.calls)
        if prior_intent:
            assert real.status(case).state is CaseState.SUBMITTING
            assert real.status(case).payment_state is (
                PaymentState.NO_CHARGE if free_beta else PaymentState.AUTHORIZED
            )
            assert not any(call[1].endswith("/cancel") for call in transport.calls)
            # The existing free path switches from same-key retries to lookups
            # at its conservative idempotency cutoff, independently of money timers.
            clock.now += timedelta(days=1)
            real.dispatch(case)
            assert lob.lookups
            cursor = real._store.submission_cursor(token, 1)
            assert cursor is not None
            assert cursor.next_at is not None
            clock.now = cursor.next_at
            # Authoritative evidence for the original still reconciles despite aged sources.
            lob.letters[prepared.request.submission_token] = "ltr_original"
            monkeypatch.setattr(lob, "find_by_metadata", original_lookup)
            real.dispatch(case)
            assert real.status(case).state is CaseState.IN_TRANSIT
            assert len(lob.calls) == 1
        else:
            assert real.status(case).state is CaseState.STOPPED_NO_CHARGE
            assert real.status(case).payment_state is (
                PaymentState.NO_CHARGE if free_beta else PaymentState.RELEASED
            )
        if free_beta:
            assert not transport.calls
    finally:
        await client.aclose()
        real._store.close()


async def test_freshness_is_checked_after_packet_validation_at_the_create_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, real, case = await drafting_case(tmp_path, monkeypatch, free_beta=True)
    try:
        real.step(case, s.WorkerStep(step_id="draft"))
        draft = real.draft(case)
        adoption = real.adopt_text(
            case, s.AdoptText(text=draft.text, text_sha256=draft.text_sha256)
        )
        real.approve(case, approve_with_preview(real, case, adoption.adopted_sha256))
        clock = Clock()
        lob = FakeLobClient([LobResponse(200, "must_not_create")])
        object.__setattr__(real._deps, "clock", clock)
        object.__setattr__(real._deps, "lob", lob)
        object.__setattr__(real._deps, "payments", cast(PaymentClient, ForbiddenPayments()))
        saved = real._store.get_research(case)
        assert saved is not None
        result = decode_research(saved.research_json)
        almost_old = clock() - timedelta(hours=24) + timedelta(seconds=1)
        result = replace(
            result,
            started_at=almost_old,
            finished_at=almost_old,
            sources=tuple(replace(source, retrieved_at=almost_old) for source in result.sources),
        )
        assert result.evidence_current_at(clock())
        real._store.save_research(
            case, research_json(result), saved.answers_json, clock(), saved.revision
        )
        validate = real._validate_prepared_packet

        def slow_validation(prepared: PreparedMailAttempt) -> None:
            validate(prepared)
            clock.now += timedelta(seconds=2)

        monkeypatch.setattr(real, "_validate_prepared_packet", slow_validation)
        real.dispatch(case)
        assert not lob.calls
        assert real.status(case).state is CaseState.STOPPED_NO_CHARGE
        assert real.status(case).payment_state is PaymentState.NO_CHARGE
    finally:
        await client.aclose()
        real._store.close()
