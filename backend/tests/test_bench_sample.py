"""Contract tests for T21 W8: the random-sample audit machinery (plan unit W8)."""

from __future__ import annotations

from datetime import date

from parkingbreaker.bench.report import render_markdown
from parkingbreaker.bench.sample import STRATA, Stratum, stratified_draw, stratum_of
from parkingbreaker.bench.score import (
    RunScore,
    Verdict,
    aggregate,
    rule_of_three_upper_bound,
)
from parkingbreaker.research.jurisdiction import DotGovRecord, DotGovSnapshot

RUN_DATE = date(2026, 10, 21)


def record(
    domain: str,
    domain_type: str,
    organization: str,
    suborganization: str | None = None,
) -> DotGovRecord:
    return DotGovRecord(domain, domain_type, organization, suborganization, "Example", "CA")


# Three records per stratum, with names that could mislead a naive classifier:
# kind words inside municipal names ("City of College Park", "Borough of State
# College") are not universities, and airport names sit under city domain types.
FRAME: dict[Stratum, tuple[DotGovRecord, ...]] = {
    "large_city": (
        record("heights.gov", "City", "City of University Heights"),
        record("newyorkcity.gov", "City", "The City of New York"),
        record("collegepark.gov", "City", "City of College Park"),
    ),
    "small_city": (
        record("collegeville.gov", "City", "Town of Collegeville"),
        record("statecollege.gov", "City", "Borough of State College"),
        record("manorhaven.gov", "City", "Village of Manorhaven"),
    ),
    "county": (
        record("gila.gov", "County", "Gila County"),
        record("lacounty.gov", "County", "County of Los Angeles"),
        record("alameda.gov", "County", "Alameda County"),
    ),
    "university": (
        record("examplestateu.edu", "State or territory", "Example State University"),
        record("orau.gov", "Federal - Executive", "Oak Ridge Associated Universities"),
        record("examplecc.gov", "Special district", "Example Community College"),
    ),
    "airport": (
        record("flysfo.gov", "City", "San Francisco International Airport"),
        record("chattairport.gov", "Special district", "Chattanooga Metropolitan Airport"),
        record("porthouston.gov", "Special district", "Port of Houston Authority"),
    ),
    "processor": (
        record("examplepayproc.gov", "Special district", "Example Payment Processor"),
        record("treaslockbox.gov", "Federal - Executive", "Treasury Lockbox Services"),
        record("citeprocessor.gov", "Special district", "Example Citation Processor"),
    ),
}
UNSTRATIFIED = record("energy.gov", "Federal - Executive", "Department of Example")


def frame() -> DotGovSnapshot:
    values = [value for group in FRAME.values() for value in group]
    return DotGovSnapshot(RUN_DATE, (*values, UNSTRATIFIED), {}, "b" * 64)


def run_score(case_id: str = "S000", verdict: Verdict = "correct") -> RunScore:
    return RunScore(
        case_id=case_id,
        preset="test-model",
        commit="deadbeef",
        key_version=1,
        run_date=RUN_DATE,
        trap=False,
        expected_family="SERVICEABLE",
        production_outcome="NOT_YET_VERIFIED:S6",
        research_outcome="SERVICEABLE",
        outcome_family="SERVICEABLE",
        hard_stop=None,
        disagreement_key=("SERVICEABLE", None, None),
        research_ran=True,
        veto_codes=(),
        stopping_veto_codes=(),
        research_reasons=(),
        issuer_correct=None,
        deciding_body_correct=None,
        destination_correct=None,
        purpose_correct=None,
        deadline_correct=None,
        required_items_correct=None,
        required_declarations_correct=None,
        unfulfillable_items_clear=None,
        mail_in_correct=None,
        destination="",
        deadline_days=None,
        deadline_trigger=None,
        deadline_counting=None,
        computed_deadline=None,
        expected_deadline=None,
        required_items=(),
        required_declarations=(),
        prehold_cost_cents=0,
        research_cost_cents=0,
        brave_requests=0,
        prehold_seconds=0.0,
        research_seconds=1.0,
        empty_ai_answers=0,
        verdict=verdict,
        notes=(),
        uncertain_fields=(),
        owner_signed=False,
    )


