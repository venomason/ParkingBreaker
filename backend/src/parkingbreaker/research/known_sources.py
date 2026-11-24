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
from parkingbreaker.research.sources import AuthorityClass

# Historical statuses eligible to suggest a starting URL.
USABLE_STATUSES = frozenset({"active", "paused"})


def _hosts(agency: ResolvedAgency) -> tuple[str, ...]:
    return (*agency.official_domains, *(scope.host for scope in agency.delegated_scopes))


def _matches(host: str, domain: str) -> bool:
    host, domain = host.lower().removeprefix("www."), domain.lower().removeprefix("www.")
    return host == domain or host.endswith("." + domain)


def known_seeds(
    agency: ResolvedAgency, records: Iterable[Mapping[str, object]]
) -> tuple[SeedUrl, ...]:
    """Official contest pages from 2.x city records on this agency's own hosts."""
    seeds: dict[str, SeedUrl] = {}
    for record in records:
        if record.get("status") not in USABLE_STATUSES:
            continue
        policy = record.get("mail_acceptance_policy")
        urls = [
            policy.get("official_source_url") if isinstance(policy, Mapping) else None,
            record.get("online_appeal_url"),
        ]
        for url in urls:
            if not isinstance(url, str) or not url.startswith("https://"):
                continue
            host = urlsplit(url).hostname or ""
            if any(_matches(host, domain) for domain in _hosts(agency)):
                seeds.setdefault(
                    url, SeedUrl(url, AuthorityClass.OFFICIAL_PROCEDURE, CRITICAL_KEYS)
                )
    return tuple(seeds.values())
