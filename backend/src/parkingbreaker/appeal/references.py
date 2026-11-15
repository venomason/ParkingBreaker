"""Compatibility types for retired, non-selectable legal quotations.

No researched code quotations are offered for a customer's letter. Historical selection
IDs are rejected against the empty reference inventory rather than injected into prose.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from parkingbreaker.research.findings import Finding
from parkingbreaker.research.questions import FactKey
from parkingbreaker.research.sources import SourceRecord


@dataclass(frozen=True)
class Reference:
    id: str
    key: FactKey
    text: str
    quote: str
    source_url: str


class UnknownReference(ValueError):
    pass


def available_references(
    findings: Iterable[Finding], sources: Sequence[SourceRecord]
) -> tuple[Reference, ...]:
    """No selectable code quotations.

    Researched legal quotations are not offered, so nothing can be injected into the
    customer's letter. Confirmed findings stay in the research record for procedural
    requirements; they are not narrative material. The signature still takes findings and
    sources so callers and stored records keep working unchanged; historical saved
    selections are rejected rather than silently injected.
    """
    del findings, sources
    return ()


def select_references(
    available: Sequence[Reference], chosen_ids: Iterable[str]
) -> tuple[Reference, ...]:
    """The references the customer chose, in display order. Unknown ids raise."""
    chosen = set(chosen_ids)
    unknown = chosen - {r.id for r in available}
    if unknown:
        raise UnknownReference(f"not an available reference: {sorted(unknown)}")
    return tuple(r for r in available if r.id in chosen)
