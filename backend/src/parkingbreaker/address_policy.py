"""Shared address provenance, consent and refund-responsibility policy (T30).

Contract: build-kit/ADDRESS-RESPONSIBILITY.md. Pure: no clocks, files, environment or network.
Callers pass every value in, including tz-aware datetimes, and persist the returned
DestinationDecision; later stages read that record instead of recomputing responsibility.

Two provenance kinds exist and nothing else:

    SYSTEM_VERIFIED               deterministic evidence we hold establishes the destination
    CUSTOMER_DIRECTED_UNVERIFIED  we could not verify; the customer gave a complete address
                                  and explicitly accepted the specific current disclosure

A customer's approval never turns the second kind into the first, and never moves
responsibility for the first onto the customer. An LLM summary is not evidence: system
verification requires deterministic evidence references, otherwise the claim is rejected.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum

from parkingbreaker.agencies import MailingAddress
from parkingbreaker.research.ladder import normalize_address
from parkingbreaker.returned_mail import Remedy, RemedyKind, ReturnCause, decide_remedy

_SHA256 = re.compile(r"[0-9a-f]{64}")
_ZIP = re.compile(r"\d{5}(-\d{4})?")
_STATE = re.compile(r"[A-Z]{2}")

AI_ONLY_VERIFICATION_REJECTED = "AI_ONLY_VERIFICATION_REJECTED"
VERIFICATION_NOT_CLAIMED = "VERIFICATION_NOT_CLAIMED"


class AddressProvenance(StrEnum):
    SYSTEM_VERIFIED = "SYSTEM_VERIFIED"
    CUSTOMER_DIRECTED_UNVERIFIED = "CUSTOMER_DIRECTED_UNVERIFIED"


class EvidenceKind(StrEnum):
    OFFICIAL_SOURCE_QUOTE = "OFFICIAL_SOURCE_QUOTE"
    RESOLUTION_CHECK = "RESOLUTION_CHECK"
    LLM_SUMMARY = "LLM_SUMMARY"


DETERMINISTIC_EVIDENCE_KINDS: frozenset[EvidenceKind] = frozenset(
    {EvidenceKind.OFFICIAL_SOURCE_QUOTE, EvidenceKind.RESOLUTION_CHECK}
)


class ResearchClaim(StrEnum):
    """What research asserted. A claim is not provenance; evidence decides that."""

    CLAIMS_VERIFIED = "CLAIMS_VERIFIED"
    NOT_VERIFIED = "NOT_VERIFIED"


class OutcomeKind(StrEnum):
    SEND_SYSTEM_VERIFIED = "SEND_SYSTEM_VERIFIED"
    STOP_NO_CAPTURE_RELEASE_HOLD = "STOP_NO_CAPTURE_RELEASE_HOLD"
    SEND_CUSTOMER_DIRECTED = "SEND_CUSTOMER_DIRECTED"
    REQUEST_COMPLETE_ADDRESS = "REQUEST_COMPLETE_ADDRESS"
    REQUEST_CURRENT_CONSENT = "REQUEST_CURRENT_CONSENT"
    INVALIDATE_AND_REBUILD = "INVALIDATE_AND_REBUILD"
    WAIT_OTHER_GATES = "WAIT_OTHER_GATES"


class OutcomeReason(StrEnum):
    VERIFIED_BY_EVIDENCE = "VERIFIED_BY_EVIDENCE"
    CONSENT_VALID = "CONSENT_VALID"
    NO_CUSTOMER_ADDRESS = "NO_CUSTOMER_ADDRESS"
    CUSTOMER_DECLINED = "CUSTOMER_DECLINED"
    ADDRESS_INCOMPLETE = "ADDRESS_INCOMPLETE"
    NO_DISCLOSURE_SHOWN = "NO_DISCLOSURE_SHOWN"
    CONSENT_ABSENT = "CONSENT_ABSENT"
    CONSENT_OTHER_CASE = "CONSENT_OTHER_CASE"
    CONSENT_OTHER_OWNER = "CONSENT_OTHER_OWNER"
    CONSENT_DISCLOSURE_STALE = "CONSENT_DISCLOSURE_STALE"
    CONSENT_POSTDATES_DECISION = "CONSENT_POSTDATES_DECISION"
    CONSENT_BOUND_TO_DIFFERENT_ADDRESS = "CONSENT_BOUND_TO_DIFFERENT_ADDRESS"
    CONSENT_STALE_REVISION = "CONSENT_STALE_REVISION"
    APPROVAL_BOUND_TO_DIFFERENT_ADDRESS = "APPROVAL_BOUND_TO_DIFFERENT_ADDRESS"
    PACKET_NOT_APPROVED = "PACKET_NOT_APPROVED"
    OTHER_GATES_UNRESOLVED = "OTHER_GATES_UNRESOLVED"


class Responsibility(StrEnum):
    OUR_ERROR_FULL_REFUND = "OUR_ERROR_FULL_REFUND"
    CUSTOMER_DIRECTED_ADDRESS_EXCLUSION = "CUSTOMER_DIRECTED_ADDRESS_EXCLUSION"
    UNKNOWN_CAUSE_EXISTING_REMEDY = "UNKNOWN_CAUSE_EXISTING_REMEDY"
    AGENCY_SIDE_EXISTING_REMEDY = "AGENCY_SIDE_EXISTING_REMEDY"
    LEGACY_NO_RECORD = "LEGACY_NO_RECORD"


class AddressFaultFinding(StrEnum):
    """What mailing evidence shows about the destination that was mailed."""

    NOT_ESTABLISHED = "NOT_ESTABLISHED"
    DESTINATION_WRONG_SOLE_CAUSE = "DESTINATION_WRONG_SOLE_CAUSE"
    DESTINATION_WRONG_WITH_OTHER_CAUSES = "DESTINATION_WRONG_WITH_OTHER_CAUSES"
    DESTINATION_NOT_AT_FAULT = "DESTINATION_NOT_AT_FAULT"


# ---------------------------------------------------------------- validation helpers


def _require_text(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-blank string")


def _require_sha256(value: object, name: str) -> None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{name} must be 64 lowercase hex characters")


def _require_aware(value: object, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")


def _require_revision(value: object, name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative int")


def _require_bool(value: object, name: str) -> None:
    if type(value) is not bool:
        raise ValueError(f"{name} must be a bool")


def address_fingerprint(address: MailingAddress) -> str:
    """sha256 of the canonical JSON of every field, exact bytes: no case folding, no
    whitespace or abbreviation normalization. It binds the exact structured address the
    customer saw; None and the empty string stay distinct."""
    canonical = json.dumps(asdict(address), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def address_is_complete(address: MailingAddress) -> bool:
    """A complete postal destination: recipient, street, city, state and ZIP present and
    well formed. Completeness is not deliverability or official status."""
    if not all(part.strip() for part in (address.recipient, address.line1, address.city)):
        return False
    if not address.country.strip():
        return False
    if address.country == "US":
        return bool(_STATE.fullmatch(address.state) and _ZIP.fullmatch(address.zip_code))
    return bool(address.state.strip() and address.zip_code.strip())


# ---------------------------------------------------------------- records


@dataclass(frozen=True)
class EvidenceRef:
    evidence_id: str
    sha256: str
    kind: EvidenceKind

    def __post_init__(self) -> None:
        _require_text(self.evidence_id, "evidence_id")
        _require_sha256(self.sha256, "evidence sha256")
        if not isinstance(self.kind, EvidenceKind):
            raise ValueError("evidence kind must be an EvidenceKind")

    @property
    def deterministic(self) -> bool:
        return self.kind in DETERMINISTIC_EVIDENCE_KINDS


@dataclass(frozen=True)
class Disclosure:
    """The exact disclosure text shown: its version and the sha256 of its bytes."""

    version: str
    sha256: str

    def __post_init__(self) -> None:
        _require_text(self.version, "disclosure version")
        _require_sha256(self.sha256, "disclosure sha256")


@dataclass(frozen=True)
class ConsentEvent:
    """An explicit, non-default acceptance bound to one case, owner, address and disclosure."""

    case_id: str
    owner: str
    address_fingerprint: str
    disclosure_sha256: str
    address_revision: int
    accepted: bool
    accepted_at: datetime

    def __post_init__(self) -> None:
        _require_text(self.case_id, "consent case_id")
        _require_text(self.owner, "consent owner")
        _require_sha256(self.address_fingerprint, "consent address_fingerprint")
        _require_sha256(self.disclosure_sha256, "consent disclosure_sha256")
        _require_revision(self.address_revision, "consent address_revision")
        if self.accepted is not True:
            raise ValueError("consent must be an explicit acceptance (accepted is True)")
        _require_aware(self.accepted_at, "consent accepted_at")


@dataclass(frozen=True)
class DestinationDecision:
    """The immutable destination record that travels with the case and the frozen request."""

    case_id: str
    owner: str
    address: MailingAddress
    fingerprint: str
    provenance: AddressProvenance
    refund_policy_version: str
    decided_at: datetime
    verification_evidence: tuple[EvidenceRef, ...] = ()
    failed_verification_code: str | None = None
    customer_address_revision: int | None = None
    disclosure: Disclosure | None = None
    consent: ConsentEvent | None = None

    def __post_init__(self) -> None:
        _require_text(self.case_id, "case_id")
        _require_text(self.owner, "owner")
        _require_text(self.refund_policy_version, "refund_policy_version")
        _require_aware(self.decided_at, "decided_at")
        if self.fingerprint != address_fingerprint(self.address):
            raise ValueError("fingerprint does not match the address")
        if not isinstance(self.provenance, AddressProvenance):
            raise ValueError("provenance must be an AddressProvenance")
        if self.provenance is AddressProvenance.SYSTEM_VERIFIED:
            self._check_system_verified()
        else:
            self._check_customer_directed()

    def _check_system_verified(self) -> None:
        if not self.verification_evidence:
            raise ValueError("SYSTEM_VERIFIED requires verification evidence")
        if not all(ref.deterministic for ref in self.verification_evidence):
            raise ValueError("SYSTEM_VERIFIED requires deterministic evidence only")
        if (
            self.failed_verification_code is not None
            or self.customer_address_revision is not None
            or self.disclosure is not None
            or self.consent is not None
        ):
            raise ValueError("SYSTEM_VERIFIED carries no customer-directed fields")

    def _check_customer_directed(self) -> None:
        if self.verification_evidence:
            raise ValueError("CUSTOMER_DIRECTED_UNVERIFIED carries no verification evidence")
        _require_text(self.failed_verification_code, "failed_verification_code")
        _require_revision(self.customer_address_revision, "customer_address_revision")
        consent = self.consent
        if self.disclosure is None or consent is None:
            raise ValueError("CUSTOMER_DIRECTED_UNVERIFIED requires a disclosure and consent")
        if consent.case_id != self.case_id or consent.owner != self.owner:
            raise ValueError("consent belongs to another case or owner")
        if consent.address_fingerprint != self.fingerprint:
            raise ValueError("consent is bound to a different address")
        if consent.address_revision != self.customer_address_revision:
            raise ValueError("consent is bound to a different address revision")
        if consent.disclosure_sha256 != self.disclosure.sha256:
            raise ValueError("consent is bound to a different disclosure")
        if consent.accepted_at > self.decided_at:
            raise ValueError("consent postdates the decision")


# ---------------------------------------------------------------- destination decision


@dataclass(frozen=True)
class ResearchVerification:
    """Research's result. The claim alone never establishes SYSTEM_VERIFIED."""

    claim: ResearchClaim
    verified_address: MailingAddress | None = None
    evidence: tuple[EvidenceRef, ...] = ()
    failure_code: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.claim, ResearchClaim):
            raise ValueError("claim must be a ResearchClaim")
        if self.claim is ResearchClaim.CLAIMS_VERIFIED:
            if self.verified_address is None:
                raise ValueError("a verification claim names the verified address")
        else:
            _require_text(self.failure_code, "failure_code")


