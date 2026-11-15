"""Returned mail remedy (architecture v6, section 8.3). Pure: no I/O, no Lob or Stripe calls.

On a confirmed return the cause is classified from the address mailed, the official source
text saved at mailing, the USPS return reason and any Lob production fault. The remedy then
follows from the cause alone:

    OUR_ERROR    free re-mail if feasible, otherwise full refund
    AGENCY_SIDE  proof-of-timely-mailing packet, plus a free re-mail only to another verified
                 official address when feasible; never a refund
    UNKNOWN      free re-mail if feasible, otherwise refund the price minus mailing cost

At most one re-mail ever exists per case, so once one has been used no remedy re-mails.
All money is integer cents.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from parkingbreaker.address_comparison import normalize_address as _comparison_key
from parkingbreaker.mail_config import EXPECTED_MAILING_COST, MailClass

MAX_REMAILS = 1


class ReturnCause(StrEnum):
    OUR_ERROR = "OUR_ERROR"
    AGENCY_SIDE = "AGENCY_SIDE"
    UNKNOWN = "UNKNOWN"


class UspsReturnReason(StrEnum):
    REFUSED = "REFUSED"
    UNCLAIMED = "UNCLAIMED"
    INSUFFICIENT_ADDRESS = "INSUFFICIENT_ADDRESS"
    ATTEMPTED_NOT_KNOWN = "ATTEMPTED_NOT_KNOWN"
    NO_SUCH_NUMBER = "NO_SUCH_NUMBER"
    VACANT = "VACANT"
    UNABLE_TO_FORWARD = "UNABLE_TO_FORWARD"
    OTHER = "OTHER"


AGENCY_SIDE_REASONS: frozenset[UspsReturnReason] = frozenset(
    {UspsReturnReason.REFUSED, UspsReturnReason.UNCLAIMED}
)


class RemedyKind(StrEnum):
    FREE_REMAIL = "FREE_REMAIL"
    FULL_REFUND = "FULL_REFUND"
    PROOF_PACKET = "PROOF_PACKET"
    PARTIAL_REFUND = "PARTIAL_REFUND"


@dataclass(frozen=True)
class Remedy:
    kind: RemedyKind
    refund_cents: int = 0
    remail_to_other_address: bool = False
    """Only with PROOF_PACKET: also re-mail to another verified official address."""

    @property
    def remails(self) -> bool:
        return self.kind is RemedyKind.FREE_REMAIL or self.remail_to_other_address


def normalize_address(address: str) -> str:
    """Case, punctuation, whitespace and common USPS abbreviations do not count as a difference."""
    return _comparison_key(address).upper()


def classify_return(
    mailed_address: str,
    official_address_at_mailing: str | None,
    usps_reason: UspsReturnReason | None,
    lob_production_fault: bool,
) -> ReturnCause:
    """Classify a confirmed return. Without a saved official source the cause is UNKNOWN
    unless Lob reports a production fault."""
    if lob_production_fault:
        return ReturnCause.OUR_ERROR
    if official_address_at_mailing is None:
        return ReturnCause.UNKNOWN
    if normalize_address(mailed_address) != normalize_address(official_address_at_mailing):
        return ReturnCause.OUR_ERROR
    if usps_reason in AGENCY_SIDE_REASONS:
        return ReturnCause.AGENCY_SIDE
    return ReturnCause.UNKNOWN


# Lob's documented carrier substatus spellings (TrackingEventDetails.event) for the
# same USPS return reasons. T26 must qualify this mapping against production webhooks.
_CARRIER_SUBSTATUS: dict[str, UspsReturnReason] = {
    "package_unclaimed": UspsReturnReason.UNCLAIMED,
    "refused": UspsReturnReason.REFUSED,
    "vacant": UspsReturnReason.VACANT,
    "no_such_number": UspsReturnReason.NO_SUCH_NUMBER,
    "insufficient_address": UspsReturnReason.INSUFFICIENT_ADDRESS,
    "unable_to_forward": UspsReturnReason.UNABLE_TO_FORWARD,
    "attempted_not_known": UspsReturnReason.ATTEMPTED_NOT_KNOWN,
    "returned_to_sender": UspsReturnReason.OTHER,
    "other": UspsReturnReason.OTHER,
}


def parse_usps_reason(value: object) -> UspsReturnReason | None:
    """The USPS return reason named by provider text, or None when it names none.

    Case and separators fold; the documented carrier substatus spellings map to the
    same USPS reasons. Anything unrecognized reports no reason rather than inventing
    one, so the classified cause stays UNKNOWN."""
    if not isinstance(value, str):
        return None
    key = re.sub(r"[\s\-]+", "_", value.strip().lower())
    for reason in UspsReturnReason:
        if key == reason.value.lower():
            return reason
    return _CARRIER_SUBSTATUS.get(key)


def estimated_mailing_cost(mail_class: MailClass, price_cents: int) -> int:
    """The documented expected cost for the mail class, as a returned-mail ESTIMATE.

    Never negative and never above the price: it only ever reduces a partial refund.
    mail_config flags the figure unverified until T26 reconciles actual Lob billing."""
    if type(price_cents) is not int or price_cents < 0:
        raise ValueError("amounts must be non-negative cents")
    return min(EXPECTED_MAILING_COST[mail_class], price_cents)


def decide_remedy(
    cause: ReturnCause,
    deadline_feasible: bool,
    other_verified_address_exists: bool,
    price_cents: int,
    mailing_cost_cents: int,
    remails_used: int,
) -> Remedy:
    if price_cents < 0 or mailing_cost_cents < 0:
        raise ValueError("amounts must be non-negative cents")
    if not 0 <= remails_used <= MAX_REMAILS:
        raise ValueError(f"remails_used must be between 0 and {MAX_REMAILS}")
    can_remail = deadline_feasible and remails_used < MAX_REMAILS
    match cause:
        case ReturnCause.OUR_ERROR:
            if can_remail:
                return Remedy(RemedyKind.FREE_REMAIL)
            return Remedy(RemedyKind.FULL_REFUND, refund_cents=price_cents)
        case ReturnCause.AGENCY_SIDE:
            return Remedy(
                RemedyKind.PROOF_PACKET,
                remail_to_other_address=can_remail and other_verified_address_exists,
            )
        case ReturnCause.UNKNOWN:
            if can_remail:
                return Remedy(RemedyKind.FREE_REMAIL)
            return Remedy(
                RemedyKind.PARTIAL_REFUND,
                refund_cents=max(0, price_cents - mailing_cost_cents),
            )
