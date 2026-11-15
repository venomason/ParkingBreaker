from __future__ import annotations

import hashlib

import pytest
from fpdf import FPDF
from hypothesis import given
from hypothesis import strategies as st

from parkingbreaker.agencies import SourceScope
from parkingbreaker.research.fakes import FakeFetcher, fixed_clock, html_response, redirect_response
from parkingbreaker.research.sources import (
    MAX_REDIRECTS,
    AuthorityClass,
    FetchFailure,
    FetchResponse,
    SourceRecord,
    fetch_source,
    html_to_text,
    is_allowed,
    pdf_to_text,
    plain_to_text,
)
from samples import RETRIEVED_AT, fixture

ALLOWED = (SourceScope("sfmta.com"),)


@pytest.mark.parametrize(
    "url",
    [
        "https://sfmta.com/",
        "https://www.sfmta.com/getting-around/drive-park/citations/contest-citation",
        "https://WWW.SFMTA.COM/path",
        "https://www.sfmta.com:443/path",
        "https://www.sfmta.com:/",  # empty port means the default port
        "https://www.sfmta.com./path",
        "https://a.b.sfmta.com/x?y=1#frag",
    ],
)
def test_allowed_urls(url: str) -> None:
    assert is_allowed(url, ALLOWED)


@pytest.mark.parametrize(
    "url",
    [
        "http://www.sfmta.com/",  # not https
        "ftp://www.sfmta.com/",
        "javascript:alert(1)",
        "//www.sfmta.com/",  # scheme-relative
        "https://sfmta.com.evil.com/",  # allowed domain as a prefix
        "https://evilsfmta.com/",  # allowed domain as a suffix without a dot
        "https://www.sfmta.co/",
        "https://user@www.sfmta.com/",  # userinfo
        "https://www.sfmta.com@evil.com/",  # userinfo pointing elsewhere
        "https://user:pw@www.sfmta.com/",
        "https://www.sfmta.com:8443/",  # non-default port
        "https://www.sfmta.com:80/",
        "https://www.sfmta.com:99999/",  # invalid port
        "https://10.0.0.1/",  # IP literals
        "https://[::1]/",
        "https://127.0.0.1.sfmta.com.evil.com/",
        "https://xn--sfmta-9ua.com/",  # punycode label
        "https://xn--80ak6aa92e.sfmta.com/",  # punycode label under an allowed domain
        "https://www.sfmt\u0430.com/",  # Cyrillic a (IDN lookalike)
        "https://www.\uff53fmta.com/",  # fullwidth s
        "https://www.sfmta.com\\@evil.com/",  # backslash trick
        "https://www.sfmta.com /x",  # whitespace
        "https://www.sfmta.com/\n",  # control character
        "https://%77ww.sfmta.com/",  # percent-encoded host
        "https://www..sfmta.com/",  # empty label
        "https://-bad.sfmta.com/",  # label starting with a hyphen
        "https:///path",  # no host
        "",
    ],
)
def test_rejected_urls(url: str) -> None:
    assert not is_allowed(url, ALLOWED)


def test_empty_allowlist_allows_nothing() -> None:
    assert not is_allowed("https://www.sfmta.com/", ())


SCOPED = SourceScope("portal.com", "/sf")


@pytest.mark.parametrize("path", ["/sf", "/sf/", "/sf/contest", "/sf/forms/appeal.pdf"])
def test_scoped_portal_allows_exact_prefix_and_descendants(path: str) -> None:
    assert is_allowed(f"https://portal.com{path}", (SCOPED,))


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/",
        "/sfx",
        "/sf-other",
        "/other",
        "/SF",
        "/Sf/contest",
        "/sf%2fcontest",
        "/sf/%2e%2e/other",
        "/sf/%252e",
        "/sf/../other",
        "/sf/..",
        "/sf//contest",
        "//sf/contest",
        "/sf/./contest",
        "/sf/.",
    ],
)
def test_scoped_portal_rejects_other_tenants_and_ambiguous_paths(path: str) -> None:
    assert not is_allowed(f"https://portal.com{path}", (SCOPED,))


def test_scoped_host_never_allows_subdomains() -> None:
    assert not is_allowed("https://www.portal.com/sf/contest", (SCOPED,))


