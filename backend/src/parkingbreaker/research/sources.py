"""Fetched source pages: URL allowlist, HTML-to-text, and fetching through a Fetcher.

Page content is untrusted DATA. Nothing here interprets it: text is extracted mechanically
and the authority class always comes from the caller (the agency registry), never from
what a page says about itself.

Fetch and isolated PDF-reading adapters own I/O. A Fetcher performs exactly one HTTP GET
and must not follow redirects; fetch_source follows them itself so every hop is checked.
Pure URL/text decisions receive saved data; untrusted PDF parsing uses the bounded adapter.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit

from parkingbreaker.agencies import SourceScope
from parkingbreaker.pdf_text_reader import bounded_pdf_text
from parkingbreaker.research.pdf_text import MAX_PDF_PAGES as MAX_PDF_PAGES
from parkingbreaker.research.pdf_text import extract_pdf_text


class AuthorityClass(StrEnum):
    STATUTE = "STATUTE"
    REGULATION = "REGULATION"
    OFFICIAL_PROCEDURE = "OFFICIAL_PROCEDURE"
    OFFICIAL_FORM = "OFFICIAL_FORM"
    OFFICIAL_CONTACT = "OFFICIAL_CONTACT"
    SECONDARY = "SECONDARY"


@dataclass(frozen=True)
class SourceRecord:
    url: str
    final_url: str
    retrieved_at: datetime
    http_status: int
    sha256: str
    content_type: str
    text: str
    authority_class: AuthorityClass
    # Transient link-extraction input, intentionally absent from existing codecs and
    # equality: persisted source identity/evidence still uses the fields above.
    html: str | None = field(default=None, compare=False, repr=False)
    # Only allowlisted artifacts count toward procedure completeness.
    linked_urls: tuple[str, ...] = ()
    off_scope_linked_urls: tuple[str, ...] = field(default=(), compare=False, repr=False)
    etag: str | None = None
    # Publisher tenant authenticated by the caller's jurisdiction-specific scopes.
    jurisdiction: str | None = None
    # Transient bounded original form bytes; persisted only in the existing PacketPlan.
    form_pdf: bytes | None = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class EvidenceArtifact:
    url: str
    final_url: str
    kind: str
    retrieved_at: datetime
    sha256: str
    etag: str | None = None


@dataclass(frozen=True)
class EvidenceManifest:
    issuer: str
    stage: str
    artifacts: tuple[EvidenceArtifact, ...]


def evidence_manifest(issuer: str, stage: str, sources: Iterable[SourceRecord]) -> EvidenceManifest:
    return EvidenceManifest(
        issuer,
        stage,
        tuple(
            EvidenceArtifact(s.url, s.final_url, s.content_type, s.retrieved_at, s.sha256, s.etag)
            for s in sources
        ),
    )


def manifest_unchanged(old: EvidenceManifest, new_hashes: Mapping[str, str]) -> bool:
    """Every recorded request URL must have a freshly checked, identical byte hash."""
    return bool(old.artifacts) and all(
        re.fullmatch(r"[0-9a-f]{64}", artifact.sha256) is not None
        and new_hashes.get(artifact.url) == artifact.sha256
        for artifact in old.artifacts
    )


CODE_PUBLISHER_DOMAINS = (
    "library.municode.com",
    "codelibrary.amlegal.com",
    "ecode360.com",
)


def is_code_publisher(url: str) -> bool:
    try:
        return (urlsplit(url).hostname or "").removesuffix(".") in CODE_PUBLISHER_DOMAINS
    except ValueError:
        return False


# --- URL allowlist -------------------------------------------------------------------

_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def _valid_hostname(host: str) -> bool:
    """Plain ASCII LDH hostname: no IP literal, no punycode label, no numeric TLD."""
    if not host or len(host) > 253 or not host.isascii():
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return False
    labels = host.split(".")
    if len(labels) < 2 or labels[-1].isdigit():
        return False
    return all(_LABEL.fullmatch(label) and not label.startswith("xn--") for label in labels)


def _unsafe_scoped_path(path: str) -> bool:
    return any(part in path for part in ("%", "//", "/./", "/../")) or path.endswith(("/..", "/."))


def is_allowed(url: str, scopes: Iterable[SourceScope]) -> bool:
    """True for an HTTPS URL matching an unscoped host tree or exact host and path.

    Rejected outright: non-ASCII URLs (IDN lookalikes), punycode labels, whitespace,
    control characters or backslashes anywhere, userinfo, IP literals, percent-encoded
    hosts, and any explicit port other than 443.
    """
    if not url.isascii() or any(c.isspace() or c == "\\" or not c.isprintable() for c in url):
        return False
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    if parts.scheme != "https" or "@" in parts.netloc or port not in (None, 443):
        return False
    host = (parts.hostname or "").removesuffix(".")
    if not _valid_hostname(host):
        return False
    for scope in scopes:
        if scope.path_prefix is None:
            # A publisher host contains many unrelated jurisdictions. Require an
            # exact tenant scope even for direct fetches and search selection.
            if host in CODE_PUBLISHER_DOMAINS:
                continue
            if host == scope.host or host.endswith("." + scope.host):
                return True
        elif (
            host == scope.host
            and not _unsafe_scoped_path(parts.path)
            and (parts.path == scope.path_prefix or parts.path.startswith(scope.path_prefix + "/"))
        ):
            return True
    return False


# --- HTML to text --------------------------------------------------------------------

# The five the design names, plus elements that never render as page text.
_DROPPED = frozenset(
    {"script", "style", "nav", "header", "footer", "noscript", "template", "title"}
)
_BLOCK = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "dd",
        "div",
        "details",
        "summary",
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
        "li",
        "main",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "td",
        "th",
        "tr",
        "ul",
    }
)


_VOID = frozenset(
    [
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
    ]
)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._dropped_depth = 0
        self._current: list[str] = []
        self.paragraphs: list[str] = []
        self._labels: list[tuple[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        style = re.sub(r"\s+", "", values.get("style") or "").lower()
        classes = re.sub(r"[-_]", "", (values.get("class") or "").lower()).split()
        label = None
        if not self._dropped_depth and tag not in _DROPPED:
            if (
                tag == "details"
                or "hidden" in values
                or values.get("aria-hidden") == "true"
                or ("display:none" in style or "visibility:hidden" in style)
            ):
                label = "Collapsed content"
            elif values.get("role") == "tabpanel" or any(
                c
                in {
                    "tabpane",
                    "tabpanel",
                    "tabcontent",
                    "uitabspanel",
                    "accordioncontent",
                    "accordionpanel",
                    "accordionbody",
                    "collapse",
                }
                for c in classes
            ):
                label = "Tab/accordion content"
        if tag not in _VOID:
            self._labels.append((tag, label))
        if tag in _DROPPED:
            self._dropped_depth += 1
        elif label:
            self.flush()
            self.paragraphs.append(f"[{label}]")
        elif tag in _BLOCK:
            self.flush()

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._labels) - 1, -1, -1):
            if self._labels[index][0] == tag:
                closing = self._labels[index:]
                del self._labels[index:]
                for _, label in reversed(closing):
                    if label:
                        self.flush()
                        self.paragraphs.append(f"[End {label.lower()}].")
                break
        if tag in _DROPPED:
            self._dropped_depth = max(0, self._dropped_depth - 1)
        elif tag in _BLOCK:
            self.flush()

    def handle_data(self, data: str) -> None:
        if not self._dropped_depth:
            self._current.append(data)

    def flush(self) -> None:
        text = " ".join("".join(self._current).split())
        if text:
            self.paragraphs.append(text)
        self._current = []


def html_to_text(html: str) -> str:
    """Page text including labelled collapsed panels; non-content elements removed.

    Inline tags (b, a, span, ...) do not break text, so a sentence split by markup stays
    one sentence. Block elements end a paragraph; paragraphs are joined by a blank line.
    """
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    parser.flush()
    return "\n\n".join(parser.paragraphs)


_ARTIFACT_TERMS = re.compile(
    r"form|affidavit|declaration|contest|appeal|protest|instructions|rules|hearing|\.pdf", re.I
)


class _ContentLinks(HTMLParser):
    """Prefer main/role=main, then article/CMS content; exclude page furniture."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, bool, int]] = []
        self.links: list[tuple[str, str, int]] = []
        self.anchor: tuple[str, int, list[str]] | None = None
        self.main_rank = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        blocked = (bool(self.stack) and self.stack[-1][1]) or tag in _DROPPED | {"aside", "head"}
        rank = self.stack[-1][2] if self.stack else 0
        if tag == "main" or values.get("role") == "main":
            rank = 3
        elif tag == "article":
            rank = max(rank, 2)
        elif re.sub(r"[-_]", "", (values.get("id") or "").lower()) in {
            "content",
            "maincontent",
            "contentarea",
            "pagecontent",
            "sitecontent",
        }:
            rank = max(rank, 1)
        if not blocked:
            self.main_rank = max(self.main_rank, rank)
        if tag not in _VOID:
            self.stack.append((tag, blocked, rank))
        if tag == "a" and not blocked and values.get("href"):
            self.anchor = (values["href"] or "", rank, [])

    def handle_data(self, data: str) -> None:
        if self.anchor is not None and not (self.stack and self.stack[-1][1]):
            self.anchor[2].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self.anchor is not None:
            href, rank, parts = self.anchor
            self.links.append((href, "".join(parts), rank))
            self.anchor = None
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break


