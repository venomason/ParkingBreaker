"""Service assistance basis, owner service controls and intake decisions. Pure, no I/O."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from types import MappingProxyType

from parkingbreaker.agencies import (
    ApprovedCapability,
    ResolvedAgency,
    ServiceMode,
    validate_basis_key,
)
from parkingbreaker.case_state import HardStop, StopFamily
from parkingbreaker.mail_config import MailClass
from parkingbreaker.mail_timing import (
    ClockStart,
    DeadlinePeriod,
    DeadlineRule,
    deadline_for_period,
    latest_safe_send_date,
)


@dataclass(frozen=True)
class StateBasis:
    key: str
    service_mode: ServiceMode = ServiceMode.RESTRICTED_CLERICAL
    approved_capabilities: tuple[ApprovedCapability, ...] = ()
    rules_checked: tuple[str, ...] = ()
    links: tuple[str, ...] = ()
    date_checked: date | None = None
    how_met: str = ""
    checked_by: str = ""

    def __post_init__(self) -> None:
        validate_basis_key(self.key)
        if self.approved_capabilities and self.service_mode is not ServiceMode.EXPANDED:
            raise ValueError("approved_capabilities requires EXPANDED mode")
        names = [cap.name for cap in self.approved_capabilities]
        if len(names) != len(set(names)):
            raise ValueError("duplicate approved capability")

    @property
    def is_recorded(self) -> bool:
        return bool(
            self.rules_checked
            and all(rule.strip() for rule in self.rules_checked)
            and self.links
            and all(link.strip() for link in self.links)
            and self.date_checked
            and self.how_met.strip()
            and self.checked_by.strip()
        )


def _state_bases_with_owner_delegation(california: StateBasis) -> dict[str, StateBasis]:
    bases = {"CA": california}
    # The other 47 states and DC; AZ and NV remain held for legal review.
    for key in [
        "AL",
        "AK",
        "AR",
        "CO",
        "CT",
        "DE",
        "DC",
        "FL",
        "GA",
        "HI",
        "ID",
        "IL",
        "IN",
        "IA",
        "KS",
        "KY",
        "LA",
        "ME",
        "MD",
        "MA",
        "MI",
        "MN",
        "MS",
        "MO",
        "MT",
        "NE",
        "NH",
        "NJ",
        "NM",
        "NY",
        "NC",
        "ND",
        "OH",
        "OK",
        "OR",
        "PA",
        "RI",
        "SC",
        "SD",
        "TN",
        "TX",
        "UT",
        "VT",
        "VA",
        "WA",
        "WV",
        "WI",
        "WY",
    ]:
        bases[key] = StateBasis(
            key=key,
            service_mode=ServiceMode.RESTRICTED_CLERICAL,
            approved_capabilities=(),
            rules_checked=(
                "Owner stance 2026-10-13 applied nationally under the owner's delegation "
                "(docs/business-strategy.md §6): ParkingBreaker is a restricted scribe and "
                "formatting service. It organises and formats the customer's own facts and "
                "position, and gives no legal advice, strategy, merits assessment, "
                "representation or outcome promise. No state-specific document-preparer "
                "finding has been made.",
            ),
            how_met=california.how_met,
            links=california.links,
            date_checked=date(2026, 10, 14),
            checked_by="Supervisor (Claude) under owner delegation 2026-10-13",
        )
    return bases


STATE_BASIS: Mapping[str, StateBasis] = MappingProxyType(
    _state_bases_with_owner_delegation(
        StateBasis(
            key="CA",
            service_mode=ServiceMode.RESTRICTED_CLERICAL,
            approved_capabilities=(),
            rules_checked=(
                "Owner determination 2026-10-13: in California ParkingBreaker is a restricted "
                "scribe and formatting service. It organises and formats the customer's own "
                "facts and position, and gives no legal advice, strategy, merits assessment, "
                "representation or outcome promise. No separate Legal Document Assistant "
                "finding is made.",
            ),
            how_met=(
                "The customer supplies the facts and position and adopts the exact text; "
                "no legal arguments or citations are added. Disclaimers are shown as in the "
                "2.x legally reviewed copy: LegalDisclaimer.tsx:41-54, terms/page.tsx:75-90, "
                "what-we-are/page.tsx:53-62 and .copy-snapshot.json:665 (see links)."
            ),
            links=(
                "parkingbreaker 2.x frontend/components/LegalDisclaimer.tsx:41-54",
                "parkingbreaker 2.x frontend/app/[locale]/terms/page.tsx:75-90",
                "parkingbreaker 2.x frontend/app/[locale]/what-we-are/page.tsx:53-62",
                "parkingbreaker 2.x frontend/.copy-snapshot.json:665",
            ),
            date_checked=date(2026, 10, 13),
            checked_by="Amir (owner)",
        )
    )
)


class ServiceControlCategory(StrEnum):
    PRODUCT_SCOPE = "PRODUCT_SCOPE"
    ECONOMICS = "ECONOMICS"
    LEGAL = "LEGAL"
    ABUSE = "ABUSE"
    PROVIDER = "PROVIDER"


@dataclass(frozen=True)
class ServiceControlEntry:
    scope: str
    category: ServiceControlCategory
    detail: str
    author: str
    entered_on: date

    def __post_init__(self) -> None:
        for name in ("scope", "detail", "author"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be blank")
        if not isinstance(self.category, ServiceControlCategory):
            raise ValueError("category must be a ServiceControlCategory")
        if type(self.entered_on) is not date:
            raise ValueError("entered_on must be a date")


SERVICE_CONTROL: tuple[ServiceControlEntry, ...] = ()

# Retained for compatibility with explicit operator pause/testing injections.
# Entries match an agency id, one of its official domains, or a delegated scope host.
# Customer intake defaults to nationwide research; this constant is not a default gate.
LAUNCH_ALLOWLIST: frozenset[str] = frozenset({"sfmta.com"})
# Owner decision 2026-10-27: intake turns away tickets older than this, before any research
# or card hold.
MAX_TICKET_AGE_DAYS = 10
# The current offer is certified only; the standard tier stays hidden.
# A ticket certified cannot serve is a plain no-charge stop, never a downgrade.
LAUNCH_STANDARD_OFFERED = False
LAUNCH_EXCLUSION_DETAIL = (
    "ParkingBreaker is not yet available for this issuer. We will let you know when it is."
)


def on_launch_allowlist(agency: ResolvedAgency, allowlist: AbstractSet[str]) -> bool:
    names = {agency.id, *agency.official_domains, *(s.host for s in agency.delegated_scopes)}
    return not names.isdisjoint(allowlist)


class IssuerKind(StrEnum):
    GOVERNMENT = "GOVERNMENT"
    PRIVATE = "PRIVATE"


def service_control_entry(
    agency: ResolvedAgency, controls: Sequence[ServiceControlEntry]
) -> ServiceControlEntry | None:
    return next((entry for entry in controls if entry.scope in {agency.id, agency.basis_key}), None)


def recorded_basis(agency: ResolvedAgency, bases: Mapping[str, StateBasis]) -> StateBasis | None:
    basis = bases.get(agency.basis_key)
    return basis if basis and basis.key == agency.basis_key and basis.is_recorded else None


class IntakeSupport(StrEnum):
    SUPPORTED = "supported"
    NOT_YET_VERIFIED = "not_yet_verified"
    BUSINESS_EXCLUSION = "business_exclusion"


class IntakeStop(StrEnum):
    TOO_OLD = "too_old"
    RENTAL_PAID = "rental_paid"
    RENTAL_PAYMENT_UNKNOWN = "rental_payment_unknown"


def is_ticket_age_serviceable(
    *,
    issue_date: date,
    today: date,
    deadline_days: int | None = None,
    deadline_rule: DeadlineRule | None = None,
    mail_class: MailClass = MailClass.CERTIFIED,
) -> bool:
    """Evaluates whether a ticket's age is serviceable at intake.
    - When verified or pre-seeded jurisdiction deadline rules (`deadline_days`
      and `deadline_rule`) are provided, uses postal reachability via
      `deadline_for_period` and `latest_safe_send_date`.
    - When jurisdiction rules are not yet known at pre-hold intake, falls back
      to the conservative static `MAX_TICKET_AGE_DAYS` (10 days) cutoff.
    """
    if (today - issue_date).days < 0:
        return False
    if deadline_days is not None and deadline_rule is not None:
        period = DeadlinePeriod(days=deadline_days)
        deadline = deadline_for_period(issue_date, period, ClockStart.ISSUE_DATE)
        latest_send = latest_safe_send_date(deadline, deadline_rule, mail_class)
        return today <= latest_send
    return (today - issue_date).days <= MAX_TICKET_AGE_DAYS


def intake_customer_stop(
    issue_date: date,
    today: date,
    rental_company_paid: bool | str | None,
    *,
    deadline_days: int | None = None,
    deadline_rule: DeadlineRule | None = None,
    mail_class: MailClass = MailClass.CERTIFIED,
) -> IntakeStop | None:
    """Customer facts checked before issuer research; today is the intake clock's date."""
    if not is_ticket_age_serviceable(
        issue_date=issue_date,
        today=today,
        deadline_days=deadline_days,
        deadline_rule=deadline_rule,
        mail_class=mail_class,
    ):
        return IntakeStop.TOO_OLD
    if rental_company_paid is True:
        return IntakeStop.RENTAL_PAID
    if rental_company_paid is False or rental_company_paid == "NOT_RENTAL":
        return None
    return IntakeStop.RENTAL_PAYMENT_UNKNOWN


