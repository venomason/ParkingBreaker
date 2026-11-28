"""Research leads from the legacy city files in backend/data/cities_2x.

The records have mixed provenance. Only their URLs enter research, where the ordinary
issuer-scope, fetch and quote checks apply. The caller loads and parses the files.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from urllib.parse import urlsplit

from parkingbreaker.agencies import ResolvedAgency
from parkingbreaker.research.questions import CRITICAL_KEYS
from parkingbreaker.research.run import SeedUrl
from parkingbreaker.research.sources import AuthorityClass, is_allowed

# Historical statuses eligible to suggest a starting URL.
USABLE_STATUSES = frozenset({"active", "paused"})


def _hosts(agency: ResolvedAgency) -> tuple[str, ...]:
    return (*agency.official_domains, *(scope.host for scope in agency.delegated_scopes))


def _matches(host: str, domain: str) -> bool:
    host, domain = host.lower().removeprefix("www."), domain.lower().removeprefix("www.")
    return host == domain or host.endswith("." + domain)


def record_urls(records: Iterable[Mapping[str, object]]) -> tuple[str, ...]:
    """Potential starting pages. Only a caller with verified scopes may fetch them."""
    urls: dict[str, None] = {}
    for record in records:
        if record.get("status") not in USABLE_STATUSES:
            continue
        policy = record.get("mail_acceptance_policy")
        for url in (
            policy.get("official_source_url") if isinstance(policy, Mapping) else None,
            record.get("online_appeal_url"),
        ):
            if isinstance(url, str) and url.startswith("https://"):
                urls[url] = None
    return tuple(urls)


def known_seeds(
    agency: ResolvedAgency,
    records: Iterable[Mapping[str, object]],
    *,
    discovered_procedure_url: str | None = None,
) -> tuple[SeedUrl, ...]:
    """Nominate same-host leads. The fetch guard records out-of-scope path refusals."""
    hosts = _hosts(agency)
    urls = record_urls(records)
    if (
        discovered_procedure_url
        and is_allowed(discovered_procedure_url, agency.source_scopes())
        and discovered_procedure_url not in urls
    ):
        urls = (*urls, discovered_procedure_url)
    return tuple(
        SeedUrl(url, AuthorityClass.OFFICIAL_PROCEDURE, CRITICAL_KEYS)
        for url in urls
        if any(_matches(urlsplit(url).hostname or "", host) for host in hosts)
    )
