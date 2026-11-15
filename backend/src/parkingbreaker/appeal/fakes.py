"""In-memory fakes for the drafting seam. For tests only."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from parkingbreaker.appeal.drafting import Draft, DraftResult, TicketFacts
from parkingbreaker.appeal.references import Reference
from parkingbreaker.llm import Usage


@dataclass
class FakeDrafter:
    """Returns drafts from a function of (attempt number, problems from the last attempt)."""

    produce: Callable[[int, Sequence[str]], Draft]
    usage: Usage = field(default_factory=Usage)
    calls: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)

    def draft(
        self,
        notes: str,
        references: Sequence[Reference],
        facts: TicketFacts,
        problems: Sequence[str] = (),
    ) -> DraftResult:
        self.calls.append((notes, tuple(problems)))
        return DraftResult(self.produce(len(self.calls), problems), self.usage)
