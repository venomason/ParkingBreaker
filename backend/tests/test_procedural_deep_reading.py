"""Official-style CMS and linked-form traps for the existing research path."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from fpdf import FPDF

from parkingbreaker.agencies import SourceScope
from parkingbreaker.research.fakes import (
    FakeExtractor,
    FakeFetcher,
    html_response,
    redirect_response,
)
from parkingbreaker.research.findings import (
    Finding,
    FindingStatus,
    apply_quote_guard,
    validate_tier1,
)
from parkingbreaker.research.procedure import unread_linked_artifacts
from parkingbreaker.research.questions import FactKey, Ticket
from parkingbreaker.research.run import RecoveryConfig, ResearchResult, run_research
from parkingbreaker.research.search import SearchHit, search_request, select_pages
from parkingbreaker.research.sources import (
    AuthorityClass,
    FetchResponse,
    SourceRecord,
    fetch_source,
    harvest_artifact_links,
    html_to_text,
    manifest_unchanged,
)
from parkingbreaker.research.veto_codes import VetoCode
from parkingbreaker.research_records import (
    decode_manifest,
    decode_research,
    manifest_json,
    research_json,
)
from samples import RETRIEVED_AT
from test_procedure_silence import PROCEDURE, complete_page_claim
from test_research_records import result_fixture
from test_research_run import CONTEST_SEED, CONTEST_URL, TEST_AGENCY, TICKET, TickingClock

FORM_URL = "https://www.sfmta.com/DocumentCenter/View/1357/Contest-Form.pdf"
# Synthetic CivicPlus/Granicus structures, with ordinary agency instructions only.
LINKED_PAGE = (
    '<header><a href="/forms/payments.pdf">Payment forms</a></header>'
    '<nav><a href="/rules">Rules</a></nav>'
    '<main id="mainContent" role="main">'
    f"<h1>San Francisco Parking Citation Initial Review</h1><p>{PROCEDURE}</p>"
    f'<p><a href="{FORM_URL}">Contest Form (PDF)</a></p>'
    "<p>Mail your initial review to SFMTA Customer Service Center, "
    "ATTN: Citation Review, 11 South Van Ness Avenue, San Francisco, CA 94103.</p>"
    '</main><footer><a href="/appeal">Appeal</a></footer>'
)


def form_pdf() -> bytes:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    pdf.multi_cell(
        0,
        8,
        "San Francisco Municipal Transportation Agency\nParking Citation Initial Review\n"
        "Citation number\nWritten explanation\nCopies of supporting evidence",
    )
    return bytes(pdf.output())


def silence_from_page(page: SourceRecord) -> Finding:
    claim = complete_page_claim()
    assert claim.procedure_assessment is not None
    return replace(
        claim,
        verbatim_quote=page.text,
        source_sha256s=(page.sha256,),
        procedure_assessment=replace(
            claim.procedure_assessment,
            procedure_text=page.text,
            scope_quote=page.text,
            source_sha256=page.sha256,
        ),
    )


@pytest.mark.parametrize(
    "panel",
    [
        '<div class="fr-view" style="display: none">{text}</div>',
        '<div class="accordion-content" aria-hidden="true">{text}</div>',
        "<details><summary>Initial Review Instructions</summary>{text}</details>",
        '<div id="Tab2" role="tabpanel" class="tab-pane">{text}</div>',
        '<div class="tab-panel" hidden>{text}</div>',
    ],
)
def test_civicplus_granicus_collapsed_requirements_are_labelled_and_extracted(panel: str) -> None:
    text = "<p>Complete the contest form and sign the declaration.</p>"
    extracted = html_to_text(
        "<main><h1>Parking Citation Initial Review</h1>" + panel.format(text=text) + "</main>"
    )
    assert "Complete the contest form and sign the declaration." in extracted
    assert "[Collapsed content]" in extracted or "[Tab/accordion content]" in extracted
    assert "[End " in extracted


@pytest.mark.parametrize("container", ["main", "article", 'div id="mainContent"'])
def test_harvesting_uses_main_content_including_hidden_tabs(container: str) -> None:
    closing = container.split()[0]
    html = (
        '<nav><a href="/appeal.pdf">Appeal</a></nav>'
        '<a href="/rules">Rules</a>'
        f'<{container}><div role="tabpanel" style="display:none">'
        '<a href="/forms/contest.pdf#page=2">Contest Form (PDF)</a>'
        '<a href="/forms/contest.pdf">Download</a>'
        '<a href="/instructions">Instructions</a>'
        '<a href="/staff">Staff Directory</a>'
        '<a href="#hearing">Hearing</a>'
        "</div>"
        f'</{closing}><footer><a href="/forms">Forms</a></footer>'
    )
    assert harvest_artifact_links(html, CONTEST_URL) == (
        "https://www.sfmta.com/forms/contest.pdf",
        "https://www.sfmta.com/instructions",
    )


def linked_run(
    response: FetchResponse | None,
    *,
    max_pages: int = 20,
    retry_delays: tuple[float, ...] = (),
    page: str = LINKED_PAGE,
) -> tuple[ResearchResult, FakeFetcher]:
    transport = FakeFetcher({CONTEST_URL: html_response(CONTEST_URL, page)})
    if response is not None:
        transport.responses = {**transport.responses, FORM_URL: response}
    result = run_research(
        TICKET,
        TEST_AGENCY,
        [CONTEST_SEED],
        transport,
        FakeExtractor(lambda pages: [silence_from_page(pages[0])]),
        TickingClock(),
        recovery=RecoveryConfig(retry_delays=retry_delays, max_pages=max_pages),
        wait=lambda _: None,
    )
    return result, transport


def test_unread_contest_form_pdf_vetoes_silence_and_remains_unknown_after_reload() -> None:
    result, transport = linked_run(None, max_pages=1)
    assert transport.calls == [CONTEST_URL]
    assert unread_linked_artifacts(result.sources) == (FORM_URL,)
    finding = next(f for f in result.findings if f.key is FactKey.MANDATORY_FORMS)
    assert finding.status is FindingStatus.UNKNOWN
    assert VetoCode.UNREAD_LINKED_ARTIFACT in finding.notes
    assert not validate_tier1([silence_from_page(result.sources[0])], result.sources).proceed
    saved = decode_research(research_json(result)).reassess()
    assert unread_linked_artifacts(saved.sources) == (FORM_URL,)
    assert (
        next(f for f in saved.findings if f.key is FactKey.MANDATORY_FORMS).status
        is FindingStatus.UNKNOWN
    )
    assert not saved.resolve().ready


@pytest.mark.parametrize("max_pages", [1, 20])
def test_off_scope_contest_online_is_recorded_without_vetoing_silence(max_pages: int) -> None:
    portal = "https://third-party.example.com/contest"
    page = LINKED_PAGE.replace(
        f'<a href="{FORM_URL}">Contest Form (PDF)</a>',
        f'<a href="{portal}">Contest online</a>',
    )
    result, transport = linked_run(None, max_pages=max_pages, page=page)
    assert transport.calls == [CONTEST_URL]
    assert any(
        attempt.url == portal and attempt.error == "off scope" for attempt in result.attempts
    )
    assert not unread_linked_artifacts(result.sources)
    finding = next(f for f in result.findings if f.key is FactKey.MANDATORY_FORMS)
    assert finding.status is FindingStatus.CONFIRMED
    assert VetoCode.UNREAD_LINKED_ARTIFACT not in finding.notes
    saved = decode_research(research_json(result)).reassess()
    assert not unread_linked_artifacts(saved.sources)
    assert next(f for f in saved.findings if f.key is FactKey.MANDATORY_FORMS).status is (
        FindingStatus.CONFIRMED
    )
    assert any(attempt.url == portal and attempt.error == "off scope" for attempt in saved.attempts)


def test_fetched_contest_form_without_extra_requirement_allows_silence() -> None:
    response = FetchResponse(
        FORM_URL, 200, {"content-type": "application/pdf", "etag": '"form-1"'}, form_pdf()
    )
    result, transport = linked_run(response)
    assert transport.calls == [CONTEST_URL, FORM_URL]
    assert not unread_linked_artifacts(result.sources)
    finding = next(f for f in result.findings if f.key is FactKey.MANDATORY_FORMS)
    assert finding.status is FindingStatus.CONFIRMED
    assert finding.value == []
    assert result.manifest.artifacts[1].kind == "application/pdf"
    assert result.manifest.artifacts[1].etag == '"form-1"'
    saved = decode_research(research_json(result)).reassess()
    assert (
        next(f for f in saved.findings if f.key is FactKey.MANDATORY_FORMS).status
        is FindingStatus.CONFIRMED
    )


@pytest.mark.parametrize("status", [403, 500])
def test_failed_linked_artifact_is_recorded_and_vetoes_silence(status: int) -> None:
    result, transport = linked_run(FetchResponse(FORM_URL, status, {}, b""), retry_delays=(0,))
    assert transport.calls.count(FORM_URL) == (2 if status == 500 else 1)
    assert len(result.failures) == 1
    assert result.failures[0].url == FORM_URL
    assert (
        VetoCode.UNREAD_LINKED_ARTIFACT
        in next(f for f in result.findings if f.key is FactKey.MANDATORY_FORMS).notes
    )


def test_linked_artifact_redirect_keeps_allowlist_and_tenant_boundary() -> None:
    target = "https://portal.example.com/other-city/contest.pdf"
    result, transport = linked_run(redirect_response(FORM_URL, target))
    assert target not in transport.calls
    assert result.failures
    assert unread_linked_artifacts(result.sources) == (FORM_URL,)


def test_linked_form_positive_requirement_overrides_silence() -> None:
    result, _ = linked_run(
        html_response(
            FORM_URL,
            "<h1>Parking Citation Initial Review</h1><p>You must sign the contest form.</p>",
        )
    )
    finding = next(f for f in result.findings if f.key is FactKey.MANDATORY_FORMS)
    assert finding.value == ["You must sign the contest form."]


@pytest.mark.parametrize(
    ("host", "tenant", "other"),
    [
        ("library.municode.com", "/ca/san_francisco", "/ca/oakland"),
        ("codelibrary.amlegal.com", "/codes/san_francisco", "/codes/oakland"),
        ("ecode360.com", "/SF1142", "/OA1142"),
    ],
)
def test_code_publishers_are_statutes_only_in_matching_issuer_scope(
    host: str, tenant: str, other: str
) -> None:
    agency = replace(TEST_AGENCY, delegated_scopes=(SourceScope(host, tenant),))
    url, wrong = f"https://{host}{tenant}/code", f"https://{host}{other}/code"
    request = search_request(TICKET, agency.official_domains)
    selected = select_pages(request, [SearchHit(url), SearchHit(wrong)], agency.source_scopes())
    assert len(selected) == 1
    assert selected[0].authority_class is AuthorityClass.STATUTE
    transport = FakeFetcher(
        {
            url: html_response(
                url,
                "<h1>San Francisco Code</h1><p>A request for initial review "
                "must be submitted within 21 days of issuance.</p>",
            )
        }
    )
    result = run_research(
        TICKET,
        agency,
        [replace(CONTEST_SEED, url=url)],
        transport,
        FakeExtractor(
            lambda pages: [
                Finding(
                    FactKey.DEADLINE_DAYS,
                    21,
                    FindingStatus.CONFIRMED,
                    "within 21 days of issuance",
                    (pages[0].sha256,),
                    stage="first-level review",
                )
            ]
        ),
        TickingClock(),
        wait=lambda _: None,
    )
    assert result.sources[0].authority_class is AuthorityClass.STATUTE
    assert result.sources[0].jurisdiction == agency.id
    assert (
        next(f for f in result.findings if f.key is FactKey.DEADLINE_DAYS).status
        is FindingStatus.CONFIRMED
    )
    # The same saved code cannot establish another jurisdiction's deadline.
    claim = replace(
        next(f for f in result.findings if f.key is FactKey.DEADLINE_DAYS),
        jurisdiction="us-ca-oakland",
    )
    assert apply_quote_guard([claim], result.sources)[0].status is FindingStatus.UNKNOWN
    assert not isinstance(
        fetch_source(
            wrong, AuthorityClass.STATUTE, agency.source_scopes(), transport, TickingClock()
        ),
        SourceRecord,
    )
    assert wrong not in transport.calls


def test_statute_only_mailing_address_is_vetoed() -> None:
    result = result_fixture()
    address = next(f for f in result.findings if f.key is FactKey.MAILING_ADDRESS)
    statute_sources = tuple(
        replace(s, authority_class=AuthorityClass.STATUTE) for s in result.sources
    )
    guarded = apply_quote_guard([address], statute_sources)[0]
    assert guarded.status is FindingStatus.UNKNOWN
    assert VetoCode.STATUTE_ONLY_ADDRESS in guarded.notes


def test_manifest_round_trip_and_research_manifest_integrity() -> None:
    result = result_fixture()
    encoded = manifest_json(result.manifest)
    assert decode_manifest(encoded) == result.manifest
    assert manifest_json(decode_manifest(encoded)) == encoded
    assert json.loads(encoded)["version"] == 1
    assert decode_research(research_json(result)).manifest == result.manifest
    broken = json.loads(research_json(result))
    broken["payload"]["manifest"]["artifacts"][0]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="manifest disagrees"):
        decode_research(json.dumps(broken))


def test_one_changed_artifact_hash_invalidates_same_day_reuse() -> None:
    result = result_fixture()
    hashes = {a.url: a.sha256 for a in result.manifest.artifacts}
    now = result.started_at + timedelta(hours=1)
    assert manifest_unchanged(result.manifest, hashes)
    assert result.reusable_for(result.ticket, now, hashes)
    hashes[result.manifest.artifacts[-1].url] = "0" * 64
    assert not manifest_unchanged(result.manifest, hashes)
    assert not result.reusable_for(result.ticket, now, hashes)
    assert not manifest_unchanged(result.manifest, {})


def test_reuse_still_requires_same_issuer_stage_and_original_24_hour_window() -> None:
    result = result_fixture()
    hashes = {a.url: a.sha256 for a in result.manifest.artifacts}
    assert not result.reusable_for(
        replace(result.ticket, city_id="us-ca-oakland", jurisdiction="us-ca-oakland"),
        result.started_at,
        hashes,
    )
    assert not result.reusable_for(
        replace(result.ticket, stage="administrative hearing"), result.started_at, hashes
    )
    assert not result.reusable_for(result.ticket, result.started_at + timedelta(hours=24), hashes)
    assert not result.reusable_for(result.ticket, result.started_at - timedelta(seconds=1), hashes)


@pytest.mark.parametrize(
    "target",
    [
        replace(TICKET, violation_code="TC 8.1.1"),
        replace(TICKET, violation_description="Different violation"),
        replace(TICKET, issue_date=RETRIEVED_AT.date()),
    ],
)
def test_reuse_never_carries_conclusions_to_another_violation_or_issue_date(
    target: Ticket,
) -> None:
    result = result_fixture()
    hashes = {a.url: a.sha256 for a in result.manifest.artifacts}
    assert not result.reusable_for(target, result.started_at, hashes)


def test_hidden_requirement_defeats_complete_procedure_silence() -> None:
    html = (
        f'<main><p>{PROCEDURE}</p><div class="ui-tabs-panel ui-widget-content">'
        "<p>You must include the contest form.</p></div></main>"
    )
    fetched = fetch_source(
        CONTEST_URL,
        AuthorityClass.OFFICIAL_PROCEDURE,
        TEST_AGENCY.source_scopes(),
        FakeFetcher({CONTEST_URL: html_response(CONTEST_URL, html)}),
        TickingClock(),
    )
    assert isinstance(fetched, SourceRecord)
    assert "[Tab/accordion content]" in fetched.text
    guarded = apply_quote_guard([silence_from_page(fetched)], [fetched])
    assert any(f.value == ["You must include the contest form."] for f in guarded)


def test_every_linked_artifact_shares_one_page_budget_including_recursive_links() -> None:
    next_url = "https://www.sfmta.com/forms/review-instructions.pdf"
    transport = FakeFetcher(
        {
            CONTEST_URL: html_response(CONTEST_URL, LINKED_PAGE),
            FORM_URL: html_response(
                FORM_URL, f'<main><a href="{next_url}">Review Instructions (PDF)</a></main>'
            ),
            next_url: FetchResponse(next_url, 200, {"content-type": "application/pdf"}, form_pdf()),
        }
    )
    result = run_research(
        TICKET,
        TEST_AGENCY,
        [CONTEST_SEED],
        transport,
        FakeExtractor(lambda pages: [silence_from_page(pages[0])]),
        TickingClock(),
        recovery=RecoveryConfig(max_pages=2),
        wait=lambda _: None,
    )
    assert transport.calls == [CONTEST_URL, FORM_URL]
    assert unread_linked_artifacts(result.sources) == (next_url,)
    assert len(result.manifest.artifacts) == 2
    assert not result.reusable_for(
        result.ticket,
        result.finished_at,
        {s.url: s.sha256 for s in result.sources},
    )


def test_linked_reads_stop_at_existing_research_time_limit() -> None:
    class ExpiringClock:
        def __init__(self) -> None:
            self.ticks = 0

        def __call__(self) -> datetime:
            self.ticks += 1
            return RETRIEVED_AT + (timedelta(minutes=30) if self.ticks >= 4 else timedelta())

    transport = FakeFetcher({CONTEST_URL: html_response(CONTEST_URL, LINKED_PAGE)})
    extractor = FakeExtractor(lambda pages: [silence_from_page(pages[0])])
    result = run_research(
        TICKET,
        TEST_AGENCY,
        [CONTEST_SEED],
        transport,
        extractor,
        ExpiringClock(),
        recovery=RecoveryConfig(),
        wait=lambda _: None,
    )
    assert transport.calls == [CONTEST_URL]
    assert not extractor.calls
    assert unread_linked_artifacts(result.sources) == (FORM_URL,)
    assert not result.decision.ok


@pytest.mark.parametrize("media", ["text/html", "application/xhtml+xml"])
def test_linked_artifacts_keep_existing_unscoped_url_rules(media: str) -> None:
    url = "https://www.sfmta.com/forms/Contest%20Form.pdf"
    html = '<main><a href="/forms/Contest%20Form.pdf">Contest Form (PDF)</a></main>'
    fetched = fetch_source(
        CONTEST_URL,
        AuthorityClass.OFFICIAL_PROCEDURE,
        TEST_AGENCY.source_scopes(),
        FakeFetcher(
            {CONTEST_URL: FetchResponse(CONTEST_URL, 200, {"content-type": media}, html.encode())}
        ),
        TickingClock(),
    )
    assert isinstance(fetched, SourceRecord)
    assert fetched.linked_urls == (url,)
    assert unread_linked_artifacts((fetched,)) == (url,)
    assert decode_research(
        research_json(
            replace(result_fixture(), sources=(fetched,), findings=(), candidates=(), pending=())
        )
    ).sources[0].linked_urls == (url,)


def test_publisher_root_scope_never_authenticates_other_jurisdictions() -> None:
    url = "https://library.municode.com/ca/oakland/codes/code_of_ordinances"
    agency = replace(TEST_AGENCY, official_domains=("library.municode.com",))
    transport = FakeFetcher({url: html_response(url, "<h1>Oakland Code of Ordinances</h1>")})
    result = run_research(
        TICKET,
        agency,
        [replace(CONTEST_SEED, url=url)],
        transport,
        FakeExtractor(lambda pages: []),
        TickingClock(),
        wait=lambda _: None,
    )
    assert not transport.calls
    assert not result.sources
    assert result.failures


@pytest.mark.parametrize(
    "authority",
    [AuthorityClass.STATUTE, AuthorityClass.SECONDARY, AuthorityClass.OFFICIAL_PROCEDURE],
)
def test_statute_address_needs_official_corroboration(authority: AuthorityClass) -> None:
    result = result_fixture()
    claim = next(f for f in result.findings if f.key is FactKey.MAILING_ADDRESS)
    original = next(s for s in result.sources if s.sha256 in claim.source_sha256s)
    statute = replace(original, authority_class=AuthorityClass.STATUTE)
    corroboration = replace(original, sha256="a" * 64, authority_class=authority)
    claim = replace(claim, source_sha256s=(statute.sha256, corroboration.sha256))
    guarded = apply_quote_guard([claim], [statute, corroboration])[0]
    assert guarded.status is (
        FindingStatus.CONFIRMED
        if authority is AuthorityClass.OFFICIAL_PROCEDURE
        else FindingStatus.UNKNOWN
    )


@pytest.mark.parametrize("damage", ["version", "hash", "timestamp", "duplicate"])
def test_manifest_codec_rejects_invalid_evidence(damage: str) -> None:
    data = json.loads(manifest_json(result_fixture().manifest))
    artifact = data["payload"]["artifacts"][0]
    if damage == "version":
        data["version"] = 2
    elif damage == "hash":
        artifact["sha256"] = "unhashed"
    elif damage == "timestamp":
        artifact["retrieved_at"] = "2026-10-15T12:00:00"
    else:
        data["payload"]["artifacts"].append(artifact)
    with pytest.raises(ValueError, match=r"unsupported|invalid|timezone-aware"):
        decode_manifest(json.dumps(data))


def test_extraction_labels_cannot_complete_a_truncated_procedure() -> None:
    html = (
        f'<main><p>{PROCEDURE}</p><div role="tabpanel" aria-hidden="true">'
        "<p>Requests for initial review must include</p></div></main>"
    )
    page = fetch_source(
        CONTEST_URL,
        AuthorityClass.OFFICIAL_PROCEDURE,
        TEST_AGENCY.source_scopes(),
        FakeFetcher({CONTEST_URL: html_response(CONTEST_URL, html)}),
        TickingClock(),
    )
    assert isinstance(page, SourceRecord)
    guarded = apply_quote_guard([silence_from_page(page)], [page])[0]
    assert guarded.status is FindingStatus.UNKNOWN
    assert "incomplete or truncated" in guarded.notes


@pytest.mark.parametrize(
    "host", ["library.municode.com", "codelibrary.amlegal.com", "ecode360.com"]
)
def test_publisher_root_scope_is_rejected_by_fetch_and_search(host: str) -> None:
    url = f"https://{host}/codes/oakland"
    scopes = (SourceScope(host),)
    transport = FakeFetcher({})
    assert not select_pages(search_request(TICKET, (host,)), [SearchHit(url)], scopes)
    assert not isinstance(
        fetch_source(url, AuthorityClass.STATUTE, scopes, transport, TickingClock()),
        SourceRecord,
    )
    assert not transport.calls
