from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest

from parkingbreaker.agencies import IssuerType
from parkingbreaker.bench.cases import (
    Deadline,
    DeadlineRule,
    Destination,
    Expected,
    ForbiddenDestination,
    KeyRow,
    Notice,
    ScriptedAnswers,
    Source2x,
)
from parkingbreaker.bench.report import render_markdown
from parkingbreaker.bench.runner import CaseRun
from parkingbreaker.bench.score import Measure, RunScore, _deadline_date, aggregate, score_run
from parkingbreaker.case_state import HardStop, StopFamily
from parkingbreaker.json_types import JsonValue
from parkingbreaker.mail_timing import ClockStart, deadline_date

TODAY = date(2026, 10, 14)
ADDRESS: dict[str, JsonValue] = {
    "recipient": "Citation Review",
    "attention": None,
    "line1": "P.O. Box 123",
    "line2": None,
    "city": "Example",
    "state": "CA",
    "zip_code": "90001-1234",
}
QUOTE = "Mail your initial review to Citation Review, P.O. Box 123, Example CA 90001-1234."


def key(case_id: str = "B01") -> KeyRow:
    return KeyRow(
        case_id=case_id,
        key_version=1,
        reviewed_by="reviewer",
        reviewed_at="2026-10-13T12:00:00Z",
        owner_signed_at=None,
        source_2x=Source2x(None, None, None, None, False),
        notice=Notice("Example City", "Example", "CA", "citation", "PARK", "parking", 10, None),
        scripted_answers=ScriptedAnswers(4, None, False, None, None),
        expected=Expected(
            issuer_name="Example City",
            issuer_type=IssuerType.MUNICIPAL,
            deciding_body="Review Office",
            basis_key="CA",
            outcome_family="SERVICEABLE",
            hard_stop=None,
            safe_set=("SERVICEABLE", "NOT_YET_VERIFIED:S2"),
            destination=Destination(
                ("Citation Review", "P.O. Box 123", "Example CA"),
                "90001",
                "initial review",
                "citation",
            ),
            forbidden_destinations=(),
            deadline=Deadline(21, "ISSUE_DATE", "postmark", ()),
            required_items=("citation copy",),
            required_declarations=("hearing waiver",),
            unfulfillable_items=(),
        ),
        evidence=(),
        status="certain",
        uncertain_fields=(),
        notes="Hand-built synthetic key.",
    )


def finding(
    name: str, value: JsonValue, quote: str = "Verified synthetic quote."
) -> dict[str, JsonValue]:
    return {
        "key": name,
        "status": "CONFIRMED",
        "value": value,
        "verbatim_quote": quote,
        "source_sha256s": ["a" * 64],
    }


def run(case_id: str = "B01", preset: str = "test-model") -> CaseRun:
    address = finding("MAILING_ADDRESS", ADDRESS.copy(), QUOTE)
    return CaseRun(
        case_id=case_id,
        preset=preset,
        commit="deadbeef",
        snapshot_sha256="b" * 64,
        snapshot_date=TODAY,
        key_version=1,
        run_date=TODAY,
        issuer_outcome={
            "outcome": "RESOLVED",
            "code": None,
            "agency": {"name": "Example City", "deciding_body": "Review Office"},
        },
        delegation_outcomes=(),
        production_outcome={"family": "NOT_YET_VERIFIED", "hard_stop": "S6"},
        research_outcome={"outcome": "SERVICEABLE", "ran": True},
        findings=(
            address,
            finding("MAIL_IN_ACCEPTED", True),
            finding("DEADLINE_DAYS", 21),
            finding("DEADLINE_CLOCK_START", "ISSUE_DATE"),
            finding("DEADLINE_RULE", "POSTMARK"),
            finding("REQUIRED_ITEMS", ["citation copy"]),
            finding("REQUIRED_DECLARATIONS", ["hearing waiver"]),
        ),
        sources=({"sha256": "a" * 64},),
        attempts=(),
        ai_calls=(),
        cost_cents={"prehold": 5, "research": 7},
        brave_requests=2,
        wall_seconds={"prehold": 1.5, "research": 20.5},
        empty_ai_answers=1,
        digest_ok=True,
        digest_error=None,
        digest=None,
        counter_keys=(),
        fetches=(),
        resolution={
            "mailing_address": address,
            "counting": {"rule": "POSTMARK"},
            "deadline": {"deadline": "2026-10-25"},
        },
        critical_problems=(),
    )