@dataclass(frozen=True)
class DestinationFacts:
    case_id: str
    owner: str
    decided_at: datetime
    refund_policy_version: str
    research: ResearchVerification
    other_gates_passed: bool
    customer_address: MailingAddress | None = None
    customer_address_revision: int | None = None
    customer_declined: bool = False
    consent: ConsentEvent | None = None
    current_disclosure: Disclosure | None = None
    approved_packet_fingerprint: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.case_id, "case_id")
        _require_text(self.owner, "owner")
        _require_text(self.refund_policy_version, "refund_policy_version")
        _require_aware(self.decided_at, "decided_at")
        _require_bool(self.other_gates_passed, "other_gates_passed")
        _require_bool(self.customer_declined, "customer_declined")
        if self.customer_address is not None:
            _require_revision(self.customer_address_revision, "customer_address_revision")
        if self.approved_packet_fingerprint is not None:
            _require_sha256(self.approved_packet_fingerprint, "approved_packet_fingerprint")


_SEND_KINDS = frozenset({OutcomeKind.SEND_SYSTEM_VERIFIED, OutcomeKind.SEND_CUSTOMER_DIRECTED})


@dataclass(frozen=True)
class DestinationOutcome:
    kind: OutcomeKind
    reason: OutcomeReason
    decision: DestinationDecision | None = None
    """The record to persist. Present when a provenance is permitted (sending or waiting on
    other gates), absent when nothing may be mailed to a destination yet."""
    failed_verification_code: str | None = None

    @property
    def may_send(self) -> bool:
        return self.kind in _SEND_KINDS

    @property
    def capture_allowed(self) -> bool:
        return self.may_send

    @property
    def release_hold(self) -> bool:
        """Every outcome other than a send leaves no capture, so any hold is released."""
        return not self.may_send

    @property
    def requires_new_consent(self) -> bool:
        return self.kind in {
            OutcomeKind.REQUEST_CURRENT_CONSENT,
            OutcomeKind.INVALIDATE_AND_REBUILD,
        }


