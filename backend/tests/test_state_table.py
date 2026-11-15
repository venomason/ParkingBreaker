from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import pytest

from parkingbreaker.state_table import IllegalTransition, Rule, Table, allowed, apply, reachable


class Light(StrEnum):
    RED = "RED"
    GREEN = "GREEN"
    OFF = "OFF"


class Fx(StrEnum):
    BEEP = "BEEP"


@dataclass(frozen=True)
class Go:
    pass


@dataclass(frozen=True)
class Stop:
    pass


Ev = Go | Stop

TABLE: Table[Light, Ev, Fx] = {
    (Light.RED, Go): Rule(Light.GREEN, (Fx.BEEP,)),
    (Light.GREEN, Stop): Rule(Light.RED),
}


def test_apply_returns_target_and_effects() -> None:
    t = apply(TABLE, Light.RED, Go())
    assert (t.source, t.target, t.effects, t.event) == (Light.RED, Light.GREEN, (Fx.BEEP,), Go())


def test_apply_raises_outside_the_table() -> None:
    with pytest.raises(IllegalTransition, match="Stop is not allowed in state RED") as info:
        apply(TABLE, Light.RED, Stop())
    assert info.value.state is Light.RED
    assert info.value.event == Stop()


def test_allowed_lists_event_types_per_state() -> None:
    assert allowed(TABLE, Light.RED) == {Go}
    assert allowed(TABLE, Light.OFF) == frozenset()


def test_reachable_respects_avoid() -> None:
    assert reachable(TABLE, Light.RED) == {Light.RED, Light.GREEN}
    assert reachable(TABLE, Light.RED, avoid=frozenset({Light.GREEN})) == {Light.RED}
    assert reachable(TABLE, Light.OFF) == {Light.OFF}