def stopped(value: CaseRun, family: str = "NOT_YET_VERIFIED", stop: str = "S2") -> CaseRun:
    return replace(
        value,
        research_outcome={"outcome": "Stopped", "family": family, "hard_stop": stop, "ran": True},
    )


def fact(value: CaseRun, name: str, new_value: JsonValue) -> CaseRun:
    return replace(
        value,
        findings=tuple(
            {**f, "value": new_value} if f["key"] == name else f for f in value.findings
        ),
    )


def measure(scores: tuple[CaseRun, ...], row: KeyRow, name: str) -> Measure:
    (model,) = aggregate(score_run(value, row) for value in scores)
    return next(m for m in model.measures if m.name == name)


def test_correct_score_contains_every_per_run_column() -> None:
    score = score_run(run(), key())
    assert score.verdict == "correct"
    assert all(score.critical_checks)
    assert score.production_outcome == "NOT_YET_VERIFIED:S6"
    assert score.research_outcome == "SERVICEABLE"
    assert score.hard_stop is None
    assert score.stage_note == "stage not represented"
    assert score.deadline_days == 21
    assert score.expected_deadline == score.computed_deadline == "2026-10-25"
    assert (score.prehold_cost_cents, score.research_cost_cents) == (5, 7)
    assert (score.prehold_seconds, score.research_seconds) == (1.5, 20.5)
    assert (score.brave_requests, score.empty_ai_answers) == (2, 1)


def test_one_wrong_serviceable_fails_trap_and_eligibility() -> None:
    row = key()
    row = replace(
        row,
        expected=replace(
            row.expected, outcome_family=StopFamily.PROCESS_INCOMPATIBLE, hard_stop=HardStop.S3
        ),
    )
    bad = score_run(run(), row)
    assert bad.verdict == "wrong SERVICEABLE"
    (model,) = aggregate((bad,), permitted_production_use={"test-model": True})
    trap = next(m for m in model.measures if m.name == "Wrong SERVICEABLE on traps")
    assert (trap.numerator, trap.denominator, trap.passed) == (1, 1, False)
    assert not model.eligible
    assert "wrong SERVICEABLE" in model.eligibility_reasons
    assert model.back_to_guards == ("wrong SERVICEABLE",)


@pytest.mark.parametrize(
    ("status", "fields"), [("excluded", ()), ("uncertain", ()), ("uncertain", ("expected",))]
)
def test_excluded_case_never_scored(status: str, fields: tuple[str, ...]) -> None:
    row = replace(key(), uncertain_fields=fields)
    if status == "excluded":
        row = replace(row, status="excluded")
    else:
        row = replace(row, status="uncertain")
    score = score_run(replace(run(), findings=(), resolution=None), row)
    assert score.verdict == "excluded"
    assert set(score.critical_checks) == {None}
    (model,) = aggregate((score,), permitted_production_use={"test-model": True})
    assert (model.scored_runs, model.excluded_runs) == (0, 1)
    assert model.coverage is None
    assert not model.friction
    assert not model.back_to_guards
    assert not model.eligible
    assert all(m.value is None for m in model.measures)


def test_uncertain_field_is_unscored_and_does_not_cause_disagreement() -> None:
    row = replace(
        key(),
        status="uncertain",
        uncertain_fields=(
            "expected.destination",
            "deadline",
            "required_items",
            "required_declarations",
        ),
    )
    first = score_run(run(), row)
    second = score_run(replace(run(), findings=(), resolution=None), row)
    assert first.destination_correct is first.purpose_correct is first.deadline_correct is None
    assert first.required_items_correct is first.required_declarations_correct is None
    # Other certain fields still matter: MAIL_IN_ACCEPTED is missing in the second run.
    assert second.verdict == "wrong SERVICEABLE"
    (model,) = aggregate((first, second))
    assert not model.disagreement_cases


