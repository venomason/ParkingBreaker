"""The growing Book of Tricks and the hunter that looks for them. Pure: callers pass text in.

A trick is a known way an official page can mislead appeal research (an online-only appeal
row, a deposit that belongs to a later hearing, a re-appeal form that admits
responsibility). Each trick's signals are phrases copied verbatim from a saved official page,
never invented wording. The hunter scans one place's saved official page texts and reports
every sighting with the page URL, its SHA-256 and the exact sentence that matched.

New tricks come from a proposer (a model worker later; a fake in tests). Proposals are held
for owner review and never enter the catalog or change research by themselves: making the
locked research guards act on a trick needs the owner's OK each time (CLAUDE.md section 2).
The hunter never stops, charges or mails anything.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

_SPACE = re.compile(r"\s+")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s")
MAX_EXCERPT = 300

# Audit A018: guard procedural "book of tricks" content against defense recommendations,
# outcome grading, and automatic plea choices while preserving routing/deadline warnings.
_PROHIBITED_TRICK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "defense_recommendation",
        re.compile(
            r"\b(?:"
            r"best\s+defense|"
            r"recommend(?:ed|s)?\s+(?:arguing|defense|plea)|"
            r"raise\s+this\s+defense|"
            r"strongest\s+argument|"
            r"assert\s+(?:this\s+)?defense|"
            r"defense\s+strategy"
            r")\b",
            re.IGNORECASE,
        ),
    ),
    (
        "outcome_grading",
        re.compile(
            r"\b(?:"
            r"dismissal\s+odds|"
            r"chance(?:s)?\s+of\s+(?:winning|dismissal)|"
            r"likely\s+to\s+(?:win|be\s+dismissed)|"
            r"win\s+rate|"
            r"success\s+rate|"
            r"grade\s+(?:your|the)\s+(?:case|appeal)"
            r")\b",
            re.IGNORECASE,
        ),
    ),
    (
        "automatic_plea_choice",
        re.compile(
            r"\b(?:"
            r"automatically\s+(?:plead|select\s+(?:a\s+)?plea|choose\s+(?:a\s+)?plea)|"
            r"should\s+plead|"
            r"choose\s+(?:mitigation|contested)\s+for\s+the\s+customer|"
            r"default\s+to\s+pleading"
            r")\b",
            re.IGNORECASE,
        ),
    ),
)


def prohibited_trick_reason(text: str) -> str | None:
    """Return a category if text recommends defenses, grades outcomes, or picks pleas."""
    normalized = _normal(text)
    for reason, pattern in _PROHIBITED_TRICK_PATTERNS:
        if pattern.search(normalized):
            return reason
    return None


def guard_procedural_trick(trick: Trick) -> None:
    """Reject tricks with defense recommendations, outcome grading, or automatic plea choices."""
    for field_text in (trick.title, trick.what_goes_wrong, *trick.signals):
        reason = prohibited_trick_reason(field_text)
        if reason is not None:
            raise ValueError(f"prohibited_non_procedural_trick:{reason}")


@dataclass(frozen=True)
class Trick:
    trick_id: str
    title: str
    what_goes_wrong: str
    signals: tuple[str, ...]
    first_seen_place: str
    first_seen_sha256: str


@dataclass(frozen=True)
class SavedPage:
    url: str
    sha256: str
    text: str


@dataclass(frozen=True)
class Sighting:
    place: str
    trick_id: str
    url: str
    sha256: str
    excerpt: str


@dataclass(frozen=True)
class Proposal:
    """A possible new trick, held for owner review; it carries its exact source sentence."""

    place: str
    title: str
    url: str
    sha256: str
    excerpt: str


class TrickProposer(Protocol):
    def propose(self, place: str, pages: Sequence[SavedPage]) -> Sequence[Proposal]: ...


@dataclass(frozen=True)
class HuntReport:
    place: str
    sightings: tuple[Sighting, ...]
    proposals: tuple[Proposal, ...]


def _normal(text: str) -> str:
    return _SPACE.sub(" ", text).strip()


def parse_catalog(text: str) -> tuple[Trick, ...]:
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("the catalog must be a JSON array")
    tricks: list[Trick] = []
    for item in data:
        fields = {"trick_id", "title", "what_goes_wrong", "signals", "first_seen"}
        if not isinstance(item, dict) or set(item) != fields:
            raise ValueError("trick has unknown or missing fields")
        signals = item["signals"]
        if (
            not isinstance(signals, list)
            or not signals
            or not all(isinstance(s, str) and len(s.strip()) >= 12 for s in signals)
        ):
            raise ValueError("each trick needs signal phrases of at least 12 characters")
        seen = item["first_seen"]
        if not isinstance(seen, dict) or set(seen) != {"place", "sha256"}:
            raise ValueError("first_seen needs place and sha256")
        if not re.fullmatch(r"[0-9a-f]{64}", str(seen["sha256"])):
            raise ValueError("first_seen sha256 must be a full SHA-256")
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", str(item["trick_id"])):
            raise ValueError("trick_id must be an upper-case token")
        trick = Trick(
            item["trick_id"],
            item["title"],
            item["what_goes_wrong"],
            tuple(_normal(s) for s in signals),
            seen["place"],
            seen["sha256"],
        )
        guard_procedural_trick(trick)
        tricks.append(trick)
    if len({t.trick_id for t in tricks}) != len(tricks):
        raise ValueError("duplicate trick_id")
    return tuple(tricks)


def _excerpt(text: str, start: int, end: int) -> str:
    """The sentence holding the match, as it appears on the page (whitespace collapsed)."""
    left = max((m.end() for m in _SENTENCE_END.finditer(text, 0, start)), default=0)
    right_match = _SENTENCE_END.search(text, end)
    right = right_match.start() if right_match else len(text)
    sentence = text[left:right]
    if len(sentence) > MAX_EXCERPT:
        sentence = text[max(left, start - 100) : min(right, end + 100)]
    return sentence.strip()


def hunt(
    place: str,
    pages: Iterable[SavedPage],
    catalog: Sequence[Trick],
    proposer: TrickProposer | None = None,
) -> HuntReport:
    """Every known trick seen on these pages, once per trick and page, plus held proposals."""
    pages = tuple(pages)
    sightings: list[Sighting] = []
    for page in pages:
        text = _normal(page.text)
        lowered = text.lower()
        for trick in catalog:
            for signal in trick.signals:
                index = lowered.find(signal.lower())
                if index >= 0:
                    sightings.append(
                        Sighting(
                            place,
                            trick.trick_id,
                            page.url,
                            page.sha256,
                            _excerpt(text, index, index + len(signal)),
                        )
                    )
                    break
    proposals: tuple[Proposal, ...] = ()
    if proposer is not None:
        known = {(p.url, p.sha256): _normal(p.text) for p in pages}
        # A proposal must quote a page that was actually scanned, verbatim, and stay procedural.
        proposals = tuple(
            p
            for p in proposer.propose(place, pages)
            if len(p.excerpt.strip()) >= 12
            and _normal(p.excerpt) in known.get((p.url, p.sha256), "")
            and prohibited_trick_reason(p.title) is None
            and prohibited_trick_reason(p.excerpt) is None
        )
    return HuntReport(place, tuple(sightings), proposals)