def _send_or_wait(
    facts: DestinationFacts,
    decision: DestinationDecision,
    send_kind: OutcomeKind,
    reason: OutcomeReason,
) -> DestinationOutcome:
    approved = facts.approved_packet_fingerprint
    if approved is not None and approved != decision.fingerprint:
        return DestinationOutcome(
            OutcomeKind.INVALIDATE_AND_REBUILD, OutcomeReason.APPROVAL_BOUND_TO_DIFFERENT_ADDRESS
        )
    if not facts.other_gates_passed:
        return DestinationOutcome(
            OutcomeKind.WAIT_OTHER_GATES, OutcomeReason.OTHER_GATES_UNRESOLVED, decision
        )
    if approved is None:
        return DestinationOutcome(
            OutcomeKind.WAIT_OTHER_GATES, OutcomeReason.PACKET_NOT_APPROVED, decision
        )
    return DestinationOutcome(send_kind, reason, decision)


def _system_verified_decision(facts: DestinationFacts) -> DestinationDecision | None:
    research = facts.research
    if research.claim is not ResearchClaim.CLAIMS_VERIFIED or research.verified_address is None:
        return None
    evidence = tuple(ref for ref in research.evidence if ref.deterministic)
    if not evidence:
        return None
    return DestinationDecision(
        case_id=facts.case_id,
        owner=facts.owner,
        address=research.verified_address,
        fingerprint=address_fingerprint(research.verified_address),
        provenance=AddressProvenance.SYSTEM_VERIFIED,
        refund_policy_version=facts.refund_policy_version,
        decided_at=facts.decided_at,
        verification_evidence=evidence,
    )


