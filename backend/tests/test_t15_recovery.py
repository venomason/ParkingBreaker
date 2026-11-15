"""T15 restart contracts over the real service and durable SQLite records.

Provider seams reuse the existing offline fakes. Faults are injected only after
the real store/service operation commits, and recovery opens a fresh service.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from contextlib import suppress
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from httpx2 import AsyncClient

from parkingbreaker import case_service
from parkingbreaker.api import schemas as s
from parkingbreaker.api.service import ServiceConflict
from parkingbreaker.appeal.drafting import Draft, DraftSentence
from parkingbreaker.appeal.fakes import FakeDrafter
from parkingbreaker.case_service import CaseService
from parkingbreaker.case_state import CaseState, CustomerAnswered, Event
from parkingbreaker.intake_store import SavedDraft
from parkingbreaker.lob_fakes import FakeLobClient
from parkingbreaker.lob_submission import LetterEvidence, LobResponse
from parkingbreaker.payment_fakes import FakePaymentClient
from parkingbreaker.research.questions import FactKey
from parkingbreaker.research.resolution import VehicleRelationship
from parkingbreaker.research.run import ResearchResult
from parkingbreaker.research_records import decode_answers
from parkingbreaker.session_window import DEFAULT_WINDOW
from parkingbreaker.transaction_store import SqliteTransactionStore
from test_case_service import (
    INTAKE,
    NOW,
    PNG_B64,
    Clock,
    approve_with_preview,
    drafting_case,
    researched_case,
    service,
)
from test_case_service import (
    anyio_backend as anyio_backend,
)
from test_case_service import (
    fake_resolution as fake_resolution,
)
from test_tier_acceptance import extract_case, model_answers, policy_page, requirement

pytestmark = pytest.mark.anyio
ANSWER = s.AnswerQuestion(
    question_id="VEHICLE_RELATIONSHIP", answer=VehicleRelationship.REGISTERED_OWNER
)


class InjectedCrash(RuntimeError):
    """An interrupted process at a known committed boundary."""


class RetryBudgetExceeded(BaseException):
    """Stop an accidental retry loop even if it catches ordinary exceptions."""


def _reopen(real: CaseService, path: Path) -> CaseService:
    # Retain external fake state, the clock and the unique-id stream, never the
    # old connection or a monkeypatched bound method.
    real._store.close()
    return CaseService(replace(real._deps, store=SqliteTransactionStore(path)))


def _history(real: CaseService, case: str) -> tuple[tuple[str, str, str, str, str], ...]:
    token = real._intake(case).case_token
    assert token is not None
    return tuple(
        (row.machine, row.source, row.target, row.event_name, row.event_json)
        for row in real._store.transition_history(token)
    )


def _events(real: CaseService, case: str) -> list[str]:
    return [row[3] for row in _history(real, case) if row[0] == "letter"]


def _inputs(real: CaseService, case: str) -> tuple[object, ...]:
    record = real._intake(case)
    return (
        record,
        real._store.get_workspace(record.workspace_token),
        real._store.workspace_history(record.workspace_token),
    )


def _count_research(monkeypatch: pytest.MonkeyPatch, result: ResearchResult) -> list[int]:
    calls: list[int] = []

    def run(*_args: object, **_kwargs: object) -> ResearchResult:
        calls.append(1)
        return result

    monkeypatch.setattr(case_service, "run_research", run)
    return calls


def _question_research() -> ResearchResult:
    quote = "Only the registered owner may contest."
    findings = [f for f in model_answers() if f["key"] != FactKey.CONTEST_ELIGIBILITY]
    findings.append(requirement(FactKey.CONTEST_ELIGIBILITY, quote))
    result, _llm, _searcher = extract_case(findings, policy=policy_page(quote))
    return result


def _crash_after_draft_save(real: CaseService, monkeypatch: pytest.MonkeyPatch) -> None:
    original = real._store.save_draft

    def save(
        case: str,
        draft: SavedDraft,
        revision: int | None,
        *,
        expected_research_revision: int | None = None,
    ) -> SavedDraft:
        original(case, draft, revision, expected_research_revision=expected_research_revision)
        raise InjectedCrash("after draft commit")

    monkeypatch.setattr(real._store, "save_draft", save)


async def test_research_commit_crash_resolves_persisted_research_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _question_research()
    control_path = tmp_path / "control"
    control_path.mkdir()
    control_client, control, control_case = await researched_case(control_path, monkeypatch, result)
    client, real, case = await researched_case(tmp_path, monkeypatch, result)
    restarted: CaseService | None = None
    try:
        expected = control.step(control_case, s.WorkerStep(step_id="research"))
        assert expected.state is CaseState.NEEDS_CUSTOMER
        calls = _count_research(monkeypatch, result)
        before = _inputs(real, case)

        def crash(*_args: object, **_kwargs: object) -> None:
            assert real._store.get_research(case) is not None
            raise InjectedCrash("after research commit, before resolution")

        monkeypatch.setattr(real, "_resolve", crash)
        with pytest.raises(InjectedCrash, match="after research commit"):
            real.step(case, s.WorkerStep(step_id="research"))
        saved = real._store.get_research(case)
        assert saved is not None
        assert real.status(case).state is CaseState.RESEARCHING
        assert "CustomerNeeded" not in _events(real, case)
        restarted = _reopen(real, tmp_path / "pb.sqlite")
        assert restarted._store.get_research(case) == saved

        for _ in range(2):
            recovered = restarted.step(case, s.WorkerStep(step_id="research"))
            assert calls == [1], "restart must consume saved evidence without another provider call"
            assert _inputs(restarted, case) == before
            assert recovered.state is expected.state
            assert recovered.question == expected.question
            assert _history(restarted, case) == _history(control, control_case)
        assert _events(restarted, case).count("CustomerNeeded") == 1
    finally:
        await client.aclose()
        await control_client.aclose()
        (restarted or real)._store.close()
        control._store.close()


async def test_customer_answer_commit_crash_resolves_saved_answer_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _question_research()
    control_path = tmp_path / "control"
    control_path.mkdir()
    control_client, control, control_case = await researched_case(control_path, monkeypatch, result)
    client, real, case = await researched_case(tmp_path, monkeypatch, result)
    restarted: CaseService | None = None
    try:
        assert (
            control.step(control_case, s.WorkerStep(step_id="research")).state
            is CaseState.NEEDS_CUSTOMER
        )
        expected = control.answer_question(control_case, ANSWER)
        assert expected.state is CaseState.DRAFTING
        calls = _count_research(monkeypatch, result)
        assert real.step(case, s.WorkerStep(step_id="research")).state is CaseState.NEEDS_CUSTOMER
        before = _inputs(real, case)
        original = real._apply

        def apply(token: str, event: Event) -> None:
            original(token, event)
            if isinstance(event, CustomerAnswered):
                raise InjectedCrash("after CustomerAnswered commit")

        monkeypatch.setattr(real, "_apply", apply)
        with pytest.raises(InjectedCrash, match="after CustomerAnswered commit"):
            real.answer_question(case, ANSWER)
        saved = real._store.get_research(case)
        assert saved is not None
        assert decode_answers(saved.answers_json).vehicle_relationship == ANSWER.answer
        assert real.status(case).state is CaseState.RESEARCHING
        assert _events(real, case).count("CustomerAnswered") == 1
        restarted = _reopen(real, tmp_path / "pb.sqlite")
        assert restarted._store.get_research(case) == saved

        for _ in range(2):
            recovered = restarted.step(case, s.WorkerStep(step_id="research"))
            assert calls == [1]
            assert _inputs(restarted, case) == before
            assert restarted._store.get_research(case) == saved
            assert recovered.state is expected.state
            assert _history(restarted, case) == _history(control, control_case)
        assert _events(restarted, case).count("CustomerAnswered") == 1
        assert _events(restarted, case).count("ResearchComplete") == 1
    finally:
        await client.aclose()
        await control_client.aclose()
        (restarted or real)._store.close()
        control._store.close()


@pytest.mark.parametrize("needs_review", [False, True], ids=["ready", "customer-review"])
async def test_draft_commit_crash_reuses_saved_text_flags_and_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, needs_review: bool
) -> None:
    client, real, case = await drafting_case(tmp_path, monkeypatch)
    restarted: CaseService | None = None
    try:
        statement = str(INTAKE["customer_position"]["statement"])
        support = () if needs_review else (f"note:0-{len(statement)}",)
        drafter = FakeDrafter(lambda *_: Draft((DraftSentence(statement, support),)))
        object.__setattr__(real._deps, "drafter", drafter)
        before = _inputs(real, case)
        _crash_after_draft_save(real, monkeypatch)
        with pytest.raises(InjectedCrash, match="after draft commit"):
            real.step(case, s.WorkerStep(step_id="draft"))
        saved = real._store.get_draft(case)
        assert saved is not None
        assert saved.letter_text
        assert bool(real.draft(case).flags) is needs_review
        assert real.status(case).state is CaseState.DRAFTING
        assert len(drafter.calls) == (3 if needs_review else 1)
        provider_calls = list(drafter.calls)
        restarted = _reopen(real, tmp_path / "pb.sqlite")

        for _ in range(2):
            assert (
                restarted.step(case, s.WorkerStep(step_id="draft")).state
                is CaseState.AWAITING_ADOPTION
            )
            assert restarted._store.get_draft(case) == saved
            assert drafter.calls == provider_calls
            assert _inputs(restarted, case) == before
        assert _events(restarted, case).count("DraftReady") == 1
        if needs_review:
            draft = restarted.draft(case)
            with pytest.raises(ServiceConflict, match=r"^draft_needs_review$"):
                restarted.adopt_text(
                    case, s.AdoptText(text=draft.text, text_sha256=draft.text_sha256)
                )
    finally:
        await client.aclose()
        (restarted or real)._store.close()


@pytest.mark.parametrize("empty", [False, True], ids=["late-text", "empty-before-provider"])
async def test_expired_draft_commit_crash_cannot_become_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, empty: bool
) -> None:
    client, real, case = await drafting_case(tmp_path, monkeypatch)
    restarted: CaseService | None = None
    try:
        clock = Clock()
        object.__setattr__(real._deps, "clock", clock)
        statement = str(INTAKE["customer_position"]["statement"])

        def late_draft(*_args: object) -> Draft:
            clock.now = NOW + DEFAULT_WINDOW + timedelta(minutes=1)
            return Draft((DraftSentence(statement, (f"note:0-{len(statement)}",)),))

        drafter = FakeDrafter(late_draft)
        object.__setattr__(real._deps, "drafter", drafter)
        if empty:
            original_refresh = real.refresh

            def refresh(case_id: str) -> None:
                original_refresh(case_id)
                # Expiry between the entry check and run_drafting's first check
                # legitimately saves WINDOW_EXPIRED with no spans/provider call.
                clock.now = NOW + DEFAULT_WINDOW + timedelta(minutes=1)

            monkeypatch.setattr(real, "refresh", refresh)
        before = _inputs(real, case)
        _crash_after_draft_save(real, monkeypatch)
        with pytest.raises(InjectedCrash, match="after draft commit"):
            real.step(case, s.WorkerStep(step_id="draft"))
        saved = real._store.get_draft(case)
        assert saved is not None
        assert bool(saved.letter_text) is not empty
        assert len(drafter.calls) == (0 if empty else 1)
        assert real.status(case).state is CaseState.DRAFTING
        restarted = _reopen(real, tmp_path / "pb.sqlite")

        for _ in range(2):
            status = restarted.step(case, s.WorkerStep(step_id="draft"))
            assert status.state is CaseState.STOPPED_NO_CHARGE
            assert restarted._store.get_draft(case) == saved
            assert _inputs(restarted, case) == before
        assert _events(restarted, case).count("WindowExpired") == 1
        assert "DraftReady" not in _events(restarted, case)
        assert len(drafter.calls) == (0 if empty else 1)
    finally:
        await client.aclose()
        (restarted or real)._store.close()


def _add_drafting_case(real: CaseService, suffix: str) -> str:
    # Each customer owns one workspace. Separate customers share the real store
    # and scheduler, while each authorization has a distinct original reference.
    workspace = real.create_workspace(f"t15-{suffix}", s.CreateWorkspace())
    photo = real.upload_photo(
        workspace.workspace_id, s.PhotoUpload(content_type="image/png", data_base64=PNG_B64)
    )
    body = copy.deepcopy(INTAKE)
    body["photo_refs"] = [photo.photo_ref]
    body["citation_photo_ref"] = photo.photo_ref
    receipt = real.submit(workspace.workspace_id, s.SubmitIntake.model_validate(body))
    case = receipt.case_id
    object.__setattr__(real._deps, "payments", FakePaymentClient(f"auth_{suffix}"))
    real.choose(case, s.ChooseTier(tier=s.MailTier.CERTIFIED))
    authorized = real.authorize(
        case, s.StartAuthorization(terms_accepted=True, payment_method_id="pm_card_visa")
    )
    assert authorized.status == "authorized"
    status = real.step(case, s.WorkerStep(step_id="research"))
    if status.state is CaseState.NEEDS_CUSTOMER:
        status = real.answer_question(case, ANSWER)
    assert status.state is CaseState.DRAFTING
    return case


def _fail_draft_for(
    real: CaseService, monkeypatch: pytest.MonkeyPatch, case: str, failure: Callable[[], None]
) -> list[str]:
    original = real._draft_step
    calls: list[str] = []

    def draft(case_id: str) -> None:
        calls.append(case_id)
        if case_id == case:
            failure()
        original(case_id)

    monkeypatch.setattr(real, "_draft_step", draft)
    return calls


def _conflict() -> None:
    raise ServiceConflict("mailing_address_not_confirmed")


async def _scheduled_cases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[AsyncClient, CaseService, str, str, str, FakeLobClient]:
    client, real, case_a = await drafting_case(tmp_path, monkeypatch)
    clock = Clock()
    object.__setattr__(real._deps, "clock", clock)
    clock.now += timedelta(seconds=1)
    case_b = _add_drafting_case(real, "B")
    clock.now += timedelta(seconds=1)
    mailing_case = _add_drafting_case(real, "MAIL")
    assert (
        real.step(mailing_case, s.WorkerStep(step_id="draft")).state is CaseState.AWAITING_ADOPTION
    )
    draft = real.draft(mailing_case)
    real.adopt_text(mailing_case, s.AdoptText(text=draft.text, text_sha256=draft.text_sha256))
    approval = approve_with_preview(real, mailing_case, draft.text_sha256)
    assert real.approve(mailing_case, approval).state is CaseState.FROZEN
    lob = FakeLobClient(
        [LobResponse(200, "ltr_t15_independent", evidence=LetterEvidence.PRODUCTION)]
    )
    object.__setattr__(real._deps, "lob", lob)
    assert real._store.intakes_in_states((CaseState.DRAFTING.value,)) == (case_a, case_b)
    return client, real, case_a, case_b, mailing_case, lob


async def test_scheduler_no_fault_control_advances_cases_and_calls_lob(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, real, case_a, case_b, mailing_case, lob = await _scheduled_cases(tmp_path, monkeypatch)
    try:
        real.advance_due()
        assert real.status(case_a).state is CaseState.AWAITING_ADOPTION
        assert real.status(case_b).state is CaseState.AWAITING_ADOPTION
        assert real.status(mailing_case).state is CaseState.IN_TRANSIT
        assert len(lob.calls) == 1
    finally:
        await client.aclose()
        real._store.close()


async def test_scheduler_isolates_failed_case_and_still_processes_b_and_dispatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, real, case_a, case_b, mailing_case, lob = await _scheduled_cases(tmp_path, monkeypatch)
    try:
        before = _inputs(real, case_a)
        saved_research = real._store.get_research(case_a)
        history = _history(real, case_a)
        calls = _fail_draft_for(real, monkeypatch, case_a, _conflict)

        escaped: str | None = None
        try:
            real.advance_due()
        except ServiceConflict as exc:
            escaped = str(exc)
        assert _inputs(real, case_a) == before
        assert real._store.get_research(case_a) == saved_research
        assert _history(real, case_a) == history
        assert real._store.get_draft(case_a) is None
        assert {
            "escaped": escaped,
            "attempted": calls,
            "case_b": real.status(case_b).state,
            "lob_calls": len(lob.calls),
            "mailing_case": real.status(mailing_case).state,
        } == {
            "escaped": None,
            "attempted": [case_a, case_b],
            "case_b": CaseState.AWAITING_ADOPTION,
            "lob_calls": 1,
            "mailing_case": CaseState.IN_TRANSIT,
        }
    finally:
        await client.aclose()
        real._store.close()


async def test_scheduler_result_exposes_failed_case_instead_of_only_a_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, real, case = await drafting_case(tmp_path, monkeypatch)
    try:
        object.__setattr__(real._deps, "lob", FakeLobClient())
        before = _inputs(real, case)
        _fail_draft_for(real, monkeypatch, case, _conflict)
        result: object = None
        with suppress(ServiceConflict):
            result = real.advance_due()
        assert _inputs(real, case) == before
        # There is currently no per-case logging/recording seam in advance_due.
        # Require its result to expose the case and stable reason, rather than
        # silently treating this failed step as successfully processed.
        failures = getattr(result, "failures", ())
        assert failures, "advance_due must expose per-case failures in its sweep result"
        assert case in str(failures)
        assert "mailing_address_not_confirmed" in str(failures)
    finally:
        await client.aclose()
        real._store.close()


def test_public_dispatch_without_lob_still_fails_closed(tmp_path: Path) -> None:
    real = service(tmp_path / "pb.sqlite")
    try:
        assert real._deps.lob is None
        with pytest.raises(ServiceConflict, match=r"^mail_not_configured$"):
            real.dispatch()
        with pytest.raises(ServiceConflict, match=r"^mail_not_configured$"):
            real.advance_due()
    finally:
        real._store.close()


async def test_repeated_case_failures_are_bounded_per_sweep_and_preserve_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, real, case = await drafting_case(tmp_path, monkeypatch)
    try:
        object.__setattr__(real._deps, "lob", FakeLobClient())
        before = _inputs(real, case)
        saved_research = real._store.get_research(case)
        history = _history(real, case)
        attempts = 0

        def fail() -> None:
            nonlocal attempts
            attempts += 1
            if attempts > 3:
                raise RetryBudgetExceeded("one sweep exceeded the three-attempt bound")
            _conflict()

        _fail_draft_for(real, monkeypatch, case, fail)
        for _ in range(3):
            attempts = 0
            # Isolation/observability are asserted separately above.
            with suppress(ServiceConflict):
                real.advance_due()
            assert 1 <= attempts <= 3
            assert _inputs(real, case) == before
            assert real._store.get_research(case) == saved_research
            assert _history(real, case) == history
            assert real._store.get_draft(case) is None
    finally:
        await client.aclose()
        real._store.close()
