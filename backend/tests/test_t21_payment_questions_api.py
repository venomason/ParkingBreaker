"""Real SQLite/API payment questions, including unknown, stale IDs and interrupted saves."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from parkingbreaker.api import schemas as s
from parkingbreaker.api.service import ServiceConflict
from parkingbreaker.case_state import CustomerAnswered, Event
from parkingbreaker.research.payment_effects import PaymentAction, PaymentCondition, PaymentEffect
from parkingbreaker.research.run import ResearchResult
from parkingbreaker.research_records import decode_answers
from samples import source
from test_case_service import ALICE, BOB, INTAKE, WORKER, researched_case
from test_case_service import anyio_backend as anyio_backend
from test_case_service import fake_resolution as fake_resolution
from test_t15_recovery import _reopen
from test_t21_payment_policy import DEPOSIT, HEADING, PLAN, rule
from test_t21_payment_records import payment_research as saved_payment_research

pytestmark = pytest.mark.anyio


def payment_research() -> ResearchResult:
    """The mocked researcher returns the date of the ticket actually submitted."""
    result = saved_payment_research()
    return replace(
        result,
        ticket=replace(
            result.ticket, issue_date=date.fromisoformat(INTAKE["ticket"]["issue_date"])
        ),
    )


@pytest.mark.parametrize("question", ["PAID_FINE", "PAID_HEARING_DEPOSIT", "ENTERED_PLAN"])
@pytest.mark.parametrize("answer", [True, False, None])
async def test_payment_question_answers_are_explicit_and_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, question: str, answer: bool | None
) -> None:
    research = payment_research()
    if question == "PAID_HEARING_DEPOSIT":
        current = rule(
            DEPOSIT,
            PaymentAction.HEARING_DEPOSIT,
            PaymentEffect.REQUIRED_DEPOSIT,
            PaymentCondition.ACTION_REQUIRED,
        )
        research = replace(
            research,
            payment_rules=(current,),
            sources=(*research.sources[:-1], source(HEADING + DEPOSIT)),
        )
    elif question == "ENTERED_PLAN":
        research = replace(
            research,
            payment_rules=(rule(PLAN, PaymentAction.PLAN_ENROLLMENT),),
            sources=(*research.sources[:-1], source(HEADING + PLAN)),
        )
    client, real, case = await researched_case(tmp_path, monkeypatch, research)
    try:
        response = await client.post(
            f"/internal/cases/{case}/step", headers=WORKER, json={"step_id": "research"}
        )
        assert response.status_code == 200, response.text
        status = response.json()
        assert status["state"] == "NEEDS_CUSTOMER", status
        assert status["question"]["question_id"] == question
        assert status["question"]["choices"] == [True, False, None]
        before = real._store.get_research(case)
        assert before is not None
        forbidden = await client.post(
            f"/cases/{case}/answers", headers=BOB, json={"question_id": question, "answer": answer}
        )
        assert forbidden.status_code == 404
        assert real._store.get_research(case) == before
        response = await client.post(
            f"/cases/{case}/answers",
            headers=ALICE,
            json={"question_id": question, "answer": answer},
        )
        assert response.status_code == 200, response.text
        state = response.json()["state"]
        proceeds = answer is True if question == "PAID_HEARING_DEPOSIT" else answer is False
        assert state == ("DRAFTING" if proceeds else "STOPPED_NO_CHARGE")
        if not proceeds:
            stop = response.json()["stop"]
            assert stop["no_charge"] is True
            assert "required items" not in stop["message"]
            assert "an item or step" not in stop["message"]
        saved = real._store.get_research(case)
        assert saved is not None
        answers = decode_answers(saved.answers_json)
        assert getattr(answers, question.lower()) is answer
        assert question in {item.value for item in answers.answered}
    finally:
        await client.aclose()
        real._store.close()


@pytest.mark.parametrize(
    "body",
    [
        {"question_id": "PAID_FINE", "answer": 1},
        {"question_id": "PAID_FINE", "answer": "false"},
        {"question_id": "PAID_FINE", "answer": "REGISTERED_OWNER"},
        {"question_id": "PAID_FINE"},
        {"question_id": "PAID_FINE", "answer": False, "free_text": "never accepted"},
        {"question_id": "VEHICLE_RELATIONSHIP", "answer": None},
    ],
)
async def test_question_schema_rejects_coercion_and_free_text_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: dict[str, object]
) -> None:
    client, real, case = await researched_case(tmp_path, monkeypatch, payment_research())
    try:
        real.step(case, s.WorkerStep(step_id="research"))
        before = real._store.get_research(case)
        response = await client.post(f"/cases/{case}/answers", headers=ALICE, json=body)
        assert response.status_code == 422
        assert real._store.get_research(case) == before
    finally:
        await client.aclose()
        real._store.close()


async def test_answer_must_match_the_recorded_open_question(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, real, case = await researched_case(tmp_path, monkeypatch, payment_research())
    try:
        real.step(case, s.WorkerStep(step_id="research"))
        before = real._store.get_research(case)
        for body in (
            {"question_id": "ENTERED_PLAN", "answer": False},
            {"question_id": "VEHICLE_RELATIONSHIP", "answer": "REGISTERED_OWNER"},
        ):
            response = await client.post(f"/cases/{case}/answers", headers=ALICE, json=body)
            assert response.status_code == 409
            assert real._store.get_research(case) == before
    finally:
        await client.aclose()
        real._store.close()


async def test_saved_answer_before_transition_keeps_same_question_and_refuses_a_changed_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, real, case = await researched_case(tmp_path, monkeypatch, payment_research())
    try:
        real.step(case, s.WorkerStep(step_id="research"))
        original = real._apply

        def crash(token: str, event: Event) -> None:
            if isinstance(event, CustomerAnswered):
                raise RuntimeError("injected before answer transition")
            original(token, event)

        monkeypatch.setattr(real, "_apply", crash)
        with pytest.raises(RuntimeError, match="injected before"):
            real.answer_question(
                case, s.PaymentAnswerQuestion(question_id="PAID_FINE", answer=False)
            )
        question = real.status(case).question
        assert question is not None
        assert question.question_id == "PAID_FINE"
        monkeypatch.setattr(real, "_apply", original)
        changed = await client.post(
            f"/cases/{case}/answers",
            headers=ALICE,
            json={"question_id": "PAID_FINE", "answer": True},
        )
        assert changed.status_code == 409
        replayed = await client.post(
            f"/cases/{case}/answers",
            headers=ALICE,
            json={"question_id": "PAID_FINE", "answer": False},
        )
        assert replayed.status_code == 200, replayed.text
        assert replayed.json()["state"] == "DRAFTING"
    finally:
        await client.aclose()
        real._store.close()


async def test_sequential_payment_questions_survive_reopen_and_reject_stale_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    research = payment_research()
    page = source(HEADING + PLAN, url="https://example.com/plan-rule")
    research = replace(
        research,
        payment_rules=(*research.payment_rules, rule(PLAN, PaymentAction.PLAN_ENROLLMENT)),
        sources=(*research.sources, page),
    )
    client, real, case = await researched_case(tmp_path, monkeypatch, research)
    try:
        real.step(case, s.WorkerStep(step_id="research"))
        real = _reopen(real, tmp_path / "pb.sqlite")
        question = real.status(case).question
        assert question is not None
        assert question.question_id == "PAID_FINE"
        fine_answer = s.PaymentAnswerQuestion(question_id="PAID_FINE", answer=False)
        second = real.answer_question(case, fine_answer)
        assert second.question is not None
        assert second.question.question_id == "ENTERED_PLAN"
        real = _reopen(real, tmp_path / "pb.sqlite")
        question = real.status(case).question
        assert question is not None
        assert question.question_id == "ENTERED_PLAN"
        before = real._store.get_research(case)
        with pytest.raises(ServiceConflict, match="question_mismatch"):
            real.answer_question(case, fine_answer)
        assert real._store.get_research(case) == before
        final = real.answer_question(
            case, s.PaymentAnswerQuestion(question_id="ENTERED_PLAN", answer=False)
        )
        assert final.state.value == "DRAFTING"
    finally:
        await client.aclose()
        real._store.close()
