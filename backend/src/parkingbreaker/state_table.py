"""Shared machinery for table-driven state machines. Pure.

A machine is a dict keyed by (state, event type) whose values are Rules: the target state
plus the side effects the I/O layer must perform. Anything missing from the table raises
IllegalTransition. case_state and payment_state both use this; there is no other
transition mechanism.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum


@dataclass(frozen=True)
class Rule[StateT: StrEnum, EffectT: StrEnum]:
    target: StateT
    effects: tuple[EffectT, ...] = ()


@dataclass(frozen=True)
class Transition[StateT: StrEnum, EventT, EffectT: StrEnum]:
    source: StateT
    event: EventT
    target: StateT
    effects: tuple[EffectT, ...]


class IllegalTransition(ValueError):
    """Raised when an event is not allowed in the current state."""

    def __init__(self, state: StrEnum, event: object) -> None:
        self.state = state
        self.event = event
        super().__init__(f"{type(event).__name__} is not allowed in state {state}")


type Table[StateT: StrEnum, EventT, EffectT: StrEnum] = Mapping[
    tuple[StateT, type[EventT]], Rule[StateT, EffectT]
]


def apply[StateT: StrEnum, EventT, EffectT: StrEnum](
    table: Table[StateT, EventT, EffectT], state: StateT, event: EventT
) -> Transition[StateT, EventT, EffectT]:
    """Return the transition for `event` in `state`, or raise IllegalTransition."""
    rule = table.get((state, type(event)))
    if rule is None:
        raise IllegalTransition(state, event)
    return Transition(state, event, rule.target, rule.effects)


def allowed[StateT: StrEnum, EventT, EffectT: StrEnum](
    table: Table[StateT, EventT, EffectT], state: StateT
) -> frozenset[type[EventT]]:
    """Event types accepted in `state`."""
    return frozenset(ev for (src, ev) in table if src is state)


def reachable[StateT: StrEnum, EventT, EffectT: StrEnum](
    table: Table[StateT, EventT, EffectT],
    start: StateT,
    avoid: frozenset[StateT] = frozenset(),
) -> frozenset[StateT]:
    """States reachable from `start` (inclusive) without entering any state in `avoid`."""
    seen = {start}
    queue = deque([start])
    while queue:
        current = queue.popleft()
        for (src, _), rule in table.items():
            if src is current and rule.target not in seen and rule.target not in avoid:
                seen.add(rule.target)
                queue.append(rule.target)
    return frozenset(seen)
