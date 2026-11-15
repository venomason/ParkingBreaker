"""Deterministic Markdown projection of pure bench scores; no filesystem or clock."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict

from parkingbreaker.bench.score import ModelAggregate, RunScore


def _cell(value: object) -> str:
    if value is None:
        return "unscored"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\r", " ")
        .replace("\n", "<br>")
    )


def _table(headers: tuple[str, ...], rows: Iterable[tuple[object, ...]]) -> list[str]:
    return [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
        *("| " + " | ".join(_cell(v) for v in row) + " |" for row in rows),
        "",
    ]


def _joined(values: tuple[str, ...]) -> str:
    return "; ".join(values) or "—"


def _percent(value: float | None, none_text: str) -> str:
    return none_text if value is None else f"{100 * value:.2f}%"


def render_markdown(aggregates: Iterable[ModelAggregate], scores: Iterable[RunScore]) -> str:
    """Stable under permutation, including repeats with the same case/date/model.

    No report timestamp, implicit winner, invented stage score or inferred permission.
    Final-key sign-off and the absence of a random set remain visible.
    """
    models = sorted(aggregates, key=lambda a: a.preset)
    runs = sorted(
        scores,
        key=lambda s: (s.preset, s.case_id, json.dumps(asdict(s), sort_keys=True, default=str)),
    )
    lines = [
        "# Phase 0 qualification bench",
        "",
        "Controls only: this case set is not the random stratified v7 §9 release gate. "
        "Uncertain fields and excluded cases are unscored. Stage not represented in research; "
        "frozen packet and letter fidelity remain unmeasured. "
        "Coverage is run-weighted; disagreement is case-weighted over repeated cases.",
        "",
    ]
    if not models:
        lines.extend(["No model aggregates.", ""])
    for model in models:
        lines.extend(
            [
                f"## {_cell(model.preset)}",
                "",
                f"Scored runs: {model.scored_runs}; excluded runs: {model.excluded_runs}. "
                f"Eligible: {_cell(model.eligible)}. "
                f"Production use confirmed: {_cell(model.permitted_production_use)}.",
                "",
                "Eligibility findings: " + _joined(model.eligibility_reasons) + ".",
                "",
                "Back to guards before Phase 1: " + _joined(model.back_to_guards) + ".",
                "",
                f"Correct SERVICEABLE on key-SERVICEABLE runs: {model.correct_serviceable}/"
                f"{model.key_serviceable_runs}. Repeated cases: {model.repeated_cases}. "
                "Disagreeing cases: " + _joined(model.disagreement_cases) + ".",
                "",
            ]
        )
        unsafe = model.unsafe_acceptances
        lines.extend(
            [
                f"Unsafe acceptances: {unsafe.failures}/{unsafe.trials}; "
                f"point estimate {_percent(unsafe.point_estimate, 'unscored')}.",
                "Rule-of-three 95% upper bound (zero failures): "
                f"{_percent(unsafe.rule_of_three_bound, 'not applicable')}.",
                "",
            ]
        )
        rows: list[tuple[object, ...]] = []
        for measure in model.measures:
            count = (
                "—" if measure.numerator is None else f"{measure.numerator}/{measure.denominator}"
            )
            value = (
                "unmeasured"
                if measure.value is None
                else f"{measure.value:.3f}"
                if measure.name in ("Median research time", "AI cost per ticket")
                else f"{100 * measure.value:.2f}%"
            )
            status = (
                "not assessed" if measure.passed is None else "pass" if measure.passed else "FAIL"
            )
            rows.append((measure.name, count, value, measure.limit, status, measure.note))
        lines.extend(
            _table(("Measure", "Count", "Value", "v7 §9 limit / reference", "Result", "Note"), rows)
        )
        lines.extend(["### Guard friction on key-SERVICEABLE cases", ""])
        lines.extend(
            _table(
                ("Kind", "Guard / research reason", "Stopped runs"),
                ((f.kind, f.reason, f.runs) for f in model.friction),
            )
        )
        model_runs = [s for s in runs if s.preset == model.preset and s.verdict != "excluded"]
        distribution = Counter(
            (s.production_outcome, s.research_outcome, s.hard_stop) for s in model_runs
        )
        lines.extend(["### Outcome distribution", ""])
        lines.extend(
            _table(
                ("Production outcome", "Research outcome", "Hard stop", "Runs"),
                (
                    (*key, count)
                    for key, count in sorted(distribution.items(), key=lambda pair: str(pair[0]))
                ),
            )
        )
    lines.extend(["## Per-run scores", ""])
    lines.extend(
        _table(
            (
                "Model",
                "Case",
                "Run",
                "Commit / key",
                "Production outcome",
                "Research outcome",
                "Hard stop",
                "Veto codes",
                "Research critical-fact reasons",
                "Issuer correct?",
                "Deciding body correct?",
                "Destination correct?",
                "Purpose correct?",
                "Stage",
                "Deadline correct?",
                "Items correct?",
                "Declarations correct?",
                "Unfulfillable items clear?",
                "Mail-in accepted?",
                "Verdict",
                "Notes",
            ),
            (
                (
                    s.preset,
                    s.case_id,
                    i,
                    f"{s.commit} / v{s.key_version}",
                    s.production_outcome,
                    s.research_outcome,
                    s.hard_stop,
                    _joined(s.veto_codes),
                    _joined(s.research_reasons),
                    s.issuer_correct,
                    s.deciding_body_correct,
                    s.destination_correct,
                    s.purpose_correct,
                    s.stage_note,
                    s.deadline_correct,
                    s.required_items_correct,
                    s.required_declarations_correct,
                    s.unfulfillable_items_clear,
                    s.mail_in_correct,
                    s.verdict,
                    _joined(s.notes),
                )
                for i, s in enumerate(runs, 1)
            ),
        )
    )
    lines.extend(["## Per-run facts, cost and time", ""])
    lines.extend(
        _table(
            (
                "Model",
                "Case",
                "Run",
                "Date",
                "Destination",
                "Days",
                "Trigger",
                "Counting",
                "Computed date",
                "Key date",
                "Items",
                "Declarations",
                "AI cents pre-hold",
                "AI cents research",
                "Brave requests",
                "Seconds pre-hold",
                "Seconds research",
                "Empty AI answers",
            ),
            (
                (
                    s.preset,
                    s.case_id,
                    i,
                    s.run_date,
                    s.destination,
                    s.deadline_days,
                    s.deadline_trigger,
                    s.deadline_counting,
                    s.computed_deadline,
                    s.expected_deadline,
                    _joined(s.required_items),
                    _joined(s.required_declarations),
                    s.prehold_cost_cents,
                    s.research_cost_cents,
                    s.brave_requests,
                    f"{s.prehold_seconds:.3f}",
                    f"{s.research_seconds:.3f}",
                    s.empty_ai_answers,
                )
                for i, s in enumerate(runs, 1)
            ),
        )
    )
    lines.extend(["## Exclusions, uncertainty and sign-off", ""])
    lines.extend(
        _table(
            ("Model", "Case", "Run", "Verdict", "Uncertain fields", "Owner signed", "Notes"),
            (
                (
                    s.preset,
                    s.case_id,
                    i,
                    s.verdict,
                    _joined(s.uncertain_fields),
                    s.owner_signed,
                    _joined(s.notes),
                )
                for i, s in enumerate(runs, 1)
                if s.verdict == "excluded" or s.uncertain_fields or not s.owner_signed
            ),
        )
    )
    lines.extend(
        [
            "The owner must sign the trap answers and unresolved cases before scores are final. "
            "Guard-friction findings are candidates for review only; no guards are changed. "
            "The low-coverage back-to-guards flag requires an explicitly supplied leading model. "
            "Search cost and commercial permission cannot be inferred from these runs.",
            "",
        ]
    )
    return "\n".join(lines)
