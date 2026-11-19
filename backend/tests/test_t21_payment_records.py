"""Optional scoped rule evidence survives research storage and every existing admission gate."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta

import pytest

from parkingbreaker.research.ladder import CustomerQuestion
from parkingbreaker.research.payment_effects import PaymentCondition, PaymentEffect
from parkingbreaker.research.questions import ANSWERABLE_KEYS
from parkingbreaker.research.resolution import ResearchAnswers
from parkingbreaker.research.run import ResearchResult
from parkingbreaker.research_records import decode_research, research_json
from samples import source
from test_research_records import result_fixture
from test_t21_payment_policy import FINE, HEADING, SCOPE, rule


def payment_research() -> ResearchResult:
    original = result_fixture()
    return replace(
        original,
        ticket=replace(original.ticket, jurisdiction=SCOPE.issuer, stage=SCOPE.stage),
        payment_rules=(rule(),),
        payment_scope=SCOPE,
        sources=(*original.sources, source(HEADING + FINE)),
    )


def test_rule_round_trip_keeps_core_findings_and_packet_requirements_separate() -> None:
    research = payment_research()
    encoded = research_json(research)
    decoded = decode_research(encoded)
    assert decoded.payment_rules == research.payment_rules
    assert decoded.payment_scope == SCOPE
    assert research_json(decoded) == encoded
    assert tuple(f.key for f in decoded.findings) == tuple(ANSWERABLE_KEYS)
    assert len(decoded.resolve().fulfilled_requirements) == len(
        result_fixture().resolve().fulfilled_requirements
    )


def test_applicable_bar_is_enforced_after_reload_with_matching_factual_answer() -> None:
    research = decode_research(research_json(payment_research()))
    unknown = research.resolve()
    assert not unknown.ready
    assert any(q.question is CustomerQuestion.PAID_FINE for q in unknown.questions)
    assert not research.resolve(
        ResearchAnswers(answered=frozenset({CustomerQuestion.PAID_FINE}))
    ).ready
    barred = research.resolve(ResearchAnswers(paid_fine=True))
    assert not barred.ready
    assert any(s.official_quote == FINE for s in barred.stops)
    assert research.resolve(ResearchAnswers(paid_fine=False)).ready


@pytest.mark.parametrize("damage", ["missing_source", "scope_mismatch", "quote_changed"])
def test_changed_rule_evidence_cannot_admit_after_reassessment(damage: str) -> None:
    research = payment_research()
    if damage == "missing_source":
        research = replace(research, sources=research.sources[:-1])
    elif damage == "scope_mismatch":
        research = replace(
            research, ticket=replace(research.ticket, stage="administrative hearing")
        )
    else:
        research = replace(
            research, payment_rules=(replace(rule(), verbatim_quote="Invented quote"),)
        )
    assert not research.resolve(ResearchAnswers(paid_fine=False)).ready


def test_opposing_scoped_rules_remain_unresolved_instead_of_selecting_one() -> None:
    research = payment_research()
    quote = "Paying the fine does not waive your right to request an initial review."
    page = source(HEADING + quote, url="https://example.com/retained-review")
    retained = rule(quote, effect=PaymentEffect.NO_STATED_EFFECT, condition=PaymentCondition.NONE)
    research = replace(
        research,
        payment_rules=(*research.payment_rules, retained),
        sources=(*research.sources, page),
    )
    for paid in (True, False, None):
        resolution = decode_research(research_json(research)).resolve(
            ResearchAnswers(paid_fine=paid)
        )
        assert not resolution.ready
        assert resolution.stops
        assert all(stop.official_quote is None for stop in resolution.stops)


def test_nonempty_benign_metadata_cannot_erase_an_opposing_saved_bar() -> None:
    research = payment_research()
    quote = "Paying the fine does not waive your right to request an initial review."
    page = source(HEADING + quote, url="https://example.com/retained-review")
    retained = rule(quote, effect=PaymentEffect.NO_STATED_EFFECT, condition=PaymentCondition.NONE)
    research = replace(research, payment_rules=(retained,), sources=(*research.sources, page))
    assert not research.resolve(ResearchAnswers(paid_fine=True)).ready


@pytest.mark.parametrize(
    "quote",
    [
        "No hearing deposit is required to request an initial review.",
        "Payment of the fine does not bar an initial review.",
        "Paying the fine online waives the processing fee for initial review.",
    ],
)
def test_non_adverse_payment_words_do_not_create_false_refusals(quote: str) -> None:
    original = result_fixture()
    page = source(HEADING + quote)
    research = replace(original, sources=(*original.sources, page))
    assert research.resolve().ready


def test_absent_rule_metadata_keeps_historical_research_payload_unchanged() -> None:
    research = result_fixture()
    payload = json.loads(research_json(research))["payload"]
    assert "payment_rules" not in payload
    assert "payment_scope" not in payload
    assert research_json(decode_research(research_json(research))) == research_json(research)


def test_invalid_in_memory_action_stops_instead_of_crashing_resolution() -> None:
    research = payment_research()
    bad = replace(rule(), action="UNKNOWN_ACTION")  # type: ignore[arg-type]
    assert not replace(research, payment_rules=(bad,)).resolve().ready


def test_payment_metadata_never_silently_changes_historical_envelopes() -> None:
    research = payment_research()
    with pytest.raises(ValueError, match="version 4"):
        research_json(replace(research, record_version=1))
    document = json.loads(research_json(research))
    document["version"] = 3
    del document["payload"]["manifest"]
    with pytest.raises(ValueError, match="version 4"):
        decode_research(json.dumps(document))


def test_rule_inventory_is_bounded_at_the_durable_boundary() -> None:
    research = payment_research()
    with pytest.raises(ValueError, match="bounded inventory"):
        research_json(replace(research, payment_rules=(rule(),) * 33))


def test_unverified_rule_text_never_crosses_to_another_customer() -> None:
    research = payment_research()
    private = "Customer private words absent from any official source"
    bad = replace(rule(), verbatim_quote=private, scope_quote=private)
    research = replace(research, payment_rules=(bad,))
    assert (
        research.reused_for(
            research.ticket,
            research.started_at + timedelta(seconds=1),
            {page.url: page.sha256 for page in research.sources},
        )
        is None
    )


def test_verified_reused_rules_retain_gate_and_use_new_customer_answers() -> None:
    research = payment_research()
    reused = research.reused_for(
        research.ticket,
        research.started_at + timedelta(seconds=1),
        {page.url: page.sha256 for page in research.sources},
    )
    assert reused is not None
    assert reused.payment_rules == research.payment_rules
    assert not reused.resolve().ready
    assert reused.resolve(ResearchAnswers(paid_fine=False)).ready


def test_removing_optional_metadata_never_erases_the_visible_payment_condition() -> None:
    research = replace(payment_research(), payment_rules=(), payment_scope=None)
    assert not research.resolve(ResearchAnswers(paid_fine=False)).ready
    assert (
        research.reused_for(
            research.ticket,
            research.started_at + timedelta(seconds=1),
            {page.url: page.sha256 for page in research.sources},
        )
        is None
    )


def test_missing_authority_body_is_persisted_unknown_and_stays_unresolved() -> None:
    research = replace(payment_research(), payment_scope=None)
    saved = decode_research(research_json(research))
    assert saved.payment_scope is None
    assert not saved.resolve(ResearchAnswers(paid_fine=False)).ready


def test_current_stage_is_used_when_rechecking_omitted_rule_metadata() -> None:
    original = result_fixture()
    page = source("A hearing deposit is required to request an administrative hearing.")
    assert replace(original, sources=(*original.sources, page)).resolve().ready


@pytest.mark.parametrize("damage", ["unknown_action", "extra_field", "bad_hash", "missing_scope"])
def test_rule_codec_rejects_malformed_present_evidence(damage: str) -> None:
    document = json.loads(research_json(payment_research()))
    item = document["payload"]["payment_rules"][0]
    if damage == "unknown_action":
        item["action"] = "PAY_ANYTHING"
    elif damage == "extra_field":
        item["customer_reason"] = "Must never enter rule evidence"
    elif damage == "bad_hash":
        item["source_sha256s"] = ["not-a-hash"]
    else:
        del document["payload"]["payment_scope"]
    with pytest.raises(ValueError, match=r"payment|fields"):
        decode_research(json.dumps(document))
