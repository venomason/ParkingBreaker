"""Lob submission decisions (design v6 section 6.3). Pure: no I/O, no clock, no Lob SDK.

    create key for (case, attempt) and persist it
    send letter with that key  -> ACCEPTED(letter_id)   -> IN_TRANSIT, request capture
                                  (production/mail-stream evidence only; a created or
                                  rendered letter is recorded but is never ACCEPTED)
                               -> REJECTED (4xx)        -> STOPPED_NO_CHARGE, release hold
                               -> UNCERTAIN (timeout, 5xx, connection lost)
                                    retry same key, same request, with backoff
                                    near the end of the idempotency window:
                                    look up by metadata token -> ACCEPTED or NOT_SENT

The provider's idempotency guarantees a single letter, so every retry of one attempt sends
the same key. The hold is never released while the outcome is uncertain. The I/O layer owns
the clock, the stored key and the LobClient; it passes values in and acts on the result.

CONFIRM AT BUILD: idempotency window length, metadata list filter, webhook event names.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from parkingbreaker.agencies import MailingAddress
from parkingbreaker.mail_config import MailClass

# Resolve the no-scan fallback six hours before the payment safety margin, leaving
# time for the existing capture/reconciliation path (§20.19).
FALLBACK_LEAD = timedelta(hours=6)

# --- Keys -------------------------------------------------------------------------------


def idempotency_key(case_token: str, attempt: int) -> str:
    """Deterministic key for one mailing attempt of one case. Carries no personal data."""
    if not case_token:
        raise ValueError("case_token must be non-empty")
    if attempt < 1:
        raise ValueError("attempt starts at 1")
    digest = hashlib.sha256(f"pb-lob:{case_token}:{attempt}".encode()).hexdigest()
    return f"pb-{digest[:40]}"


# --- Outcomes ---------------------------------------------------------------------------


class LetterEvidence(StrEnum):
    """How far the provider has shown the exact original letter has progressed.

    PRODUCTION permits scan-triggered capture. EXPIRY_FALLBACK records the distinct,
    current-object exception at the hold margin (rendered and past its cancellation
    window). A cancellation refusal alone remains uncertain and cannot permit capture.
    Rendering alone never permits capture. VOIDED means Lob shows this exact letter failed or was
    deleted, with no mail-stream evidence: it will never be mailed.
    """

    CREATED = "created"  # provider has the object (processed/pending) but has not rendered it
    RENDERED = "rendered"  # provider rendered the PDF; this is not proof it was mailed
    PRODUCTION = "production"  # provider-defined production/mail-stream evidence (piece B)
    EXPIRY_FALLBACK = "expiry_fallback"  # current rendered object, cancellation window passed
    VOIDED = "voided"  # provider shows the exact letter failed or deleted: never mailed


@dataclass(frozen=True)
class UspsScan:
    """One provider tracking event kept exactly as sent (§20.19, 2026-10-22).

    Only the provider's own fields are retained: the event type and name, the USPS
    time and the details object. Nothing is invented or re-dated, so the customer's
    delivery report can show the actual scans.
    """

    event_type: str
    name: str | None = None
    time: str | None = None
    details: Mapping[str, object] | None = None

    def as_sent(self) -> dict[str, object]:
        """The provider's fields only, in one JSON-stable record."""
        record: dict[str, object] = {"type": self.event_type}
        if self.name is not None:
            record["name"] = self.name
        if self.time is not None:
            record["time"] = self.time
        if self.details is not None:
            record["details"] = dict(self.details)
        return record


@dataclass(frozen=True)
class Accepted:
    letter_id: str
    tracking_number: str | None = None
    # None is reserved for the old codec, which recorded rendering as acceptance.
    # Explicit constructors are trusted lifecycle fixtures; adapters must prove evidence.
    evidence: LetterEvidence | None = LetterEvidence.PRODUCTION
    # USPS scan proof for this exact attempt, from webhook or lookup acceptance.
    scans: tuple[UspsScan, ...] = ()


