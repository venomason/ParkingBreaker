"""One fake provider boundary for privacy checks across drafting and research."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from parkingbreaker.json_types import JsonValue
from parkingbreaker.llm import Prompt, Usage, schema_json
from parkingbreaker.oplog.scrubber import identifier_spans
from parkingbreaker.research.fakes import FakeLlmClient


class PromptCapture(FakeLlmClient):
    def __init__(
        self,
        forbidden: Sequence[str],
        produce: Callable[[Prompt, int], JsonValue] = lambda *_: {},
    ) -> None:
        super().__init__({})
        self.forbidden = tuple(forbidden)
        self.produce = produce
        self.prompts: list[Prompt] = []

    def complete_json(
        self,
        system: str,
        user: str,
        schema: Mapping[str, JsonValue],
        *,
        max_cost_cents: int | None = None,
    ) -> tuple[JsonValue, Usage]:
        prompt = Prompt(system, user, schema)
        self.prompts.append(prompt)
        wire = "\n".join((system, user, schema_json(schema)))
        assert all(value.casefold() not in wire.casefold() for value in self.forbidden)
        assert not identifier_spans(
            wire, self.forbidden, include_generic=False, min_sensitive_chars=1
        ), "case identifier reached the fake AI provider"
        self.reply = self.produce(prompt, len(self.prompts))
        return super().complete_json(system, user, schema, max_cost_cents=max_cost_cents)
