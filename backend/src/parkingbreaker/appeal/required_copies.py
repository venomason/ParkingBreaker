"""Verified, request-specific document copies; never physical-original fulfilment."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass

from parkingbreaker.research.findings import Finding
from parkingbreaker.research.fulfilment import (
    customer_optional,
    requirement_interpretable,
    verified_findings,
)
from parkingbreaker.research.questions import FactKey, Ticket
from parkingbreaker.research.sources import SourceRecord


@dataclass(frozen=True)
class RequiredCopy:
    requirement_id: str
    title: str
    quote: str
    source_sha256s: tuple[str, ...]


@dataclass(frozen=True)
class RequiredCopyPages:
    requirement_id: str
    pages: tuple[bytes, ...]


_UNSUPPORTED = re.compile(
    r"\b(?:citation|ticket|notice|photo\w*|form\w*|original|ink|notar\w*|wet|certified|"
    r"if|unless|when|where|either|or|only|may|might|depending|applicable)\b",
    re.I,
)
_ENCLOSURE_KEYS = frozenset(
    {
        FactKey.REQUIRED_ITEMS,
        FactKey.TICKET_SPECIFIC_REQUIREMENTS,
        FactKey.MANDATORY_FORMS,
        FactKey.SIGNATURE_REQUIREMENT,
        FactKey.REQUIRED_DECLARATIONS,
    }
)
_QUALIFIED_COPY = re.compile(r"\b(?:originals?|ink|notar\w*|wet|certified)\b", re.I)


def unsupported_copy_requirements(
    findings: Sequence[Finding], sources: Sequence[SourceRecord], ticket: Ticket
) -> bool:
    """Never let an ordinary-copy rule erase a stronger, separately evidenced rule.

    We cannot prove original, certified, ink or notarial compliance from an uploaded
    raster. Keep the whole request blocked when verified copy/enclosure rules include
    those qualifications, even when they occur in another fact or source. Matching
    document names across independently worded obligations is not yet supported.
    """
    guarded = tuple(
        finding
        for finding in verified_findings(findings, sources, ticket)
        if finding.key in _ENCLOSURE_KEYS and isinstance(finding.value, list) and finding.value
    )
    texts = []
    for finding in guarded:
        assert isinstance(finding.value, list)
        texts.append(
            " ".join(str(item) for item in finding.value) + " " + (finding.verbatim_quote or "")
        )
    return any(re.search(r"\bcop(?:y|ies)\b", text, re.I) for text in texts) and any(
        _QUALIFIED_COPY.search(text) for text in texts
    )


def required_copy_items(
    findings: Sequence[Finding], sources: Sequence[SourceRecord], ticket: Ticket
) -> tuple[RequiredCopy, ...]:
    if unsupported_copy_requirements(findings, sources, ticket):
        return ()
    result: dict[str, RequiredCopy] = {}
    for finding in verified_findings(findings, sources, ticket):
        if finding.key not in {FactKey.REQUIRED_ITEMS, FactKey.TICKET_SPECIFIC_REQUIREMENTS}:
            continue
        if not isinstance(finding.value, list):
            continue
        quote = finding.verbatim_quote or ""
        for item in finding.value:
            if not isinstance(item, str):
                continue
            if (
                not re.fullmatch(r"copy of (?:the )?[A-Za-z][A-Za-z -]{1,100}", item, re.I)
                or _UNSUPPORTED.search(item + " " + quote)
                or customer_optional(item, quote)
                or not requirement_interpretable(finding.key, item, quote)
            ):
                continue
            hashes = tuple(sorted(set(finding.source_sha256s)))
            payload = json.dumps(
                [finding.key.value, item, quote, hashes, asdict(ticket)],
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            copy = RequiredCopy(hashlib.sha256(payload.encode()).hexdigest(), item, quote, hashes)
            identity = " ".join(item.casefold().split())
            previous = result.get(identity)
            if previous is None or copy.requirement_id < previous.requirement_id:
                result[identity] = copy
    return tuple(result[key] for key in sorted(result))


def validated_copy_pages(
    items: Sequence[RequiredCopy], selected: Sequence[RequiredCopyPages]
) -> tuple[tuple[RequiredCopy, tuple[bytes, ...]], ...]:
    inventory = {item.requirement_id: item for item in items}
    pages = {item.requirement_id: item.pages for item in selected}
    if (
        len(inventory) != len(items)
        or len(pages) != len(selected)
        or pages.keys() != inventory.keys()
    ):
        raise ValueError("required copies must match the complete verified inventory")
    if (
        any(not 1 <= len(value) <= 10 for value in pages.values())
        or sum(map(len, pages.values())) > 20
    ):
        raise ValueError("required copy page limit exceeded")
    if any(not isinstance(page, bytes) or not page for value in pages.values() for page in value):
        raise ValueError("required copy page bytes are missing")
    return tuple((item, pages[item.requirement_id]) for item in items)
