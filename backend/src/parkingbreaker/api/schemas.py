"""Customer API wire models. Money is integer USD cents; timestamps are UTC."""

import re
from datetime import date, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    field_validator,
    model_validator,
)

from parkingbreaker.appeal.correction import Flag, TextSpan
from parkingbreaker.case_hold_store import HoldReason
from parkingbreaker.case_state import CaseState, HardStop, StopFamily
from parkingbreaker.health import AlertKind, Severity, StuckWhy
from parkingbreaker.overseer_feed import (
    OPS_ID_PATTERN,
    CaseErrorClass,
    FreshnessState,
    HealthDomain,
    SweepErrorClass,
)
from parkingbreaker.payment_state import PaymentState
from parkingbreaker.research.resolution import VehicleRelationship
from parkingbreaker.returned_mail import RemedyKind
from parkingbreaker.session_window import WindowStatus
from parkingbreaker.sweep_run_store import SweepOutcome
from parkingbreaker.ticket_vision import TicketField

if TYPE_CHECKING:
    from parkingbreaker.ticket_fact_binding import TicketFactBinding

Nonempty = Annotated[str, Field(min_length=1, max_length=20000, pattern=r"\S")]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
TicketOperationId = Annotated[
    str, Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}$")
]
TicketLiteralField = Annotated[str, Field(min_length=1, max_length=200)]
TicketFieldValue = Annotated[str, Field(min_length=1, max_length=200, pattern=r"\S")] | None


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Health(Model):
    status: Literal["ok"] = "ok"
    mode: Literal["preview", "fake", "test", "live"] = "fake"
    # Reported only when the running service actually drafts from customer text with no
    # provider call. Never a claim about a mode that is not active.
    customer_authored_mode: bool = False
    customer_authored_mode_version: str | None = None
    # Safe build and policy identity (audit A064); never exposes credentials or secrets.
    build_commit: str | None = None
    policy_hashes: dict[str, str] = Field(default_factory=dict)


class Error(Model):
    detail: str


OperationsId = Annotated[str, Field(pattern=OPS_ID_PATTERN)]
FeedCount = Annotated[int, Field(strict=True, ge=0)]


class OverseerFreshness(Model):
    state: FreshnessState
    stale: StrictBool
    last_started_at: AwareDatetime | None
    last_finished_at: AwareDatetime | None
    last_successful_finished_at: AwareDatetime | None
    last_outcome: SweepOutcome | None
    unfinished_runs: FeedCount
    error_class: SweepErrorClass | None


class OverseerAlert(Model):
    kind: AlertKind
    severity: Severity
    domain: HealthDomain
    count: Annotated[int, Field(strict=True, ge=1)]


class OverseerCase(Model):
    """Closed operational fields; no free text or internal customer identifiers."""

    ops_id: OperationsId
    letter_state: CaseState
    payment_state: PaymentState
    attempt: Literal[1, 2]
    age_hours: FeedCount
    payment_due_at: AwareDatetime | None
    submission_due_at: AwareDatetime | None
    held: StrictBool
    hold_generation: Annotated[int, Field(strict=True, ge=1)] | None
    hold_reason: HoldReason | None
    review_due_at: AwareDatetime | None
    stuck: StuckWhy | None
    error_class: CaseErrorClass | None
    ai_settled_cents: FeedCount
    ai_unsettled_reserved_cents: FeedCount
    ai_unknown_cost_calls: FeedCount


class OverseerFeed(Model):
    generated_at: AwareDatetime
    freshness: OverseerFreshness
    alerts: list[OverseerAlert]
    cases: list[OverseerCase]
    next_after: OperationsId | None


class CreateWorkspace(Model):
    notes: str = Field(default="", max_length=20000)


class Address(Model):
    line1: Nonempty
    line2: str = ""
    city: Nonempty
    state: Annotated[str, Field(pattern=r"^[A-Z]{2}$")]
    zip_code: Annotated[str, Field(pattern=r"^\d{5}(-\d{4})?$")]