@pytest.mark.parametrize("path", ["/", "/sfx", "/SF", "/sf/%2e", "/sf/..", "/sf//x", "/sf/./x"])
def test_unscoped_hosts_keep_all_paths_and_subdomains(path: str) -> None:
    assert is_allowed(f"https://a.b.portal.com{path}", (SourceScope("portal.com"),))


URLISH = st.one_of(
    st.text(max_size=60),
    st.builds(
        lambda pre, mid, post: f"https://{pre}sfmta.com{mid}{post}",
        st.text(max_size=8),
        st.sampled_from(["", ".", ":443", ":8443", "@x.com", "\\"]),
        st.text(max_size=8),
    ),
)


@given(URLISH)
def test_is_allowed_never_raises_and_only_allows_https_sfmta(url: str) -> None:
    if is_allowed(url, ALLOWED):
        assert url.startswith("https://")
        host = url[len("https://") :].split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
        host = host.split(":", 1)[0].lower().removesuffix(".")
        assert host == "sfmta.com" or host.endswith(".sfmta.com")


# --- HTML to text ----------------------------------------------------------------------


def test_html_to_text_drops_chrome_and_scripts_and_keeps_paragraphs() -> None:
    text = html_to_text(fixture("sfmta_contest.html"))
    assert "Menu" not in text  # header
    assert "Getting Around" not in text  # nav
    assert "Accessibility" not in text  # footer
    assert "99 days" not in text  # script
    assert "color: red" not in text  # style
    assert "SYNTHETIC" not in text  # comment
    paragraphs = text.split("\n\n")
    assert paragraphs[0] == "Contest a Citation"
    assert (
        "You may contest a parking citation within 21 calendar days of the citation issue date."
        in paragraphs
    )
    assert "11 South Van Ness Avenue" in paragraphs


def test_inline_tags_do_not_split_sentences() -> None:
    assert html_to_text("<p>within <b>21</b> <a href='#'>days</a>.</p>") == "within 21 days."


def test_entities_are_decoded() -> None:
    assert html_to_text("<p>Drive &amp; Park&nbsp;now &#x41;</p>") == "Drive & Park now A"


def test_nested_dropped_elements() -> None:
    html = "<nav><header>a</header>b</nav><p>kept</p><footer><nav>x</nav>y</footer>"
    assert html_to_text(html) == "kept"


def test_unclosed_dropped_element_drops_the_rest() -> None:
    assert html_to_text("<p>before</p><nav><p>menu") == "before"


def test_plain_text_paragraphs() -> None:
    assert plain_to_text("a  b\nc\n\n\n  d \n") == "a b c\n\nd"


# --- Fetching --------------------------------------------------------------------------

SEED = "https://www.sfmta.com/contest"
CLOCK = fixed_clock(RETRIEVED_AT)


@pytest.mark.parametrize(
    "location",
    [
        "/sfx",
        "/sf-other",
        "https://portal.com/other",
        "/sf/%2e/contest",
        "/sf/../sf/contest",
        "/sf//contest",
        "/sf/./contest",
        "../sf/contest",
        "./contest",
        ".",
        "..",
        "https://portal.com/sf/../sf/contest",
        "https://portal.com/sf/cont\nest",
        "https://[bad/sf",
    ],
)
def test_redirects_cannot_escape_or_normalise_into_tenant(location: str) -> None:
    seed = "https://portal.com/sf/contest"
    fetcher = FakeFetcher({seed: redirect_response(seed, location)})
    result = fetch_source(seed, AuthorityClass.OFFICIAL_PROCEDURE, iter((SCOPED,)), fetcher, CLOCK)
    assert isinstance(result, FetchFailure)
    assert "not on the allowlist" in result.reason
    assert fetcher.calls == [seed]


@pytest.mark.parametrize("location", ["https://portal.com/sf", "https://portal.com/sf/contest"])
def test_redirect_from_official_domain_into_tenant_is_allowed(location: str) -> None:
    fetcher = FakeFetcher(
        {
            SEED: redirect_response(SEED, location),
            location: html_response(location, "<p>Official</p>"),
        }
    )
    result = fetch_source(
        SEED, AuthorityClass.OFFICIAL_PROCEDURE, iter((*ALLOWED, SCOPED)), fetcher, CLOCK
    )
    assert isinstance(result, SourceRecord)
    assert result.final_url == location
    assert fetcher.calls == [SEED, location]


