"""The service phase controlling new paid authorizations.

Pure values only: no IO, environment, clock, dates or policy read. The configured phase is
resolved once at the configuration boundary and remains the fallback for the process. An
operator may store a private runtime override in the existing transaction database (see
``service_phase_store``); the effective phase is then resolved per request from that store,
never cached per process. Existing admitted money and case work are not phases; they keep
their own durable recovery paths.
"""

from enum import StrEnum


class ServicePhase(StrEnum):
    """Whether new paid authorization attempts may begin.

    ``PAID`` is the ordinary paid service. ``CONTROLLED_FREE_BETA`` pauses only the start
    of a *new* paid authorization; existing holds, linked paid cases and free-beta
    entitlements keep their own behavior.
    """

    PAID = "paid"
    CONTROLLED_FREE_BETA = "controlled_free_beta"


def parse_service_phase(value: str) -> ServicePhase:
    """Accept only the exact configured spellings; no aliases or guessing."""
    try:
        return ServicePhase(value)
    except ValueError:
        raise ValueError("service phase must be paid or controlled_free_beta") from None
