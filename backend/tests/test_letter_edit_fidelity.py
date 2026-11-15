"""Editing preserves customer content and cannot adopt surviving unsupported AI words."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from httpx2 import AsyncClient
from pydantic import TypeAdapter

from parkingbreaker.api import schemas as s
from parkingbreaker.appeal.correction import (
    Flag,
    SpanAuthor,
    TextSpan,
    check_spans,
    edited_spans,
    flags_after_edit,
    flags_from_problems,
)
from parkingbreaker.appeal.drafting import Draft, DraftSentence, TicketFacts
from parkingbreaker.appeal.fakes import FakeDrafter
from parkingbreaker.appeal.proofread import proofread
from parkingbreaker.case_service import CaseService
from test_case_service import ALICE, INTAKE, NOW, drafting_case, service
from test_case_service import fake_resolution as fake_resolution

pytestmark = pytest.mark.usefixtures("fake_resolution")
FACTS = TicketFacts("SYN-998877", "SYN123", date(2026, 10, 1), "SYN-METER", "Meter expired")
NOTES = "I parked there."
ASSERTION = "I paid the meter."
SPANS = TypeAdapter(tuple[TextSpan, ...])
FLAGS = TypeAdapter(tuple[Flag, ...])


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def flagged(text: str, notes: str = NOTES) -> tuple[tuple[TextSpan, ...], tuple[Flag, ...]]:
    spans = (TextSpan("original", text, SpanAuthor.AI, (f"note:0-{len(notes)}",)),)
    flags = flags_from_problems(
        spans,
        proofread(
            Draft((DraftSentence(text, spans[0].support),)),
            notes,
            (),
            FACTS,
            require_all_notes=False,
        ),
    )
    assert flags
    return spans, flags


@pytest.mark.parametrize(
    "text",
    [
        "I  paid the meter.",
        "i paid the meter.",
        "I paid the meter!",
        "I paid\nthe meter.",
        "I paid the meter",
        "I,paid;the:meter",
    ],
)
def test_cosmetic_edit_preserves_unsupported_warning(text: str) -> None:
    spans, flags = flagged(ASSERTION)
    edited = edited_spans(ASSERTION, text, spans, "r1")
    assert "".join(span.text for span in edited) == text
    assert flags_after_edit(flags, edited, text, NOTES)


@pytest.mark.parametrize("separator", [". ", "\n", "\n\n"])
def test_editing_one_sentence_keeps_unchanged_unsupported_sibling(separator: str) -> None:
    old = "I parked there" + separator + ASSERTION
    new = "I parked near the kiosk. " + ASSERTION
    spans, flags = flagged(old)
    edited = edited_spans(old, new, spans, "r1")
    assert flags_after_edit(flags, edited, new, NOTES)


def test_persisted_overlap_keeps_unsupported_origin_until_removed() -> None:
    notes = "I paid."
    spans: tuple[TextSpan, ...] = (
        TextSpan("supported", notes, SpanAuthor.AI, ("note:0-7",)),
        TextSpan("unsupported", "paid the meter.", SpanAuthor.AI, ("note:0-7",)),
    )
    flags = flags_from_problems(spans, check_spans(spans, notes, (), FACTS))
    assert flags
    old = "I paid. paid the meter."
    for revision, new in enumerate(
        ["I paid the meter.", "Paid the meter!", "Please review. paid the meter?"], start=1
    ):
        spans = edited_spans(old, new, spans, f"r{revision}")
        flags = flags_after_edit(flags, spans, new, notes)
        assert flags
        assert "".join(span.text for span in spans) == new
        spans = SPANS.validate_json(SPANS.dump_json(spans))
        flags = FLAGS.validate_json(FLAGS.dump_json(flags))
        old = new
    new = "My own updated explanation."
    edited = edited_spans(old, new, spans, "r4")
    assert not flags_after_edit(flags, edited, new, notes)


def test_rewriting_unsupported_sibling_frees_supported_survivor() -> None:
    old = NOTES + " " + ASSERTION
    spans, flags = flagged(old)
    new = NOTES + " The kiosk display was blank."
    edited = edited_spans(old, new, spans, "r1")
    assert not flags_after_edit(flags, edited, new, NOTES)


def test_missing_originals_need_restoration_or_explicit_replacement_without_ai() -> None:
    first = "The sign was covered."
    notes = first + " The meter was broken."
    spans = (TextSpan("original", first, SpanAuthor.AI, (f"note:0-{len(first)}",)),)
    flags = flags_from_problems(spans, check_spans(spans, notes, (), FACTS))
    replacement = "My complete new customer statement."
    edited = edited_spans(first, replacement, spans, "r1")
    assert all(span.author is SpanAuthor.CUSTOMER for span in edited)
    assert flags_after_edit(flags, edited, replacement, notes)
    assert not flags_after_edit(flags, edited, replacement, notes, replace_customer_content=True)
    assert not flags_after_edit(flags, edited, notes, notes)


def test_restoring_original_order_clears_obsolete_global_warning() -> None:
    first, second = "First I read the sign.", "Then I parked."
    notes = first + " " + second
    start = len(first) + 1
    spans = (
        TextSpan("second", second, SpanAuthor.AI, (f"note:{start}-{len(notes)}",)),
        TextSpan("first", first, SpanAuthor.AI, (f"note:0-{len(first)}",)),
    )
    flags = flags_from_problems(spans, check_spans(spans, notes, (), FACTS))
    assert any("customer order changed" in detail for flag in flags for detail in flag.details)
    edited = edited_spans(second + " " + first, notes, spans, "r1")
    assert not flags_after_edit(flags, edited, notes, notes)


async def omitted_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsupported: bool
) -> tuple[AsyncClient, CaseService, str, s.Draft]:
    client, real, case = await drafting_case(tmp_path, monkeypatch)
    record = real._intake(case)
    workspace = real._store.get_workspace(record.workspace_token)
    assert workspace is not None
    extra = "The sign was covered."
    real._store.save_workspace(
        record.workspace_token,
        replace(workspace.workspace, notes=extra),
        NOW,
        expected_revision=workspace.revision,
    )
    statement = INTAKE["customer_position"]["statement"]
    start = len(extra) + 2
    text = statement + (" " + ASSERTION if unsupported else "")
    object.__setattr__(
        real._deps,
        "drafter",
        FakeDrafter(
            lambda *_: Draft((DraftSentence(text, (f"note:{start}-{start + len(statement)}",)),))
        ),
    )
    real.step(case, s.WorkerStep(step_id="draft"))
    draft = real.draft(case)
    assert any("omitted customer text" in detail for flag in draft.flags for detail in flag.details)
    return client, real, case, draft


@pytest.mark.anyio
@pytest.mark.parametrize("unsupported", [False, True])
async def test_same_text_explicit_replacement_only_resolves_omission_through_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsupported: bool
) -> None:
    client, _real, case, draft = await omitted_case(tmp_path, monkeypatch, unsupported)
    try:
        text = (
            draft.text.replace(ASSERTION, "i paid the meter!")
            if unsupported
            else "My complete new customer statement."
        )
        response = await client.put(
            f"/cases/{case}/draft",
            headers=ALICE,
            json={"text": text, "expected_sha256": draft.text_sha256},
        )
        assert response.status_code == 200, response.text
        edited = s.Draft.model_validate(response.json())
        assert any(
            "omitted customer text" in detail for flag in edited.flags for detail in flag.details
        )
        refused = await client.post(
            f"/cases/{case}/adoption",
            headers=ALICE,
            json={"text": text, "text_sha256": edited.text_sha256},
        )
        assert refused.status_code == 409
        assert refused.json()["detail"] == "draft_needs_review"
        response = await client.put(
            f"/cases/{case}/draft",
            headers=ALICE,
            json={
                "text": text,
                "expected_sha256": edited.text_sha256,
                "replace_customer_content": True,
            },
        )
        assert response.status_code == 200, response.text
        current = s.Draft.model_validate(response.json())
        assert current.revision > edited.revision
        assert not any(
            "omitted customer text" in detail for flag in current.flags for detail in flag.details
        )
        restarted = service(tmp_path / "pb.sqlite")
        assert bool(restarted.draft(case).flags) is unsupported
        adopted = await client.post(
            f"/cases/{case}/adoption",
            headers=ALICE,
            json={"text": text, "text_sha256": current.text_sha256},
        )
        if unsupported:
            assert adopted.status_code == 409
            assert adopted.json()["detail"] == "draft_needs_review"
        else:
            assert adopted.status_code == 200, adopted.text
            assert restarted.draft(case).adopted_sha256 == current.text_sha256
    finally:
        await client.aclose()