def _failed_verification_code(research: ResearchVerification) -> str:
    if research.claim is ResearchClaim.CLAIMS_VERIFIED:
        return AI_ONLY_VERIFICATION_REJECTED
    return research.failure_code or VERIFICATION_NOT_CLAIMED


def _consent_problem(
    facts: DestinationFacts, consent: ConsentEvent, fingerprint: str, disclosure: Disclosure
) -> tuple[OutcomeKind, OutcomeReason] | None:
    request = OutcomeKind.REQUEST_CURRENT_CONSENT
    invalidate = OutcomeKind.INVALIDATE_AND_REBUILD
    if consent.case_id != facts.case_id:
        return request, OutcomeReason.CONSENT_OTHER_CASE
    if consent.owner != facts.owner:
        return request, OutcomeReason.CONSENT_OTHER_OWNER
    if consent.address_fingerprint != fingerprint:
        return invalidate, OutcomeReason.CONSENT_BOUND_TO_DIFFERENT_ADDRESS
    if consent.address_revision != facts.customer_address_revision:
        return invalidate, OutcomeReason.CONSENT_STALE_REVISION
    if consent.disclosure_sha256 != disclosure.sha256:
        return request, OutcomeReason.CONSENT_DISCLOSURE_STALE
    if consent.accepted_at > facts.decided_at:
        return request, OutcomeReason.CONSENT_POSTDATES_DECISION
    return None


