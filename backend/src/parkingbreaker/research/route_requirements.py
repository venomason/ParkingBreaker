"""Bounded route-language recognition shared by source guards and fulfilment.

Pure syntax only: callers still verify official authority, issuer, stage and quotes.
Unsupported conditions are never evidence that this service is incompatible.
"""

from __future__ import annotations

import re

_STAGE = (
    r"(?P<stage>initial review|first-level review|administrative review|administrative hearing)"
)
_ARTICLE = r"(?:your |the |an? )?"
_PORTAL = r"(?:using|through|in) (?:the )?(?:online )?portal"
_OBJECT = rf"{_ARTICLE}(?:initial review|first-level review|appeal|request)"
_ONLINE = r"(?:online|electronically|through (?:the )?(?:online )?portal)"
_MAIL = r"(?:by |via )?(?:postal )?mail"
_REQUIRED = (
    rf"you must (?:first )?(?:submit|file|register) {_ARTICLE}(?:signed )?{_STAGE}"
    rf"(?: form| requests?)? {_PORTAL}(?: before mailing (?:your appeal|your evidence))?",
    rf"{_ARTICLE}{_STAGE}(?: form| requests?)? must be (?:submitted|filed) "
    rf"(?:{_PORTAL}|electronically|online)",
    rf"before mailing {_ARTICLE}{_STAGE}, you must (?:first )?(?:upload|submit) "
    rf"{_ARTICLE}(?:signed )?(?:contest|appeal|review) form {_PORTAL}",
    rf"if mailing {_ARTICLE}{_STAGE}, you must first register {_PORTAL}",
    rf"you must (?:appear in person for|attend an in-person appointment before mailing) "
    rf"{_ARTICLE}{_STAGE}",
    rf"{_ARTICLE}{_STAGE}(?: requests?)? (?:must be requested|is available) in person only",
    rf"(?:submit|file) {_ARTICLE}{_STAGE} only {_PORTAL}",
)
_ONLINE_BODY = (
    r"(?:submit (?:your request|your initial review|supporting evidence) electronically"
    r"|include PDF evidence|upload PDF evidence|sign the portal form)"
)
_CAP = r"(?:under |no more than )(?:two|2)\s*(?:megabytes?|MB)"
_LOCAL = (
    rf"if you (?:choose to |opt to |elect to )?(?:submit|file) "
    rf"(?:{_OBJECT} )?{_ONLINE}, you must {_ONLINE_BODY}",
    rf"when (?:submitting|filing) online, you must {_ONLINE_BODY}",
    rf"online submissions(?: of initial review requests)? must {_ONLINE_BODY}",
    rf"for online (?:submissions|appeals), uploads must be (?:PDF files )?{_CAP}",
    r"if you choose an in-person hearing, you must appear in person",
    r"if you choose to appeal in person, you must attend the hearing",
    rf"you must (?:submit|file) {_OBJECT} (?:{_ONLINE} or {_MAIL}|{_MAIL} or {_ONLINE})",
    rf"{_OBJECT} must be (?:submitted|filed) (?:{_ONLINE} or {_MAIL}|{_MAIL} or {_ONLINE})",
    rf"you must not (?:submit|file) {_OBJECT} {_ONLINE}",
    rf"you must submit {_OBJECT} by mail and can pay your fine online",
)


def _content(text: str) -> str:
    return " ".join(text.split()).removesuffix(".")


def route_local_requirement(sentence: str) -> bool:
    """Only whole known channel-local/choice statements; never erase a global clause."""
    return any(re.fullmatch(pattern, _content(sentence), re.I) for pattern in _LOCAL)


def nonlocal_route_statements(quote: str) -> tuple[str, ...]:
    """Remove only whole known optional statements, retaining headings and global clauses.

    A requirement value must still have support outside these statements. Finding
    optional wording anywhere in a quote cannot waive another sentence's requirement.
    """
    sentences = tuple(s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+|[;:]\s*", quote) if s.strip())
    remaining = tuple(s for s in sentences if not route_local_requirement(s))
    # Punctuation splitting alone must not activate the caller's optional-claim filter.
    return remaining if len(remaining) != len(sentences) else (quote,)


def compulsory_route_stage(sentence: str) -> str | None:
    for pattern in _REQUIRED:
        match = re.fullmatch(pattern, _content(sentence), re.I)
        if match:
            return (
                "administrative hearing"
                if match["stage"].lower() == "administrative hearing"
                else "first-level review"
            )
    return None


def possible_route_requirement(sentence: str) -> bool:
    """A conservative unresolved claim, not proof of a compulsory route."""
    return bool(
        compulsory_route_stage(sentence) is not None
        or (
            not route_local_requirement(sentence)
            and re.search(r"\b(?:must|shall|required|only|before)\b", sentence, re.I)
            and re.search(r"\b(?:portal|online|electronically|in.person)\b", sentence, re.I)
            and re.search(r"\b(?:submit|file|register|upload|appear|attend)\w*\b", sentence, re.I)
            and re.search(r"\b(?:appeals?|reviews?|contests?|requests?|uploads?)\b", sentence, re.I)
        )
    )


def quoted_route_stage(item: str, quote: str) -> str | None:
    """Require an adjacent parking-citation heading, rather than a bare instruction."""
    stage = compulsory_route_stage(item)
    item_text, full = " ".join(item.split()), " ".join(quote.split())
    if stage is None or not full.endswith(item_text):
        return None
    heading = full[: -len(item_text)].strip().removesuffix(".").strip()
    if not heading or any(mark in heading for mark in (".", ":", ";", "!", "?")):
        return None
    if not re.search(r"\bparking citations?\b", heading, re.I):
        return None
    heading_stage = (
        "administrative hearing"
        if re.search(r"\badministrative hearing\b", heading, re.I)
        else "first-level review"
        if re.search(
            r"\b(?:initial review|first-level review|administrative review)\b", heading, re.I
        )
        else None
    )
    return stage if heading_stage == stage else None
