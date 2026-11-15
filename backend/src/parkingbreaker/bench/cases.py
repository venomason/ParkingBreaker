"""Strict, immutable answer-key rows and pure production-input builders (bench T4).

JSON uses arrays for tuples and enum values for enums. Draft review and absent 2.x
metadata may be null. Scripted dates use issue_offset_days (backward from the run
date) and printed_deadline_offset_days (forward, signed); a null answer spends the
question. The scripted issue date is the notice's date, which can differ from the
citation date (B16). Safe sets use canonical FAMILY[:S#] strings or SERVICEABLE.
Deadline alternatives contain days, trigger and counting, without nested alternatives.
No key evidence is treated as verified by parsing it; adjudication is a separate task.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from types import UnionType
from typing import Literal, TypeAliasType, Union, cast, get_args, get_origin, get_type_hints

from parkingbreaker.agencies import IssuerType
from parkingbreaker.case_state import HardStop, StopFamily
from parkingbreaker.research.jurisdiction import IssuerQuery
from parkingbreaker.research.ladder import CustomerQuestion
from parkingbreaker.research.questions import Ticket
from parkingbreaker.research.resolution import (
    ResearchAnswers,
    VehicleRelationship,
    answer_question,
)

type OutcomeFamily = StopFamily | Literal["SERVICEABLE"]
type Counting = Literal["postmark", "received"]
type KeyStatus = Literal["certain", "uncertain", "excluded"]


@dataclass(frozen=True)
class Source2x:
    file: str | None
    verification_source: str | None
    mail_policy_basis: str | None
    confidence_score: float | None
    filler_excerpt: bool


@dataclass(frozen=True)
class Notice:
    printed_issuer: str
    customer_city: str
    customer_state: str
    stage_text: str
    violation_code: str
    violation_description: str
    issue_offset_days: int
    printed_deadline_offset_days: int | None


@dataclass(frozen=True)
class ScriptedAnswers:
    issue_offset_days: int | None
    printed_deadline_offset_days: int | None
    prior_step_required: bool | None
    address_choice: str | None
    vehicle_relationship: VehicleRelationship | None


@dataclass(frozen=True)
class ForbiddenDestination:
    lines: tuple[str, ...]
    zip5: str
    purpose: str


@dataclass(frozen=True)
class Destination:
    lines: tuple[str, ...]
    zip5: str
    purpose: str
    stage: str


@dataclass(frozen=True)
class DeadlineRule:
    days: int
    trigger: str
    counting: Counting


@dataclass(frozen=True)
class Deadline:
    days: int
    trigger: str
    counting: Counting
    alternatives: tuple[DeadlineRule, ...]


@dataclass(frozen=True)
class Expected:
    issuer_name: str
    issuer_type: IssuerType
    deciding_body: str
    basis_key: str
    outcome_family: OutcomeFamily
    hard_stop: HardStop | None
    safe_set: tuple[str, ...]
    destination: Destination
    forbidden_destinations: tuple[ForbiddenDestination, ...]
    deadline: Deadline
    required_items: tuple[str, ...]
    required_declarations: tuple[str, ...]
    unfulfillable_items: tuple[str, ...]


@dataclass(frozen=True)
class Evidence:
    url: str
    retrieved_at: str
    sha256: str
    quote: str
    supports: tuple[str, ...]


@dataclass(frozen=True)
class KeyRow:
    case_id: str
    key_version: int
    reviewed_by: str | None
    reviewed_at: str | None
    owner_signed_at: str | None
    source_2x: Source2x
    notice: Notice
    scripted_answers: ScriptedAnswers
    expected: Expected
    evidence: tuple[Evidence, ...]
    status: KeyStatus
    uncertain_fields: tuple[str, ...]
    notes: str


def _decode(annotation: object, value: object, field: str) -> object:
    """Decode bench record JSON types; never coerce strings, bools or arrays."""
    if isinstance(annotation, TypeAliasType):
        return _decode(annotation.__value__, value, field)
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in (UnionType, Union):
        for option in args:
            try:
                return _decode(option, value, field)
            except ValueError:
                pass
    elif origin is Literal:
        if type(value) is str and value in args:
            return value
    elif origin is tuple:
        if isinstance(value, list):
            return tuple(_decode(args[0], item, f"{field}[{i}]") for i, item in enumerate(value))
    elif origin is list:
        if isinstance(value, list):
            return [_decode(args[0], item, f"{field}[{i}]") for i, item in enumerate(value)]
    elif origin is dict:
        if isinstance(value, dict):
            return {
                _decode(args[0], key, field): _decode(args[1], item, f"{field}.{key}")
                for key, item in value.items()
            }
    elif isinstance(annotation, type):
        if is_dataclass(annotation):
            if not isinstance(value, dict):
                raise ValueError(f"{field} must be an object")
            wanted = {f.name for f in fields(annotation)}
            missing = wanted - value.keys()
            unknown = value.keys() - wanted
            if missing or unknown:
                raise ValueError(
                    f"{field}: missing fields {sorted(missing)}, unknown fields {sorted(unknown)}"
                )
            hints = get_type_hints(annotation)
            values = {name: _decode(hints[name], value[name], f"{field}.{name}") for name in wanted}
            return cast(Callable[..., object], annotation)(**values)
        if issubclass(annotation, StrEnum):
            if type(value) is str:
                try:
                    return annotation(value)
                except ValueError:
                    pass
        elif annotation is date:
            if type(value) is str:
                try:
                    return date.fromisoformat(value)
                except ValueError:
                    pass
        elif annotation is float:
            if type(value) in (int, float):
                try:
                    number = float(cast(int | float, value))
                except OverflowError:
                    raise ValueError(f"{field} must be finite") from None
                if math.isfinite(number):
                    return number
        elif type(value) is annotation:
            return value
    raise ValueError(f"{field}: invalid value for {annotation}")


def _timestamp(value: str | None, field: str) -> None:
    if value is not None:
        try:
            datetime.fromisoformat(value)
        except ValueError:
            raise ValueError(f"{field} must be an ISO datetime") from None


def _validate(row: KeyRow) -> None:
    if not row.case_id.strip():
        raise ValueError("case_id must not be blank")
    if row.key_version < 1:
        raise ValueError("key_version must be positive")
    for field in ("reviewed_at", "owner_signed_at"):
        _timestamp(getattr(row, field), field)
    score = row.source_2x.confidence_score
    if score is not None and not 0 <= score <= 1:
        raise ValueError("source_2x.confidence_score must be between 0 and 1")
    # Validate the same closed location inputs the production query accepts.
    for field in ("printed_issuer", "customer_city", "stage_text"):
        if not cast(str, getattr(row.notice, field)).strip():
            raise ValueError(f"notice.{field} must not be blank")
    if not re.fullmatch(r"[A-Z]{2}", row.notice.customer_state):
        raise ValueError("notice.customer_state must be two uppercase letters")
    for field, offset in (
        ("notice.issue_offset_days", row.notice.issue_offset_days),
        ("scripted_answers.issue_offset_days", row.scripted_answers.issue_offset_days),
    ):
        if offset is not None and offset < 0:
            raise ValueError(f"{field} must be nonnegative")
    if (
        row.scripted_answers.address_choice is not None
        and not row.scripted_answers.address_choice.strip()
    ):
        raise ValueError("scripted_answers.address_choice must not be blank")
    for i, entry in enumerate(row.evidence):
        if not re.fullmatch(r"[0-9a-fA-F]{64}", entry.sha256):
            raise ValueError(f"evidence[{i}].sha256 must be 64 hex digits")
        _timestamp(entry.retrieved_at, f"evidence[{i}].retrieved_at")
    for i, outcome in enumerate(row.expected.safe_set):
        family, separator, stop = outcome.partition(":")
        try:
            if family != "SERVICEABLE":
                StopFamily(family)
            if separator:
                HardStop(stop)
                if family == "SERVICEABLE":
                    raise ValueError
        except ValueError:
            raise ValueError(f"expected.safe_set[{i}] must be FAMILY[:S#] or SERVICEABLE") from None
    destinations: tuple[tuple[str, Destination | ForbiddenDestination], ...] = (
        ("expected.destination", row.expected.destination),
        *(
            (f"expected.forbidden_destinations[{i}]", item)
            for i, item in enumerate(row.expected.forbidden_destinations)
        ),
    )
    for field, destination in destinations:
        if not re.fullmatch(r"[0-9]{5}", destination.zip5):
            raise ValueError(f"{field}.zip5 must be five digits")
    rules: tuple[tuple[str, Deadline | DeadlineRule], ...] = (
        ("expected.deadline", row.expected.deadline),
        *(
            (f"expected.deadline.alternatives[{i}]", item)
            for i, item in enumerate(row.expected.deadline.alternatives)
        ),
    )
    for field, rule in rules:
        if rule.days < 1:
            raise ValueError(f"{field}.days must be positive")


def _unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for field, value in pairs:
        if field in result:
            raise ValueError(f"{field}: duplicate field")
        result[field] = value
    return result


def parse_answer_key(jsonl_text: str) -> tuple[KeyRow, ...]:
    """Parse already-read JSONL, ignoring blank lines; errors name physical line and field."""
    rows: list[KeyRow] = []
    seen: set[str] = set()
    for line_number, line in enumerate(jsonl_text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            raw: object = json.loads(line, object_pairs_hook=_unique_fields)
            row = cast(KeyRow, _decode(KeyRow, raw, "row"))
            _validate(row)
            if row.case_id in seen:
                raise ValueError("case_id: duplicate id")
        except ValueError as exc:
            raise ValueError(f"line {line_number}: {exc}") from exc
        seen.add(row.case_id)
        rows.append(row)
    return tuple(rows)


def build_issuer_query(row: KeyRow, run_date: date) -> IssuerQuery:
    """The production query has no date field; run_date keeps the common builder API."""
    notice = row.notice
    return IssuerQuery(notice.printed_issuer, notice.customer_city, notice.customer_state)


def build_ticket(row: KeyRow, run_date: date) -> Ticket:
    notice = row.notice
    return Ticket(
        f"{notice.customer_city} {notice.customer_state}",
        notice.violation_code,
        notice.violation_description,
        run_date - timedelta(days=notice.issue_offset_days),
    )


def build_scripted_answers(row: KeyRow, run_date: date) -> ResearchAnswers:
    """Build customer answers only from notice/script, including explicitly declined answers."""
    scripted = row.scripted_answers
    issue = scripted.issue_offset_days
    deadline = scripted.printed_deadline_offset_days
    answers = ResearchAnswers()
    for question, value in (
        (
            CustomerQuestion.NOTICE_ISSUE_DATE,
            None if issue is None else run_date - timedelta(days=issue),
        ),
        (
            CustomerQuestion.NOTICE_PRINTED_DEADLINE,
            None if deadline is None else run_date + timedelta(days=deadline),
        ),
        (CustomerQuestion.NOTICE_PRIOR_STEP, scripted.prior_step_required),
        (CustomerQuestion.NOTICE_ADDRESS_CHOICE, scripted.address_choice),
        (CustomerQuestion.VEHICLE_RELATIONSHIP, scripted.vehicle_relationship),
        (CustomerQuestion.NOTICE_ISSUING_AGENCY, row.notice.printed_issuer),
        (CustomerQuestion.NOTICE_STAGE, row.notice.stage_text),
    ):
        answers = answer_question(answers, question, value)
    return answers
