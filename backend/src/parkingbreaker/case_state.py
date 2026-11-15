"""Letter (case) lifecycle state machine. Pure: no I/O, no clock, no provider calls.

Follows docs/architecture-v6.md section 8.1. The happy path is linear:

    INTAKE -> RESEARCHING -> DRAFTING -> AWAITING_ADOPTION -> FROZEN
           -> SUBMITTING -> IN_TRANSIT -> DELIVERED

Side branches:

    NEEDS_CUSTOMER     research needs the customer (ladder step 2, inside the window). The
                       answer sends the case back to RESEARCHING.
    AWAITING_ADOPTION  the customer edits their notes -> back to DRAFTING.
    STOPPED_NO_CHARGE  the single no-charge terminal, always with a StopReason. Reachable
                       from INTAKE (nothing authorized, no release), from every timed state
                       before SUBMITTING, and from SUBMITTING when Lob rejected the letter or
                       it was confirmed never sent. Every exit after authorization releases
                       the card hold.
    RETURNED           USPS returned the letter (a confirmed return). The remedy either
                       re-mails it once (attempt 2) or closes it as CLOSED_RETURNED.

Window expiry applies to every state before SUBMITTING. INTAKE is untimed.

Attempts. A case has at most two mailing attempts. Attempt 1 runs on TRANSITIONS. The only
way to start attempt 2 is RemailRequested in RETURNED, which emits SUBMIT_REMAIL (the I/O
layer submits with a new idempotency key). From then on the case runs on
REMAIL_TRANSITIONS, which has no re-mail entry and no payment effects: the re-mail is free,
and a second re-mail is impossible. Callers track the attempt with next_attempt.

Payment status lives in payment_state, not here. Entering IN_TRANSIT on attempt 1 emits
CAPTURE_PAYMENT, and stopping after authorization emits RELEASE_HOLD; the I/O layer turns
those into payment events.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar

from parkingbreaker.state_table import Rule, Table, Transition, allowed, apply


class CaseState(StrEnum):
    INTAKE = "INTAKE"
    RESEARCHING = "RESEARCHING"
    NEEDS_CUSTOMER = "NEEDS_CUSTOMER"
    DRAFTING = "DRAFTING"
    AWAITING_ADOPTION = "AWAITING_ADOPTION"
    FROZEN = "FROZEN"
    SUBMITTING = "SUBMITTING"
    IN_TRANSIT = "IN_TRANSIT"
    DELIVERED = "DELIVERED"
    RETURNED = "RETURNED"
    CLOSED_RETURNED = "CLOSED_RETURNED"
    STOPPED_NO_CHARGE = "STOPPED_NO_CHARGE"


class StopFamily(StrEnum):
    PROCESS_INCOMPATIBLE = "PROCESS_INCOMPATIBLE"
    NOT_YET_VERIFIED = "NOT_YET_VERIFIED"
    BUSINESS_EXCLUSION = "BUSINESS_EXCLUSION"
    TRANSACTION_FAILURE = "TRANSACTION_FAILURE"


class HardStop(StrEnum):
    S1 = "S1"
    S2 = "S2"
    S3 = "S3"
    S4 = "S4"
    S5 = "S5"
    S6 = "S6"
    S7 = "S7"


class StopReason(StrEnum):
    """Why a case ended in STOPPED_NO_CHARGE."""

    PROCESS_INCOMPATIBLE = "PROCESS_INCOMPATIBLE"
    NOT_YET_VERIFIED = "NOT_YET_VERIFIED"
    BUSINESS_EXCLUSION = "BUSINESS_EXCLUSION"
    # Legacy stored reasons; new paths do not emit them.
    UNSUPPORTED = "UNSUPPORTED"
    UNRESOLVABLE_FACT = "UNRESOLVABLE_FACT"
    NOT_FEASIBLE = "NOT_FEASIBLE"
    DECLINED = "DECLINED"
    AUTHORIZATION_EXPIRED = "AUTHORIZATION_EXPIRED"
    DESTINATION_NOT_CONFIRMED = "DESTINATION_NOT_CONFIRMED"
    LETTER_NOT_SUBMITTABLE = "LETTER_NOT_SUBMITTABLE"
    WINDOW_EXPIRED = "WINDOW_EXPIRED"
    ADDRESS_UNDELIVERABLE = "ADDRESS_UNDELIVERABLE"
    LOB_REJECTED = "LOB_REJECTED"
    NOT_SENT = "NOT_SENT"
    DRAFTING_UNAVAILABLE = "DRAFTING_UNAVAILABLE"


def stop_family(reason: StopReason) -> StopFamily:
    if reason in {StopReason.PROCESS_INCOMPATIBLE, StopReason.NOT_FEASIBLE}:
        return StopFamily.PROCESS_INCOMPATIBLE
    if reason in {StopReason.NOT_YET_VERIFIED, StopReason.UNRESOLVABLE_FACT}:
        return StopFamily.NOT_YET_VERIFIED
    if reason in {StopReason.BUSINESS_EXCLUSION, StopReason.UNSUPPORTED}:
        return StopFamily.BUSINESS_EXCLUSION
    return StopFamily.TRANSACTION_FAILURE


TERMINAL_STATES: frozenset[CaseState] = frozenset(
    {CaseState.DELIVERED, CaseState.CLOSED_RETURNED, CaseState.STOPPED_NO_CHARGE}
)

# The session window runs in these states; expiry stops the case without charge.
WINDOWED_STATES: frozenset[CaseState] = frozenset(
    {
        CaseState.RESEARCHING,
        CaseState.NEEDS_CUSTOMER,
        CaseState.DRAFTING,
        CaseState.AWAITING_ADOPTION,
        CaseState.FROZEN,
    }
)

# A letter has been with USPS in these states.
MAILED_STATES: frozenset[CaseState] = frozenset(
    {
        CaseState.IN_TRANSIT,
        CaseState.DELIVERED,
        CaseState.RETURNED,
        CaseState.CLOSED_RETURNED,
    }
)

FIRST_ATTEMPT = 1
REMAIL_ATTEMPT = 2


class Effect(StrEnum):
    """Side effects a transition asks the I/O layer to perform."""

    SUBMIT_LETTER = "SUBMIT_LETTER"
    SUBMIT_REMAIL = "SUBMIT_REMAIL"
    CAPTURE_PAYMENT = "CAPTURE_PAYMENT"
    RELEASE_HOLD = "RELEASE_HOLD"


# --- Events -------------------------------------------------------------------------
# One frozen dataclass per event. Every event that ends in STOPPED_NO_CHARGE carries a
# StopReason so the audit trail says why.


@dataclass(frozen=True)
class PaymentAuthorized:
    """The city is supported and the card hold succeeded at intake."""


@dataclass(frozen=True)
class CustomerNeeded:
    """Research needs an answer from the customer (resolution ladder step 2)."""

    question: str


@dataclass(frozen=True)
class CustomerAnswered:
    """The customer answered; research resumes."""


@dataclass(frozen=True)
class ResearchComplete:
    pass


@dataclass(frozen=True)
class DraftReady:
    """Drafting produced a letter the customer must adopt."""


@dataclass(frozen=True)
class NotesEdited:
    """The customer edited their notes at adoption; the letter is redrafted."""


@dataclass(frozen=True)
class CustomerApproved:
    """The customer adopted the letter (made only by appeal.packet.approval_gate)."""


@dataclass(frozen=True)
class SubmitRequested:
    pass


@dataclass(frozen=True)
class SubmitConfirmed:
    """Lob accepted the letter and returned its id."""

    lob_letter_id: str


@dataclass(frozen=True)
class DeliveryProofStored:
    proof_ref: str


@dataclass(frozen=True)
class ReturnConfirmed:
    """A confirmed return event for the mailed letter, with the USPS return reason."""

    usps_reason: str


@dataclass(frozen=True)
class RemailRequested:
    """The returned-mail remedy chose a free re-mail (attempt 2)."""


@dataclass(frozen=True)
class ReturnClosed:
    """The returned-mail remedy ends the mailing without another letter."""


# Stop events. Their reasons are disjoint, so the reason alone says which path stopped.
_DEDICATED_REASONS = frozenset(
    {
        StopReason.WINDOW_EXPIRED,
        StopReason.LOB_REJECTED,
        StopReason.NOT_SENT,
        StopReason.AUTHORIZATION_EXPIRED,
        StopReason.DESTINATION_NOT_CONFIRMED,
        StopReason.LETTER_NOT_SUBMITTABLE,
    }
)


@dataclass(frozen=True)
class Stopped:
    """A case-specific stop before submission (unsupported, unresolved fact, ...)."""

    reason: StopReason
    detail: str = ""
    hard_stop: HardStop | None = None

    def __post_init__(self) -> None:
        if self.hard_stop is not None and not isinstance(self.hard_stop, HardStop):
            raise ValueError("hard_stop must be a HardStop")
        if self.reason in {StopReason.PROCESS_INCOMPATIBLE, StopReason.NOT_YET_VERIFIED}:
            if self.hard_stop is None:
                raise ValueError(f"{self.reason} requires a hard_stop")
            if self.reason is StopReason.PROCESS_INCOMPATIBLE and not self.detail.strip():
                raise ValueError("PROCESS_INCOMPATIBLE requires an official requirement detail")
        elif self.hard_stop is not None:
            raise ValueError(f"{self.reason} must not carry a hard_stop")
        if self.reason in _DEDICATED_REASONS:
            raise ValueError(f"{self.reason} has its own event")


@dataclass(frozen=True)
class AuthorizationExpired:
    """First mailing create was refused locally because its payment hold expired."""

    reason: ClassVar[StopReason] = StopReason.AUTHORIZATION_EXPIRED


@dataclass(frozen=True)
class TimingRefused:
    """The first create was refused locally because its mailing date became unsafe."""

    reason: ClassVar[StopReason] = StopReason.NOT_FEASIBLE


@dataclass(frozen=True)
class DestinationRefused:
    """The final mailing gate refused the first create locally; Lob was never called."""

    reason: ClassVar[StopReason] = StopReason.DESTINATION_NOT_CONFIRMED


@dataclass(frozen=True)
class LetterNotSubmittable:
    """The first create request was refused locally before any provider call."""

    reason: ClassVar[StopReason] = StopReason.LETTER_NOT_SUBMITTABLE


@dataclass(frozen=True)
class WindowExpired:
    """The session window ran out before submission."""

    reason: ClassVar[StopReason] = StopReason.WINDOW_EXPIRED


@dataclass(frozen=True)
class SubmitRejected:
    """Lob definitively rejected the letter."""

    detail: str
    reason: ClassVar[StopReason] = StopReason.LOB_REJECTED


@dataclass(frozen=True)
class ConfirmedNotSent:
    """Lob confirms the letter never existed, or was canceled, deleted or failed.

    The I/O layer emits this only on that confirmation (never for a letter Lob may still
    print and mail); it is the sole way SUBMITTING releases the hold after a create.
    """

    detail: str
    reason: ClassVar[StopReason] = StopReason.NOT_SENT


StopEvent = (
    Stopped
    | WindowExpired
    | SubmitRejected
    | ConfirmedNotSent
    | AuthorizationExpired
    | TimingRefused
    | DestinationRefused
    | LetterNotSubmittable
)

Event = (
    PaymentAuthorized
    | CustomerNeeded
    | CustomerAnswered
    | ResearchComplete
    | DraftReady
    | NotesEdited
    | CustomerApproved
    | SubmitRequested
    | SubmitConfirmed
    | DeliveryProofStored
    | ReturnConfirmed
    | RemailRequested
    | ReturnClosed
    | Stopped
    | WindowExpired
    | AuthorizationExpired
    | TimingRefused
    | DestinationRefused
    | LetterNotSubmittable
    | SubmitRejected
    | ConfirmedNotSent
)

S = CaseState
E = Effect
_STOP = Rule(S.STOPPED_NO_CHARGE, (E.RELEASE_HOLD,))

TRANSITIONS: Table[CaseState, Event, Effect] = {
    # Intake: nothing is authorized yet, so a stop releases nothing.
    (S.INTAKE, PaymentAuthorized): Rule(S.RESEARCHING),
    (S.INTAKE, Stopped): Rule(S.STOPPED_NO_CHARGE),
    # Research and the customer loop
    (S.RESEARCHING, CustomerNeeded): Rule(S.NEEDS_CUSTOMER),
    (S.NEEDS_CUSTOMER, CustomerAnswered): Rule(S.RESEARCHING),
    (S.RESEARCHING, ResearchComplete): Rule(S.DRAFTING),
    # Drafting and adoption
    (S.DRAFTING, DraftReady): Rule(S.AWAITING_ADOPTION),
    (S.AWAITING_ADOPTION, NotesEdited): Rule(S.DRAFTING),
    (S.AWAITING_ADOPTION, CustomerApproved): Rule(S.FROZEN),
    # Submission
    (S.FROZEN, SubmitRequested): Rule(S.SUBMITTING, (E.SUBMIT_LETTER,)),
    (S.SUBMITTING, SubmitConfirmed): Rule(S.IN_TRANSIT, (E.CAPTURE_PAYMENT,)),
    (S.FROZEN, AuthorizationExpired): _STOP,
    (S.SUBMITTING, AuthorizationExpired): _STOP,
    (S.SUBMITTING, TimingRefused): _STOP,
    (S.SUBMITTING, DestinationRefused): _STOP,
    (S.SUBMITTING, LetterNotSubmittable): _STOP,
    (S.SUBMITTING, SubmitRejected): _STOP,
    (S.SUBMITTING, ConfirmedNotSent): _STOP,
    # Mailing outcome
    (S.IN_TRANSIT, DeliveryProofStored): Rule(S.DELIVERED),
    (S.IN_TRANSIT, ReturnConfirmed): Rule(S.RETURNED),
    (S.RETURNED, RemailRequested): Rule(S.SUBMITTING, (E.SUBMIT_REMAIL,)),
    (S.RETURNED, ReturnClosed): Rule(S.CLOSED_RETURNED),
    # Stops before submission: every windowed state accepts both kinds.
    **{(state, Stopped): _STOP for state in WINDOWED_STATES},
    **{(state, WindowExpired): _STOP for state in WINDOWED_STATES},
}

# Attempt 2: a free re-mail of a letter already paid for. No payment effects, and no way
# back to SUBMITTING. If the re-mail itself fails, the mailing ends as returned.
REMAIL_TRANSITIONS: Table[CaseState, Event, Effect] = {
    (S.SUBMITTING, SubmitConfirmed): Rule(S.IN_TRANSIT),
    (S.SUBMITTING, SubmitRejected): Rule(S.CLOSED_RETURNED),
    (S.SUBMITTING, ConfirmedNotSent): Rule(S.CLOSED_RETURNED),
    (S.IN_TRANSIT, DeliveryProofStored): Rule(S.DELIVERED),
    (S.IN_TRANSIT, ReturnConfirmed): Rule(S.RETURNED),
    (S.RETURNED, ReturnClosed): Rule(S.CLOSED_RETURNED),
}

del S, E

_TABLES: Mapping[int, Table[CaseState, Event, Effect]] = {
    FIRST_ATTEMPT: TRANSITIONS,
    REMAIL_ATTEMPT: REMAIL_TRANSITIONS,
}


def table_for(attempt: int) -> Table[CaseState, Event, Effect]:
    """The transition table for mailing attempt 1 or 2."""
    try:
        return _TABLES[attempt]
    except KeyError:
        raise ValueError(f"attempt must be 1 or 2, not {attempt}") from None


def transition(
    state: CaseState, event: Event, attempt: int = FIRST_ATTEMPT
) -> Transition[CaseState, Event, Effect]:
    """Return the transition for `event` in `state`, or raise IllegalTransition."""
    return apply(table_for(attempt), state, event)


def next_attempt(attempt: int, t: Transition[CaseState, Event, Effect]) -> int:
    """The attempt after `t`: 2 once the re-mail is submitted, otherwise unchanged."""
    return REMAIL_ATTEMPT if Effect.SUBMIT_REMAIL in t.effects else attempt


def allowed_events(state: CaseState, attempt: int = FIRST_ATTEMPT) -> frozenset[type[Event]]:
    """Event types accepted in `state`."""
    return allowed(table_for(attempt), state)
