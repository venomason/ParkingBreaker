"""Exercise each veto return with existing synthetic research fixtures."""

from __future__ import annotations

from dataclasses import replace

import pytest

from parkingbreaker.agencies import IssuerType, SourceScope
from parkingbreaker.research.delegation import (
    DelegationOutcome,
    LinkPurpose,
    PageLink,
    decide_delegation,
)
from parkingbreaker.research.issuer_assessment import (
    IssuerAssessment,
    IssuerQuote,
    resolve_issuer,
)
from parkingbreaker.research.jurisdiction import IssuerCandidate
from parkingbreaker.research.link_assessment import LinkAssessment, assess_delegations
from parkingbreaker.research.sources import SourceRecord
from parkingbreaker.research.veto_codes import VetoCode
from test_research_delegation import ISSUER, ROOT, saved_page
from test_research_issuer_assessment import AI, CANDIDATE, PAGE, QUERY, case
from test_research_link_assessment import AI as LINK_AI
from test_research_link_assessment import ISSUER as LINK_ISSUER
from test_research_link_assessment import LINK, assess

CONTEST = PageLink("https://portal.example.com/tenant/contest", "Contest a citation", "")
OFFICIAL = PageLink("https://city.org/", "Official website", ISSUER)


@pytest.mark.parametrize(
    ("page", "root", "issuer", "link", "purpose", "reason", "code"),
    [
        (
            replace(saved_page(), final_url="https://evil.gov/"),
            ROOT,
            ISSUER,
            CONTEST,
            LinkPurpose.CONTEST,
            "The saved page's final URL is outside the authenticated root.",
            VetoCode.CITATION_OFF_DOMAIN,
        ),
        (
            saved_page(),
            SourceScope("www.sf.gov", "/departments"),
            ISSUER,
            CONTEST,
            LinkPurpose.CONTEST,
            "A delegated tenant cannot authenticate another delegation hop.",
            VetoCode.DELEGATED_ROOT,
        ),
        (
            saved_page(),
            ROOT,
            "Missing issuer",
            CONTEST,
            LinkPurpose.CONTEST,
            "The saved visible page text does not name the issuer.",
            VetoCode.ISSUER_NOT_NAMED,
        ),
        (
            saved_page(),
            ROOT,
            ISSUER,
            replace(CONTEST, href="https://[/"),
            LinkPurpose.CONTEST,
            "The linked host is not a valid source scope.",
            VetoCode.UNSAFE_URL,
        ),
        (
            saved_page(),
            ROOT,
            ISSUER,
            replace(CONTEST, href="http://portal.example.com/tenant"),
            LinkPurpose.CONTEST,
            "The link must be a safe HTTPS URL.",
            VetoCode.UNSAFE_URL,
        ),
        (
            saved_page(),
            ROOT,
            ISSUER,
            replace(CONTEST, raw_href="../contest"),
            LinkPurpose.CONTEST,
            "The linked URL contains unsafe path syntax or characters.",
            VetoCode.UNSAFE_URL,
        ),
        (
            saved_page(),
            ROOT,
            ISSUER,
            CONTEST,
            LinkPurpose.UNRESOLVED,
            "Link purpose or ticket stage needs resolution.",
            VetoCode.PURPOSE_UNRESOLVED,
        ),
        (
            saved_page(),
            ROOT,
            ISSUER,
            CONTEST,
            LinkPurpose.PAYMENT,
            "A payment or other link does not establish contest delegation.",
            VetoCode.PURPOSE_NOT_CONTEST,
        ),
        (
            saved_page(),
            ROOT,
            ISSUER,
            replace(CONTEST, href=CONTEST.href + "?tenant=sf"),
            LinkPurpose.CONTEST,
            "Query-selected tenants cannot establish a source scope.",
            VetoCode.QUERY_TENANT,
        ),
        (
            replace(saved_page(), final_url="https://city.org/"),
            SourceScope("city.org"),
            ISSUER,
            OFFICIAL,
            LinkPurpose.OFFICIAL_SITE,
            "Root authentication requires a DotGov-owned linking page.",
            VetoCode.ROOT_NOT_DOTGOV,
        ),
        (
            saved_page(),
            ROOT,
            ISSUER,
            replace(OFFICIAL, context=""),
            LinkPurpose.OFFICIAL_SITE,
            "The official-site link's own anchor or context does not name the issuer.",
            VetoCode.ISSUER_NOT_NAMED,
        ),
        (
            saved_page(),
            ROOT,
            ISSUER,
            replace(OFFICIAL, href="https://city.io/"),
            LinkPurpose.OFFICIAL_SITE,
            "The site's last host label is not supported for root authentication.",
            VetoCode.UNSUPPORTED_ROOT,
        ),
        (
            saved_page(),
            ROOT,
            ISSUER,
            replace(CONTEST, href="https://portal.example.com/"),
            LinkPurpose.CONTEST,
            "A root-path link does not identify a contest tenant.",
            VetoCode.ROOT_PATH,
        ),
        (
            saved_page(),
            ROOT,
            ISSUER,
            replace(CONTEST, href="https://portal.example.com/a.b/path"),
            LinkPurpose.CONTEST,
            "Only a final filename extension may contain a dot.",
            VetoCode.UNSAFE_URL,
        ),
        (
            saved_page(),
            ROOT,
            ISSUER,
            replace(CONTEST, href="https://portal.example.com/form.pdf"),
            LinkPurpose.CONTEST,
            "A file at the host root does not identify a contest tenant.",
            VetoCode.ROOT_PATH,
        ),
        (
            saved_page(),
            ROOT,
            ISSUER,
            replace(CONTEST, href="https://portal.example.com/tenant/"),
            LinkPurpose.CONTEST,
            "The linked path is not a valid tenant source scope.",
            VetoCode.UNSAFE_URL,
        ),
    ],
)
def test_every_delegation_veto_has_stable_code(
    page: SourceRecord,
    root: SourceScope,
    issuer: str,
    link: PageLink,
    purpose: LinkPurpose,
    reason: str,
    code: VetoCode,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The final SourceScope constructor is defensive: prior URL checks normally
    # exclude every invalid path. Force its rejection to exercise that return too.
    if reason == "The linked path is not a valid tenant source scope.":
        original = SourceScope

        def reject_tenant(host: str, path_prefix: str | None = None) -> SourceScope:
            if path_prefix is not None:
                raise ValueError("synthetic scope refusal")
            return original(host)

        monkeypatch.setattr("parkingbreaker.research.delegation.SourceScope", reject_tenant)
    result = decide_delegation(page, root, issuer, link, purpose)
    assert result.outcome in {DelegationOutcome.NOT_YET_VERIFIED, DelegationOutcome.UNRESOLVED}
    assert result.reason == reason
    assert isinstance(result.code, VetoCode)
    assert result.code is code


@pytest.mark.parametrize(
    ("ai", "link", "stage", "reason", "code"),
    [
        (
            replace(LINK_AI, purpose=LinkPurpose.UNRESOLVED),
            LINK,
            None,
            "AI gave an unsupported purpose.",
            VetoCode.MALFORMED_ASSESSMENT,
        ),
        (
            replace(LINK_AI, verbatim_quote=None),
            LINK,
            None,
            "AI gave no supporting quote.",
            VetoCode.QUOTE_MISSING,
        ),
        (
            replace(LINK_AI, verbatim_quote="Invented supporting quote"),
            LINK,
            None,
            "AI quote is too short or absent verbatim from the link's own anchor or context.",
            VetoCode.QUOTE_NOT_ON_LINK,
        ),
        (
            replace(LINK_AI, belongs_to_issuer=False),
            LINK,
            None,
            "AI did not establish that the link belongs to the issuer.",
            VetoCode.NOT_ISSUER,
        ),
        (
            replace(LINK_AI, verbatim_quote="Pay this parking citation"),
            replace(LINK, anchor_text="Pay this parking citation"),
            None,
            "A payment link cannot establish a contest route.",
            VetoCode.PAYMENT_AS_CONTEST,
        ),
        (
            replace(LINK_AI, verbatim_quote="Official website"),
            replace(LINK, anchor_text="Official website", context=""),
            None,
            "AI contest purpose lacks a contest term in its supporting quote.",
            VetoCode.AI_GUARD_DISAGREE,
        ),
        (
            replace(LINK_AI, purpose=LinkPurpose.OTHER),
            replace(LINK, anchor_text="Contest a citation"),
            None,
            "AI purpose disagrees with the literal contest guard.",
            VetoCode.AI_GUARD_DISAGREE,
        ),
        (
            replace(LINK_AI, stage="Hearing"),
            LINK,
            "Initial review",
            "AI procedural stage disagrees with the ticket stage text.",
            VetoCode.STAGE_MISMATCH,
        ),
    ],
)
def test_every_link_veto_has_stable_code(
    ai: LinkAssessment,
    link: PageLink,
    stage: str | None,
    reason: str,
    code: VetoCode,
) -> None:
    result = assess(ai, link, stage=stage)
    assert result.outcome is DelegationOutcome.NOT_YET_VERIFIED
    assert result.reason == reason
    assert isinstance(result.code, VetoCode)
    assert result.code is code


def test_missing_link_assessment_and_propagated_delegation_code() -> None:
    from test_research_link_assessment import PAGE as LINK_PAGE

    result = assess_delegations(LINK_PAGE, ROOT, LINK_ISSUER, (LINK,), None, ())[0]
    assert result.reason == "AI gave no assessment"
    assert result.code is VetoCode.NO_ASSESSMENT
    assert assess(link=replace(LINK, href=LINK.href + "?tenant=sf")).code is VetoCode.QUERY_TENANT
    assert assess().code is None


@pytest.mark.parametrize(
    ("ai", "candidate", "pages", "reason", "code"),
    [
        (None, CANDIDATE, (PAGE,), "AI gave no valid issuer assessment.", VetoCode.NO_ASSESSMENT),
        (
            replace(AI, candidate_index=None),
            CANDIDATE,
            (PAGE,),
            "AI did not identify a supplied issuer candidate.",
            VetoCode.NO_CANDIDATE,
        ),
        (
            replace(AI, is_private_operator=True),
            CANDIDATE,
            (PAGE,),
            "Private operator claim lacks saved operator and non-delegation evidence.",
            VetoCode.PRIVATE_CLAIM,
        ),
        (
            AI,
            replace(CANDIDATE, domains=()),
            (PAGE,),
            "Candidate has no official domains.",
            VetoCode.NO_OFFICIAL_DOMAINS,
        ),
        (
            AI,
            replace(CANDIDATE, domains=("not a domain",)),
            (PAGE,),
            "Candidate official domains cannot form safe source scopes.",
            VetoCode.UNSAFE_URL,
        ),
        (
            replace(AI, issuer_evidence=None),
            CANDIDATE,
            (PAGE,),
            "AI gave no supporting quote and page hash for issuer.",
            VetoCode.QUOTE_MISSING,
        ),
        (
            replace(AI, issuer_evidence=IssuerQuote("Official issuer quote", "f" * 64)),
            CANDIDATE,
            (PAGE,),
            "AI cited an unknown saved page hash for issuer.",
            VetoCode.PAGE_NOT_SAVED,
        ),
        (
            AI,
            CANDIDATE,
            (PAGE, replace(PAGE, text="Different saved text")),
            "A cited page hash identifies conflicting saved page texts.",
            VetoCode.PAGE_HASH_CONFLICT,
        ),
        (
            AI,
            CANDIDATE,
            (replace(PAGE, final_url="https://evil.gov/"),),
            "The cited issuer page is outside the issuer's official domains.",
            VetoCode.CITATION_OFF_DOMAIN,
        ),
        (
            replace(AI, issuer_evidence=IssuerQuote("Invented supporting quote", PAGE.sha256)),
            CANDIDATE,
            (PAGE,),
            "AI quote for issuer is too short or absent verbatim from its saved page.",
            VetoCode.QUOTE_NOT_ON_PAGE,
        ),
        (
            replace(AI, conflicted=True),
            CANDIDATE,
            (PAGE,),
            "AI reported unresolved conflicting official pages.",
            VetoCode.AI_CONFLICT,
        ),
        (
            replace(AI, deciding_body=None),
            CANDIDATE,
            (PAGE,),
            "Official pages did not establish every required issuer item.",
            VetoCode.ISSUER_ITEMS_MISSING,
        ),
        (
            replace(AI, issuer_type=IssuerType.COUNTY),
            CANDIDATE,
            (PAGE,),
            "AI issuer type disagrees with the candidate's DotGov type.",
            VetoCode.TYPE_INCOMPATIBLE,
        ),
        (
            replace(AI, violation_state="WA"),
            CANDIDATE,
            (PAGE,),
            "AI violation state disagrees with the customer's state.",
            VetoCode.STATE_MISMATCH,
        ),
        (
            replace(AI, issuer_name="Unrelated County"),
            CANDIDATE,
            (PAGE,),
            "AI issuer name shares no distinctive token with the candidate organization.",
            VetoCode.NAME_MISMATCH,
        ),
        (
            replace(AI, deciding_body=""),
            CANDIDATE,
            (PAGE,),
            "AI issuer fields cannot form a valid resolved agency.",
            VetoCode.MALFORMED_ASSESSMENT,
        ),
    ],
)
def test_every_issuer_veto_has_stable_code(
    ai: IssuerAssessment | None,
    candidate: IssuerCandidate,
    pages: tuple[SourceRecord, ...],
    reason: str,
    code: VetoCode,
) -> None:
    result = resolve_issuer(QUERY, (candidate,), pages, ai)
    assert result.agency is None
    assert result.reason == reason
    assert isinstance(result.code, VetoCode)
    assert result.code is code


def test_tribal_veto_and_resolved_issuer_codes() -> None:
    query, candidate, page, ai = case(
        "Cherokee Nation",
        "Cherokee District Court",
        IssuerType.TRIBAL,
        "Tribal",
        "cherokee.gov",
        "OK",
    )
    result = resolve_issuer(query, (candidate,), (page,), ai)
    assert result.reason == "tribal service basis key not established"
    assert result.code is VetoCode.TRIBAL_BASIS
    assert resolve_issuer(QUERY, (CANDIDATE,), (PAGE,), AI).code is None
