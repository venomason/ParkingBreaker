from __future__ import annotations

from datetime import date

import pytest

from parkingbreaker.appeal.correction import (
    MAX_AUTO_REDRAFTS,
    MAX_LETTER_CHARS,
    MAX_SPAN_CHARS,
    CustomerAction,
    CustomerResolution,
    CustomerReview,
    Flag,
    FlagKind,
    Redraft,
    SpanAuthor,
    TextSpan,
    adoption_allowed,
    apply_customer_resolution,
    check_customer_text,
    check_spans,
    flags_after_edit,
    flags_from_problems,
    next_correction_step,
    unresolved_flags,
)
from parkingbreaker.appeal.drafting import Draft, TicketFacts
from parkingbreaker.appeal.proofread import Problem, proofread

NOTES = "the street cleaning sign was covered by a tree branch."
FACTS = TicketFacts(
    citation_number="SF-998877",
    plate="7ABC123",
    issue_date=date(2026, 10, 1),
    violation_code="TC 7.2.22",
    violation_description="Street cleaning",
)
GOOD = TextSpan(
    "s1", "The street cleaning sign was covered by a tree branch.", SpanAuthor.AI, ("note:0-54",)
)
UNTRACED = TextSpan("s2", "I parked there for 45 minutes.", SpanAuthor.AI, ("note:0-54",))
PROMISE = TextSpan("s3", "This ticket will be dismissed.", SpanAuthor.AI, ("note:0-54",))
MINE = TextSpan("c1", "I saw Officer Smith leave at noon.", SpanAuthor.CUSTOMER)

MODE = Flag("s3", FlagKind.MODE_BOUNDARY, ("banned phrase: 'will be dismissed'",))
TRACE = Flag("s2", FlagKind.TRACEABILITY, ("unsupported number: '45'",))


def test_clean_spans_have_no_problems() -> None:
    assert check_spans([GOOD], NOTES, (), FACTS) == ()


def test_only_ai_spans_are_proofread() -> None:
    # The customer's span names a person and a time absent from the notes: not checked.
    assert check_spans([GOOD, MINE], NOTES, (), FACTS) == ()


def test_problem_indices_point_into_all_spans() -> None:
    problems = check_spans([MINE, GOOD, UNTRACED], NOTES, (), FACTS)
    assert problems == (Problem(2, "unsupported number", "'45'"),)


def test_customer_span_checked_for_length_and_characters() -> None:
    long = TextSpan("c2", "a" * (MAX_SPAN_CHARS + 1), SpanAuthor.CUSTOMER)
    odd = TextSpan("c3", "Hello <script>", SpanAuthor.CUSTOMER)
    problems = check_spans([long, odd], NOTES, (), FACTS)
    assert [(p.sentence, p.kind) for p in problems] == [(0, "customer text"), (1, "customer text")]
    assert "<" in problems[1].detail


def test_check_customer_text() -> None:
    assert check_customer_text("I paid $40 on 10/1 - see 7.2.") == ()
    # Typographic characters outside the font are named, not silently converted.
    problems = check_customer_text("I paid \u2014 honestly")
    assert problems
    assert "\u2014" in problems[0]
    assert check_customer_text("  ") == ("text is empty",)
    assert check_customer_text("tab\there") != ()


