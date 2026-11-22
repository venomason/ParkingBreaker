"""FastAPI preview entry point. All state and provider outcomes are fake and volatile."""

import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from parkingbreaker.access_email import AccessEmailService
from parkingbreaker.api import schemas as s
from parkingbreaker.api.auth import BearerVerifier, Customer, FakeBearerVerifier, bearer_token
from parkingbreaker.api.customer_auth import AuthHttp, install_auth_routes
from parkingbreaker.api.fakes import FakeServices
from parkingbreaker.api.service import CaseServices, ServiceConflict
from parkingbreaker.appeal.drafting import CUSTOMER_AUTHORED_MODE_VERSION
from parkingbreaker.outcome_follow_up import OutcomeFollowUpService, OutcomeLinkService
from parkingbreaker.overseer_feed import OPS_ID_PATTERN
from parkingbreaker.provider_credentials import valid_header_credential
from parkingbreaker.support_comms import SupportCommsService, SupportIntakeRequest
from parkingbreaker.support_store import SupportChannel
from parkingbreaker.sweep_run_store import SweepOutcome, SweepRunRecorder

ACCESS_LOG = logging.getLogger("parkingbreaker.api.access")
LOG = logging.getLogger(__name__)


MAX_JSON_REQUEST_BYTES = 1_048_576
MAX_PHOTO_REQUEST_BYTES = 13_500_000


