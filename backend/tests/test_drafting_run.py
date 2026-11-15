from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime

import pytest

from parkingbreaker.agencies import ApprovedCapability, ResolvedAgency, ServiceMode
from parkingbreaker.appeal.correction import FlagKind, SpanAuthor
from parkingbreaker.appeal.drafting import Draft, DraftSentence, TicketFacts
from parkingbreaker.appeal.fakes import FakeDrafter
from parkingbreaker.appeal.run import DraftingStatus, run_drafting
from parkingbreaker.llm import Usage
from parkingbreaker.mode_gate import Operation, mode_permission
from parkingbreaker.service_policy import ServiceControlCategory, ServiceControlEntry
from parkingbreaker.session_window import DEFAULT_WINDOW
from samples import SAN_FRANCISCO, TEST_BASIS

NOW = datetime(2026, 10, 11, 12, tzinfo=UTC)
NOTES = "The meter was broken."
FACTS = TicketFacts("C123", "ABC123", date(2026, 10, 1), "V1", "Expired meter")
CLEAN = Draft((DraftSentence(NOTES, (f"note:0-{len(NOTES)}",), 2),))
ACTIVE = SAN_FRANCISCO


def test_missing_state_basis_cannot_draft() -> None:
    drafter = FakeDrafter(lambda *_: CLEAN)
    result = run_drafting(
        NOTES,
        (),
        FACTS,
        drafter,
        lambda: NOW,
        NOW,
        lambda: ACTIVE,
        new_revision=lambda: "draft-1",
        get_state_basis=lambda: {},
    )
    assert result.status is DraftingStatus.MODE_BLOCKED
    assert result.attempts == 0
    assert drafter.calls == []


def test_expanded_basis_cannot_enable_unimplemented_capability() -> None:
    basis = replace(
        TEST_BASIS,
        service_mode=ServiceMode.EXPANDED,
        approved_capabilities=(ApprovedCapability("recommend_defense", "test basis"),),
    )
    assert mode_permission(ACTIVE, Operation.DRAFT, state_basis={"CA": basis}).allowed
    assert not mode_permission(
        ACTIVE, Operation.DRAFT, "recommend_defense", state_basis={"CA": basis}
    ).allowed


def test_clean_first_draft_preserves_content_and_usage() -> None:
    drafter = FakeDrafter(lambda *_: CLEAN, Usage(10, 20, 1))
    result = run_drafting(
        NOTES,
        (),
        FACTS,
        drafter,
        lambda: NOW,
        NOW,
        lambda: ACTIVE,
        get_state_basis=lambda: {"CA": TEST_BASIS},
        new_revision=lambda: "draft-1",
    )
    assert result.status is DraftingStatus.READY
    assert result.attempts == 1
    assert result.flags == ()
    assert result.usage == Usage(10, 20, 1)
    assert len(result.spans) == 1
    span = result.spans[0]
    assert span.id == "draft-1:ai-1-1"
    assert (span.text, span.support, span.paragraph, span.author) == (
        NOTES,
        CLEAN.sentences[0].support,
        2,
        SpanAuthor.AI,
    )


def test_two_redrafts_then_preserve_whole_draft_failure() -> None:
    drafter = FakeDrafter(lambda *_: Draft(()), Usage(10, 20, 1))
    result = run_drafting(
        NOTES,
        (),
        FACTS,
        drafter,
        lambda: NOW,
        NOW,
        lambda: ACTIVE,
        get_state_basis=lambda: {"CA": TEST_BASIS},
        new_revision=lambda: "draft-1",
    )
    assert result.status is DraftingStatus.CUSTOMER_REVIEW
    assert result.attempts == len(drafter.calls) == 3
    assert result.usage == Usage(30, 60, 3)
    assert result.flags[0].span_id is None
    assert result.flags[0].kind is FlagKind.MODE_BOUNDARY
    assert drafter.calls[0][1] == ()
    assert "empty" in drafter.calls[1][1][0]
    assert "empty" in drafter.calls[2][1][0]


def test_redraft_can_succeed_without_exhausting_budget() -> None:
    drafter = FakeDrafter(lambda attempt, _: Draft(()) if attempt == 1 else CLEAN)
    result = run_drafting(
        NOTES,
        (),
        FACTS,
        drafter,
        lambda: NOW,
        NOW,
        lambda: ACTIVE,
        get_state_basis=lambda: {"CA": TEST_BASIS},
        new_revision=lambda: "draft-1",
    )
    assert result.status is DraftingStatus.READY
    assert result.attempts == 2
    assert result.flags == ()


def test_replacement_draft_spans_do_not_reuse_previous_attempt_ids() -> None:
    drafter = FakeDrafter(lambda *_: Draft((DraftSentence("The meter was broken.", ()),)))
    result = run_drafting(
        NOTES,
        (),
        FACTS,
        drafter,
        lambda: NOW,
        NOW,
        lambda: ACTIVE,
        get_state_basis=lambda: {"CA": TEST_BASIS},
        new_revision=lambda: "draft-1",
    )
    assert result.status is DraftingStatus.CUSTOMER_REVIEW
    assert result.attempts == 3
    assert result.spans[0].id == "draft-1:ai-3-1"
    assert result.flags[0].span_id == result.spans[0].id