def test_page_fit_is_a_draft_level_problem() -> None:
    spans = [
        TextSpan(f"c{i}", "x" * MAX_SPAN_CHARS, SpanAuthor.CUSTOMER)
        for i in range(MAX_LETTER_CHARS // MAX_SPAN_CHARS + 1)
    ]
    problems = check_spans(spans, NOTES, (), FACTS)
    assert [(p.sentence, p.kind) for p in problems] == [(None, "page fit")]


def test_flags_classify_per_span() -> None:
    spans = [GOOD, UNTRACED, PROMISE]
    flags = flags_from_problems(spans, check_spans(spans, NOTES, (), FACTS))
    assert [(f.span_id, f.kind) for f in flags] == [
        ("s2", FlagKind.TRACEABILITY),
        ("s3", FlagKind.MODE_BOUNDARY),
    ]


def test_mixed_problems_make_a_mode_boundary_flag() -> None:
    problems = [Problem(0, "unsupported number", "'45'"), Problem(0, "banned phrase", "'x'")]
    (flag,) = flags_from_problems([UNTRACED], problems)
    assert flag.kind is FlagKind.MODE_BOUNDARY
    assert len(flag.details) == 2


def test_draft_level_problems_remain_blocking_flags() -> None:
    problems = [Problem(None, "page fit", "too long")]
    (flag,) = flags_from_problems([GOOD], problems)
    assert flag == Flag(None, FlagKind.MODE_BOUNDARY, ("page fit: too long",))
    assert str(flag) == "draft: mode_boundary: page fit: too long"
    assert not adoption_allowed([flag])


@pytest.mark.parametrize("failed", range(1, MAX_AUTO_REDRAFTS + 1))
def test_redraft_with_feedback_up_to_the_limit(failed: int) -> None:
    step = next_correction_step(failed, [TRACE])
    assert step == Redraft((str(TRACE),))


def test_customer_review_after_the_limit() -> None:
    step = next_correction_step(MAX_AUTO_REDRAFTS + 1, [TRACE, MODE])
    assert step == CustomerReview((TRACE, MODE))


def test_next_step_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="1-based"):
        next_correction_step(0, [TRACE])
    with pytest.raises(ValueError, match="at least one flag"):
        next_correction_step(1, [])


def test_edit_makes_span_the_customers() -> None:
    result = apply_customer_resolution(
        [GOOD, PROMISE], MODE, CustomerResolution(CustomerAction.EDIT, "I ask for a review.")
    )
    assert result.note is None
    assert result.spans[1] == TextSpan("s3", "I ask for a review.", SpanAuthor.CUSTOMER)
    assert result.spans[0] == GOOD


def test_delete_removes_span() -> None:
    result = apply_customer_resolution(
        [GOOD, PROMISE], MODE, CustomerResolution(CustomerAction.DELETE)
    )
    assert result.spans == (GOOD,)


def test_unsupported_facts_require_actual_edits_not_confirmation() -> None:
    with pytest.raises(ValueError, match="not allowed"):
        apply_customer_resolution(
            [GOOD, UNTRACED], TRACE, CustomerResolution(CustomerAction.CONFIRM)
        )


def test_mode_boundary_cannot_be_confirmed() -> None:
    with pytest.raises(ValueError, match="not allowed"):
        apply_customer_resolution([PROMISE], MODE, CustomerResolution(CustomerAction.CONFIRM))


def test_unselected_reference_cannot_be_confirmed_as_customer_input() -> None:
    span = TextSpan("unselected", "The vehicle was loading.", SpanAuthor.AI, ("exception-3",))
    flags = flags_from_problems([span], check_spans([span], NOTES, (), FACTS))
    flag = next(flag for flag in flags if flag.span_id == span.id)
    assert flag.kind is FlagKind.MODE_BOUNDARY
    with pytest.raises(ValueError, match="not allowed"):
        apply_customer_resolution([span], flag, CustomerResolution(CustomerAction.CONFIRM))


@pytest.mark.parametrize(
    "resolution",
    [CustomerResolution(CustomerAction.EDIT), CustomerResolution(CustomerAction.DELETE, "x")],
)
def test_text_only_with_edit(resolution: CustomerResolution) -> None:
    with pytest.raises(ValueError, match="text"):
        apply_customer_resolution([UNTRACED], TRACE, resolution)


def test_unknown_span_is_rejected() -> None:
    with pytest.raises(ValueError, match="no span"):
        apply_customer_resolution([GOOD], TRACE, CustomerResolution(CustomerAction.DELETE))


def test_adoption_waits_for_every_flag() -> None:
    flags = (TRACE, MODE)
    assert not adoption_allowed(unresolved_flags(flags, {"s2"}))
    assert adoption_allowed(unresolved_flags(flags, {"s2", "s3"}))
    assert adoption_allowed(())