@dataclass(frozen=True)
class IntakeSupportResult:
    status: IntakeSupport
    family: StopFamily | None = None
    hard_stop: HardStop | None = None
    detail: str = ""
    control_entry: ServiceControlEntry | None = None


def intake_support_check(
    agency: ResolvedAgency | None,
    issuer_kind: IssuerKind,
    state_basis: Mapping[str, StateBasis],
    controls: Sequence[ServiceControlEntry],
    notice_date: date,
    printed_deadline: date | None,
    today: date,
    *,
    launch_allowlist: AbstractSet[str] | None,
) -> IntakeSupportResult:
    """Calendar and policy check before a hold; unfamiliar agencies need no activation.

    ``launch_allowlist=None`` is the nationwide customer default. An explicitly injected
    set remains available for temporary operator pauses and focused tests.
    """
    if notice_date > today:
        return IntakeSupportResult(
            IntakeSupport.NOT_YET_VERIFIED,
            StopFamily.NOT_YET_VERIFIED,
            HardStop.S4,
            "The notice date is after today and must be corrected by the customer.",
        )
    # Product scope comes first: a private issuer usually has no official source to
    # resolve, and "not verified" would misstate a deliberate exclusion.
    if issuer_kind is IssuerKind.PRIVATE:
        return IntakeSupportResult(
            IntakeSupport.BUSINESS_EXCLUSION,
            StopFamily.BUSINESS_EXCLUSION,
            detail="Private parking notices are excluded by product scope.",
        )
    if agency is None:
        return IntakeSupportResult(
            IntakeSupport.NOT_YET_VERIFIED,
            StopFamily.NOT_YET_VERIFIED,
            detail="Issuing agency could not be resolved to an official source.",
        )
    entry = service_control_entry(agency, controls)
    if entry is not None:
        return IntakeSupportResult(
            IntakeSupport.BUSINESS_EXCLUSION,
            StopFamily.BUSINESS_EXCLUSION,
            detail=entry.detail,
            control_entry=entry,
        )
    if launch_allowlist is not None and not on_launch_allowlist(agency, launch_allowlist):
        return IntakeSupportResult(
            IntakeSupport.BUSINESS_EXCLUSION,
            StopFamily.BUSINESS_EXCLUSION,
            detail=LAUNCH_EXCLUSION_DETAIL,
        )
    if recorded_basis(agency, state_basis) is None:
        return IntakeSupportResult(
            IntakeSupport.NOT_YET_VERIFIED,
            StopFamily.NOT_YET_VERIFIED,
            HardStop.S6,
            "Service basis is not yet recorded.",
        )
    if printed_deadline is not None and printed_deadline < today:
        return IntakeSupportResult(
            IntakeSupport.NOT_YET_VERIFIED,
            StopFamily.NOT_YET_VERIFIED,
            HardStop.S4,
            "The printed deadline has passed; the applicable rule and any later stage "
            "still need verification.",
        )
    return IntakeSupportResult(IntakeSupport.SUPPORTED)
