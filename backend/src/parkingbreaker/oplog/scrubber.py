"""Deterministic scrubber for log entries. Pure: no I/O.

Every entry the log writer produces is checked against the case's own sensitive strings
and against generic identifying patterns (docs/architecture-v6.md section 9.2). A failing
entry is replaced by its code-built tag line alone. Reasons name only the kind of match,
never the matched text, so the result itself is safe to record.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

_MIN_SENSITIVE_CHARS = 1

_MONTH = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
    r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
_FULL_MONTH_NOT_MAY = (
    r"(?:january|february|march|april|june|july|august|september|october|november|december)"
)
_STREET_SUFFIX = (
    r"(?:street|avenue|boulevard|road|drive|lane|court|terrace|parkway|highway|place|way"
    r"|st|ave|blvd|rd|dr|ln|ct|pl|pkwy|hwy)"
)
_PROSE_AFTER_NUMBER = (
    r"(?:days?|weeks?|months?|hours?|minutes?|times?|of|and|or|to|in|on|more|fewer|other"
    r"|questions?|letters?|pages?|quotes?|conflicts?|cents?|redrafts?|flags?|attempts?)\b"
)

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")),
    (
        "phone",
        re.compile(
            r"(?<![\w-])(?:\+?1[\s.-]?)?(?:\(\d{3}\)\s?|\d{3}[\s.-])\d{3}[\s.-]\d{4}(?![\w-])"
            r"|(?<![\w-])\d{3}[.-]\d{4}(?![\w-])"
        ),
    ),
    ("digit_run", re.compile(r"\d{6,}")),
    (
        "street_address",
        re.compile(
            r"(?<![\w-])\d{1,6}(?:[a-z]\b)?\s+(?!" + _PROSE_AFTER_NUMBER + r")"
            r"(?:[A-Za-z0-9][\w.'-]*\s+){1,3}" + _STREET_SUFFIX + r"\b\.?",
            re.IGNORECASE,
        ),
    ),
    (
        "exact_date",
        re.compile(
            r"(?<![\w-])\d{4}[-/.]\d{1,2}[-/.]\d{1,2}(?![\w-])"
            r"|(?<![\w/-])\d{1,2}[/-]\d{1,2}[/-]\d{2,4}(?![\w/-])"
            r"|\b" + _MONTH + r"\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}\b"
            r"|\b\d{1,2}(?:st|nd|rd|th)?\s+(?:of\s+)?" + _MONTH + r"\.?,?\s+\d{4}\b"
            r"|\b" + _FULL_MONTH_NOT_MAY + r"\s+\d{1,2}(?:st|nd|rd|th)?\b",
            re.IGNORECASE,
        ),
    ),
)

_PLATE_WORD = re.compile(r"\bplates?\b", re.IGNORECASE)
_PLATE_WINDOW = 30
_PLATE_TOKEN = re.compile(r"\b(?=[A-Za-z0-9]*\d)(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{2,8}\b")
_ISO_WEEK = re.compile(r"\b\d{4}-W\d{2}\b")


@dataclass(frozen=True)
class ScrubResult:
    clean: bool
    reasons: tuple[str, ...]


def _normalise(text: str) -> str:
    return " ".join(text.lower().split())


def _sensitive_pattern(
    value: str, *, min_chars: int = _MIN_SENSITIVE_CHARS
) -> re.Pattern[str] | None:
    """Matches the value whatever its case, spacing or punctuation, on word boundaries."""
    chars = [c for c in _normalise(value) if c.isalnum()]
    if not chars or len(chars) < min_chars:
        return None
    body = r"[^a-z0-9]{0,2}".join(re.escape(c) for c in chars)
    return re.compile(r"(?<![a-z0-9])" + body + r"(?![a-z0-9])")


def _plate_spans(text: str) -> Iterable[tuple[int, int]]:
    masked = _ISO_WEEK.sub(lambda m: " " * len(m.group()), text)
    for word in _PLATE_WORD.finditer(masked):
        start = max(0, word.start() - _PLATE_WINDOW)
        window = masked[start : word.end() + _PLATE_WINDOW]
        for match in _PLATE_TOKEN.finditer(window):
            yield start + match.start(), start + match.end()


@dataclass(frozen=True)
class IdentifierSpan:
    start: int
    end: int
    reason: str


def identifier_spans(
    text: str,
    sensitive_strings: Iterable[str] = (),
    *,
    include_generic: bool = True,
    standalone_plates: bool = False,
    min_sensitive_chars: int = _MIN_SENSITIVE_CHARS,
) -> tuple[IdentifierSpan, ...]:
    """The scrubber's matches with original offsets, including overlapping detections.

    Prompt screening and logs can request standalone plate-like tokens. ISO week tags
    are excluded from generic plate detection. Exact case strings use the same
    normalisation as logs, with a map back through collapsed whitespace and lowercasing.
    """
    normalised: list[str] = []
    offsets: list[int] = []
    for index, char in enumerate(text):
        if char.isspace():
            if not normalised or normalised[-1] == " ":
                continue
            normalised.append(" ")
            offsets.append(index)
        else:
            lowered = char.lower()
            normalised.extend(lowered)
            offsets.extend([index] * len(lowered))
    haystack = "".join(normalised)
    found: list[IdentifierSpan] = []
    for value in sensitive_strings:
        pattern = _sensitive_pattern(value, min_chars=min_sensitive_chars)
        if pattern is not None:
            found.extend(
                IdentifierSpan(
                    offsets[m.start()], offsets[m.end() - 1] + 1, "case_sensitive_string"
                )
                for m in pattern.finditer(haystack)
            )
    if include_generic:
        for reason, pattern in _PATTERNS:
            found.extend(IdentifierSpan(m.start(), m.end(), reason) for m in pattern.finditer(text))
        plates: Iterable[tuple[int, int]]
        if standalone_plates:
            masked = _ISO_WEEK.sub(lambda match: " " * len(match.group()), text)
            plates = ((m.start(), m.end()) for m in _PLATE_TOKEN.finditer(masked))
        else:
            plates = _plate_spans(text)
        found.extend(IdentifierSpan(start, end, "plate") for start, end in plates)
    return tuple(found)


@dataclass(frozen=True)
class Redaction:
    start: int
    end: int
    placeholder: str


@dataclass(frozen=True)
class RedactedText:
    text: str
    spans: tuple[Redaction, ...]

    def original_offset(self, offset: int) -> int:
        """Map a boundary in masked text back to the original character offsets."""
        shift = 0
        for span in self.spans:
            masked_start = span.start + shift
            if offset <= masked_start:
                break
            masked_end = masked_start + len(span.placeholder)
            if offset < masked_end:
                raise ValueError("offset is inside an identifier placeholder")
            shift += len(span.placeholder) - (span.end - span.start)
        return offset - shift


def redact_identifiers(
    text: str,
    sensitive_strings: Iterable[str] = (),
    *,
    include_generic: bool = True,
    prefix: str = "[[PB_PRIVATE_",
) -> RedactedText:
    """Replace merged identifier spans; keep the mapping local for later restoration.

    Public research evidence uses exact case strings only, so official mailing addresses,
    source hashes and dates remain usable evidence. Notes also use generic detection.
    """
    while prefix in text:
        prefix += "_"
    matches = sorted(
        identifier_spans(
            text,
            sensitive_strings,
            include_generic=include_generic,
            standalone_plates=True,
            min_sensitive_chars=1,
        ),
        key=lambda span: (span.start, span.end),
    )
    merged: list[tuple[int, int]] = []
    for match in matches:
        if merged and match.start < merged[-1][1]:
            start, end = merged[-1]
            merged[-1] = (start, max(end, match.end))
        else:
            merged.append((match.start, match.end))
    spans = tuple(Redaction(start, end, f"{prefix}{start}_{end}]]") for start, end in merged)
    pieces: list[str] = []
    end = 0
    for span in spans:
        pieces.extend((text[end : span.start], span.placeholder))
        end = span.end
    pieces.append(text[end:])
    return RedactedText("".join(pieces), spans)


def scrub(entry_text: str, sensitive_strings: Iterable[str]) -> ScrubResult:
    """Check one entry. Clean only when no sensitive string and no generic pattern appears."""
    found = {
        span.reason
        for span in identifier_spans(entry_text, sensitive_strings, standalone_plates=True)
    }
    order = ("case_sensitive_string", *(reason for reason, _ in _PATTERNS), "plate")
    return ScrubResult(
        clean=not found, reasons=tuple(reason for reason in order if reason in found)
    )


def entry_or_tagline(entry: str, tagline: str, sensitive_strings: Iterable[str]) -> str:
    """The entry when it scrubs clean, otherwise the code-built tag line alone."""
    return entry if scrub(entry, sensitive_strings).clean else tagline