def harvest_artifact_links(
    html: str, final_url: str, scopes: Iterable[SourceScope] = ()
) -> tuple[str, ...]:
    """Inventory all relevant main-content links, including hidden panels and off-scope leads.

    Fetching remains bounded separately. Unread links beyond that budget cannot vanish
    from the completeness guard. Fragments and duplicate destinations add no reads.
    """
    scopes = tuple(scopes)
    parser = _ContentLinks()
    parser.feed(html)
    parser.close()
    links: dict[str, None] = {}
    for href, anchor, rank in parser.links:
        if rank != parser.main_rank or not _ARTIFACT_TERMS.search(href + " " + anchor):
            continue
        if href.startswith("#"):
            continue
        # Do not let urljoin strip unsafe characters/dot segments into an allowed tenant.
        if not href.isascii() or any(c.isspace() or c == "\\" or not c.isprintable() for c in href):
            links[href] = None
            continue
        try:
            raw_path = urlsplit(href).path
            url, _ = urldefrag(urljoin(final_url, href))
            if _unsafe_scoped_path("/" + raw_path.lstrip("/")):
                raw_target = urlunsplit(
                    urlsplit(url)._replace(
                        path=raw_path if raw_path.startswith("/") else "/" + raw_path
                    )
                )
                if not is_allowed(raw_target, scopes):
                    links[raw_target] = None
                    continue
            if urlsplit(url).scheme not in {"https", "http"}:
                continue
        except ValueError:
            links[href] = None
            continue
        if url != urldefrag(final_url)[0]:
            links[url] = None
    return tuple(links)


