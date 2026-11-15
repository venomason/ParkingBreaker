"""Pure Phase 0 scoring (bench-plan §§4.3, 6, 7).

None means unscored, never a pass or a failure. Partial uncertain_fields use key
paths (with or without ``expected.``); an uncertain row without paths is excluded.
The trap set is the plan's B01-B13 and B17, not inferred from pipeline output.
Production-use permission and reviewer inventory synonyms are caller-supplied facts.
Stage and frozen-packet fidelity are not represented by research runs.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import median
from typing import Literal

from parkingbreaker.bench.cases import DeadlineRule, KeyRow
from parkingbreaker.bench.runner import CaseRun
from parkingbreaker.case_state import HardStop, StopFamily
from parkingbreaker.json_types import JsonValue
from parkingbreaker.mail_timing import (
    ClockStart,
    DeadlinePeriod,
    deadline_for_period,
    parse_deadline_period,
)
from parkingbreaker.research.support import address_matches, address_purpose_problem, address_text
from parkingbreaker.research.veto_codes import VetoCode

type Verdict = Literal[
    "correct", "safe-wrong-family", "wrong SERVICEABLE", "wrong PI", "untraceable", "excluded"
]
TRAPS = frozenset([*(f"B{i:02}" for i in range(1, 14)), "B17"])


@dataclass(frozen=True)
class RunScore:
    case_id: str
    preset: str
    commit: str
    key_version: int
    run_date: date
    trap: bool
    expected_family: str | None
    production_outcome: str
    research_outcome: str
    outcome_family: str
    hard_stop: str | None
    disagreement_key: tuple[str, str | None, str | None]
    research_ran: bool
    veto_codes: tuple[str, ...]
    stopping_veto_codes: tuple[str, ...]
    research_reasons: tuple[str, ...]
    issuer_correct: bool | None
    deciding_body_correct: bool | None
    destination_correct: bool | None
    purpose_correct: bool | None
    deadline_correct: bool | None
    required_items_correct: bool | None
    required_declarations_correct: bool | None
    unfulfillable_items_clear: bool | None
    mail_in_correct: bool | None
    destination: str
    deadline_days: JsonValue
    deadline_trigger: JsonValue
    deadline_counting: JsonValue
    computed_deadline: JsonValue
    expected_deadline: str | None
    required_items: tuple[str, ...]
    required_declarations: tuple[str, ...]
    prehold_cost_cents: int
    research_cost_cents: int
    brave_requests: int
    prehold_seconds: float
    research_seconds: float
    empty_ai_answers: int
    verdict: Verdict
    notes: tuple[str, ...]
    uncertain_fields: tuple[str, ...]
    owner_signed: bool
    stage_note: str = "stage not represented"

    @property
    def critical_checks(self) -> tuple[bool | None, ...]:
        return (
            self.issuer_correct,
            self.deciding_body_correct,
            self.destination_correct,
            self.purpose_correct,
            self.deadline_correct,
            self.required_items_correct,
            self.required_declarations_correct,
            self.unfulfillable_items_clear,
            self.mail_in_correct,
        )


@dataclass(frozen=True)
class Measure:
    name: str
    numerator: int | None
    denominator: int | None
    value: float | None
    limit: str
    passed: bool | None
    note: str = ""


@dataclass(frozen=True)
class Friction:
    kind: Literal["VetoCode", "research"]
    reason: str
    runs: int


@dataclass(frozen=True)
class FailureRate:
    """Failures observed over trials, keeping the two reported numbers apart.

    ``point_estimate`` is the measured rate; ``rule_of_three_bound`` is its 95%
    upper limit when zero failures were seen. The bound never stands in for the
    point estimate and is None as soon as any failure is seen.
    """

    failures: int
    trials: int

    @property
    def point_estimate(self) -> float | None:
        return self.failures / self.trials if self.trials else None

    @property
    def rule_of_three_bound(self) -> float | None:
        return rule_of_three_upper_bound(self.failures, self.trials)


@dataclass(frozen=True)
class ModelAggregate:
    preset: str
    scored_runs: int
    excluded_runs: int
    measures: tuple[Measure, ...]
    friction: tuple[Friction, ...]
    disagreement_cases: tuple[str, ...]
    repeated_cases: int
    correct_serviceable: int
    key_serviceable_runs: int
    coverage: float | None
    permitted_production_use: bool | None
    eligible: bool
    eligibility_reasons: tuple[str, ...]
    back_to_guards: tuple[str, ...]
    unsafe_acceptances: FailureRate


def _obj(value: JsonValue) -> dict[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def _strings(value: JsonValue) -> tuple[str, ...]:
    return tuple(v for v in value if isinstance(v, str)) if isinstance(value, list) else ()


def _normal(text: str) -> str:
    return " ".join(text.casefold().split())


def _known(row: KeyRow, path: str) -> bool:
    path = path.removeprefix("expected.")
    return not any(
        field in ("", "expected", "*")
        or path == field
        or path.startswith(field + ".")
        or field.startswith(path + ".")
        for raw in row.uncertain_fields
        for field in (raw.removeprefix("expected."),)
    )


def _family(outcome: dict[str, JsonValue]) -> str:
    if outcome.get("outcome") == "SERVICEABLE" or outcome.get("status") == "supported":
        return "SERVICEABLE"
    value = outcome.get("family")
    return value if isinstance(value, str) else str(outcome.get("outcome", "unknown"))


def _outcome(outcome: dict[str, JsonValue]) -> str:
    family = _family(outcome)
    stop = outcome.get("hard_stop")
    return f"{family}:{stop}" if isinstance(stop, str) else family


def _finding(run: CaseRun, key: str) -> dict[str, JsonValue]:
    matches = [f for f in run.findings if f.get("key") == key]
    # Do not silently select one of multiple inconsistent values.
    if len({json.dumps(f.get("value"), sort_keys=True) for f in matches}) > 1:
        return {}
    return next((f for f in matches if f.get("status") == "CONFIRMED"), {})


def _address(run: CaseRun) -> dict[str, JsonValue]:
    resolved = _obj((run.resolution or {}).get("mailing_address"))
    return resolved or _finding(run, "MAILING_ADDRESS")


def _destination(value: JsonValue) -> tuple[str, str]:
    address = _obj(value)
    lines = " ".join(
        item
        for field in ("recipient", "attention", "line1", "line2", "city", "state")
        if isinstance(item := address.get(field), str)
    )
    zip_code = address.get("zip_code")
    return address_text(lines), zip_code[:5] if isinstance(zip_code, str) else ""


def _key_lines(lines: tuple[str, ...], zip5: str) -> str:
    # Some reviewed keys include the ZIP in their final address line as well.
    return address_text(re.sub(rf"\b{re.escape(zip5)}(?:-\d{{4}})?\s*$", "", " ".join(lines)))


def _inventory(
    wanted: tuple[str, ...], found: tuple[str, ...], synonyms: Mapping[str, tuple[str, ...]]
) -> bool:
    available = {_normal(item) for item in found}
    return all(
        any(_normal(candidate) in available for candidate in (item, *synonyms.get(item, ())))
        for item in wanted
    )


def _deadline_date(
    run: CaseRun, row: KeyRow, rule: DeadlineRule, period: DeadlinePeriod | None = None
) -> str | None:
    period = period or DeadlinePeriod(rule.days)
    trigger = rule.trigger.upper()
    if trigger == "ISSUE_DATE":
        issue_date = run.run_date - timedelta(days=row.notice.issue_offset_days)
        return deadline_for_period(issue_date, period, ClockStart.ISSUE_DATE).isoformat()
    elif trigger == "NOTICE_DATE":
        notice_offset = row.scripted_answers.issue_offset_days
        if notice_offset is None:
            return None
        issue_date = run.run_date - timedelta(days=row.notice.issue_offset_days)
        notice_date = run.run_date - timedelta(days=notice_offset)
        return deadline_for_period(
            issue_date, period, ClockStart.NOTICE_DATE, notice_date
        ).isoformat()
    elif trigger in ("PRINTED_DEADLINE", "PRINTED_DATE"):
        printed = row.scripted_answers.printed_deadline_offset_days
        return None if printed is None else (run.run_date + timedelta(days=printed)).isoformat()
    else:
        return None


def _pi_quote(run: CaseRun) -> bool:
    # Quotes have already been guarded by the production path; require saved provenance.
    evidence = _obj(run.issuer_outcome.get("evidence"))
    quotes = evidence.get("quotes")
    if not isinstance(quotes, list):
        return False
    hashes = {s.get("sha256") for s in run.sources if isinstance(s.get("sha256"), str)}
    return any(
        isinstance(q, dict)
        and isinstance(q.get("verbatim_quote"), str)
        and bool(str(q["verbatim_quote"]).strip())
        and q.get("source_sha256") in hashes
        for q in quotes
        if isinstance(q, dict) and isinstance(q.get("source_sha256"), str)
    )


def score_run(
    case_run: CaseRun,
    key_row: KeyRow,
    *,
    item_synonyms: Mapping[str, tuple[str, ...]] | None = None,
) -> RunScore:
    """Score guarded run records, without fetching, adjudicating or changing the key.

    Re-scoring against a newer adjudicated key is allowed; case identity must match.
    Reviewer synonyms map each key inventory item to approved equivalent strings.
    """
    run, row = case_run, key_row
    if run.case_id != row.case_id:
        raise ValueError("case_id: run and answer key disagree")
    excluded = (
        row.status == "excluded"
        or (row.status == "uncertain" and not row.uncertain_fields)
        or any(f in ("expected", "*", "") for f in row.uncertain_fields)
    )
    family = _family(run.research_outcome)
    serviceable = family == "SERVICEABLE"
    stop_value = run.research_outcome.get("hard_stop")
    stop = stop_value if isinstance(stop_value, str) else None
    expected = row.expected
    expected_family = str(expected.outcome_family) if _known(row, "outcome_family") else None
    synonyms = item_synonyms or {}
    address_finding = _address(run)
    address_value = address_finding.get("value")
    address, zip5 = _destination(address_value)
    destination = expected.destination
    checks: dict[str, bool | None] = {}

    def check(name: str, path: str, value: bool) -> None:
        checks[name] = value if not excluded and _known(row, path) else None

    agency = _obj(run.issuer_outcome.get("agency"))
    check(
        "issuer",
        "issuer_name",
        _normal(str(agency.get("name", ""))) == _normal(expected.issuer_name),
    )
    check(
        "deciding_body",
        "deciding_body",
        _normal(str(agency.get("deciding_body", ""))) == _normal(expected.deciding_body),
    )
    matches = (address, zip5) == (_key_lines(destination.lines, destination.zip5), destination.zip5)
    forbidden = _known(row, "forbidden_destinations") and any(
        (address, zip5) == (_key_lines(d.lines, d.zip5), d.zip5)
        for d in expected.forbidden_destinations
    )
    # Stage is explicitly unrepresented; do not turn that known limitation into a pass.
    checks["destination"] = (
        None
        if excluded
        else False
        if forbidden
        else matches
        if _known(row, "destination.lines") and _known(row, "destination.zip5")
        else None
    )
    quote = address_finding.get("verbatim_quote")
    check(
        "purpose",
        "destination.purpose",
        isinstance(quote, str)
        and f" {address_text(destination.purpose)} " in f" {address_text(quote)} "
        and address_matches(address_value, quote)
        and address_purpose_problem(address_value, quote) is None,
    )
    days = _finding(run, "DEADLINE_DAYS").get("value")
    trigger = _finding(run, "DEADLINE_CLOCK_START").get("value")
    counting = _obj((run.resolution or {}).get("counting")).get("rule")
    if counting is None:
        counting = _finding(run, "DEADLINE_RULE").get("value")
    computed = _obj((run.resolution or {}).get("deadline")).get("deadline")
    rules = (
        DeadlineRule(expected.deadline.days, expected.deadline.trigger, expected.deadline.counting),
        *expected.deadline.alternatives,
    )
    dates = tuple(_deadline_date(run, row, rule) for rule in rules)
    check(
        "deadline",
        "deadline",
        any(
            (period := parse_deadline_period(days)) is not None
            and period.days == rule.days
            and isinstance(trigger, str)
            and trigger.upper() == rule.trigger.upper()
            and isinstance(counting, str)
            and counting.upper() == ("POSTMARK" if rule.counting == "postmark" else "RECEIPT")
            and expected_date is not None
            and computed == expected_date
            and computed == _deadline_date(run, row, rule, period)
            for rule, expected_date in zip(rules, dates, strict=True)
        ),
    )
    items_finding = _finding(run, "REQUIRED_ITEMS")
    declarations_finding = _finding(run, "REQUIRED_DECLARATIONS")
    items = _strings(items_finding.get("value"))
    declarations = _strings(declarations_finding.get("value"))
    check(
        "required_items",
        "required_items",
        bool(items_finding) and _inventory(expected.required_items, items, synonyms),
    )
    check(
        "required_declarations",
        "required_declarations",
        bool(declarations_finding)
        and _inventory(expected.required_declarations, declarations, synonyms),
    )
    check(
        "unfulfillable",
        "unfulfillable_items",
        not serviceable
        or not any(
            _inventory((item,), (*items, *declarations), synonyms)
            for item in expected.unfulfillable_items
        ),
    )
    check(
        "mail_in",
        "outcome_family",
        not serviceable or _finding(run, "MAIL_IN_ACCEPTED").get("value") is True,
    )
    codes = tuple(
        sorted(
            {
                str(code)
                for outcome in (run.issuer_outcome, *run.delegation_outcomes)
                if isinstance(code := outcome.get("code"), str)
            }
        )
    )
    research_reasons = {
        str(p.get("reason")) for p in run.critical_problems if isinstance(p.get("reason"), str)
    }
    if any(
        a.get("phase") == "research" and a.get("error") == "budget exhausted" for a in run.attempts
    ):
        research_reasons.add("budget exhausted")
    reasons = tuple(sorted(research_reasons))
    notes: list[str] = []
    if forbidden and not excluded:
        notes.append("forbidden destination")
    if excluded or expected_family is None:
        verdict: Verdict = "excluded"
        notes.append(row.notes or "uncertain or excluded answer key")
    elif serviceable:
        failures = [name for name, passed in checks.items() if passed is False]
        if expected_family != "SERVICEABLE":
            failures.append("outcome_family")
        verdict = "wrong SERVICEABLE" if failures else "correct"
        notes.extend(failures)
    elif (
        family not in set(StopFamily)
        or stop not in set(HardStop)
        or (family == "PROCESS_INCOMPATIBLE" and not _pi_quote(run))
    ):
        verdict = "untraceable"
        notes.append("refusal lacks family, hard stop or saved PI quote")
    elif family == expected_family and (not _known(row, "hard_stop") or stop == expected.hard_stop):
        verdict = "correct"
    elif (
        family == "PROCESS_INCOMPATIBLE"
        and expected_family != family
        and (
            not _known(row, "safe_set")
            or not any(allowed in (family, f"{family}:{stop}") for allowed in expected.safe_set)
        )
    ):
        verdict = "wrong PI"
    else:
        verdict = "safe-wrong-family"
    if row.key_version != run.key_version:
        notes.append(f"rescored run key v{run.key_version} against key v{row.key_version}")
    return RunScore(
        case_id=run.case_id,
        preset=run.preset,
        commit=run.commit,
        key_version=row.key_version,
        run_date=run.run_date,
        trap=row.case_id in TRAPS,
        expected_family=None if excluded else expected_family,
        production_outcome=_outcome(run.production_outcome),
        research_outcome=_outcome(run.research_outcome),
        outcome_family=family,
        hard_stop=stop,
        disagreement_key=(
            family,
            stop if _known(row, "hard_stop") else None,
            json.dumps((address, zip5)) if checks["destination"] is not None else None,
        ),
        research_ran=run.research_outcome.get("ran") is True,
        veto_codes=codes,
        # Rejected links on a resolved route did not stop issuer resolution.
        stopping_veto_codes=codes if not agency else (),
        research_reasons=reasons,
        issuer_correct=checks["issuer"],
        deciding_body_correct=checks["deciding_body"],
        destination_correct=checks["destination"],
        purpose_correct=checks["purpose"],
        deadline_correct=checks["deadline"],
        required_items_correct=checks["required_items"],
        required_declarations_correct=checks["required_declarations"],
        unfulfillable_items_clear=checks["unfulfillable"],
        mail_in_correct=checks["mail_in"],
        destination=json.dumps((address, zip5), separators=(",", ":")),
        deadline_days=days,
        deadline_trigger=trigger,
        deadline_counting=counting,
        computed_deadline=computed,
        expected_deadline=(
            "; ".join(sorted({d for d in dates if d is not None})) or None
            if checks["deadline"] is not None
            else None
        ),
        required_items=items,
        required_declarations=declarations,
        prehold_cost_cents=run.cost_cents.get("prehold", 0),
        research_cost_cents=run.cost_cents.get("research", 0),
        brave_requests=run.brave_requests,
        prehold_seconds=run.wall_seconds.get("prehold", 0.0),
        research_seconds=run.wall_seconds.get("research", 0.0),
        empty_ai_answers=run.empty_ai_answers,
        verdict=verdict,
        notes=tuple(notes),
        uncertain_fields=tuple(sorted(row.uncertain_fields)),
        owner_signed=row.owner_signed_at is not None,
    )


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def rule_of_three_upper_bound(failures: int, trials: int) -> float | None:
    """95% upper bound on a failure rate observed at zero failures: three times 1/n.

    Zero failures in n trials bounds the true rate below 3/n at 95% confidence
    (T21.md rule of three). None when the rule does not apply: any failure observed,
    or no trials at all.
    """
    if failures != 0 or trials <= 0:
        return None
    return 3.0 / trials


def _rate_row(
    measures: list[Measure],
    name: str,
    count: int,
    total: int,
    maximum: float,
    limit: str,
    note: str = "",
) -> None:
    value = _rate(count, total)
    measures.append(
        Measure(
            name,
            count,
            total,
            value,
            limit,
            None if value is None else value <= maximum,
            note,
        )
    )


def aggregate(
    scores: Iterable[RunScore],
    *,
    permitted_production_use: Mapping[str, bool] | None = None,
    leading_model: str | None = None,
    ai_cost_limit_cents: float | None = None,
) -> tuple[ModelAggregate, ...]:
    """Return all §6 rows in model order; no model or permission is silently chosen.

    Rates use scored runs; disagreement uses cases with at least two scored runs.
    Coverage is a run-weighted share, so extra repeats must be interpreted accordingly.
    The low-coverage guard flag applies only to an explicitly supplied leading_model.
    Cost limits are AI-only; search dollars cannot be inferred from request counts.
    """
    grouped: dict[str, list[RunScore]] = defaultdict(list)
    for score in scores:
        grouped[score.preset].append(score)
    if leading_model is not None and leading_model not in grouped:
        raise ValueError("leading_model has no scores")
    result: list[ModelAggregate] = []
    for preset, all_runs in sorted(grouped.items()):
        runs = [s for s in all_runs if s.verdict != "excluded"]
        served = [s for s in runs if s.outcome_family == "SERVICEABLE"]
        key_served = [s for s in runs if s.expected_family == "SERVICEABLE"]
        correct = sum(
            s.verdict == "correct" and s.outcome_family == "SERVICEABLE" for s in key_served
        )
        coverage = _rate(
            sum(s.outcome_family == "SERVICEABLE" for s in key_served), len(key_served)
        )
        by_case: dict[str, list[RunScore]] = defaultdict(list)
        for s in runs:
            by_case[s.case_id].append(s)
        repeated = {case: group for case, group in by_case.items() if len(group) > 1}
        disagreements = tuple(
            sorted(
                case
                for case, group in repeated.items()
                if len({s.disagreement_key for s in group}) > 1
            )
        )
        wrong = sum(s.verdict == "wrong SERVICEABLE" for s in runs)
        wrong_pi = sum(
            s.verdict == "wrong PI"
            or (s.outcome_family == "PROCESS_INCOMPATIBLE" and s.expected_family == "SERVICEABLE")
            for s in runs
        )
        untraceable = sum(s.verdict == "untraceable" for s in runs)
        bad_destination = sum(
            s.destination_correct is False or s.purpose_correct is False for s in served
        )
        critical_runs = [s for s in served if any(v is not None for v in s.critical_checks)]
        critical_wrong = sum(False in s.critical_checks for s in critical_runs)
        measures: list[Measure] = []

        traps = [s for s in runs if s.trap]
        controls = [s for s in runs if not s.trap]
        _rate_row(
            measures,
            "Wrong SERVICEABLE on traps",
            sum(s.verdict == "wrong SERVICEABLE" for s in traps),
            len(traps),
            0,
            "any",
        )
        _rate_row(
            measures,
            "Wrong SERVICEABLE, other cases",
            sum(s.verdict == "wrong SERVICEABLE" for s in controls),
            len(controls),
            0.01,
            ">1%",
            "Controls only; no random stratified sample in Phase 0.",
        )
        _rate_row(measures, "Wrong PROCESS_INCOMPATIBLE", wrong_pi, len(runs), 0.02, ">2%")
        assessable_dest = [
            s for s in served if s.destination_correct is not None or s.purpose_correct is not None
        ]
        _rate_row(
            measures,
            "Wrong destination or purpose among SERVICEABLE",
            bad_destination,
            len(assessable_dest),
            0,
            "any",
        )
        _rate_row(
            measures,
            "Wrong critical fact among SERVICEABLE",
            critical_wrong,
            len(critical_runs),
            0.01,
            ">1%",
        )
        _rate_row(measures, "Untraceable refusal", untraceable, len(runs), 0, "any")
        _rate_row(
            measures,
            "Run-to-run disagreement",
            len(disagreements),
            len(repeated),
            0.05,
            ">5%",
            "Cases differing on family, hard stop or destination; single runs unmeasured.",
        )
        times = [s.research_seconds for s in runs if s.research_ran]
        middle = float(median(times)) if times else None
        measures.append(
            Measure(
                "Median research time",
                None,
                len(times),
                middle,
                ">300 seconds",
                None if middle is None else middle <= 300,
            )
        )
        costs = [s.prehold_cost_cents + s.research_cost_cents for s in runs]
        mean_cost = sum(costs) / len(costs) if costs else None
        measures.append(
            Measure(
                "AI cost per ticket",
                sum(costs),
                len(costs),
                mean_cost,
                "owner's economics line",
                None
                if mean_cost is None or ai_cost_limit_cents is None
                else mean_cost <= ai_cost_limit_cents,
                "Cents, AI only; brief reference ~$0.123 AI+search per letter "
                "(not directly comparable).",
            )
        )
        _rate_row(
            measures,
            "Pre-hold cap exceeded",
            sum(s.prehold_cost_cents > 25 for s in runs),
            len(runs),
            0,
            "$0.25 cap",
        )
        measures.append(
            Measure(
                "Coverage",
                sum(s.outcome_family == "SERVICEABLE" for s in key_served),
                len(key_served),
                coverage,
                "not blocking",
                None,
            )
        )
        measures.append(
            Measure(
                "NYV share",
                sum(s.outcome_family == "NOT_YET_VERIFIED" for s in runs),
                len(runs),
                _rate(sum(s.outcome_family == "NOT_YET_VERIFIED" for s in runs), len(runs)),
                "not blocking",
                None,
            )
        )
        friction: Counter[tuple[Literal["VetoCode", "research"], str]] = Counter()
        for s in key_served:
            if s.outcome_family == "SERVICEABLE":
                continue
            friction.update(
                ("VetoCode", code) for code in set(s.stopping_veto_codes) if code in set(VetoCode)
            )
            friction.update(("research", reason) for reason in set(s.research_reasons))
        permission = (permitted_production_use or {}).get(preset)
        eligibility: list[str] = []
        for failed, reason in (
            (not runs, "no scored runs"),
            (wrong > 0, "wrong SERVICEABLE"),
            (bad_destination > 0, "wrong destination or purpose"),
            (untraceable > 0, "untraceable refusal"),
            (bool(disagreements), "run-to-run disagreement"),
            (len(repeated) < len(by_case), "repeat evidence incomplete"),
            (permission is not True, "permitted production use not confirmed"),
        ):
            if failed:
                eligibility.append(reason)
        flags: list[str] = []
        if wrong:
            flags.append("wrong SERVICEABLE")
        if untraceable:
            flags.append("untraceable refusal")
        if runs and wrong_pi / len(runs) > 0.02:
            flags.append("wrong PI above 2%")
        if preset == leading_model and coverage is not None and coverage < 0.5:
            flags.append("leading model coverage below 50%")
        result.append(
            ModelAggregate(
                preset,
                len(runs),
                len(all_runs) - len(runs),
                tuple(measures),
                tuple(
                    Friction(kind, reason, count)
                    for (kind, reason), count in sorted(friction.items())
                ),
                disagreements,
                len(repeated),
                correct,
                len(key_served),
                coverage,
                permission,
                not eligibility,
                tuple(eligibility),
                tuple(flags),
                FailureRate(failures=wrong, trials=len(runs)),
            )
        )
    return tuple(result)
