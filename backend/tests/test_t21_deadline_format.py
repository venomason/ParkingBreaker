"""T21: the DEADLINE_DAYS question must describe the period shapes the engine accepts.

value_format is the extractor's only instruction about a fact's value shape, and
findings.validate_value enforces that shape. The engine accepts a plain calendar-day
integer or a {days, unit, time_cutoff} period object; a prompt that forbids one of
those shapes makes the extractor withhold a usable fact as UNKNOWN.
"""

from __future__ import annotations

import re

from parkingbreaker.json_types import JsonValue
from parkingbreaker.research.findings import validate_value
from parkingbreaker.research.questions import FactKey, build_questions
from test_research_questions import TICKET

K = FactKey
BUSINESS_PERIOD: JsonValue = {"days": 30, "unit": "BUSINESS", "time_cutoff": False}
CUTOFF_PERIOD: JsonValue = {"days": 30, "unit": "CALENDAR", "time_cutoff": True}


def _deadline_value_format() -> str:
    return next(q for q in build_questions(TICKET) if q.key is K.DEADLINE_DAYS).value_format


def _forbids(fmt: str, *terms: str) -> bool:
    """True when a clause calls one of `terms` unsupported or UNKNOWN, i.e. forbids it."""
    return re.search(rf"({'|'.join(terms)})[^.;]*\b(unsupported|UNKNOWN)\b", fmt, re.I) is not None


def test_deadline_question_value_format_permits_business_periods() -> None:
    assert validate_value(K.DEADLINE_DAYS, BUSINESS_PERIOD) is None
    fmt = _deadline_value_format()
    assert "BUSINESS" in fmt
    assert "CALENDAR" in fmt
    assert not _forbids(fmt, "business", "working")


def test_deadline_question_value_format_permits_stated_cutoff() -> None:
    assert validate_value(K.DEADLINE_DAYS, CUTOFF_PERIOD) is None
    fmt = _deadline_value_format()
    assert "time_cutoff" in fmt
    assert not _forbids(fmt, "time_cutoff", "cutoff")
