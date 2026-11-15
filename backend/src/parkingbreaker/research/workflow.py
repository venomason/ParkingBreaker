"""Bounded capabilities derived from independently guarded official requirements.

Unknown wording stays with the existing requirement ladder. Recognizing a hearing
track is a refusal to choose it, never a customer election or permission to mail.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence

from parkingbreaker.research.findings import Finding
from parkingbreaker.research.fulfilment import verified_findings
from parkingbreaker.research.questions import FactKey, Ticket
from parkingbreaker.research.sources import SourceRecord

_REQUIREMENT_KEYS = frozenset(
    {FactKey.PRIOR_STEP_REQUIREMENT, FactKey.TICKET_SPECIFIC_REQUIREMENTS, FactKey.REQUIRED_ITEMS}
)
_TRACK_CHOICE = (
    r"(?:a\s+)?(?:mitigation(?:\s+hearing)?\s+(?:or|and)\s+(?:a\s+)?contested"
    r"|contested(?:\s+hearing)?\s+(?:or|and)\s+(?:a\s+)?mitigation)\b"
)
_ELECTION = re.compile(
    r"^(?:please\s+)?(?:you\s+(?:must|shall|are required to)\s+)?(?:first\s+)?"
    r"(?:choose|select|elect|indicate|specify|check (?:the|one|a) box)\b\s*"
    r"(?:(?:to\s+)?(?:choose|select)\s+|whether\s+(?:you\s+(?:are requesting|want)"
    r"|to request)\s+|between\s+|either\s+|one of\s+|:\s*)*" + _TRACK_CHOICE
)


def _leaves(findings: Sequence[Finding]) -> Iterator[Finding]:
    for finding in findings:
        if finding.evidence:
            # A parent cannot lend its key, authority or stage to an unrelated child.
            yield from _leaves(tuple(f for f in finding.evidence if f.key is finding.key))
        else:
            yield finding


def unsupported_workflow_requirements(
    ticket: Ticket, findings: Sequence[Finding], sources: Sequence[SourceRecord]
) -> tuple[tuple[FactKey, str], ...]:
    """Inspect all current, official constituent quotes without order-dependent trust.

    Qualify each leaf before consolidation so an other-stage/secondary constituent
    cannot inherit its sibling's provenance. Re-consolidation preserves conflicts.
    A stage label alone never waives a prerequisite or an outstanding track election;
    later-stage permission, form and timing still need their own verified findings.
    """
    qualified = tuple(
        guarded
        for leaf in _leaves(findings)
        if leaf.key in _REQUIREMENT_KEYS
        for guarded in verified_findings((leaf,), sources, ticket)
    )
    blocked: set[tuple[FactKey, str]] = set()
    for finding in _leaves(verified_findings(qualified, sources, ticket)):
        if not finding.value:
            continue
        quote = finding.verbatim_quote or ""
        clauses = re.split(r"[.!?;\n]+", quote.casefold())
        for clause in clauses:
            text = " ".join(clause.split())
            request_first = re.search(
                r"written statements?.*without.*hearing scheduled.*will not be read", text
            )
            if request_first or (_ELECTION.search(text) and "hearing" in text):
                blocked.add((finding.key, quote))
                break
    return tuple(sorted(blocked))