class Contact(Model):
    name: Nonempty
    # SFMTA's protest form asks for a phone number; the letter carries it.
    phone: Annotated[str, Field(pattern=r"^\+?[0-9][0-9 ().-]{6,19}$")]
    email: Annotated[str, Field(max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")]
    mailing_address: Address


class TicketFacts(Model):
    citation_number: Nonempty
    license_plate: Nonempty
    license_plate_state: Annotated[str, Field(pattern=r"^[A-Z]{2}$")]
    issuer_hint: Nonempty
    # Where the ticket was issued (not the customer's address); drives issuer resolution.
    issued_city: Nonempty
    issued_state: Annotated[str, Field(pattern=r"^[A-Z]{2}$")]
    # Customer's answer to "who issued it": a public agency or a private lot operator.
    issuer_kind: Literal["GOVERNMENT", "PRIVATE"] = "GOVERNMENT"
    violation_code: Nonempty
    violation_description: Nonempty
    issue_date: date
    stage: Nonempty = "first-level review"
    notice_date: date | None = None
    printed_deadline: date | None = None


class CustomerPosition(Model):
    statement: Nonempty
    provision_selections: list[str] = Field(default_factory=list)


class PhotoUpload(Model):
    content_type: Literal["image/jpeg", "image/png"]
    # Base64 of at most 10 MB of image bytes.
    data_base64: Annotated[str, Field(min_length=4, max_length=13_400_000)]


class PhotoRef(Model):
    photo_ref: str


class SubmitIntake(Model):
    ticket: TicketFacts
    customer_position: CustomerPosition
    contact: Contact
    # Null/missing is unknown, never a negative answer. Omit null on legacy replays.
    rental_company_paid: StrictBool | Literal["NOT_RENTAL"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    # The photo of the ticket itself, printed as "Copy of citation" in the packet.
    citation_photo_ref: str | None = None
    citation_back_photo_ref: str | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    photo_refs: list[str] = Field(default_factory=list)
    # Opaque reference to the customer's current ticket confirmation. Omitted by legacy
    # submissions; persisted with the immutable intake so a later mailing can revalidate.
    ticket_confirmation_command_id: TicketOperationId | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class TicketFactBindingPayload(Model):
    """The closed literal/adopted envelope the customer confirmed for their ticket.

    It is validated by the accepted pure binding primitive; the import is local so the
    binding module can keep depending on these wire models without a cycle. Dates stay
    literal in ``literal`` and separately selected ISO calendar dates in ``adopted``.
    """

    literal: dict[str, TicketFieldValue]
    adopted: dict[str, str | None]

    @model_validator(mode="after")
    def closed_and_valid(self) -> Self:
        from parkingbreaker.ticket_fact_binding import (
            TicketFactBinding,
            TicketFactBindingError,
        )

        try:
            binding = TicketFactBinding.bind(self.literal, self.adopted)
        except TicketFactBindingError:
            raise ValueError("invalid ticket fact binding") from None
        # Canonical closed projections, so serialization round-trips exactly.
        self.literal = dict(binding.literal)
        self.adopted = dict(binding.adopted)
        return self

    @property
    def binding(self) -> "TicketFactBinding":
        """The validated immutable projection pair."""
        from parkingbreaker.ticket_fact_binding import TicketFactBinding

        return TicketFactBinding.bind(self.literal, self.adopted)


class IntakeForm(Model):
    """Incomplete customer input, kept only in the existing private workspace."""

    values: dict[str, Annotated[str, Field(max_length=20000)]] = Field(
        default_factory=dict, max_length=30
    )
    ticket_photo_ref: str | None = Field(default=None, pattern=r"^photo_[a-f0-9]{64}$")
    ticket_back_photo_ref: str | None = Field(
        default=None,
        pattern=r"^photo_[a-f0-9]{64}$",
        exclude_if=lambda value: value is None,
    )
    evidence_photo_ref: str | None = Field(default=None, pattern=r"^photo_[a-f0-9]{64}$")
    evidence_photo_refs: list[Annotated[str, Field(pattern=r"^photo_[a-f0-9]{64}$")]] | None = (
        Field(default=None, exclude_if=lambda value: value is None)
    )
    # Ticket-only confirmation envelope; unrelated story/contact edits leave it untouched.
    ticket_fact_binding: TicketFactBindingPayload | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    step: Literal[1, 2] = 1

    @model_validator(mode="after")
    def consistent_evidence_selection(self) -> Self:
        if (
            self.evidence_photo_refs is not None
            and self.evidence_photo_ref is not None
            and self.evidence_photo_refs != [self.evidence_photo_ref]
        ):
            raise ValueError("conflicting evidence photo selections")
        return self

    @property
    def selected_evidence_photo_refs(self) -> tuple[str, ...]:
        if self.evidence_photo_refs is not None:
            return tuple(self.evidence_photo_refs)
        return () if self.evidence_photo_ref is None else (self.evidence_photo_ref,)


class SaveIntakeForm(Model):
    expected_revision: Annotated[int, Field(ge=1)]
    form: IntakeForm


class WorkspaceView(Model):
    workspace_id: str
    notes: str
    intake: SubmitIntake | None = None
    case_id: str | None = None
    draft_revision: int = Field(default=1, exclude_if=lambda value: value == 1)
    intake_form: IntakeForm | None = Field(default=None, exclude_if=lambda value: value is None)
    # Whether this process requires the ticket-reading confirmation before submit.
    ticket_reading_required: bool = Field(default=False, exclude_if=lambda value: value is False)
    work_preserved: Literal[True] = True


class IntakeReceipt(Model):
    workspace_id: str
    case_id: str
    state: CaseState = CaseState.INTAKE
    pre_hold_status: Literal["ready"] = "ready"


class MailTier(StrEnum):
    CERTIFIED = "CERTIFIED"
    STANDARD = "STANDARD"


class TierOffer(Model):
    tier: MailTier
    amount_cents: Annotated[int, Field(gt=0)]
    currency: Literal["usd"] = "usd"
    description: str


class ResolvedIssuer(Model):
    issuer_id: str
    issuer_name: str
    deciding_body: str
    state: str
    stage: str
    service_basis: str
    delegated_tenant: str | None = None
    evidence_ref: str


class Offer(Model):
    kind: Literal["offer"] = "offer"
    case_id: str
    issuer: ResolvedIssuer
    tiers: list[TierOffer]
    selected_tier: MailTier | None = None


class Stop(Model):
    kind: Literal["stop"] = "stop"
    case_id: str
    family: StopFamily
    hard_stop: HardStop | None = None
    reason_key: str
    message: str
    no_charge: Literal[True] = True
    work_preserved: Literal[True] = True
    official_quote: str | None = None
    official_url: str | None = Field(default=None, exclude_if=lambda value: value is None)


type PreHoldResult = Annotated[Offer | Stop, Field(discriminator="kind")]


class ChooseTier(Model):
    tier: MailTier


class StartAuthorization(Model):
    terms_accepted: Literal[True]
    # Created in the browser by Stripe.js with the publishable key; card data never
    # reaches our API (extra="forbid" rejects any other payment field).
    payment_method_id: str = Field(pattern=r"^pm_[A-Za-z0-9_]{1,250}$")


class BetaOffer(Model):
    """Read-only view of one selected free entitlement; never a payment or mailing offer."""

    case_id: str
    funding: Literal["NO_CHARGE"] = "NO_CHARGE"
    amount_cents: Literal[0] = 0
    currency: Literal["usd"] = "usd"
    tier: Literal[MailTier.CERTIFIED] = MailTier.CERTIFIED
    disclosure_version: int
    disclosure_text: str
    disclosure_sha256: Digest
    eligible_until: datetime
    accepted_at: datetime | None


class AcceptBeta(Model):
    """Customer consent to the exact free-beta disclosure; no payment or campaign input."""

    terms_accepted: Literal[True]
    disclosure_sha256: Digest


class BetaAcceptanceReceipt(Model):
    """Durable zero-charge acceptance. Replays return the same persisted timestamps."""

    case_id: str
    funding: Literal["NO_CHARGE"] = "NO_CHARGE"
    amount_cents: Literal[0] = 0
    currency: Literal["usd"] = "usd"
    tier: Literal[MailTier.CERTIFIED] = MailTier.CERTIFIED
    accepted_at: AwareDatetime
    window_started_at: AwareDatetime
    window_expires_at: AwareDatetime


class Authorization(Model):
    case_id: str
    payment_intent_id: str
    # Preview only: the real path confirms server-side with the browser's pm_ id.
    client_secret: str | None = None
    amount_cents: int
    currency: Literal["usd"] = "usd"
    status: Literal["requires_confirmation", "authorized"]
    window_started_at: datetime | None = None
    window_expires_at: datetime | None = None
    fake: bool = True


class ClosedQuestion(Model):
    question_id: Literal["VEHICLE_RELATIONSHIP"] = "VEHICLE_RELATIONSHIP"
    prompt: str
    choices: list[VehicleRelationship]


class AnswerQuestion(Model):
    question_id: Literal["VEHICLE_RELATIONSHIP"]
    answer: VehicleRelationship


PaymentQuestionId = Literal["PAID_FINE", "PAID_HEARING_DEPOSIT", "ENTERED_PLAN"]


class PaymentClosedQuestion(Model):
    question_id: PaymentQuestionId
    prompt: str
    choices: list[StrictBool | None]


class PaymentAnswerQuestion(Model):
    question_id: PaymentQuestionId
    answer: StrictBool | None


CaseAnswer = Annotated[AnswerQuestion | PaymentAnswerQuestion, Field(discriminator="question_id")]
CaseQuestion = Annotated[ClosedQuestion | PaymentClosedQuestion, Field(discriminator="question_id")]


class ResearchSource(Model):
    url: str
    authority: str
    retrieved_at: datetime


class ResearchFinding(Model):
    fact: str
    status: Literal["CONFIRMED", "NOT_APPLICABLE", "UNKNOWN", "CONFLICTED"]
    official_quote: str | None = None


class ResearchView(Model):
    """The live research view: official pages opened and what they showed."""

    sources: list[ResearchSource]
    findings: list[ResearchFinding]


class RequiredCopySelection(Model):
    requirement_id: Digest
    page_refs: list[Annotated[str, Field(pattern=r"^photo_[a-f0-9]{64}$")]] = Field(
        min_length=1, max_length=10
    )
    confirmed: StrictBool

    @model_validator(mode="after")
    def customer_confirmation(self) -> Self:
        if not self.confirmed:
            raise ValueError("confirm the complete document copy")
        return self


class SaveRequiredCopies(Model):
    research_sha256: Digest
    expected_revision: Annotated[int, Field(ge=0, strict=True)]
    selections: list[RequiredCopySelection] = Field(max_length=20)


class RequiredCopyItem(Model):
    requirement_id: Digest
    title: str
    official_quote: str
    source_urls: list[str]
    page_refs: list[str]


class RequiredCopiesView(Model):
    research_sha256: Digest
    revision: int
    items: list[RequiredCopyItem]
    ready: bool
    editable: bool


AddressText = Annotated[str, Field(min_length=1, max_length=100, pattern=r"\S")]


class DestinationChoice(Model):
    """A pending customer-directed destination choice. Present only while one is required."""

    required: Literal[True] = True
    reason_code: str
    disclosure_version: str
    disclosure_sha256: Digest
    # The customer's latest saved address, which a consent would have to name; None until saved.
    address_revision: int | None = None
    address_fingerprint: Digest | None = None


class DestinationDisclosure(Model):
    version: str
    sha256: Digest
    text: str


class DestinationAddress(Model):
    """A complete US postal destination the customer supplies. Never inferred or defaulted."""

    recipient: AddressText
    attention: AddressText | None = None
    line1: AddressText
    line2: AddressText | None = None
    city: AddressText
    state: Annotated[str, Field(pattern=r"^[A-Z]{2}$")]
    zip_code: Annotated[str, Field(pattern=r"^\d{5}(-\d{4})?$")]


class DestinationAddressReceipt(Model):
    case_id: str
    address_revision: Annotated[int, Field(ge=1)]
    address_fingerprint: Digest


class DestinationConsent(Model):
    """Explicit acceptance bound to one saved address revision and the shown disclosure.

    `accepted` has no default and must be the JSON boolean true.
    """

    address_revision: Annotated[int, Field(ge=1, strict=True)]
    address_fingerprint: Digest
    disclosure_sha256: Digest
    accepted: Annotated[bool, Field(strict=True)]


class DestinationConsentReceipt(Model):
    case_id: str
    provenance: Literal["CUSTOMER_DIRECTED_UNVERIFIED"] = "CUSTOMER_DIRECTED_UNVERIFIED"
    address_revision: Annotated[int, Field(ge=1)]
    address_fingerprint: Digest
    disclosure_version: str
    disclosure_sha256: Digest
    accepted_at: datetime


class ReturnProofEvent(Model):
    event: str
    occurred_at: datetime


class ReturnProofPacket(Model):
    """Stored mailing records; never a claim of delivery or an agency decision.

    Also the customer's mailing record for any accepted letter: the exact approved packet
    and the provider's tracking events, not a USPS Return Receipt.
    """

    pdf_base64: str
    pdf_sha256: Digest
    letter_id: str
    tracking_number: str | None
    events: list[ReturnProofEvent]


class ReturnedMailRemedy(Model):
    attempt: Literal[1, 2]
    kind: RemedyKind | Literal["ADDRESS_EXCLUSION", "BETA_RETURN_CLOSED", "RETURN_PENDING"]
    reason_code: Annotated[str, Field(pattern=r"^[A-Z0-9_]+$")]
    message: str
    refund_cents: Annotated[int, Field(ge=0)]
    refund_status: Literal["pending", "completed"] | None
    remail: bool
    proof_packet: ReturnProofPacket | None = None


class CaseStatus(Model):
    case_id: str
    workspace_id: str
    state: CaseState
    payment_state: PaymentState | None
    selected_tier: MailTier | None
    amount_cents: int | None = Field(default=None, exclude_if=lambda value: value is None)
    window_started_at: datetime | None
    window_expires_at: datetime | None
    seconds_remaining: int | None
    window_status: WindowStatus | None
    question: CaseQuestion | None
    stop: Stop | None
    research: ResearchView | None = None
    required_copies: RequiredCopiesView | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    draft_spans: list[TextSpan] = Field(default_factory=list, exclude_if=lambda value: not value)
    draft_flags: list[Flag] = Field(default_factory=list, exclude_if=lambda value: not value)
    destination_choice: DestinationChoice | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    return_remedy: ReturnedMailRemedy | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    fake: bool = True


class Draft(Model):
    case_id: str
    revision: int
    text: str
    text_sha256: Digest
    adopted_sha256: Digest | None
    adoption_screen_text: str
    spans: list[TextSpan] = Field(default_factory=list, exclude_if=lambda value: not value)
    flags: list[Flag] = Field(default_factory=list, exclude_if=lambda value: not value)


class EditDraft(Model):
    text: Nonempty
    expected_sha256: Digest
    replace_customer_content: bool = Field(default=False, strict=True)


class AdoptText(Model):
    text: Nonempty
    text_sha256: Digest


class AdoptionReceipt(Model):
    case_id: str
    adopted_sha256: Digest
    adopted_at: datetime


class PacketPreviewRequest(Model):
    adopted_sha256: Digest
    # The customer's drawn signature, uploaded through the photos route (PNG), used only
    # when the agency's own requirements call for a signature.
    signature_ref: str | None = None


class DeclarationView(Model):
    """One official declaration the customer must affirm. Never pre-checked."""

    declaration_id: str
    field_name: str
    verbatim_text: str
    text_sha256: Digest
    required: bool
    signature_required: bool


class DeclarationAffirmation(Model):
    declaration_id: str
    text_sha256: Digest


class PacketPreview(Model):
    pdf_base64: str
    packet_sha256: Digest
    envelope_sha256: Digest
    letter_text_sha256: Digest
    page_count: int = Field(ge=1)
    mail_to: list[str]
    return_address: list[str]
    declarations: list[DeclarationView] = []


class Approve(PacketPreviewRequest):
    packet_sha256: Digest
    envelope_sha256: Digest
    # Explicit customer affirmations. Empty means nothing was affirmed, which blocks
    # approval whenever the agency requires a declaration.
    affirmations: list[DeclarationAffirmation] = []


class WorkerStep(Model):
    step_id: Annotated[str, Field(min_length=1, max_length=100)]


class TrackingEvent(Model):
    event: str
    occurred_at: datetime
    description: str


class Tracking(Model):
    case_id: str
    state: CaseState
    tier: MailTier | None
    tracking_number: str | None
    events: list[TrackingEvent]
    delivery_confirmation_title: Literal["ParkingBreaker Delivery Confirmation"]
    fake: bool = True


# Accepted-beta customer-reported notices -------------------------------------------------
_NoticeRef = Annotated[str, Field(pattern=r"^photo_[a-f0-9]{64}$")]
_NoticeId = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")


def _exact_iso_date(value: object) -> str | None:
    """Accept only a JSON string in canonical YYYY-MM-DD form, or null."""
    if value is None:
        return None
    if not isinstance(value, str) or _ISO_DATE.fullmatch(value) is None:
        raise ValueError("invalid beta feedback date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError("invalid beta feedback date") from None
    if parsed.isoformat() != value:
        raise ValueError("invalid beta feedback date")
    return value


class BetaFeedbackNoticePageInput(Model):
    """One saved photo in the customer's stated order; role is never inferred."""

    role: Literal["envelope", "page"]
    photo_ref: _NoticeRef


class BetaFeedbackNoticeRequest(Model):
    """Customer-reported notice metadata only; no owner, status or evidence claims."""

    request_id: Annotated[
        str, Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}$")
    ]
    pages: list[BetaFeedbackNoticePageInput] = Field(min_length=1, max_length=20)
    customer_seen_on: str | None = None
    printed_on: str | None = None
    stated_deadline_text: Annotated[str, Field(max_length=1000)] | None = None
    correction_of: _NoticeId | None = None

    @field_validator("customer_seen_on", "printed_on", mode="before")
    @classmethod
    def exact_dates(cls, value: object) -> str | None:
        return _exact_iso_date(value)

    @model_validator(mode="after")
    def unique_photo_refs(self) -> Self:
        refs = [page.photo_ref for page in self.pages]
        if len(refs) != len(set(refs)):
            raise ValueError("duplicate notice photo reference")
        return self


class BetaFeedbackNoticePageView(Model):
    """Saved 1-based page order; no photo reference or storage path is exposed."""

    page_number: Annotated[int, Field(ge=1, le=20)]
    role: Literal["envelope", "page"]


class BetaFeedbackReportedFields(Model):
    """Exactly what the customer reported, uninterpreted; never an agency deadline claim."""

    customer_seen_on: date | None
    printed_on: date | None
    stated_deadline_text: str | None


class BetaFeedbackNoticeReceipt(Model):
    """Owner-safe receipt: no owner, case token, photo ref or payload digest."""

    notice_id: _NoticeId
    case_id: str
    request_id: str
    received_at: AwareDatetime
    pages: list[BetaFeedbackNoticePageView]
    reported_fields: BetaFeedbackReportedFields
    correction_of: _NoticeId | None
    needs_confirmation: Literal[True] = True
    review_state: Literal["unreviewed"] = "unreviewed"


class BetaFeedbackNoticeList(Model):
    notices: list[BetaFeedbackNoticeReceipt]
    next_cursor: str | None


# Ticket reading, independent review and customer confirmation ---------------------------
class TicketOperationRequest(Model):
    """One idempotent vision request; the caller's request id is the replay key."""

    request_id: TicketOperationId
    expected_revision: Annotated[int, Field(ge=1, strict=True)]


class TicketReadingField(Model):
    """One closed reading suggestion or an explicit absence state. Never a verified fact."""

    field: TicketField
    state: Literal["shown", "not_shown", "unreadable"]
    value: str | None


class TicketReadingSnapshot(Model):
    """The current durable extraction for this workspace, replayable after a restart."""

    request_id: str | None = None
    status: Literal["missing", "pending", "success", "refused", "unknown"] = "missing"
    current: bool = False
    synthetic: bool = False
    fields: list[TicketReadingField] = Field(default_factory=list)


class TicketReviewField(Model):
    """One model comparison and the literal visible text; never adoption or verification."""

    field: TicketField
    comparison: Literal["match", "conflict", "not_shown", "unreadable"]
    visible_text: str | None


class TicketReviewSnapshot(Model):
    """The current durable independent review for this workspace."""

    request_id: str | None = None
    status: Literal["missing", "pending", "success", "refused", "unknown"] = "missing"
    current: bool = False
    synthetic: bool = False
    basis: Literal["model comparison only; not external verification"] = (
        "model comparison only; not external verification"
    )
    fields: list[TicketReviewField] = Field(default_factory=list)


class TicketDiscrepancyResolution(StrEnum):
    """The customer's explicit answer to each non-match comparison; never inferred."""

    CONFIRMED_VALUE_STANDS = "CONFIRMED_VALUE_STANDS"
    ABSENCE_ACKNOWLEDGED = "ABSENCE_ACKNOWLEDGED"
    UNREADABLE_ACKNOWLEDGED = "UNREADABLE_ACKNOWLEDGED"


class TicketConfirmationRequest(Model):
    """One explicit confirmation command for the current saved envelope and review."""

    command_id: TicketOperationId
    expected_revision: Annotated[int, Field(ge=1, strict=True)]
    review_request_id: TicketOperationId
    expected_confirmation_generation: Annotated[int, Field(ge=0, strict=True)]
    resolved_fields: dict[str, TicketDiscrepancyResolution] = Field(
        default_factory=dict, max_length=11
    )

    @field_validator("resolved_fields")
    @classmethod
    def closed_literal_fields(
        cls, value: dict[str, TicketDiscrepancyResolution]
    ) -> dict[str, TicketDiscrepancyResolution]:
        allowed = {field.value for field in TicketField}
        if any(key not in allowed for key in value):
            raise ValueError("resolved fields must be closed literal ticket fields")
        return value


class TicketConfirmationSnapshot(Model):
    """Current, stale (with a fixed reason) or missing confirmation. Never verification."""

    state: Literal["missing", "current", "stale"] = "missing"
    command_id: str | None = None
    review_request_id: str | None = None
    generation: Annotated[int, Field(ge=0)] = 0
    discrepancy_status: Literal["none", "resolved", "unresolved"] | None = None
    reason: str | None = None


class TicketReadingStatus(Model):
    """Owner-safe capability and durable snapshot; no claim token or operator internals."""

    enabled: bool
    required: bool
    synthetic: bool = False
    extraction: TicketReadingSnapshot = Field(default_factory=TicketReadingSnapshot)
    review: TicketReviewSnapshot = Field(default_factory=TicketReviewSnapshot)
    confirmation: TicketConfirmationSnapshot = Field(default_factory=TicketConfirmationSnapshot)
    next_confirmation_generation: Annotated[int, Field(ge=0)] = 0


class TicketConfirmationReceipt(Model):
    """A recorded customer confirmation, replayable by its exact command id."""

    command_id: str
    state: Literal["current"] = "current"
    generation: Annotated[int, Field(ge=1)]
    review_request_id: str
    discrepancy_status: Literal["none", "resolved", "unresolved"]
    synthetic: bool = False