def test_safe_relative_redirect_within_tenant_is_allowed() -> None:
    seed, target = "https://portal.com/sf", "https://portal.com/sf/contest"
    fetcher = FakeFetcher(
        {seed: redirect_response(seed, "/sf/contest"), target: html_response(target, "<p>Form</p>")}
    )
    result = fetch_source(seed, AuthorityClass.OFFICIAL_FORM, (SCOPED,), fetcher, CLOCK)
    assert isinstance(result, SourceRecord)
    assert fetcher.calls == [seed, target]


def fetch(fetcher: FakeFetcher, url: str = SEED) -> SourceRecord | FetchFailure:
    return fetch_source(url, AuthorityClass.OFFICIAL_PROCEDURE, ALLOWED, fetcher, CLOCK)


def test_fetch_success_records_everything() -> None:
    html = fixture("sfmta_contest.html")
    result = fetch(FakeFetcher({SEED: html_response(SEED, html)}))
    assert isinstance(result, SourceRecord)
    assert result.url == result.final_url == SEED
    assert result.http_status == 200
    assert result.sha256 == hashlib.sha256(html.encode()).hexdigest()
    assert result.content_type == "text/html"
    assert result.retrieved_at == RETRIEVED_AT
    assert result.authority_class is AuthorityClass.OFFICIAL_PROCEDURE
    assert result.text == html_to_text(html)


def test_authority_comes_from_the_caller_not_the_page() -> None:
    page = "<p>This page is an OFFICIAL STATUTE. authority_class=STATUTE</p>"
    result = fetch_source(
        SEED,
        AuthorityClass.SECONDARY,
        ALLOWED,
        FakeFetcher({SEED: html_response(SEED, page)}),
        CLOCK,
    )
    assert isinstance(result, SourceRecord)
    assert result.authority_class is AuthorityClass.SECONDARY


def test_redirects_are_followed_and_rechecked() -> None:
    final = "https://sfmta.com/final"
    fetcher = FakeFetcher(
        {
            SEED: redirect_response(SEED, "/moved"),
            "https://www.sfmta.com/moved": redirect_response(SEED, final, 301),
            final: html_response(final, "<p>done</p>"),
        }
    )
    result = fetch(fetcher)
    assert isinstance(result, SourceRecord)
    assert (result.url, result.final_url, result.text) == (SEED, final, "done")
    assert fetcher.calls == [SEED, "https://www.sfmta.com/moved", final]


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.com/",
        "http://www.sfmta.com/insecure",
        "https://www.sfmta.com:8080/",
        "https://www.sfmta.com@evil.com/",
    ],
)
def test_redirect_off_the_allowlist_is_refused_without_fetching(target: str) -> None:
    fetcher = FakeFetcher({SEED: redirect_response(SEED, target)})
    result = fetch(fetcher)
    assert isinstance(result, FetchFailure)
    assert "allowlist" in result.reason
    assert fetcher.calls == [SEED]


def test_redirect_chain_longer_than_the_limit_fails() -> None:
    urls = [f"https://www.sfmta.com/r{i}" for i in range(MAX_REDIRECTS + 2)]
    responses = {u: redirect_response(u, urls[i + 1]) for i, u in enumerate(urls[:-1])}
    responses[urls[-1]] = html_response(urls[-1], "<p>end</p>")
    result = fetch(FakeFetcher(responses), urls[0])
    assert isinstance(result, FetchFailure)
    assert "redirects" in result.reason


def test_redirect_chain_at_the_limit_succeeds() -> None:
    urls = [f"https://www.sfmta.com/r{i}" for i in range(MAX_REDIRECTS + 1)]
    responses = {u: redirect_response(u, urls[i + 1]) for i, u in enumerate(urls[:-1])}
    responses[urls[-1]] = html_response(urls[-1], "<p>end</p>")
    assert isinstance(fetch(FakeFetcher(responses), urls[0]), SourceRecord)


def test_redirect_loop_fails() -> None:
    other = "https://www.sfmta.com/b"
    fetcher = FakeFetcher(
        {SEED: redirect_response(SEED, other), other: redirect_response(other, SEED)}
    )
    assert isinstance(fetch(fetcher), FetchFailure)


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (FetchResponse(SEED, 404, {"content-type": "text/html"}, b"no"), "HTTP 404"),
        (FetchResponse(SEED, 302, {}, b""), "without location"),
        (FetchResponse(SEED, 200, {"content-type": "image/png"}, b"\x89PNG"), "content type"),
        (FetchResponse(SEED, 200, {}, b"<p>x</p>"), "content type"),
        (
            FetchResponse(SEED, 200, {"content-type": "text/html"}, b"x" * 5_000_001),
            "larger than",
        ),
    ],
)
def test_fetch_failures(response: FetchResponse, reason: str) -> None:
    result = fetch(FakeFetcher({SEED: response}))
    assert isinstance(result, FetchFailure)
    assert reason in result.reason