def decide_destination(facts: DestinationFacts) -> DestinationOutcome:
    """Apply every row of the ADDRESS-RESPONSIBILITY.md decision table."""
    verified = _system_verified_decision(facts)
    if verified is not None:
        # The customer's consent or approval is irrelevant: the provenance is ours.
        return _send_or_wait(
            facts,
            verified,
            OutcomeKind.SEND_SYSTEM_VERIFIED,
            OutcomeReason.VERIFIED_BY_EVIDENCE,
        )

    code = _failed_verification_code(facts.research)
    address = facts.customer_address

    def stop(kind: OutcomeKind, reason: OutcomeReason) -> DestinationOutcome:
        return DestinationOutcome(kind, reason, failed_verification_code=code)

    if facts.customer_declined:
        return stop(OutcomeKind.STOP_NO_CAPTURE_RELEASE_HOLD, OutcomeReason.CUSTOMER_DECLINED)
    if address is None:
        return stop(OutcomeKind.STOP_NO_CAPTURE_RELEASE_HOLD, OutcomeReason.NO_CUSTOMER_ADDRESS)
    if not address_is_complete(address):
        return stop(OutcomeKind.REQUEST_COMPLETE_ADDRESS, OutcomeReason.ADDRESS_INCOMPLETE)

    fingerprint = address_fingerprint(address)
    disclosure = facts.current_disclosure
    if disclosure is None:
        return stop(OutcomeKind.REQUEST_CURRENT_CONSENT, OutcomeReason.NO_DISCLOSURE_SHOWN)
    consent = facts.consent
    if consent is None:
        return stop(OutcomeKind.REQUEST_CURRENT_CONSENT, OutcomeReason.CONSENT_ABSENT)
    problem = _consent_problem(facts, consent, fingerprint, disclosure)
    if problem is not None:
        return stop(*problem)

    revision = facts.customer_address_revision
    decision = DestinationDecision(
        case_id=facts.case_id,
        owner=facts.owner,
        address=address,
        fingerprint=fingerprint,
        provenance=AddressProvenance.CUSTOMER_DIRECTED_UNVERIFIED,
        refund_policy_version=facts.refund_policy_version,
        decided_at=facts.decided_at,
        failed_verification_code=code,
        customer_address_revision=revision,
        disclosure=disclosure,
        consent=consent,
    )
    return _send_or_wait(
        facts, decision, OutcomeKind.SEND_CUSTOMER_DIRECTED, OutcomeReason.CONSENT_VALID
    )


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    reason: str


