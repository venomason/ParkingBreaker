from __future__ import annotations

import copy
import json
import re
from collections.abc import Iterator
from dataclasses import FrozenInstanceError, asdict
from datetime import date
from pathlib import Path
from typing import cast

import pytest

from parkingbreaker.agencies import IssuerType
from parkingbreaker.bench.cases import (
    KeyRow,
    build_issuer_query,
    build_scripted_answers,
    build_ticket,
    parse_answer_key,
)
from parkingbreaker.case_state import HardStop, StopFamily
from parkingbreaker.research.ladder import CustomerQuestion
from parkingbreaker.research.resolution import ResearchAnswers, VehicleRelationship

type PathParts = tuple[str | int, ...]


@pytest.fixture
def sample_text() -> str:
    return (Path(__file__).parent / "fixtures" / "bench_b16.jsonl").read_text()


@pytest.fixture
def sample(sample_text: str) -> dict[str, object]:
    return cast(dict[str, object], json.loads(sample_text))


def _children(value: object, path: PathParts = ()) -> Iterator[tuple[PathParts, object]]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield (*path, key), child
            yield from _children(child, (*path, key))
    elif isinstance(value, list):
        for i, child in enumerate(value):
            yield (*path, i), child
            yield from _children(child, (*path, i))


def _parent(document: dict[str, object], path: PathParts) -> dict[str, object] | list[object]:
    current: object = document
    for part in path[:-1]:
        if isinstance(part, str):
            current = cast(dict[str, object], current)[part]
        else:
            current = cast(list[object], current)[part]
    assert isinstance(current, (dict, list))
    return current


def _set(document: dict[str, object], path: PathParts, value: object) -> None:
    parent = _parent(document, path)
    if isinstance(parent, dict):
        assert isinstance(path[-1], str)
        parent[path[-1]] = value
    else:
        assert isinstance(path[-1], int)
        parent[path[-1]] = value


def test_sample_round_trips_all_values(sample_text: str, sample: dict[str, object]) -> None:
    (row,) = parse_answer_key(sample_text)
    assert isinstance(row, KeyRow)
    assert json.loads(json.dumps(asdict(row))) == sample
    assert parse_answer_key(json.dumps(asdict(row))) == (row,)
    assert row.expected.issuer_type is IssuerType.MUNICIPAL
    assert row.scripted_answers.vehicle_relationship is VehicleRelationship.REGISTERED_OWNER
    assert row.expected.deadline.alternatives[0].days == 21
    assert isinstance(row.expected.destination.lines, tuple)
    with pytest.raises(FrozenInstanceError):
        row.notes = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        row.notice.customer_city = "changed"  # type: ignore[misc]


def test_every_missing_field_is_rejected(sample: dict[str, object]) -> None:
    for path, _ in _children(sample):
        if not isinstance(path[-1], str):
            continue
        malformed = copy.deepcopy(sample)
        parent = _parent(malformed, path)
        assert isinstance(parent, dict)
        del parent[path[-1]]
        with pytest.raises(ValueError, match=rf"line 2:.*{re.escape(path[-1])}"):
            parse_answer_key("\n" + json.dumps(malformed))


def test_unknown_fields_at_every_object_are_rejected(sample: dict[str, object]) -> None:
    for path, value in [((), sample), *_children(sample)]:
        if not isinstance(value, dict):
            continue
        malformed = copy.deepcopy(sample)
        if not path:
            malformed["unexpected_field"] = 1
        else:
            _set(malformed, path, {**value, "unexpected_field": 1})
        with pytest.raises(ValueError, match=r"line 1:.*unexpected_field"):
            parse_answer_key(json.dumps(malformed))