def test_forbidden_destination_wins_even_if_equal_to_expected_destination() -> None:
    row = key()
    row = replace(
        row,
        expected=replace(
            row.expected,
            forbidden_destinations=(
                ForbiddenDestination(row.expected.destination.lines, "90001", "payment"),
            ),
        ),
    )
    score = score_run(run(), row)
    assert score.destination_correct is False
    assert score.verdict == "wrong SERVICEABLE"
    assert "forbidden destination" in score.notes


def test_address_and_purpose_are_separate_checks() -> None:
    value = run()
    address = finding("MAILING_ADDRESS", ADDRESS, QUOTE.replace("initial review", "payment"))
    score = score_run(
        replace(value, resolution={**(value.resolution or {}), "mailing_address": address}), key()
    )
    assert score.destination_correct is True
    assert score.purpose_correct is False
    assert score.verdict == "wrong SERVICEABLE"
    # A purpose phrase without the actual address also cannot prove purpose.
    address = finding("MAILING_ADDRESS", ADDRESS, "Mail your initial review elsewhere.")
    score = score_run(
        replace(value, resolution={**(value.resolution or {}), "mailing_address": address}), key()
    )
    assert score.purpose_correct is False


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("DEADLINE_DAYS", 22),
        ("DEADLINE_CLOCK_START", "NOTICE_DATE"),
        ("REQUIRED_ITEMS", []),
        ("REQUIRED_DECLARATIONS", []),
        ("MAIL_IN_ACCEPTED", False),
    ],
)
def test_wrong_critical_fact_on_serviceable_is_wrong(field: str, bad: JsonValue) -> None:
    assert score_run(fact(run(), field, bad), key()).verdict == "wrong SERVICEABLE"


def test_computed_date_and_counting_are_checked() -> None:
    value = run()
    resolutions: tuple[dict[str, JsonValue], ...] = (
        {**(value.resolution or {}), "deadline": {"deadline": "2026-10-26"}},
        {**(value.resolution or {}), "counting": {"rule": "RECEIPT"}},
    )
    for resolution in resolutions:
        score = score_run(replace(value, resolution=resolution), key())
        assert score.deadline_correct is False
        assert score.verdict == "wrong SERVICEABLE"


@pytest.mark.parametrize(
    ("unit", "time_cutoff"),
    [("BUSINESS", False), ("CALENDAR", True), ("BUSINESS", True)],
)
def test_calendar_date_cannot_pass_for_a_different_period(unit: str, time_cutoff: bool) -> None:
    value = fact(run(), "DEADLINE_DAYS", {"days": 21, "unit": unit, "time_cutoff": time_cutoff})
    score = score_run(value, key())
    assert score.deadline_correct is False
    assert score.verdict == "wrong SERVICEABLE"


def test_notice_trigger_alternative_uses_synthetic_notice_date() -> None:
    row = key()
    row = replace(
        row,
        expected=replace(
            row.expected,
            deadline=Deadline(
                21, "ISSUE_DATE", "postmark", (DeadlineRule(14, "NOTICE_DATE", "received"),)
            ),
        ),
    )
    value = fact(fact(run(), "DEADLINE_DAYS", 14), "DEADLINE_CLOCK_START", "NOTICE_DATE")
    value = replace(
        value,
        resolution={
            **(value.resolution or {}),
            "counting": {"rule": "RECEIPT"},
            "deadline": {"deadline": "2026-10-24"},
        },
    )
    assert score_run(value, row).deadline_correct is True
    # The citation was 10 days ago; the notice was 4 days ago.
    wrong = replace(
        value, resolution={**(value.resolution or {}), "deadline": {"deadline": "2026-10-18"}}
    )
    assert score_run(wrong, row).deadline_correct is False


@pytest.mark.parametrize(
    ("trigger", "notice_date", "clock"),
    [
        ("ISSUE_DATE", None, ClockStart.ISSUE_DATE),
        ("NOTICE_DATE", date(2026, 10, 10), ClockStart.NOTICE_DATE),
    ],
)
def test_benchmark_uses_the_production_deadline_calculator(
    trigger: str, notice_date: date | None, clock: ClockStart
) -> None:
    row = key()
    run_value = run()
    if notice_date is not None:
        row = replace(row, scripted_answers=replace(row.scripted_answers, issue_offset_days=4))
    issue_date = run_value.run_date - timedelta(days=row.notice.issue_offset_days)
    expected_notice = notice_date
    expected = deadline_date(issue_date, 21, clock, expected_notice).isoformat()
    assert _deadline_date(run_value, row, DeadlineRule(21, trigger, "postmark")) == expected