def plain_to_text(text: str) -> str:
    """Plain-text pages: same paragraph and whitespace rules as HTML."""
    paragraphs = (" ".join(p.split()) for p in re.split(r"\n\s*\n", text))
    return "\n\n".join(p for p in paragraphs if p)


def pdf_to_text(body: bytes) -> str | None:
    """Text layer of a PDF under the plain-text paragraph rules.

    None when the file is unreadable, encrypted, longer than MAX_PDF_PAGES or has no
    text layer (a scanned image): research must not cite what it cannot read.
    """
    raw = extract_pdf_text(body)
    if raw is None:
        return None
    text = plain_to_text(raw)
    return text or None


# --- Fetching ------------------------------------------------------------------------

MAX_REDIRECTS = 5
MAX_BODY_BYTES = 5_000_000
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
# Agencies often serve forms as generic downloads; the PDF signature decides those.
_SNIFFED_TYPES = frozenset({"", "application/octet-stream", "binary/octet-stream"})


@dataclass(frozen=True)
class FetchResponse:
    url: str
    status: int
    headers: Mapping[str, str]  # keys lower-case
    body: bytes


class FetchError(Exception):
    """Raised by a Fetcher when the request could not be completed."""


class Fetcher(Protocol):
    def get(self, url: str) -> FetchResponse:
        """One GET of `url`. Must not follow redirects."""
        ...


@dataclass(frozen=True)
class FetchFailure:
    url: str
    reason: str
    retryable: bool = False


Clock = Callable[[], datetime]