@dataclass(frozen=True)
class Rejected:
    """Definitive validation failure: no letter exists for this key."""

    status: int
    reason: str = ""


@dataclass(frozen=True)
class Uncertain:
    """Timeout, 5xx, connection lost or similar: a letter may or may not exist.

    `letter_id` anchors the exact original letter once a create response has shown it, so
    recovery never adopts another letter and the same key is never replaced. `rendered`
    records that the provider reported it rendered; neither field makes the outcome
    capture-eligible and the hold stays.
    """

    reason: str
    letter_id: str | None = None
    rendered: bool = False
    tracking_number: str | None = None


@dataclass(frozen=True)
class NotSent:
    """The metadata lookup near the end of the idempotency window found nothing."""


@dataclass(frozen=True)
class Canceled:
    """Lob confirmed the exact original letter was canceled, deleted or failed.

    It will never be mailed, so (and only so) the hold may be released. Anything short of
    that confirmation, including an unanswered cancel request, is Uncertain.
    """

    letter_id: str


SubmissionOutcome = Accepted | Rejected | Uncertain | NotSent | Canceled


def pending_evidence(outcome: SubmissionOutcome) -> bool:
    """Old accepted encodings need fresh evidence, just like created/rendered letters."""
    return isinstance(outcome, Uncertain) or (
        isinstance(outcome, Accepted) and outcome.evidence is None
    )


# 4xx codes that do not prove the request was refused: retry with the same key.
NON_DEFINITIVE_4XX: frozenset[int] = frozenset({408, 409, 425, 429})


@dataclass(frozen=True)
class LobResponse:
    """What came back from one create request. status None means no HTTP response."""

    status: int | None
    letter_id: str | None = None
    error: str = ""
    tracking_number: str | None = None  # certified mail only
    # Fail closed: a response that does not explicitly carry PRODUCTION evidence can never
    # become Accepted (and so can never trigger payment capture). Lifecycle fixtures that
    # stand in for an already-proven provider must say PRODUCTION explicitly.
    evidence: LetterEvidence = LetterEvidence.CREATED
    # The request was refused locally, before any provider call (definite non-send).
    local_refusal: bool = False
    # The provider's own tracking events for this letter, kept as sent.
    scans: tuple[UspsScan, ...] = ()


# Fixed reason for a request refused locally, before any provider call (definite non-send).
LOCAL_REFUSAL_REASON = "LOCAL_REQUEST_REFUSED"
TIMING_REFUSAL_REASON = "LOCAL_TIMING_REFUSED"
# The unpaid re-mail guard's own fixed refusal code. It must survive classify_response
# so the customer copy can say the original payment was never captured (H2).
UNPAID_REMAIL_REASON = "ORIGINAL_PAYMENT_NOT_CAPTURED"


def classify_response(response: LobResponse) -> SubmissionOutcome:
    status = response.status
    if response.local_refusal:
        # A local refusal keeps its own fixed service reason; anything else is the
        # generic definite non-send code.
        reason = (
            response.error
            if response.error in {UNPAID_REMAIL_REASON, TIMING_REFUSAL_REASON}
            else LOCAL_REFUSAL_REASON
        )
        return Rejected(422, reason)
    if status is None:
        return Uncertain(response.error or "no response")
    if 200 <= status < 300:
        if response.letter_id:
            if response.evidence is not LetterEvidence.PRODUCTION:
                return Uncertain(
                    f"letter {response.evidence.value}; production evidence not established",
                    response.letter_id,
                    response.evidence is LetterEvidence.RENDERED,
                    response.tracking_number,
                )
            return Accepted(response.letter_id, response.tracking_number, scans=response.scans)
        return Uncertain(f"{status} without letter id")
    if 400 <= status < 500 and status not in NON_DEFINITIVE_4XX:
        return Rejected(status, response.error)
    return Uncertain(f"http {status}")


