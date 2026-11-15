from __future__ import annotations

import pytest

from parkingbreaker.llm import LlmBudgetExceeded, LlmClient, Usage
from parkingbreaker.research.fakes import FakeLlmClient


@pytest.mark.parametrize("ceiling", [-1, -25])
def test_negative_ceiling_rejected_without_request(ceiling: int) -> None:
    fake = FakeLlmClient({"answer": "must not return"}, Usage(10, 20, 3))
    client: LlmClient = fake
    with pytest.raises(ValueError, match="max_cost_cents"):
        client.complete_json("system", "user", {}, max_cost_cents=ceiling)
    assert fake.calls == []
    assert fake.ceilings == [ceiling]


@pytest.mark.parametrize("cost", [0, 3])
def test_zero_ceiling_always_refused_without_request(cost: int) -> None:
    fake = FakeLlmClient({"answer": "must not return"}, Usage(10, 20, cost))
    client: LlmClient = fake
    with pytest.raises(LlmBudgetExceeded):
        client.complete_json("system", "user", {}, max_cost_cents=0)
    assert fake.calls == []
    assert fake.ceilings == [0]


def test_none_and_omitted_ceiling_preserve_reply_and_usage() -> None:
    fake = FakeLlmClient({"answer": "ok"}, Usage(10, 20, 30))
    client: LlmClient = fake
    assert client.complete_json("system", "user", {}) == (fake.reply, fake.usage)
    assert client.complete_json("system", "user", {}, max_cost_cents=None) == (
        fake.reply,
        fake.usage,
    )
    assert fake.calls == [("system", "user")] * 2
    assert fake.ceilings == [None, None]


@pytest.mark.parametrize("ceiling", [3, 4])
def test_fitting_cost_is_allowed(ceiling: int) -> None:
    fake = FakeLlmClient({"answer": "ok"}, Usage(10, 20, 3))
    assert fake.complete_json("system", "user", {}, max_cost_cents=ceiling) == (
        fake.reply,
        fake.usage,
    )
    assert fake.calls == [("system", "user")]
    assert fake.ceilings == [ceiling]


def test_over_ceiling_cost_refused_without_request_or_data() -> None:
    fake = FakeLlmClient({"answer": "must not return"}, Usage(10, 20, 8))
    with pytest.raises(LlmBudgetExceeded):
        fake.complete_json("system", "user", {}, max_cost_cents=7)
    assert fake.calls == []
    assert fake.ceilings == [7]