def test_customer_edit_must_restore_omitted_words_before_clearing_the_flag() -> None:
    first = TextSpan("first", "The sign was covered.", SpanAuthor.AI, ("note:0-21",))
    notes = first.text + " The meter was broken."
    flag = Flag(None, FlagKind.MODE_BOUNDARY, ("omitted customer text: missing statement 2",))
    assert flags_after_edit([flag], [first], first.text + " Please review.", notes) == (flag,)
    assert flags_after_edit([flag], [first], notes, notes) == ()


def test_unrelated_edit_does_not_clear_an_unchanged_unsupported_ai_span() -> None:
    assert flags_after_edit([TRACE], [UNTRACED], "Edited other text.", NOTES) == (TRACE,)


def test_empty_workspace_draft_is_not_clean_or_adoptable() -> None:
    problems = check_spans([], NOTES, (), FACTS)
    assert problems == (Problem(None, "empty", "the draft has no spans"),)
    flags = flags_from_problems([], problems)
    assert not adoption_allowed(flags)
    assert isinstance(next_correction_step(1, flags), Redraft)
    assert next_correction_step(MAX_AUTO_REDRAFTS + 1, flags) == CustomerReview(flags)


def test_malformed_model_reply_can_follow_bounded_correction_policy() -> None:
    # parse_draft rejects malformed output as an empty Draft. Preserve that failure.
    problems = proofread(Draft(()), NOTES, (), FACTS)
    flags = flags_from_problems([], problems)
    assert flags == (Flag(None, FlagKind.MODE_BOUNDARY, ("empty: the draft has no sentences",)),)
    assert next_correction_step(1, flags) == Redraft((str(flags[0]),))
    assert not adoption_allowed(flags)


def test_page_fit_flag_cannot_be_cleared_by_resolving_every_span() -> None:
    spans = [
        TextSpan(f"c{i}", "x" * MAX_SPAN_CHARS, SpanAuthor.CUSTOMER)
        for i in range(MAX_LETTER_CHARS // MAX_SPAN_CHARS + 1)
    ]
    flags = flags_from_problems(spans, check_spans(spans, NOTES, (), FACTS))
    remaining = unresolved_flags(flags, {span.id for span in spans})
    assert remaining == flags
    assert not adoption_allowed(remaining)
    # Editing the document to fit, then rechecking, is the only way to clear it.
    edited = spans[:-2]
    assert adoption_allowed(flags_from_problems(edited, check_spans(edited, NOTES, (), FACTS)))


@pytest.mark.parametrize("action", list(CustomerAction))
def test_whole_draft_flag_cannot_be_resolved_as_a_sentence(action: CustomerAction) -> None:
    flag = Flag(None, FlagKind.MODE_BOUNDARY, ("page fit: too long",))
    resolution = CustomerResolution(action, "new text" if action is CustomerAction.EDIT else None)
    with pytest.raises(ValueError, match="editing and rechecking"):
        apply_customer_resolution([GOOD], flag, resolution)


def test_span_and_draft_problems_both_survive_flag_conversion() -> None:
    problems = [Problem(0, "unsupported number", "45"), Problem(None, "page fit", "too long")]
    flags = flags_from_problems([UNTRACED], problems)
    assert flags == (
        Flag(None, FlagKind.MODE_BOUNDARY, ("page fit: too long",)),
        Flag(UNTRACED.id, FlagKind.TRACEABILITY, ("unsupported number: 45",)),
    )
    assert unresolved_flags(flags, {UNTRACED.id}) == flags[:1]


@pytest.mark.parametrize("text", ["", " ", "\n"])
def test_blank_ai_sentence_is_not_adoptable_even_with_valid_support(text: str) -> None:
    spans = [TextSpan("s1", text, SpanAuthor.AI, GOOD.support)]
    problems = check_spans(spans, NOTES, (), FACTS)
    assert Problem(0, "empty", "sentence text is empty") in problems
    assert not adoption_allowed(flags_from_problems(spans, problems))
