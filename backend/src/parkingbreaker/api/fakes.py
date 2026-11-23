"""In-memory preview script, using existing machines; never production orchestration.

Issuer, evidence, eligibility, draft and mailing acceptance are fabricated fixtures.
No research, packet validation, provider calls or persistence are implemented here.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from parkingbreaker import case_state as cs
from parkingbreaker import payment_state as ps
from parkingbreaker.address_disclosure import DISCLOSURE_SHA256, DISCLOSURE_TEXT, DISCLOSURE_VERSION
from parkingbreaker.api import schemas as s
from parkingbreaker.api.service import ServiceConflict, SweepResult, store_upload
from parkingbreaker.appeal.adoption import (
    FINAL_SCREEN_TEXT,
    Adoption,
    adopt,
    text_sha256,
)
from parkingbreaker.blob_store import FakeBlobStore
from parkingbreaker.case_service import intake_stop_view
from parkingbreaker.prices import CERTIFIED_CENTS, STANDARD_CENTS
from parkingbreaker.research.resolution import VehicleRelationship
from parkingbreaker.service_phase import ServicePhase
from parkingbreaker.service_policy import LAUNCH_STANDARD_OFFERED, intake_customer_stop
from parkingbreaker.session_window import (
    DEFAULT_WINDOW,
    WindowStatus,
    applies_to,
    seconds_remaining,
    window_status,
)
from parkingbreaker.submit_gate import hashes_match
from parkingbreaker.ticket_intake_service import (
    TicketIntakeCoordinator,
    TicketIntakeServiceError,
)
from parkingbreaker.workspace import Workspace


class FakeConflict(ServiceConflict):
    """Carries only a stable reason key, never customer content."""


@dataclass
class FakeWorkspace:
    owner: str
    view: s.WorkspaceView
    saved: Workspace


@dataclass
class FakeCase:
    owner: str
    workspace_id: str
    intake: s.SubmitIntake
    pre_hold: s.Offer | s.Stop
    state: cs.CaseState = cs.CaseState.INTAKE
    payment: ps.PaymentState | None = None
    authorization: s.Authorization | None = None
    started_at: datetime | None = None
    question: s.ClosedQuestion | None = None
    answer: s.AnswerQuestion | None = None
    text: str = ""
    revision: int = 0
    adoption: Adoption | None = None
    stop: s.Stop | None = None
    events: list[s.TrackingEvent] = field(default_factory=list)
    steps: set[str] = field(default_factory=set)


class FakeServices:
    def __init__(
        self,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        pre_hold_stop: s.Stop | None = None,
        standard_offered: bool = LAUNCH_STANDARD_OFFERED,
        service_phase: ServicePhase = ServicePhase.PAID,
        ticket_intake: TicketIntakeCoordinator | None = None,
    ) -> None:
        self.clock = clock
        self.pre_hold_stop = pre_hold_stop
        self.standard_offered = standard_offered
        self._service_phase = service_phase
        # Preview is disabled by default; an explicit composition can inject the path.
        self.ticket_intake = ticket_intake
        self.workspaces: dict[str, FakeWorkspace] = {}
        self.cases: dict[str, FakeCase] = {}
        self.photos = FakeBlobStore()

    @property
    def service_phase(self) -> ServicePhase:
        """The preview's authoritative phase; never inferred from a request."""
        return self._service_phase

    def appeal_workspace(self, owner: str) -> s.WorkspaceView | None:
        views = [
            workspace.view for workspace in self.workspaces.values() if workspace.owner == owner
        ]
        if len(views) > 1:
            raise FakeConflict("ambiguous_legacy_scope")
        return views[0] if views else None

    def workspace_finished(self, workspace_id: str) -> bool:
        """Preview has no durable transaction, so a start always replays its workspace."""
        return False

    def create_workspace(self, owner: str, body: s.CreateWorkspace) -> s.WorkspaceView:
        workspace_id = f"ws_{uuid4().hex}"
        view = s.WorkspaceView(
            workspace_id=workspace_id,
            notes=body.notes,
            ticket_reading_required=self.ticket_reading_required,
        )
        self.workspaces[workspace_id] = FakeWorkspace(owner, view, Workspace(notes=body.notes))
        return view

    def _submit(self, workspace: FakeWorkspace, body: s.SubmitIntake) -> s.IntakeReceipt:
        if (
            body.citation_back_photo_ref is not None
            and self.photos.get(workspace.owner, body.citation_back_photo_ref) is None
        ):
            raise FakeConflict("unknown_photo_ref")
        if workspace.view.case_id:
            if workspace.view.intake != body:
                raise FakeConflict("intake_already_submitted_create_new_workspace")
            return s.IntakeReceipt(
                workspace_id=workspace.view.workspace_id,
                case_id=workspace.view.case_id,
                state=self.cases[workspace.view.case_id].state,
            )
        case_id = f"case_{uuid4().hex}"
        tiers = [
            s.TierOffer(
                tier=s.MailTier.CERTIFIED,
                amount_cents=CERTIFIED_CENTS,
                description="Certified mail with tracking; no promised arrival date.",
            )
        ]
        if self.standard_offered:
            tiers.append(
                s.TierOffer(
                    tier=s.MailTier.STANDARD,
                    amount_cents=STANDARD_CENTS,
                    description="First-class mail; no arrival guarantee.",
                )
            )
        offer = s.Offer(
            case_id=case_id,
            issuer=s.ResolvedIssuer(
                issuer_id="demo-ca",
                issuer_name="Demo Municipal Parking Agency",
                deciding_body="Demo Parking Administrative Review",
                state="CA",
                stage=body.ticket.stage,
                service_basis="FAKE_LOCAL_GOVERNMENT_FIXTURE",
                evidence_ref="fake://pre-hold/demo-ca",
            ),
            tiers=tiers,
        )
        pre_hold = (
            self.pre_hold_stop.model_copy(update={"case_id": case_id})
            if self.pre_hold_stop
            else offer
        )
        early_stop = intake_customer_stop(
            body.ticket.issue_date, self.clock().date(), body.rental_company_paid
        )
        if early_stop is not None and self.pre_hold_stop is None:
            pre_hold = intake_stop_view(case_id, early_stop)
        case = FakeCase(workspace.owner, workspace.view.workspace_id, body, pre_hold)
        if isinstance(pre_hold, s.Stop):
            case.stop = pre_hold
            case.state = cs.transition(
                case.state,
                cs.Stopped(
                    reason=cs.StopReason(pre_hold.family.value)
                    if pre_hold.family is not cs.StopFamily.TRANSACTION_FAILURE
                    else cs.StopReason.DECLINED,
                    hard_stop=pre_hold.hard_stop,
                    detail=pre_hold.official_quote or "",
                ),
            ).target
        self.cases[case_id] = case
        workspace.view = workspace.view.model_copy(update={"case_id": case_id, "intake": body})
        workspace.saved = Workspace(
            notes=workspace.view.notes,
            position=body.customer_position.statement,
            photo_refs=tuple(body.photo_refs),
            provision_selections=frozenset(body.customer_position.provision_selections),
        )
        return s.IntakeReceipt(
            workspace_id=workspace.view.workspace_id, case_id=case_id, state=case.state
        )

    def _offer(self, case: FakeCase) -> s.Offer:
        if not isinstance(case.pre_hold, s.Offer):
            raise FakeConflict("pre_hold_stopped")
        return case.pre_hold

    def _choose(self, case: FakeCase, body: s.ChooseTier) -> s.Offer:
        if case.state is cs.CaseState.STOPPED_NO_CHARGE:
            raise FakeConflict("case_stopped")
        offer = self._offer(case)
        if case.authorization:
            if offer.selected_tier == body.tier:
                return offer
            raise FakeConflict("tier_locked_at_authorization")
        if body.tier not in {tier.tier for tier in offer.tiers}:
            raise FakeConflict("tier_not_offered")
        offer.selected_tier = body.tier
        return offer

    def _authorize(self, case: FakeCase) -> s.Authorization:
        if case.state is cs.CaseState.STOPPED_NO_CHARGE:
            raise FakeConflict("case_stopped")
        offer = self._offer(case)
        if case.authorization:
            return case.authorization
        if self.service_phase is ServicePhase.CONTROLLED_FREE_BETA:
            raise FakeConflict("paid_orders_paused")
        if offer.selected_tier is None:
            raise FakeConflict("choose_tier_first")
        tier = next(t for t in offer.tiers if t.tier == offer.selected_tier)
        case.authorization = s.Authorization(
            case_id=offer.case_id,
            payment_intent_id=f"pi_fake_{offer.case_id}",
            client_secret=f"pi_fake_{offer.case_id}_secret_placeholder",
            amount_cents=tier.amount_cents,
            status="requires_confirmation",
        )
        return case.authorization

    def _expire(self, case: FakeCase) -> None:
        if (
            case.started_at
            and applies_to(case.state)
            and window_status(case.started_at, self.clock()) is WindowStatus.EXPIRED
        ):
            case.state = cs.transition(case.state, cs.WindowExpired()).target
            if case.payment is ps.PaymentState.AUTHORIZED:
                case.payment = ps.payment_transition(case.payment, ps.ReleaseRequested()).target
            case.stop = s.Stop(
                case_id=case.pre_hold.case_id,
                family=cs.StopFamily.TRANSACTION_FAILURE,
                reason_key="window_expired",
                message="The review window ended. You were not charged.",
            )

    def _status(self, case: FakeCase) -> s.CaseStatus:
        self._expire(case)
        return s.CaseStatus(
            case_id=case.pre_hold.case_id,
            workspace_id=case.workspace_id,
            state=case.state,
            payment_state=case.payment,
            amount_cents=case.authorization.amount_cents if case.authorization else None,
            selected_tier=case.pre_hold.selected_tier
            if isinstance(case.pre_hold, s.Offer)
            else None,
            window_started_at=case.started_at,
            window_expires_at=case.started_at + DEFAULT_WINDOW if case.started_at else None,
            seconds_remaining=seconds_remaining(case.started_at, self.clock())
            if case.started_at and applies_to(case.state)
            else None,
            window_status=window_status(case.started_at, self.clock())
            if case.started_at and applies_to(case.state)
            else None,
            question=case.question if case.state is cs.CaseState.NEEDS_CUSTOMER else None,
            stop=case.stop,
        )

    def _answer_question(self, case: FakeCase, body: s.CaseAnswer) -> s.CaseStatus:
        if not isinstance(body, s.AnswerQuestion):
            raise FakeConflict("no_open_question")
        if case.answer == body:
            return self._status(case)
        if case.state is not cs.CaseState.NEEDS_CUSTOMER:
            raise FakeConflict("no_open_question")
        case.answer = body
        workspace = self.workspaces[case.workspace_id]
        workspace.saved = Workspace(
            notes=workspace.saved.notes,
            position=workspace.saved.position,
            photo_refs=workspace.saved.photo_refs,
            provision_selections=workspace.saved.provision_selections,
            answers={body.question_id: body.answer.value},
        )
        case.state = cs.transition(case.state, cs.CustomerAnswered()).target
        return self._status(case)

    def _draft(self, case: FakeCase) -> s.Draft:
        if not case.text:
            raise FakeConflict("draft_not_ready")
        return s.Draft(
            case_id=case.pre_hold.case_id,
            revision=case.revision,
            text=case.text,
            text_sha256=text_sha256(case.text),
            adopted_sha256=case.adoption.sha256 if case.adoption else None,
            adoption_screen_text=FINAL_SCREEN_TEXT,
        )

    def _edit(self, case: FakeCase, body: s.EditDraft) -> s.Draft:
        if case.state is not cs.CaseState.AWAITING_ADOPTION:
            raise FakeConflict("draft_not_editable")
        if case.text == body.text:
            return self._draft(case)
        if not hashes_match(body.expected_sha256, text_sha256(case.text)):
            raise FakeConflict("stale_draft")
        case.text = body.text
        case.revision += 1
        case.adoption = None
        saved = self.workspaces[case.workspace_id].saved
        self.workspaces[case.workspace_id].saved = Workspace(
            notes=saved.notes,
            position=saved.position,
            photo_refs=saved.photo_refs,
            provision_selections=saved.provision_selections,
            letter_edits=(*saved.letter_edits, body.text),
            answers=saved.answers,
        )
        return self._draft(case)

    def _adopt_text(self, case: FakeCase, body: s.AdoptText) -> s.AdoptionReceipt:
        if case.text != body.text or not hashes_match(body.text_sha256, text_sha256(case.text)):
            raise FakeConflict("stale_draft")
        if case.state is not cs.CaseState.AWAITING_ADOPTION and case.adoption is None:
            raise FakeConflict("draft_not_adoptable")
        if case.adoption is None:
            case.adoption = adopt(case.text, case.owner, self.clock)
        return s.AdoptionReceipt(
            case_id=case.pre_hold.case_id,
            adopted_sha256=case.adoption.sha256,
            adopted_at=case.adoption.adopted_at,
        )

    def _approve(self, case: FakeCase, body: s.Approve) -> s.CaseStatus:
        if (
            case.adoption is None
            or not hashes_match(body.adopted_sha256, case.adoption.sha256)
            or not hashes_match(body.adopted_sha256, text_sha256(case.text))
        ):
            raise FakeConflict("adopt_current_text_first")
        if case.state in {cs.CaseState.FROZEN, cs.CaseState.IN_TRANSIT}:
            return self._status(case)
        if case.state is not cs.CaseState.AWAITING_ADOPTION:
            raise FakeConflict("case_not_approvable")
        # Preview fixture only. Production must call packet.approval_gate on saved research/PDF.
        case.state = cs.transition(case.state, cs.CustomerApproved()).target
        return self._status(case)

    def _step(self, case: FakeCase, body: s.WorkerStep) -> s.CaseStatus:
        self._expire(case)
        if body.step_id in case.steps:
            return self._status(case)
        if case.state is cs.CaseState.INTAKE:
            if case.authorization is None:
                raise FakeConflict("start_authorization_first")
            case.started_at = self.clock()
            case.payment = ps.PaymentState.AUTHORIZED
            case.authorization = case.authorization.model_copy(
                update={
                    "status": "authorized",
                    "window_started_at": case.started_at,
                    "window_expires_at": case.started_at + DEFAULT_WINDOW,
                }
            )
            case.state = cs.transition(case.state, cs.PaymentAuthorized()).target
        elif case.state is cs.CaseState.RESEARCHING:
            if case.answer is None:
                case.question = s.ClosedQuestion(
                    prompt="What is your relationship to the vehicle?",
                    choices=list(VehicleRelationship),
                )
                case.state = cs.transition(
                    case.state, cs.CustomerNeeded(case.question.prompt)
                ).target
            else:
                case.state = cs.transition(case.state, cs.ResearchComplete()).target
                case.text = (
                    f"To Demo Parking Administrative Review:\n\n"
                    f"I request review of citation {case.intake.ticket.citation_number}.\n\n"
                    f"{case.intake.customer_position.statement}\n\n"
                    f"Sincerely,\n{case.intake.contact.name}"
                )
                case.revision = 1
                case.state = cs.transition(case.state, cs.DraftReady()).target
        elif case.state is cs.CaseState.FROZEN:
            # Simulated acceptance, never a real submit gate, Lob call, or capture.
            case.state = cs.transition(case.state, cs.SubmitRequested()).target
            case.state = cs.transition(case.state, cs.SubmitConfirmed("lob_fake")).target
            assert case.payment is not None
            case.payment = ps.payment_transition(case.payment, ps.CaptureRequested()).target
            case.payment = ps.payment_transition(
                case.payment, ps.CaptureSucceeded("ch_fake")
            ).target
            case.events.append(
                s.TrackingEvent(
                    event="accepted",
                    occurred_at=self.clock(),
                    description="Fake mailing accepted; this is not delivery confirmation.",
                )
            )
        else:
            raise FakeConflict("no_worker_step_ready")
        case.steps.add(body.step_id)
        return self._status(case)

    def _tracking(self, case: FakeCase) -> s.Tracking:
        tier = self._offer(case).selected_tier if isinstance(case.pre_hold, s.Offer) else None
        return s.Tracking(
            case_id=case.pre_hold.case_id,
            state=case.state,
            tier=tier,
            tracking_number="FAKE-NOT-A-USPS-NUMBER"
            if case.events and tier is s.MailTier.CERTIFIED
            else None,
            events=case.events,
            delivery_confirmation_title="ParkingBreaker Delivery Confirmation",
        )

    # CaseServices protocol: the API addresses workspaces and cases by id only.
    def workspace_owner(self, workspace_id: str) -> str | None:
        workspace = self.workspaces.get(workspace_id)
        return workspace.owner if workspace else None

    def case_owner(self, case_id: str) -> str | None:
        case = self.cases.get(case_id)
        return case.owner if case else None

    def refresh(self, case_id: str) -> None:
        self._expire(self.cases[case_id])

    def workspace_view(self, workspace_id: str) -> s.WorkspaceView:
        return self.workspaces[workspace_id].view

    def submit(self, workspace_id: str, body: s.SubmitIntake) -> s.IntakeReceipt:
        return self._submit(self.workspaces[workspace_id], body)

    def pre_hold(self, case_id: str) -> s.PreHoldResult:
        return self.cases[case_id].pre_hold

    def choose(self, case_id: str, body: s.ChooseTier) -> s.Offer:
        return self._choose(self.cases[case_id], body)

    def authorize(self, case_id: str, body: s.StartAuthorization) -> s.Authorization:
        return self._authorize(self.cases[case_id])

    def recover_authorization(self, case_id: str) -> s.CaseStatus:
        return self.status(case_id)

    def beta_offer(self, case_id: str) -> s.BetaOffer:
        raise ServiceConflict("beta_requires_durable_service")

    def accept_beta(self, case_id: str, body: s.AcceptBeta) -> s.BetaAcceptanceReceipt:
        raise ServiceConflict("beta_requires_durable_service")

    def record_beta_feedback_notice(
        self, case_id: str, body: s.BetaFeedbackNoticeRequest
    ) -> s.BetaFeedbackNoticeReceipt:
        raise ServiceConflict("beta_feedback_requires_durable_service")

    def beta_feedback_notices(
        self, case_id: str, *, limit: int = 100, cursor: str | None = None
    ) -> s.BetaFeedbackNoticeList:
        raise ServiceConflict("beta_feedback_requires_durable_service")

    def beta_feedback_notice_page(self, case_id: str, notice_id: str, page_number: int) -> bytes:
        raise ServiceConflict("beta_feedback_requires_durable_service")

    def status(self, case_id: str) -> s.CaseStatus:
        return self._status(self.cases[case_id])

    def answer_question(self, case_id: str, body: s.CaseAnswer) -> s.CaseStatus:
        return self._answer_question(self.cases[case_id], body)

    def draft(self, case_id: str) -> s.Draft:
        return self._draft(self.cases[case_id])

    def edit(self, case_id: str, body: s.EditDraft) -> s.Draft:
        return self._edit(self.cases[case_id], body)

    def adopt_text(self, case_id: str, body: s.AdoptText) -> s.AdoptionReceipt:
        return self._adopt_text(self.cases[case_id], body)

    def approve(self, case_id: str, body: s.Approve) -> s.CaseStatus:
        return self._approve(self.cases[case_id], body)

    def packet_preview(self, case_id: str, body: s.PacketPreviewRequest) -> s.PacketPreview:
        raise ServiceConflict("packet_preview_requires_durable_service")

    def destination_disclosure(self, case_id: str) -> s.DestinationDisclosure:
        return s.DestinationDisclosure(
            version=DISCLOSURE_VERSION, sha256=DISCLOSURE_SHA256, text=DISCLOSURE_TEXT
        )

    def save_destination_address(
        self, case_id: str, body: s.DestinationAddress
    ) -> s.DestinationAddressReceipt:
        raise ServiceConflict("destination_choice_requires_durable_service")

    def consent_destination(
        self, case_id: str, body: s.DestinationConsent
    ) -> s.DestinationConsentReceipt:
        raise ServiceConflict("destination_choice_requires_durable_service")

    def decline_destination(self, case_id: str) -> s.CaseStatus:
        raise ServiceConflict("destination_choice_requires_durable_service")

    def tracking(self, case_id: str) -> s.Tracking:
        return self._tracking(self.cases[case_id])

    def mailing_record(self, case_id: str) -> s.ReturnProofPacket:
        raise ServiceConflict("mailing_record_requires_durable_service")

    def step(self, case_id: str, body: s.WorkerStep) -> s.CaseStatus:
        return self._step(self.cases[case_id], body)

    def save_intake_form(self, workspace_id: str, body: s.SaveIntakeForm) -> s.WorkspaceView:
        workspace = self.workspaces[workspace_id]
        if workspace.view.intake_form == body.form:
            return workspace.view
        if workspace.view.case_id is not None:
            raise FakeConflict("intake_already_submitted")
        if body.expected_revision != workspace.view.draft_revision:
            raise FakeConflict("workspace_changed_reload_required")
        for ref in (
            body.form.ticket_photo_ref,
            body.form.ticket_back_photo_ref,
            *body.form.selected_evidence_photo_refs,
        ):
            if ref is not None and self.photos.get(workspace.owner, ref) is None:
                raise FakeConflict("unknown_photo_ref")
        workspace.view = workspace.view.model_copy(
            update={"intake_form": body.form, "draft_revision": body.expected_revision + 1}
        )
        return workspace.view

    def upload_photo(self, workspace_id: str, body: s.PhotoUpload) -> s.PhotoRef:
        return s.PhotoRef(
            photo_ref=store_upload(self.photos, self.workspaces[workspace_id].owner, body)
        )

    @property
    def ticket_reading_required(self) -> bool:
        return self.ticket_intake is not None and self.ticket_intake.required

    def _ticket(self) -> TicketIntakeCoordinator:
        if self.ticket_intake is None:
            raise FakeConflict("ticket_reading_disabled")
        return self.ticket_intake

    def _ticket_path[T](self, work: Callable[[], T]) -> T:
        try:
            return work()
        except TicketIntakeServiceError as exc:
            raise FakeConflict(str(exc)) from None

    def ticket_reading_status(self, workspace_id: str) -> s.TicketReadingStatus:
        if self.ticket_intake is None:
            return s.TicketReadingStatus(enabled=False, required=False)
        return self._ticket_path(lambda: self._ticket().status(workspace_id))

    def request_ticket_reading(
        self, workspace_id: str, body: s.TicketOperationRequest
    ) -> s.TicketReadingSnapshot:
        return self._ticket_path(
            lambda: self._ticket().request_reading(
                workspace_id, body.request_id, body.expected_revision
            )
        )

    def request_ticket_review(
        self, workspace_id: str, body: s.TicketOperationRequest
    ) -> s.TicketReviewSnapshot:
        return self._ticket_path(
            lambda: self._ticket().request_review(
                workspace_id, body.request_id, body.expected_revision
            )
        )

    def confirm_ticket_reading(
        self, workspace_id: str, body: s.TicketConfirmationRequest
    ) -> s.TicketConfirmationReceipt:
        return self._ticket_path(lambda: self._ticket().confirm(workspace_id, body))

    def ticket_observability(self, workspace_id: str) -> s.TicketObservabilitySnapshot:
        return self._ticket_path(lambda: self._ticket().ticket_observability(workspace_id))

    def start_ticket_review_session(
        self, workspace_id: str, body: s.TicketReviewSessionRequest
    ) -> s.TicketReviewSession:
        return self._ticket_path(lambda: self._ticket().start_review_session(workspace_id, body))

    def record_ticket_review_observation(
        self,
        workspace_id: str,
        session_id: str,
        body: s.TicketReviewObservationRequest,
    ) -> s.TicketReviewObservationReceipt:
        return self._ticket_path(
            lambda: self._ticket().record_review_observation(workspace_id, session_id, body)
        )

    def save_required_copies(self, case_id: str, body: s.SaveRequiredCopies) -> s.CaseStatus:
        raise ServiceConflict("required_copies_not_available")

    def required_copy_page(self, case_id: str, requirement_id: str, page_number: int) -> bytes:
        raise ServiceConflict("required_copy_page_not_found")

    def dispatch(self, case_id: str | None = None) -> int:
        processed = 0
        for token in self.cases if case_id is None else (case_id,):
            case = self.cases[token]
            self._expire(case)
            if case.state is cs.CaseState.FROZEN:
                self._step(case, s.WorkerStep(step_id="dispatch"))
                processed += 1
        return processed

    def webhook(self, provider: str, payload: bytes, headers: dict[str, str]) -> bool:
        raise ServiceConflict("invalid_signature")

    def advance_due(self) -> SweepResult:
        return SweepResult(self.dispatch())