def test_wrong_types_at_every_field_and_array_element_are_rejected(
    sample: dict[str, object],
) -> None:
    for path, value in _children(sample):
        malformed = copy.deepcopy(sample)
        _set(malformed, path, [] if isinstance(value, dict) else {})
        field = next(part for part in reversed(path) if isinstance(part, str))
        with pytest.raises(ValueError, match=rf"line 1:.*{re.escape(field)}"):
            parse_answer_key(json.dumps(malformed))


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("key_version",), True),
        (("key_version",), 1.0),
        (("key_version",), 0),
        (("case_id",), " "),
        (("status",), "reviewed"),
        (("notice", "issue_offset_days"), True),
        (("notice", "issue_offset_days"), -1),
        (("notice", "printed_deadline_offset_days"), False),
        (("notice", "customer_state"), "California"),
        (("scripted_answers", "prior_step_required"), 1),
        (("scripted_answers", "issue_offset_days"), False),
        (("scripted_answers", "vehicle_relationship"), "owner"),
        (("scripted_answers", "address_choice"), " "),
        (("source_2x", "filler_excerpt"), 1),
        (("source_2x", "confidence_score"), True),
        (("source_2x", "confidence_score"), float("nan")),
        (("source_2x", "confidence_score"), float("inf")),
        (("source_2x", "confidence_score"), 1.1),
        (("expected", "outcome_family"), "SERV"),
        (("expected", "hard_stop"), "S8"),
        (("expected", "issuer_type"), "city"),
        (("expected", "safe_set", 0), "NYV:S2"),
        (("expected", "safe_set", 0), "NOT_YET_VERIFIED:S8"),
        (("expected", "safe_set", 0), "SERVICEABLE:S1"),
        (("expected", "deadline", "days"), True),
        (("expected", "deadline", "days"), 0),
        (("expected", "deadline", "counting"), "POSTMARK"),
        (("expected", "deadline", "alternatives", 0, "counting"), "receipt"),
        (("expected", "destination", "zip5"), "92112-9038"),
        (("expected", "forbidden_destinations", 0, "zip5"), "1234"),
        (("evidence", 0, "sha256"), "a" * 63),
        (("evidence", 0, "sha256"), "g" * 64),
        (("evidence", 0, "sha256"), "a" * 64 + "\n"),
        (("evidence", 0, "retrieved_at"), "yesterday"),
        (("reviewed_at",), "yesterday"),
        (("owner_signed_at",), "yesterday"),
    ],
)
def test_malformed_values_name_line_and_field(
    sample: dict[str, object], path: PathParts, value: object
) -> None:
    _set(sample, path, value)
    field = next(part for part in reversed(path) if isinstance(part, str))
    with pytest.raises(ValueError, match=rf"line 2:.*{re.escape(field)}"):
        parse_answer_key("\n" + json.dumps(sample))


def test_builders_use_supplied_date_and_notice_only(sample_text: str) -> None:
    (row,) = parse_answer_key(sample_text)
    run_date = date(2026, 3, 1)
    query = build_issuer_query(row, run_date)
    assert (query.printed_issuer, query.customer_city, query.customer_state) == (
        row.notice.printed_issuer,
        "San Diego",
        "CA",
    )
    ticket = build_ticket(row, run_date)
    assert ticket.city_id == "San Diego CA"
    assert ticket.issue_date == date(2026, 1, 30)
    assert ticket.violation_code == row.notice.violation_code
    assert ticket.violation_description == row.notice.violation_description
    answers = build_scripted_answers(row, run_date)
    assert isinstance(answers, ResearchAnswers)
    assert answers.notice_issue_date == date(2026, 2, 22)
    assert answers.printed_deadline == date(2026, 3, 8)
    assert answers.prior_step_required is False
    assert answers.address_choice is None
    assert answers.vehicle_relationship is VehicleRelationship.REGISTERED_OWNER
    assert answers.issuing_agency == row.notice.printed_issuer
    assert answers.notice_stage == row.notice.stage_text
    assert answers.answered == frozenset(
        {
            CustomerQuestion.NOTICE_ISSUE_DATE,
            CustomerQuestion.NOTICE_PRINTED_DEADLINE,
            CustomerQuestion.NOTICE_PRIOR_STEP,
            CustomerQuestion.NOTICE_ADDRESS_CHOICE,
            CustomerQuestion.VEHICLE_RELATIONSHIP,
            CustomerQuestion.NOTICE_ISSUING_AGENCY,
            CustomerQuestion.NOTICE_STAGE,
        }
    )
    later = build_scripted_answers(row, date(2028, 3, 1))
    assert later.notice_issue_date == date(2028, 2, 23)
    assert build_ticket(row, date(2028, 3, 1)).issue_date == date(2028, 1, 31)


