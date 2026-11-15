"""Closed payment-action evidence persists without changing historical answer bytes."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from parkingbreaker.bench.runner import _record
from parkingbreaker.research.ladder import CustomerQuestion
from parkingbreaker.research.resolution import ResearchAnswers, answer_question
from parkingbreaker.research_records import (
    answers_json,
    decode_answers,
    decode_resolution,
    resolution_json,
)
from test_research_records import result_fixture

FIELDS = ("paid_fine", "paid_hearing_deposit", "entered_plan")


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("value", [True, False, None])
def test_closed_payment_answer_round_trips_as_fact_or_answered_unknown(
    field: str, value: bool | None
) -> None:
    question = CustomerQuestion[field.upper()]
    answers = answer_question(ResearchAnswers(), question, value)
    assert getattr(answers, field) is value
    assert question in answers.answered
    encoded = answers_json(answers)
    assert decode_answers(encoded) == answers
    resolution = replace(result_fixture().resolve(), customer_answers=answers)
    assert decode_resolution(resolution_json(resolution)).customer_answers == answers
    payload = json.loads(encoded)["payload"]
    bench = _record(answers)
    if value is None:
        assert field not in payload
        assert field not in bench
    else:
        assert payload[field] is value
        assert bench[field] is value
    bench_answers = bench["answered"]
    assert isinstance(bench_answers, list)
    assert field.upper() in bench_answers
    for other in set(FIELDS) - {field}:
        assert other not in payload


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("bad", [0, 1, "yes", "false", [], {}, 1.0])
def test_payment_answers_never_coerce_non_boolean_values(field: str, bad: object) -> None:
    with pytest.raises(ValueError, match="boolean"):
        ResearchAnswers(**{field: bad})  # type: ignore[arg-type]
    question = CustomerQuestion[field.upper()]
    with pytest.raises(ValueError, match="boolean"):
        answer_question(ResearchAnswers(), question, bad)  # type: ignore[arg-type]
    document = json.loads(answers_json(ResearchAnswers()))
    document["payload"][field] = bad
    with pytest.raises(ValueError, match="boolean"):
        decode_answers(json.dumps(document))


def test_absent_payment_answers_preserve_exact_legacy_bytes() -> None:
    expected = (
        '{"kind":"answers","payload":{"address_choice":null,"answered":[],"notice_issue_date":'
        'null,"printed_deadline":null,"prior_step_required":null},"version":1}'
    )
    assert answers_json(ResearchAnswers()) == expected
