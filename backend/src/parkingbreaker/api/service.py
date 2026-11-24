"""The one interface the API calls. The preview fake and the real service implement it.

Routes check ownership with workspace_owner/case_owner before any other call, so every
other method may assume the id exists and belongs to the caller.
"""

import base64
import binascii
from dataclasses import dataclass
from typing import Protocol

from parkingbreaker.api import schemas as s
from parkingbreaker.blob_store import BlobStore, PhotoRejected
from parkingbreaker.service_phase import ServicePhase


class ServiceConflict(ValueError):
    """A refused request. Carries only a stable reason key, never customer content."""


@dataclass(frozen=True)
class SweepFailure:
    """One case the scheduler could not advance; reason is a stable key, never customer text."""

    case_id: str
    reason: str


@dataclass(frozen=True)
class SweepResult:
    processed: int
    failures: tuple[SweepFailure, ...] = ()


class CaseServices(Protocol):
    @property
    def service_phase(self) -> ServicePhase:
        """The effective phase: stored runtime override, else the configured fallback."""
        ...

    @property
    def ticket_reading_required(self) -> bool:
        """Whether this process requires the ticket-reading confirmation path."""
        ...

    def workspace_owner(self, workspace_id: str) -> str | None: ...

    def case_owner(self, case_id: str) -> str | None: ...

    def refresh(self, case_id: str) -> None:
        """Apply time-driven transitions (window expiry) before a customer read."""

    def create_workspace(self, owner: str, body: s.CreateWorkspace) -> s.WorkspaceView: ...

    def appeal_workspace(self, owner: str) -> s.WorkspaceView | None: ...
    def workspace_finished(self, workspace_id: str) -> bool:
        """Whether this workspace's transaction is terminal (a restart is allowed)."""
        ...

    def workspace_view(self, workspace_id: str) -> s.WorkspaceView: ...

    def save_intake_form(self, workspace_id: str, body: s.SaveIntakeForm) -> s.WorkspaceView: ...

    def upload_photo(self, workspace_id: str, body: s.PhotoUpload) -> s.PhotoRef: ...

    def ticket_reading_status(self, workspace_id: str) -> s.TicketReadingStatus: ...

    def request_ticket_reading(
        self, workspace_id: str, body: s.TicketOperationRequest
    ) -> s.TicketReadingSnapshot: ...

    def request_ticket_review(
        self, workspace_id: str, body: s.TicketOperationRequest
    ) -> s.TicketReviewSnapshot: ...

    def confirm_ticket_reading(
        self, workspace_id: str, body: s.TicketConfirmationRequest
    ) -> s.TicketConfirmationReceipt: ...

    def ticket_observability(self, workspace_id: str) -> s.TicketObservabilitySnapshot: ...

    def start_ticket_review_session(
        self, workspace_id: str, body: s.TicketReviewSessionRequest
    ) -> s.TicketReviewSession: ...

    def record_ticket_review_observation(
        self,
        workspace_id: str,
        session_id: str,
        body: s.TicketReviewObservationRequest,
    ) -> s.TicketReviewObservationReceipt: ...

    def submit(self, workspace_id: str, body: s.SubmitIntake) -> s.IntakeReceipt: ...

    def pre_hold(self, case_id: str) -> s.PreHoldResult: ...

    def choose(self, case_id: str, body: s.ChooseTier) -> s.Offer: ...

    def authorize(self, case_id: str, body: s.StartAuthorization) -> s.Authorization: ...

    def recover_authorization(self, case_id: str) -> s.CaseStatus: ...

    def beta_offer(self, case_id: str) -> s.BetaOffer: ...

    def accept_beta(self, case_id: str, body: s.AcceptBeta) -> s.BetaAcceptanceReceipt: ...

    def record_beta_feedback_notice(
        self, case_id: str, body: s.BetaFeedbackNoticeRequest
    ) -> s.BetaFeedbackNoticeReceipt: ...

    def beta_feedback_notices(
        self, case_id: str, *, limit: int = 100, cursor: str | None = None
    ) -> s.BetaFeedbackNoticeList: ...

    def beta_feedback_notice_page(
        self, case_id: str, notice_id: str, page_number: int
    ) -> bytes: ...

    def pilot_feedback(self, case_id: str) -> s.PilotFeedbackView: ...

    def record_pilot_feedback(
        self, case_id: str, body: s.PilotFeedbackUpdate
    ) -> s.PilotFeedbackView: ...

    def status(self, case_id: str) -> s.CaseStatus: ...

    def answer_question(self, case_id: str, body: s.CaseAnswer) -> s.CaseStatus: ...

    def save_required_copies(self, case_id: str, body: s.SaveRequiredCopies) -> s.CaseStatus: ...

    def required_copy_page(self, case_id: str, requirement_id: str, page_number: int) -> bytes: ...

    def draft(self, case_id: str) -> s.Draft: ...

    def edit(self, case_id: str, body: s.EditDraft) -> s.Draft: ...

    def adopt_text(self, case_id: str, body: s.AdoptText) -> s.AdoptionReceipt: ...

    def packet_preview(self, case_id: str, body: s.PacketPreviewRequest) -> s.PacketPreview: ...

    def approve(self, case_id: str, body: s.Approve) -> s.CaseStatus: ...

    def destination_disclosure(self, case_id: str) -> s.DestinationDisclosure: ...

    def save_destination_address(
        self, case_id: str, body: s.DestinationAddress
    ) -> s.DestinationAddressReceipt: ...

    def consent_destination(
        self, case_id: str, body: s.DestinationConsent
    ) -> s.DestinationConsentReceipt: ...

    def decline_destination(self, case_id: str) -> s.CaseStatus: ...

    def tracking(self, case_id: str) -> s.Tracking: ...
    def mailing_record(self, case_id: str) -> s.ReturnProofPacket: ...

    def dispatch(self, case_id: str | None = None) -> int: ...

    def advance_due(self) -> SweepResult:
        """Scheduler sweep: every due worker step for every case, then the outbox."""

    def webhook(self, provider: str, payload: bytes, headers: dict[str, str]) -> bool: ...

    def step(self, case_id: str, body: s.WorkerStep) -> s.CaseStatus: ...


def store_upload(store: BlobStore, owner: str, body: s.PhotoUpload) -> str:
    """Decode and keep one customer photo; refusals carry a stable key only."""
    try:
        data = base64.b64decode(body.data_base64, validate=True)
    except (binascii.Error, ValueError):
        raise ServiceConflict("photo_not_base64") from None
    try:
        return store.put(owner, data)
    except PhotoRejected as exc:
        raise ServiceConflict(str(exc)) from None
