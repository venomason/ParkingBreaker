"""Pure deterministic delegation from saved official pages (architecture v7 §4).

HTML and page text are untrusted data, never instructions. No I/O or LLM is used.
Callers authenticate the root's owner and pass the saved page and an extracted link;
this module rechecks the final URL and a nonblank, casefolded, whitespace-normalised
issuer name in the saved visible text. Page-wide text supplies identity only, never
link purpose. The evidence retains the caller's saved URL, time and hash unchanged.

Extraction uses HTMLParser, ignores script/style/template/noscript/head and hidden
subtrees (hidden, aria-hidden=true, display:none, visibility:hidden), resolves hrefs
with urljoin, removes fragments and keeps only HTTP(S). Inline visible text is joined
without invented spaces; blocks separate words. Context is the nearest enclosing p,
li, td, dd, section or div, including text after the link, collapsed and capped at 600
characters; absent a block it is empty. First occurrences of (href, anchor) survive
in document order. Raw hrefs are retained solely to reject unsafe URL normalisation.

_PURPOSE_TERMS is the single purpose term table. Matching is casefolded whole words
and phrases after whitespace collapse, in anchor and context separately:
* Contest in the anchor, with no payment in the anchor, is CONTEST (context may name pay).
* Contest only in context with a neutral anchor is UNRESOLVED, never CONTEST: a large
  block may describe another link.
* Payment without any contest is PAYMENT.
* Payment in the anchor plus contest in anchor or context is UNRESOLVED, never CONTEST.
* Official-site terms with neither contest nor payment are OFFICIAL_SITE; else OTHER.
* With ticket stage text supplied, any context stage in _STAGE_TERMS absent from that
  text overrides the above with UNRESOLVED, including unknown/blank ticket stages.
  These literal keywords are conservative signals, not a procedural-stage ontology.

All decisions first require an allowlisted final URL, an unscoped authenticated root,
issuer text and a safe HTTPS link under the existing URL validator. UNRESOLVED is
reserved for the part-2 fallback and yields no scope; PAYMENT/OTHER yield S2. A contest
link on the root's host tree is ALREADY_OFFICIAL with no new scope. External contests
require a query-free, non-root path: reject percent escapes, doubled slashes, dot
segments and dots except one final filename extension (ASCII alphanumeric extension,
nonempty dot-free basename). For a file use its non-root parent; otherwise use the
exact linked path minus one trailing slash. SourceScope must accept the result.

OFFICIAL_SITE produces an unscoped exact-host AUTHENTICATED_ROOT only for edu, mil,
us, org, com or net, with no query, from an unscoped .gov root. The normalised issuer
name must also appear in that link's own anchor or context; page-wide identity alone
does not authenticate the linked site's owner. This is valid only because a DotGov-owned
page named and linked it (v7 §4 rule 1); callers must establish that ownership, not infer
it from the suffix. gov is excluded: DotGov covers it.
Malformed/unsafe inputs and failed prerequisites yield NOT_YET_VERIFIED:S2, never a
scope. Even an empty query delimiter is refused. No decision finalises ticket authority.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

from parkingbreaker.agencies import SourceScope
from parkingbreaker.case_state import HardStop
from parkingbreaker.research.sources import SourceRecord, is_allowed
from parkingbreaker.research.veto_codes import VetoCode


class LinkPurpose(StrEnum):
    CONTEST = "CONTEST"
    PAYMENT = "PAYMENT"
    OFFICIAL_SITE = "OFFICIAL_SITE"
    OTHER = "OTHER"
    UNRESOLVED = "UNRESOLVED"


class DelegationOutcome(StrEnum):
    DELEGATED_CONTEST = "DELEGATED_CONTEST"
    AUTHENTICATED_ROOT = "AUTHENTICATED_ROOT"
    ALREADY_OFFICIAL = "ALREADY_OFFICIAL"
    NOT_YET_VERIFIED = "NOT_YET_VERIFIED"
    UNRESOLVED = "UNRESOLVED"


@dataclass(frozen=True)
class PageLink:
    href: str
    anchor_text: str
    context: str
    # urljoin removes dot segments and controls: preserve their original spelling.
    raw_href: str | None = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class DelegationEvidence:
    issuer_name: str
    root_host: str
    linking_page_url: str
    final_url: str
    retrieved_at: datetime
    sha256: str
    href: str
    anchor_text: str
    context: str
    purpose: LinkPurpose
    method: str
    derived_scope: SourceScope | None
    ai_quote: str | None = None
    ai_stage: str | None = None


@dataclass(frozen=True)
class DelegationResult:
    outcome: DelegationOutcome
    scope: SourceScope | None
    stop: HardStop | None
    reason: str
    evidence: DelegationEvidence
    code: VetoCode | None = None


_PURPOSE_TERMS = {
    LinkPurpose.CONTEST: (
        "contest",
        "dispute",
        "appeal",
        "protest",
        "request a review",
        "request a hearing",
        "administrative review",
        "challenge",
    ),
    LinkPurpose.PAYMENT: (
        "pay",
        "payment",
        "pay online",
        "pay a ticket",
        "pay citation",
        "fine lookup",
        "look up",
        "balance",
    ),
    LinkPurpose.OFFICIAL_SITE: ("official website", "official site", "visit the", "home page"),
}
_STAGE_TERMS = ("hearing", "review", "first level", "second level", "late")
_CONTEXT_BLOCKS = frozenset({"p", "li", "td", "dd", "section", "div"})
_DROPPED = frozenset({"script", "style", "template", "noscript", "head"})
_VOID = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
_TEXT_BLOCKS = _CONTEXT_BLOCKS | frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "hr",
        "main",
        "ol",
        "pre",
        "table",
        "th",
        "tr",
        "ul",
    }
)
_ROOT_TLDS = frozenset({"edu", "mil", "us", "org", "com", "net"})


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _matches(text: str, terms: tuple[str, ...]) -> set[str]:
    normalised = _collapse(text.casefold())
    return {
        term for term in terms if re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", normalised)
    }


def has_purpose_term(text: str, purpose: LinkPurpose) -> bool:
    """Whether text contains a whole-word purpose term from the shared literal table."""
    return bool(_matches(text, _PURPOSE_TERMS.get(purpose, ())))


def stage_matches(stage: str, ticket_stage_text: str) -> bool:
    """All recognised stage terms must also occur in the supplied ticket stage text."""
    return not (_matches(stage, _STAGE_TERMS) - _matches(ticket_stage_text, _STAGE_TERMS))


def classify_link(link: PageLink, ticket_stage_text: str | None = None) -> LinkPurpose:
    """Apply the literal term and stage rules above; never inspect page-wide claims."""
    if ticket_stage_text is not None and not stage_matches(link.context, ticket_stage_text):
        return LinkPurpose.UNRESOLVED
    anchor_contest = bool(_matches(link.anchor_text, _PURPOSE_TERMS[LinkPurpose.CONTEST]))
    context_contest = bool(_matches(link.context, _PURPOSE_TERMS[LinkPurpose.CONTEST]))
    anchor_payment = bool(_matches(link.anchor_text, _PURPOSE_TERMS[LinkPurpose.PAYMENT]))
    payment = anchor_payment or bool(_matches(link.context, _PURPOSE_TERMS[LinkPurpose.PAYMENT]))
    if anchor_contest and not anchor_payment:
        return LinkPurpose.CONTEST
    if anchor_contest or context_contest:
        return LinkPurpose.UNRESOLVED
    if payment:
        return LinkPurpose.PAYMENT
    if _matches(link.anchor_text + " " + link.context, _PURPOSE_TERMS[LinkPurpose.OFFICIAL_SITE]):
        return LinkPurpose.OFFICIAL_SITE
    return LinkPurpose.OTHER


@dataclass
class _LinkDraft:
    href: str
    raw_href: str
    anchor: list[str]
    context: list[str]


@dataclass
class _Frame:
    tag: str
    hidden: bool
    context: list[str] | None = None
    link: _LinkDraft | None = None


def _hidden(attrs: list[tuple[str, str | None]]) -> bool:
    return any(
        name == "hidden"
        or (name == "aria-hidden" and (value or "").strip().casefold() == "true")
        or (
            name == "style"
            and any(
                rule in "".join((value or "").casefold().split())
                for rule in ("display:none", "visibility:hidden")
            )
        )
        for name, value in attrs
    )


class _LinkExtractor(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.frames: list[_Frame] = []
        self.links: list[_LinkDraft] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        hidden = (bool(self.frames) and self.frames[-1].hidden) or tag in _DROPPED or _hidden(attrs)
        if not hidden and tag in _TEXT_BLOCKS:
            self.handle_data(" ")
        frame = _Frame(tag, hidden, [] if tag in _CONTEXT_BLOCKS else None)
        if tag == "a" and not hidden:
            hrefs = [value for name, value in attrs if name == "href"]
            # Duplicate href attributes have ambiguous browser interpretations.
            if len(hrefs) == 1 and hrefs[0] is not None:
                raw = hrefs[0]
                try:
                    parts = urlsplit(urljoin(self.base_url, raw))
                    href = urlunsplit(parts._replace(fragment=""))
                except ValueError:
                    pass
                else:
                    if parts.scheme in {"http", "https"} and parts.netloc:
                        context = next(
                            (f.context for f in reversed(self.frames) if f.context is not None), []
                        )
                        frame.link = _LinkDraft(href, raw, [], context)
                        self.links.append(frame.link)
        if tag not in _VOID:
            self.frames.append(frame)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.frames) - 1, -1, -1):
            frame = self.frames[index]
            if frame.tag == tag:
                del self.frames[index:]
                if not frame.hidden and tag in _TEXT_BLOCKS:
                    self.handle_data(" ")
                break
            # A mismatched outer closing tag must not expose a hidden subtree.
            if frame.hidden and (index == 0 or not self.frames[index - 1].hidden):
                break

    def handle_data(self, data: str) -> None:
        if self.frames and self.frames[-1].hidden:
            return
        for frame in self.frames:
            if frame.context is not None:
                frame.context.append(data)
            if frame.link is not None:
                frame.link.anchor.append(data)


def extract_links(html: str, base_url: str) -> tuple[PageLink, ...]:
    """Visible HTTP(S) links, nearest-block context and stable first-occurrence order."""
    parser = _LinkExtractor(base_url)
    parser.feed(html)
    parser.close()
    links: list[PageLink] = []
    seen: set[tuple[str, str]] = set()
    for draft in parser.links:
        anchor = _collapse("".join(draft.anchor))
        key = (draft.href, anchor)
        if key not in seen:
            seen.add(key)
            links.append(
                PageLink(
                    draft.href, anchor, _collapse("".join(draft.context))[:600], draft.raw_href
                )
            )
    return tuple(links)


def _unsafe_reference(href: str) -> bool:
    if not href.isascii() or any(c.isspace() or c == "\\" or not c.isprintable() for c in href):
        return True
    try:
        path = urlsplit(href).path
    except ValueError:
        return True
    return any(part in path for part in ("%", "//")) or any(
        segment in {".", ".."} for segment in path.split("/")
    )


def decide_delegation(
    page: SourceRecord,
    root: SourceScope,
    issuer_name: str,
    link: PageLink,
    purpose: LinkPurpose,
) -> DelegationResult:
    """Decide one delegation hop; ownership and link/page provenance are caller inputs."""

    def result(
        outcome: DelegationOutcome,
        reason: str,
        scope: SourceScope | None = None,
        *,
        code: VetoCode | None = None,
    ) -> DelegationResult:
        evidence = DelegationEvidence(
            issuer_name,
            root.host,
            page.url,
            page.final_url,
            page.retrieved_at,
            page.sha256,
            link.href,
            link.anchor_text,
            link.context,
            purpose,
            "deterministic",
            scope,
        )
        stop = HardStop.S2 if outcome is DelegationOutcome.NOT_YET_VERIFIED else None
        return DelegationResult(outcome, scope, stop, reason, evidence, code)

    refused = DelegationOutcome.NOT_YET_VERIFIED
    if not is_allowed(page.final_url, (root,)):
        return result(
            refused,
            "The saved page's final URL is outside the authenticated root.",
            code=VetoCode.CITATION_OFF_DOMAIN,
        )
    if root.path_prefix is not None:
        return result(
            refused,
            "A delegated tenant cannot authenticate another delegation hop.",
            code=VetoCode.DELEGATED_ROOT,
        )
    issuer = _collapse(issuer_name.casefold())
    if not issuer or issuer not in _collapse(page.text.casefold()):
        return result(
            refused,
            "The saved visible page text does not name the issuer.",
            code=VetoCode.ISSUER_NOT_NAMED,
        )
    try:
        parts = urlsplit(link.href)
        host = (parts.hostname or "").removesuffix(".")
        candidate = SourceScope(host)
    except ValueError:
        return result(
            refused, "The linked host is not a valid source scope.", code=VetoCode.UNSAFE_URL
        )
    if not is_allowed(link.href, (candidate,)):
        return result(refused, "The link must be a safe HTTPS URL.", code=VetoCode.UNSAFE_URL)
    if _unsafe_reference(link.href) or (
        link.raw_href is not None and _unsafe_reference(link.raw_href)
    ):
        return result(
            refused,
            "The linked URL contains unsafe path syntax or characters.",
            code=VetoCode.UNSAFE_URL,
        )
    if purpose is LinkPurpose.UNRESOLVED:
        return result(
            DelegationOutcome.UNRESOLVED,
            "Link purpose or ticket stage needs resolution.",
            code=VetoCode.PURPOSE_UNRESOLVED,
        )
    if purpose not in {LinkPurpose.CONTEST, LinkPurpose.OFFICIAL_SITE}:
        return result(
            refused,
            "A payment or other link does not establish contest delegation.",
            code=VetoCode.PURPOSE_NOT_CONTEST,
        )
    if any("?" in href.split("#", 1)[0] for href in (link.href, link.raw_href or "")):
        return result(
            refused,
            "Query-selected tenants cannot establish a source scope.",
            code=VetoCode.QUERY_TENANT,
        )
    if purpose is LinkPurpose.OFFICIAL_SITE:
        if not root.host.endswith(".gov"):
            return result(
                refused,
                "Root authentication requires a DotGov-owned linking page.",
                code=VetoCode.ROOT_NOT_DOTGOV,
            )
        if not any(
            issuer in _collapse(text.casefold()) for text in (link.anchor_text, link.context)
        ):
            return result(
                refused,
                "The official-site link's own anchor or context does not name the issuer.",
                code=VetoCode.ISSUER_NOT_NAMED,
            )
        if host.rsplit(".", 1)[-1] not in _ROOT_TLDS:
            return result(
                refused,
                "The site's last host label is not supported for root authentication.",
                code=VetoCode.UNSUPPORTED_ROOT,
            )
        return result(
            DelegationOutcome.AUTHENTICATED_ROOT,
            "The DotGov-owned issuer page names and links this official site.",
            candidate,
        )
    if host == root.host or host.endswith("." + root.host):
        return result(
            DelegationOutcome.ALREADY_OFFICIAL, "The contest link is already on the root host tree."
        )
    path = parts.path.removesuffix("/")
    if not path:
        return result(
            refused, "A root-path link does not identify a contest tenant.", code=VetoCode.ROOT_PATH
        )
    segments = path[1:].split("/")
    if any("." in segment for segment in segments):
        if (
            any("." in segment for segment in segments[:-1])
            or not re.fullmatch(r"[^.]+\.[A-Za-z0-9]+", segments[-1])
            or parts.path.endswith("/")
        ):
            return result(
                refused,
                "Only a final filename extension may contain a dot.",
                code=VetoCode.UNSAFE_URL,
            )
        path = path.rsplit("/", 1)[0]
        if not path:
            return result(
                refused,
                "A file at the host root does not identify a contest tenant.",
                code=VetoCode.ROOT_PATH,
            )
    try:
        scope = SourceScope(host, path)
    except ValueError:
        return result(
            refused, "The linked path is not a valid tenant source scope.", code=VetoCode.UNSAFE_URL
        )
    return result(
        DelegationOutcome.DELEGATED_CONTEST,
        "The issuer page links a contest route at this exact host and tenant path.",
        scope,
    )