def _decode(body: bytes, content_type_header: str) -> tuple[str, str]:
    """(media type, decoded text) from a Content-Type header and body."""
    media, _, params = content_type_header.partition(";")
    charset = "utf-8"
    for param in params.split(";"):
        name, _, value = param.strip().partition("=")
        if name.lower() == "charset" and value:
            charset = value.strip().strip('"')
    try:
        text = body.decode(charset, errors="replace")
    except LookupError:
        text = body.decode("utf-8", errors="replace")
    return media.strip().lower(), text


def fetch_source(
    url: str,
    authority_class: AuthorityClass,
    scopes: Iterable[SourceScope],
    fetcher: Fetcher,
    clock: Clock,
) -> SourceRecord | FetchFailure:
    """Fetch `url`, following up to MAX_REDIRECTS allowlisted redirects."""
    scopes = tuple(scopes)
    current = url
    for _hop in range(MAX_REDIRECTS + 1):
        if not is_allowed(current, scopes):
            return FetchFailure(url, f"not on the allowlist: {current}")
        try:
            response = fetcher.get(current)
        except FetchError as exc:
            return FetchFailure(url, f"fetch error at {current}: {exc}", retryable=True)
        if response.status in _REDIRECT_STATUSES:
            location = response.headers.get("location")
            if not location:
                return FetchFailure(url, f"redirect without location at {current}")
            # urljoin normalises dot segments and strips some controls. Check the raw
            # reference too, so those transformations cannot bypass a tenant scope.
            if not location.isascii() or any(
                c.isspace() or c == "\\" or not c.isprintable() for c in location
            ):
                return FetchFailure(url, f"not on the allowlist: {location}")
            try:
                raw_path = urlsplit(location).path
                target = urljoin(current, location)
                checked_path = raw_path if raw_path.startswith("/") else "/" + raw_path
                if _unsafe_scoped_path(checked_path):
                    parts = urlsplit(target)
                    raw_target = urlunsplit(parts._replace(path=checked_path))
                    if not is_allowed(raw_target, scopes):
                        return FetchFailure(url, f"not on the allowlist: {location}")
            except ValueError:
                return FetchFailure(url, f"not on the allowlist: {location}")
            current = target
            continue
        if not 200 <= response.status < 300:
            return FetchFailure(
                url,
                f"HTTP {response.status} at {current}",
                retryable=response.status in {408, 429} or response.status >= 500,
            )
        if len(response.body) > MAX_BODY_BYTES:
            return FetchFailure(url, f"body larger than {MAX_BODY_BYTES} bytes at {current}")
        media, decoded = _decode(response.body, response.headers.get("content-type", ""))
        if media in _HTML_TYPES:
            text = html_to_text(decoded)
        elif media == "text/plain":
            text = plain_to_text(decoded)
        elif media == "application/pdf" or (
            media in _SNIFFED_TYPES and response.body.startswith(b"%PDF-")
        ):
            raw_pdf_text = bounded_pdf_text(response.body)
            pdf_text = None if raw_pdf_text is None else (plain_to_text(raw_pdf_text) or None)
            if pdf_text is None:
                return FetchFailure(url, f"PDF has no readable text at {current}")
            media, text = "application/pdf", pdf_text
        else:
            return FetchFailure(url, f"unsupported content type {media or '(none)'}")
        harvested = harvest_artifact_links(decoded, current, scopes) if media in _HTML_TYPES else ()
        return SourceRecord(
            url=url,
            final_url=current,
            retrieved_at=clock(),
            http_status=response.status,
            sha256=hashlib.sha256(response.body).hexdigest(),
            content_type=media,
            text=text,
            authority_class=authority_class,
            html=decoded if media == "text/html" else None,
            linked_urls=tuple(link for link in harvested if is_allowed(link, scopes))
            if media in _HTML_TYPES
            else (),
            off_scope_linked_urls=tuple(link for link in harvested if not is_allowed(link, scopes)),
            etag=response.headers.get("etag"),
            form_pdf=response.body
            if media == "application/pdf"
            and authority_class is AuthorityClass.OFFICIAL_FORM
            and response.status == 200
            else None,
        )
    return FetchFailure(url, f"more than {MAX_REDIRECTS} redirects")