def test_null_and_past_deadline_answers(sample: dict[str, object]) -> None:
    for field in (
        "issue_offset_days",
        "printed_deadline_offset_days",
        "prior_step_required",
        "address_choice",
        "vehicle_relationship",
    ):
        _set(sample, ("scripted_answers", field), None)
    (row,) = parse_answer_key(json.dumps(sample))
    answers = build_scripted_answers(row, date(2026, 1, 1))
    assert answers.notice_issue_date is None
    assert answers.printed_deadline is None
    assert answers.prior_step_required is None
    assert answers.vehicle_relationship is None
    assert CustomerQuestion.NOTICE_PRINTED_DEADLINE in answers.answered
    _set(sample, ("scripted_answers", "printed_deadline_offset_days"), -2)
    _set(sample, ("scripted_answers", "prior_step_required"), True)
    _set(sample, ("scripted_answers", "address_choice"), "Printed address")
    (row,) = parse_answer_key(json.dumps(sample))
    answers = build_scripted_answers(row, date(2026, 1, 1))
    assert answers.printed_deadline == date(2025, 12, 30)
    assert answers.prior_step_required is True
    assert answers.address_choice == "Printed address"


@pytest.mark.parametrize("status", ["certain", "uncertain", "excluded"])
def test_all_statuses_parse(sample: dict[str, object], status: str) -> None:
    sample["status"] = status
    (row,) = parse_answer_key(json.dumps(sample))
    assert row.status == status


@pytest.mark.parametrize("family", list(StopFamily))
@pytest.mark.parametrize("stop", list(HardStop))
def test_existing_outcome_enums_parse(
    sample: dict[str, object], family: StopFamily, stop: HardStop
) -> None:
    _set(sample, ("expected", "outcome_family"), family.value)
    _set(sample, ("expected", "hard_stop"), stop.value)
    (row,) = parse_answer_key(json.dumps(sample))
    assert row.expected.outcome_family is family
    assert row.expected.hard_stop is stop


def test_duplicate_ids_name_physical_line(sample_text: str) -> None:
    with pytest.raises(ValueError, match=r"line 3:.*case_id"):
        parse_answer_key(sample_text.strip() + "\n\n" + sample_text)


def test_multiple_distinct_rows_and_empty_input(
    sample: dict[str, object], sample_text: str
) -> None:
    sample["case_id"] = "B17"
    assert [row.case_id for row in parse_answer_key(sample_text + json.dumps(sample))] == [
        "B16",
        "B17",
    ]
    assert parse_answer_key("\n \n") == ()


def test_duplicate_object_fields_are_rejected(sample_text: str) -> None:
    malformed = sample_text.replace('"case_id":"B16"', '"case_id":"B16","case_id":"B17"')
    with pytest.raises(ValueError, match=r"line 1:.*case_id"):
        parse_answer_key(malformed)


@pytest.mark.parametrize("text", ["{", "[]", "null", "true", '"row"'])
def test_malformed_or_nonobject_rows_are_rejected(text: str) -> None:
    with pytest.raises(ValueError, match="line 2:"):
        parse_answer_key("\n" + text)


def test_sf_answer_key_parses_with_six_launch_rows() -> None:
    path = Path(__file__).parents[2] / "docs/bench/sf-answer-key.jsonl"
    rows = parse_answer_key(path.read_text(encoding="utf-8"))
    assert [row.case_id for row in rows] == ["S01", "S02", "S03", "S04", "S05", "S06"]
    assert all(row.owner_signed_at is None for row in rows)
    for row in rows:
        assert all(e.url.startswith("https://www.sfmta.com/") for e in row.evidence)