def test_reviewer_synonyms_and_unfulfillable_inventory() -> None:
    value = fact(run(), "REQUIRED_ITEMS", ["copy of citation"])
    assert score_run(value, key()).required_items_correct is False
    assert (
        score_run(
            value, key(), item_synonyms={"citation copy": ("copy of citation",)}
        ).required_items_correct
        is True
    )
    row = key()
    row = replace(row, expected=replace(row.expected, unfulfillable_items=("citation copy",)))
    assert score_run(run(), row).unfulfillable_items_clear is False


def test_refusal_verdicts_and_saved_pi_quote() -> None:
    row = key()
    nyv = stopped(run())
    assert score_run(nyv, row).verdict == "safe-wrong-family"
    pi = stopped(run(), "PROCESS_INCOMPATIBLE", "S3")
    assert score_run(pi, row).verdict == "untraceable"
    pi = replace(
        pi,
        issuer_outcome={
            **pi.issuer_outcome,
            "evidence": {
                "quotes": [
                    {"verbatim_quote": "A mandatory form is required.", "source_sha256": "a" * 64}
                ]
            },
        },
    )
    assert score_run(pi, row).verdict == "wrong PI"
    incompatible = replace(
        row,
        expected=replace(
            row.expected, outcome_family=StopFamily.PROCESS_INCOMPATIBLE, hard_stop=HardStop.S3
        ),
    )
    assert score_run(pi, incompatible).verdict == "correct"
    assert score_run(nyv, incompatible).verdict == "safe-wrong-family"
    assert (
        score_run(
            replace(nyv, research_outcome={"outcome": "Stopped", "family": "NOT_YET_VERIFIED"}), row
        ).verdict
        == "untraceable"
    )


def test_disagreement_is_share_of_cases_not_pairwise_runs() -> None:
    scores: list[RunScore] = []
    for i in range(1, 18):
        case_id = f"B{i:02}"
        value = run(case_id)
        scores.extend(score_run(value, key(case_id)) for _ in range(3))
    scores.append(score_run(stopped(run()), key()))
    (model,) = aggregate(scores, permitted_production_use={"test-model": True})
    disagreement = next(m for m in model.measures if m.name == "Run-to-run disagreement")
    assert disagreement.numerator == 1
    assert disagreement.denominator == 17
    assert disagreement.value == pytest.approx(1 / 17)
    assert disagreement.passed is False
    assert model.disagreement_cases == ("B01",)
    assert not model.eligible


def test_destination_and_hard_stop_each_produce_disagreement() -> None:
    value = run()
    address = finding("MAILING_ADDRESS", {**ADDRESS, "line1": "P.O. Box 999"}, QUOTE)
    different = replace(value, resolution={**(value.resolution or {}), "mailing_address": address})
    for left, right in ((value, different), (stopped(value), stopped(value, stop="S1"))):
        (model,) = aggregate((score_run(left, key()), score_run(right, key())))
        assert model.disagreement_cases == ("B01",)


def test_friction_counts_stopped_runs_not_duplicate_veto_occurrences() -> None:
    value = stopped(run())
    problem: dict[str, JsonValue] = {"key": "REQUIRED_ITEMS", "reason": "REQUIRED_ITEMS: unknown"}
    value = replace(
        value,
        issuer_outcome={"outcome": "Stopped", "agency": None, "code": "NAME_MISMATCH"},
        delegation_outcomes=({"code": "NAME_MISMATCH"}, {"code": "BUDGET_EXHAUSTED"}),
        critical_problems=(problem, problem),
    )
    score = score_run(value, key())
    # A vetoed link in a successful run is diagnostic, not a stopped run.
    success = score_run(replace(run(), delegation_outcomes=value.delegation_outcomes), key())
    (model,) = aggregate((score, score, success))
    assert {(f.kind, f.reason): f.runs for f in model.friction} == {
        ("VetoCode", "BUDGET_EXHAUSTED"): 2,
        ("VetoCode", "NAME_MISMATCH"): 2,
        ("research", "REQUIRED_ITEMS: unknown"): 2,
    }


