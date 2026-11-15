"""Versioned canonical JSON codecs for durable research evidence records.

This module is a pure boundary codec: it never performs I/O or makes research decisions.
"""

from __future__ import annotations

import base64
import json
import math
import re
from datetime import date, datetime
from enum import Enum
from typing import cast

from parkingbreaker.agencies import IssuerType, ResolvedAgency, SourceScope
from parkingbreaker.case_state import HardStop
from parkingbreaker.json_types import JsonValue
from parkingbreaker.llm import Usage
from parkingbreaker.research.findings import (
    EvidenceBasis,
    Finding,
    FindingStatus,
    ReleaseDecision,
    UnknownReason,
    complete_findings,
)
from parkingbreaker.research.ladder import (
    Ask,
    CountingChoice,
    CountingRule,
    CriticalFact,
    CustomerQuestion,
    DeadlineChoice,
    Default,
    DefaultRule,
    LadderStep,
    Recover,
    RecoveryAction,
    Stop,
    StopReason,
)
from parkingbreaker.research.payment_effects import (
    PaymentRule,
    PaymentScope,
    payment_rule_from_data,
    payment_scope_from_data,
)
from parkingbreaker.research.procedure import assessment_data, parse_assessment
from parkingbreaker.research.questions import (
    ANSWERABLE_KEYS,
    CustomerPosition,
    EffectiveTiers,
    FactKey,
    PacketPlan,
    Question,
    RequirementEvidence,
    Ticket,
    VerifiedRequirements,
    effective_tiers,
)
from parkingbreaker.research.resolution import (
    NoticeConflict,
    PendingResearch,
    RecoveryProgress,
    ResearchAnswers,
    ResearchResolution,
    VehicleRelationship,
)
from parkingbreaker.research.run import ResearchAttempt, ResearchResult
from parkingbreaker.research.sources import (
    AuthorityClass,
    EvidenceArtifact,
    EvidenceManifest,
    FetchFailure,
    SourceRecord,
)

