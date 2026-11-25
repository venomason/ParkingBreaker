"""Timed, provider-free rehearsal through the customer intake and normal research worker."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from time import perf_counter
from typing import cast

import pytest
from httpx2 import ASGITransport, AsyncClient

from parkingbreaker import config
from parkingbreaker.agencies import MailingAddress
from parkingbreaker.api import schemas as s
from parkingbreaker.api.app import create_app
from parkingbreaker.case_service import CaseService
from parkingbreaker.case_state import CaseState, HardStop
from parkingbreaker.journey_timeline import TimelineSource
from parkingbreaker.lob_fakes import FakeLobClient
from parkingbreaker.lob_submission import LetterEvidence, LobResponse
from parkingbreaker.mail_config import MailClass
from parkingbreaker.payment_records import PaymentClient
from parkingbreaker.payment_state import PaymentState
from parkingbreaker.research.findings import FindingStatus
from parkingbreaker.research.questions import FactKey
from parkingbreaker.research.resolution import VehicleRelationship
from parkingbreaker.research_records import decode_research
from parkingbreaker.route_registry import RouteScope
from parkingbreaker.service_phase import ServicePhase
from test_beta_dispatch_timing import ForbiddenPayments
from test_case_service import INTAKE, approve_with_preview
from test_route_revocation_dispatch import change_route
from test_ticket_intake_service import BACK, FRONT, adopted, binding_for, literal_values
from test_ticket_vision_composition import ALICE_HEADERS, fake_settings, form_json, upload


@contextmanager
def timed(durations: dict[str, float], stage: str) -> Iterator[None]:
    start = perf_counter()
    yield
    durations[stage] = round((perf_counter() - start) * 1000, 2)


async def confirmed_intake(
    api: AsyncClient, durations: dict[str, float], stage: str, issue_date: str
) -> str:
    ticket = adopted()
    ticket.update(stage=stage, issue_date=issue_date)
    binding = binding_for(dict.fromkeys(literal_values()), ticket)
    response = await api.post("/workspaces", json={"notes": ""})
    response.raise_for_status()
    workspace = response.json()["workspace_id"]
    path = f"/workspaces/{workspace}"
    front = await upload(api, workspace, FRONT)
    back = await upload(api, workspace, BACK)
    saved = await api.put(
        f"{path}/intake-form",
        json={"expected_revision": 1, "form": form_json(binding, front, back)},
    )
    saved.raise_for_status()
    with timed(durations, "ocr"):
        reading = await api.post(
            f"{path}/ticket-reading", json={"expected_revision": 2, "request_id": "reading"}
        )
        reading.raise_for_status()
        assert reading.json()["status"] == "success"
        assert reading.json()["synthetic"]

    with timed(durations, "review_and_confirmation"):
        review = await api.post(
            f"{path}/ticket-review", json={"expected_revision": 2, "request_id": "review"}
        )
        review.raise_for_status()
        assert review.json()["fields"] == []
        confirmation = await api.post(
            f"{path}/ticket-confirmation",
            json={
                "expected_revision": 2,
                "command_id": "confirm",
                "review_request_id": "review",
                "expected_confirmation_generation": 0,
                "accuracy_acknowledged": True,
                "resolved_fields": {},
            },
        )
        confirmation.raise_for_status()
    intake = copy.deepcopy(INTAKE)
    intake.update(
        ticket=ticket,
        citation_photo_ref=front,
        citation_back_photo_ref=back,
        photo_refs=[],
        ticket_confirmation_command_id="confirm",
    )
    with timed(durations, "intake_and_issuer_resolution"):
        receipt = await api.post(f"{path}/intake", json=intake)
        receipt.raise_for_status()
        assert receipt.json()["pre_hold_status"] == "ready", receipt.json()
    return str(receipt.json()["case_id"])


@pytest.mark.anyio
@pytest.mark.parametrize("scenario", ["supported", "payment_only"])
async def test_timed_first_class_rehearsal(
    scenario: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_testsuite_property: Callable[[str, str], None],
) -> None:
    settings = replace(
        fake_settings(tmp_path, preview=True), service_phase=ServicePhase.CONTROLLED_FREE_BETA
    )
    lob = FakeLobClient([LobResponse(200, "ltr_rehearsal", evidence=LetterEvidence.PRODUCTION)])

    def open_service() -> CaseService:
        service = config.build_services(settings)
        assert isinstance(service, CaseService)
        return CaseService(
            replace(service._deps, payments=cast(PaymentClient, ForbiddenPayments()), lob=lob)
        )

    service = open_service()
    durations: dict[str, float] = {}
    reason = None
    started = perf_counter()
    scope = RouteScope("CA:sfmta.gov", "first-level review", MailClass.FIRST_CLASS)
    now = service._deps.clock()
    try:
        # Precondition: an enrolled synthetic route in an isolated SQLite database.
        address = MailingAddress(
            "SFMTA Customer Service Center",
            "ATTN: Citation Review",
            "11 South Van Ness Avenue",
            None,
            "San Francisco",
            "CA",
            "94103",
        )
        change_route(service._store, scope, 0, now, address=address)
        service._store.open_first_class_pilot("trial", scope, "research", "approval", "a" * 64, now)
        app = create_app(service, provider_mode="fake", worker_secret=settings.worker_secret)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://rehearsal.test",
            headers=ALICE_HEADERS,
        ) as api:
            case = await confirmed_intake(
                api, durations, "first-level review", now.date().isoformat()
            )
        offer = service.beta_offer(case)
        assert offer.tier == s.MailTier.STANDARD
        assert offer.amount_cents == 0
        acceptance = service.accept_beta(
            case, s.AcceptBeta(terms_accepted=True, disclosure_sha256=offer.disclosure_sha256)
        )
        assert acceptance.amount_cents == 0
        if scenario == "payment_only":
            # A new fetch now describes the same office as a payment address.
            html = config.FIXTURE.read_text()
            assert html.count("<h2>Contest by mail</h2>") == 1
            path = tmp_path / "payment-only.html"
            path.write_text(
                html.replace("<h2>Contest by mail</h2>", "<h2>Mail payments only to</h2>")
            )
            monkeypatch.setattr(config, "FIXTURE", path)
            service._store.close()
            service = open_service()

        with timed(durations, "research"):
            status = service.step(case, s.WorkerStep(step_id="research"))
        saved = service._store.get_research(case)
        assert saved is not None
        research = decode_research(saved.research_json)
        assert research.sources
        finding = next(item for item in research.findings if item.key == FactKey.MAILING_ADDRESS)
        assert finding.status == (
            FindingStatus.CONFIRMED if scenario == "supported" else FindingStatus.UNKNOWN
        )
        if scenario == "payment_only":
            assert finding.notes == "quote guard: address quote names another purpose (payments)"
            assert any("Mail payments only to" in source.text for source in research.sources)
        if status.state == CaseState.NEEDS_CUSTOMER:
            with timed(durations, "scripted_customer_answer"):
                status = service.answer_question(
                    case,
                    s.AnswerQuestion(
                        question_id="VEHICLE_RELATIONSHIP",
                        answer=VehicleRelationship.REGISTERED_OWNER,
                    ),
                )
        if scenario == "supported":
            assert status.state == CaseState.DRAFTING, status
            with timed(durations, "draft"):
                service.step(case, s.WorkerStep(step_id="draft"))
                draft = service.draft(case)
            with timed(durations, "adoption_and_preview"):
                service.adopt_text(
                    case, s.AdoptText(text=draft.text, text_sha256=draft.text_sha256)
                )
                approval = approve_with_preview(service, case, draft.text_sha256)
            with timed(durations, "approval"):
                service.approve(case, approval)
        else:
            assert status.state == CaseState.STOPPED_NO_CHARGE
            assert status.stop is not None
            assert status.stop.hard_stop == HardStop.S2
            reason = status.stop.reason_key
        assert not lob.calls
        service._store.close()
        with timed(durations, "restart_and_dispatch_replays"):
            service = open_service()
            service.dispatch(case)
            service._store.close()
            service = open_service()
            service.dispatch(case)
        status = service.status(case)
        assert status.payment_state == PaymentState.NO_CHARGE
        if scenario == "supported":
            assert len(lob.calls) == 1
            request = lob.calls[0][1]
            assert request.options.mail_class == MailClass.FIRST_CLASS
            assert hashlib.sha256(request.pdf).hexdigest() == approval.packet_sha256
        else:
            assert not lob.calls
            assert status.state == CaseState.STOPPED_NO_CHARGE
        total_ms = round((perf_counter() - started) * 1000, 2)
        timeline = service._store.journey_timeline(workspace_token=status.workspace_id)
        events = [
            json.loads(entry.details_json)["kind"]
            for entry in timeline.entries
            if entry.source == TimelineSource.MILESTONE
        ]
        assert "facts_confirmed" in events
        assert "intake_submitted" in events
        assert ("packet_approved" in events) == (scenario == "supported")
        ocr = [
            json.loads(entry.details_json)["coordinator_duration_ms"]
            for entry in timeline.entries
            if entry.source == TimelineSource.OCR
        ]
        assert len(ocr) == 1
        record_testsuite_property(
            scenario,
            json.dumps(
                {
                    "total_ms": total_ms,
                    "ocr_coordinator_ms": ocr[0],
                    "milestones": events,
                    "address_finding": finding.status.value,
                    "guard_detail": finding.notes if scenario == "payment_only" else None,
                    "address_reason": finding.unknown_reason.value
                    if finding.unknown_reason
                    else None,
                    "research_source_hashes": sorted(
                        {source.sha256 for source in research.sources}
                    ),
                    "stage_ms": durations,
                    "final_state": status.state.value,
                    "stop_reason": reason,
                    "fake_mailings": len(lob.calls),
                    "payment_state": status.payment_state.value,
                }
            ),
        )
    finally:
        service._store.close()
