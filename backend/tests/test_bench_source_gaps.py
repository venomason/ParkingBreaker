"""Offline regression contract for source-gap corrections, not live qualification."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from parkingbreaker.bench.cases import KeyRow, parse_answer_key
from parkingbreaker.bench.key_review import audit_key_row, load_saved_pages, render_review_packet
from parkingbreaker.bench.score import score_run
from test_bench_score import fact, finding, run

ROOT = Path(__file__).resolve().parents[2]
SOURCES = ROOT / "build-kit/evidence/benchmark-source-gaps-2026-11-14"
PAGES = ROOT / "build-kit/evidence/bench-pages"


def row(case_id: str) -> KeyRow:
    return next(
        r
        for r in parse_answer_key((ROOT / "docs/bench/answer-key.jsonl").read_text())
        if r.case_id == case_id
    )


@pytest.mark.parametrize("case_id", ["B01", "B03", "B06", "B15"])
def test_partial_evidence_never_reports_source_gap_ready(case_id: str) -> None:
    current = row(case_id)
    audit = audit_key_row(current, load_saved_pages((PAGES,)))
    assert current.owner_signed_at is None
    assert current.status == "uncertain"
    assert not audit.ready_for_signoff
    assert audit.blocking_reasons
    if case_id != "B01":
        assert audit.evidence_complete
    packet = render_review_packet((audit,), (current,))
    assert "**Ready for sign-off:** `NO`" in packet


@pytest.mark.parametrize("box", ["P.O. Box 30247", "P.O. Box 30420"])
def test_ladot_conflicting_box_is_neither_forbidden_nor_certified(box: str) -> None:
    current = row("B01")
    address = finding(
        "MAILING_ADDRESS",
        {
            "recipient": "Parking Violations Bureau",
            "attention": None,
            "line1": box,
            "line2": None,
            "city": "Los Angeles",
            "state": "CA",
            "zip_code": "90030",
        },
    )
    value = replace(run("B01"), resolution={"mailing_address": address})
    score = score_run(value, current)
    assert "forbidden destination" not in score.notes
    assert score.destination_correct is None
    assert score.purpose_correct is None
    assert score.expected_family is None
    assert current.expected.outcome_family == "NOT_YET_VERIFIED"
    assert "PROCESS_INCOMPATIBLE:S3" not in current.expected.safe_set
    assert score.unfulfillable_items_clear is None


def test_seattle_later_statement_receipt_is_not_an_initial_deadline() -> None:
    current = row("B03")
    initial = fact(run("B03"), "DEADLINE_DAYS", 30)
    initial = replace(
        initial,
        resolution={
            "counting": {"rule": "POSTMARK"},
            "deadline": {"deadline": "2026-11-06"},
        },
    )
    assert score_run(initial, current).deadline_correct is True
    later = fact(initial, "DEADLINE_DAYS", 3)
    later = fact(later, "DEADLINE_CLOCK_START", "SCHEDULED_PREHEARING_CONFERENCE")
    later = replace(
        later,
        resolution={
            "counting": {"rule": "RECEIPT"},
            "deadline": {"deadline": "2026-11-06"},
        },
    )
    assert score_run(later, current).deadline_correct is False
    assert current.expected.deadline.alternatives[0].days == 33
    assert current.expected.deadline.alternatives[0].counting == "postmark"
    assert current.expected.outcome_family == "PROCESS_INCOMPATIBLE"
    assert current.expected.destination.purpose == "initial hearing request"
    for evidence in current.evidence:
        if evidence.url.endswith("/hearings-by-mail"):
            assert evidence.supports == ("notes",)


@pytest.mark.parametrize(("case_id", "days"), [("B06", 7), ("B15", 15), ("B15", 30)])
def test_legacy_placeholders_and_payment_period_do_not_become_verified(
    case_id: str,
    days: int,
) -> None:
    current = row(case_id)
    value = fact(run(case_id), "DEADLINE_DAYS", days)
    score = score_run(value, current)
    assert score.verdict == "wrong SERVICEABLE"
    assert score.destination_correct is None
    assert score.purpose_correct is None
    assert score.deadline_correct is None
    assert score.expected_family == "NOT_YET_VERIFIED"
    assert current.expected.outcome_family == "NOT_YET_VERIFIED"
    assert "SERVICEABLE" not in current.expected.safe_set


def test_payment_and_office_leads_do_not_claim_complete_postal_evidence() -> None:
    austin = row("B06")
    payment = next(e for e in austin.evidence if "2135" in e.quote)
    assert payment.supports == ("notes",)
    assert "payment" in austin.expected.destination.purpose
    assert austin.expected.deadline.trigger == "PRINTED_DEADLINE"
    omaha = row("B15")
    assert "Supervisor, City Cashier" in omaha.evidence[0].quote
    assert all(
        "expected.destination" not in e.supports and "expected.deadline" not in e.supports
        for e in omaha.evidence
    )


def test_imported_evidence_preserves_complete_raw_and_text_identities() -> None:
    records = json.loads((SOURCES / "captures.json").read_text())["captures"]
    imported = {
        "seattle-hearings",
        "wa-response-rule",
        "austin-civil",
        "austin-faq",
        "omaha-citations",
    }
    for capture in records:
        if capture["id"] not in imported:
            continue
        assert capture["usable_as_page_evidence"]
        assert capture.get("complete", True)
        raw = (SOURCES / capture["raw_file"]).read_bytes()
        text = (SOURCES / capture["text_file"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == capture["raw_sha256"]
        assert hashlib.sha256(text).hexdigest() == capture["text_sha256"]
        assert (PAGES / (capture["raw_sha256"] + ".txt")).read_bytes() == text