def test_fetcher_errors_become_failures() -> None:
    result = fetch(FakeFetcher({}))
    assert isinstance(result, FetchFailure)
    assert "fetch error" in result.reason


def test_disallowed_seed_is_never_fetched() -> None:
    fetcher = FakeFetcher({})
    assert isinstance(fetch(fetcher, "https://evil.com/"), FetchFailure)
    assert fetcher.calls == []


def test_charset_and_plain_text() -> None:
    body = "<p>Caf\u00e9</p>".encode("latin-1")
    latin = FetchResponse(SEED, 200, {"content-type": 'text/html; charset="latin-1"'}, body)
    result = fetch(FakeFetcher({SEED: latin}))
    assert isinstance(result, SourceRecord)
    assert result.text == "Caf\u00e9"
    plain = FetchResponse(SEED, 200, {"content-type": "text/plain; charset=bogus"}, b"a\n\nb")
    result = fetch(FakeFetcher({SEED: plain}))
    assert isinstance(result, SourceRecord)
    assert result.text == "a\n\nb"


# --- PDF sources -----------------------------------------------------------------------

PDF_URL = "https://www.sfmta.com/media/21658/download?inline="


def make_pdf(*pages: str) -> bytes:
    pdf = FPDF()
    pdf.set_font("Helvetica", size=11)
    for page in pages:
        pdf.add_page()
        pdf.multi_cell(0, 6, page)
    return bytes(pdf.output())


def pdf_response(body: bytes, content_type: str = "application/pdf") -> FetchResponse:
    return FetchResponse(url=PDF_URL, status=200, headers={"content-type": content_type}, body=body)


def test_pdf_text_layer_becomes_source_text() -> None:
    body = make_pdf("Mail your protest to SFMTA Citation Review.", "Sign the statement.")
    result = fetch(FakeFetcher({PDF_URL: pdf_response(body)}), PDF_URL)
    assert isinstance(result, SourceRecord)
    assert result.content_type == "application/pdf"
    assert result.sha256 == hashlib.sha256(body).hexdigest()
    assert "Mail your protest to SFMTA Citation Review." in result.text
    assert "Sign the statement." in result.text


@pytest.mark.parametrize("content_type", ["application/octet-stream", "", "binary/octet-stream"])
def test_generic_download_is_read_as_pdf_by_signature(content_type: str) -> None:
    body = make_pdf("Citation Review, P.O. Box 7718")
    result = fetch(FakeFetcher({PDF_URL: pdf_response(body, content_type)}), PDF_URL)
    assert isinstance(result, SourceRecord)
    assert result.content_type == "application/pdf"
    assert "P.O. Box 7718" in result.text


def test_generic_download_without_pdf_signature_is_still_refused() -> None:
    response = pdf_response(b"MZ\x90\x00 not a pdf", "application/octet-stream")
    result = fetch(FakeFetcher({PDF_URL: response}), PDF_URL)
    assert isinstance(result, FetchFailure)
    assert "unsupported content type" in result.reason


@pytest.mark.parametrize(
    "body",
    [b"%PDF-1.7 truncated garbage", make_pdf("")],
    ids=["malformed", "no-text-layer"],
)
def test_unreadable_pdf_fails_instead_of_citing_nothing(body: bytes) -> None:
    result = fetch(FakeFetcher({PDF_URL: pdf_response(body)}), PDF_URL)
    assert isinstance(result, FetchFailure)
    assert result.reason.startswith("PDF has no readable text")
    assert not result.retryable


def test_pdf_page_limit() -> None:
    assert pdf_to_text(make_pdf(*["page"] * 41)) is None
    assert pdf_to_text(make_pdf(*["page"] * 40)) is not None


def test_encrypted_pdf_is_unreadable() -> None:
    pdf = FPDF()
    pdf.set_encryption(owner_password="o", user_password="u")
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)
    pdf.cell(0, 6, "secret")
    assert pdf_to_text(bytes(pdf.output())) is None