def reconcile_with_original(
    previous: SubmissionOutcome | None, outcome: SubmissionOutcome
) -> SubmissionOutcome:
    """Keep the first recorded original letter id and refuse any other letter.

    A create retry or lookup that shows another id, or an absence/rejection after a letter
    was already recorded, cannot be adopted: it stays uncertain with the original anchor.
    """
    if not isinstance(previous, Uncertain) or previous.letter_id is None:
        return outcome
    anchor = previous.letter_id
    rendered = previous.rendered
    tracking = previous.tracking_number
    if isinstance(outcome, Uncertain):
        if outcome.letter_id is None:
            return Uncertain(outcome.reason, anchor, rendered, tracking)
        if outcome.letter_id != anchor:
            return Uncertain(
                "letter differs from the original create response", anchor, rendered, tracking
            )
        return Uncertain(
            outcome.reason,
            anchor,
            rendered or outcome.rendered,
            outcome.tracking_number or tracking,
        )
    if isinstance(outcome, Accepted | Canceled) and outcome.letter_id == anchor:
        return outcome
    return Uncertain(
        "outcome conflicts with the original create response", anchor, rendered, tracking
    )


def hold_releasable(outcome: SubmissionOutcome) -> bool:
    """Only a definite non-send (never created, or confirmed canceled/failed) frees the hold."""
    return isinstance(outcome, Rejected | NotSent | Canceled)


# --- Retry schedule ---------------------------------------------------------------------


@dataclass(frozen=True)
class RetrySameKey:
    at: datetime


@dataclass(frozen=True)
class LookupByMetadata:
    pass


NextAction = RetrySameKey | LookupByMetadata


@dataclass(frozen=True)
class RetryPolicy:
    base_delay: timedelta = timedelta(seconds=30)
    max_delay: timedelta = timedelta(minutes=30)
    lookup_margin: timedelta = timedelta(hours=1)

    def __post_init__(self) -> None:
        if self.base_delay <= timedelta(0) or self.max_delay < self.base_delay:
            raise ValueError("need 0 < base_delay <= max_delay")
        if self.lookup_margin < timedelta(0):
            raise ValueError("lookup_margin must be >= 0")


DEFAULT_RETRY_POLICY = RetryPolicy()
# Verified contract in architecture-v6 implementation note 12.
VERIFIED_IDEMPOTENCY_WINDOW = timedelta(hours=24)


def create_allowed(first_sent_at: datetime, now: datetime) -> bool:
    """No create at/after the conservative lookup cutoff, even following a pause."""
    if now < first_sent_at:
        raise ValueError("now is before first_sent_at")
    return now < first_sent_at + VERIFIED_IDEMPOTENCY_WINDOW - DEFAULT_RETRY_POLICY.lookup_margin


def backoff(attempts: int, policy: RetryPolicy = DEFAULT_RETRY_POLICY) -> timedelta:
    """Delay after `attempts` uncertain requests: doubling from base, capped."""
    if attempts < 1:
        raise ValueError("attempts starts at 1")
    exponent = min(attempts - 1, 30)
    return min(policy.base_delay * (1 << exponent), policy.max_delay)


def next_action(
    first_sent_at: datetime,
    now: datetime,
    attempts: int,
    idempotency_window: timedelta,
    policy: RetryPolicy = DEFAULT_RETRY_POLICY,
) -> NextAction:
    """After an uncertain outcome: retry with the same key, or look up before the window ends.

    `attempts` counts requests already sent for this key (at least 1).
    """
    if now < first_sent_at:
        raise ValueError("now is before first_sent_at")
    if policy.lookup_margin >= idempotency_window:
        raise ValueError("lookup_margin must be shorter than the idempotency window")
    at = now + backoff(attempts, policy)
    lookup_from = first_sent_at + idempotency_window - policy.lookup_margin
    if at >= lookup_from:
        return LookupByMetadata()
    return RetrySameKey(at)


# --- Webhook gate -----------------------------------------------------------------------


