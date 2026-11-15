"""403 + archive fallback for blocked official sites (architecture-v8 16.2 item 2).

A blocked official site (HTTP 403) is never PROCESS_INCOMPATIBLE and never creates
a serviceability block: it is an access condition, not a process verdict. A
customer-confirmed ticket back plus an archived snapshot of the official page may
carry the mailing address, stage and deadline only when the snapshot is at most
MAX_SNAPSHOT_AGE_DAYS old and its three facts match the ticket back exactly. Any
mismatch, missing piece or older snapshot stops as NOT_YET_VERIFIED (failed
research is never relabelled unsupported). Facts the customer confirms from the
ticket back are labelled customer-provided, never official. When the fallback
verifies, the customer is told verbatim that the live site blocks automated access.

Pure: no I/O. Callers pass the block, the snapshot, the ticket back and today in.
Fetching the archived page is external and goes through the usual fetch interface.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from parkingbreaker.research.sources import FetchFailure

MAX_SNAPSHOT_AGE_DAYS = 90

BLOCK_DISCLOSURE = (
    "Verified by matching your ticket against the most recent archived copy of {site}; "
    "the live site blocks automated access."
)


class FactProvenance(StrEnum):
    CUSTOMER_PROVIDED = "CUSTOMER_PROVIDED"
    OFFICIAL = "OFFICIAL"


class TicketBackField(StrEnum):
    MAILING_ADDRESS = "MAILING_ADDRESS"
    STAGE = "STAGE"
    DEADLINE = "DEADLINE"


class ArchiveOutcome(StrEnum):
    VERIFIED = "VERIFIED"
    NOT_YET_VERIFIED = "NOT_YET_VERIFIED"


class ArchiveStopCause(StrEnum):
    STALE_SNAPSHOT = "STALE_SNAPSHOT"
    MISSING_PIECE = "MISSING_PIECE"
    FACT_MISMATCH = "FACT_MISMATCH"


@dataclass(frozen=True)
class LiveSiteBlock:
    """An official page that refused automated access (HTTP 403).

    A block is never a process verdict and never a serviceability block on its own.
    """

    site: str
    http_status: int


@dataclass(frozen=True)
class TicketBackFacts:
    """The notice on the ticket back, exactly as the customer confirms it."""

    mailing_address: str
    stage: str
    deadline: str

    def fields(self) -> tuple[tuple[TicketBackField, str], ...]:
        return (
            (TicketBackField.MAILING_ADDRESS, self.mailing_address),
            (TicketBackField.STAGE, self.stage),
            (TicketBackField.DEADLINE, self.deadline),
        )


@dataclass(frozen=True)
class ArchiveSnapshot:
    """One archived capture of the official page, already fetched by the caller."""

    site: str
    captured_on: date
    mailing_address: str | None
    stage: str | None
    deadline: str | None

    def fields(self) -> tuple[tuple[TicketBackField, str | None], ...]:
        return (
            (TicketBackField.MAILING_ADDRESS, self.mailing_address),
            (TicketBackField.STAGE, self.stage),
            (TicketBackField.DEADLINE, self.deadline),
        )


@dataclass(frozen=True)
class TicketBackFact:
    field: TicketBackField
    value: str
    provenance: FactProvenance


@dataclass(frozen=True)
class ArchiveFallbackDecision:
    outcome: ArchiveOutcome
    stop_cause: ArchiveStopCause | None
    disclosure: str | None
    facts: tuple[TicketBackFact, ...]
    # Serviceability block key (W9). Always None: a blocked site never creates one.
    block: str | None = None


def is_blocked_status(status: int) -> bool:
    """True for a bot-block response. HTTP 403 only; never a process verdict."""
    return status == 403


_HTTP_STATUS_REASON = re.compile(r"HTTP (\d{3}) at ")


def blocked_site(failure: FetchFailure) -> LiveSiteBlock | None:
    """The bot block behind a fetch failure, or None when the site was not blocked.

    A definitive 4xx such as 404 is just a failed fetch; only 403 is a block.
    """
    match = _HTTP_STATUS_REASON.match(failure.reason)
    if match is None or not is_blocked_status(int(match.group(1))):
        return None
    return LiveSiteBlock(site=failure.url, http_status=int(match.group(1)))


def decide_archive_fallback(
    *,
    block: LiveSiteBlock,
    snapshot: ArchiveSnapshot | None,
    ticket_back: TicketBackFacts | None,
    today: date,
) -> ArchiveFallbackDecision:
    """Decide whether the ticket back plus the snapshot carry the Tier 1 facts.

    The snapshot must be at most MAX_SNAPSHOT_AGE_DAYS old and match the ticket
    back exactly on mailing address, stage and deadline. Anything else stops as
    NOT_YET_VERIFIED; the ticket back alone is never official.
    """
    if snapshot is None or ticket_back is None:
        return ArchiveFallbackDecision(
            outcome=ArchiveOutcome.NOT_YET_VERIFIED,
            stop_cause=ArchiveStopCause.MISSING_PIECE,
            disclosure=None,
            facts=(),
        )
    age_days = (today - snapshot.captured_on).days
    if not 0 <= age_days <= MAX_SNAPSHOT_AGE_DAYS:
        return ArchiveFallbackDecision(
            outcome=ArchiveOutcome.NOT_YET_VERIFIED,
            stop_cause=ArchiveStopCause.STALE_SNAPSHOT,
            disclosure=None,
            facts=(),
        )
    snapshot_fields = dict(snapshot.fields())
    ticket_fields = dict(ticket_back.fields())

    def blank(value: str | None) -> bool:
        return value is None or not value.strip()

    if any(blank(snapshot_fields[field]) for field in TicketBackField) or any(
        blank(ticket_fields[field]) for field in TicketBackField
    ):
        return ArchiveFallbackDecision(
            outcome=ArchiveOutcome.NOT_YET_VERIFIED,
            stop_cause=ArchiveStopCause.MISSING_PIECE,
            disclosure=None,
            facts=(),
        )
    if any(snapshot_fields[field] != ticket_fields[field] for field in TicketBackField):
        return ArchiveFallbackDecision(
            outcome=ArchiveOutcome.NOT_YET_VERIFIED,
            stop_cause=ArchiveStopCause.FACT_MISMATCH,
            disclosure=None,
            facts=(),
        )
    return ArchiveFallbackDecision(
        outcome=ArchiveOutcome.VERIFIED,
        stop_cause=None,
        disclosure=BLOCK_DISCLOSURE.format(site=block.site),
        facts=tuple(
            TicketBackFact(field=field, value=value, provenance=FactProvenance.CUSTOMER_PROVIDED)
            for field, value in ticket_back.fields()
        ),
    )
