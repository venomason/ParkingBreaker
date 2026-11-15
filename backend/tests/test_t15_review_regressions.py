"""T15 review regressions: paid-draft reuse, per-case storage isolation, answer race."""

from __future__ import annotations

from pathlib import Path

import pytest

from parkingbreaker import case_service
from parkingbreaker.api import schemas as s
from parkingbreaker.api.service import ServiceConflict
from parkingbreaker.appeal.letter import LetterError
from parkingbreaker.case_service import CaseService
from parkingbreaker.case_state import CaseState, CustomerAnswered, Event
from parkingbreaker.lob_fakes import FakeLobClient
from test_case_service import anyio_backend as anyio_backend
from test_case_service import drafting_case, researched_case
from test_case_service import fake_resolution as fake_resolution
from test_t15_recovery import (
    ANSWER,
    _events,
    _history,
    _question_research,
    _scheduled_cases,
)

pytestmark = pytest.mark.anyio


def _drafter_calls(real: CaseService) -> int:
    drafter = real._deps.drafter
    assert drafter is not None
    return len(drafter.calls)  # type: ignore[attr-defined]


async def test_unconfirmed_mailing_address_costs_no_paid_drafting_across_sweeps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, real, case = await drafting_case(tmp_path, monkeypatch)
    try:
        object.__setattr__(real._deps, "lob", FakeLobClient())

        def unconfirmed(_research: object) -> object:
            raise ServiceConflict("mailing_address_not_confirmed")

        monkeypatch.setattr(real, "_mailing_address", unconfirmed)
        for _ in range(5):
            result = real.advance_due()
            assert [(f.case_id, f.reason) for f in result.failures] == [
                (case, "mailing_address_not_confirmed")
            ]
        assert _drafter_calls(real) == 0
        assert real.status(case).state is CaseState.DRAFTING
    finally:
        await client.aclose()
        real._store.close()


async def test_post_draft_validation_failure_pays_for_drafting_once_and_never_becomes_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, real, case = await drafting_case(tmp_path, monkeypatch)
    try:
        object.__setattr__(real._deps, "lob", FakeLobClient())

        def unprintable(*_args: object, **_kwargs: object) -> object:
            raise LetterError("characters the letter cannot print")

        monkeypatch.setattr(case_service, "render_letter", unprintable)
        for _ in range(5):
            result = real.advance_due()
            assert [(f.case_id, f.reason) for f in result.failures] == [
                (case, "letter_not_printable")
            ]
            assert real.status(case).state is CaseState.DRAFTING
        assert _drafter_calls(real) == 1
        assert "DraftReady" not in _events(real, case)
        with pytest.raises(ServiceConflict, match=r"^draft_not_ready$"):
            real.draft(case)
    finally:
        await client.aclose()
        real._store.close()


async def test_normal_draft_path_pays_once_and_becomes_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, real, case = await drafting_case(tmp_path, monkeypatch)
    try:
        object.__setattr__(real._deps, "lob", FakeLobClient())
        for _ in range(3):
            assert not real.advance_due().failures
        assert real.status(case).state is CaseState.AWAITING_ADOPTION
        assert _drafter_calls(real) == 1
        assert _events(real, case).count("DraftReady") == 1
    finally:
        await client.aclose()
        real._store.close()


async def test_malformed_case_row_is_isolated_and_other_cases_still_advance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, real, case_a, case_b, mailing_case, lob = await _scheduled_cases(tmp_path, monkeypatch)
    try:
        token_a = real._intake(case_a).case_token
        database = real._store._require_open()
        database.execute("PRAGMA ignore_check_constraints=ON")
        database.execute("UPDATE pb_cases SET amount_cents=-1 WHERE case_token=?", (token_a,))
        database.commit()
        database.execute("PRAGMA ignore_check_constraints=OFF")

        result = real.advance_due()
        assert [(f.case_id, f.reason) for f in result.failures] == [(case_a, "storage_corruption")]
        assert real.status(case_b).state is CaseState.AWAITING_ADOPTION
        assert real.status(mailing_case).state is CaseState.IN_TRANSIT
        assert len(lob.calls) == 1
    finally:
        await client.aclose()
        real._store.close()


async def test_answer_race_with_scheduler_resolution_is_success_without_duplicate_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _question_research()
    client, real, case = await researched_case(tmp_path, monkeypatch, result)
    try:
        object.__setattr__(real._deps, "lob", FakeLobClient())
        assert real.step(case, s.WorkerStep(step_id="research")).state is CaseState.NEEDS_CUSTOMER
        original = real._apply
        swept: list[int] = []

        def apply(token: str, event: Event) -> None:
            original(token, event)
            if isinstance(event, CustomerAnswered) and not swept:
                swept.append(1)
                real.advance_due()

        monkeypatch.setattr(real, "_apply", apply)
        status = real.answer_question(case, ANSWER)
        assert swept == [1]
        assert status.state is CaseState.DRAFTING
        events = _events(real, case)
        assert events.count("CustomerAnswered") == 1
        assert events.count("ResearchComplete") == 1
        assert len(_history(real, case)) == len(set(_history(real, case)))
    finally:
        await client.aclose()
        real._store.close()