class WebhookAction(StrEnum):
    APPLY = "APPLY"  # newer than the current state: move forward
    RECORD_ONLY = "RECORD_ONLY"  # same or older: store, never move backwards
    DUPLICATE = "DUPLICATE"  # event id already seen: ignore


@dataclass(frozen=True)
class WebhookDecision:
    action: WebhookAction
    seen_event_ids: frozenset[str]


def apply_event(
    current_rank: int, event_rank: int, seen_event_ids: frozenset[str], event_id: str
) -> WebhookDecision:
    """Dedup by event id, then forward-only by rank. Signature checks happen before this."""
    if not event_id:
        raise ValueError("event_id must be non-empty")
    if event_id in seen_event_ids:
        return WebhookDecision(WebhookAction.DUPLICATE, seen_event_ids)
    seen = seen_event_ids | {event_id}
    if event_rank > current_rank:
        return WebhookDecision(WebhookAction.APPLY, seen)
    return WebhookDecision(WebhookAction.RECORD_ONLY, seen)


# --- Provider interface -----------------------------------------------------------------


@dataclass(frozen=True)
class LetterOptions:
    """Frozen mail choices for a letter request (v6 section 6.3)."""

    mail_class: MailClass
    color: bool = False
    double_sided: bool = False
    return_receipt: bool = False


@dataclass(frozen=True)
class LetterRequest:
    """The frozen document, envelope and mail choices for every retry (v6 section 6.3)."""

    pdf: bytes
    submission_token: str  # random, sent as metadata; never personal data
    sender: MailingAddress
    recipient: MailingAddress
    options: LetterOptions


@dataclass(frozen=True)
class MetadataLookup:
    """Successful lookup; absence is definitive only if authoritative is True.

    A letter_id asserts exactly one matching letter over the complete bounded search.
    The adapter must establish completeness and exclude delayed/in-flight creates
    before asserting authoritative absence. An ordinary empty list is insufficient.
    """

    letter_id: str | None = None
    authoritative: bool = False
    evidence: LetterEvidence = LetterEvidence.CREATED
    # Lob Letter.send_date (lob-python 5.2.1 model/letter.py): cancellable until passed.
    # The adapter supplies this only for a current, rendered, nonfailed/nondeleted object.
    send_date: datetime | None = None
    tracking_number: str | None = None
    # The provider's own tracking events for this exact letter, kept as sent.
    scans: tuple[UspsScan, ...] = ()


def resolve_lookup(
    found: str | MetadataLookup | None, original_letter_id: str | None = None
) -> SubmissionOutcome:
    """An incomplete/legacy empty lookup never proves a letter was not sent.

    The adapter must return exactly one token match, across all pages. The unique random
    per-attempt token identifies the frozen request after a lost create response; refusing
    that recovery would strand an already mailed letter. Once recorded, its id is the
    permanent anchor. A bare string supplies neither identity verification nor evidence.
    """
    found_id = found.letter_id if isinstance(found, MetadataLookup) else found
    if (
        original_letter_id is not None
        and isinstance(found_id, str)
        and found_id.strip()
        and found_id != original_letter_id
    ):
        return Uncertain(
            "lookup letter differs from the original create response", original_letter_id
        )
    if isinstance(found, MetadataLookup):
        if type(found.authoritative) is not bool:
            return Uncertain("invalid lookup completeness")
        if isinstance(found.letter_id, str) and found.letter_id.strip():
            if found.evidence is not LetterEvidence.PRODUCTION:
                anchored = original_letter_id is not None
                reason = (
                    f"lookup found letter {found.evidence.value}; no production evidence"
                    if anchored
                    else "lookup found a letter not anchored to the original create response"
                )
                return Uncertain(
                    reason,
                    original_letter_id,
                    anchored and found.evidence is LetterEvidence.RENDERED,
                )
            return Accepted(found.letter_id, found.tracking_number, scans=found.scans)
        if found.letter_id is None and found.authoritative:
            return NotSent()
        return Uncertain("lookup did not establish authoritative absence")
    return Uncertain("legacy lookup did not establish authoritative absence")