_VERSION = 1
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def _object(value: object, fields: set[str], where: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
        raise ValueError(f"{where} must be an object")
    result = cast(dict[str, object], value)
    missing = fields - result.keys()
    extra = result.keys() - fields
    if missing or extra:
        raise ValueError(
            f"{where} fields invalid (missing={sorted(missing)}, extra={sorted(extra)})"
        )
    return result


def _str(value: object, where: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{where} must be a string")
    return value


def _bool(value: object, where: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{where} must be a boolean")
    return value


def _int(value: object, where: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{where} must be an integer >= {minimum}")
    return value


def _enum[E: Enum](enum_type: type[E], value: object, where: str) -> E:
    raw = _str(value, where)
    try:
        return enum_type(raw)
    except ValueError as exc:
        raise ValueError(f"{where} has an unknown value") from exc


def _date(value: object, where: str) -> date:
    raw = _str(value, where)
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{where} must be an ISO date") from exc
    if parsed.isoformat() != raw:
        raise ValueError(f"{where} must use canonical ISO date form")
    return parsed


def _datetime(value: object, where: str) -> datetime:
    raw = _str(value, where)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{where} must be an ISO datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{where} must be timezone-aware")
    if parsed.isoformat() != raw:
        raise ValueError(f"{where} must use canonical ISO datetime form")
    return parsed


def _json_value(value: object, where: str = "value") -> JsonValue:
    if value is None or isinstance(value, str) or type(value) is bool:
        return value
    if type(value) is int:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{where} must be finite")
        return value
    if isinstance(value, list):
        return [_json_value(item, where) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: _json_value(item, where) for key, item in value.items()}
    raise ValueError(f"{where} is not a JSON value")


def _str_tuple(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{where} must be an array")
    return tuple(_str(item, where) for item in value)


def _strings(value: tuple[str, ...]) -> list[str]:
    return list(value)


def _finding_data(f: Finding) -> dict[str, object]:
    data: dict[str, object] = {
        "key": f.key.value,
        "value": _json_value(f.value),
        "status": f.status.value,
        "verbatim_quote": f.verbatim_quote,
        "source_sha256s": _strings(f.source_sha256s),
        "notes": f.notes,
    }
    for name in ("unknown_reason", "jurisdiction", "stage", "evidence_basis", "scope_quote"):
        value = getattr(f, name)
        if value is not None:
            data[name] = value
    if f.procedure_assessment is not None:
        data["procedure_assessment"] = assessment_data(f.procedure_assessment)
    if f.evidence:
        data["evidence"] = [_finding_data(item) for item in f.evidence]
    return data


def _finding(value: object) -> Finding:
    optional = {
        "unknown_reason",
        "jurisdiction",
        "stage",
        "evidence",
        "evidence_basis",
        "procedure_assessment",
        "scope_quote",
    }
    present = optional.intersection(value) if isinstance(value, dict) else set()
    data = _object(
        value,
        {"key", "value", "status", "verbatim_quote", "source_sha256s", "notes"} | present,
        "finding",
    )
    quote = data["verbatim_quote"]
    if quote is not None:
        quote = _str(quote, "finding.verbatim_quote")
    shas = _str_tuple(data["source_sha256s"], "finding.source_sha256s")
    status = _enum(FindingStatus, data["status"], "finding.status")
    if status is FindingStatus.CONFIRMED and any(_HASH.fullmatch(sha) is None for sha in shas):
        raise ValueError("confirmed finding provenance contains an invalid hash")
    assessment = parse_assessment(data.get("procedure_assessment"))
    if "procedure_assessment" in data and assessment is None:
        raise ValueError("malformed procedure assessment")
    return Finding(
        key=_enum(FactKey, data["key"], "finding.key"),
        value=_json_value(data["value"], "finding.value"),
        status=status,
        verbatim_quote=quote,
        source_sha256s=shas,
        notes=_str(data["notes"], "finding.notes"),
        unknown_reason=_enum(UnknownReason, data["unknown_reason"], "finding.unknown_reason")
        if "unknown_reason" in data
        else None,
        jurisdiction=_str(data["jurisdiction"], "finding.jurisdiction")
        if "jurisdiction" in data
        else None,
        stage=_str(data["stage"], "finding.stage") if "stage" in data else None,
        evidence_basis=_enum(EvidenceBasis, data["evidence_basis"], "finding.evidence_basis")
        if "evidence_basis" in data
        else None,
        procedure_assessment=assessment,
        scope_quote=_str(data["scope_quote"], "finding.scope_quote")
        if "scope_quote" in data
        else None,
        evidence=tuple(
            _finding(item) for item in _array(data.get("evidence", []), "finding.evidence")
        ),
    )


def _ticket_data(ticket: Ticket) -> dict[str, object]:
    data: dict[str, object] = {
        "city_id": ticket.city_id,
        "violation_code": ticket.violation_code,
        "violation_description": ticket.violation_description,
        "issue_date": ticket.issue_date.isoformat(),
    }
    for name in ("jurisdiction", "stage"):
        value = getattr(ticket, name)
        if value is not None:
            data[name] = value
    return data


def _ticket(value: object) -> Ticket:
    data = _object(
        value,
        {"city_id", "violation_code", "violation_description", "issue_date"}
        | ({"jurisdiction", "stage"}.intersection(value) if isinstance(value, dict) else set()),
        "ticket",
    )
    return Ticket(
        _str(data["city_id"], "ticket.city_id"),
        _str(data["violation_code"], "ticket.violation_code"),
        _str(data["violation_description"], "ticket.violation_description"),
        _date(data["issue_date"], "ticket.issue_date"),
        _str(data["jurisdiction"], "ticket.jurisdiction") if "jurisdiction" in data else None,
        _str(data["stage"], "ticket.stage") if "stage" in data else None,
    )


def _question_data(question: Question) -> dict[str, object]:
    return {
        "key": question.key.value,
        "critical": question.critical,
        "ask": question.ask,
        "value_format": question.value_format,
    }


def _question(value: object) -> Question:
    data = _object(value, {"key", "critical", "ask", "value_format"}, "question")
    return Question(
        _enum(FactKey, data["key"], "question.key"),
        _bool(data["critical"], "question.critical"),
        _str(data["ask"], "question.ask"),
        _str(data["value_format"], "question.value_format"),
    )


def _source_data(source: SourceRecord) -> dict[str, object]:
    data: dict[str, object] = {
        "url": source.url,
        "final_url": source.final_url,
        "retrieved_at": source.retrieved_at.isoformat(),
        "http_status": source.http_status,
        "sha256": source.sha256,
        "content_type": source.content_type,
        "text": source.text,
        "authority_class": source.authority_class.value,
    }
    if source.linked_urls:
        data["linked_urls"] = list(source.linked_urls)
    if source.etag is not None:
        data["etag"] = source.etag
    if source.jurisdiction is not None:
        data["jurisdiction"] = source.jurisdiction
    return data


def _source(value: object) -> SourceRecord:
    data = _object(
        value,
        {
            "url",
            "final_url",
            "retrieved_at",
            "http_status",
            "sha256",
            "content_type",
            "text",
            "authority_class",
        }
        | (
            {"linked_urls", "etag", "jurisdiction"}.intersection(value)
            if isinstance(value, dict)
            else set()
        ),
        "source",
    )
    sha = _str(data["sha256"], "source.sha256")
    status = _int(data["http_status"], "source.http_status", minimum=100)
    if status > 599 or _HASH.fullmatch(sha) is None:
        raise ValueError("source status or hash is invalid")
    return SourceRecord(
        _str(data["url"], "source.url"),
        _str(data["final_url"], "source.final_url"),
        _datetime(data["retrieved_at"], "source.retrieved_at"),
        status,
        sha,
        _str(data["content_type"], "source.content_type"),
        _str(data["text"], "source.text"),
        _enum(AuthorityClass, data["authority_class"], "source.authority_class"),
        linked_urls=_str_tuple(data["linked_urls"], "source.linked_urls")
        if "linked_urls" in data
        else (),
        etag=_str(data["etag"], "source.etag") if "etag" in data else None,
        jurisdiction=_str(data["jurisdiction"], "source.jurisdiction")
        if "jurisdiction" in data
        else None,
    )


def _failure_data(failure: FetchFailure) -> dict[str, object]:
    return {"url": failure.url, "reason": failure.reason, "retryable": failure.retryable}


def _failure(value: object) -> FetchFailure:
    data = _object(value, {"url", "reason", "retryable"}, "failure")
    return FetchFailure(
        _str(data["url"], "failure.url"),
        _str(data["reason"], "failure.reason"),
        _bool(data["retryable"], "failure.retryable"),
    )


def _usage_data(usage: Usage) -> dict[str, object]:
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cost_cents": usage.cost_cents,
    }


def _usage(value: object) -> Usage:
    data = _object(value, {"input_tokens", "output_tokens", "cost_cents"}, "usage")
    return Usage(
        *(
            _int(data[name], f"usage.{name}")
            for name in ("input_tokens", "output_tokens", "cost_cents")
        )
    )


def _attempt_data(attempt: ResearchAttempt) -> dict[str, object]:
    return {"stage": attempt.stage, "url": attempt.url, "error": attempt.error}


def _attempt(value: object) -> ResearchAttempt:
    data = _object(value, {"stage", "url", "error"}, "attempt")
    url, error = data["url"], data["error"]
    return ResearchAttempt(
        _str(data["stage"], "attempt.stage"),
        None if url is None else _str(url, "attempt.url"),
        None if error is None else _str(error, "attempt.error"),
    )


def _pending_data(pending: PendingResearch) -> dict[str, object]:
    step = pending.step
    if isinstance(step, Recover):
        step_data: dict[str, object] = {"type": "recover", "action": step.action.value}
    elif isinstance(step, Ask):
        step_data = {"type": "ask", "question": step.question.value}
    elif isinstance(step, Default):
        step_data = {"type": "default", "rule": step.rule.value}
    elif isinstance(step, Stop):
        step_data = {"type": "stop", "reason": step.reason.value}
        if step.hard_stop is not None:
            step_data["hard_stop"] = step.hard_stop.value
        if step.official_quote is not None:
            step_data["official_quote"] = step.official_quote
    else:
        raise ValueError("unsupported ladder step")
    return {
        "fact": pending.fact.value,
        "keys": [key.value for key in pending.keys],
        "step": step_data,
        "official_addresses": [_finding_data(f) for f in pending.official_addresses],
    }


def _pending(value: object) -> PendingResearch:
    data = _object(value, {"fact", "keys", "step", "official_addresses"}, "pending")
    keys = data["keys"]
    if not isinstance(keys, list):
        raise ValueError("pending.keys must be an array")
    step_data = data["step"]
    if not isinstance(step_data, dict):
        raise ValueError("pending.step must be an object")
    tag = step_data.get("type")
    if tag == "recover":
        fields = _object(step_data, {"type", "action"}, "pending.step")
        step: LadderStep = Recover(_enum(RecoveryAction, fields["action"], "pending.step.action"))
    elif tag == "ask":
        fields = _object(step_data, {"type", "question"}, "pending.step")
        step = Ask(_enum(CustomerQuestion, fields["question"], "pending.step.question"))
    elif tag == "default":
        fields = _object(step_data, {"type", "rule"}, "pending.step")
        step = Default(_enum(DefaultRule, fields["rule"], "pending.step.rule"))
    elif tag == "stop":
        allowed = {"type", "reason"}
        if "hard_stop" in step_data:
            allowed.add("hard_stop")
        if "official_quote" in step_data:
            allowed.add("official_quote")
        fields = _object(step_data, allowed, "pending.step")
        step = Stop(
            _enum(StopReason, fields["reason"], "pending.step.reason"),
            _enum(HardStop, fields["hard_stop"], "pending.step.hard_stop")
            if "hard_stop" in fields
            else None,
            _str(fields["official_quote"], "pending.step.official_quote")
            if "official_quote" in fields
            else None,
        )
    else:
        raise ValueError("pending.step has an unknown type")
    addresses = data["official_addresses"]
    if not isinstance(addresses, list):
        raise ValueError("pending.official_addresses must be an array")
    return PendingResearch(
        _enum(CriticalFact, data["fact"], "pending.fact"),
        tuple(_enum(FactKey, key, "pending.keys") for key in keys),
        step,
        tuple(_finding(item) for item in addresses),
    )


def _progress_data(progress: RecoveryProgress) -> dict[str, object]:
    return {
        "re_extraction_done": progress.re_extraction_done,
        "broadened_search_done": progress.broadened_search_done,
    }


def _progress(value: object) -> RecoveryProgress:
    data = _object(value, {"re_extraction_done", "broadened_search_done"}, "progress")
    return RecoveryProgress(
        _bool(data["re_extraction_done"], "progress.re_extraction_done"),
        _bool(data["broadened_search_done"], "progress.broadened_search_done"),
    )


def _answers_data(answers: ResearchAnswers) -> dict[str, object]:
    data: dict[str, object] = {
        "notice_issue_date": answers.notice_issue_date.isoformat()
        if answers.notice_issue_date
        else None,
        "printed_deadline": answers.printed_deadline.isoformat()
        if answers.printed_deadline
        else None,
        "address_choice": answers.address_choice,
        "prior_step_required": answers.prior_step_required,
        "answered": sorted(question.value for question in answers.answered),
    }
    # Additive fields are omitted when absent so earlier canonical payloads are unchanged.
    for name in _OPTIONAL_ANSWER_FIELDS:
        if getattr(answers, name) is not None:
            data[name] = getattr(answers, name)
    return data


_OPTIONAL_ANSWER_FIELDS = (
    "issuing_agency",
    "notice_stage",
    "vehicle_relationship",
    "initial_review_completed",
    "paid_fine",
    "paid_hearing_deposit",
    "entered_plan",
)


def _answers(value: object) -> ResearchAnswers:
    present = set(value) & set(_OPTIONAL_ANSWER_FIELDS) if isinstance(value, dict) else set()
    data = _object(
        value,
        {
            "notice_issue_date",
            "printed_deadline",
            "address_choice",
            "prior_step_required",
            "answered",
        }
        | present,
        "answers",
    )
    issue, deadline = data["notice_issue_date"], data["printed_deadline"]
    choice, prior, answered = data["address_choice"], data["prior_step_required"], data["answered"]
    if choice is not None:
        choice = _str(choice, "answers.address_choice")
    if prior is not None:
        prior = _bool(prior, "answers.prior_step_required")
    if not isinstance(answered, list):
        raise ValueError("answers.answered must be an array")
    questions = tuple(_enum(CustomerQuestion, item, "answers.answered") for item in answered)
    if len(questions) != len(set(questions)):
        raise ValueError("answers.answered contains duplicates")
    return ResearchAnswers(
        None if issue is None else _date(issue, "answers.notice_issue_date"),
        None if deadline is None else _date(deadline, "answers.printed_deadline"),
        choice,
        prior,
        frozenset(questions),
        issuing_agency=_str(data["issuing_agency"], "answers.issuing_agency")
        if "issuing_agency" in data
        else None,
        notice_stage=_str(data["notice_stage"], "answers.notice_stage")
        if "notice_stage" in data
        else None,
        initial_review_completed=_bool(
            data["initial_review_completed"], "answers.initial_review_completed"
        )
        if "initial_review_completed" in data
        else None,
        vehicle_relationship=_enum(
            VehicleRelationship, data["vehicle_relationship"], "answers.vehicle_relationship"
        )
        if "vehicle_relationship" in data
        else None,
        paid_fine=_bool(data["paid_fine"], "answers.paid_fine") if "paid_fine" in data else None,
        paid_hearing_deposit=_bool(data["paid_hearing_deposit"], "answers.paid_hearing_deposit")
        if "paid_hearing_deposit" in data
        else None,
        entered_plan=_bool(data["entered_plan"], "answers.entered_plan")
        if "entered_plan" in data
        else None,
    )


def _resolution_data(resolution: ResearchResolution) -> dict[str, object]:
    deadline = resolution.deadline
    return {
        "mailing_address": None
        if resolution.mailing_address is None
        else _finding_data(resolution.mailing_address),
        "deadline": None
        if deadline is None
        else {
            "deadline": deadline.deadline.isoformat(),
            "official": deadline.official.isoformat() if deadline.official else None,
            "notice": deadline.notice.isoformat() if deadline.notice else None,
            "disclose": deadline.disclose,
        },
        "official_deadline_candidates": [
            item.isoformat() for item in resolution.official_deadline_candidates
        ],
        "counting": None
        if resolution.counting is None
        else {"rule": resolution.counting.rule.value, "disclose": resolution.counting.disclose},
        "pending": [_pending_data(item) for item in resolution.pending],
        "customer_answers": _answers_data(resolution.customer_answers),
        "timing_disclosures": _strings(resolution.timing_disclosures),
        **(
            {
                "notice_conflicts": [
                    {
                        "field": c.field,
                        "printed_value": c.printed_value,
                        "official_value": c.official_value,
                        "printed_quote": c.printed_quote,
                        "official_quote": c.official_quote,
                    }
                    for c in resolution.notice_conflicts
                ]
            }
            if resolution.notice_conflicts
            else {}
        ),
        **(
            {
                "fulfilled_requirements": [
                    _finding_data(f) for f in resolution.fulfilled_requirements
                ]
            }
            if resolution.fulfilled_requirements
            else {}
        ),
        **({"awaiting_packet": True} if resolution.awaiting_packet else {}),
    }


def _notice_conflict(value: object) -> NoticeConflict:
    c = _object(
        value,
        {"field", "printed_value", "official_value", "printed_quote", "official_quote"},
        "notice conflict",
    )
    return NoticeConflict(
        _str(c["field"], "conflict field"),
        cast(JsonValue, c["printed_value"]),
        cast(JsonValue, c["official_value"]),
        _str(c["printed_quote"], "printed quote"),
        _str(c["official_quote"], "official quote"),
    )


def _resolution(value: object) -> ResearchResolution:
    data = _object(
        value,
        {
            "mailing_address",
            "deadline",
            "official_deadline_candidates",
            "counting",
            "pending",
            "customer_answers",
            "timing_disclosures",
        }
        | (
            {"fulfilled_requirements", "notice_conflicts", "awaiting_packet"}.intersection(value)
            if isinstance(value, dict)
            else set()
        ),
        "resolution",
    )
    mailing = None if data["mailing_address"] is None else _finding(data["mailing_address"])
    raw_deadline = data["deadline"]
    deadline = None
    if raw_deadline is not None:
        d = _object(
            raw_deadline, {"deadline", "official", "notice", "disclose"}, "resolution.deadline"
        )
        official, notice = d["official"], d["notice"]
        deadline = DeadlineChoice(
            _date(d["deadline"], "resolution.deadline.deadline"),
            None if official is None else _date(official, "resolution.deadline.official"),
            None if notice is None else _date(notice, "resolution.deadline.notice"),
            _bool(d["disclose"], "resolution.deadline.disclose"),
        )
    candidates = data["official_deadline_candidates"]
    pending = data["pending"]
    if not isinstance(candidates, list) or not isinstance(pending, list):
        raise ValueError("resolution candidate and pending fields must be arrays")
    raw_counting = data["counting"]
    counting = None
    if raw_counting is not None:
        c = _object(raw_counting, {"rule", "disclose"}, "resolution.counting")
        counting = CountingChoice(
            _enum(CountingRule, c["rule"], "resolution.counting.rule"),
            _bool(c["disclose"], "resolution.counting.disclose"),
        )
    return ResearchResolution(
        mailing,
        deadline,
        tuple(_date(item, "resolution.official_deadline_candidates") for item in candidates),
        counting,
        tuple(_pending(item) for item in pending),
        _answers(data["customer_answers"]),
        _str_tuple(data["timing_disclosures"], "resolution.timing_disclosures"),
        tuple(_finding(f) for f in _array(data["fulfilled_requirements"], "fulfilled_requirements"))
        if "fulfilled_requirements" in data
        else (),
        tuple(
            _notice_conflict(v)
            for v in _array(data.get("notice_conflicts", []), "notice conflicts")
        ),
        _bool(data.get("awaiting_packet", False), "resolution.awaiting_packet"),
    )


def _array(value: object, where: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{where} must be an array")
    return cast(list[object], value)


def _evidence_data(evidence: RequirementEvidence | None) -> object:
    return (
        None
        if evidence is None
        else {
            "verbatim_quote": evidence.verbatim_quote,
            "source_sha256s": list(evidence.source_sha256s),
        }
    )


def _evidence(value: object) -> RequirementEvidence | None:
    if value is None:
        return None
    data = _object(value, {"verbatim_quote", "source_sha256s"}, "requirement evidence")
    return RequirementEvidence(
        _str(data["verbatim_quote"], "evidence.quote"),
        _str_tuple(data["source_sha256s"], "evidence.hashes"),
    )


def _evaluation_data(evaluation: EffectiveTiers) -> dict[str, object]:
    return {
        "tiers": dict(evaluation.tiers),
        "promotions": [
            {
                "key": p.key,
                "reason": p.reason,
                "evidence": _evidence_data(p.evidence),
                "customer_value": p.customer_value,
            }
            for p in evaluation.promotions
        ],
        "compulsory_gaps": [_evidence_data(e) for e in evaluation.compulsory_gaps],
    }


def _tier_context_data(result: ResearchResult) -> dict[str, object]:
    requirements, position, plan = (
        result.verified_requirements,
        result.customer_position,
        result.packet_plan,
    )
    evaluation = effective_tiers(result.ticket, requirements, position, plan)
    return {
        "requirements": {
            "code_required": _evidence_data(requirements.code_required),
            "amount_required": _evidence_data(requirements.amount_required),
            "compulsory_conditions": [
                _evidence_data(e) for e in requirements.compulsory_conditions
            ],
        },
        "position": {
            "exception_basis": position.exception_basis,
            "code_citation": position.code_citation,
        },
        "packet": {
            "code_required": _evidence_data(plan.code_required),
            "fulfilled_items": sorted(plan.fulfilled_items),
            **(
                {
                    "form_templates": [
                        base64.b64encode(b).decode("ascii") for b in plan.form_templates
                    ]
                }
                if plan.form_templates
                else {}
            ),
            **(
                {"customer_fields": [list(pair) for pair in plan.customer_fields]}
                if plan.customer_fields
                else {}
            ),
            **(
                {"frozen_pdf": base64.b64encode(plan.frozen_pdf).decode("ascii")}
                if plan.frozen_pdf is not None
                else {}
            ),
        },
        "evaluation": _evaluation_data(evaluation),
    }


def _customer_field(value: object) -> tuple[str, str]:
    pair = _str_tuple(value, "customer field")
    if len(pair) != 2:
        raise ValueError("customer field must be a key and value pair")
    return pair[0], pair[1]


def _tier_context(
    value: object, ticket: Ticket
) -> tuple[VerifiedRequirements, CustomerPosition, PacketPlan, EffectiveTiers]:
    data = _object(value, {"requirements", "position", "packet", "evaluation"}, "tier_context")
    req = _object(
        data["requirements"],
        {"code_required", "amount_required", "compulsory_conditions"},
        "requirements",
    )
    conditions = tuple(_evidence(e) for e in _array(req["compulsory_conditions"], "conditions"))
    if any(e is None for e in conditions):
        raise ValueError("compulsory conditions cannot be null")
    requirements = VerifiedRequirements(
        _evidence(req["code_required"]),
        _evidence(req["amount_required"]),
        tuple(e for e in conditions if e is not None),
    )
    pos = _object(data["position"], {"exception_basis", "code_citation"}, "position")
    position = CustomerPosition(
        *(
            _str(pos[k], k) if pos[k] is not None else None
            for k in ("exception_basis", "code_citation")
        )
    )
    packet_data = data["packet"]
    present = (
        {"frozen_pdf", "form_templates", "customer_fields"}.intersection(packet_data)
        if isinstance(packet_data, dict)
        else set()
    )
    packet = _object(packet_data, {"code_required", "fulfilled_items"} | present, "packet")
    plan = PacketPlan(
        _evidence(packet["code_required"]),
        frozenset(_str_tuple(packet["fulfilled_items"], "fulfilled_items")),
        base64.b64decode(_str(packet["frozen_pdf"], "frozen_pdf"), validate=True)
        if "frozen_pdf" in packet
        else None,
        tuple(
            base64.b64decode(_str(v, "form template"), validate=True)
            for v in _array(packet.get("form_templates", []), "form_templates")
        ),
        tuple(
            _customer_field(v) for v in _array(packet.get("customer_fields", []), "customer_fields")
        ),
    )
    evaluation = effective_tiers(ticket, requirements, position, plan)
    if data["evaluation"] != _evaluation_data(evaluation):
        raise ValueError("saved tier evaluation does not match saved inputs")
    return requirements, position, plan, evaluation


def _envelope(kind: str, payload: object, version: int = _VERSION) -> str:
    return json.dumps(
        {"version": version, "kind": kind, "payload": payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _decode(text: str, kind: str) -> object:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def constant(value: str) -> None:
        raise ValueError(f"invalid JSON number: {value}")

    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("invalid research JSON") from exc
    data = _object(value, {"version", "kind", "payload"}, "envelope")
    if (
        type(data["version"]) is not int
        or data["version"] not in ({1, 2, 3, 4} if kind == "research" else {1})
        or data["kind"] != kind
    ):
        raise ValueError("unsupported research record version or kind")
    return data["payload"]


def _payment_scope_data(scope: PaymentScope) -> dict[str, object]:
    return {"issuer": scope.issuer, "deciding_body": scope.deciding_body, "stage": scope.stage}


def _payment_rule_data(rule: PaymentRule) -> dict[str, object]:
    return {
        "action": rule.action.value,
        "effect": rule.effect.value,
        "condition": rule.condition.value,
        "status": rule.status.value,
        "scope": None if rule.scope is None else _payment_scope_data(rule.scope),
        "verbatim_quote": rule.verbatim_quote,
        "scope_quote": rule.scope_quote,
        "source_sha256s": list(rule.source_sha256s),
    }


def research_json(result: ResearchResult) -> str:
    """Encode versioned evidence; preserve earlier canonical history."""
    payload = {
        "ticket": _ticket_data(result.ticket),
        "questions": [_question_data(q) for q in result.questions],
        "sources": [_source_data(s) for s in result.sources],
        "failures": [_failure_data(f) for f in result.failures],
        "findings": [_finding_data(f) for f in result.findings],
        "decision": {"ok": result.decision.ok, "reasons": _strings(result.decision.reasons)},
        "cost": _usage_data(result.cost),
        "started_at": result.started_at.isoformat(),
        "finished_at": result.finished_at.isoformat(),
        "candidates": [_finding_data(f) for f in result.candidates],
        "progress": _progress_data(result.progress),
        "pending": [_pending_data(p) for p in result.pending],
        "attempts": [_attempt_data(a) for a in result.attempts],
        "address_mismatch": result.address_mismatch,
    }
    if result.record_version >= 2:
        payload["tier_context"] = _tier_context_data(result)
        # Complete over the answerable keys plus any retired key this record actually
        # stored. A retired finding is never invented, but one that was genuinely
        # recorded survives re-encoding instead of being dropped as history.
        retired_stored = [f.key for f in result.findings if f.key not in ANSWERABLE_KEYS]
        keys = (*ANSWERABLE_KEYS, *dict.fromkeys(retired_stored))
        payload["findings"] = [_finding_data(f) for f in complete_findings(result.findings, keys)]
    if result.record_version >= 4:
        payload["manifest"] = _manifest_data(result.manifest)
    if result.payment_rules:
        if result.record_version < 4:
            raise ValueError("payment rules require research record version 4")
        if len(result.payment_rules) > 32:
            raise ValueError("payment rules exceed bounded inventory")
        payload["payment_rules"] = [_payment_rule_data(rule) for rule in result.payment_rules]
        payload["payment_scope"] = (
            _payment_scope_data(result.payment_scope) if result.payment_scope is not None else None
        )
    encoded = _envelope("research", payload, result.record_version)
    decode_research(encoded)
    return encoded


def decode_research(text: str) -> ResearchResult:
    """Decode historical records, procedure assessments and version-4 manifests."""
    decoded = _decode(text, "research")
    version = json.loads(text)["version"]
    data = _object(
        decoded,
        {
            "ticket",
            "questions",
            "sources",
            "failures",
            "findings",
            "decision",
            "cost",
            "started_at",
            "finished_at",
            "candidates",
            "progress",
            "pending",
            "attempts",
            "address_mismatch",
        }
        | ({"tier_context"} if version >= 2 else set())
        | ({"manifest"} if version >= 4 else set())
        | (
            {"payment_rules", "payment_scope"}
            if isinstance(decoded, dict) and "payment_rules" in decoded
            else set()
        ),
        "research",
    )
    if "payment_rules" in data and version < 4:
        raise ValueError("payment rules require research record version 4")
    if "payment_rules" in data and (
        not isinstance(data["payment_rules"], list) or len(data["payment_rules"]) > 32
    ):
        raise ValueError("payment rules exceed bounded inventory")

    def records(name: str) -> list[object]:
        value = data[name]
        if not isinstance(value, list):
            raise ValueError(f"research.{name} must be an array")
        return cast(list[object], value)

    sources = tuple(_source(x) for x in records("sources"))
    known_hashes = {s.sha256 for s in sources}
    findings = tuple(_finding(x) for x in records("findings"))
    candidates = tuple(_finding(x) for x in records("candidates"))
    pending = tuple(_pending(x) for x in records("pending"))

    def check_provenance(finding: Finding) -> None:
        if version < 3 and (
            finding.evidence_basis is not None or finding.procedure_assessment is not None
        ):
            raise ValueError("procedure assessment requires research record version 3")
        if (
            finding.status is FindingStatus.CONFIRMED
            and finding.procedure_assessment is not None
            and finding.procedure_assessment.source_sha256 not in known_hashes
        ):
            raise ValueError("procedure assessment references an absent source")
        if (
            finding.status is FindingStatus.CONFIRMED
            and not set(finding.source_sha256s) <= known_hashes
        ):
            raise ValueError("confirmed finding provenance references an absent source")
        for evidence in finding.evidence:
            check_provenance(evidence)

    for finding in (*findings, *candidates, *(f for p in pending for f in p.official_addresses)):
        check_provenance(finding)
    decision = _object(data["decision"], {"ok", "reasons"}, "decision")
    started, finished = (
        _datetime(data["started_at"], "research.started_at"),
        _datetime(data["finished_at"], "research.finished_at"),
    )
    if finished < started:
        raise ValueError("research times are out of order")
    ticket = _ticket(data["ticket"])
    requirements, position, plan, evaluation = (
        _tier_context(data["tier_context"], ticket)
        if version >= 2
        else (VerifiedRequirements(), CustomerPosition(), PacketPlan(), None)
    )
    result = ResearchResult(
        ticket,
        tuple(_question(x) for x in records("questions")),
        sources,
        tuple(_failure(x) for x in records("failures")),
        findings,
        ReleaseDecision(
            _bool(decision["ok"], "decision.ok"),
            _str_tuple(decision["reasons"], "decision.reasons"),
        ),
        _usage(data["cost"]),
        started,
        finished,
        candidates,
        _progress(data["progress"]),
        pending,
        tuple(_attempt(x) for x in records("attempts")),
        _bool(data["address_mismatch"], "address_mismatch"),
        requirements,
        position,
        plan,
        evaluation,
        version,
        payment_rules=tuple(payment_rule_from_data(item) for item in records("payment_rules"))
        if "payment_rules" in data
        else (),
        payment_scope=payment_scope_from_data(data["payment_scope"])
        if data.get("payment_scope") is not None
        else None,
    )
    if version >= 4 and _manifest(data["manifest"]) != result.manifest:
        raise ValueError("research manifest disagrees with saved sources or scope")
    return result


def _manifest_data(manifest: EvidenceManifest) -> dict[str, object]:
    return {
        "issuer": manifest.issuer,
        "stage": manifest.stage,
        "artifacts": [
            {
                "url": a.url,
                "final_url": a.final_url,
                "kind": a.kind,
                "retrieved_at": a.retrieved_at.isoformat(),
                "sha256": a.sha256,
                **({"etag": a.etag} if a.etag is not None else {}),
            }
            for a in manifest.artifacts
        ],
    }


def _manifest(value: object) -> EvidenceManifest:
    data = _object(value, {"issuer", "stage", "artifacts"}, "manifest")
    issuer, stage = _str(data["issuer"], "manifest.issuer"), _str(data["stage"], "manifest.stage")
    if not issuer.strip() or not stage.strip():
        raise ValueError("manifest scope must not be blank")
    artifacts: list[EvidenceArtifact] = []
    urls: set[str] = set()
    for item in _array(data["artifacts"], "manifest.artifacts"):
        a = _object(
            item,
            {"url", "final_url", "kind", "retrieved_at", "sha256"}
            | ({"etag"} if isinstance(item, dict) and "etag" in item else set()),
            "artifact",
        )
        sha = _str(a["sha256"], "artifact.sha256")
        url = _str(a["url"], "artifact.url")
        if _HASH.fullmatch(sha) is None or not url or url in urls:
            raise ValueError("invalid manifest artifact hash or duplicate URL")
        urls.add(url)
        artifacts.append(
            EvidenceArtifact(
                url,
                _str(a["final_url"], "artifact.final_url"),
                _str(a["kind"], "artifact.kind"),
                _datetime(a["retrieved_at"], "artifact.retrieved_at"),
                sha,
                _str(a["etag"], "artifact.etag") if "etag" in a else None,
            )
        )
    return EvidenceManifest(issuer, stage, tuple(artifacts))


def manifest_json(manifest: EvidenceManifest) -> str:
    """Version-1 canonical evidence manifest, independently usable by reuse callers."""
    encoded = _envelope("evidence_manifest", _manifest_data(manifest))
    decode_manifest(encoded)
    return encoded


def decode_manifest(text: str) -> EvidenceManifest:
    return _manifest(_decode(text, "evidence_manifest"))


def answers_json(answers: ResearchAnswers) -> str:
    encoded = _envelope("answers", _answers_data(answers))
    decode_answers(encoded)
    return encoded


def decode_answers(text: str) -> ResearchAnswers:
    return _answers(_decode(text, "answers"))


def resolution_json(resolution: ResearchResolution) -> str:
    encoded = _envelope("resolution", _resolution_data(resolution))
    decode_resolution(encoded)
    return encoded


def decode_resolution(text: str) -> ResearchResolution:
    return _resolution(_decode(text, "resolution"))


def agency_json(agency: ResolvedAgency) -> str:
    """The pre-hold's resolved agency, saved so research runs against the same identity."""
    payload = {
        "id": agency.id,
        "name": agency.name,
        "state": agency.state,
        "official_domains": list(agency.official_domains),
        "issuer_type": agency.issuer_type.value,
        "deciding_body": agency.deciding_body,
        "basis_key": agency.basis_key,
        "delegated_scopes": [
            {"host": scope.host, "path_prefix": scope.path_prefix}
            for scope in agency.delegated_scopes
        ],
    }
    encoded = _envelope("agency", payload, 1)
    decode_agency(encoded)
    return encoded


def decode_agency(text: str) -> ResolvedAgency:
    data = _object(
        _decode(text, "agency"),
        {
            "id",
            "name",
            "state",
            "official_domains",
            "issuer_type",
            "deciding_body",
            "basis_key",
            "delegated_scopes",
        },
        "agency",
    )
    scopes = []
    for item in _array(data["delegated_scopes"], "agency.delegated_scopes"):
        scope = _object(item, {"host", "path_prefix"}, "agency.delegated_scopes[]")
        prefix = scope["path_prefix"]
        scopes.append(
            SourceScope(
                _str(scope["host"], "scope.host"),
                None if prefix is None else _str(prefix, "scope.path_prefix"),
            )
        )
    return ResolvedAgency(
        _str(data["id"], "agency.id"),
        _str(data["name"], "agency.name"),
        _str(data["state"], "agency.state"),
        _str_tuple(data["official_domains"], "agency.official_domains"),
        issuer_type=_enum(IssuerType, data["issuer_type"], "agency.issuer_type"),
        deciding_body=_str(data["deciding_body"], "agency.deciding_body"),
        basis_key=_str(data["basis_key"], "agency.basis_key"),
        delegated_scopes=tuple(scopes),
    )
