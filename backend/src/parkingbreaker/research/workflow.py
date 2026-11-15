"""Bounded workflow capabilities derived from guarded official requirements.

These are product limits, not city exclusions or new model-inferred facts. Unknown
wording remains subject to the existing requirement ladder. No court track is chosen.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from parkingbreaker.research.findings import Finding
from parkingbreaker.research.fulfilment import verified_findings
from parkingbreaker.research.questions import FactKey, Ticket
from parkingbreaker.research.sources import SourceRecord

_REQUIREMENT_KEYS = frozenset(
    {FactKey.PRIOR_STEP_REQUIREMENT, FactKey.TICKET_SPECIFIC_REQUIREMENTS, FactKey.REQUIRED_ITEMS}
)


def unsupported_workflow_requirements(
    ticket: Ticket, findings: Sequence[Finding], sources: Sequence[SourceRecord]
) -> tuple[tuple[FactKey, str], ...]:
    """Return evidenced requirements a single customer-authored mailing cannot meet.

    Accept only findings that survive existing quote, authority and issuer/stage
    guards. A scheduled statement is a separate workflow and must independently
    prove its own form and timing; an initial-request instruction never supplies them.
    """
    stage = (ticket.stage or "").casefold()
    scheduled = bool(re.search(r"\bscheduled (?:hearing|conference)\b", stage)) and not bool(
        re.search(r"\b(?:no|not|un)\w*.*scheduled", stage)
    )
    blocked = []
    for finding in verified_findings(findings, sources, ticket):
        if finding.key not in _REQUIREMENT_KEYS or not finding.value:
            continue
        quote = finding.verbatim_quote or ""
        text = " ".join(quote.casefold().split())
        # The first mailing only schedules a hearing; it is not a merits statement.
        request_first = bool(
            re.search(r"written statements?.*without.*hearing scheduled.*will not be read", text)
        )
        # The agency explicitly asks the customer to elect a mutually exclusive track.
        election = (
            "mitigation" in text
            and "contested" in text
            and bool(
                re.search(
                    r"\b(?:must|shall|required to)\s+(?:first\s+)?(?:choose|select|elect)\b"
                    r"|(?:^|[.!?]\s+)check the box\b",
                    text,
                )
            )
        )
        if not scheduled and (request_first or election):
            blocked.append((finding.key, quote))
    return tuple(blocked)