def one_failure_scores() -> tuple[RunScore, ...]:
    return (
        run_score("S000", verdict="wrong SERVICEABLE"),
        *(run_score(f"S{i:03d}") for i in range(1, 300)),
    )


def test_sample_is_deterministic_for_a_seed() -> None:
    first = stratified_draw(frame(), seed="audit-v1", size=12)
    assert first == stratified_draw(frame(), seed="audit-v1", size=12)
    shuffled = DotGovSnapshot(
        RUN_DATE, tuple(reversed(frame().records)), {}, frame().content_sha256
    )
    assert first == stratified_draw(shuffled, seed="audit-v1", size=12)
    assert first.draws != stratified_draw(frame(), seed="audit-v2", size=12).draws


def test_every_declared_stratum_is_populated() -> None:
    draw = stratified_draw(frame(), seed="audit-v1", size=18)
    assert tuple(entry.stratum for entry in draw.draws) == STRATA
    for entry in draw.draws:
        assert len(entry.records) == 3, entry.stratum
        assert all(stratum_of(value) == entry.stratum for value in entry.records)
    # Quotas split evenly; the remainder goes to the leading strata in STRATA order.
    quotas = stratified_draw(frame(), seed="audit-v1", size=8)
    assert tuple(entry.requested for entry in quotas.draws) == (2, 2, 1, 1, 1, 1)


def test_rule_of_three_zero_failures_is_one_over_n() -> None:
    # Zero failures in n trials bounds the rate below 3/n at 95%: three times
    # one-over-n (T21.md rule of three).
    assert rule_of_three_upper_bound(0, 300) == 3 / 300
    assert rule_of_three_upper_bound(0, 1) == 3.0
    at_300 = rule_of_three_upper_bound(0, 300)
    assert at_300 is not None
    assert rule_of_three_upper_bound(0, 600) == at_300 / 2
    assert rule_of_three_upper_bound(1, 300) is None
    assert rule_of_three_upper_bound(0, 0) is None


def test_bound_is_reported_separately_from_the_point_estimate() -> None:
    scores = tuple(run_score(f"S{i:03d}") for i in range(300))
    (model,) = aggregate(scores)
    rate = model.unsafe_acceptances
    assert (rate.failures, rate.trials) == (0, 300)
    assert rate.point_estimate == 0.0
    assert rate.rule_of_three_bound == 3 / 300
    lines = render_markdown((model,), scores).splitlines()
    point_line = next(line for line in lines if "point estimate" in line)
    bound_line = next(line for line in lines if "Rule-of-three" in line)
    assert "point estimate 0.00%" in point_line
    assert "Rule-of-three 95% upper bound (zero failures): 1.00%" in bound_line
    assert point_line != bound_line


def test_bound_is_not_applicable_once_a_failure_is_seen() -> None:
    scores = one_failure_scores()
    (model,) = aggregate(scores)
    rate = model.unsafe_acceptances
    assert (rate.failures, rate.trials) == (1, 300)
    assert rate.point_estimate == 1 / 300
    assert rate.rule_of_three_bound is None
    lines = render_markdown((model,), scores).splitlines()
    point_line = next(line for line in lines if "point estimate" in line)
    bound_line = next(line for line in lines if "Rule-of-three" in line)
    assert "point estimate 0.33%" in point_line
    assert "not applicable" in bound_line


def test_unstratified_records_are_counted_and_never_drawn() -> None:
    draw = stratified_draw(frame(), seed="audit-v1", size=18)
    assert draw.unstratified == 1
    drawn = {value.domain for entry in draw.draws for value in entry.records}
    assert UNSTRATIFIED.domain not in drawn


def test_frame_records_classify_into_their_named_strata() -> None:
    for stratum, records in FRAME.items():
        for value in records:
            assert stratum_of(value) == stratum, value.domain
    assert stratum_of(UNSTRATIFIED) is None


def test_stratum_of_tolerates_organisation_and_suborganisation() -> None:
    federal = record(
        "orau2.gov", "Federal - Executive", "Department of Energy", "Example University"
    )
    assert stratum_of(federal) == "university"
    assert stratum_of(record("plain.gov", "City", "Baxter Estates")) == "small_city"