def mailing_gate(
    decision: DestinationDecision | None,
    consents: Sequence[ConsentEvent],
    *,
    case_id: str,
    owner: str,
    mailed_address: MailingAddress,
    latest_customer_address_revision: int | None,
    now: datetime,
    current_disclosure_sha256: str | None = None,
    returned_address: MailingAddress | None = None,
) -> GateResult:
    """Permit only a persisted, scoped instruction for the exact outgoing bytes.

    `returned_address` marks the one free re-mail after a return: the destination must then
    be a persisted SYSTEM_VERIFIED decision whose fingerprint differs from the address of
    the returned letter. Never the old address and never a customer-directed unverified one.

    The caller loads persisted consents, the current disclosure hash and latest address revision:
    an embedded acceptance cannot prove persistence or cover a subsequent edit.
    Refusal codes contain no customer data.
    """
    refused = GateResult(False, "ADDRESS_MAILING_GATE_REFUSED")
    _require_aware(now, "now")
    if decision is None:
        return refused
    if (
        decision.case_id != case_id
        or decision.owner != owner
        or decision.decided_at > now
        or address_fingerprint(mailed_address) != decision.fingerprint
    ):
        return refused
    try:
        # Revalidate even a forged frozen value; includes deterministic evidence,
        # exact address integrity and all decide_destination consent bindings.
        decision.__post_init__()
    except ValueError:
        return refused
    if returned_address is not None and (
        decision.provenance is not AddressProvenance.SYSTEM_VERIFIED
        or decision.fingerprint == address_fingerprint(returned_address)
    ):
        return refused
    if decision.provenance is AddressProvenance.SYSTEM_VERIFIED:
        return GateResult(True, "VERIFIED_BY_EVIDENCE")
    # Owner override 2026-11-14. Historical consent is preserved/decodable, but
    # no longer authorizes a new physical create. Prior intents reconcile through
    # the coordinator's lookup path; this gate never rewrites the saved decision.
    return refused


# ---------------------------------------------------------------- responsibility


@dataclass(frozen=True)
class FailureFacts:
    """Evidence about a failed mailing, read from the persisted record and mailing evidence."""

    case_id: str
    return_cause: ReturnCause = ReturnCause.UNKNOWN
    mailed_address_fingerprint: str | None = None
    omitted_required_contents: bool = False
    duplicate_charge: bool = False
    service_failure: bool = False
    address_finding: AddressFaultFinding = AddressFaultFinding.NOT_ESTABLISHED
    finding_evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.case_id, "case_id")
        _require_bool(self.omitted_required_contents, "omitted_required_contents")
        _require_bool(self.duplicate_charge, "duplicate_charge")
        _require_bool(self.service_failure, "service_failure")
        if self.mailed_address_fingerprint is not None:
            _require_sha256(self.mailed_address_fingerprint, "mailed_address_fingerprint")


@dataclass(frozen=True)
class ResponsibilityDecision:
    responsibility: Responsibility
    reason: str
    existing_cause: ReturnCause | None
    """The returned-mail cause whose existing remedy applies; None when the exclusion applies."""
    unconditional_full_refund: bool = False
    """True only for a SYSTEM_VERIFIED destination that proved wrong: full refund, always."""

    @property
    def exclusion_applies(self) -> bool:
        return self.responsibility is Responsibility.CUSTOMER_DIRECTED_ADDRESS_EXCLUSION


def _ours(reason: str, unconditional_full_refund: bool = False) -> ResponsibilityDecision:
    return ResponsibilityDecision(
        Responsibility.OUR_ERROR_FULL_REFUND,
        reason,
        ReturnCause.OUR_ERROR,
        unconditional_full_refund=unconditional_full_refund,
    )


def _existing(cause: ReturnCause, reason: str) -> ResponsibilityDecision:
    if cause is ReturnCause.OUR_ERROR:
        return _ours(reason)
    if cause is ReturnCause.AGENCY_SIDE:
        return ResponsibilityDecision(Responsibility.AGENCY_SIDE_EXISTING_REMEDY, reason, cause)
    return ResponsibilityDecision(Responsibility.UNKNOWN_CAUSE_EXISTING_REMEDY, reason, cause)


_WRONG = frozenset(
    {
        AddressFaultFinding.DESTINATION_WRONG_SOLE_CAUSE,
        AddressFaultFinding.DESTINATION_WRONG_WITH_OTHER_CAUSES,
    }
)


