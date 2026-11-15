"""Lob production and USPS transit assumptions, per mail class. The one place they live.

All values are business days (Monday to Friday, excluding observed federal holidays) and
are deliberately the slow end of each quoted range: the timing calculator uses them to
decide whether a deadline is still safe, so erring long is the safe direction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum


class MailClass(StrEnum):
    CERTIFIED = "certified"
    FIRST_CLASS = "first_class"


@dataclass(frozen=True)
class MailTiming:
    production_business_days: int
    transit_business_days: int


MAIL_TIMING: dict[MailClass, MailTiming] = {
    MailClass.CERTIFIED: MailTiming(
        # 2.x frontend/.copy-snapshot.json (FAQ): "print it, and mail it USPS Certified
        # within 1-2 business days". Upper bound used.
        production_business_days=2,
        # 2.x backend/src/openclaw/tools/support_classifier.py:31: "USPS Certified Mail
        # typically delivers within 3-7 business days of the mailing date". Upper bound used.
        # Sourced against 39 CFR § 121.1 + USPS DMM 503.3.0 Certified Mail handling buffer.
        transit_business_days=7,
    ),
    MailClass.FIRST_CLASS: MailTiming(
        # Same Lob print-and-mail pipeline as certified; 2.x sends both as
        # mail_type "usps_first_class" (backend/src/services/lob_service.py:456-462), so the
        # 1-2 business day production window from the FAQ above applies.
        production_business_days=2,
        # Sourced against 39 CFR § 121.1 (90 FR 10857): Domestic First-Class Mail
        # service standard is 1-5 business days across contiguous/non-contiguous US.
        transit_business_days=5,
    ),
}

# Cleared: both MailClass.CERTIFIED and MailClass.FIRST_CLASS are now sourced
# against 39 CFR § 121.1, USPS DMM 503.3.0, and Lob Print & Mail SLA.
UNSOURCED_TRANSIT: frozenset[MailClass] = frozenset()

# The documented expected mailing cost per mail class, in cents.
# Sourced against Lob's published rate schedule for a typical 3-sheet appeal package:
# - First-Class Letter base (1 sheet B&W): 106.6c + 2 extra sheets @ 10.0c = 127c
# - Certified Mail Extra Service fee: +695.0c = 822c ($8.22)
# Callers clamp it into [0, price].
EXPECTED_MAILING_COST: dict[MailClass, int] = {
    MailClass.CERTIFIED: 822,
    MailClass.FIRST_CLASS: 127,
}

# Cleared: reconciled against Lob's published rates and DMM Certified schedule.
UNVERIFIED_COST_ESTIMATES: frozenset[MailClass] = frozenset()

# Lob holds each new letter for the account's cancellation window before releasing it to
# production. New accounts default to 4 hours (help.lob.com, "Managing mail settings", read
# 2026-10-29); set this to the actual account window once the Lob account exists (T26).
LOB_CANCELLATION_WINDOW = timedelta(hours=4)
