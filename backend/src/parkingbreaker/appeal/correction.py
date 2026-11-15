"""Draft correction (architecture v6, section 5.4). Pure policy, no I/O.

A draft in the customer's workspace is a list of spans, each written by the AI or by the
customer. Only AI spans go through pass 3 (proofread). Customer spans are the customer's
own statement and are checked only for length, allowed characters and page fit.

When pass 3 fails, pass 2 redrafts with the violations as feedback, at most
MAX_AUTO_REDRAFTS times. After that the failing sentences are shown to the customer as
flags, and adoption stays disabled until every flag is resolved:

- MODE_BOUNDARY (banned phrase, code the customer did not select, ...): EDIT or DELETE.
- TRACEABILITY (a fact not found in the notes): EDIT or DELETE. An unchanged AI
  assertion keeps its attribution and warning through cosmetic edits and copies.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum

from parkingbreaker.appeal.drafting import (
    NOTICE_PREFIX,
    NOTICE_SUPPORT,
    Draft,
    DraftSentence,
    TicketFacts,
    note_segments,
)
from parkingbreaker.appeal.proofread import Problem, missing_customer_statements, proofread
from parkingbreaker.appeal.references import Reference

MAX_AUTO_REDRAFTS = 2
MAX_SPAN_CHARS = 600
MAX_LETTER_CHARS = 3_000  # page fit: a simple character budget for the letter body

# Printable letters, digits (including Latin-1 accents the PDF fonts support), whitespace
# and ordinary letter punctuation. Characters outside this set are named in the flag, never
# silently substituted.
_ALLOWED_TEXT = re.compile("[A-Za-z0-9 \u00a1-\u00ff\n.,;:!?'\"()\\-/&$%#@+*]*")

# Proofread problem kinds that are facts missing from the notes. Everything else is a
# mode-boundary problem, which the customer may not simply confirm.
TRACEABILITY_KINDS: frozenset[str] = frozenset(
    {
        "unsupported",
        "unsupported date",
        "unsupported identifier",
        "unsupported number",
        "unsupported name",
    }
)


class SpanAuthor(StrEnum):
    AI = "ai"
    CUSTOMER = "customer"


@dataclass(frozen=True)
class TextSpan:
    id: str
    text: str
    author: SpanAuthor
    support: tuple[str, ...] = ()
    paragraph: int = 0
    source_span_id: str | None = None
    source_units: tuple[tuple[str, str], ...] = ()


class FlagKind(StrEnum):
    MODE_BOUNDARY = "mode_boundary"
    TRACEABILITY = "traceability"


class CustomerAction(StrEnum):
    EDIT = "edit"
    DELETE = "delete"
    CONFIRM = "confirm"


ALLOWED_ACTIONS: dict[FlagKind, frozenset[CustomerAction]] = {
    FlagKind.MODE_BOUNDARY: frozenset({CustomerAction.EDIT, CustomerAction.DELETE}),
    FlagKind.TRACEABILITY: frozenset({CustomerAction.EDIT, CustomerAction.DELETE}),
}


@dataclass(frozen=True)
class Flag:
    """One failing span, or the whole draft when span_id is None.

    MODE_BOUNDARY wins if any span problem is one. Whole-draft failures must be
    rechecked after editing; they cannot be confirmed or cleared by a span action.
    """

    span_id: str | None
    kind: FlagKind
    details: tuple[str, ...]

    def __str__(self) -> str:
        return f"{self.span_id or 'draft'}: {self.kind}: {'; '.join(self.details)}"


# --- Checks --------------------------------------------------------------------------


def check_customer_text(text: str) -> tuple[str, ...]:
    """Problems with customer-typed text: only length and characters."""
    problems: list[str] = []
    if not text.strip():
        problems.append("text is empty")
    if len(text) > MAX_SPAN_CHARS:
        problems.append(f"text is {len(text)} characters, the limit is {MAX_SPAN_CHARS}")
    if not _ALLOWED_TEXT.fullmatch(text):
        bad = sorted({c for c in text if not _ALLOWED_TEXT.fullmatch(c)})
        problems.append(f"characters not allowed: {''.join(bad)!r}")
    return tuple(problems)


def check_spans(
    spans: Sequence[TextSpan],
    notes: str,
    selected: Sequence[Reference],
    facts: TicketFacts,
) -> tuple[Problem, ...]:
    """Pass 3 on AI spans, light checks on customer spans, and page fit for the whole.
    Problem.sentence is the index into `spans`."""
    problems: list[Problem] = []
    if not spans:
        return (Problem(None, "empty", "the draft has no spans"),)
    ai_index = [
        i
        for i, s in enumerate(spans)
        if s.author is SpanAuthor.AI
        or NOTICE_SUPPORT in s.support
        or NOTICE_PREFIX.casefold() in s.text.casefold()
    ]
    if ai_index:
        draft = Draft(
            tuple(
                DraftSentence(spans[i].text, spans[i].support, spans[i].paragraph) for i in ai_index
            )
        )
        for p in proofread(draft, notes, selected, facts, require_all_notes=False):
            sentence = None if p.sentence is None else ai_index[p.sentence]
            problems.append(Problem(sentence, p.kind, p.detail))
    if all(span.author is SpanAuthor.AI for span in spans):
        missing = missing_customer_statements("\n".join(span.text for span in spans), notes)
        if missing:
            problems.append(
                Problem(None, "omitted customer text", f"customer statements missing: {missing}")
            )
    for i, span in enumerate(spans):
        if span.author is SpanAuthor.AI and not span.text.strip():
            problems.append(Problem(i, "empty", "sentence text is empty"))
        if span.author is SpanAuthor.CUSTOMER:
            problems.extend(Problem(i, "customer text", d) for d in check_customer_text(span.text))
    total = len(" ".join(s.text for s in spans))
    if total > MAX_LETTER_CHARS:
        problems.append(
            Problem(None, "page fit", f"{total} characters, the limit is {MAX_LETTER_CHARS}")
        )
    return tuple(problems)


def flags_from_problems(spans: Sequence[TextSpan], problems: Sequence[Problem]) -> tuple[Flag, ...]:
    """One flag per failing span, plus a whole-draft flag for empty/page-fit failures."""
    by_index: dict[int, list[Problem]] = {}
    draft_problems: list[Problem] = []
    for p in problems:
        if p.sentence is None:
            draft_problems.append(p)
        else:
            by_index.setdefault(p.sentence, []).append(p)
    flags: list[Flag] = []
    if draft_problems:
        flags.append(
            Flag(
                None,
                FlagKind.MODE_BOUNDARY,
                tuple(f"{p.kind}: {p.detail}" for p in draft_problems),
            )
        )
    for i in sorted(by_index):
        found = by_index[i]
        traceable = all(p.kind in TRACEABILITY_KINDS for p in found)
        kind = FlagKind.TRACEABILITY if traceable else FlagKind.MODE_BOUNDARY
        span = spans[i]
        source_ids = tuple(dict.fromkeys(source_id for source_id, _ in span.source_units))
        for source_id in source_ids or (span.source_span_id or span.id,):
            flags.append(Flag(source_id, kind, tuple(f"{p.kind}: {p.detail}" for p in found)))
    return tuple(flags)


# --- Policy --------------------------------------------------------------------------


@dataclass(frozen=True)
class Redraft:
    feedback: tuple[str, ...]


@dataclass(frozen=True)
class CustomerReview:
    flags: tuple[Flag, ...]


CorrectionStep = Redraft | CustomerReview


def next_correction_step(failed_attempts: int, flags: Sequence[Flag]) -> CorrectionStep:
    """After `failed_attempts` drafts have failed pass 3 (1-based): redraft with the
    violations as feedback up to MAX_AUTO_REDRAFTS times, then hand the flags to the
    customer."""
    if failed_attempts < 1:
        raise ValueError(f"failed_attempts is 1-based: {failed_attempts}")
    if not flags:
        raise ValueError("a failed draft has at least one flag")
    if failed_attempts <= MAX_AUTO_REDRAFTS:
        return Redraft(tuple(str(f) for f in flags))
    return CustomerReview(tuple(flags))


# --- Customer resolution -------------------------------------------------------------


@dataclass(frozen=True)
class CustomerResolution:
    action: CustomerAction
    text: str | None = None  # the new sentence, for EDIT only


@dataclass(frozen=True)
class CustomerNote:
    """A fact the customer confirmed; the caller appends it to their notes."""

    span_id: str
    text: str


@dataclass(frozen=True)
class ResolutionResult:
    spans: tuple[TextSpan, ...]
    note: CustomerNote | None = None


def apply_customer_resolution(
    spans: Sequence[TextSpan], flag: Flag, resolution: CustomerResolution
) -> ResolutionResult:
    """Apply the customer's answer to one flag. Raises ValueError for an action the flag's
    kind does not allow, a missing span, or text given where none belongs."""
    if flag.span_id is None:
        raise ValueError("whole-draft problems require editing and rechecking the draft")
    if resolution.action not in ALLOWED_ACTIONS[flag.kind]:
        raise ValueError(f"{resolution.action} is not allowed for a {flag.kind} flag")
    index = next((i for i, s in enumerate(spans) if s.id == flag.span_id), None)
    if index is None:
        raise ValueError(f"no span {flag.span_id!r}")
    if (resolution.text is not None) != (resolution.action is CustomerAction.EDIT):
        raise ValueError("text is required for EDIT and not accepted otherwise")
    span = spans[index]
    match resolution.action:
        case CustomerAction.EDIT:
            assert resolution.text is not None
            edited = replace(span, text=resolution.text, author=SpanAuthor.CUSTOMER, support=())
            return ResolutionResult((*spans[:index], edited, *spans[index + 1 :]))
        case CustomerAction.DELETE:
            return ResolutionResult((*spans[:index], *spans[index + 1 :]))
        case CustomerAction.CONFIRM:
            raise ValueError("confirmation cannot replace an actual customer edit")


def unresolved_flags(flags: Sequence[Flag], resolved_span_ids: Collection[str]) -> tuple[Flag, ...]:
    # Whole-draft flags survive all per-span acknowledgments. The caller runs check_spans
    # again on the edited draft and rebuilds flags to clear an empty/page-fit failure.
    return tuple(f for f in flags if f.span_id is None or f.span_id not in resolved_span_ids)


def adoption_allowed(flags_unresolved: Collection[Flag]) -> bool:
    return not flags_unresolved


def flags_after_edit(
    flags: Sequence[Flag],
    spans: Sequence[TextSpan],
    letter_text: str,
    notes: str,
    *,
    replace_customer_content: bool = False,
) -> tuple[Flag, ...]:
    """Keep surviving AI failures and omissions until restored or explicitly replaced.

    A split AI span keeps its original flag's lineage. Unsupported block details become
    obsolete only when every surviving descendant is a complete original note unit.
    Replacement intent clears omission details alone, never an unchanged AI failure.
    """
    retained = [span for span in spans if span.author is SpanAuthor.AI]
    complete = not missing_customer_statements(letter_text, notes)
    original_order = {
        " ".join(text.split()).casefold().rstrip("."): index
        for index, (_, _, text) in reversed(tuple(enumerate(note_segments(notes))))
    }
    rendered_order = [
        original_order[key]
        for _, _, text in note_segments(letter_text)
        if (key := " ".join(text.split()).casefold().rstrip(".")) in original_order
    ]
    order_restored = complete and rendered_order == sorted(rendered_order)
    result: list[Flag] = []
    for flag in flags:
        descendants = (
            [
                span
                for span in retained
                if flag.span_id in (span.id, span.source_span_id)
                or any(source_id == flag.span_id for source_id, _ in span.source_units)
            ]
            if flag.span_id is not None
            else retained
        )
        supported = bool(descendants) and all(
            not missing_customer_statements(notes, span.text) for span in descendants
        )
        details: list[str] = []
        for detail in flag.details:
            if detail.startswith("omitted customer text:"):
                if not (complete or replace_customer_content):
                    details.append(detail)
            elif flag.span_id is None and detail.startswith("page fit:"):
                if len(letter_text) > MAX_LETTER_CHARS:
                    details.append(detail)
            elif flag.span_id is None and detail.startswith("customer order changed:"):
                if not order_restored and descendants:
                    details.append(detail)
            elif descendants and not (
                flag.span_id is not None and supported and detail.startswith("unsupported")
            ):
                details.append(detail)
        if details:
            result.append(replace(flag, details=tuple(details)))
    return tuple(result)


def edited_spans(
    old: str, new: str, spans: tuple[TextSpan, ...], revision: str
) -> tuple[TextSpan, ...]:
    """Retain each complete AI sentence wherever its words survive, including copies.

    Word matching ignores case, punctuation and whitespace. The emitted spans always
    slice the actual new text, so formatting is preserved exactly. Overlapping matches
    merge into one AI span with every original complete unit and its oldest source ID.
    Keeping those units lets later saves independently recheck each origin, even after
    punctuation changes, line breaks, copies or a partial rewrite of the merged text.
    """
    words = list(re.finditer(r"[^\W_]+", new))
    keys = [word.group().casefold() for word in words]
    candidates: list[tuple[int, int, TextSpan, tuple[str, str]]] = []
    for span in spans:
        if span.author is not SpanAuthor.AI:
            continue
        units = span.source_units or tuple(
            (span.source_span_id or span.id, text) for _, _, text in note_segments(span.text)
        )
        for source_unit in units:
            _, unit = source_unit
            wanted = [word.casefold() for word in re.findall(r"[^\W_]+", unit)]
            if not wanted:
                continue
            for index in range(len(words) - len(wanted) + 1):
                if keys[index : index + len(wanted)] != wanted:
                    continue
                start = words[index].start()
                end = words[index + len(wanted) - 1].end()
                # Keep adjacent punctuation with its sentence, without swallowing the
                # separator or any following customer's words.
                while end < len(new) and not new[end].isalnum() and not new[end].isspace():
                    end += 1
                candidates.append((start, end, span, source_unit))
    retained: list[tuple[int, int, TextSpan, tuple[tuple[str, str], ...]]] = []
    for start, end, span, matched_unit in sorted(candidates, key=lambda item: (item[0], -item[1])):
        if retained and start < retained[-1][1]:
            previous_start, previous_end, previous_span, previous_units = retained[-1]
            retained[-1] = (
                previous_start,
                max(previous_end, end),
                replace(
                    previous_span,
                    support=tuple(dict.fromkeys((*previous_span.support, *span.support))),
                ),
                tuple(dict.fromkeys((*previous_units, matched_unit))),
            )
        else:
            retained.append((start, end, span, (matched_unit,)))
    result: list[TextSpan] = []
    offset = 0
    for start, end, span, units in retained:
        if start > offset:
            result.append(
                TextSpan(f"{revision}:customer-{offset}", new[offset:start], SpanAuthor.CUSTOMER)
            )
        result.append(
            replace(
                span,
                id=f"{revision}:ai-{start}",
                text=new[start:end],
                source_span_id=units[0][0],
                source_units=units,
            )
        )
        offset = end
    if offset < len(new):
        result.append(TextSpan(f"{revision}:customer-{offset}", new[offset:], SpanAuthor.CUSTOMER))
    return tuple(result)
