"""T21 reuse must preserve ticket scope, provenance and the original time limit."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from parkingbreaker.research.fakes import FakeFetcher, fixed_clock, redirect_response
from parkingbreaker.research.findings import Finding, FindingStatus
from parkingbreaker.research.questions import FactKey
from parkingbreaker.research.run import ResearchAttempt, ResearchResult, reuse_research
from parkingbreaker.research.sources import FetchFailure, FetchResponse
from parkingbreaker.research_records import decode_research, research_json
from test_research_run import TEST_AGENCY, fetcher
from test_t21_research_reuse import attempt, first_research


def test_changed_issue_date_is_rejected_before_fetching() -> None:
    saved, _ = first_research()
    changed = replace(saved.ticket, issue_date=saved.ticket.issue_date + timedelta(days=1))
    transport = fetcher()

    assert attempt(saved, changed, 2, transport=transport) is None
    assert transport.calls == []


def test_changed_violation_description_is_rejected_before_fetching() -> None:
    saved, _ = first_research()
    changed = replace(saved.ticket, violation_description="Different ticket facts")
    transport = fetcher()

    assert attempt(saved, changed, 2, transport=transport) is None
    assert transport.calls == []


def _with_private_research_metadata(saved: ResearchResult) -> ResearchResult:
    note_secret = "PRIVATE_FINDING_NOTE_9f6a"
    nested_secret = "PRIVATE_NESTED_NOTE_b07c"
    candidate_value_secret = "PRIVATE_CANDIDATE_VALUE_81d2"
    candidate_note_secret = "PRIVATE_CANDIDATE_NOTE_6e10"
    judgment_secret = "PRIVATE_PROCEDURE_JUDGMENT_3ca9"
    failure_secret = "PRIVATE_FETCH_FAILURE_9c3f"
    attempt_secret = "PRIVATE_ATTEMPT_ERROR_730d"

    source_finding = saved.findings[0]
    nested = replace(source_finding, notes=nested_secret)
    findings = tuple(
        replace(
            finding,
            notes=note_secret,
            evidence=(nested,) if finding is source_finding else finding.evidence,
        )
        for finding in saved.findings
    )
    assessment_finding = next(
        finding for finding in findings if finding.procedure_assessment is not None
    )
    assessment = assessment_finding.procedure_assessment
    assert assessment is not None
    findings = tuple(
        replace(
            finding,
            procedure_assessment=replace(assessment, judgment=judgment_secret),
        )
        if finding is assessment_finding
        else finding
        for finding in findings
    )
    candidate = Finding(
        key=source_finding.key,
        value={"private_candidate": candidate_value_secret},
        status=FindingStatus.UNKNOWN,
        verbatim_quote=None,
        source_sha256s=(),
        notes=candidate_note_secret,
    )
    return replace(
        saved,
        findings=findings,
        candidates=(candidate,),
        failures=(FetchFailure("https://example.test/private", failure_secret),),
        attempts=(ResearchAttempt("fetch", "https://example.test/private", attempt_secret),),
    )


def _confirmed_quotes(result: ResearchResult) -> set[tuple[str, str | None]]:
    return {
        (finding.key.value, finding.verbatim_quote)
        for finding in result.findings
        if finding.status is FindingStatus.CONFIRMED
    }


def test_reuse_carries_public_quotes_and_decision_but_strips_private_metadata() -> None:
    original, _ = first_research()
    saved = _with_private_research_metadata(original)

    reused = attempt(saved, saved.ticket, 2)

    assert reused is not None
    assert reused.decision.ok
    assert _confirmed_quotes(reused) == _confirmed_quotes(original)
    serialized = str(reused) + "\n" + research_json(reused)
    for secret in (
        "PRIVATE_FINDING_NOTE_9f6a",
        "PRIVATE_NESTED_NOTE_b07c",
        "PRIVATE_CANDIDATE_VALUE_81d2",
        "PRIVATE_CANDIDATE_NOTE_6e10",
        "PRIVATE_PROCEDURE_JUDGMENT_3ca9",
        "PRIVATE_FETCH_FAILURE_9c3f",
        "PRIVATE_ATTEMPT_ERROR_730d",
    ):
        assert secret not in serialized


def test_research_expiring_during_refetch_is_rejected() -> None:
    saved, _ = first_research()
    now_before_expiry = saved.started_at + timedelta(hours=23, minutes=59, seconds=59)
    expired_during_fetch = saved.started_at + timedelta(hours=24, seconds=1)

    reused = reuse_research(
        [saved],
        saved.ticket,
        TEST_AGENCY,
        fetcher(),
        lambda: expired_during_fetch,
        now_before_expiry,
    )

    assert reused is None


def test_redirect_to_a_different_allowed_final_url_is_rejected() -> None:
    saved, _ = first_research()
    original_responses = fetcher().responses
    original = original_responses[saved.sources[0].url]
    redirected_url = "https://www.sfmta.com/alternate/contest-citation"
    transport = FakeFetcher(
        {
            **original_responses,
            saved.sources[0].url: redirect_response(saved.sources[0].url, redirected_url),
            redirected_url: FetchResponse(
                url=redirected_url,
                status=200,
                headers=original.headers,
                body=original.body,
            ),
        }
    )

    reused = attempt(saved, saved.ticket, 2, transport=transport)

    assert reused is None
    assert redirected_url in transport.calls


@pytest.mark.parametrize(
    "mutation",
    [
        "unused_procedure_text",
        "unverified_scope_quote",
        "rejected_optional_quote",
        "mailing_address_extra_field",
    ],
)
def test_roundtrip_reuse_removes_untrusted_finding_fields(mutation: str) -> None:
    original, _ = first_research()
    secret = f"PRIVATE_T21_{mutation.upper()}_93ad"
    key = (
        FactKey.MAILING_ADDRESS
        if mutation == "mailing_address_extra_field"
        else FactKey.FINE_AMOUNT
    )
    target = next(finding for finding in original.findings if finding.key is key)

    if mutation == "unused_procedure_text":
        procedure = next(
            finding.procedure_assessment
            for finding in original.findings
            if finding.procedure_assessment is not None
        )
        assert procedure is not None
        changed_target = replace(
            target,
            procedure_assessment=replace(procedure, procedure_text=secret),
        )
    elif mutation == "unverified_scope_quote":
        changed_target = replace(target, scope_quote=secret, stage=None)
    elif mutation == "rejected_optional_quote":
        changed_target = replace(target, verbatim_quote=secret)
    elif mutation == "mailing_address_extra_field":
        assert isinstance(target.value, dict)
        changed_target = replace(
            target,
            value={**target.value, "original_customer": secret},
        )
    else:
        raise AssertionError(f"unexpected mutation: {mutation}")

    changed = replace(
        original,
        findings=tuple(
            changed_target if finding.key is key else finding for finding in original.findings
        ),
    )
    decoded = decode_research(research_json(changed))
    reuse_at = decoded.started_at + timedelta(hours=2)
    reused = reuse_research(
        [decoded],
        decoded.ticket,
        TEST_AGENCY,
        fetcher(),
        fixed_clock(reuse_at),
        reuse_at,
    )

    assert reused is not None
    assert reused.decision.ok
    assert secret not in repr(reused.findings)
    assert secret not in repr(reused.candidates)
    assert secret not in str(reused) + research_json(reused)


def test_roundtrip_reuse_canonicalizes_an_alias_stage_with_private_suffix() -> None:
    original, _ = first_research()
    secret = "PRIVATE_T21_STAGE_SUFFIX_728b"
    target = next(finding for finding in original.findings if finding.key is FactKey.FINE_AMOUNT)
    assert target.verbatim_quote is not None
    changed_target = replace(
        target,
        stage=f"first-level review {secret}",
        scope_quote=target.verbatim_quote,
    )
    changed = replace(
        original,
        findings=tuple(
            changed_target if finding.key is FactKey.FINE_AMOUNT else finding
            for finding in original.findings
        ),
    )
    decoded = decode_research(research_json(changed))
    reuse_at = decoded.started_at + timedelta(hours=2)
    reused = reuse_research(
        [decoded],
        decoded.ticket,
        TEST_AGENCY,
        fetcher(),
        fixed_clock(reuse_at),
        reuse_at,
    )

    assert reused is not None
    assert reused.decision.ok
    reused_fine = next(finding for finding in reused.findings if finding.key is FactKey.FINE_AMOUNT)
    assert reused_fine.stage == "first-level review"
    assert secret not in repr(reused.findings)
    assert secret not in repr(reused.candidates)
    assert secret not in str(reused) + research_json(reused)


def test_dynamic_state_comparison_matches_captured_form_and_ignores_inert_tags() -> None:
    import re
    from pathlib import Path

    from parkingbreaker.research.sources import aspnet_state_digest

    captured = (Path(__file__).parent / "fixtures/unfamiliar_issuers/cambridge.html").read_bytes()
    rotated, replacements = re.subn(
        rb'(name="__VIEWSTATE" id="__VIEWSTATE" value=")[A-Za-z0-9+/=]+(")',
        rb"\g<1>SYNTHETICNEWVALUE==\2",
        captured,
    )
    assert replacements == 1
    assert aspnet_state_digest(captured) == aspnet_state_digest(rotated) is not None
    assert aspnet_state_digest(rotated + b" ") != aspnet_state_digest(captured)
    hidden = b'<input type="hidden" name="__VIEWSTATE" value="OLD" />'
    assert aspnet_state_digest(b"<form><!-- " + hidden + b" --></form>") is None
    assert aspnet_state_digest(b"<form><textarea>" + hidden + b"</textarea></form>") is None


def test_rotated_state_rebinds_scoped_payment_rule_without_weakening_rule_guard() -> None:
    import hashlib

    from parkingbreaker.research.fakes import html_response
    from parkingbreaker.research.resolution import ResearchAnswers
    from parkingbreaker.research.sources import AuthorityClass, SourceRecord, fetch_source
    from test_t21_payment_policy import FINE, HEADING, rule
    from test_t21_research_reuse import _TOKEN_FORM

    saved, _ = first_research()
    url = "https://www.sfmta.com/payment-policy-test"
    page = f"<p>{HEADING}{FINE}</p>{_TOKEN_FORM}"
    source = fetch_source(
        url,
        AuthorityClass.OFFICIAL_PROCEDURE,
        TEST_AGENCY.source_scopes(),
        FakeFetcher({url: html_response(url, page)}),
        lambda: saved.finished_at,
    )
    assert isinstance(source, SourceRecord)
    assert source.aspnet_state_sha256 is not None
    payment = rule()
    assert payment.scope is not None
    scope = replace(payment.scope, issuer=TEST_AGENCY.id)
    payment = replace(payment, scope=scope, source_sha256s=(source.sha256,))
    saved = replace(
        saved, sources=(*saved.sources, source), payment_rules=(payment,), payment_scope=scope
    )
    saved = decode_research(research_json(saved))
    refreshed = page.replace("STATEABC", "ROTATEDTOKEN")
    transport = FakeFetcher({**fetcher().responses, url: html_response(url, refreshed)})
    reused = attempt(saved, saved.ticket, 2, transport=transport)
    assert reused is not None
    assert reused.payment_rules[0].source_sha256s == (
        hashlib.sha256(refreshed.encode()).hexdigest(),
    )
    assert not reused.resolve(ResearchAnswers(paid_fine=True)).ready
    bad = replace(saved, payment_rules=(replace(payment, verbatim_quote="Unverified prose."),))
    assert attempt(bad, saved.ticket, 2, transport=transport) is None
