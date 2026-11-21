"""The real CaseServices (Lane C), built job by job on the transaction store.

Workspaces, intake, authorization, research, drafting, adoption and approval use
the existing production rules. Mailing and tracking are wired by the next job.
Providers, clock and ids are injected; nothing reads the
environment here.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import io
import json
import logging
import os
import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from itertools import chain
from typing import Any, Literal
from uuid import uuid4

from pydantic import TypeAdapter
from pypdf import PdfReader

from parkingbreaker import case_state, prices
from parkingbreaker.address_disclosure import DISCLOSURE_SHA256, DISCLOSURE_TEXT, DISCLOSURE_VERSION
from parkingbreaker.address_policy import (
    AddressProvenance,
    ConsentEvent,
    DestinationDecision,
    DestinationFacts,
    Disclosure,
    EvidenceKind,
    EvidenceRef,
    FailureFacts,
    OutcomeKind,
    ResearchClaim,
    ResearchVerification,
    Responsibility,
    address_fingerprint,
    address_is_complete,
    classify_address_failure,
    decide_destination,
    mailing_gate,
    payment_address_matches,
    payment_addresses_in_text,
    remedy_for_responsibility,
)
from parkingbreaker.agencies import MailingAddress
from parkingbreaker.api import schemas as s
from parkingbreaker.api.service import ServiceConflict, SweepFailure, SweepResult, store_upload
from parkingbreaker.appeal.adoption import FINAL_SCREEN_TEXT, Adoption, adopt, text_sha256
from parkingbreaker.appeal.correction import Flag, TextSpan, edited_spans, flags_after_edit
from parkingbreaker.appeal.drafting import (
    Draft,
    Drafter,
    DraftResult,
    DraftSentence,
    LlmDrafter,
    TicketFacts,
    customer_name_sensitive_strings,
)
from parkingbreaker.appeal.letter import (
    LetterError,
    LetterInput,
    Sender,
    is_printable,
    redirect_letter_address,
    render_letter,
)
from parkingbreaker.appeal.packet import (
    ApprovalRefused,
    Packet,
    PacketError,
    approval_gate,
    build_packet,
    frozen_signature_artwork,
)
from parkingbreaker.appeal.references import Reference
from parkingbreaker.appeal.required_copies import (
    RequiredCopyPages,
    required_copy_items,
    validated_copy_pages,
)
from parkingbreaker.appeal.run import DraftingStatus, run_drafting
from parkingbreaker.approval_manifest import (
    ApprovalIntegrityError,
    ApprovalManifest,
    build_approval_manifest,
    build_remail_approval_manifest,
    verify_dispatch_integrity,
    verify_remail_approval,
)
from parkingbreaker.beta_feedback_store import (
    BetaFeedbackConflict,
    NoticePage,
    NoticeReceipt,
)
from parkingbreaker.beta_store import (
    BETA_DISCLOSURE_SHA256,
    BETA_DISCLOSURE_TEXT,
    BETA_DISCLOSURE_VERSION,
    BetaConflict,
    BetaEntitlement,
)
from parkingbreaker.blob_store import BlobStore, photo_ref
from parkingbreaker.case_hold_store import (
    CaseHold,
    CommandOutcome,
    HoldCommand,
    HoldReason,
)
from parkingbreaker.case_state import (
    TERMINAL_STATES,
    CaseState,
    CustomerAnswered,
    CustomerApproved,
    CustomerNeeded,
    DraftReady,
    Event,
    HardStop,
    ResearchComplete,
    StopFamily,
    Stopped,
    StopReason,
    WindowExpired,
    stop_family,
)
from parkingbreaker.declarations import (
    DeclarationError,
    affirmation_map,
    declarations_for,
    unaffirmed_required,
    unknown_affirmations,
)
from parkingbreaker.disputes import DISPUTE_EVENTS, FRAUD_WARNING_EVENTS, dispute_fact
from parkingbreaker.draft_ownership_store import (
    DraftCallsExhausted,
    DraftOutcome,
    DraftRunRefused,
)
from parkingbreaker.effect_dispatch import dispatch_once
from parkingbreaker.intake_pause_store import IntakePauseConflict
from parkingbreaker.intake_store import (
    IntakeConflict,
    IntakeRecord,
    SavedDraft,
    SavedResearch,
    authorization_dispatch_refusal,
    authorization_request_json,
)
from parkingbreaker.llm import LlmBudgetExceeded, LlmProviderError, Usage
from parkingbreaker.lob_run import production_date
from parkingbreaker.lob_submission import (
    Accepted,
    CancelableLobClient,
    Canceled,
    CancelOutcome,
    LetterOptions,
    LetterRequest,
    LobResponse,
    MetadataLookup,
    NotSent,
    Rejected,
    Uncertain,
    pending_evidence,
)
from parkingbreaker.lob_webhook import (
    FAILURE_WEBHOOKS,
    failure_webhook_qualifies,
    scan_proof,
    scan_webhook_qualifies,
    usps_return_reason,
)
from parkingbreaker.lob_webhook import verify_webhook as verify_lob_webhook
from parkingbreaker.mail_attempt_store import (
    PreparationConflict,
    PreparedMailAttempt,
    StorageCorruption,
)
from parkingbreaker.mail_config import MailClass
from parkingbreaker.mail_request import envelope_sha256, validate_request
from parkingbreaker.mail_timing import DeadlineRule, latest_safe_send_date
from parkingbreaker.oplog.ai_journal import AiCall, AiCallStatus, AiJournalEntry, AiNote, AiWorker
from parkingbreaker.payment_records import PaymentClient, mutation_key
from parkingbreaker.payment_state import EXPIRY_MARGIN, PaymentState
from parkingbreaker.prehold import (
    PROVIDER_ERRORS,
    PreholdDeps,
    PreholdRequest,
    PreholdResult,
    run_prehold,
)
from parkingbreaker.prices import CERTIFIED_CENTS, STANDARD_CENTS
from parkingbreaker.research.extractor import Extractor
from parkingbreaker.research.findings import FindingStatus, verify_quote
from parkingbreaker.research.fulfilment import complete_required_forms, verified_findings
from parkingbreaker.research.jurisdiction import IssuerQuery
from parkingbreaker.research.known_sources import known_seeds
from parkingbreaker.research.ladder import CustomerQuestion
from parkingbreaker.research.questions import RETIRED_KEYS, FactKey, PacketPlan, Ticket
from parkingbreaker.research.resolution import (
    ResearchAnswers,
    ResearchResolution,
    VehicleRelationship,
    answer_question,
    resolution_event,
)
from parkingbreaker.research.run import (
    RESEARCH_REUSE_WINDOW,
    RecoveryConfig,
    ResearchResult,
    reuse_research,
    run_research,
)
from parkingbreaker.research.search import Searcher
from parkingbreaker.research.sources import AuthorityClass, Fetcher
from parkingbreaker.research.workflow import unsupported_workflow_requirements
from parkingbreaker.research_records import (
    agency_json,
    answers_json,
    decode_agency,
    decode_answers,
    decode_research,
    research_json,
)
from parkingbreaker.returned_mail import (
    RemedyKind,
    ReturnCause,
    classify_return,
    estimated_mailing_cost,
)
from parkingbreaker.service_phase import ServicePhase
from parkingbreaker.service_policy import (
    LAUNCH_STANDARD_OFFERED,
    IntakeStop,
    IntakeSupport,
    IssuerKind,
    intake_customer_stop,
)
from parkingbreaker.session_window import (
    DEFAULT_WINDOW,
    WindowStatus,
    applies_to,
    remaining,
    seconds_remaining,
    window_status,
)
from parkingbreaker.state_table import IllegalTransition
from parkingbreaker.stop_reasons import stop_message
from parkingbreaker.stripe_payment import verify_webhook as verify_stripe_webhook
from parkingbreaker.sweep_run_store import SweepRunRecorder
from parkingbreaker.ticket_intake_service import (
    TicketIntakeCoordinator,
    TicketIntakeServiceError,
)
from parkingbreaker.transaction_records import CaseRecord
from parkingbreaker.transaction_store import (
    GATE_REFUSED_REASON,
    ReturnedMailResolution,
    RevisionConflict,
    SqliteTransactionStore,
)
from parkingbreaker.tricks import SavedPage, Trick, hunt
from parkingbreaker.webhook_backlog_store import (
    CANCEL_CONFIRMED,
    CONFLICTING_LETTER,
    CONTRADICTS_CASE_STATE,
    UNMATCHED_EXPIRED,
    BacklogRow,
    WebhookEnvelopeConflict,
)
from parkingbreaker.workspace import Workspace, restart_prefill

LOG = logging.getLogger(__name__)
# T74: escalate a held case this long before the card authorization's expiry stop.
REVIEW_LEAD = timedelta(hours=2)
# Lob's own ids are a prefix and a lowercase token; anything else (capitals, separators,
# free text) is logged only as a short hash, so names, plates and citation numbers never are.
_LOB_LOG_ID = re.compile(r"(?:evt|ltr)_[a-z0-9]{1,40}\Z")


def _log_id(value: str) -> str:
    """A Lob event or letter id as is; anything else only as a short hash (K04)."""
    if _LOB_LOG_ID.fullmatch(value):
        return value
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()[:12]


# Card holds last about 7 days; Stripe's capture_before wins when it is reported.
AUTHORIZATION_VALIDITY = timedelta(days=6)
# Stripe may prune at 24h; 1h covers 80s SDK timeout + clock skew so replays reach retained keys.
AUTHORIZATION_REPLAY_LIMIT = timedelta(hours=23)
# One sweep replays at most this many parked Lob events in fair persistent order.
WEBHOOK_REPLAY_BATCH = 100
# A parked event no case claims within this time gets its final outcome: the unmatched record.
WEBHOOK_UNMATCHED_AFTER = timedelta(days=30)
# How long a returned letter waits for an authoritative actual mailing cost before the
# documented expected cost is used as a flagged ESTIMATE. Bounded: never blocks forever.
RETURN_COST_EVIDENCE_WINDOW = timedelta(days=14)
_PREHOLD = TypeAdapter[s.PreHoldResult](s.PreHoldResult)
_COPY_SELECTIONS = TypeAdapter(list[s.RequiredCopySelection])
_FAILURE_FACTS = TypeAdapter(FailureFacts)
ADDRESS_REFUND_POLICY_VERSION = "address-responsibility-v1"
# The recorded Stopped detail for a customer's decline of the customer-directed destination.
DESTINATION_DECLINED = "DESTINATION_DECLINED"
# States where a pending destination choice can be made: before any draft exists.
_CHOICE_STATES = frozenset({CaseState.RESEARCHING, CaseState.NEEDS_CUSTOMER, CaseState.DRAFTING})
_DECLINE_STATES = _CHOICE_STATES | {CaseState.AWAITING_ADOPTION}
CERTIFIED_OFFER = s.TierOffer(
    tier=s.MailTier.CERTIFIED,
    amount_cents=CERTIFIED_CENTS,
    description="Certified mail with tracking; no promised arrival date.",
)
STANDARD_OFFER = s.TierOffer(
    tier=s.MailTier.STANDARD,
    amount_cents=STANDARD_CENTS,
    description="First-class mail; no arrival guarantee.",
)


@dataclass(frozen=True)
class ResearchDeps:
    fetcher: Fetcher
    extractor: Extractor
    searcher: Searcher | None
    wait: Callable[[float], None]
    recovery: RecoveryConfig | None = field(default_factory=RecoveryConfig)
    budget_cents: int = 50
    # Verified starting pages (2.x city files); every page is still fetched and guarded live.
    known_records: tuple[Mapping[str, object], ...] = ()
    # The configured model preset or id, recorded with each run's AI call log entry.
    model_label: str = "unrecorded"
    # The Book of Tricks the hunter looks for on each run's official pages (may be empty).
    tricks: tuple[Trick, ...] = ()


_SPANS = TypeAdapter(tuple[TextSpan, ...])
_DRAFT_RESULT = TypeAdapter(DraftResult)
_FLAGS = TypeAdapter(tuple[Flag, ...])


VEHICLE_QUESTION = s.ClosedQuestion(
    prompt="What is your relationship to the vehicle?", choices=list(VehicleRelationship)
)
_PAYMENT_QUESTIONS = {
    "PAID_FINE": s.PaymentClosedQuestion(
        question_id="PAID_FINE",
        prompt="Have you paid the fine for this citation?",
        choices=[True, False, None],
    ),
    "PAID_HEARING_DEPOSIT": s.PaymentClosedQuestion(
        question_id="PAID_HEARING_DEPOSIT",
        prompt="Have you paid the agency's hearing deposit for this citation?",
        choices=[True, False, None],
    ),
    "ENTERED_PLAN": s.PaymentClosedQuestion(
        question_id="ENTERED_PLAN",
        prompt="Have you enrolled this citation in a payment plan?",
        choices=[True, False, None],
    ),
}
_VISIBLE_QUESTIONS = {"VEHICLE_RELATIONSHIP", *_PAYMENT_QUESTIONS}


def _address_lines(address: MailingAddress) -> list[str]:
    return [
        line
        for line in (
            address.recipient,
            address.attention,
            address.line1,
            address.line2,
            f"{address.city}, {address.state} {address.zip_code}",
            address.country,
        )
        if line is not None
    ]


def _address_text(address: MailingAddress) -> str:
    """One comparable text form of a structured address (both sides formatted alike)."""
    return " ".join(
        part
        for part in (
            address.recipient,
            address.attention,
            address.line1,
            address.line2,
            address.city,
            address.state,
            address.zip_code,
            address.country,
        )
        if part
    )


def intake_answers(intake: s.SubmitIntake) -> ResearchAnswers:
    """What the notice already tells us, answered once from the confirmed intake."""
    ticket = intake.ticket
    answers = ResearchAnswers()
    for question, value in (
        (CustomerQuestion.NOTICE_ISSUE_DATE, ticket.notice_date or ticket.issue_date),
        (CustomerQuestion.NOTICE_PRINTED_DEADLINE, ticket.printed_deadline),
        (CustomerQuestion.NOTICE_ISSUING_AGENCY, ticket.issuer_hint),
        (CustomerQuestion.NOTICE_STAGE, ticket.stage),
    ):
        answers = answer_question(answers, question, value)
    return answers


def _intake_form_plan(intake: s.SubmitIntake) -> PacketPlan:
    """Closed factual fields and original customer words; no drafting or inference."""
    contact = intake.contact
    address = contact.mailing_address
    return PacketPlan(
        customer_fields=(
            ("citation_number", intake.ticket.citation_number),
            ("plate", intake.ticket.license_plate),
            ("customer_name", contact.name),
            ("customer_phone", contact.phone),
            ("customer_email", contact.email),
            (
                "customer_address",
                "\n".join(
                    line
                    for line in (
                        address.line1,
                        address.line2,
                        f"{address.city}, {address.state} {address.zip_code}",
                    )
                    if line
                ),
            ),
            ("customer_statement", intake.customer_position.statement),
        )
    )


def _ticket_values(values: Mapping[str, str]) -> dict[str, str]:
    """The ticket-only subset of the free-form form values; story/contact never qualify."""
    return {
        key: value for key, value in values.items() if key == "ticket" or key.startswith("ticket.")
    }


def _merge_ticket_binding(previous: s.IntakeForm, incoming: s.IntakeForm) -> s.IntakeForm:
    """Keep the ticket-only envelope across story/contact/evidence edits.

    A form that does not carry a binding keeps the previous one only while the ticket
    fields and original ticket photos are unchanged; any ticket-field or photo change
    drops it, and an explicitly supplied binding always wins.
    """
    if incoming.ticket_fact_binding is not None or previous.ticket_fact_binding is None:
        return incoming
    unchanged = (
        previous.ticket_photo_ref == incoming.ticket_photo_ref
        and previous.ticket_back_photo_ref == incoming.ticket_back_photo_ref
        and _ticket_values(previous.values) == _ticket_values(incoming.values)
    )
    if not unchanged:
        return incoming
    return incoming.model_copy(update={"ticket_fact_binding": previous.ticket_fact_binding})


def research_view(result: ResearchResult) -> s.ResearchView:
    def passages(quote: str | None, hashes: tuple[str, ...]) -> list[s.RulePassage]:
        if not quote:
            return []
        return [
            s.RulePassage(quote=quote, source_url=src.final_url)
            for src in result.sources
            if src.sha256 in hashes
            and src.authority_class is not AuthorityClass.SECONDARY
            and verify_quote(quote, src.text)
        ]

    findings = []
    for finding in result.findings:
        if finding.key in RETIRED_KEYS:
            continue
        alternatives = list(finding.evidence)
        if finding.status is FindingStatus.CONFLICTED:
            alternatives.extend(f for f in result.candidates if f.key == finding.key)
        quotes = {
            (ref.quote, ref.source_url): ref
            for item in (finding, *alternatives)
            for ref in passages(item.verbatim_quote, item.source_sha256s)
        }
        findings.append(
            s.ResearchFinding(
                fact=finding.key.value,
                status=(
                    FindingStatus.UNKNOWN.value
                    if finding.status is FindingStatus.CONFIRMED and not quotes
                    else finding.status.value
                ),
                official_quote=next((ref.quote for ref in quotes.values()), None),
                passages=list(quotes.values()),
            )
        )

    # Surface the agency's own instruction, never the provision a driver might enter.
    for label, evidence in (
        ("FILING_INSTRUCTION", result.verified_requirements.code_required),
        ("FILING_INSTRUCTION", result.packet_plan.code_required),
        ("AMOUNT_OR_DEPOSIT_INSTRUCTION", result.verified_requirements.amount_required),
        *(
            ("ADDITIONAL_FILING_RULE", item)
            for item in result.verified_requirements.compulsory_conditions
        ),
    ):
        if evidence:
            refs = passages(evidence.verbatim_quote, evidence.source_sha256s)
            item = s.ResearchFinding(
                fact=label,
                status="CONFIRMED" if refs else "UNKNOWN",
                official_quote=refs[0].quote if refs else None,
                passages=refs,
            )
            if item not in findings:
                findings.append(item)
    for rule in result.payment_rules:
        refs = passages(rule.verbatim_quote, rule.source_sha256s)
        if not refs and rule.status is FindingStatus.CONFIRMED:
            status = FindingStatus.UNKNOWN
        else:
            status = rule.status
        if rule.scope_quote and rule.scope_quote != rule.verbatim_quote:
            refs.extend(passages(rule.scope_quote, rule.source_sha256s))
        item = s.ResearchFinding(
            fact="PAYMENT_AND_REVIEW",
            status=status.value,
            official_quote=refs[0].quote if refs else None,
            passages=refs,
        )
        if item not in findings:
            findings.append(item)
    # Historical ordinance-only sources remain in the case audit record. A
    # shared source that also supplies a filing instruction still belongs here.
    retired_hashes = {
        digest
        for finding in (*result.findings, *result.candidates)
        if finding.key in RETIRED_KEYS
        for digest in finding.source_sha256s
    }
    rule_urls = {ref.source_url for row in findings for ref in row.passages}
    return s.ResearchView(
        jurisdiction=result.ticket.jurisdiction or result.ticket.city_id,
        stage=result.ticket.stage,
        sources=[
            s.ResearchSource(
                url=source.final_url,
                authority=source.authority_class.value,
                retrieved_at=source.retrieved_at,
            )
            for source in result.sources
            if source.authority_class is not AuthorityClass.SECONDARY
            and (source.sha256 not in retired_hashes or source.final_url in rule_urls)
        ],
        findings=findings,
    )


@dataclass(frozen=True)
class CaseServiceDeps:
    store: SqliteTransactionStore
    prehold: PreholdDeps
    clock: Callable[[], datetime]
    payments: PaymentClient
    photos: BlobStore
    research: ResearchDeps | None = None
    drafter: Drafter | None = None
    lob: CancelableLobClient | None = None
    stripe_webhook_secret: str = ""
    lob_webhook_secret: str = ""
    new_id: Callable[[], str] = lambda: uuid4().hex
    # Nationwide research is the customer default. An explicit set is an operator pause/test.
    launch_allowlist: AbstractSet[str] | None = None
    standard_offered: bool = LAUNCH_STANDARD_OFFERED
    # The customer's price for the T56 test; production assigns one of three per customer.
    price_for: Callable[[str], int] = lambda _owner: CERTIFIED_CENTS
    prehold_for_subject: Callable[[str], PreholdDeps] | None = None
    research_for_subject: Callable[[str], ResearchDeps] | None = None
    drafter_for_subject: Callable[[str], Drafter] | None = None
    # Configured fallback for the effective phase, appended last with a paid default so
    # existing positional test compositions keep their original paid behavior. A stored
    # private runtime override wins over this value and is read from the shared store.
    service_phase: ServicePhase = ServicePhase.PAID
    # The actual owner-scoped ticket-reading path; None keeps legacy fixtures unguarded.
    ticket_intake: TicketIntakeCoordinator | None = None
    known_records: tuple[Mapping[str, object], ...] = ()
    paid_cohort_cap: int | None = None
    # The release assembly may expose these only with a qualified support channel.
    support_codes_enabled: bool = False


def prehold_view(
    case_id: str,
    ticket: s.TicketFacts,
    result: PreholdResult,
    deps: CaseServiceDeps,
    amount_cents: int = CERTIFIED_CENTS,
) -> s.PreHoldResult:
    """The customer's pre-hold answer: an offer, or a plain no-charge stop."""
    support = result.support
    agency = result.agency
    if support.status is IntakeSupport.SUPPORTED and agency is not None:
        if amount_cents not in prices.ALLOWED_PRICE_CENTS:
            raise ValueError("offer price must be an allowed price")
        certified = CERTIFIED_OFFER.model_copy(update={"amount_cents": amount_cents})
        tiers = [certified, *([STANDARD_OFFER] if deps.standard_offered else [])]
        return s.Offer(
            case_id=case_id,
            issuer=s.ResolvedIssuer(
                issuer_id=agency.id,
                issuer_name=agency.name,
                deciding_body=agency.deciding_body,
                state=agency.state,
                stage=ticket.stage,
                service_basis=agency.basis_key,
                delegated_tenant=(
                    agency.delegated_scopes[0].host if agency.delegated_scopes else None
                ),
                evidence_ref=f"prehold:{case_id}",
            ),
            tiers=tiers,
        )
    if support.family is StopFamily.BUSINESS_EXCLUSION:
        message = stop_message(StopReason.BUSINESS_EXCLUSION, policy_reason=support.detail)
        return s.Stop(
            case_id=case_id,
            family=StopFamily.BUSINESS_EXCLUSION,
            reason_key=message.key,
            message=message.text,
            official_url=f"https://{agency.official_domains[0]}/"
            if agency is not None and agency.official_domains
            else None,
        )
    # Unresolved issuers and provider failures are verification stops; the resolution's
    # own hard stop is kept when it has one, S2 (where to send) otherwise.
    hard_stop = support.hard_stop or (
        result.issuer.stop if result.issuer is not None and result.issuer.stop else HardStop.S2
    )
    code = None if result.issuer is None else result.issuer.code
    message = stop_message(
        StopReason.NOT_YET_VERIFIED,
        hard_stop,
        veto_code=code,
        issuer_name=agency.name if agency is not None else ticket.issuer_hint,
    )
    return s.Stop(
        case_id=case_id,
        family=StopFamily.NOT_YET_VERIFIED,
        hard_stop=hard_stop,
        reason_key=message.key,
        message=message.text,
        official_url=f"https://{agency.official_domains[0]}/"
        if agency is not None and agency.official_domains
        else None,
    )