def test_separate_runs_do_not_reuse_span_ids() -> None:
    drafter = FakeDrafter(lambda *_: CLEAN)
    first = run_drafting(
        NOTES,
        (),
        FACTS,
        drafter,
        lambda: NOW,
        NOW,
        lambda: ACTIVE,
        get_state_basis=lambda: {"CA": TEST_BASIS},
        new_revision=lambda: "draft-1",
    )
    second = run_drafting(
        NOTES,
        (),
        FACTS,
        drafter,
        lambda: NOW,
        NOW,
        lambda: ACTIVE,
        get_state_basis=lambda: {"CA": TEST_BASIS},
        new_revision=lambda: "draft-2",
    )
    assert first.spans[0].id == "draft-1:ai-1-1"
    assert second.spans[0].id == "draft-2:ai-1-1"


def test_expiry_before_first_call() -> None:
    drafter = FakeDrafter(lambda *_: CLEAN)
    result = run_drafting(
        NOTES,
        (),
        FACTS,
        drafter,
        lambda: NOW + DEFAULT_WINDOW,
        NOW,
        lambda: ACTIVE,
        get_state_basis=lambda: {"CA": TEST_BASIS},
        new_revision=lambda: "draft-1",
    )
    assert result.status is DraftingStatus.WINDOW_EXPIRED
    assert drafter.calls == []


def test_expiry_during_call_retains_returned_draft_and_usage() -> None:
    times = iter([NOW, NOW, NOW + DEFAULT_WINDOW])
    drafter = FakeDrafter(lambda *_: CLEAN, Usage(10, 20, 1))
    result = run_drafting(
        NOTES,
        (),
        FACTS,
        drafter,
        lambda: next(times),
        NOW,
        lambda: ACTIVE,
        get_state_basis=lambda: {"CA": TEST_BASIS},
        new_revision=lambda: "draft-1",
    )
    assert result.status is DraftingStatus.WINDOW_EXPIRED
    assert result.attempts == 1
    assert result.spans[0].text == NOTES
    assert result.usage == Usage(10, 20, 1)


def test_expiry_before_redraft_preserves_flags() -> None:
    times = iter([NOW, NOW, NOW, NOW + DEFAULT_WINDOW])
    drafter = FakeDrafter(lambda *_: Draft(()))
    result = run_drafting(
        NOTES,
        (),
        FACTS,
        drafter,
        lambda: next(times),
        NOW,
        lambda: ACTIVE,
        get_state_basis=lambda: {"CA": TEST_BASIS},
        new_revision=lambda: "draft-1",
    )
    assert result.status is DraftingStatus.WINDOW_EXPIRED
    assert result.attempts == 1
    assert result.flags[0].span_id is None


def test_policy_lookup_cannot_start_call_after_window_expiry() -> None:
    now = NOW

    def lookup() -> ResolvedAgency:
        nonlocal now
        now = NOW + DEFAULT_WINDOW
        return ACTIVE

    drafter = FakeDrafter(lambda *_: CLEAN)
    result = run_drafting(
        NOTES,
        (),
        FACTS,
        drafter,
        lambda: now,
        NOW,
        lookup,
        get_state_basis=lambda: {"CA": TEST_BASIS},
        new_revision=lambda: "draft-1",
    )
    assert result.status is DraftingStatus.WINDOW_EXPIRED
    assert result.attempts == 0
    assert drafter.calls == []


def test_permission_is_rechecked_before_redraft() -> None:
    controls = iter(
        [
            (),
            (
                ServiceControlEntry(
                    "CA", ServiceControlCategory.LEGAL, "test block", "owner", date(2026, 10, 1)
                ),
            ),
        ]
    )
    drafter = FakeDrafter(lambda *_: Draft(()))
    result = run_drafting(
        NOTES,
        (),
        FACTS,
        drafter,
        lambda: NOW,
        NOW,
        lambda: ACTIVE,
        get_state_basis=lambda: {"CA": TEST_BASIS},
        get_service_control=lambda: next(controls),
        new_revision=lambda: "draft-1",
    )
    assert result.status is DraftingStatus.MODE_BLOCKED
    assert result.attempts == 1
    assert result.flags[0].span_id is None


def test_naive_clock_is_rejected_before_provider_call() -> None:
    drafter = FakeDrafter(lambda *_: CLEAN)
    with pytest.raises(ValueError, match="timezone-aware"):
        run_drafting(
            NOTES,
            (),
            FACTS,
            drafter,
            lambda: NOW.replace(tzinfo=None),
            NOW,
            lambda: ACTIVE,
            get_state_basis=lambda: {"CA": TEST_BASIS},
            new_revision=lambda: "draft-1",
        )
    assert drafter.calls == []


def test_state_basis_is_rechecked_before_redraft() -> None:
    bases = iter([{"CA": TEST_BASIS}, {}])
    drafter = FakeDrafter(lambda *_: Draft(()))
    result = run_drafting(
        NOTES,
        (),
        FACTS,
        drafter,
        lambda: NOW,
        NOW,
        lambda: ACTIVE,
        get_state_basis=lambda: next(bases),
        new_revision=lambda: "draft-1",
    )
    assert result.status is DraftingStatus.MODE_BLOCKED
    assert result.attempts == len(drafter.calls) == 1
    assert result.flags[0].span_id is None