def classify_address_failure(
    decision: DestinationDecision | None, facts: FailureFacts
) -> ResponsibilityDecision:
    """Responsibility for a failed mailing. `decision` is the persisted record, or None for a
    legacy case that has none. The exclusion is narrow: it needs a customer-directed record
    with valid matching consent, proof we mailed exactly that address, and evidence that the
    supplied address was the sole cause. Unknown cause is never customer fault."""
    if decision is not None and decision.case_id != facts.case_id:
        raise ValueError("destination decision belongs to another case")

    # A SYSTEM_VERIFIED destination that proved wrong keeps its unconditional full
    # refund even when another of our faults also appears: an earlier branch may stand
    # in for the wrong-destination finding, but never drops the refund for a re-mail.
    wrong_verified_destination = (
        decision is not None
        and decision.provenance is AddressProvenance.SYSTEM_VERIFIED
        and facts.address_finding in _WRONG
    )

    if facts.service_failure:
        return _ours("SERVICE_FAILURE", wrong_verified_destination)
    if facts.duplicate_charge:
        return _ours("DUPLICATE_CHARGE", wrong_verified_destination)
    if facts.omitted_required_contents:
        return _ours("OMITTED_REQUIRED_CONTENTS", wrong_verified_destination)
    if facts.return_cause is ReturnCause.OUR_ERROR:
        return _ours("RETURN_CAUSE_OUR_ERROR", wrong_verified_destination)
    if (
        decision is not None
        and facts.mailed_address_fingerprint is not None
        and facts.mailed_address_fingerprint != decision.fingerprint
    ):
        return _ours("MAILED_BYTES_DIFFER_FROM_DECISION", wrong_verified_destination)

    if decision is None:
        return ResponsibilityDecision(
            Responsibility.LEGACY_NO_RECORD, "NO_PROVENANCE_RECORD", facts.return_cause
        )

    if decision.provenance is AddressProvenance.SYSTEM_VERIFIED:
        if facts.address_finding in _WRONG:
            return ResponsibilityDecision(
                Responsibility.OUR_ERROR_FULL_REFUND,
                "SYSTEM_VERIFIED_DESTINATION_WRONG",
                ReturnCause.OUR_ERROR,
                unconditional_full_refund=True,
            )
        return _existing(facts.return_cause, "SYSTEM_VERIFIED_NO_ADDRESS_FAULT_FOUND")

    if (
        facts.address_finding is AddressFaultFinding.DESTINATION_WRONG_SOLE_CAUSE
        and facts.finding_evidence_ids
        and all(item.strip() for item in facts.finding_evidence_ids)
        and facts.mailed_address_fingerprint == decision.fingerprint
        and facts.return_cause is ReturnCause.UNKNOWN
    ):
        return ResponsibilityDecision(
            Responsibility.CUSTOMER_DIRECTED_ADDRESS_EXCLUSION,
            "SUPPLIED_ADDRESS_SOLE_CAUSE",
            None,
        )
    return _existing(facts.return_cause, "SOLE_CAUSE_NOT_ESTABLISHED")


def remedy_for_responsibility(
    result: ResponsibilityDecision,
    *,
    deadline_feasible: bool,
    other_verified_address_exists: bool,
    price_cents: int,
    mailing_cost_cents: int,
    remails_used: int,
) -> Remedy | None:
    """Map a responsibility onto returned_mail's existing remedies. The exclusion removes only
    the ordinary address-error remedy (None); it never invents an amount. A system-verified
    destination that proved wrong is an unconditional full refund, whatever the deadline or
    re-mail state; other our-errors keep the existing service-error remedy."""
    if result.unconditional_full_refund:
        if price_cents < 0:
            raise ValueError("amounts must be non-negative cents")
        return Remedy(RemedyKind.FULL_REFUND, refund_cents=price_cents)
    if result.existing_cause is None:
        return None
    return decide_remedy(
        result.existing_cause,
        deadline_feasible,
        other_verified_address_exists,
        price_cents,
        mailing_cost_cents,
        remails_used,
    )