def intake_stop_view(case_id: str, stop: IntakeStop) -> s.Stop:
    """Use the existing pre-hold no-charge stop, without opening a payment case."""
    unknown = stop is IntakeStop.RENTAL_PAYMENT_UNKNOWN
    reason = StopReason.NOT_YET_VERIFIED if unknown else StopReason.BUSINESS_EXCLUSION
    hard_stop = HardStop.S7 if unknown else None
    message = stop_message(
        reason,
        hard_stop,
        policy_reason=None if unknown else stop.value,
        intake_stop=stop,
    )
    return s.Stop(
        case_id=case_id,
        family=stop_family(reason),
        hard_stop=hard_stop,
        reason_key=message.key,
        message=message.text,
    )


def _sweep_reason(exc: Exception) -> str:
    """Stable failure key for a sweep; unexpected errors never carry exception text."""
    if isinstance(exc, ServiceConflict):
        return str(exc)
    if isinstance(exc, StorageCorruption):
        return "storage_corruption"
    return f"internal_error:{type(exc).__name__}"


def _accepted_tracking_row(data: Mapping[str, object]) -> bool:
    """True when this stored webhook row was accepted as evidence (T17 piece B).

    Identity-mismatched notifications are kept as records with no evidence and no
    evidence flags; they never count as the attempt's own return or tracking proof.
    """
    return (
        data.get("scan_evidence") is True
        or data.get("failure_evidence") is True
        or "failure_facts" in data
        or "return_cause" in data
    )


def _no_charge_sentence(payment_state: PaymentState | None) -> str:
    """Truthful no-charge wording while a capture can still land (T17 piece B).

    A still-capturable hold says "yet"; only terminal no-charge states (released,
    written off, expired or never authorized) claim a final no charge.
    """
    return (
        "No charge has been made yet."
        if payment_state
        in {PaymentState.AUTHORIZED, PaymentState.CAPTURE_PENDING, PaymentState.CAPTURE_RETRYING}
        else "No charge was made."
    )


def _without_mailing_stops(
    resolution: ResearchResolution,
) -> tuple[ResearchResolution, str | None]:
    """No address waiver for new work under the current owner policy.

    Retain the call shape for saved/replayed research; consent remains historical
    evidence and cannot replace a verified submission destination.
    """
    return resolution, None


def _draft_context_sha256(
    ticket: object, copies: s.RequiredCopiesView | None, mailing: MailingAddress | None
) -> str:
    """Fingerprint of a run's fixed context; each call adds its own arguments to it.

    Copies count by what the letter is built from. ``editable`` only says whether the
    destination lock is taken yet, so it differs between a paid run and its recovery run.
    """
    fixed = None if copies is None else (copies.research_sha256, copies.revision, copies.items)
    return hashlib.sha256(repr((ticket, fixed, mailing)).encode()).hexdigest()


class _RecordedDrafter:
    """Records every paid drafting call before it is made and how it ended (T40)."""

    def __init__(
        self,
        inner: Drafter,
        store: SqliteTransactionStore,
        case_token: str,
        generation: int,
        context_sha256: str,
        clock: Callable[[], datetime],
        run_revision: str = "",
    ) -> None:
        self._inner = inner
        self._store = store
        self._case_token = case_token
        self._generation = generation
        self._context_sha256 = context_sha256
        self._clock = clock
        self._run_revision = run_revision
        self._made: dict[str, int] = {}

    def draft(
        self,
        notes: str,
        references: Sequence[Reference],
        facts: TicketFacts,
        problems: Sequence[str] = (),
    ) -> DraftResult:
        # Each call's own inputs, including the correction feedback it is given. That feedback
        # names spans by this run's fresh revision id, which is no new input for a recovery run.
        feedback = tuple(
            problem.replace(self._run_revision, "") if self._run_revision else problem
            for problem in problems
        )
        inputs = hashlib.sha256(
            repr((self._context_sha256, notes, tuple(references), facts, feedback)).encode()
        ).hexdigest()
        # A run's nth identical call reuses the nth that returned, so a recovery run follows the
        # paid run exactly, while a repeat inside one run stays a real attempt.
        repeat = self._made.get(inputs, 0)
        self._made[inputs] = repeat + 1
        saved = self._store.returned_draft(self._case_token, self._generation, inputs, repeat)
        if saved is not None:
            # This exact call already returned and was paid for: reuse it, spend nothing.
            return replace(_DRAFT_RESULT.validate_json(saved), usage=Usage())
        attempt = self._store.begin_draft_attempt(
            self._case_token, self._generation, inputs, self._clock()
        )
        if attempt is None:
            raise DraftCallsExhausted(self._case_token)
        try:
            result = self._inner.draft(notes, references, facts, problems)
        except (LlmProviderError, LlmBudgetExceeded):
            # Recorded before the stop is applied, so a crash in between is replayed as a
            # refusal and never followed by another paid call. Best effort: a storage fault
            # must not hide the refusal, whose existing handler stops the case.
            with contextlib.suppress(Exception):
                self._finish(attempt, inputs, DraftOutcome.REFUSED)
            raise
        except BaseException:
            with contextlib.suppress(Exception):
                self._finish(attempt, inputs, DraftOutcome.FAILED)
            raise
        self._finish(
            attempt, inputs, DraftOutcome.RETURNED, _DRAFT_RESULT.dump_json(result).decode()
        )
        return result

    def _finish(
        self, attempt: int, inputs: str, outcome: DraftOutcome, result_json: str | None = None
    ) -> None:
        self._store.finish_draft_attempt(
            self._case_token, self._generation, attempt, inputs, outcome, result_json
        )


class _RefusedDrafter:
    """Stands in for the drafter when a text-independent check already failed (no paid call)."""

    def __init__(self, reason: str) -> None:
        self._reason = reason

    def draft(
        self,
        notes: str,
        references: Sequence[Reference],
        facts: TicketFacts,
        problems: Sequence[str] = (),
    ) -> DraftResult:
        raise ServiceConflict(self._reason)


@dataclass(frozen=True)
class _OriginalCreateConfirmationGuard:
    """Refuses a first original create whose immutable intake no longer matches.

    The durable create intent is committed before this seam runs, so a restart resumes a
    SUBMITTING case here without repeating the FROZEN confirmation check. Every other call
    passes straight through: an already-committed uncertain intent must still be reconciled
    by lookup, retrieval or cancellation, never blocked by this guard.
    """

    client: CancelableLobClient
    confirmed: Callable[[LetterRequest], bool]

    def create_letter(self, request: LetterRequest, idempotency_key: str) -> LobResponse:
        if not self.confirmed(request):
            # A local refusal before any provider call is a definite non-send. The fixed
            # reason stays out of customer copy; the store maps it to its own stop event.
            return LobResponse(422, error="ticket_confirmation_required", local_refusal=True)
        return self.client.create_letter(request, idempotency_key)

    def find_by_metadata(self, submission_token: str) -> str | MetadataLookup | None:
        return self.client.find_by_metadata(submission_token)

    def lookup_letter(self, letter_id: str, submission_token: str) -> MetadataLookup:
        return self.client.lookup_letter(letter_id, submission_token)

    def cancel_letter(self, letter_id: str) -> CancelOutcome:
        return self.client.cancel_letter(letter_id)


