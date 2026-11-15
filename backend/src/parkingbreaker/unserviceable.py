"""Places found unserviceable by mailed appeal, shared so research looks there first. Pure.

Each entry names a place, why it could not be served by mail (in person only, online only,
no written appeal), the official page that showed it with its verbatim quote and saved page
SHA-256, and the date it was found. An entry is
never a decision: it only adds that official page as a research starting page. Research
still fetches the page live through the same allowlist, scope and quote guards, so a city
that starts accepting mail is served again the next time a ticket arrives, and a listing
can never stop a ticket by itself. The caller loads the data file and passes its text.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from urllib.parse import urlsplit

from parkingbreaker.policy_record import US_STATES_AND_DC


class UnserviceableReason(StrEnum):
    IN_PERSON_ONLY = "IN_PERSON_ONLY"
    ONLINE_ONLY = "ONLINE_ONLY"
    NO_WRITTEN_APPEAL = "NO_WRITTEN_APPEAL"


@dataclass(frozen=True)
class UnserviceableEntry:
    place: str
    state: str
    reason: UnserviceableReason
    official_source_url: str
    quote: str
    saved_page_sha256: str
    summary: str
    found_on: date


_FIELDS = {
    "place",
    "state",
    "reason",
    "official_source_url",
    "quote",
    "saved_page_sha256",
    "summary",
    "found_on",
}
_SHA256 = re.compile(r"[0-9a-f]{64}")
MIN_QUOTE = 12


def parse_entries(text: str) -> tuple[UnserviceableEntry, ...]:
    """Validate the shared list; any malformed entry rejects the whole file."""
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("the list must be a JSON array")
    entries = []
    for item in data:
        if not isinstance(item, dict) or set(item) != _FIELDS:
            raise ValueError("entry has unknown or missing fields")
        url = item["official_source_url"]
        if (
            not isinstance(url, str)
            or urlsplit(url).scheme != "https"
            or not urlsplit(url).hostname
        ):
            raise ValueError("official source must be an https URL")
        if item["state"] not in US_STATES_AND_DC:
            raise ValueError("state must be a US state code")
        place, summary = item["place"], item["summary"]
        if not isinstance(place, str) or not place.strip():
            raise ValueError("place is required")
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("summary is required")
        quote, sha = item["quote"], item["saved_page_sha256"]
        if not isinstance(quote, str) or len(quote.strip()) < MIN_QUOTE:
            raise ValueError("a verbatim quote of at least 12 characters is required")
        if not isinstance(sha, str) or not _SHA256.fullmatch(sha):
            raise ValueError("saved_page_sha256 must be a full SHA-256")
        entries.append(
            UnserviceableEntry(
                place,
                item["state"],
                UnserviceableReason(item["reason"]),
                url,
                quote,
                sha,
                summary,
                date.fromisoformat(item["found_on"]),
            )
        )
    return tuple(entries)


def seed_records(entries: tuple[UnserviceableEntry, ...]) -> tuple[dict[str, object], ...]:
    """Research starting pages in the 2.x city-record shape that known_seeds already reads."""
    return tuple(
        {
            "status": "active",
            "mail_acceptance_policy": {"official_source_url": e.official_source_url},
        }
        for e in entries
    )