class CancelOutcome(StrEnum):
    """Lob's answer to a request to cancel one exact letter."""

    CANCELED = "canceled"  # Lob confirmed the letter was removed from production
    NOT_CANCELABLE = "not_cancelable"  # Lob refused: the letter is no longer cancelable
    UNKNOWN = "unknown"  # error, timeout or unrecognised answer


@dataclass(frozen=True)
class CancelRequest:
    """The fallback decided this exact letter must be canceled before the hold can move."""

    letter_id: str


def fallback_resolution(
    found: str | MetadataLookup | None,
    original_id: str | None,
    now: datetime,
    last_safe_capture: datetime,
) -> SubmissionOutcome | CancelRequest:
    """No-scan fallback decision from one fresh lookup (section 20.19, as amended).

    Accepted: a USPS scan, or a rendered letter past its cancellation window; capture.
    Canceled/NotSent: Lob shows the letter failed/deleted or never existed; release.
    CancelRequest: a created or rendered letter that may still be mailed; the caller must
    ask Lob to cancel it and decide from the answer. Anything else stays Uncertain and
    keeps the hold. Past `last_safe_capture` a letter that has certainly entered the mail
    stream is still Accepted, never released: the payment machine owns the late capture.
    """
    resolved = resolve_lookup(found, original_id)
    if not isinstance(found, MetadataLookup) or not isinstance(resolved, Uncertain):
        return resolved
    letter_id = found.letter_id
    if not isinstance(letter_id, str) or not letter_id.strip():
        return resolved
    if original_id is not None and letter_id != original_id:
        return resolved
    if found.evidence is LetterEvidence.VOIDED:
        return Canceled(letter_id)
    if found.evidence is LetterEvidence.RENDERED:
        if (
            found.send_date is not None
            and found.send_date.tzinfo is not None
            and found.send_date < now
            and now.tzinfo is not None
        ):
            # TODO: Carry the observed send_date and lookup time into the saved fallback outcome.
            # Recovery should be able to explain this decision after the Lob object changes.
            return Accepted(letter_id, found.tracking_number, LetterEvidence.EXPIRY_FALLBACK)
        return CancelRequest(letter_id)
    if found.evidence is LetterEvidence.CREATED:
        return CancelRequest(letter_id)
    return resolved


def cancel_resolution(
    result: CancelOutcome,
    letter_id: str,
    tracking_number: str | None = None,
) -> SubmissionOutcome:
    """Turn Lob's answer to a cancel request into an outcome. Only confirmation releases.

    A refusal alone never establishes production entry, regardless of the error's
    shape or text (H3). Corroborating exact-letter scans and a passed send_date are
    evaluated by fallback_resolution before cancellation. Otherwise keep the hold
    and retry the existing lookup/cancel schedule.
    """
    if result is CancelOutcome.CANCELED:
        return Canceled(letter_id)
    return Uncertain("cancellation outcome not confirmed", letter_id, False, tracking_number)


class LobClient(Protocol):
    def create_letter(self, request: LetterRequest, idempotency_key: str) -> LobResponse:
        """Send one create request. Timeouts and lost connections return status None."""
        ...

    def find_by_metadata(self, submission_token: str) -> str | MetadataLookup | None:
        """Find the token; only an explicit authoritative result can establish absence."""
        ...


class CancelableLobClient(LobClient, Protocol):
    """The seam the submission fallback needs: exact-letter retrieval and cancellation."""

    def lookup_letter(self, letter_id: str, submission_token: str) -> MetadataLookup:
        """Retrieve the one known original letter by id, including a failed/deleted one."""
        ...

    def cancel_letter(self, letter_id: str) -> CancelOutcome:
        """Ask Lob to cancel exactly this letter; only an explicit confirmation is CANCELED."""
        ...