class CaseService:
    """CaseServices over SqliteTransactionStore, through the frozen packet."""

    def __init__(self, deps: CaseServiceDeps) -> None:
        self._deps = deps
        self._store = deps.store

    def _public_support_code(self, case_token: str) -> str | None:
        if not self._deps.support_codes_enabled:
            return None
        return self._store.support_code_for_case(case_token)

    @property
    def service_phase(self) -> ServicePhase:
        """The effective phase for this call: stored override, else the configured fallback.

        Resolved from the existing shared store every time, so an operator change is
        visible without a rebuild or restart and is never cached per process. A missing or
        unreadable stored phase fails closed instead of falling back to paid.
        """
        return self._store.effective_service_phase(self._deps.service_phase)

    # Ownership -------------------------------------------------------------------------
    def workspace_owner(self, workspace_id: str) -> str | None:
        return self._store.workspace_owner(workspace_id)

    def case_owner(self, case_id: str) -> str | None:
        record = self._store.get_intake(case_id)
        return None if record is None else record.owner

    def refresh(self, case_id: str) -> None:
        """Expire the 30-minute window: the letter stops and the hold release is queued."""
        record = self._store.get_intake(case_id)
        if record is None or record.case_token is None:
            return
        case = self._store.get_case(record.case_token)
        now = self._deps.clock()
        if (
            case is not None
            and applies_to(case.letter_state)
            and window_status(case.authorized_at, now) is WindowStatus.EXPIRED
        ):
            self._store.apply_letter_event(
                case.case_token,
                WindowExpired(),
                case.revision,
                occurred_at=now,
                new_effect_id=lambda: f"eff_{self._deps.new_id()}",
            )

    # Workspaces --------------------------------------------------------------------------
    def appeal_workspace(self, owner: str) -> s.WorkspaceView | None:
        """The owner's current workspace; repeat customers keep earlier ones too."""
        latest = self._store.latest_owned_workspace(owner)
        return None if latest is None else self.workspace_view(latest)

    def create_workspace(self, owner: str, body: s.CreateWorkspace) -> s.WorkspaceView:
        """Start the customer's transaction, prefilled from a finished one.

        While a transaction is still live, a repeated start replays the same workspace so a
        double click never forks work. Once its case is terminal the customer is starting
        another appeal: a fresh workspace is created carrying their own words, photos and
        answers, with provision selections dropped because no new research has run.
        """
        now = self._deps.clock()
        previous = self._store.latest_owned_workspace(owner)
        if previous is not None and self._finished_transaction(previous):
            saved_previous = self._store.get_workspace(previous)
            if saved_previous is None:
                raise ServiceConflict("not_found")
            carried = restart_prefill(saved_previous.workspace, ())
            if body.notes:
                carried = replace(carried, notes=body.notes)
            token = f"ws_{self._deps.new_id()}"
            try:
                created = self._store.create_owned_workspace(
                    owner, token, carried, now, create_new=True
                )
            except IntakeConflict as exc:
                raise ServiceConflict(str(exc)) from None
            # Lineage is audit material; a failure must not strand a paid start.
            with contextlib.suppress(IntakePauseConflict):
                self._store.record_workspace_lineage(created.workspace_token, previous, now=now)
            return s.WorkspaceView(
                workspace_id=created.workspace_token, notes=created.workspace.notes
            )
        token = f"ws_{self._deps.new_id()}"
        try:
            saved = self._store.create_owned_workspace(
                owner, token, Workspace(notes=body.notes), now
            )
        except IntakeConflict as exc:
            raise ServiceConflict(str(exc)) from None
        return s.WorkspaceView(workspace_id=saved.workspace_token, notes=saved.workspace.notes)

    def _finished_transaction(self, workspace_id: str) -> bool:
        """Whether this workspace's transaction reached a terminal case state."""
        intake = self._store.intake_for_workspace(workspace_id)
        if intake is None or intake.case_token is None:
            return False
        case = self._store.get_case(intake.case_token)
        return case is not None and case.letter_state in TERMINAL_STATES

    def workspace_view(self, workspace_id: str) -> s.WorkspaceView:
        saved = self._store.get_workspace(workspace_id)
        if saved is None:
            raise ServiceConflict("not_found")
        intake = self._store.intake_for_workspace(workspace_id)
        return s.WorkspaceView(
            workspace_id=workspace_id,
            notes=saved.workspace.notes,
            draft_revision=saved.revision,
            intake_form=None
            if "web_intake_v1" not in saved.workspace.answers
            else s.IntakeForm.model_validate_json(saved.workspace.answers["web_intake_v1"]),
            intake=None
            if intake is None
            else s.SubmitIntake.model_validate_json(intake.intake_json),
            case_id=None if intake is None else intake.intake_id,
            ticket_reading_required=self.ticket_reading_required,
        )

    def save_intake_form(self, workspace_id: str, body: s.SaveIntakeForm) -> s.WorkspaceView:
        saved = self._store.get_workspace(workspace_id)
        owner = self._store.workspace_owner(workspace_id)
        if saved is None or owner is None:
            raise ServiceConflict("not_found")
        previous = saved.workspace.answers.get("web_intake_v1")
        form = body.form
        if previous is not None:
            previous_form = s.IntakeForm.model_validate_json(previous)
            form = _merge_ticket_binding(previous_form, form)
            if previous_form == form:
                return self.workspace_view(workspace_id)
        if self._store.intake_for_workspace(workspace_id) is not None:
            raise ServiceConflict("intake_already_submitted")
        for ref in (
            form.ticket_photo_ref,
            form.ticket_back_photo_ref,
            *form.selected_evidence_photo_refs,
        ):
            if ref is not None and self._deps.photos.get(owner, ref) is None:
                raise ServiceConflict("unknown_photo_ref")
        updated = replace(
            saved.workspace,
            answers={**saved.workspace.answers, "web_intake_v1": form.model_dump_json()},
        )
        try:
            self._store.save_workspace(
                workspace_id, updated, self._deps.clock(), expected_revision=body.expected_revision
            )
        except RevisionConflict:
            raise ServiceConflict("workspace_changed_reload_required") from None
        return self.workspace_view(workspace_id)

    def upload_photo(self, workspace_id: str, body: s.PhotoUpload) -> s.PhotoRef:
        owner = self._store.workspace_owner(workspace_id)
        if owner is None:
            raise ServiceConflict("not_found")
        return s.PhotoRef(photo_ref=store_upload(self._deps.photos, owner, body))

    # Ticket reading, independent review and confirmation -------------------------------
    @property
    def ticket_reading_required(self) -> bool:
        return self._deps.ticket_intake is not None and self._deps.ticket_intake.required

    def _ticket_path[T](self, work: Callable[[], T]) -> T:
        try:
            return work()
        except TicketIntakeServiceError as exc:
            raise ServiceConflict(str(exc)) from None

    def _ticket_intake(self) -> TicketIntakeCoordinator:
        if self._deps.ticket_intake is None:
            raise ServiceConflict("ticket_reading_disabled")
        return self._deps.ticket_intake

    def ticket_reading_status(self, workspace_id: str) -> s.TicketReadingStatus:
        if self._deps.ticket_intake is None:
            return s.TicketReadingStatus(enabled=False, required=False)
        coordinator = self._deps.ticket_intake
        return self._ticket_path(lambda: coordinator.status(workspace_id))

    def request_ticket_reading(
        self, workspace_id: str, body: s.TicketOperationRequest
    ) -> s.TicketReadingSnapshot:
        coordinator = self._ticket_intake()
        return self._ticket_path(
            lambda: coordinator.request_reading(
                workspace_id, body.request_id, body.expected_revision
            )
        )

    def request_ticket_review(
        self, workspace_id: str, body: s.TicketOperationRequest
    ) -> s.TicketReviewSnapshot:
        coordinator = self._ticket_intake()
        return self._ticket_path(
            lambda: coordinator.request_review(
                workspace_id,
                body.request_id,
                body.expected_revision,
                customer_supplied=isinstance(body, s.TicketReviewRequest)
                and body.customer_supplied,
            )
        )

    def confirm_ticket_reading(
        self, workspace_id: str, body: s.TicketConfirmationRequest
    ) -> s.TicketConfirmationReceipt:
        coordinator = self._ticket_intake()
        return self._ticket_path(lambda: coordinator.confirm(workspace_id, body))

    def ticket_observability(self, workspace_id: str) -> s.TicketObservabilitySnapshot:
        coordinator = self._ticket_intake()
        return self._ticket_path(lambda: coordinator.ticket_observability(workspace_id))

    def start_ticket_review_session(
        self, workspace_id: str, body: s.TicketReviewSessionRequest
    ) -> s.TicketReviewSession:
        coordinator = self._ticket_intake()
        return self._ticket_path(lambda: coordinator.start_review_session(workspace_id, body))

    def record_ticket_review_observation(
        self,
        workspace_id: str,
        session_id: str,
        body: s.TicketReviewObservationRequest,
    ) -> s.TicketReviewObservationReceipt:
        coordinator = self._ticket_intake()
        return self._ticket_path(
            lambda: coordinator.record_review_observation(workspace_id, session_id, body)
        )

    # Intake ------------------------------------------------------------------------------
    def submit(self, workspace_id: str, body: s.SubmitIntake) -> s.IntakeReceipt:
        intake_json = body.model_dump_json()
        owner = self._store.workspace_owner(workspace_id)
        refs = [
            *body.photo_refs,
            *(
                ref
                for ref in (body.citation_photo_ref, body.citation_back_photo_ref)
                if ref is not None
            ),
        ]
        if owner is None or any(self._deps.photos.get(owner, ref) is None for ref in refs):
            raise ServiceConflict("unknown_photo_ref")
        existing = self._store.intake_for_workspace(workspace_id)
        if existing is None and self._store.intake_paused():
            # Emergency kill-switch: no new intake starts. Replaying an intake that was
            # already accepted stays allowed, because nothing new is created.
            raise ServiceConflict("intake_paused")
        if existing is not None and existing.intake_json != intake_json:
            raise ServiceConflict("intake_already_submitted_create_new_workspace")
        if existing is not None:
            try:
                record = self._store.record_intake_with_workspace_snapshot(
                    existing.intake_id,
                    workspace_id,
                    intake_json,
                    existing.prehold_json,
                    body.customer_position.statement,
                    tuple(body.photo_refs),
                    frozenset(body.customer_position.provision_selections),
                    self._deps.clock(),
                    agency_json=existing.agency_json,
                )
            except IntakeConflict as exc:
                raise ServiceConflict(str(exc)) from None
            return self._receipt(workspace_id, record.intake_id, record.prehold_json)
        if not is_printable(body.customer_position.statement):
            raise ServiceConflict("customer_statement_not_printable")
        if self._deps.ticket_intake is not None and self._deps.ticket_intake.required:
            self._ticket_path(
                lambda: self._deps.ticket_intake.require_submission_confirmation(  # type: ignore[union-attr]
                    workspace_id, body
                )
            )
        case_id = f"case_{self._deps.new_id()}"
        if self._deps.ticket_intake is not None:
            case_id = self._ticket_path(
                lambda: self._deps.ticket_intake.reserved_intake_id(  # type: ignore[union-attr]
                    workspace_id, owner, case_id
                )
            )
        ticket = body.ticket
        submitted_at = self._deps.clock()
        today = submitted_at.date()
        deadline_days: int | None = None
        deadline_rule: DeadlineRule | None = None
        for rec in self._deps.known_records:
            city_name = str(rec.get("name", "")).lower()
            city_id = str(rec.get("city_id", ""))
            rec_state = city_id.split("-")[1] if "-" in city_id else ""
            matches_city = bool(
                ticket.issued_city and city_name == ticket.issued_city.strip().lower()
            )
            matches_state = not ticket.issued_state or (
                rec_state.lower() == ticket.issued_state.strip().lower()
            )
            if matches_city and matches_state:
                deadline_val = rec.get("appeal_deadline_days")
                if isinstance(deadline_val, int):
                    deadline_days = deadline_val
                profile = rec.get("submissionAcceptanceProfile")
                if isinstance(profile, Mapping):
                    clock_prof = profile.get("deadlineClock")
                    if isinstance(clock_prof, Mapping):
                        if clock_prof.get("postmarkAccepted") is True:
                            deadline_rule = DeadlineRule.POSTMARK
                        elif clock_prof.get("receivedByDeadlineRequired") is True:
                            deadline_rule = DeadlineRule.RECEIPT
                break
        early_stop = intake_customer_stop(
            ticket.issue_date,
            today,
            body.rental_company_paid,
            deadline_days=deadline_days,
            deadline_rule=deadline_rule,
        )
        result: PreholdResult | None = None
        if early_stop is not None:
            view: s.PreHoldResult = intake_stop_view(case_id, early_stop)
        else:
            prehold_deps = (
                self._deps.prehold
                if self._deps.prehold_for_subject is None
                else self._deps.prehold_for_subject(case_id)
            )
            result = run_prehold(
                PreholdRequest(
                    IssuerQuery(ticket.issuer_hint, ticket.issued_city, ticket.issued_state),
                    ticket.stage,
                    IssuerKind(ticket.issuer_kind),
                    ticket.notice_date or ticket.issue_date,
                    ticket.printed_deadline,
                    today,
                ),
                prehold_deps,
                launch_allowlist=self._deps.launch_allowlist,
            )
            view = prehold_view(case_id, ticket, result, self._deps, self._deps.price_for(owner))
        prehold_json = _PREHOLD.dump_json(view).decode()
        try:
            record = self._store.record_intake_with_workspace_snapshot(
                case_id,
                workspace_id,
                intake_json,
                prehold_json,
                body.customer_position.statement,
                tuple(body.photo_refs),
                frozenset(body.customer_position.provision_selections),
                submitted_at,
                agency_json=(
                    agency_json(result.agency)
                    if (
                        isinstance(view, s.Offer)
                        and result is not None
                        and result.agency is not None
                    )
                    else None
                ),
            )
        except IntakeConflict as exc:
            raise ServiceConflict(str(exc)) from None
        return self._receipt(workspace_id, record.intake_id, record.prehold_json)

    def _receipt(self, workspace_id: str, case_id: str, prehold_json: str) -> s.IntakeReceipt:
        stopped = isinstance(self.pre_hold(case_id), s.Stop)
        return s.IntakeReceipt(
            workspace_id=workspace_id,
            case_id=case_id,
            state=CaseState.STOPPED_NO_CHARGE if stopped else CaseState.INTAKE,
        )

    def pre_hold(self, case_id: str, *, today: date | None = None) -> s.PreHoldResult:
        record = self._store.get_intake(case_id)
        if record is None:
            raise ServiceConflict("not_found")
        view = _PREHOLD.validate_json(record.prehold_json)
        if (
            isinstance(view, s.Offer)
            and record.case_token is None
            and not self._store.has_active_authorization_attempt(case_id)
        ):
            intake = s.SubmitIntake.model_validate_json(record.intake_json)
            stop = intake_customer_stop(
                intake.ticket.issue_date,
                self._deps.clock().date() if today is None else today,
                intake.rental_company_paid,
            )
            stopped = None
            if stop is not None:
                stopped = intake_stop_view(case_id, stop)
            elif (
                self._deps.paid_cohort_cap is not None
                and self.service_phase is ServicePhase.PAID
                and self._store.beta_entitlement(case_id) is None
                and self._store.paid_cohort_full(case_id, self._deps.paid_cohort_cap)
            ):
                stopped = s.Stop(
                    case_id=case_id,
                    family=StopFamily.BUSINESS_EXCLUSION,
                    reason_key="paid_cohort_full",
                    message="The paid beta is full. Your work is saved. "
                    "Check your ticket for the issuer's filing instructions and deadline.",
                )
            if stopped is not None:
                if record.agency_json is not None:
                    agency = decode_agency(record.agency_json)
                    if agency.official_domains:
                        stopped.official_url = f"https://{agency.official_domains[0]}/"
                return stopped
        if isinstance(view, s.Offer) and record.selected_tier is not None:
            view = view.model_copy(update={"selected_tier": s.MailTier(record.selected_tier)})
        return view

    def choose(self, case_id: str, body: s.ChooseTier) -> s.Offer:
        record = self._store.get_intake(case_id)
        if record is None:
            raise ServiceConflict("not_found")
        offer = self.pre_hold(case_id)
        if isinstance(offer, s.Stop) and offer.reason_key == "paid_cohort_full":
            raise ServiceConflict(offer.reason_key)
        if not isinstance(offer, s.Offer):
            raise ServiceConflict("pre_hold_stopped")
        if record.case_token is not None:
            if record.selected_tier == body.tier.value:
                return offer.model_copy(update={"selected_tier": body.tier})
            raise ServiceConflict("tier_locked_at_authorization")
        if body.tier not in {tier.tier for tier in offer.tiers}:
            raise ServiceConflict("tier_not_offered")
        try:
            self._store.update_intake(
                case_id, record.revision, selected_tier=body.tier.value, case_token=None
            )
        except IntakeConflict as exc:
            raise ServiceConflict(str(exc)) from None
        return offer.model_copy(update={"selected_tier": body.tier})

    # Built by later Lane C jobs (docs/specs/lane-c-core-jobs.md) ------------------------
    def authorize(self, case_id: str, body: s.StartAuthorization) -> s.Authorization:
        """Authorize payment for a case."""
        if self._store.beta_entitlement(case_id) is not None:
            raise ServiceConflict("beta_no_payment_required")
        record = self._store.get_intake(case_id)
        if record is None:
            raise ServiceConflict("not_found")
        # Reuse the existing authorization-start clock read for the intake gate.
        started_at = self._deps.clock() if record.case_token is None else None
        offer = self.pre_hold(case_id, today=None if started_at is None else started_at.date())
        if isinstance(offer, s.Stop) and offer.reason_key == "paid_cohort_full":
            raise ServiceConflict(offer.reason_key)
        if not isinstance(offer, s.Offer):
            raise ServiceConflict("case_stopped")
        if record.case_token is not None:
            return self._authorization(case_id, record.case_token)
        if record.selected_tier is None:
            raise ServiceConflict("choose_tier_first")
        tier = next(t for t in offer.tiers if t.tier.value == record.selected_tier)
        if (
            type(tier.amount_cents) is not int
            or tier.amount_cents not in prices.ALLOWED_PRICE_CENTS
        ):
            raise ServiceConflict("authorization_amount_not_supported")
        assert started_at is not None
        assignment = self._store.pricing_for_scope(record.owner)
        request_json = authorization_request_json(
            case_id,
            tier.tier.value,
            tier.amount_cents,
            body.payment_method_id,
            assigned=(
                tier.tier is s.MailTier.CERTIFIED
                and assignment is not None
                and assignment.experiment_id == prices.PRICE_EXPERIMENT_ID
                and assignment.amount_cents == tier.amount_cents
            ),
        )
        key = mutation_key(
            "authorization",
            case_id,
            f"{tier.tier.value}:{tier.amount_cents}",
            body.payment_method_id,
        )
        try:
            self._store.begin_authorization_attempt(
                case_id,
                key,
                tier.tier.value,
                tier.amount_cents,
                body.payment_method_id,
                request_json,
                started_at,
                allow_new_paid_authorization=self.service_phase is ServicePhase.PAID,
                paid_cohort_cap=self._deps.paid_cohort_cap,
            )
        except IntakeConflict as exc:
            current = self._store.get_intake(case_id)
            if current is not None and current.case_token is not None:
                return self._authorization(case_id, current.case_token)
            raise ServiceConflict(str(exc)) from None

        now = self._deps.clock()
        try:
            intent = self._store.claim_authorization_dispatch(
                case_id, key, now, AUTHORIZATION_REPLAY_LIMIT
            )
        except IntakeConflict as exc:
            raise ServiceConflict(str(exc)) from None
        if intent.state == "authorized":
            current = self._store.get_intake(case_id)
            if current is not None and current.case_token is not None:
                return self._authorization(case_id, current.case_token)
            raise ServiceConflict("authorization_outcome_unresolved")

        saved_request = json.loads(intent.request_json)
        saved_metadata = saved_request.get("metadata")
        if not isinstance(saved_metadata, dict) or not all(
            isinstance(name, str) and isinstance(value, str)
            for name, value in saved_metadata.items()
        ):
            raise ServiceConflict("authorization_identity_invalid")

        refusal = authorization_dispatch_refusal(
            intent, self._deps.clock(), AUTHORIZATION_REPLAY_LIMIT
        )
        if refusal is not None:
            self._record_authorization_outcome(
                case_id, intent.idempotency_key, "uncertain", intent.authorization_ref
            )
            raise ServiceConflict(refusal)

        try:
            if intent.authorization_ref is not None:
                held = self._deps.payments.retrieve(intent.authorization_ref)
            else:
                held = self._deps.payments.authorize(
                    intent.amount_cents,
                    intent.payment_method_id,
                    intent.idempotency_key,
                    saved_metadata,
                )
        except Exception:
            self._record_authorization_outcome(
                case_id, intent.idempotency_key, "uncertain", intent.authorization_ref
            )
            raise ServiceConflict("card_not_authorized") from None

        now = self._deps.clock()
        if now < intent.created_at or now < intent.updated_at:
            raise ServiceConflict("authorization_outcome_unresolved")

        if (
            intent.authorization_ref is not None
            and held.authorization_ref != intent.authorization_ref
        ):
            self._record_authorization_outcome(
                case_id,
                intent.idempotency_key,
                "uncertain",
                intent.authorization_ref,
            )
            raise ServiceConflict("card_not_authorized")

        inspection = held.authorization_inspection
        hold_is_verified = (
            held.status == "requires_capture"
            and bool(held.authorization_ref)
            and (
                intent.authorization_ref is None
                or held.authorization_ref == intent.authorization_ref
            )
            and intent.amount_cents in prices.ALLOWED_PRICE_CENTS
            and held.amount_cents == intent.amount_cents
            and held.currency == "usd"
            and held.capture_method == "manual"
            and held.full_object
            and (
                not self._deps.payments.authorization_evidence_required
                or (
                    inspection is not None
                    and inspection.authorization_ref == held.authorization_ref
                )
            )
        )
        if hold_is_verified:
            expires = (
                inspection.expires_at
                if inspection is not None
                else (
                    datetime.fromtimestamp(held.capture_before, UTC)
                    if held.capture_before
                    else now + AUTHORIZATION_VALIDITY
                )
            )
            if expires > now:
                self._record_authorization_outcome(
                    case_id,
                    intent.idempotency_key,
                    "uncertain",
                    held.authorization_ref,
                )
                try:
                    case = self._store.complete_authorization_attempt(
                        case_id,
                        intent.idempotency_key,
                        held.authorization_ref,
                        self._deps.clock(),
                        expires,
                    )
                except IntakeConflict as exc:
                    raise ServiceConflict(str(exc)) from None
                return self._authorization(case_id, case.case_token)

        amount_matches = (
            type(held.amount_cents) is int
            and held.amount_cents == intent.amount_cents
            and intent.amount_cents in prices.ALLOWED_PRICE_CENTS
        )
        definitely_declined = (
            amount_matches
            and held.status in {"declined", "requires_payment_method"}
            and (
                not self._deps.payments.authorization_evidence_required
                or held.classification == "verified_object"
            )
        )
        definitely_canceled = (
            amount_matches
            and held.status == "canceled"
            and held.classification == "verified_object"
            and held.full_object
        )
        recorded_ref = (
            intent.authorization_ref
            if intent.authorization_ref is not None
            else (held.authorization_ref or None)
        )
        self._record_authorization_outcome(
            case_id,
            intent.idempotency_key,
            "declined" if definitely_declined or definitely_canceled else "uncertain",
            recorded_ref,
        )
        raise ServiceConflict("card_not_authorized")

    def _record_authorization_outcome(
        self, case_id: str, key: str, state: str, authorization_ref: str | None
    ) -> None:
        """Every outcome write gets a fresh clock and a handled store refusal."""
        try:
            self._store.record_authorization_outcome(
                case_id, key, state, authorization_ref, self._deps.clock()
            )
        except IntakeConflict as exc:
            raise ServiceConflict(str(exc)) from None

    def _authorization(self, case_id: str, case_token: str) -> s.Authorization:
        if self._store.beta_entitlement(case_id) is not None:
            raise ServiceConflict("beta_no_payment_required")
        case = self._store.get_case(case_token)
        if case is None:
            raise ServiceConflict("not_found")
        return s.Authorization(
            case_id=case_id,
            support_code=self._public_support_code(case.case_token),
            payment_intent_id=case.authorization_ref,
            amount_cents=case.amount_cents,
            status="authorized",
            window_started_at=case.authorized_at,
            window_expires_at=case.authorized_at + DEFAULT_WINDOW,
            fake=False,
        )

    # Free beta acceptance ---------------------------------------------------------------
    def _beta_first_start_refusal(
        self, case_id: str, entitlement: BetaEntitlement, now: datetime
    ) -> str | None:
        """Current first-start refusal for an unaccepted snapshot, or None to proceed."""
        if not entitlement.selected_at <= now < entitlement.eligible_until:
            return "beta_qualification_expired"
        # A newly accepting case keeps the existing pre-hold/customer-age refusal.
        if isinstance(self.pre_hold(case_id, today=now.date()), s.Stop):
            return "case_stopped"
        return None

    def _reconcile_beta_refusal(self, case_id: str, refusal: str) -> BetaEntitlement:
        """A last durable read decides; a concurrent acceptance supersedes the refusal."""
        current = self._store.beta_entitlement(case_id)
        if current is None:
            raise ServiceConflict("beta_entitlement_required")
        if current.accepted_at is None:
            raise ServiceConflict(refusal)
        return current

    def beta_offer(self, case_id: str) -> s.BetaOffer:
        """Read one selected entitlement without selecting, reserving or charging."""
        if self._store.get_intake(case_id) is None:
            raise ServiceConflict("not_found")
        if self._store.intake_paused():
            raise ServiceConflict("intake_paused")
        entitlement = self._store.beta_entitlement(case_id)
        if entitlement is None:
            raise ServiceConflict("beta_entitlement_required")
        if entitlement.accepted_at is None:
            now = self._deps.clock()
            refusal = self._beta_first_start_refusal(case_id, entitlement, now)
            if refusal is not None:
                entitlement = self._reconcile_beta_refusal(case_id, refusal)
        return s.BetaOffer(
            case_id=case_id,
            disclosure_version=BETA_DISCLOSURE_VERSION,
            disclosure_text=BETA_DISCLOSURE_TEXT,
            disclosure_sha256=BETA_DISCLOSURE_SHA256,
            eligible_until=entitlement.eligible_until,
            accepted_at=entitlement.accepted_at,
        )

    def accept_beta(self, case_id: str, body: s.AcceptBeta) -> s.BetaAcceptanceReceipt:
        """Accept the exact disclosure once; the store binds the case and entitlement."""
        if self._store.get_intake(case_id) is None:
            raise ServiceConflict("not_found")
        entitlement = self._store.beta_entitlement(case_id)
        if entitlement is None:
            raise ServiceConflict("beta_entitlement_required")
        now = self._deps.clock()
        if entitlement.accepted_at is None:
            # One acceptance clock; current eligibility and customer-age/stop rules.
            refusal = self._beta_first_start_refusal(case_id, entitlement, now)
            if refusal is not None:
                self._reconcile_beta_refusal(case_id, refusal)
        try:
            case = self._store.start_beta_case(case_id, body.disclosure_sha256, now)
        except BetaConflict as exc:
            raise ServiceConflict(str(exc)) from None
        accepted = self._store.beta_entitlement(case_id)
        if accepted is None or accepted.accepted_at is None:
            raise ServiceConflict("beta_entitlement_required")
        return s.BetaAcceptanceReceipt(
            case_id=case_id,
            support_code=self._public_support_code(case.case_token),
            accepted_at=accepted.accepted_at,
            window_started_at=case.authorized_at,
            window_expires_at=case.authorization_expires_at,
        )

    # Accepted-beta customer-reported notices --------------------------------------------
    def _notice_view(self, receipt: NoticeReceipt) -> s.BetaFeedbackNoticeReceipt:
        return s.BetaFeedbackNoticeReceipt(
            notice_id=receipt.notice_id,
            case_id=receipt.case_id,
            request_id=receipt.request_id,
            received_at=receipt.received_at,
            pages=[
                s.BetaFeedbackNoticePageView(
                    page_number=index,
                    role="envelope" if page.role == "envelope" else "page",
                )
                for index, page in enumerate(receipt.pages, start=1)
            ],
            reported_fields=s.BetaFeedbackReportedFields(
                customer_seen_on=(
                    None
                    if receipt.customer_seen_on is None
                    else date.fromisoformat(receipt.customer_seen_on)
                ),
                printed_on=(
                    None if receipt.printed_on is None else date.fromisoformat(receipt.printed_on)
                ),
                stated_deadline_text=receipt.stated_deadline_text,
            ),
            correction_of=receipt.correction_of,
        )

    def record_beta_feedback_notice(
        self, case_id: str, body: s.BetaFeedbackNoticeRequest
    ) -> s.BetaFeedbackNoticeReceipt:
        record = self._intake(case_id)
        # Verify every customer-claimed photo through the actual owner-scoped store.
        for page in body.pages:
            data = self._deps.photos.get(record.owner, page.photo_ref)
            if data is None or photo_ref(data) != page.photo_ref:
                raise ServiceConflict("beta_feedback_photo_missing")
        try:
            receipt = self._store.record_beta_feedback_notice(
                case_id,
                record.owner,
                body.request_id,
                tuple(NoticePage(page.role, page.photo_ref) for page in body.pages),
                self._deps.clock(),
                customer_seen_on=body.customer_seen_on,
                printed_on=body.printed_on,
                stated_deadline_text=body.stated_deadline_text,
                correction_of=body.correction_of,
            )
        except (BetaFeedbackConflict, IntakeConflict) as exc:
            raise ServiceConflict(str(exc)) from None
        return self._notice_view(receipt)

    def beta_feedback_notices(
        self, case_id: str, *, limit: int = 100, cursor: str | None = None
    ) -> s.BetaFeedbackNoticeList:
        record = self._intake(case_id)
        internal_cursor: str | None = None
        if cursor is not None:
            try:
                internal_cursor = self._store.beta_feedback_notice_cursor(
                    case_id, record.owner, cursor
                )
            except (BetaFeedbackConflict, IntakeConflict, ValueError):
                raise ServiceConflict("invalid_beta_feedback_cursor") from None
        try:
            listing = self._store.list_beta_feedback_notices(
                case_id, record.owner, limit=limit, cursor=internal_cursor
            )
        except (BetaFeedbackConflict, IntakeConflict) as exc:
            raise ServiceConflict(str(exc)) from None
        return s.BetaFeedbackNoticeList(
            notices=[self._notice_view(item) for item in listing.notices],
            next_cursor=(
                listing.notices[-1].notice_id
                if listing.next_cursor is not None and listing.notices
                else None
            ),
        )

    def beta_feedback_notice_page(self, case_id: str, notice_id: str, page_number: int) -> bytes:
        record = self._intake(case_id)
        try:
            notice = self._store.beta_feedback_notice(case_id, record.owner, notice_id)
        except (BetaFeedbackConflict, IntakeConflict, ValueError):
            raise ServiceConflict("beta_feedback_notice_not_found") from None
        if not 1 <= page_number <= len(notice.pages):
            raise ServiceConflict("beta_feedback_page_not_found")
        ref = notice.pages[page_number - 1].photo_ref
        data = self._deps.photos.get(record.owner, ref)
        if data is None or photo_ref(data) != ref:
            raise ServiceConflict("beta_feedback_page_not_found")
        return data

    def status(self, case_id: str) -> s.CaseStatus:
        record = self._intake(case_id)
        offer = self.pre_hold(case_id)
        tier = None if record.selected_tier is None else s.MailTier(record.selected_tier)
        case = None if record.case_token is None else self._store.get_case(record.case_token)
        if case is None:
            stopped = offer if isinstance(offer, s.Stop) else None
            return s.CaseStatus(
                case_id=case_id,
                workspace_id=record.workspace_token,
                state=CaseState.STOPPED_NO_CHARGE if stopped else CaseState.INTAKE,
                payment_state=None,
                selected_tier=tier,
                window_started_at=None,
                window_expires_at=None,
                seconds_remaining=None,
                window_status=None,
                question=None,
                stop=stopped,
                fake=False,
            )
        now = self._deps.clock()
        windowed = applies_to(case.letter_state)
        saved = self._store.get_research(case_id)
        draft = self._store.get_draft(case_id)
        return s.CaseStatus(
            case_id=case_id,
            support_code=self._public_support_code(case.case_token),
            workspace_id=record.workspace_token,
            state=case.letter_state,
            payment_state=case.payment_state,
            selected_tier=tier,
            amount_cents=case.amount_cents,
            window_started_at=case.authorized_at,
            window_expires_at=case.authorized_at + DEFAULT_WINDOW,
            seconds_remaining=seconds_remaining(case.authorized_at, now) if windowed else None,
            window_status=window_status(case.authorized_at, now) if windowed else None,
            question=self._open_question(case.case_token)
            if case.letter_state is CaseState.NEEDS_CUSTOMER
            else None,
            stop=self._case_stop(case_id, case.case_token),
            research=None if saved is None else research_view(decode_research(saved.research_json)),
            required_copies=None if saved is None else self._required_copy_state(record, saved)[0],
            draft_spans=[] if draft is None else list(_SPANS.validate_json(draft.spans_json)),
            draft_flags=[] if draft is None else list(_FLAGS.validate_json(draft.flags_json)),
            destination_choice=self._destination_choice(record, case, draft is not None),
            return_remedy=self._return_remedy_status(case, case_id),
            fake=False,
        )

    def _remail_outcome_copy(self, case: CaseRecord) -> tuple[str, bool]:
        """Fixed copy and active flag for the actual second-attempt outcome."""
        if case.letter_state is CaseState.RETURNED:
            return "The free re-mail was returned.", False
        if case.letter_state is CaseState.DELIVERED:
            return "Delivery proof for the free re-mail has been recorded.", False
        cursor = self._store.submission_cursor(case.case_token, 2)
        outcome = None if cursor is None else cursor.outcome
        if isinstance(outcome, Rejected):
            if outcome.reason == "ORIGINAL_PAYMENT_NOT_CAPTURED":
                return (
                    "The free re-mail was not sent because no original payment was captured.",
                    False,
                )
            if outcome.reason == GATE_REFUSED_REASON:
                return (
                    "The free re-mail was not sent because its address verification "
                    "changed before dispatch.",
                    False,
                )
            return (
                "The free re-mail could not be sent because the mail provider "
                "rejected the request.",
                False,
            )
        if isinstance(outcome, NotSent | Canceled):
            return "The free re-mail was not sent.", False
        if isinstance(outcome, Accepted) and case.letter_state is CaseState.IN_TRANSIT:
            return "The free re-mail is in transit to a different verified address.", True
        if isinstance(outcome, Uncertain):
            return "We are confirming whether the free re-mail was sent.", True
        return "A free re-mail to a different verified address has been arranged.", True

    def _pending_second_return(self, case: CaseRecord, case_id: str) -> s.ReturnedMailRemedy:
        """Expose the second return while its own cost-dependent remedy is pending."""
        message = "Your free re-mail was returned. The returned-mail remedy is being resolved."
        prepared = self._store.get(case.case_token, 2)
        cursor = self._store.submission_cursor(case.case_token, 2)
        if prepared is not None and cursor is not None and isinstance(cursor.outcome, Accepted):
            facts, cost, estimate = self._returned_mail_evidence(
                case_id, case, prepared.request.recipient, cursor.outcome.letter_id
            )
            responsibility = classify_address_failure(
                self._store.frozen_destination(case.case_token, 2), facts
            )
            prospective = remedy_for_responsibility(
                responsibility,
                deadline_feasible=False,
                other_verified_address_exists=False,
                price_cents=case.amount_cents,
                mailing_cost_cents=0,
                remails_used=1,
            )
            returned_at = self._first_return_confirmation(case)
            if (
                prospective is not None
                and prospective.kind is RemedyKind.PARTIAL_REFUND
                and (cost is None or estimate)
                and returned_at is not None
                and self._deps.clock() < returned_at + RETURN_COST_EVIDENCE_WINDOW
            ):
                message = (
                    "Your free re-mail was returned. We are awaiting mailing cost evidence "
                    "before resolving this return."
                )
        return s.ReturnedMailRemedy(
            attempt=2,
            kind="RETURN_PENDING",
            reason_code="RETURN_REMEDY_PENDING",
            message=message,
            refund_cents=0,
            refund_status=None,
            remail=False,
        )

    def _beta_return_closed(
        self, case: CaseRecord, resolved: ReturnedMailResolution
    ) -> s.ReturnedMailRemedy:
        """The terminal free-beta return: nothing was charged, so no money or re-mail exists.

        The recorded responsibility stays exactly as the evidence classified it, so a generic
        beta return is never relabelled as a customer address failure. The original mailing
        proof stays with the case for the customer and the owner.
        """
        return s.ReturnedMailRemedy(
            attempt=1 if resolved.attempt == 1 else 2,
            kind="BETA_RETURN_CLOSED",
            reason_code="BETA_RETURN_CLOSED",
            message=(
                "Your free beta letter was returned. This free beta appeal is closed with "
                "no charge: nothing was charged, so there is no refund and no re-mail. "
                "Your original mailing record remains available."
            ),
            refund_cents=0,
            refund_status=None,
            remail=False,
            proof_packet=self._return_proof(case, resolved.attempt),
        )

    def _return_remedy_status(self, case: CaseRecord, case_id: str) -> s.ReturnedMailRemedy | None:
        resolved = self._store.returned_mail_resolution(case.case_token, case.attempt)
        if resolved is None and case.attempt == 2:
            if case.letter_state is CaseState.RETURNED:
                return self._pending_second_return(case, case_id)
            resolved = self._store.returned_mail_resolution(case.case_token, 1)
        if resolved is None:
            if case.letter_state is CaseState.RETURNED:
                reason_code = "RETURN_REMEDY_PENDING"
                if case.payment_state is PaymentState.NO_CHARGE:
                    # Free beta: no payment exists to confirm, so never claim one.
                    message = "Your returned letter is being closed under your free beta appeal."
                elif case.payment_state is not PaymentState.CAPTURED:
                    message = (
                        "Your letter was returned. "
                        + _no_charge_sentence(case.payment_state)
                        + " We are confirming the original payment before resolving this return."
                    )
                else:
                    message = (
                        "Your letter was returned. The returned-mail remedy is being resolved."
                    )
                    first = self._store.get(case.case_token, 1)
                    record = self._intake(case_id)
                    correction = self._store.latest_destination_decision(case_id, record.owner)
                    if (
                        first is not None
                        and correction is not None
                        and correction.decision.provenance is AddressProvenance.SYSTEM_VERIFIED
                        and correction.decision.address != first.request.recipient
                    ):
                        reason_code = "REMAIL_REVIEW_AVAILABLE"
                        message = (
                            "Your letter was returned. Check whether a corrected packet "
                            "is available to review."
                        )
                return s.ReturnedMailRemedy(
                    attempt=1,
                    kind="RETURN_PENDING",
                    reason_code=reason_code,
                    message=message,
                    refund_cents=0,
                    refund_status=None,
                    remail=False,
                )
            return None
        if case.payment_state is PaymentState.NO_CHARGE:
            return self._beta_return_closed(case, resolved)
        remedy = resolved.remedy
        refund_status: Literal["pending", "completed"] | None = None
        kind: RemedyKind | Literal["ADDRESS_EXCLUSION", "RETURN_PENDING"]
        remail_copy, remail_active = (
            self._remail_outcome_copy(case)
            if remedy is not None and remedy.remails
            else ("", False)
        )
        cost_label = (
            "estimated"
            if resolved.cost_is_estimate is True
            else "actual"
            if resolved.cost_is_estimate is False
            else "recorded"
        )
        # The arranged free re-mail, once its own submission ended definitively.
        failed: Rejected | NotSent | Canceled | None = None
        if remedy is not None and remedy.remails and case.attempt == 2:
            cursor = self._store.submission_cursor(case.case_token, 2)
            if cursor is not None and isinstance(cursor.outcome, Rejected | NotSent | Canceled):
                failed = cursor.outcome
        reason_code = resolved.reason
        refund_cents = 0 if remedy is None else remedy.refund_cents
        if remedy is None:
            kind = "ADDRESS_EXCLUSION"
            message = (
                "Your letter was returned solely because the address you supplied was wrong. "
                "No refund applies to this address-only failure under your recorded address choice."
            )
        else:
            kind = remedy.kind
            if remedy.kind in {RemedyKind.FULL_REFUND, RemedyKind.PARTIAL_REFUND}:
                if remedy.refund_cents:
                    completed = case.payment_state is PaymentState.REFUNDED
                    refund_status = "completed" if completed else "pending"
                    if remedy.refund_cents == case.amount_cents:
                        message = (
                            "Your letter was returned. Your full refund is confirmed."
                            if completed
                            else "Your letter was returned. A full refund has been requested; "
                            "it is not yet confirmed."
                        )
                    else:
                        message = (
                            "Your letter was returned. Your refund of the price minus the "
                            f"{cost_label} mailing cost is confirmed."
                            if completed
                            else "Your letter was returned. A refund of the price minus the "
                            f"{cost_label} mailing cost has been requested; "
                            "it is not yet confirmed."
                        )
                else:
                    message = (
                        f"Your letter was returned. The {cost_label} mailing cost leaves no "
                        "refundable balance under the returned-mail remedy."
                    )
            elif remedy.kind is RemedyKind.PROOF_PACKET:
                message = (
                    "Your letter was returned by the agency. Your proof packet contains the "
                    "mailed packet and recorded tracking events; it does not confirm delivery. "
                    "This return does not receive a refund."
                )
                if remedy.remails:
                    message += " " + remail_copy
            else:
                message = (
                    "Your letter was returned. " + remail_copy + " You will not be charged again."
                )
        if failed is not None:
            # The chosen free re-mail can never be completed. The ruling resolves the
            # case to one full refund of the captured amount; the copy still names what
            # actually happened, and our own dispatch refusals are our service failure.
            completed = case.payment_state is PaymentState.REFUNDED
            refund_status = "completed" if completed else "pending"
            kind = RemedyKind.FULL_REFUND
            refund_cents = case.amount_cents
            sentence = (
                "Your full refund is confirmed."
                if completed
                else (
                    "A full refund has been requested; it is not yet confirmed."
                    if self._store.returned_mail_refund(case.case_token) is not None
                    else "A full refund is pending."
                )
            )
            message = "Your letter was returned. " + remail_copy + " " + sentence
            reason_code = (
                "SERVICE_FAILURE"
                if isinstance(failed, Rejected) and failed.reason == GATE_REFUSED_REASON
                else "REMAIL_NOT_SENT"
            )
        if case.payment_state not in {PaymentState.CAPTURED, PaymentState.REFUNDED}:
            kind = "RETURN_PENDING"
            refund_cents = 0
            refund_status = None
            message = " ".join(
                part
                for part in (
                    "Your letter was returned.",
                    remail_copy,
                    _no_charge_sentence(case.payment_state),
                )
                if part
            )
        if resolved.attempt == 2:
            message = message.replace(
                "Your letter was returned", "Your free re-mail was returned", 1
            )
        return s.ReturnedMailRemedy(
            attempt=1 if resolved.attempt == 1 else 2,
            kind=kind,
            reason_code=reason_code,
            message=message,
            refund_cents=refund_cents,
            refund_status=refund_status,
            remail=remail_active,
            proof_packet=self._return_proof(case, resolved.attempt),
        )

    def _return_proof(self, case: CaseRecord, attempt: int) -> s.ReturnProofPacket | None:
        """The mailed packet's own proof, kept for an agency-side return (L1)."""
        resolved = self._store.returned_mail_resolution(case.case_token, attempt)
        if (
            resolved is None
            or resolved.responsibility is not Responsibility.AGENCY_SIDE_EXISTING_REMEDY
        ):
            return None
        return self._mailed_proof(case, attempt)

    def _mailed_proof(self, case: CaseRecord, attempt: int) -> s.ReturnProofPacket | None:
        """The exact packet Lob accepted for this attempt and its tracking events so far."""
        prepared = self._store.get(case.case_token, attempt)
        cursor = self._store.submission_cursor(case.case_token, attempt)
        if prepared is None or cursor is None or not isinstance(cursor.outcome, Accepted):
            return None
        return s.ReturnProofPacket(
            pdf_base64=base64.b64encode(prepared.request.pdf).decode("ascii"),
            pdf_sha256=hashlib.sha256(prepared.request.pdf).hexdigest(),
            letter_id=cursor.outcome.letter_id,
            tracking_number=cursor.outcome.tracking_number,
            events=[
                s.ReturnProofEvent(
                    event=data["event"],
                    occurred_at=datetime.fromisoformat(data["occurred_at"]),
                )
                for data in (
                    json.loads(payload)
                    for payload in self._store._iter_tracking_payloads(case.case_token)
                )
                if data.get("attempt") == attempt
                and data.get("letter_id") == cursor.outcome.letter_id
                and _accepted_tracking_row(data)
            ],
        )

    def _case_stop(self, case_id: str, case_token: str) -> s.Stop | None:
        """The recorded stop, in plain words, from the case's own transition history."""
        if not self._stopped(case_token):
            return None
        for item in self._store._iter_transition_history(case_token, newest_first=True):
            if item.machine != "letter" or item.target != CaseState.STOPPED_NO_CHARGE.value:
                continue
            payload = json.loads(item.event_json)
            if item.event_name == "Stopped":
                reason = StopReason(payload["reason"])
                hard_stop = HardStop(payload["hard_stop"]) if "hard_stop" in payload else None
                detail = str(payload.get("detail", ""))
            else:
                # WindowExpired, SubmitRejected, ConfirmedNotSent carry a fixed reason.
                fixed: object = getattr(getattr(case_state, item.event_name, None), "reason", None)
                if not isinstance(fixed, StopReason):
                    return None
                reason, hard_stop, detail = fixed, None, ""
            message = stop_message(
                reason,
                hard_stop,
                official_quote=detail if reason is StopReason.PROCESS_INCOMPATIBLE else None,
            )
            return s.Stop(
                case_id=case_id,
                family=stop_family(reason),
                hard_stop=hard_stop,
                reason_key=message.key,
                message=message.text,
                official_quote=detail if reason is StopReason.PROCESS_INCOMPATIBLE else None,
            )
        return None

    def step(self, case_id: str, body: s.WorkerStep) -> s.CaseStatus:
        self.refresh(case_id)
        if body.step_id == "research":
            self._research(case_id)
        elif body.step_id == "dispatch":
            self.dispatch(case_id)
        elif body.step_id == "draft":
            self._draft_step(case_id)
        else:
            raise ServiceConflict("unknown_step")
        return self.status(case_id)

    def _research(self, case_id: str) -> None:
        """Research once per case on the saved agency; the result is saved before use."""
        record = self._intake(case_id)
        case = None if record.case_token is None else self._store.get_case(record.case_token)
        if case is None or case.letter_state is not CaseState.RESEARCHING:
            return
        if self._held(case.case_token):
            # T74: an incident hold stops new paid research for this case.
            return
        # The per-case ownership a hold takes, so a hold cannot land mid-research (T74).
        with self._store.draft_ownership(case.case_token) as owned:
            if owned:
                self._research_owned(case_id)

    def _research_owned(self, case_id: str) -> None:
        # Re-read under ownership: a hold or another owner may have acted meanwhile.
        record = self._intake(case_id)
        case = None if record.case_token is None else self._store.get_case(record.case_token)
        if case is None or case.letter_state is not CaseState.RESEARCHING:
            return
        if self._held(case.case_token):
            return
        saved_research = self._store.get_research(case_id)
        if saved_research is not None:
            # A crash after saving research (or the customer's answer) but before the
            # decision leaves the case here: resolve from the saved records, once.
            self._resolve(
                case_id,
                case.case_token,
                decode_research(saved_research.research_json),
                decode_answers(saved_research.answers_json),
                saved_research.revision,
            )
            return
        if record.agency_json is None:
            return
        deps = (
            self._deps.research_for_subject(case.case_token)
            if self._deps.research_for_subject is not None
            else self._deps.research
        )
        if deps is None:
            raise ServiceConflict("research_not_configured")
        intake = s.SubmitIntake.model_validate_json(record.intake_json)
        ticket = intake.ticket
        now = self._deps.clock()
        agency = decode_agency(record.agency_json)
        research_ticket = Ticket(
            f"{ticket.issued_city} {ticket.issued_state}",
            ticket.violation_code,
            ticket.violation_description,
            ticket.issue_date,
            stage=ticket.stage,
        )
        candidates: list[ResearchResult] = []
        try:
            recent = self._store.recent_research_json(now - RESEARCH_REUSE_WINDOW, case_id, 10)
        except StorageCorruption:
            # A different case's cache cannot prevent this case's fresh research.
            recent = ()
        for raw in recent:
            try:
                candidates.append(decode_research(raw))
            except ValueError:
                continue
        try:
            reused = True
            result = reuse_research(
                candidates,
                research_ticket,
                agency,
                deps.fetcher,
                self._deps.clock,
                now,
                packet_plan=_intake_form_plan(intake),
                remaining_time=remaining(case.authorized_at, now),
            )
            if result is None:
                reused = False
                result = run_research(
                    research_ticket,
                    agency,
                    known_seeds(agency, deps.known_records),
                    deps.fetcher,
                    deps.extractor,
                    self._deps.clock,
                    searcher=deps.searcher,
                    recovery=deps.recovery,
                    wait=deps.wait,
                    max_cost_cents=deps.budget_cents,
                    stage=ticket.stage,
                    remaining_time=remaining(case.authorized_at, self._deps.clock()),
                    packet_plan=_intake_form_plan(intake),
                )
        except PROVIDER_ERRORS:
            # Failed research is a verification stop, never "unsupported" (research tiers).
            self._apply(
                case.case_token, Stopped(StopReason.NOT_YET_VERIFIED, hard_stop=HardStop.S1)
            )
            self._log_research_call(case.case_token, deps.model_label, None, reused=False)
            return
        answers = intake_answers(intake)
        saved = self._store.save_research(
            case_id, research_json(result), answers_json(answers), self._deps.clock(), None
        )
        self._log_research_call(case.case_token, deps.model_label, result, reused=reused)
        self._record_tricks(agency.id, result, deps.tricks)
        self._resolve(case_id, case.case_token, result, answers, saved.revision)

    def _record_tricks(
        self, place_key: str, result: ResearchResult, catalog: tuple[Trick, ...]
    ) -> None:
        """Best effort, after research is saved: sightings never change or block the appeal."""
        if not catalog:
            return
        try:
            pages = [SavedPage(s.final_url, s.sha256, s.text) for s in result.sources]
            report = hunt(place_key, pages, catalog)
            if report.sightings:
                self._store.record_trick_sightings(place_key, report.sightings, self._deps.clock())
        except Exception:  # the hunter and its store must never interrupt the appeal
            return

    def _log_research_call(
        self, case_token: str, model: str, result: ResearchResult | None, *, reused: bool
    ) -> None:
        """Best effort, after the research outcome is saved: logging never changes the appeal."""
        if result is None:
            status, note, usage = (
                AiCallStatus.FALLBACK_AFTER_FAILURE,
                AiNote.SOURCE_MISSING,
                Usage(),
            )
        else:
            status = AiCallStatus.SKIPPED if reused else AiCallStatus.OK
            note = AiNote.SOURCE_FOUND if result.sources else AiNote.SOURCE_MISSING
            usage = Usage() if reused else result.cost
        try:
            call = AiCall(
                AiWorker.RESEARCH,
                model,
                status,
                usage.input_tokens,
                usage.output_tokens,
                usage.cost_cents,
            )
            now = self._deps.clock()
            if self._deps.research_for_subject is None:
                self._store.try_append_ai_call(case_token, call, now)
            self._store.try_append_ai_journal(
                case_token,
                AiJournalEntry(
                    AiWorker.RESEARCH,
                    note,
                    status,
                    0 if reused or result is None else 1,
                    usage.input_tokens,
                    usage.output_tokens,
                ),
                now,
            )
        except Exception:  # logging must never interrupt the appeal
            return

    def _resolve(
        self,
        case_id: str,
        case_token: str,
        result: ResearchResult,
        answers: ResearchAnswers,
        revision: int,
    ) -> None:
        # Unsupported closed-list questions remain unknown. Visible factual questions
        # require the customer's explicit answer through the existing question card.
        lane_code: str | None = None
        for _ in CustomerQuestion:
            effective, lane_code = _without_mailing_stops(result.resolve(answers))
            event = resolution_event(effective)
            if not (
                isinstance(event, CustomerNeeded)
                and event.question not in _VISIBLE_QUESTIONS
                and event.question in CustomerQuestion.__members__
            ):
                break
            answers = answer_question(answers, CustomerQuestion(event.question), None)
            revision = self._store.save_research(
                case_id,
                research_json(result),
                answers_json(answers),
                self._deps.clock(),
                revision,
            ).revision
        if (
            event is None
            and lane_code is not None
            and not effective.pending
            and effective.deadline is not None
            and effective.counting is not None
            and not effective.counting.disclose
            and self._customer_directed_current(self._intake(case_id))
        ):
            # Only the mailing address was unverifiable, and the customer has explicitly
            # accepted the current disclosure for their own current address.
            event = ResearchComplete()
        if event is not None:
            if isinstance(event, ResearchComplete) and not effective.deadline_feasible(
                production_date(self._deps.clock()), MailClass.CERTIFIED
            ):
                event = Stopped(StopReason.NOT_FEASIBLE)
            self._apply(case_token, event)

    def _apply(self, case_token: str, event: Event) -> None:
        case = self._store.get_case(case_token)
        if case is None:
            raise ServiceConflict("not_found")
        self._store.apply_letter_event(
            case_token,
            event,
            case.revision,
            occurred_at=self._deps.clock(),
            new_effect_id=lambda: f"eff_{self._deps.new_id()}",
        )

    def _open_question(self, case_token: str) -> s.CaseQuestion | None:
        # Use the durable question event, including after an answer-save crash. Saved
        # answers may already imply a later question whose event has not committed yet.
        for item in self._store._iter_transition_history(case_token, newest_first=True):
            if item.machine != "letter":
                continue
            if item.event_name != "CustomerNeeded" or item.target != CaseState.NEEDS_CUSTOMER.value:
                return None
            question_id = json.loads(item.event_json).get("question")
            if question_id == "VEHICLE_RELATIONSHIP":
                return VEHICLE_QUESTION
            return _PAYMENT_QUESTIONS.get(question_id)
        return None

    def answer_question(self, case_id: str, body: s.CaseAnswer) -> s.CaseStatus:
        record = self._intake(case_id)
        case = None if record.case_token is None else self._store.get_case(record.case_token)
        saved = self._store.get_research(case_id)
        if case is None or saved is None or case.letter_state is not CaseState.NEEDS_CUSTOMER:
            raise ServiceConflict("no_open_question")
        current_question = self._open_question(case.case_token)
        if current_question is None or current_question.question_id != body.question_id:
            raise ServiceConflict("question_mismatch")
        previous_answers = decode_answers(saved.answers_json)
        question = CustomerQuestion(body.question_id)
        answers = answer_question(previous_answers, question, body.answer)
        if question in previous_answers.answered and answers != previous_answers:
            raise ServiceConflict("question_mismatch")
        result = decode_research(saved.research_json)
        revision = (
            saved.revision
            if answers == previous_answers
            else self._store.save_research(
                case_id,
                saved.research_json,
                answers_json(answers),
                self._deps.clock(),
                saved.revision,
            ).revision
        )
        self._apply(case.case_token, CustomerAnswered())
        try:
            self._resolve(case_id, case.case_token, result, answers, revision)
        except (IllegalTransition, RevisionConflict, IntakeConflict):
            # The scheduler may resolve the saved answer between our commit and this call.
            current = self._store.get_case(case.case_token)
            if current is None or current.letter_state is CaseState.RESEARCHING:
                raise
        return self.status(case_id)

    def _required_copy_state(
        self, record: IntakeRecord, research: SavedResearch
    ) -> tuple[s.RequiredCopiesView | None, tuple[RequiredCopyPages, ...]]:
        result = decode_research(research.research_json)
        items = required_copy_items(result.findings, result.sources, result.ticket)
        if not items:
            return None, ()
        digest = hashlib.sha256(research.research_json.encode()).hexdigest()
        stored = self._store.get_required_copies(record.intake_id, record.owner)
        selections = (
            []
            if stored is None or stored.research_sha256 != digest
            else _COPY_SELECTIONS.validate_json(stored.selections_json)
        )
        by_id = {selection.requirement_id: selection for selection in selections}
        pages: list[RequiredCopyPages] = []
        views: list[s.RequiredCopyItem] = []
        for item in items:
            selection = by_id.get(item.requirement_id)
            refs = [] if selection is None else selection.page_refs
            images = []
            for ref in refs:
                data = self._deps.photos.get(record.owner, ref)
                if data is None or photo_ref(data) != ref:
                    break
                images.append(data)
            if refs and len(images) == len(refs):
                pages.append(RequiredCopyPages(item.requirement_id, tuple(images)))
            views.append(
                s.RequiredCopyItem(
                    requirement_id=item.requirement_id,
                    title=item.title,
                    official_quote=item.quote,
                    source_urls=[
                        source.final_url
                        for source in result.sources
                        if source.sha256 in item.source_sha256s
                    ],
                    page_refs=refs,
                )
            )
        try:
            validated_copy_pages(items, pages)
            ready = len(by_id) == len(items)
        except ValueError:
            ready = False
        case = None if record.case_token is None else self._store.get_case(record.case_token)
        editable = (
            case is not None
            and case.letter_state in _CHOICE_STATES
            and self._store.get_draft(record.intake_id) is None
            and not self._store.has_destination_lock(record.intake_id, record.owner)
        )
        return (
            s.RequiredCopiesView(
                research_sha256=digest,
                revision=0 if stored is None else stored.revision,
                items=views,
                ready=ready,
                editable=editable,
            ),
            tuple(pages),
        )

    def save_required_copies(self, case_id: str, body: s.SaveRequiredCopies) -> s.CaseStatus:
        self.refresh(case_id)
        record = self._intake(case_id)
        research = self._store.get_research(case_id)
        if research is None:
            raise ServiceConflict("research_not_ready")
        view, _ = self._required_copy_state(record, research)
        if view is None:
            raise ServiceConflict("required_copies_not_available")
        if body.research_sha256 != view.research_sha256:
            raise ServiceConflict("required_copies_research_stale")
        allowed = {item.requirement_id for item in view.items}
        seen: set[str] = set()
        if sum(len(selection.page_refs) for selection in body.selections) > 20:
            raise ServiceConflict("required_copies_limit")
        for selection in body.selections:
            if selection.requirement_id not in allowed or selection.requirement_id in seen:
                raise ServiceConflict("required_copy_mismatch")
            seen.add(selection.requirement_id)
            for ref in selection.page_refs:
                data = self._deps.photos.get(record.owner, ref)
                if data is None or photo_ref(data) != ref:
                    raise ServiceConflict("required_copy_photo_missing")
        try:
            self._store.save_required_copies(
                case_id,
                record.owner,
                body.research_sha256,
                _COPY_SELECTIONS.dump_json(body.selections).decode(),
                body.expected_revision,
                self._deps.clock(),
            )
        except IntakeConflict as exc:
            raise ServiceConflict(str(exc)) from None
        return self.status(case_id)

    def required_copy_page(self, case_id: str, requirement_id: str, page_number: int) -> bytes:
        record = self._intake(case_id)
        research = self._store.get_research(case_id)
        if research is not None:
            view, _ = self._required_copy_state(record, research)
            if view is not None:
                for item in view.items:
                    if item.requirement_id == requirement_id and 1 <= page_number <= len(
                        item.page_refs
                    ):
                        ref = item.page_refs[page_number - 1]
                        data = self._deps.photos.get(record.owner, ref)
                        if data is not None and photo_ref(data) == ref:
                            return data
        raise ServiceConflict("required_copy_page_not_found")

    def _intake(self, case_id: str) -> IntakeRecord:
        record = self._store.get_intake(case_id)
        if record is None:
            raise ServiceConflict("not_found")
        return record

    def _mailing_address(self, research: ResearchResult) -> MailingAddress:
        findings = verified_findings(research.findings, research.sources, research.ticket)
        addresses = [f.value for f in findings if f.key is FactKey.MAILING_ADDRESS]
        if len(addresses) != 1 or not isinstance(addresses[0], dict):
            raise ServiceConflict("mailing_address_not_confirmed")
        value = addresses[0]
        try:
            return MailingAddress(
                recipient=str(value["recipient"]),
                attention=str(value["attention"]) if value.get("attention") else None,
                line1=str(value["line1"]),
                line2=str(value["line2"]) if value.get("line2") else None,
                city=str(value["city"]),
                state=str(value["state"]),
                zip_code=str(value["zip_code"]),
            )
        except (KeyError, TypeError, ValueError):
            raise ServiceConflict("mailing_address_not_confirmed") from None

    def _save_draft(
        self,
        case_id: str,
        draft: SavedDraft,
        revision: int | None,
        *,
        expected_research_revision: int | None = None,
    ) -> SavedDraft:
        try:
            return self._store.save_draft(
                case_id, draft, revision, expected_research_revision=expected_research_revision
            )
        except IntakeConflict as exc:
            raise ServiceConflict(str(exc)) from None

    def _destination_address(
        self, record: IntakeRecord, research: ResearchResult
    ) -> MailingAddress:
        saved = self._store.latest_destination_decision(record.intake_id, record.owner)
        if (
            saved is not None
            and saved.decision.provenance is AddressProvenance.CUSTOMER_DIRECTED_UNVERIFIED
        ):
            raise ServiceConflict("destination_verification_required")
        if saved is None:
            address = self._mailing_address(research)
            self._refuse_payment_address(record, address)
            official = {
                source.sha256
                for source in research.sources
                if source.authority_class is not AuthorityClass.SECONDARY
                and source.http_status == 200
            }
            hashes = {
                sha
                for finding in verified_findings(
                    research.findings, research.sources, research.ticket
                )
                if finding.key is FactKey.MAILING_ADDRESS
                for sha in finding.source_sha256s
                if sha in official
            }
            decision = DestinationDecision(
                record.intake_id,
                record.owner,
                address,
                address_fingerprint(address),
                AddressProvenance.SYSTEM_VERIFIED,
                ADDRESS_REFUND_POLICY_VERSION,
                self._deps.clock(),
                verification_evidence=tuple(
                    EvidenceRef(sha, sha, EvidenceKind.OFFICIAL_SOURCE_QUOTE)
                    for sha in sorted(hashes)
                ),
            )
            try:
                saved = self._store.save_destination_decision(
                    record.intake_id, record.owner, decision, None
                )
            except IntakeConflict as exc:
                raise ServiceConflict(str(exc)) from None
        return saved.decision.address

    # Customer-directed destination (T30) ---------------------------------------------------
    def _unverified_code(self, record: IntakeRecord, case: CaseRecord) -> str | None:
        """Why the system cannot verify this case's destination now, or None.

        None also when a SYSTEM_VERIFIED decision is saved or research has not run yet.
        """
        saved = self._store.latest_destination_decision(record.intake_id, record.owner)
        if saved is not None and saved.decision.provenance is AddressProvenance.SYSTEM_VERIFIED:
            return None
        research = self._store.get_research(record.intake_id)
        if research is None:
            return None
        result = decode_research(research.research_json)
        if case.letter_state in {CaseState.RESEARCHING, CaseState.NEEDS_CUSTOMER}:
            return _without_mailing_stops(result.resolve(decode_answers(research.answers_json)))[1]
        try:
            self._mailing_address(result)
        except ServiceConflict as exc:
            return str(exc)
        return None

    def _customer_directed_current(self, record: IntakeRecord) -> bool:
        """The saved customer-directed decision still stands: same consent, latest address
        revision and the current disclosure. Uses the same pure gate as the mailing boundary."""
        saved = self._store.latest_destination_decision(record.intake_id, record.owner)
        if saved is None:
            return False
        decision = saved.decision
        if (
            decision.provenance is not AddressProvenance.CUSTOMER_DIRECTED_UNVERIFIED
            or decision.disclosure is None
            or decision.disclosure.sha256 != DISCLOSURE_SHA256
        ):
            return False
        addresses = self._store.customer_address_revisions(record.intake_id, record.owner)
        return mailing_gate(
            decision,
            self._store.address_consent_events(record.intake_id, record.owner),
            case_id=record.intake_id,
            owner=record.owner,
            mailed_address=decision.address,
            latest_customer_address_revision=addresses[-1].revision if addresses else None,
            now=self._deps.clock(),
            current_disclosure_sha256=DISCLOSURE_SHA256,
        ).allowed

    def _destination_choice(
        self, record: IntakeRecord, case: CaseRecord, draft_saved: bool
    ) -> s.DestinationChoice | None:
        # Preserve saved consents, but never offer a new unverified send instruction.
        del record, case, draft_saved
        return None

    def _destination_locked(self, record: IntakeRecord) -> bool:
        return self._store.has_destination_lock(record.intake_id, record.owner)

    def _open_choice(self, case_id: str) -> tuple[IntakeRecord, CaseRecord, str]:
        """The case with a destination choice that may be answered now, and its reason code."""
        self._intake(case_id)  # owner scope before policy refusal; no state mutation
        raise ServiceConflict("destination_verification_required")

    def _refuse_payment_address(self, record: IntakeRecord, address: MailingAddress) -> None:
        """Refuse a customer address that the case's official research lists for payment.

        Read from the saved text of official sources only (no new retention); an address that
        official text also offers for contests is not a payment address. Fixed code, no echo.
        """
        research = self._store.get_research(record.intake_id)
        if research is None:
            return
        sources = decode_research(research.research_json).sources
        known = frozenset().union(
            *(
                payment_addresses_in_text(source.text)
                for source in sources
                if source.authority_class is not AuthorityClass.SECONDARY
                and source.http_status == 200
            )
        )
        if payment_address_matches(address, known):
            raise ServiceConflict("destination_address_is_payment_address")

    def destination_disclosure(self, case_id: str) -> s.DestinationDisclosure:
        self._intake(case_id)
        return s.DestinationDisclosure(
            version=DISCLOSURE_VERSION, sha256=DISCLOSURE_SHA256, text=DISCLOSURE_TEXT
        )

    def save_destination_address(
        self, case_id: str, body: s.DestinationAddress
    ) -> s.DestinationAddressReceipt:
        """Save the customer's address as a new revision; a different address supersedes (and
        so invalidates) any earlier consent. The same address again is a no-op."""
        record, _case, _code = self._open_choice(case_id)
        address = MailingAddress(
            recipient=body.recipient,
            attention=body.attention,
            line1=body.line1,
            line2=body.line2,
            city=body.city,
            state=body.state,
            zip_code=body.zip_code,
        )
        if not address_is_complete(address):
            raise ServiceConflict("destination_address_incomplete")
        self._refuse_payment_address(record, address)
        history = self._store.customer_address_revisions(record.intake_id, record.owner)
        if history and history[-1].address == address:
            saved_revision = history[-1].revision
        else:
            try:
                saved_revision = self._store.save_customer_address(
                    record.intake_id,
                    record.owner,
                    address,
                    self._deps.clock(),
                    history[-1].revision if history else None,
                ).revision
            except IntakeConflict as exc:
                raise ServiceConflict(str(exc)) from None
        return s.DestinationAddressReceipt(
            case_id=case_id,
            address_revision=saved_revision,
            address_fingerprint=address_fingerprint(address),
        )

    def _consent_receipt(
        self, case_id: str, decision: DestinationDecision
    ) -> s.DestinationConsentReceipt:
        consent, disclosure = decision.consent, decision.disclosure
        assert consent is not None
        assert disclosure is not None
        return s.DestinationConsentReceipt(
            case_id=case_id,
            address_revision=consent.address_revision,
            address_fingerprint=consent.address_fingerprint,
            disclosure_version=disclosure.version,
            disclosure_sha256=disclosure.sha256,
            accepted_at=consent.accepted_at,
        )

    def _existing_consent(
        self, record: IntakeRecord, body: s.DestinationConsent
    ) -> DestinationDecision | None:
        """The saved customer-directed decision that this exact request already produced."""
        saved = self._store.latest_destination_decision(record.intake_id, record.owner)
        if saved is None:
            return None
        decision = saved.decision
        consent = decision.consent
        if (
            decision.provenance is AddressProvenance.CUSTOMER_DIRECTED_UNVERIFIED
            and consent is not None
            and consent.address_revision == body.address_revision
            and consent.address_fingerprint == body.address_fingerprint
            and consent.disclosure_sha256 == body.disclosure_sha256
            and self._customer_directed_current(record)
        ):
            return decision
        return None

    def consent_destination(
        self, case_id: str, body: s.DestinationConsent
    ) -> s.DestinationConsentReceipt:
        """Record an explicit acceptance and persist the policy's CUSTOMER_DIRECTED decision.

        Nothing is inferred: the request must carry accepted=true, the current disclosure
        hash and the exact latest address revision and fingerprint. The paid drafting call
        happens only after this decision is saved, on a later step or sweep.
        """
        self.refresh(case_id)
        record = self._intake(case_id)
        if body.accepted is not True:
            raise ServiceConflict("destination_consent_not_accepted")
        replay = self._existing_consent(record, body)
        if replay is not None:
            return self._consent_receipt(case_id, replay)
        record, _case, code = self._open_choice(case_id)
        if not hmac.compare_digest(body.disclosure_sha256, DISCLOSURE_SHA256):
            raise ServiceConflict("destination_disclosure_stale")
        addresses = self._store.customer_address_revisions(record.intake_id, record.owner)
        if not addresses:
            raise ServiceConflict("destination_address_required")
        latest = addresses[-1]
        if body.address_revision != latest.revision:
            raise ServiceConflict("destination_address_stale")
        self._refuse_payment_address(record, latest.address)
        fingerprint = address_fingerprint(latest.address)
        if not hmac.compare_digest(body.address_fingerprint, fingerprint):
            raise ServiceConflict("destination_fingerprint_mismatch")
        consent = next(
            (
                event
                for event in reversed(
                    self._store.address_consent_events(record.intake_id, record.owner)
                )
                if event.address_revision == latest.revision
                and event.address_fingerprint == fingerprint
                and event.disclosure_sha256 == DISCLOSURE_SHA256
            ),
            None,
        )
        if consent is None:
            consent = ConsentEvent(
                record.intake_id,
                record.owner,
                fingerprint,
                DISCLOSURE_SHA256,
                latest.revision,
                True,
                self._deps.clock(),
            )
            try:
                self._store.record_address_consent(record.intake_id, record.owner, consent)
            except IntakeConflict as exc:
                raise ServiceConflict(str(exc)) from None
        outcome = decide_destination(
            DestinationFacts(
                case_id=record.intake_id,
                owner=record.owner,
                decided_at=max(self._deps.clock(), consent.accepted_at),
                refund_policy_version=ADDRESS_REFUND_POLICY_VERSION,
                research=ResearchVerification(ResearchClaim.NOT_VERIFIED, failure_code=code),
                # The packet and the remaining gates come later; the final mailing gate
                # re-checks this decision against the exact outgoing bytes.
                other_gates_passed=False,
                customer_address=latest.address,
                customer_address_revision=latest.revision,
                consent=consent,
                current_disclosure=Disclosure(DISCLOSURE_VERSION, DISCLOSURE_SHA256),
            )
        )
        if outcome.decision is None or outcome.kind is not OutcomeKind.WAIT_OTHER_GATES:
            raise ServiceConflict("destination_consent_refused")
        previous = self._store.latest_destination_decision(record.intake_id, record.owner)
        try:
            self._store.save_destination_decision(
                record.intake_id,
                record.owner,
                outcome.decision,
                None if previous is None else previous.revision,
            )
        except IntakeConflict as exc:
            # A concurrent identical request may have won; anything else is a real conflict.
            replay = self._existing_consent(record, body)
            if replay is None:
                raise ServiceConflict(str(exc)) from None
            return self._consent_receipt(case_id, replay)
        return self._consent_receipt(case_id, outcome.decision)

    def _stopped(self, case_token: str) -> bool:
        """Only the terminal STOPPED_NO_CHARGE state has a recorded stop; checking the case
        first keeps every live case's status from reading its history at all (T18)."""
        case = self._store.get_case(case_token)
        return case is not None and case.letter_state is CaseState.STOPPED_NO_CHARGE

    def _declined(self, case_token: str) -> bool:
        return self._stopped(case_token) and any(
            item.machine == "letter"
            and item.event_name == "Stopped"
            and json.loads(item.event_json).get("detail") == DESTINATION_DECLINED
            for item in self._store._iter_transition_history(case_token, newest_first=True)
        )

    def decline_destination(self, case_id: str) -> s.CaseStatus:
        """Stop with no charge and release the hold, through the existing stop path."""
        self.refresh(case_id)
        record = self._intake(case_id)
        case = None if record.case_token is None else self._store.get_case(record.case_token)
        if case is None:
            raise ServiceConflict("case_not_started")
        if case.letter_state is CaseState.STOPPED_NO_CHARGE:
            if self._declined(case.case_token):
                return self.status(case_id)
            raise ServiceConflict("case_stopped")
        if case.letter_state not in _DECLINE_STATES:
            raise ServiceConflict("destination_choice_not_open")
        code = self._unverified_code(record, case)
        if code is None:
            raise ServiceConflict("destination_choice_not_required")
        outcome = decide_destination(
            DestinationFacts(
                case_id=record.intake_id,
                owner=record.owner,
                decided_at=self._deps.clock(),
                refund_policy_version=ADDRESS_REFUND_POLICY_VERSION,
                research=ResearchVerification(ResearchClaim.NOT_VERIFIED, failure_code=code),
                other_gates_passed=False,
                customer_declined=True,
            )
        )
        if outcome.kind is not OutcomeKind.STOP_NO_CAPTURE_RELEASE_HOLD or not outcome.release_hold:
            raise ServiceConflict("destination_decline_refused")
        try:
            self._apply(case.case_token, Stopped(StopReason.DECLINED, DESTINATION_DECLINED))
        except (IllegalTransition, RevisionConflict):
            raise ServiceConflict("destination_choice_not_open") from None
        return self.status(case_id)

    def _check_destination(self, record: IntakeRecord, request: LetterRequest) -> None:
        saved = self._store.latest_destination_decision(record.intake_id, record.owner)
        if saved is None or saved.decision.fingerprint != address_fingerprint(request.recipient):
            raise ServiceConflict("destination_decision_mismatch")
        if saved.decision.provenance is AddressProvenance.CUSTOMER_DIRECTED_UNVERIFIED:
            raise ServiceConflict("destination_verification_required")

    def _draft_step(self, case_id: str) -> None:
        record = self._intake(case_id)
        case = None if record.case_token is None else self._store.get_case(record.case_token)
        if case is None or case.letter_state is not CaseState.DRAFTING:
            return
        if self._held(case.case_token):
            # T74: an incident hold stops new paid drafting for this case.
            return
        # One paid drafting call per case at a time, across threads and processes.
        with self._store.draft_ownership(case.case_token) as owned:
            if owned:
                self._draft_step_owned(case_id)

    def _draft_step_owned(self, case_id: str) -> None:
        # Re-read under ownership: another owner may have drafted or stopped meanwhile.
        record = self._intake(case_id)
        case = None if record.case_token is None else self._store.get_case(record.case_token)
        if case is None or case.letter_state is not CaseState.DRAFTING:
            return
        if self._held(case.case_token):
            # T74: a hold recorded while we waited for ownership still wins.
            return
        # A crash after saving the result but before the machine event must not redraft.
        saved = self._store.get_draft(case_id)
        if saved is not None:
            if not saved.letter_text and _SPANS.validate_json(saved.spans_json):
                # Paid output kept after it failed print validation: never ready, never redrafted.
                raise ServiceConflict("letter_not_printable")
            self._apply(case.case_token, DraftReady())
            return
        research = self._store.get_research(case_id)
        workspace = self._store.get_workspace(record.workspace_token)
        drafter = (
            self._deps.drafter_for_subject(case.case_token)
            if self._deps.drafter_for_subject is not None
            else self._deps.drafter
        )
        if research is None or workspace is None or record.agency_json is None:
            raise ServiceConflict("research_not_ready")
        copy_view, _ = self._required_copy_state(record, research)
        if copy_view is not None and not copy_view.ready:
            # Collection is customer work, never a paid drafting attempt or a stop.
            return
        if drafter is None:
            raise ServiceConflict("drafting_not_configured")
        intake = s.SubmitIntake.model_validate_json(record.intake_json)
        ticket = intake.ticket
        result = decode_research(research.research_json)
        agency = decode_agency(record.agency_json)
        # Researched code quotations are retired. Legacy saved provision selections stay
        # inert: they are never re-validated into an injected citation, and the
        # customer-authored letter needs no reference objects.
        references: Sequence[Reference] = ()
        notes = "\n\n".join(
            text for text in (workspace.workspace.notes, workspace.workspace.position) if text
        )
        if isinstance(drafter, LlmDrafter):
            contact = intake.contact
            address = contact.mailing_address
            drafter = drafter.with_sensitive_strings(
                (
                    *customer_name_sensitive_strings(contact.name),
                    contact.phone,
                    contact.email,
                    address.line1,
                    address.line2,
                    address.city,
                    address.zip_code,
                )
            )
        # A check that needs no draft text must fail before any paid drafting call.
        mailing: MailingAddress | None = None
        try:
            mailing = self._destination_address(record, result)
        except ServiceConflict as exc:
            drafter = _RefusedDrafter(str(exc))
        if mailing is not None:
            # Commit the permanent destination lock and revalidate in its write
            # transaction before any paid drafting call. Never use the earlier snapshot.
            try:
                mailing = self._store.lock_destination_for_draft(
                    record.intake_id,
                    record.owner,
                    self._deps.clock(),
                    DISCLOSURE_SHA256,
                    required_copy_revision=None if copy_view is None else copy_view.revision,
                    required_copy_research_sha256=None
                    if copy_view is None
                    else copy_view.research_sha256,
                    expected_research_revision=research.revision,
                ).decision.address
            except IntakeConflict as exc:
                raise ServiceConflict(str(exc)) from None
        revision = self._deps.new_id()
        # Deterministic authorship makes no provider call, so it must not consume or be
        # blocked by the paid draft-run allowance. Window and revision guards still apply.
        if not isinstance(drafter, _RefusedDrafter) and not getattr(
            drafter, "deterministic", False
        ):
            run = self._store.begin_draft_run(
                case.case_token, research.revision, self._deps.clock()
            )
            if isinstance(run, DraftRunRefused):
                # A recorded refusal whose stop never committed, or the normal and the one
                # recovery run are used: make no further paid call. The case waits for its
                # existing window stop.
                return
            drafter = _RecordedDrafter(
                drafter,
                self._store,
                case.case_token,
                research.revision,
                _draft_context_sha256(ticket, copy_view, mailing),
                self._deps.clock,
                revision,
            )
        try:
            drafted = run_drafting(
                notes,
                references,
                TicketFacts(
                    ticket.citation_number,
                    ticket.license_plate,
                    ticket.issue_date,
                    ticket.violation_code,
                    ticket.violation_description,
                ),
                drafter,
                self._deps.clock,
                case.authorized_at,
                lambda: agency,
                new_revision=lambda: revision,
            )
        except (LlmProviderError, LlmBudgetExceeded):
            self._apply(case.case_token, Stopped(StopReason.DRAFTING_UNAVAILABLE))
            return
        except DraftCallsExhausted:
            # Earlier calls ended unknown (a crash mid-call) and the recovery allowance is
            # used: make no further paid call. The case waits for its existing expiry stop.
            return
        if drafted.status is DraftingStatus.MODE_BLOCKED:
            self._apply(case.case_token, Stopped(StopReason.BUSINESS_EXCLUSION))
            return
        contact = intake.contact
        address = contact.mailing_address
        text = ""
        printable = True
        if drafted.spans:
            assert mailing is not None
            try:
                text = render_letter(
                    LetterInput(
                        Sender(
                            contact.name,
                            (
                                address.line1,
                                address.line2,
                                f"{address.city}, {address.state} {address.zip_code}",
                            ),
                            contact.phone,
                            contact.email,
                        ),
                        self._deps.clock().date(),
                        agency,
                        mailing,
                        ticket.citation_number,
                        ticket.license_plate,
                        Draft(
                            tuple(
                                DraftSentence(span.text, span.support, span.paragraph)
                                for span in drafted.spans
                            )
                        ),
                        research=result,
                        encloses_citation=intake.citation_photo_ref is not None,
                        evidence_photos=len(intake.photo_refs),
                        encloses_citation_back=intake.citation_back_photo_ref is not None,
                    )
                ).text
            except LetterError:
                printable = False
        saved = SavedDraft(
            text,
            _SPANS.dump_json(drafted.spans).decode(),
            _FLAGS.dump_json(drafted.flags).decode(),
            None,
            None,
            None,
            None,
            self._deps.clock(),
            1,
        )
        # Save the paid output before validation fails, so the next sweep reuses it.
        self._save_draft(case_id, saved, None, expected_research_revision=research.revision)
        if not printable:
            raise ServiceConflict("letter_not_printable")
        self._apply(
            case.case_token,
            WindowExpired() if drafted.status is DraftingStatus.WINDOW_EXPIRED else DraftReady(),
        )

    def draft(self, case_id: str) -> s.Draft:
        self._intake(case_id)
        saved = self._store.get_draft(case_id)
        if saved is None or not saved.letter_text:
            raise ServiceConflict("draft_not_ready")
        return s.Draft(
            case_id=case_id,
            revision=saved.revision,
            text=saved.letter_text,
            text_sha256=text_sha256(saved.letter_text),
            adopted_sha256=saved.adopted_sha256,
            adoption_screen_text=FINAL_SCREEN_TEXT,
            spans=list(_SPANS.validate_json(saved.spans_json)),
            flags=list(_FLAGS.validate_json(saved.flags_json)),
        )

    def edit(self, case_id: str, body: s.EditDraft) -> s.Draft:
        self.refresh(case_id)
        record = self._intake(case_id)
        case = None if record.case_token is None else self._store.get_case(record.case_token)
        saved = self._store.get_draft(case_id)
        if (
            case is None
            or case.letter_state is not CaseState.AWAITING_ADOPTION
            or saved is None
            or saved.packet_sha256 is not None
            or self._store.get(case.case_token, 1) is not None
        ):
            raise ServiceConflict("draft_not_editable")
        saved_flags = _FLAGS.validate_json(saved.flags_json)
        has_omissions = any(
            detail.startswith("omitted customer text:")
            for flag in saved_flags
            for detail in flag.details
        )
        # A lost response can safely replay an already-saved request. An unresolved
        # same-text replacement is a new intent and must still pass the hash guard.
        if saved.letter_text == body.text and (
            not body.replace_customer_content or not has_omissions
        ):
            return self.draft(case_id)
        if body.expected_sha256 != text_sha256(saved.letter_text):
            raise ServiceConflict("stale_draft")
        if not is_printable(body.text):
            raise ServiceConflict("customer_statement_not_printable")
        spans = edited_spans(
            saved.letter_text,
            body.text,
            _SPANS.validate_json(saved.spans_json),
            self._deps.new_id(),
        )
        workspace = self._store.get_workspace(record.workspace_token)
        if workspace is None:
            raise ServiceConflict("not_found")
        notes = "\n\n".join(
            value for value in (workspace.workspace.notes, workspace.workspace.position) if value
        )
        flags = flags_after_edit(
            saved_flags,
            spans,
            body.text,
            notes,
            replace_customer_content=body.replace_customer_content,
        )
        self._save_draft(
            case_id,
            replace(
                saved,
                letter_text=body.text,
                spans_json=_SPANS.dump_json(spans).decode(),
                flags_json=_FLAGS.dump_json(flags).decode(),
                adopted_sha256=None,
                adopted_at=None,
                adopted_by=None,
                updated_at=self._deps.clock(),
            ),
            saved.revision,
        )
        self._store.save_workspace(
            record.workspace_token,
            replace(
                workspace.workspace, letter_edits=(*workspace.workspace.letter_edits, body.text)
            ),
            self._deps.clock(),
            expected_revision=workspace.revision,
        )
        return self.draft(case_id)

    def adopt_text(self, case_id: str, body: s.AdoptText) -> s.AdoptionReceipt:
        self.refresh(case_id)
        record = self._intake(case_id)
        saved = self._store.get_draft(case_id)
        if (
            saved is None
            or body.text != saved.letter_text
            or body.text_sha256 != text_sha256(saved.letter_text)
        ):
            raise ServiceConflict("stale_draft")
        case = None if record.case_token is None else self._store.get_case(record.case_token)
        if case is None or (
            case.letter_state is not CaseState.AWAITING_ADOPTION and saved.adopted_sha256 is None
        ):
            raise ServiceConflict("draft_not_adoptable")
        if _FLAGS.validate_json(saved.flags_json):
            raise ServiceConflict("draft_needs_review")
        if saved.adopted_sha256 is None:
            adoption = adopt(saved.letter_text, record.owner, self._deps.clock)
            saved = self._save_draft(
                case_id,
                replace(
                    saved,
                    adopted_sha256=adoption.sha256,
                    adopted_at=adoption.adopted_at,
                    adopted_by=adoption.customer_id,
                    updated_at=self._deps.clock(),
                ),
                saved.revision,
            )
        assert saved.adopted_at is not None
        assert saved.adopted_sha256 is not None
        return s.AdoptionReceipt(
            case_id=case_id, adopted_sha256=saved.adopted_sha256, adopted_at=saved.adopted_at
        )

    def _adopted_context(
        self, case_id: str, adopted_sha256: str
    ) -> tuple[IntakeRecord, SavedDraft, CaseRecord]:
        self.refresh(case_id)
        record = self._intake(case_id)
        saved = self._store.get_draft(case_id)
        if (
            saved is None
            or saved.adopted_sha256 != adopted_sha256
            or text_sha256(saved.letter_text) != adopted_sha256
        ):
            raise ServiceConflict("adopt_current_text_first")
        case = None if record.case_token is None else self._store.get_case(record.case_token)
        if case is None:
            raise ServiceConflict("case_not_approvable")
        return record, saved, case

    def _frozen_packet(
        self,
        record: IntakeRecord,
        saved: SavedDraft,
        signature: bytes | None,
        letter_text: str | None = None,
    ) -> tuple[Packet, ResearchResult, PacketPlan, int]:
        """The one packet build: saved research, required forms and the exact attachments.

        `letter_text` re-renders the same adopted letter for a corrected destination
        (the customer's words and every other section stay identical).
        """
        if saved.adopted_at is None or saved.adopted_sha256 is None:
            raise ServiceConflict("adopt_current_text_first")
        research = self._store.get_research(record.intake_id)
        if research is None:
            raise ServiceConflict("research_not_ready")
        result = decode_research(research.research_json)
        intake = s.SubmitIntake.model_validate_json(record.intake_json)
        form_plan = replace(
            result.packet_plan, customer_fields=_intake_form_plan(intake).customer_fields
        )
        copy_view, copies = self._required_copy_state(record, research)
        if copy_view is not None and not copy_view.ready:
            raise ServiceConflict("required_copies_not_ready")
        forms = complete_required_forms(result.findings, result.sources, result.ticket, form_plan)
        if forms is None:
            raise ServiceConflict("required_form_not_ready")
        citation = (
            None
            if intake.citation_photo_ref is None
            else self._deps.photos.get(record.owner, intake.citation_photo_ref)
        )
        if citation is None:
            raise ServiceConflict("citation_photo_required")
        citation_back = None
        if intake.citation_back_photo_ref is not None:
            citation_back = self._deps.photos.get(record.owner, intake.citation_back_photo_ref)
            if citation_back is None:
                raise ServiceConflict("citation_back_photo_required")
        images: list[bytes] = []
        for ref in intake.photo_refs:
            image = self._deps.photos.get(record.owner, ref)
            if image is None:
                raise ServiceConflict("evidence_photo_required")
            images.append(image)
        try:
            packet = build_packet(
                saved.letter_text if letter_text is None else letter_text,
                citation,
                images,
                saved.adopted_at,
                research=result,
                customer_signature=signature,
                citation_number=intake.ticket.citation_number,
                citation_back_image=citation_back,
                completed_forms=forms,
                required_copies=copies,
                packet_plan=form_plan,
            )
        except PacketError:
            raise ServiceConflict("packet_not_ready") from None
        return packet, result, form_plan, research.revision

    def _assemble_packet(
        self, record: IntakeRecord, saved: SavedDraft, signature_ref: str | None
    ) -> tuple[Packet, CustomerApproved, LetterRequest, int]:
        """The single packet, requirements and envelope path for inspection and freeze."""
        if (
            saved.adopted_sha256 is None
            or saved.adopted_at is None
            or saved.adopted_by is None
            or _FLAGS.validate_json(saved.flags_json)
        ):
            raise ServiceConflict("adopt_current_text_first")
        research = self._store.get_research(record.intake_id)
        if research is None:
            raise ServiceConflict("research_not_ready")
        timing = decode_research(research.research_json).resolve(
            decode_answers(research.answers_json)
        )
        if timing.deadline is None or timing.counting is None or timing.counting.disclose:
            raise ServiceConflict("research_not_ready")
        if not timing.deadline_feasible(production_date(self._deps.clock()), MailClass.CERTIFIED):
            assert record.case_token is not None
            self._apply(record.case_token, Stopped(StopReason.NOT_FEASIBLE))
            raise ServiceConflict("deadline_not_feasible")
        destination = self._store.latest_destination_decision(record.intake_id, record.owner)
        if destination is None:
            raise ServiceConflict("destination_decision_mismatch")
        if destination.decision.provenance is AddressProvenance.CUSTOMER_DIRECTED_UNVERIFIED:
            raise ServiceConflict("destination_verification_required")
        adoption = Adoption(saved.adopted_by, saved.adopted_at, saved.adopted_sha256)
        signature = None
        if signature_ref is not None:
            signature = self._deps.photos.get(record.owner, signature_ref)
            if signature is None:
                raise ServiceConflict("unknown_signature_ref")
        packet, result, form_plan, research_revision = self._frozen_packet(record, saved, signature)
        intake = s.SubmitIntake.model_validate_json(record.intake_json)
        try:
            event = approval_gate(
                adoption,
                packet,
                research=result,
                answers=decode_answers(research.answers_json),
                citation_number=intake.ticket.citation_number,
                packet_plan=form_plan,
                required_copies=self._required_copy_state(record, research)[1],
            )
        except ApprovalRefused:
            raise ServiceConflict("approval_refused") from None
        contact = intake.contact
        address = contact.mailing_address
        request = LetterRequest(
            packet.pdf,
            "packet-preview-validation",
            MailingAddress(
                contact.name,
                None,
                address.line1,
                address.line2 or None,
                address.city,
                address.state,
                address.zip_code,
            ),
            destination.decision.address,
            LetterOptions(MailClass.CERTIFIED),
        )
        try:
            validate_request(request)
        except ValueError:
            raise ServiceConflict("mail_request_invalid") from None
        return packet, event, request, research_revision

    def _current_research(self, record: IntakeRecord) -> ResearchResult:
        saved_research = self._store.get_research(record.intake_id)
        if saved_research is None:
            raise ServiceConflict("research_not_ready")
        return decode_research(saved_research.research_json)

    def _declaration_views(self, record: IntakeRecord) -> list[s.DeclarationView]:
        research = self._current_research(record)
        try:
            declarations = declarations_for(research.findings, research.sources, research.ticket)
        except (DeclarationError, ValueError) as exc:
            raise ServiceConflict("declaration_wording_unverified") from exc
        return [
            s.DeclarationView(
                declaration_id=declaration.declaration_id,
                field_name=declaration.field_name,
                verbatim_text=declaration.verbatim_text,
                text_sha256=declaration.text_sha256,
                required=declaration.required,
                signature_required=declaration.signature_required,
            )
            for declaration in declarations
        ]

    def _check_affirmations(self, record: IntakeRecord, body: s.Approve) -> None:
        """Fail closed: every required declaration must be affirmed against exact wording."""
        research = self._current_research(record)
        try:
            declarations = declarations_for(research.findings, research.sources, research.ticket)
            affirmed = affirmation_map(
                tuple((item.declaration_id, item.text_sha256) for item in body.affirmations)
            )
        except (DeclarationError, ValueError) as exc:
            raise ServiceConflict("declaration_unaffirmed") from exc
        if unknown_affirmations(declarations, affirmed):
            raise ServiceConflict("declaration_unknown")
        if unaffirmed_required(declarations, affirmed):
            raise ServiceConflict("declaration_unaffirmed")

    def packet_preview(self, case_id: str, body: s.PacketPreviewRequest) -> s.PacketPreview:
        if body.remail:
            remail_preview = self._resolve_return(case_id, review=body)
            if remail_preview is None:
                raise ServiceConflict("remail_not_available")
            return remail_preview
        record, saved, case = self._adopted_context(case_id, body.adopted_sha256)
        if case.letter_state is not CaseState.AWAITING_ADOPTION:
            raise ServiceConflict("case_not_approvable")
        packet, _, request, _ = self._assemble_packet(record, saved, body.signature_ref)
        return s.PacketPreview(
            pdf_base64=base64.b64encode(packet.pdf).decode("ascii"),
            packet_sha256=packet.sha256,
            envelope_sha256=envelope_sha256(request),
            letter_text_sha256=packet.letter_text_sha256,
            page_count=packet.page_count,
            mail_to=_address_lines(request.recipient),
            return_address=_address_lines(request.sender),
            declarations=self._declaration_views(record),
        )

    @staticmethod
    def _check_inspected_request(body: s.Approve, request: LetterRequest) -> None:
        packet_matches = hmac.compare_digest(
            body.packet_sha256, hashlib.sha256(request.pdf).hexdigest()
        )
        envelope_matches = hmac.compare_digest(body.envelope_sha256, envelope_sha256(request))
        if not packet_matches or not envelope_matches:
            raise ServiceConflict("packet_preview_mismatch")

    def approve(self, case_id: str, body: s.Approve) -> s.CaseStatus:
        record, saved, case = self._adopted_context(case_id, body.adopted_sha256)
        if body.remail:
            if body.signature_ref is not None:
                raise ServiceConflict("remail_not_available")
            if case.attempt == 2:
                prepared = self._store.get(case.case_token, 2)
                manifest = self._store.get_approval_manifest(case.case_token, 2)
                if prepared is None or manifest is None:
                    raise ServiceConflict("remail_not_available")
                self._check_inspected_request(body, prepared.request)
                try:
                    verify_remail_approval(
                        self._store.get_approval_manifest(case.case_token, 1),
                        manifest,
                        prepared.prepared_at,
                    )
                    verify_dispatch_integrity(
                        manifest,
                        prepared.request,
                        tuple(
                            (item.declaration_id, item.text_sha256) for item in body.affirmations
                        ),
                    )
                except ApprovalIntegrityError:
                    raise ServiceConflict("frozen_packet_mismatch") from None
                return self.status(case_id)
            self._resolve_return(case_id, review=body)
            return self.status(case_id)
        research = self._current_research(record)
        if unsupported_workflow_requirements(research.ticket, research.findings, research.sources):
            raise ServiceConflict("research_not_ready")
        self._check_affirmations(record, body)
        affirmations = tuple((item.declaration_id, item.text_sha256) for item in body.affirmations)
        statement = s.SubmitIntake.model_validate_json(
            record.intake_json
        ).customer_position.statement

        def _manifest_for(req: LetterRequest, when: datetime) -> ApprovalManifest:
            persisted = self._store.get_approval_manifest(case.case_token, 1)
            return build_approval_manifest(
                case_id=case_id,
                case_token=case.case_token,
                attempt=1,
                approved_at=when,
                # A deployment between approval and crash recovery cannot change the
                # original approval's build identity.
                build_commit=(
                    persisted.build_commit
                    if persisted is not None
                    else os.environ.get("PB_BUILD_COMMIT")
                ),
                raw_customer_text=statement,
                approved_text=saved.letter_text,
                request=req,
                affirmations=affirmations,
            )

        prepared = self._store.get(case.case_token, 1)
        if case.letter_state in {CaseState.FROZEN, CaseState.IN_TRANSIT}:
            if prepared is None:
                raise ServiceConflict("frozen_packet_mismatch")
            self._check_destination(record, prepared.request)
            self._check_inspected_request(body, prepared.request)
            existing_manifest = self._store.get_approval_manifest(case.case_token, 1)
            try:
                if existing_manifest is None:
                    self._store.record_approval_manifest(
                        _manifest_for(prepared.request, prepared.prepared_at)
                    )
                else:
                    verify_dispatch_integrity(existing_manifest, prepared.request, affirmations)
            except ApprovalIntegrityError:
                raise ServiceConflict("frozen_packet_mismatch") from None
            return self.status(case_id)
        if case.letter_state is not CaseState.AWAITING_ADOPTION:
            raise ServiceConflict("case_not_approvable")
        packet, event, request, research_revision = self._assemble_packet(
            record, saved, body.signature_ref
        )
        self._check_inspected_request(body, request)
        if prepared is not None:
            self._check_destination(record, prepared.request)
            self._check_inspected_request(body, prepared.request)
            request = prepared.request
        self._check_destination(record, request)
        # Reserve the adopted revision before preparation, so a concurrent edit cannot
        # change the approved text. A crash here is recovered by rebuilding identical bytes.
        self.refresh(case_id)
        current = self._store.get_case(case.case_token)
        if current is None or current.letter_state is not CaseState.AWAITING_ADOPTION:
            raise ServiceConflict("case_not_approvable")
        if saved.packet_sha256 is None:
            self._save_draft(
                case_id,
                replace(saved, packet_sha256=packet.sha256, updated_at=self._deps.clock()),
                saved.revision,
                expected_research_revision=research_revision,
            )
        elif saved.packet_sha256 != packet.sha256:
            raise ServiceConflict("frozen_packet_mismatch")
        if prepared is None:
            request = replace(request, submission_token=f"sub_{self._deps.new_id()}")
        approved_at = self._deps.clock()
        try:
            approved_attempt = self._store.prepare(
                case.case_token,
                1,
                request,
                approved_at,
                expected_research=(case_id, research_revision),
            )
            # Preparation is durable and returns its original identity on replay.
            # A restart must not mint a different timestamp for the same approval.
            self._store.record_approval_manifest(
                _manifest_for(request, approved_attempt.prepared_at)
            )
        except (PreparationConflict, ApprovalIntegrityError) as exc:
            reason = (
                "research_revision_stale"
                if str(exc) == "research_revision_stale"
                else "frozen_packet_mismatch"
            )
            raise ServiceConflict(reason) from None
        self._apply(case.case_token, event)
        return self.status(case_id)

    def _return_evidence(
        self, case_token: str, attempt: int | None, body: dict[str, object]
    ) -> dict[str, object]:
        """Derived facts persisted with a signed return webhook record.

        The return cause comes from the webhook's own USPS return reason through
        returned_mail.classify_return and the frozen records; the mailing cost is the
        documented expected cost for the mail class, recorded as a flagged ESTIMATE
        (T26 verifies it against actual Lob billing). Nothing else is invented from
        provider text: no typed findings and no fabricated actual cost.
        """
        if attempt is None:
            return {}
        case = self._store.get_case(case_token)
        frozen = self._store.get(case_token, attempt)
        if case is None or frozen is None:
            return {}
        reason = usps_return_reason(body)
        decision = self._store.frozen_destination(case_token, attempt)
        official = (
            decision.address
            if decision is not None and decision.provenance is AddressProvenance.SYSTEM_VERIFIED
            else None
        )
        cause = classify_return(
            _address_text(frozen.request.recipient),
            None if official is None else _address_text(official),
            reason,
            False,
        )
        return {
            "usps_reason": None if reason is None else reason.value,
            "return_cause": cause.value,
            "mailing_cost_cents": estimated_mailing_cost(
                frozen.request.options.mail_class, case.amount_cents
            ),
            "mailing_cost_status": "ESTIMATE",
            "mailing_cost_unverified": True,
        }

    def _returned_mail_evidence(
        self, case_id: str, case: CaseRecord, recipient: MailingAddress, letter_id: str
    ) -> tuple[FailureFacts, int | None, bool]:
        fingerprint = address_fingerprint(recipient)
        facts = FailureFacts(case_id, mailed_address_fingerprint=fingerprint)
        recorded_cost: int | None = None
        cost_is_estimate = False
        # This is the stored application evidence seam, not a Lob payload contract.
        # USPS reason text alone cannot establish sole customer address fault.
        for payload in self._store._iter_tracking_payloads(case.case_token, newest_first=True):
            data = json.loads(payload)
            if (
                data.get("event")
                not in {"letter.returned_to_sender", "letter.certified.returned_to_sender"}
                or data.get("attempt") != case.attempt
                or data.get("letter_id") != letter_id
                # A record without return evidence is not the return proof and must
                # never shadow the real return record behind it.
                or not _accepted_tracking_row(data)
            ):
                continue
            if "failure_facts" in data:
                facts = _FAILURE_FACTS.validate_python(data["failure_facts"])
                if facts.case_id != case_id or facts.mailed_address_fingerprint != fingerprint:
                    raise ServiceConflict("return_evidence_mismatch")
            elif "return_cause" in data:
                # Production ingress derived this cause from the signed webhook's USPS
                # return reason; a bare payload still defaults to unknown cause.
                facts = FailureFacts(
                    case_id,
                    mailed_address_fingerprint=fingerprint,
                    return_cause=ReturnCause(data["return_cause"]),
                )
            recorded_cost = data.get("mailing_cost_cents")
            cost_is_estimate = data.get("mailing_cost_status") == "ESTIMATE"
            break
        return facts, recorded_cost, cost_is_estimate

    def _first_return_confirmation(self, case: CaseRecord) -> datetime | None:
        """First durable transition for this attempt, timed by our ingestion clock.

        Provider dates and subsequent tracking notifications are evidence only; they
        cannot advance this immutable anchor. History also covers pre-change packets.
        """
        attempt = 1
        for item in self._store._iter_transition_history(case.case_token):
            if item.machine != "letter":
                continue
            if item.event_name == "RemailRequested":
                attempt += 1
            elif item.event_name == "ReturnConfirmed" and attempt == case.attempt:
                return item.occurred_at
        return None

    def _resolve_return(
        self, case_id: str, *, review: s.PacketPreviewRequest | None = None
    ) -> s.PacketPreview | None:
        """Sweep existing remedies, or serve an explicit corrected packet decision."""
        record = self._intake(case_id)
        case = None if record.case_token is None else self._store.get_case(record.case_token)
        if review is not None:
            _, _, current = self._adopted_context(case_id, review.adopted_sha256)
            if (
                current != case
                or current.attempt != 1
                or current.letter_state is not CaseState.RETURNED
                or current.payment_state is not PaymentState.CAPTURED
                or review.signature_ref is not None
            ):
                raise ServiceConflict("remail_not_available")
        if case is None:
            return None
        if case.letter_state is CaseState.CLOSED_RETURNED:
            # Definitive failed re-mails use the same returned-mail refund path, on a
            # later sweep after both completion transactions have committed. A pending
            # original capture (or uncertain submission) keeps waiting across restarts.
            cursor = (
                self._store.submission_cursor(case.case_token, 2) if case.attempt == 2 else None
            )
            if (
                cursor is not None
                and isinstance(cursor.outcome, Rejected | NotSent | Canceled)
                and case.payment_state is PaymentState.CAPTURED
                and self._store.returned_mail_refund(case.case_token) is None
            ):
                self._store.request_returned_mail_refund(
                    case.case_token,
                    RemedyKind.FULL_REFUND,
                    case.amount_cents,
                    now=self._deps.clock(),
                )
            return None
        if case.letter_state is not CaseState.RETURNED:
            return None
        prepared = self._store.get(case.case_token, case.attempt)
        cursor = self._store.submission_cursor(case.case_token, case.attempt)
        if prepared is None or cursor is None or not isinstance(cursor.outcome, Accepted):
            raise ServiceConflict("returned_mailing_not_confirmed")
        fingerprint = address_fingerprint(prepared.request.recipient)
        facts, recorded_cost, cost_is_estimate = self._returned_mail_evidence(
            case_id, case, prepared.request.recipient, cursor.outcome.letter_id
        )
        returned_at = self._first_return_confirmation(case)
        resolved_cost_is_estimate: bool | None = None
        responsibility = classify_address_failure(
            self._store.frozen_destination(case.case_token, case.attempt), facts
        )
        if case.payment_state is PaymentState.NO_CHARGE:
            # Free beta: no authorization, capture, refund or re-mail exists or can be
            # funded, so waiting for a payment remedy would strand the case forever.
            # Close through the same atomic remedy transaction, keeping the factual
            # responsibility and this attempt's identity. No money or mailing effect
            # is emitted, and an unpaid original provider answer never reaches here:
            # the accepted-submission check above already refuses that.
            self._store.resolve_returned_mail(
                case.case_token,
                case.revision,
                responsibility,
                None,
                None,
                now=self._deps.clock(),
            )
            return None
        if case.payment_state is not PaymentState.CAPTURED:
            if case.payment_state in {PaymentState.RELEASED, PaymentState.WRITTEN_OFF}:
                # No original charge exists to refund or fund a free re-mail (H2).
                # Close through the existing remedy transaction, outside dispatch.
                self._store.resolve_returned_mail(
                    case.case_token,
                    case.revision,
                    responsibility,
                    None,
                    None,
                    now=self._deps.clock(),
                )
            # Pending/uncertain collection is reconciled by the existing dispatcher.
            return None
        correction = self._store.latest_destination_decision(case_id, record.owner)
        other_address = (
            correction is not None
            and correction.decision.provenance is AddressProvenance.SYSTEM_VERIFIED
            and correction.decision.fingerprint != fingerprint
            and mailing_gate(
                correction.decision,
                (),
                case_id=case_id,
                owner=record.owner,
                mailed_address=correction.decision.address,
                latest_customer_address_revision=None,
                current_disclosure_sha256=DISCLOSURE_SHA256,
                now=self._deps.clock(),
                returned_address=prepared.request.recipient,
            ).allowed
        )
        feasible = False
        saved_research = self._store.get_research(case_id)
        if other_address and case.attempt == 1 and saved_research is not None:
            resolution = decode_research(saved_research.research_json).resolve(
                decode_answers(saved_research.answers_json)
            )
            if resolution.deadline is not None and resolution.counting is not None:
                feasible = production_date(self._deps.clock()) <= latest_safe_send_date(
                    resolution.deadline.deadline,
                    DeadlineRule(resolution.counting.rule.value.lower()),
                    prepared.request.options.mail_class,
                )
        # Responsibility is already decided above: the remedy kind never reads the cost,
        # and a full refund of the captured amount never depends on one. Only the partial
        # refund subtracts a mailing cost, so only that path can refuse an invalid one.
        remedy = remedy_for_responsibility(
            responsibility,
            # Every re-mail must have the persisted correction, including OUR_ERROR/UNKNOWN.
            deadline_feasible=feasible and other_address,
            other_verified_address_exists=other_address,
            price_cents=case.amount_cents,
            mailing_cost_cents=0,
            remails_used=case.attempt - 1,
        )
        if review is not None and (remedy is None or not remedy.remails):
            raise ServiceConflict("remail_not_available")
        if remedy is not None and remedy.kind is RemedyKind.PARTIAL_REFUND:
            if recorded_cost is not None and (type(recorded_cost) is not int or recorded_cost < 0):
                raise ServiceConflict("return_cost_invalid")
            now = self._deps.clock()
            if recorded_cost is not None and not cost_is_estimate:
                # An actual cost is never negative (checked) and never above the price.
                cost = min(recorded_cost, case.amount_cents)
                resolved_cost_is_estimate = False
            elif returned_at is None or now >= returned_at + RETURN_COST_EVIDENCE_WINDOW:
                # No authoritative actual cost arrived in the bounded window: the documented
                # expected cost is used, recorded as a flagged ESTIMATE. Never blocks forever.
                resolved_cost_is_estimate = True
                cost = estimated_mailing_cost(
                    prepared.request.options.mail_class, case.amount_cents
                )
            else:
                # Missing actual cost is not a zero-cost ledger fact. Wait for evidence.
                return None
            remedy = remedy_for_responsibility(
                responsibility,
                deadline_feasible=feasible and other_address,
                other_verified_address_exists=other_address,
                price_cents=case.amount_cents,
                mailing_cost_cents=cost,
                remails_used=case.attempt - 1,
            )
        if (
            remedy is not None
            and remedy.refund_cents
            and case.payment_state is not PaymentState.CAPTURED
        ):
            # The existing dispatcher first reconciles the original capture.
            return None
        request = None
        remail_manifest = None
        expected_research = None
        approved_at = None
        if remedy is not None and remedy.remails:
            assert correction is not None
            saved = self._store.get_draft(case_id)
            try:
                original = self._store.get_approval_manifest(case.case_token, 1)
                if (
                    saved is None
                    or original is None
                    or original.approved_text_sha256 != saved.adopted_sha256
                ):
                    raise ServiceConflict("draft_not_ready")
                verify_dispatch_integrity(original, prepared.request)
                redirected = redirect_letter_address(
                    saved.letter_text,
                    prepared.request.recipient,
                    correction.decision.address,
                )
                packet, result, plan, revision = self._frozen_packet(
                    record,
                    saved,
                    frozen_signature_artwork(prepared.request.pdf),
                    letter_text=redirected,
                )
                request = replace(
                    prepared.request,
                    pdf=packet.pdf,
                    recipient=correction.decision.address,
                )
                if not result.evidence_current_at(self._deps.clock()):
                    raise ServiceConflict("research_not_ready")
                validate_request(request)
                assert saved_research is not None
                if revision != saved_research.revision:
                    raise ServiceConflict("research_not_ready")
                if saved.adopted_at is None or saved.adopted_by is None:
                    raise ServiceConflict("adopt_current_text_first")
                approval_gate(
                    Adoption(saved.adopted_by, saved.adopted_at, text_sha256(redirected)),
                    packet,
                    research=result,
                    answers=decode_answers(saved_research.answers_json),
                    packet_plan=plan,
                    citation_number=s.SubmitIntake.model_validate_json(
                        record.intake_json
                    ).ticket.citation_number,
                    required_copies=self._required_copy_state(record, saved_research)[1],
                )
                expected_research = (case_id, revision)
            except (LetterError, PacketError, ServiceConflict, ApprovalRefused, ValueError):
                if review is not None:
                    raise ServiceConflict("remail_not_available") from None
                # We cannot safely reproduce the approved packet. This is our service
                # failure: resolve with a full refund through the same atomic path,
                # without freezing a second letter or leaving a permanently stuck case.
                # The return's own classification is kept next to the failure (L1), so
                # an agency-side customer keeps their proof packet with the refund.
                failure = classify_address_failure(
                    self._store.frozen_destination(case.case_token, case.attempt),
                    replace(facts, service_failure=True),
                )
                remedy = remedy_for_responsibility(
                    failure,
                    deadline_feasible=False,
                    other_verified_address_exists=False,
                    price_cents=case.amount_cents,
                    mailing_cost_cents=0,
                    remails_used=case.attempt - 1,
                )
                responsibility = replace(responsibility, reason=failure.reason)
                request = None
                if case.payment_state is not PaymentState.CAPTURED:
                    return None
            else:
                if review is None:
                    # The verified correction is eligible, but the original approval
                    # cannot authorize a different packet or recipient.
                    return None
                if not isinstance(review, s.Approve):
                    return s.PacketPreview(
                        pdf_base64=base64.b64encode(packet.pdf).decode("ascii"),
                        packet_sha256=packet.sha256,
                        envelope_sha256=envelope_sha256(request),
                        letter_text_sha256=packet.letter_text_sha256,
                        page_count=packet.page_count,
                        mail_to=_address_lines(request.recipient),
                        return_address=_address_lines(request.sender),
                        declarations=self._declaration_views(record),
                    )
                self._check_inspected_request(review, request)
                self._check_affirmations(record, review)
                request = replace(request, submission_token=f"sub_{self._deps.new_id()}")
                approved_at = self._deps.clock()
                remail_manifest = build_remail_approval_manifest(
                    original,
                    request,
                    approved_at=approved_at,
                    approved_text=redirected,
                    build_commit=os.environ.get("PB_BUILD_COMMIT"),
                    affirmations=tuple(
                        (item.declaration_id, item.text_sha256) for item in review.affirmations
                    ),
                )
        try:
            self._store.resolve_returned_mail(
                case.case_token,
                case.revision,
                responsibility,
                remedy,
                request,
                now=approved_at or self._deps.clock(),
                cost_is_estimate=resolved_cost_is_estimate,
                remail_manifest=remail_manifest,
                expected_research=expected_research,
            )
        except (RevisionConflict, PreparationConflict, ApprovalIntegrityError):
            if review is None:
                raise
            raise ServiceConflict("remail_not_available") from None
        return None

    # Incident hold, resume and retry (T74) --------------------------------------------
    def _held(self, case_token: str) -> bool:
        hold = self._store.case_hold(case_token)
        return hold is not None and hold.held

    def _known_case(self, case_token: str) -> CaseRecord:
        case = self._store.get_case(case_token)
        if case is None:
            raise ServiceConflict("unknown_case")
        return case

    def hold_case(
        self, case_token: str, reason: HoldReason, command_id: str
    ) -> tuple[CommandOutcome, CaseHold | None]:
        """Hold one case's new paid research, drafting and physical create initiation.

        A hold is an overlay, never a new state: it gates the durable create-intent claim,
        but it does not recall or alter a letter already created. Activation linearizes
        under the per-case drafting ownership and the same global submission ownership a
        create uses, so while a create is in flight the command is logged busy and stays
        open for the same command to take effect after that call and its result commit.
        """
        case = self._known_case(case_token)
        now = self._deps.clock()
        if case.letter_state in TERMINAL_STATES:
            return self._store.record_hold_command(
                command_id,
                case_token,
                HoldCommand.HOLD,
                now,
                expected_generation=None,
                reason=reason,
                allowed=False,
            )

        def busy() -> tuple[CommandOutcome, CaseHold | None]:
            decided = self._store.record_busy_hold(command_id, case_token, reason, now)
            outcome = CommandOutcome.BUSY if decided is None else decided
            return outcome, self._store.case_hold(case_token)

        with self._store.draft_ownership(case_token) as owned:
            if not owned:
                return busy()
            try:
                with self._store.submission_ownership():
                    # Re-read under both locks: the case may have finished meanwhile, and
                    # only this read may decide that no new hold becomes effective.
                    current = self._store.get_case(case_token)
                    if current is None or current.letter_state in TERMINAL_STATES:
                        return self._store.record_hold_command(
                            command_id,
                            case_token,
                            HoldCommand.HOLD,
                            now,
                            expected_generation=None,
                            reason=reason,
                            allowed=False,
                        )
                    # Escalate well before the card authorization's existing expiry stop.
                    review_due = max(
                        now, current.authorization_expires_at - EXPIRY_MARGIN - REVIEW_LEAD
                    )
                    return self._store.record_hold_command(
                        command_id,
                        case_token,
                        HoldCommand.HOLD,
                        now,
                        expected_generation=None,
                        reason=reason,
                        review_due_at=review_due,
                    )
            except RevisionConflict:
                # The submission mutex is held by a create or another hold: no new hold
                # is reported effective until that call and its result commit complete.
                return busy()

    def resume_case(
        self, case_token: str, command_id: str, expected_generation: int
    ) -> tuple[CommandOutcome, CaseHold | None]:
        """Clear the hold the caller saw, then advance this one case through its usual steps."""
        case = self._known_case(case_token)
        outcome, hold = self._store.record_hold_command(
            command_id,
            case_token,
            HoldCommand.RESUME,
            self._deps.clock(),
            expected_generation=expected_generation,
            allowed=case.letter_state not in TERMINAL_STATES,
        )
        if outcome is CommandOutcome.APPLIED:
            self.advance_case(case_token)
        return outcome, hold

    def retry_case(
        self, case_token: str, command_id: str
    ) -> tuple[CommandOutcome, CaseHold | None]:
        """Ask for this case's eligible work now; an active hold stays in place.

        On a held case research and drafting stay paused, while expiry stops, lookups,
        capture, release and refunds run if due. Like a sweep, a retry cannot lift a hold:
        the durable create-intent claim still pauses a held mail initiation.
        """
        case = self._known_case(case_token)
        outcome, hold = self._store.record_hold_command(
            command_id,
            case_token,
            HoldCommand.RETRY,
            self._deps.clock(),
            expected_generation=None,
            allowed=case.letter_state not in TERMINAL_STATES,
        )
        if outcome is CommandOutcome.APPLIED:
            self.advance_case(case_token)
        return outcome, hold

    def advance_case(self, case_token: str) -> None:
        """One case through the sweep's own steps, in the sweep's order.

        Reuses stored effects, cursors, idempotency keys and lookup-before-retry; scheduled
        backoff (``next_at``) is kept. Frozen cases re-check authorization expiry and
        deadline feasibility in the dispatcher before any create.
        """
        case_id = self._case_ids_by_token().get(case_token)
        if case_id is None:
            raise ServiceConflict("unknown_case")

        def state() -> CaseState:
            return self._known_case(case_token).letter_state

        if state() in (
            CaseState.RESEARCHING,
            CaseState.NEEDS_CUSTOMER,
            CaseState.DRAFTING,
            CaseState.AWAITING_ADOPTION,
        ):
            self.refresh(case_id)
        self._research(case_id)
        self._draft_step(case_id)
        if (
            state() is CaseState.RETURNED
            or case_id in self._store.failed_remail_refund_candidates()
        ):
            self._resolve_return(case_id)
        if self._deps.lob is not None:
            self._dispatch(case_token)

    def configure_overseer_ids(self, key: bytes, version: int = 1) -> None:
        """Write-side setup for both API roles, before either accepts case creation."""
        self._store.configure_ops_ids(key, version)

    def overseer_feed(self, after: str | None, limit: int) -> s.OverseerFeed:
        """Read an allow-listed snapshot without refreshing or changing any case."""
        return s.OverseerFeed.model_validate(
            asdict(self._store.overseer_page(self._deps.clock(), after, limit))
        )

    def advance_due(self) -> SweepResult:
        """Scheduler sweep: expire windows, run due research and drafting, then mail and pay.

        Each step is idempotent and saved before use, so a sweep after a crash resumes.
        A failing case is recorded once and skipped; the rest of the sweep still runs.
        A malformed case row is isolated as storage_corruption; database-level errors
        propagate (fail closed).
        """
        advanced = 0
        failed: dict[str, str] = {}

        def isolate(case_id: str, work: Callable[[str], object]) -> bool:
            try:
                work(case_id)
            except sqlite3.DatabaseError:
                raise
            except Exception as exc:
                failed.setdefault(case_id, _sweep_reason(exc))
                return False
            return True

        windowed = (
            CaseState.RESEARCHING.value,
            CaseState.NEEDS_CUSTOMER.value,
            CaseState.DRAFTING.value,
            CaseState.AWAITING_ADOPTION.value,
        )
        for case_id in self._store.intakes_in_states(windowed):
            isolate(case_id, self.refresh)
        for case_id in self._store.intakes_in_states((CaseState.RESEARCHING.value,)):
            if case_id not in failed and isolate(case_id, self._research):
                advanced += 1
        for case_id in self._store.intakes_in_states((CaseState.DRAFTING.value,)):
            if case_id not in failed and isolate(case_id, self._draft_step):
                advanced += 1
        for case_id in chain(
            self._store.intakes_in_states((CaseState.RETURNED.value,)),
            self._store.failed_remail_refund_candidates(),
        ):
            if case_id not in failed and isolate(case_id, self._resolve_return):
                advanced += 1
        if self._deps.lob is None:
            raise ServiceConflict("mail_not_configured")
        # One bounded pass over the oldest parked Lob events; each case below replays only
        # the rows its own letter id or token matches. A failing pass never stops the mailing.
        try:
            self._replay_webhooks()
        except sqlite3.DatabaseError:
            raise
        except Exception as exc:
            failed.setdefault("webhook-backlog", _sweep_reason(exc))
        owners = self._case_ids_by_token() if failed else None
        for token in self._store.case_ids():
            if owners is not None and owners.get(token) in failed:
                continue
            try:
                advanced += self._dispatch(token)
            except sqlite3.DatabaseError:
                raise
            except Exception as exc:
                if owners is None:
                    owners = self._case_ids_by_token()
                failed.setdefault(owners.get(token, "unlinked"), _sweep_reason(exc))
        return SweepResult(advanced, tuple(SweepFailure(c, r) for c, r in failed.items()))

    def _case_ids_by_token(self) -> dict[str, str]:
        """Case token -> intake id, built once per failing sweep; never raises."""
        owners: dict[str, str] = {}
        try:
            ids = self._store.intakes_in_states(tuple(state.value for state in CaseState))
        except Exception:
            return owners
        for case_id in ids:
            try:
                record = self._store.get_intake(case_id)
            except Exception:
                continue
            if record is not None and record.case_token is not None:
                owners[record.case_token] = case_id
        return owners

    @property
    def sweep_runs(self) -> SweepRunRecorder:
        """Where the scheduled sweep records its runs for health monitoring (T24)."""
        return self._store

    def dispatch(self, case_id: str | None = None) -> int:
        token = None if case_id is None else self._intake(case_id).case_token
        if case_id is not None and token is None:
            return 0
        if self._deps.lob is None:
            raise ServiceConflict("mail_not_configured")
        return self._dispatch(token)

    def webhook(self, provider: str, payload: bytes, headers: dict[str, str]) -> bool:
        now = self._deps.clock()
        try:
            if provider == "stripe":
                data = verify_stripe_webhook(
                    payload,
                    headers.get("stripe-signature", ""),
                    self._deps.stripe_webhook_secret,
                    now,
                )
            else:
                data = verify_lob_webhook(
                    payload,
                    headers.get("lob-signature", ""),
                    headers.get("lob-signature-timestamp", ""),
                    self._deps.lob_webhook_secret,
                    now,
                )
        except ValueError:
            raise ServiceConflict("invalid_signature") from None
        return self._record_verified_webhook(provider, data, now)

    def _record_verified_webhook(
        self,
        provider: str,
        data: dict[str, object],
        now: datetime,
        saved: BacklogRow | None = None,
    ) -> bool:
        """Retry once with the immutable first envelope if a concurrent delivery won."""
        for _ in range(2):
            try:
                return self._record_verified_webhook_once(provider, data, now, saved)
            except WebhookEnvelopeConflict as exc:
                saved = exc.row
                data = json.loads(saved.payload)
        raise ServiceConflict("webhook_busy")

    def _record_verified_webhook_once(
        self,
        provider: str,
        data: dict[str, object],
        now: datetime,
        saved: BacklogRow | None = None,
    ) -> bool:
        """One evidence path for verified ingress and its durable reduced envelope.

        ``saved`` is the parked row being replayed; a live delivery looks its row up by id.
        """
        event_id = data.get("id")
        if not isinstance(event_id, str) or not event_id:
            raise ServiceConflict("invalid_webhook")
        # Keep only identifiers and tracking facts; webhook bodies can contain customer PII.
        event_type = data.get("event_type") if provider == "lob" else data.get("type")
        if isinstance(event_type, dict):
            event_type = event_type.get("id")
        obj = data.get("body") if provider == "lob" else data.get("data")
        if provider == "stripe" and isinstance(obj, dict):
            obj = obj.get("object")
        if not isinstance(obj, dict) or not isinstance(event_type, str):
            raise ServiceConflict("invalid_webhook")
        if provider == "stripe" and event_type in DISPUTE_EVENTS | FRAUD_WARNING_EVENTS:
            return self._record_dispute(event_id, event_type, obj, data.get("created"), now)
        # An early delivery owns its event identity and evidence across retries.
        # A later signed body with the same event id cannot replace that evidence.
        if provider == "lob":
            if saved is None:
                saved = self._store.backlog_row(provider, event_id)
            if saved is not None:
                kept: Any = json.loads(saved.payload)
                data, event_type, obj = kept, kept["event_type"], kept["body"]
        ref = obj.get("id")
        if provider == "lob" and (not isinstance(ref, str) or not ref):
            raise ServiceConflict("invalid_webhook")
        token = None
        attempt = None
        metadata = obj.get("metadata")
        submission_token = metadata.get("submission_token") if isinstance(metadata, dict) else None
        if not isinstance(submission_token, str):
            submission_token = None
        unmatched_reason: str | None = None
        if provider == "stripe":
            if isinstance(ref, str):
                token = self._store.case_for_authorization(ref)
        elif provider == "lob":
            assert isinstance(ref, str)
            found, unmatched_reason = self._match_lob_attempt(ref, submission_token, event_type)
            if found is not None:
                token, attempt = found
        tracking_number = obj.get("tracking_number")
        occurred = data.get("date_created")
        try:
            occurred_at = (
                datetime.fromisoformat(occurred.replace("Z", "+00:00"))
                if isinstance(occurred, str)
                else now
            )
            if occurred_at.tzinfo is None:
                raise ValueError("naive event")
        except ValueError:
            raise ServiceConflict("invalid_webhook") from None
        envelope = ""
        if provider == "lob":
            # Keep only the fields needed by the SAME predicates on replay. Preserve
            # explicit invalid metadata/status rather than silently making it valid.
            reduced: dict[str, object] = {
                "id": ref,
                "tracking_number": tracking_number if isinstance(tracking_number, str) else None,
                "metadata": (
                    {"submission_token": submission_token}
                    if isinstance(metadata, dict)
                    else None
                    if metadata is None
                    else {}
                ),
                "tracking_events": [
                    scan.as_sent() for scan in scan_proof(obj.get("tracking_events"))
                ],
            }
            for field in ("deleted", "status", "failure_reason"):
                if field in obj:
                    value = obj[field]
                    # Store predicate-relevant flags, never provider error/customer text.
                    reduced[field] = (
                        value
                        if field == "deleted" and (value is None or type(value) is bool)
                        else value
                        if field == "status" and value in (None, "processed", "rendered")
                        else None
                        if value is None
                        else True
                    )
            envelope = json.dumps(
                {
                    "id": event_id,
                    "event_type": event_type,
                    "date_created": occurred_at.isoformat(),
                    "body": reduced,
                }
            )

        if token is None:
            if provider != "lob":
                raise ServiceConflict("webhook_case_not_ready")
            return self._settle_unbound(event_id, str(ref), envelope, saved, unmatched_reason, now)
        is_return = event_type in {
            "letter.returned_to_sender",
            "letter.certified.returned_to_sender",
        }
        # Delivery tracking is not certified delivery proof. Keep it as evidence, never
        # manufacture DeliveryProofStored from a status or a temporary provider URL.
        try:
            with self._store.submission_ownership():
                event: Event | None = None
                case = self._store.get_case(token)
                assert case is not None
                acceptance = None
                prepared = None if attempt is None else self._store.get(token, attempt)
                cursor = None if attempt is None else self._store.submission_cursor(token, attempt)
                if provider == "lob":
                    # Matching happened before ownership. Revalidate the exact attempt
                    # before projecting a return or writing any tracking/effects.
                    held = (
                        cursor.outcome.letter_id
                        if cursor is not None
                        and isinstance(cursor.outcome, Accepted | Uncertain | Canceled)
                        else None
                    )
                    if held is not None and held != ref:
                        return self._settle_unbound(
                            event_id, str(ref), envelope, saved, CONFLICTING_LETTER, now
                        )
                    if cursor is None or not isinstance(cursor.outcome, Accepted | Uncertain):
                        reason = (
                            CANCEL_CONFIRMED
                            if cursor is not None
                            and isinstance(cursor.outcome, Canceled)
                            and event_type in FAILURE_WEBHOOKS
                            else CONTRADICTS_CASE_STATE
                        )
                        return self._settle_unbound(
                            event_id, str(ref), envelope, saved, reason, now
                        )
                scan_matches = (
                    provider == "lob"
                    and prepared is not None
                    and scan_webhook_qualifies(event_type, obj, prepared.request.submission_token)
                )
                failure_matches = (
                    provider == "lob"
                    and prepared is not None
                    and failure_webhook_qualifies(
                        event_type, obj, prepared.request.submission_token
                    )
                )
                # Already accepted letters retain their existing signed exact-id
                # return path. These notifications need not repeat metadata; explicit
                # contradictory metadata never supplies evidence. A first return still
                # requires the full scan/token gate before acceptance (M4).
                metadata = obj.get("metadata")
                return_matches = is_return and (
                    scan_matches
                    or (
                        cursor is not None
                        and isinstance(cursor.outcome, Accepted)
                        and prepared is not None
                        and (
                            metadata is None
                            or (
                                isinstance(metadata, dict)
                                and metadata.get("submission_token")
                                == prepared.request.submission_token
                            )
                        )
                    )
                )
                if (
                    provider == "lob"
                    and is_return
                    and metadata is None
                    and cursor is not None
                    and pending_evidence(cursor.outcome)
                ):
                    # A metadata-free return cannot prove first acceptance. Keep it
                    # until independently proven acceptance unlocks the exact-id path.
                    return self._settle_unbound(event_id, str(ref), envelope, saved, None, now)
                evidence = self._return_evidence(token, attempt, obj) if return_matches else {}
                scans = (
                    scan_proof(obj.get("tracking_events")) if scan_matches or return_matches else ()
                )
                safe = json.dumps(
                    {
                        "event": event_type,
                        "occurred_at": occurred_at.isoformat(),
                        "tracking_number": tracking_number
                        if isinstance(tracking_number, str)
                        else None,
                        "letter_id": ref,
                        "attempt": attempt,
                        "failure_evidence": failure_matches,
                        "scan_evidence": scan_matches or return_matches,
                        "evidence_ignored": (
                            "submission_identity_mismatch"
                            if provider == "lob"
                            and event_type in FAILURE_WEBHOOKS
                            and not failure_matches
                            else None
                        ),
                        "tracking_events": [scan.as_sent() for scan in scans],
                        **evidence,
                    }
                )
                if (
                    provider == "lob"
                    and attempt == case.attempt
                    and cursor is not None
                    and pending_evidence(cursor.outcome)
                    and prepared is not None
                    and isinstance(ref, str)
                    and scan_matches
                ):
                    acceptance = Accepted(
                        ref,
                        tracking_number if isinstance(tracking_number, str) else None,
                        scans=scans,
                    )
                if (
                    provider == "lob"
                    and attempt == case.attempt
                    and is_return
                    and return_matches
                    and (case.letter_state is CaseState.IN_TRANSIT or acceptance is not None)
                ):
                    event = case_state.ReturnConfirmed(
                        str(evidence.get("usps_reason") or "provider confirmed return")
                    )
                recorded = self._store.record_webhook(
                    provider,
                    event_id,
                    token,
                    safe,
                    letter_event=event,
                    occurred_at=now,
                    new_effect_id=self._deps.new_id,
                    submission_acceptance=acceptance,
                    submission_token=submission_token
                    if isinstance(submission_token, str)
                    else None,
                    expected_envelope=(envelope if saved is None else saved.payload)
                    if provider == "lob"
                    else None,
                    submission_identity=(attempt, str(ref))
                    if provider == "lob" and attempt is not None
                    else None,
                )
        except RevisionConflict:
            raise ServiceConflict("webhook_busy") from None
        # Payment webhooks are reconciliation signals. The existing coordinator retrieves
        # full original-object evidence and atomically records payment events/cursor/outbox.
        # Scheduler recovery remains possible if the process exits before this dispatch.
        if recorded and provider == "stripe":
            self._dispatch(token)
        return recorded

    def _record_dispute(
        self,
        event_id: str,
        event_type: str,
        obj: dict[str, object],
        event_created: object,
        now: datetime,
    ) -> bool:
        """Keep a reduced dispute/fraud-warning fact on its case. No money or letter change.

        Stripe moves disputed funds itself; this records the fact once (event-id dedup) for
        the ledger and review, and never dispatches capture, release or refund.
        """
        try:
            fact = dispute_fact(event_type, obj, event_created)
        except ValueError:
            raise ServiceConflict("invalid_webhook") from None
        token = self._store.case_for_authorization(fact.payment_intent)
        if token is None:
            raise ServiceConflict("webhook_case_not_ready")
        try:
            return self._store.record_webhook(
                "stripe",
                event_id,
                token,
                fact.to_json(),
                occurred_at=now,
                new_effect_id=self._deps.new_id,
            )
        except RevisionConflict:
            raise ServiceConflict("webhook_busy") from None

    def _match_lob_attempt(
        self, ref: str, submission_token: str | None, event_type: str
    ) -> tuple[tuple[str, int] | None, str | None]:
        """The one attempt a Lob letter belongs to, else why none does. Indexed lookups only.

        A letter belongs to an attempt by its exact id, or by the frozen submission token
        while that attempt's cursor has no letter id and no other parked letter carries the
        token. Several claimants are ambiguous: nothing binds and the event stays parked.
        With no claimant, an attempt that already holds a different letter for this token
        makes the event a conflicting letter, and one that was canceled or refused
        contradicts the evidence; neither can ever bind. Lob's failed/deleted notice for the
        exact letter this system canceled only confirms the cancellation.
        """
        by_letter = self._store.attempts_for_letter(ref)
        by_token = (
            None
            if submission_token is None
            else self._store.attempt_for_submission_token(submission_token)
        )
        candidates = list(dict.fromkeys((*by_letter, *(() if by_token is None else (by_token,)))))
        competing: frozenset[str] | None = None
        matches: list[tuple[str, int]] = []
        reason: str | None = None
        for candidate in candidates:
            cursor = self._store.submission_cursor(*candidate)
            if cursor is None:
                continue
            outcome = cursor.outcome
            held = (
                outcome.letter_id if isinstance(outcome, Accepted | Uncertain | Canceled) else None
            )
            if isinstance(outcome, Accepted | Uncertain) and held == ref:
                matches.append(candidate)
            elif isinstance(outcome, Accepted | Uncertain) and held is None:
                # Only the token can name an attempt whose create response never anchored
                # a letter; a rival parked letter for the same token makes it ambiguous.
                assert submission_token is not None
                if competing is None:
                    competing = self._store.backlog_letters_for_token(submission_token)
                if not (competing - {ref}):
                    matches.append(candidate)
            elif held is not None and held != ref:
                reason = reason or CONFLICTING_LETTER
            elif isinstance(outcome, Canceled) and event_type in FAILURE_WEBHOOKS:
                reason = reason or CANCEL_CONFIRMED
            else:
                reason = reason or CONTRADICTS_CASE_STATE
        if len(matches) == 1:
            return matches[0], None
        return None, reason if not matches else None

    def _settle_unbound(
        self,
        event_id: str,
        ref: str,
        envelope: str,
        saved: BacklogRow | None,
        reason: str | None,
        now: datetime,
    ) -> bool:
        """Park an event no case claims yet, or give it its final outcome and tell an operator.

        An event that can never bind (a conflicting letter, evidence against the case state)
        moves at once; one nothing has claimed in thirty days moves as expired. Everything
        else stays durable in the backlog, keeping its first envelope.
        """
        if (
            reason is None
            and saved is not None
            and now - saved.received_at > WEBHOOK_UNMATCHED_AFTER
        ):
            reason = UNMATCHED_EXPIRED
        try:
            if reason is None:
                return self._store.record_webhook(
                    "lob",
                    event_id,
                    None,
                    envelope,
                    occurred_at=now,
                    new_effect_id=self._deps.new_id,
                    expected_envelope=envelope if saved is None else saved.payload,
                )
            received_at = now if saved is None else saved.received_at
            moved = self._store.move_unmatched(
                "lob",
                event_id,
                envelope if saved is None else saved.payload,
                reason,
                received_at=received_at,
                moved_at=now,
            )
        except RevisionConflict:
            raise ServiceConflict("webhook_busy") from None
        if moved:
            # Opaque ids and a fixed reason only, never provider or customer content. Only
            # evidence that needs a human is a warning; a confirmation of our own cancel is not.
            LOG.log(
                logging.INFO if reason == CANCEL_CONFIRMED else logging.WARNING,
                "unmatched lob webhook event=%s letter=%s reason=%s received_at=%s",
                _log_id(event_id),
                _log_id(ref),
                reason,
                received_at.isoformat(),
            )
        return moved

    def _replay_row(self, row: BacklogRow) -> None:
        """Replay one parked event through the shared decision path; a conflict skips it.

        Another worker or a lock held elsewhere leaves the row durable for the next pass.
        One busy row never aborts the replay, the dispatch around it, or a committed webhook.
        """
        try:
            self._record_verified_webhook(
                row.provider, json.loads(row.payload), self._deps.clock(), row
            )
        except ServiceConflict:
            return
        finally:
            self._store.defer_backlog(row)

    # Replay runs only outside coordinator transactions. A failed binding leaves the
    # envelope durable; a failed replay cannot roll back a committed create.
    def _replay_webhooks(self) -> None:
        """One bounded fair pass; unresolved rows cannot starve later acceptance evidence."""
        for row in self._store.backlog_batch(WEBHOOK_REPLAY_BATCH):
            self._replay_row(row)

    def _replay_case_webhooks(self, token: str | None) -> None:
        """When a case's cursor may have landed or changed: only the rows it matches."""
        if token is None:
            self._replay_webhooks()
            return
        for row in self._store.backlog_for_case(token, WEBHOOK_REPLAY_BATCH):
            self._replay_row(row)

    def _validate_prepared_packet(self, prepared: PreparedMailAttempt) -> None:
        """Recheck existing approved bytes using the approval path's current context.

        Called under submission ownership immediately before create. It reads saved
        facts/templates/copies, never authors, rebuilds or mutates the frozen packet.
        """
        case = self._store.get_case(prepared.case_token)
        record = None if case is None else self._store.intake_for_workspace(case.workspace_token)
        if record is None or record.case_token != prepared.case_token:
            raise ServiceConflict("research_not_ready")
        saved = self._store.get_draft(record.intake_id)
        research = self._store.get_research(record.intake_id)
        manifest = self._store.get_approval_manifest(prepared.case_token, prepared.attempt)
        if (
            saved is None
            or research is None
            or manifest is None
            or saved.adopted_by is None
            or saved.adopted_at is None
        ):
            raise ServiceConflict("research_not_ready")
        result = decode_research(research.research_json)
        intake = s.SubmitIntake.model_validate_json(record.intake_json)
        plan = replace(
            result.packet_plan, customer_fields=_intake_form_plan(intake).customer_fields
        )
        copy_view, copies = self._required_copy_state(record, research)
        if copy_view is not None and not copy_view.ready:
            raise ServiceConflict("required_copies_not_ready")
        declarations = declarations_for(result.findings, result.sources, result.ticket)
        affirmed = affirmation_map(manifest.affirmations)
        if unknown_affirmations(declarations, affirmed) or unaffirmed_required(
            declarations, affirmed
        ):
            raise ServiceConflict("declaration_unaffirmed")
        packet_text_hash = text_sha256(saved.letter_text)
        if packet_text_hash != saved.adopted_sha256:
            raise ServiceConflict("adopt_current_text_first")
        if prepared.attempt == 2:
            original = self._store.get(prepared.case_token, 1)
            if original is None:
                raise ServiceConflict("research_not_ready")
            packet_text_hash = text_sha256(
                redirect_letter_address(
                    saved.letter_text, original.request.recipient, prepared.request.recipient
                )
            )
        if packet_text_hash != manifest.approved_text_sha256:
            raise ServiceConflict("adopt_current_text_first")
        packet = Packet(
            prepared.request.pdf,
            manifest.packet_pdf_sha256,
            packet_text_hash,
            len(PdfReader(io.BytesIO(prepared.request.pdf)).pages),
        )
        approval_gate(
            Adoption(saved.adopted_by, saved.adopted_at, packet_text_hash),
            packet,
            research=result,
            answers=decode_answers(research.answers_json),
            packet_plan=plan,
            citation_number=intake.ticket.citation_number,
            required_copies=copies,
        )

    def _dispatch(self, token: str | None) -> int:
        if self._deps.lob is None:
            return 0
        self._replay_case_webhooks(token)
        processed = dispatch_once(
            self._store,
            self._original_create_client(self._deps.lob, token),
            self._deps.payments,
            self._deps.clock,
            self._deps.new_id,
            case_id=token,
            deadline_feasible=self._mailing_deadline_feasible,
            validate_prepared=self._validate_prepared_packet,
        )
        self._replay_case_webhooks(token)
        return processed

    def _original_create_client(
        self, client: CancelableLobClient, token: str | None
    ) -> CancelableLobClient:
        """Guard the actual original create (not a re-mail) with the same confirmation.

        ``dispatch_once`` commits the FROZEN -> SUBMITTING intent and only then invokes the
        create seam, so the FROZEN-only recheck cannot protect a resumed SUBMITTING case.
        """
        if self._deps.ticket_intake is None:
            return client
        return _OriginalCreateConfirmationGuard(
            client, lambda request: self._original_create_confirmed(request, token)
        )

    def _original_create_confirmed(self, request: LetterRequest, token: str | None) -> bool:
        """Revalidate the immutable intake before this exact first original create."""
        case_token = token
        if case_token is None:
            case_token = self._case_for_submission_token(request)
        if case_token is None or not self._mailing_attempts_original_create(case_token):
            return True
        try:
            self._require_ticket_confirmation_for_mailing(case_token)
        except ServiceConflict:
            return False
        return True

    def _case_for_submission_token(self, request: LetterRequest) -> str | None:
        for token in self._store.case_ids():
            prepared = self._store.get(token, 1)
            if (
                prepared is not None
                and prepared.request.submission_token == request.submission_token
            ):
                return token
        return None

    def _mailing_attempts_original_create(self, token: str) -> bool:
        """The resumed first original create seam: SUBMITTING, attempt 1, not held."""
        case = self._store.get_case(token)
        return (
            case is not None
            and case.attempt == 1
            and case.letter_state is CaseState.SUBMITTING
            and not self._held(token)
        )

    def _mailing_deadline_feasible(self, token: str, now: datetime) -> bool:
        """The existing timing seam, with the confirmation recheck before any mailing.

        ``dispatch_once`` calls this immediately before the original SubmitRequested, so a
        changed immutable intake cannot commit a create intent. The same check runs again at
        the actual create seam (``_original_create_client``) for a resumed SUBMITTING case,
        and only a genuine first original create is refused there.
        """
        if self._mailing_attempts_original(token, now):
            self._require_ticket_confirmation_for_mailing(token)
        return self._deadline_feasible(token, now)

    def _mailing_attempts_original(self, token: str, now: datetime) -> bool:
        """The FROZEN intent boundary: the first original can still be refused outright."""
        if self._deps.ticket_intake is None:
            return False
        case = self._store.get_case(token)
        if (
            case is None
            or case.letter_state is not CaseState.FROZEN
            or case.attempt != 1
            or self._held(token)
        ):
            return False
        return not (
            case.payment_state is not PaymentState.NO_CHARGE
            and now >= case.authorization_expires_at - EXPIRY_MARGIN
        )

    def _require_ticket_confirmation_for_mailing(self, token: str) -> None:
        coordinator = self._deps.ticket_intake
        if coordinator is None:
            return
        case = self._store.get_case(token)
        if case is None:
            raise ServiceConflict("not_found")
        record = self._store.intake_for_workspace(case.workspace_token)
        if record is None or record.case_token != token:
            raise ServiceConflict("ticket_confirmation_required")
        body = s.SubmitIntake.model_validate_json(record.intake_json)
        self._ticket_path(lambda: coordinator.revalidate_for_mailing(record, body))

    def _deadline_feasible(self, token: str, now: datetime) -> bool:
        """Read current durable research at the existing dispatch boundary."""
        case = self._store.get_case(token)
        record = None if case is None else self._store.intake_for_workspace(case.workspace_token)
        if record is None or record.case_token != token:
            raise ServiceConflict("research_not_ready")
        saved = self._store.get_research(record.intake_id)
        prepared = self._store.get(token, 1)
        if saved is None or prepared is None:
            raise ServiceConflict("research_not_ready")
        try:
            timing = decode_research(saved.research_json).resolve(
                decode_answers(saved.answers_json)
            )
        except (ValueError, StorageCorruption):
            raise ServiceConflict("research_not_ready") from None
        if timing.deadline is None or timing.counting is None or timing.counting.disclose:
            raise ServiceConflict("research_not_ready")
        return timing.deadline_feasible(production_date(now), prepared.request.options.mail_class)

    def mailing_record(self, case_id: str) -> s.ReturnProofPacket:
        """The customer's mailing record: the approved packet Lob accepted, with tracking.

        Only after Lob accepted the current attempt; never a USPS Return Receipt, delivery
        proof or agency decision.
        """
        record = self._intake(case_id)
        if record.case_token is not None:
            case = self._store.get_case(record.case_token)
            if case is not None:
                proof = self._mailed_proof(case, case.attempt)
                if proof is not None:
                    return proof
        raise ServiceConflict("mailing_record_not_ready")

    def tracking(self, case_id: str) -> s.Tracking:
        record = self._intake(case_id)
        status = self.status(case_id)
        events: list[s.TrackingEvent] = []
        tracking_number = None
        if record.case_token is not None:
            case = self._store.get_case(record.case_token)
            assert case is not None
            for attempt in (1, 2):
                cursor = self._store.submission_cursor(record.case_token, attempt)
                if cursor is not None and isinstance(cursor.outcome, Accepted):
                    accepted_at = next(
                        item.occurred_at
                        for item in self._store._iter_transition_history(record.case_token)
                        if item.event_name == "SubmitConfirmed"
                        and json.loads(item.event_json)["lob_letter_id"] == cursor.outcome.letter_id
                    )
                    events.append(
                        s.TrackingEvent(
                            event="accepted",
                            occurred_at=accepted_at,
                            description=(
                                "The mail provider accepted the letter; "
                                "this is not delivery confirmation."
                            ),
                        )
                    )
                    if attempt == case.attempt:
                        tracking_number = cursor.outcome.tracking_number
            for text in self._store._iter_tracking_payloads(record.case_token):
                data = json.loads(text)
                events.append(
                    s.TrackingEvent(
                        event=data["event"],
                        occurred_at=datetime.fromisoformat(data["occurred_at"]),
                        description="Mail provider tracking event; not stored delivery proof.",
                    )
                )
                if data["attempt"] == case.attempt and data["tracking_number"]:
                    tracking_number = data["tracking_number"]
        return s.Tracking(
            case_id=case_id,
            state=status.state,
            tier=status.selected_tier,
            tracking_number=tracking_number,
            events=sorted(events, key=lambda e: e.occurred_at),
            delivery_confirmation_title="ParkingBreaker Delivery Confirmation",
            fake=False,
        )