class RequestBodyLimitMiddleware:
    """Bound the ASGI stream before FastAPI/JSON/provider code buffers it."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        photo = (
            path.startswith("/workspaces/") and path.endswith("/photos") and path.count("/") == 3
        )
        limit = (
            4096
            if path.startswith("/auth/")
            else (MAX_PHOTO_REQUEST_BYTES if photo else MAX_JSON_REQUEST_BYTES)
        )
        lengths = [
            value for key, value in scope.get("headers", []) if key.lower() == b"content-length"
        ]
        if len(lengths) > 1 or (lengths and (not lengths[0].isdigit() or len(lengths[0]) > 20)):
            await JSONResponse({"detail": "invalid_request"}, status_code=400)(scope, receive, send)
            return
        if lengths and int(lengths[0]) > limit:
            await JSONResponse({"detail": "request_too_large"}, status_code=413)(
                scope, receive, send
            )
            return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > limit:
                await JSONResponse({"detail": "request_too_large"}, status_code=413)(
                    scope, receive, send
                )
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        delivered = False

        async def bounded_receive() -> Message:
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, bounded_receive, send)


class AccessLogMiddleware:
    """Log an explicit four-field allowlist, including failures and unknown routes."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = perf_counter()
        status = 500

        async def capture_status(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, capture_status)
        finally:
            route = scope.get("route")
            method = scope["method"]
            ACCESS_LOG.info(
                json.dumps(
                    {
                        "method": method
                        if method in {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"}
                        else "OTHER",
                        "route": route.path if route else "<unmatched>",
                        "status": status,
                        "duration_ms": round((perf_counter() - started) * 1000, 3),
                    }
                )
            )


def create_app(
    services: CaseServices | None = None,
    verifier: BearerVerifier | None = None,
    *,
    worker_secret: str = "fake-worker",
    provider_mode: Literal["preview", "fake", "test", "live"] = "fake",
    customer_auth: AuthHttp | None = None,
    notifications: AccessEmailService | None = None,
    role: Literal["all", "customer", "worker"] = "all",
    sweep_runs: SweepRunRecorder | None = None,
    overseer_feed: Callable[[str | None, int], s.OverseerFeed] | None = None,
    overseer_feed_secret: str | None = None,
    support_comms: SupportCommsService | None = None,
    support_intake_secret: str | None = None,
    follow_ups: OutcomeFollowUpService | None = None,
    outcome_links: OutcomeLinkService | None = None,
) -> FastAPI:
    """``role="customer"`` leaves out the /internal worker routes (T31): research, mailing
    and capture sweeps then run only in the separate worker process and cannot hold up
    customer requests on this process's event loop."""
    if role not in {"all", "customer", "worker"}:
        raise RuntimeError("unknown_api_role")
    if provider_mode in {"test", "live"} and customer_auth is None:
        raise RuntimeError("customer_auth_not_configured")
    if provider_mode in {"test", "live"}:
        from parkingbreaker.config import ProviderMode, validate_worker_secret

        validate_worker_secret(worker_secret, ProviderMode(provider_mode))
    if customer_auth is not None and verifier is not None:
        raise RuntimeError("conflicting_customer_authentication")
    if overseer_feed is not None and not _valid_feed_secret(overseer_feed_secret, worker_secret):
        raise RuntimeError("overseer_feed_secret_invalid")
    if overseer_feed is None and overseer_feed_secret is not None:
        raise RuntimeError("overseer_feed_not_configured")
    if support_comms is not None:
        if not _valid_feed_secret(support_intake_secret, worker_secret) or (
            overseer_feed_secret is not None
            and hmac.compare_digest(support_intake_secret or "", overseer_feed_secret)
        ):
            raise RuntimeError("support_intake_secret_invalid")
    elif support_intake_secret is not None:
        raise RuntimeError("support_intake_not_configured")
    services = services if services is not None else FakeServices()
    verifier = verifier if verifier is not None else FakeBearerVerifier()
    api = FastAPI(
        title="ParkingBreaker fake API",
        version="0.1.0",
        description="Volatile local preview. No real authentication, research, payments or mail.",
        responses={code: {"model": s.Error} for code in (401, 404, 409, 422)},
    )
    api.add_middleware(RequestBodyLimitMiddleware)
    api.add_middleware(AccessLogMiddleware)
    if customer_auth is not None:
        install_auth_routes(
            api,
            customer_auth,
            service_phase_provider=lambda: services.service_phase,
        )

    @api.exception_handler(ServiceConflict)
    async def conflict(_request: Request, exc: ServiceConflict) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @api.exception_handler(RequestValidationError)
    async def invalid(_request: Request, _exc: RequestValidationError) -> JSONResponse:
        # Pydantic's default response contains rejected customer input.
        return JSONResponse(status_code=422, content={"detail": "invalid_request"})

    async def current_customer(request: Request) -> Customer:
        if customer_auth is not None:
            return await customer_auth.customer(request)
        customer = verifier.verify(bearer_token(request))
        if customer is None:
            raise HTTPException(401, "invalid_token", headers={"WWW-Authenticate": "Bearer"})
        return customer

    async def owned_workspace(
        workspace_id: str, customer: Annotated[Customer, Depends(current_customer)]
    ) -> str:
        if services.workspace_owner(workspace_id) != customer.customer_id:
            raise HTTPException(404, "not_found")
        return workspace_id

    async def owned_case(
        case_id: str, customer: Annotated[Customer, Depends(current_customer)]
    ) -> str:
        if services.case_owner(case_id) != customer.customer_id:
            raise HTTPException(404, "not_found")
        services.refresh(case_id)
        return case_id

    async def worker_auth(request: Request) -> None:
        if not worker_secret or not hmac.compare_digest(bearer_token(request), worker_secret):
            raise HTTPException(401, "worker_authentication_required")

    async def worker_case(case_id: str, request: Request) -> str:
        await worker_auth(request)
        if services.case_owner(case_id) is None:
            raise HTTPException(404, "not_found")
        return case_id

    _REPO_ROOT = Path(__file__).resolve().parents[4]
    _COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")

    def _safe_build_commit() -> str | None:
        env_commit = os.environ.get("PB_BUILD_COMMIT", "").strip()
        if _COMMIT_RE.fullmatch(env_commit):
            return env_commit.lower()
        return None

    def _safe_policy_hashes() -> dict[str, str]:
        hashes: dict[str, str] = {}
        policy_path = _REPO_ROOT / "build-kit" / "state" / "free-beta-policy.json"
        with contextlib.suppress(OSError):
            hashes["free_beta_policy"] = hashlib.sha256(policy_path.read_bytes()).hexdigest()
        return hashes

    _BUILD_COMMIT = _safe_build_commit()
    _POLICY_HASHES = _safe_policy_hashes()

    def _customer_authored_mode() -> s.Health:
        deps = getattr(services, "_deps", None)
        drafter = getattr(deps, "drafter", None)
        active = bool(getattr(drafter, "deterministic", False))
        return s.Health(
            mode=provider_mode,
            customer_authored_mode=active,
            customer_authored_mode_version=CUSTOMER_AUTHORED_MODE_VERSION if active else None,
            build_commit=_BUILD_COMMIT,
            policy_hashes=dict(_POLICY_HASHES) if active else {},
        )

    @api.get("/health", response_model=s.Health)
    async def health() -> s.Health:
        return _customer_authored_mode()

    @api.post("/workspaces", response_model=s.WorkspaceView, status_code=201)
    async def create_workspace(
        body: s.CreateWorkspace, customer: Annotated[Customer, Depends(current_customer)]
    ) -> s.WorkspaceView:
        if customer_auth is not None:
            existing = services.appeal_workspace(customer.customer_id)
            if existing is not None and not services.workspace_finished(existing.workspace_id):
                if existing.notes != body.notes:
                    raise HTTPException(409, "appeal_already_started")
                return existing
        return services.create_workspace(customer.customer_id, body)

    @api.get("/auth/appeal", response_model=s.WorkspaceView | None)
    async def current_appeal(
        customer: Annotated[Customer, Depends(current_customer)],
    ) -> s.WorkspaceView | None:
        return services.appeal_workspace(customer.customer_id)

    @api.get("/workspaces/{workspace_id}", response_model=s.WorkspaceView)
    async def workspace(
        workspace: Annotated[str, Depends(owned_workspace)],
    ) -> s.WorkspaceView:
        return services.workspace_view(workspace)

    @api.put("/workspaces/{workspace_id}/intake-form", response_model=s.WorkspaceView)
    async def save_intake_form(
        body: s.SaveIntakeForm,
        workspace: Annotated[str, Depends(owned_workspace)],
    ) -> s.WorkspaceView:
        return services.save_intake_form(workspace, body)

    @api.post("/workspaces/{workspace_id}/photos", response_model=s.PhotoRef, status_code=201)
    async def photo(
        body: s.PhotoUpload, workspace: Annotated[str, Depends(owned_workspace)]
    ) -> s.PhotoRef:
        return services.upload_photo(workspace, body)

    @api.get(
        "/workspaces/{workspace_id}/ticket-reading",
        response_model=s.TicketReadingStatus,
    )
    async def ticket_reading(
        workspace: Annotated[str, Depends(owned_workspace)],
    ) -> s.TicketReadingStatus:
        return services.ticket_reading_status(workspace)

    @api.post(
        "/workspaces/{workspace_id}/ticket-reading",
        response_model=s.TicketReadingSnapshot,
    )
    async def request_ticket_reading(
        body: s.TicketOperationRequest,
        workspace: Annotated[str, Depends(owned_workspace)],
    ) -> s.TicketReadingSnapshot:
        return services.request_ticket_reading(workspace, body)

    @api.post(
        "/workspaces/{workspace_id}/ticket-review",
        response_model=s.TicketReviewSnapshot,
    )
    async def request_ticket_review(
        body: s.TicketReviewRequest,
        workspace: Annotated[str, Depends(owned_workspace)],
    ) -> s.TicketReviewSnapshot:
        return services.request_ticket_review(workspace, body)

    @api.post(
        "/workspaces/{workspace_id}/ticket-confirmation",
        response_model=s.TicketConfirmationReceipt,
    )
    async def confirm_ticket_reading(
        body: s.TicketConfirmationRequest,
        workspace: Annotated[str, Depends(owned_workspace)],
    ) -> s.TicketConfirmationReceipt:
        return services.confirm_ticket_reading(workspace, body)

    @api.get(
        "/workspaces/{workspace_id}/ticket-observability",
        response_model=s.TicketObservabilitySnapshot,
    )
    async def ticket_observability(
        workspace: Annotated[str, Depends(owned_workspace)],
    ) -> s.TicketObservabilitySnapshot:
        return services.ticket_observability(workspace)

    @api.post(
        "/workspaces/{workspace_id}/ticket-review-sessions",
        response_model=s.TicketReviewSession,
    )
    async def start_ticket_review_session(
        body: s.TicketReviewSessionRequest,
        workspace: Annotated[str, Depends(owned_workspace)],
    ) -> s.TicketReviewSession:
        return services.start_ticket_review_session(workspace, body)

    @api.post(
        "/workspaces/{workspace_id}/ticket-review-sessions/{session_id}/observations",
        response_model=s.TicketReviewObservationReceipt,
    )
    async def record_ticket_review_observation(
        session_id: str,
        body: s.TicketReviewObservationRequest,
        workspace: Annotated[str, Depends(owned_workspace)],
    ) -> s.TicketReviewObservationReceipt:
        try:
            return services.record_ticket_review_observation(workspace, session_id, body)
        except ServiceConflict as exc:
            if str(exc) == "not_found":
                raise HTTPException(404, "not_found") from None
            raise

    @api.post("/workspaces/{workspace_id}/intake", response_model=s.IntakeReceipt)
    async def intake(
        body: s.SubmitIntake, workspace: Annotated[str, Depends(owned_workspace)]
    ) -> s.IntakeReceipt:
        return services.submit(workspace, body)

    @api.get("/cases/{case_id}/pre-hold", response_model=s.PreHoldResult)
    async def pre_hold(case: Annotated[str, Depends(owned_case)]) -> s.PreHoldResult:
        return services.pre_hold(case)

    @api.put("/cases/{case_id}/tier", response_model=s.Offer)
    async def tier(body: s.ChooseTier, case: Annotated[str, Depends(owned_case)]) -> s.Offer:
        return services.choose(case, body)

    @api.post("/cases/{case_id}/authorization", response_model=s.Authorization)
    async def authorization(
        body: s.StartAuthorization, case: Annotated[str, Depends(owned_case)]
    ) -> s.Authorization:
        try:
            return services.authorize(case, body)
        except ServiceConflict as exc:
            if str(exc) == "card_not_authorized":
                saved = services.status(case)
                if saved.authorization_pending or saved.payment_state is not None:
                    raise ServiceConflict("authorization_outcome_unresolved") from None
            raise

    @api.post("/cases/{case_id}/authorization/recovery", response_model=s.CaseStatus)
    async def authorization_recovery(case: Annotated[str, Depends(owned_case)]) -> s.CaseStatus:
        return services.recover_authorization(case)

    @api.get("/cases/{case_id}/beta-offer", response_model=s.BetaOffer)
    async def beta_offer(case: Annotated[str, Depends(owned_case)]) -> s.BetaOffer:
        return services.beta_offer(case)

    @api.post("/cases/{case_id}/beta-acceptance", response_model=s.BetaAcceptanceReceipt)
    async def beta_acceptance(
        body: s.AcceptBeta, case: Annotated[str, Depends(owned_case)]
    ) -> s.BetaAcceptanceReceipt:
        return services.accept_beta(case, body)

    @api.post(
        "/cases/{case_id}/notices",
        response_model=s.BetaFeedbackNoticeReceipt,
        status_code=201,
    )
    async def record_notice(
        body: s.BetaFeedbackNoticeRequest, case: Annotated[str, Depends(owned_case)]
    ) -> Response:
        receipt = services.record_beta_feedback_notice(case, body)
        return JSONResponse(
            status_code=201,
            content=receipt.model_dump(mode="json"),
            headers={"Cache-Control": "no-store"},
        )

    @api.get("/cases/{case_id}/notices", response_model=s.BetaFeedbackNoticeList)
    async def notices(
        case: Annotated[str, Depends(owned_case)],
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
        cursor: Annotated[str | None, Query(max_length=2000)] = None,
    ) -> Response:
        listing = services.beta_feedback_notices(case, limit=limit, cursor=cursor)
        return JSONResponse(
            content=listing.model_dump(mode="json"),
            headers={"Cache-Control": "no-store"},
        )

    @api.get("/cases/{case_id}/notices/{notice_id}/pages/{page_number}")
    async def notice_page(
        notice_id: str,
        page_number: int,
        case: Annotated[str, Depends(owned_case)],
    ) -> Response:
        data = services.beta_feedback_notice_page(case, notice_id, page_number)
        return Response(
            content=data,
            media_type="image/png" if data.startswith(b"\x89PNG") else "image/jpeg",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    @api.get("/cases/{case_id}/status", response_model=s.CaseStatus)
    async def status(case: Annotated[str, Depends(owned_case)]) -> s.CaseStatus:
        return services.status(case)

    @api.post("/cases/{case_id}/answers", response_model=s.CaseStatus)
    async def answer(body: s.CaseAnswer, case: Annotated[str, Depends(owned_case)]) -> s.CaseStatus:
        return services.answer_question(case, body)

    @api.put("/cases/{case_id}/required-copies", response_model=s.CaseStatus)
    async def required_copies(
        body: s.SaveRequiredCopies, case: Annotated[str, Depends(owned_case)]
    ) -> s.CaseStatus:
        return services.save_required_copies(case, body)

    @api.get("/cases/{case_id}/required-copies/{requirement_id}/pages/{page_number}")
    async def required_copy_page(
        requirement_id: str,
        page_number: int,
        case: Annotated[str, Depends(owned_case)],
    ) -> Response:
        data = services.required_copy_page(case, requirement_id, page_number)
        return Response(
            content=data,
            media_type="image/png" if data.startswith(b"\x89PNG") else "image/jpeg",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    @api.get("/cases/{case_id}/draft", response_model=s.Draft)
    async def draft(case: Annotated[str, Depends(owned_case)]) -> s.Draft:
        return services.draft(case)

    @api.put("/cases/{case_id}/draft", response_model=s.Draft)
    async def edit(body: s.EditDraft, case: Annotated[str, Depends(owned_case)]) -> s.Draft:
        return services.edit(case, body)

    @api.post("/cases/{case_id}/adoption", response_model=s.AdoptionReceipt)
    async def adoption(
        body: s.AdoptText, case: Annotated[str, Depends(owned_case)]
    ) -> s.AdoptionReceipt:
        return services.adopt_text(case, body)

    @api.post("/cases/{case_id}/packet-preview", response_model=s.PacketPreview)
    async def packet_preview(
        body: s.PacketPreviewRequest, case: Annotated[str, Depends(owned_case)]
    ) -> s.PacketPreview:
        return services.packet_preview(case, body)

    @api.post("/cases/{case_id}/approval", response_model=s.CaseStatus)
    async def approval(body: s.Approve, case: Annotated[str, Depends(owned_case)]) -> s.CaseStatus:
        return services.approve(case, body)

    @api.get("/cases/{case_id}/destination/disclosure", response_model=s.DestinationDisclosure)
    async def destination_disclosure(
        case: Annotated[str, Depends(owned_case)],
    ) -> s.DestinationDisclosure:
        return services.destination_disclosure(case)

    @api.post("/cases/{case_id}/destination/address", response_model=s.DestinationAddressReceipt)
    async def destination_address(
        body: s.DestinationAddress, case: Annotated[str, Depends(owned_case)]
    ) -> s.DestinationAddressReceipt:
        return services.save_destination_address(case, body)

    @api.post("/cases/{case_id}/destination/consent", response_model=s.DestinationConsentReceipt)
    async def destination_consent(
        body: s.DestinationConsent, case: Annotated[str, Depends(owned_case)]
    ) -> s.DestinationConsentReceipt:
        return services.consent_destination(case, body)

    @api.post("/cases/{case_id}/destination/decline", response_model=s.CaseStatus)
    async def destination_decline(case: Annotated[str, Depends(owned_case)]) -> s.CaseStatus:
        return services.decline_destination(case)

    @api.get("/cases/{case_id}/tracking", response_model=s.Tracking)
    async def tracking(case: Annotated[str, Depends(owned_case)]) -> s.Tracking:
        return services.tracking(case)

    @api.get("/cases/{case_id}/mailing-record", response_model=s.ReturnProofPacket)
    async def mailing_record(case: Annotated[str, Depends(owned_case)]) -> s.ReturnProofPacket:
        return services.mailing_record(case)

    if role != "customer":
        if overseer_feed is not None:
            feed = overseer_feed
            feed_secret = overseer_feed_secret or ""

            @api.get("/internal/overseer/feed", response_model=s.OverseerFeed)
            def overseer(
                request: Request,
                response: Response,
                after: Annotated[str | None, Query(pattern=OPS_ID_PATTERN)] = None,
                limit: Annotated[int, Query(ge=1, le=100)] = 50,
            ) -> s.OverseerFeed:
                presented = bearer_token(request)
                if not valid_header_credential(presented) or not hmac.compare_digest(
                    presented, feed_secret
                ):
                    raise HTTPException(401, "overseer_authentication_required")
                if set(request.query_params) - {"after", "limit"}:
                    raise HTTPException(422, "invalid_request")
                response.headers["Cache-Control"] = "no-store"
                try:
                    return feed(after, limit)
                except Exception:
                    # Validation/storage errors may contain planted private values. Neither
                    # their text nor their original exception reaches this response/log.
                    LOG.warning("overseer feed unavailable")
                    raise HTTPException(503, "overseer_feed_unavailable") from None

        @api.post("/internal/cases/{case_id}/step", response_model=s.CaseStatus)
        async def step(
            body: s.WorkerStep, case: Annotated[str, Depends(worker_case)]
        ) -> s.CaseStatus:
            return services.step(case, body)

        if support_comms is not None:
            comms = support_comms
            intake_secret = support_intake_secret or ""

            async def support_auth(request: Request) -> None:
                presented = bearer_token(request)
                if not presented.isascii() or not hmac.compare_digest(presented, intake_secret):
                    raise HTTPException(
                        401, "invalid_token", headers={"WWW-Authenticate": "Bearer"}
                    )

            @api.post("/internal/support/intake", response_model=s.SupportIntakeResponse)
            async def support_intake(
                body: s.SupportIntakeBody,
                _auth: Annotated[None, Depends(support_auth)],
            ) -> s.SupportIntakeResponse:
                res = comms.handle_intake(
                    SupportIntakeRequest(
                        inbound_message_id=body.message_id,
                        channel=SupportChannel(body.channel),
                        text=body.text,
                        support_code=body.support_code,
                        received_at=body.received_at,
                    )
                )
                return s.SupportIntakeResponse(
                    outcome=res.outcome.value,
                    support_code=res.support_code,
                    intent=res.intent.value,
                    hold_reason=None if res.hold_reason is None else res.hold_reason.value,
                    urgent=res.urgent,
                    email_sent=res.email_sent,
                    owner_alerted=res.owner_alerted,
                    duplicate=res.duplicate,
                )

        @api.post(
            "/internal/follow-ups",
            response_model=s.FollowUpRunReceipt,
            include_in_schema=False,
        )
        async def run_follow_ups(
            _auth: Annotated[None, Depends(worker_auth)],
        ) -> s.FollowUpRunReceipt:
            if follow_ups is None:
                raise HTTPException(503, "follow_ups_not_configured")
            report = follow_ups.run()
            return s.FollowUpRunReceipt(
                sent=report.sent,
                skipped_no_consent=report.skipped_no_consent,
                unknown_recorded=report.unknown_recorded,
                deferred=report.deferred,
            )

        @api.post("/internal/dispatch")
        async def dispatch(_auth: Annotated[None, Depends(worker_auth)]) -> dict[str, int]:
            # T24: record the whole sweep, started before any work and finished in cleanup.
            # Recording never blocks the sweep; a missing record shows up as stale health.
            run: int | None = None
            if sweep_runs is not None:
                try:
                    run = sweep_runs.start_sweep_run(datetime.now(UTC))
                except Exception:
                    LOG.warning("sweep run record failed to start")
            outcome, processed, failed = SweepOutcome.FAILED, 0, 0
            try:
                # Access emails share this existing authenticated worker sweep and database.
                # Dispatch them first so a case failure cannot prevent sign-in delivery.
                emailed = 0 if notifications is None else notifications.dispatch(limit=5)
                result = services.advance_due()
                # The response is counts only; opaque ids and fixed reasons go to the log.
                for failure in result.failures:
                    LOG.warning(
                        "sweep case failed case=%s reason=%s", failure.case_id, failure.reason
                    )
                processed, failed = emailed + result.processed, len(result.failures)
                outcome = SweepOutcome.PARTIAL if failed else SweepOutcome.OK
                return {"processed": processed, "failed": failed}
            finally:
                if sweep_runs is not None and run is not None:
                    try:
                        sweep_runs.finish_sweep_run(
                            run, datetime.now(UTC), outcome, processed, failed
                        )
                    except Exception:
                        LOG.warning("sweep run record failed to finish")

    if role != "worker":

        @api.post(
            "/outcomes/opt-out",
            response_model=s.OutcomeLinkReceipt,
            include_in_schema=False,
        )
        async def outcome_opt_out(body: s.OutcomeLinkRequest) -> s.OutcomeLinkReceipt:
            if outcome_links is None:
                raise HTTPException(503, "outcome_links_not_configured")
            if not outcome_links.opt_out(body.case_id, body.token):
                raise HTTPException(400, "invalid_link")
            return s.OutcomeLinkReceipt(status="opted_out")

        @api.post(
            "/outcomes/report",
            response_model=s.OutcomeLinkReceipt,
            include_in_schema=False,
        )
        async def outcome_report(body: s.OutcomeReportRequest) -> s.OutcomeLinkReceipt:
            if outcome_links is None:
                raise HTTPException(503, "outcome_links_not_configured")
            if not outcome_links.report(body.case_id, body.token, body.decision):
                raise HTTPException(400, "invalid_link")
            status: Literal["recorded", "pending"] = (
                "pending" if body.decision == "pending" else "recorded"
            )
            return s.OutcomeLinkReceipt(status=status)

    async def provider_webhook(provider: str, request: Request) -> dict[str, bool]:
        try:
            recorded = services.webhook(provider, await request.body(), dict(request.headers))
        except ServiceConflict as exc:
            if str(exc) == "invalid_signature":
                raise HTTPException(400, "invalid_signature") from None
            raise
        return {"received": True, "duplicate": not recorded}

    @api.post("/webhooks/stripe")
    async def stripe_webhook(request: Request) -> dict[str, bool]:
        return await provider_webhook("stripe", request)

    @api.post("/webhooks/lob")
    async def lob_webhook(request: Request) -> dict[str, bool]:
        return await provider_webhook("lob", request)

    return api


# The documented uvicorn command must not enable its raw URL/query access log.
# Configure only our allowlisted logger; leave all unrelated application loggers alone.
logging.getLogger("uvicorn.access").disabled = True
ACCESS_LOG.setLevel(logging.INFO)
if not ACCESS_LOG.handlers:
    ACCESS_LOG.addHandler(logging.StreamHandler())


def app_from_environment() -> FastAPI:
    """The served app: PB_PROVIDER_MODE picks the preview fake or the real service."""
    from parkingbreaker.auth_config import (
        build_customer_access,
        build_outcome_follow_up,
        load_auth_settings,
    )
    from parkingbreaker.case_service import CaseService
    from parkingbreaker.config import (
        build_services,
        journey_identity_from_settings,
        load_settings,
        require_recovery_gates,
        resolve_service_phase,
    )
    from parkingbreaker.transaction_store import SqliteTransactionStore

    settings = load_settings()
    require_recovery_gates(settings)  # before any store open or migration
    roles: dict[str, Literal["all", "customer", "worker"]] = {
        "all": "all",
        "customer": "customer",
        "worker": "worker",
    }
    configured = settings.values.get("PB_API_ROLE", "all")
    if configured not in roles:
        raise RuntimeError("PB_API_ROLE must be all, customer or worker")
    role = roles[configured]
    if (
        role != "all"
        and settings.mode.value == "fake"
        and settings.values.get("PB_FAKE_SPLIT_FOR_MEASUREMENT") != "1"
    ):
        # Fake providers keep their state in each process's memory, so a hold made by the
        # customer process could not be captured or released by the worker process.
        raise RuntimeError("the customer/worker split needs real providers; use PB_API_ROLE=all")
    auth_settings = load_auth_settings(settings)
    if auth_settings is not None:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        store = SqliteTransactionStore(
            settings.data_dir / "parkingbreaker.sqlite",
            journey_identity=journey_identity_from_settings(settings),
        )
        try:
            services = build_services(settings, store=store)
            if services is None:
                # PREVIEW builds no durable service, so keep the phase resolved by the
                # configuration boundary on the fake instead of falling back to PAID.
                services = FakeServices(service_phase=resolve_service_phase(settings))
            auth, notifications = build_customer_access(
                auth_settings, store, clock=lambda: datetime.now(UTC)
            )
            follow_ups, outcome_links = build_outcome_follow_up(
                auth_settings, store, clock=lambda: datetime.now(UTC)
            )
            return create_app(
                services,
                worker_secret=settings.worker_secret,
                provider_mode=settings.mode.value,
                customer_auth=auth,
                notifications=notifications,
                role=role,
                sweep_runs=store,
                follow_ups=follow_ups,
                outcome_links=outcome_links,
                **overseer_options(settings.values, services, settings.worker_secret, role),
            )
        except Exception:
            store.close()
            raise
    services = build_services(settings)
    if services is None:
        # PREVIEW builds no durable service, so keep the phase resolved by the
        # configuration boundary on the fake instead of falling back to PAID.
        services = FakeServices(service_phase=resolve_service_phase(settings))
    return create_app(
        services,
        worker_secret=settings.worker_secret,
        provider_mode=settings.mode.value,
        role=role,
        sweep_runs=services.sweep_runs if isinstance(services, CaseService) else None,
        **overseer_options(settings.values, services, settings.worker_secret, role),
    )


def _valid_feed_secret(secret: str | None, worker_secret: str) -> bool:
    return (
        secret is not None
        and len(secret) >= 16
        and valid_header_credential(secret)
        and not hmac.compare_digest(secret, worker_secret)
    )


def overseer_options(
    values: Mapping[str, str],
    services: object,
    worker_secret: str = "",
    role: Literal["all", "customer", "worker"] = "all",
) -> dict[str, Any]:
    """Configure identifiers on both roles; expose the feed only with its own secret."""
    from parkingbreaker.case_service import CaseService

    secret = values.get("PB_OVERSEER_FEED_SECRET", "")
    key_hex = values.get("PB_OVERSEER_OPS_ID_KEY", "")
    version_text = values.get("PB_OVERSEER_OPS_ID_KEY_VERSION", "")
    if not secret and not key_hex and not version_text:
        return {}
    if not key_hex or not isinstance(services, CaseService):
        raise RuntimeError("overseer_identifier_key_required")
    if secret and not _valid_feed_secret(secret, worker_secret):
        raise RuntimeError("overseer_feed_secret_invalid")
    try:
        if re.fullmatch(r"[0-9a-f]{64}", key_hex) is None:
            raise ValueError
        key = bytes.fromhex(key_hex)
        if re.fullmatch(r"[1-9][0-9]{0,4}", version_text or "1") is None:
            raise ValueError
        version = int(version_text or "1")
        if not 1 <= version <= 65535:
            raise ValueError
    except ValueError:
        raise RuntimeError("overseer_identifier_key_or_version_invalid") from None
    services.configure_overseer_ids(key, version)
    if role == "customer" or not secret:
        return {}
    return {"overseer_feed": services.overseer_feed, "overseer_feed_secret": secret}


app = app_from_environment()
