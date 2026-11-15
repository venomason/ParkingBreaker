from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date

from parkingbreaker.research.ladder import CriticalFact
from parkingbreaker.research.questions import (
    CRITICAL_FACT_FOR,
    CRITICAL_KEYS,
    DEFAULT_TIERS,
    RETIRED_KEYS,
    FactKey,
    Ticket,
    build_questions,
)

TICKET = Ticket("us-ca-san_francisco", "TC 7.2.22", "Street cleaning", date(2026, 10, 1))


def test_critical_and_non_critical_keys() -> None:
    assert set(FactKey) - CRITICAL_KEYS == {
        FactKey.GOVERNING_CODE_SECTION,
        FactKey.FINE_AMOUNT,
        FactKey.CODE_EXCEPTIONS,
    }


def test_critical_fact_mapping_covers_researched_critical_facts() -> None:
    assert frozenset(CRITICAL_FACT_FOR) == CRITICAL_KEYS
    assert all(isinstance(fact, CriticalFact) for fact in CRITICAL_FACT_FOR.values())
    assert set(CRITICAL_FACT_FOR.values()) == set(CriticalFact)


def test_one_question_per_key_in_order() -> None:
    questions = build_questions(TICKET)
    # Retired keys are never asked; they stay in the enum for historical decoding.
    assert [q.key for q in questions] == [k for k in FactKey if k not in RETIRED_KEYS]
    assert all(q.critical == (q.key in CRITICAL_KEYS) for q in questions)
    assert all(q.tier is DEFAULT_TIERS[q.key] for q in questions)
    assert all(q.ask.endswith("?") and q.value_format for q in questions)


def test_questions_mention_the_ticket() -> None:
    by_key = {q.key: q for q in build_questions(TICKET)}
    assert "TC 7.2.22 (Street cleaning)" in by_key[FactKey.GOVERNING_CODE_SECTION].ask
    assert "us-ca-san_francisco" in by_key[FactKey.MAILING_ADDRESS].ask
    assert "2026-10-01" in by_key[FactKey.DEADLINE_DAYS].ask


def test_questions_are_plain_data() -> None:
    questions = build_questions(TICKET)
    assert json.loads(json.dumps([asdict(q) for q in questions]))
    assert build_questions(TICKET) == questions  # deterministic


def test_exhaustive_default_tier_registry_and_stage_scoped_questions() -> None:
    from parkingbreaker.research.questions import FactTier

    assert set(DEFAULT_TIERS) == set(FactKey)
    assert all(isinstance(tier, FactTier) for tier in DEFAULT_TIERS.values())
    assert {k for k, tier in DEFAULT_TIERS.items() if tier is FactTier.MUST_HAVE} == CRITICAL_KEYS
    # Retired defence key: present so historical records decode, never researched.
    assert {k for k, tier in DEFAULT_TIERS.items() if tier is FactTier.IMPORTANT} == {
        FactKey.GOVERNING_CODE_SECTION,
    }
    assert {k for k, tier in DEFAULT_TIERS.items() if tier is FactTier.NICE_TO_HAVE} == {
        FactKey.FINE_AMOUNT,
        FactKey.CODE_EXCEPTIONS,
    }
    questions = build_questions(TICKET, jurisdiction="resolved-agency", stage="second review")
    assert len(questions) == len({q.key for q in questions}) == len(set(FactKey) - RETIRED_KEYS)
    assert RETIRED_KEYS.isdisjoint({q.key for q in questions})
    assert all("resolved-agency" in q.ask and "second review" in q.ask for q in questions)
    assert all(TICKET.violation_code in q.ask for q in questions)
    assert TICKET.jurisdiction is TICKET.stage is None  # legacy constructor preserved