# --- Payment-address match (T30, owner decision 20.21) --------------------------------------
# The commonest customer-directed mistake is typing the ticket's payment address (a lockbox)
# instead of the appeal address. Payment addresses are read mechanically from the saved text
# of the case's official sources; the customer's address is compared as (normalised line1,
# ZIP5). An address that official text also offers for contests or appeals is never treated
# as a payment address, so this check can only refuse a mistake, never a legitimate choice.

_PO_BOX_TEXT = re.compile(
    r"\b(?:p\.?\s*o\.?\s*box|post\s+office\s+box)\b\.?\s*#?\s*", re.IGNORECASE
)
_PO_LINE = r"(?:p\.?[ ]?o\.?[ ]?box|post[ ]office[ ]box)[ ]*#?[ ]*\d{1,10}"
_STREET_SUFFIX = (
    r"(?:st|street|ave|av|avenue|blvd|boulevard|rd|road|dr|drive|ln|lane|pl|place|ct|court"
    r"|way|pkwy|parkway|hwy|highway|plaza|sq|square|ter|terrace|cir|circle)"
)
_STREET_LINE = rf"\d{{1,6}}[ ](?:[A-Za-z0-9.'-]+[ ]){{0,4}}{_STREET_SUFFIX}\b\.?"
_PAYMENT_ADDRESS = re.compile(
    rf"(?P<line1>{_PO_LINE}|{_STREET_LINE})"
    r"[,\s]+(?:(?:suite|ste|room|rm|floor|fl|unit)\.?[ ]*#?\w+[,\s]+)?"
    r"[A-Za-z][A-Za-z .'-]{1,30}?,?\s+[A-Z]{2}\s+(?P<zip>\d{5})(?:-\d{4})?(?!\d)",
    re.IGNORECASE,
)
_PAYMENT_CUE = re.compile(
    r"\b(?:pre-?pay\w*|pay(?:s|ing|ments?|able)?|lockbox|remit\w*"
    r"|mail(?:ing)? (?:a )?(?:check|money order))\b",
    re.I,
)
_CONTEST_CUE = re.compile(
    r"\b(?:contest\w*|appeal\w*|protest\w*|dispute\w*|reviews?|hearings?|adjudicat\w*)\b", re.I
)
_SENTENCE_END = re.compile(r"[.;!?](?:\s|$)|\n\s*\n")
_CONTEXT_CHARS = 200
_TAIL_CHARS = 60


def address_line_key(line1: str) -> str:
    """Comparison key for a delivery line: PO Box variants, suffix and punctuation folded."""
    return normalize_address(_PO_BOX_TEXT.sub("po box ", line1))


def _zip5(zip_code: str) -> str:
    return zip_code.strip()[:5]


def _last_sentence(text: str) -> str:
    parts = _SENTENCE_END.split(text)
    return parts[-1]


def payment_addresses_in_text(text: str) -> frozenset[tuple[str, str]]:
    """(normalised line1, ZIP5) of every address that the text offers for payment only.

    The wording just before the address in its own sentence (and a short tail after it) decides:
    a payment cue with no contest or appeal cue marks it. Mixed or cue-less addresses are not
    marked. Pure: the caller passes the text of official sources only.
    """
    found: set[tuple[str, str]] = set()
    previous_end = 0
    for match in _PAYMENT_ADDRESS.finditer(text):
        start = max(previous_end, match.start() - _CONTEXT_CHARS)
        before = _last_sentence(text[start : match.start()])
        tail = _SENTENCE_END.split(text[match.end() : match.end() + _TAIL_CHARS])[0]
        previous_end = match.end()
        around = f"{before} {tail}"
        if _PAYMENT_CUE.search(around) and not _CONTEST_CUE.search(around):
            found.add((address_line_key(match.group("line1")), match.group("zip")))
    return frozenset(found)


def payment_address_matches(
    address: MailingAddress, known: frozenset[tuple[str, str]] | set[tuple[str, str]]
) -> bool:
    """Same normalised delivery line (incl. PO Box number) and the same ZIP5."""
    return (address_line_key(address.line1), _zip5(address.zip_code)) in known