def test_recovered_delegation_does_not_become_friction_at_a_later_research_stop() -> None:
    value = replace(stopped(run()), delegation_outcomes=({"code": "NAME_MISMATCH"},))
    score = score_run(value, key())
    assert score.veto_codes == ("NAME_MISMATCH",)
    assert not score.stopping_veto_codes
    (model,) = aggregate((score,))
    assert not model.friction


def test_research_budget_exhaustion_is_friction() -> None:
    attempt: dict[str, JsonValue] = {
        "phase": "research",
        "stage": "extract",
        "error": "budget exhausted",
    }
    score = score_run(replace(stopped(run()), attempts=(attempt,)), key())
    (model,) = aggregate((score,))
    assert [(f.kind, f.reason, f.runs) for f in model.friction] == [
        ("research", "budget exhausted", 1)
    ]


def test_eligibility_requires_explicit_production_use_and_repeat_evidence() -> None:
    score = score_run(run(), key())
    (model,) = aggregate((score, score), permitted_production_use={"test-model": True})
    assert model.eligible
    (model,) = aggregate((score, score))
    assert not model.eligible
    assert model.eligibility_reasons == ("permitted production use not confirmed",)
    (model,) = aggregate((score,), permitted_production_use={"test-model": True})
    assert model.eligibility_reasons == ("repeat evidence incomplete",)


def test_back_to_guards_flags_and_threshold_boundaries() -> None:
    value = stopped(run(), "PROCESS_INCOMPATIBLE", "S3")
    score = score_run(value, key())  # Both untraceable and a false PI on a fulfilable case.
    (model,) = aggregate((score, score), leading_model="test-model")
    assert set(model.back_to_guards) == {
        "untraceable refusal",
        "wrong PI above 2%",
        "leading model coverage below 50%",
    }
    good = score_run(run(), key())
    (model,) = aggregate((score, *(good for _ in range(49))))
    pi = next(m for m in model.measures if m.name == "Wrong PROCESS_INCOMPATIBLE")
    assert pi.value == 0.02
    assert pi.passed is True
    assert "wrong PI above 2%" not in model.back_to_guards
    at_limit = replace(run(), wall_seconds={"research": 300})
    assert measure((at_limit,), key(), "Median research time").passed is True
    assert (
        measure(
            (replace(at_limit, wall_seconds={"research": 301}),), key(), "Median research time"
        ).passed
        is False
    )


def test_median_uses_research_runs_and_cost_uses_both_phases() -> None:
    value = replace(
        stopped(run()),
        research_outcome={
            "outcome": "Stopped",
            "family": "NOT_YET_VERIFIED",
            "hard_stop": "S2",
            "ran": False,
        },
        wall_seconds={"research": 0},
    )
    assert measure((run(), value), key(), "Median research time").value == 20.5
    assert measure((run(),), key(), "AI cost per ticket").value == 12


def test_markdown_deterministic_and_lists_exclusions() -> None:
    scores = (
        score_run(run(preset="z-model"), key()),
        score_run(stopped(run()), key()),
        score_run(run(), key()),
        score_run(
            run("B17"), replace(key("B17"), status="excluded", notes="review pending | uncertain")
        ),
    )
    models = aggregate(scores)
    markdown = render_markdown(models, scores)
    assert markdown == render_markdown(reversed(models), reversed(scores))
    assert "stage not represented" in markdown
    assert "review pending \\| uncertain" in markdown
    assert "Wrong SERVICEABLE on traps" in markdown
    assert "Guard friction on key-SERVICEABLE cases" in markdown
    assert "Exclusions, uncertainty and sign-off" in markdown
    assert "not the random stratified" in markdown
    assert markdown.endswith("\n")
    assert render_markdown((), ()) == render_markdown((), ())


def test_mismatched_case_rejected_and_new_key_can_rescore() -> None:
    with pytest.raises(ValueError, match="case_id"):
        score_run(run(), key("B02"))
    score = score_run(run(), replace(key(), key_version=2))
    assert score.key_version == 2
    assert "rescored run key v1 against key v2" in score.notes
