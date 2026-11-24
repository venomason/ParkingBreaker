"""A customer can accept and approve exactly the class that reaches the mail provider."""

from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from httpx2 import ASGITransport, AsyncClient

from parkingbreaker import case_service, prehold
from parkingbreaker.api import schemas as s
from parkingbreaker.api.app import create_app
from parkingbreaker.appeal.drafting import Draft, DraftSentence
from parkingbreaker.appeal.fakes import FakeDrafter
from parkingbreaker.beta_campaign_cli import _feedback_report
from parkingbreaker.beta_store import BETA_DISCLOSURE_SHA256
from parkingbreaker.case_service import CaseService, ResearchDeps
from parkingbreaker.case_state import CaseState
from parkingbreaker.lob_fakes import FakeLobClient
from parkingbreaker.lob_submission import LetterEvidence, LobResponse
from parkingbreaker.mail_config import MailClass
from parkingbreaker.payment_records import PaymentClient
from parkingbreaker.payment_state import PaymentState
from parkingbreaker.research.resolution import VehicleRelationship
from parkingbreaker.service_phase import ServicePhase
from parkingbreaker.transaction_store import GATE_REFUSED_REASON, SqliteTransactionStore
from prepared_packet_support import fetched_at
from samples import SAN_FRANCISCO
from test_beta_dispatch_timing import ForbiddenPayments
from test_case_service import ALICE, INTAKE, NOW, Resolved, approve_with_preview, service, submit
from test_route_revocation_dispatch import SCOPE, change_route
from test_tier_acceptance import extract_case, model_answers


@pytest.mark.anyio
@pytest.mark.parametrize("office_moved", [False, True])
async def test_free_pilot_first_class_packet_survives_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, office_moved: bool
) -> None:
    scope = replace(SCOPE, mail_class=MailClass.FIRST_CLASS)
    agency = replace(SAN_FRANCISCO, id=scope.issuer, official_domains=("sandiego.gov",))
    monkeypatch.setattr(prehold, "resolve_ticket_issuer", lambda *_a, **_k: Resolved(agency))
    monkeypatch.setitem(INTAKE["ticket"], "stage", scope.stage)
    result, _, _ = extract_case(model_answers())
    monkeypatch.setattr(case_service, "run_research", lambda *_a, **_kw: fetched_at(result, NOW))
    real = service(tmp_path / "cases.sqlite", cast(PaymentClient, ForbiddenPayments()))
    unused: Any = object()
    lob = FakeLobClient([LobResponse(200, "ltr_pilot", evidence=LetterEvidence.PRODUCTION)])
    statement = INTAKE["customer_position"]["statement"]
    deps = replace(
        real._deps,
        service_phase=ServicePhase.CONTROLLED_FREE_BETA,
        lob=lob,
        research=ResearchDeps(unused, unused, None, lambda _: None),
        drafter=FakeDrafter(
            lambda *_: Draft((DraftSentence(statement, (f"note:0-{len(statement)}",)),))
        ),
    )
    real = CaseService(deps)
    store = real._store
    try:
        change_route(store, scope, 0, NOW, address=real._mailing_address(result))
        store.open_first_class_pilot("trial", scope, "issuer-research", "approval", "a" * 64, NOW)
        async with AsyncClient(
            transport=ASGITransport(app=create_app(real)), base_url="http://test"
        ) as client:
            _, receipt = await submit(client, INTAKE)
            case_id = receipt["case_id"]
            offer = (await client.get(f"/cases/{case_id}/beta-offer", headers=ALICE)).json()
            assert offer["tier"] == "STANDARD"
            assert offer["amount_cents"] == 0
            assert _feedback_report(store) == {
                "pilot": "trial",
                "selected": 1,
                "accepted_free_offer": 0,
                "dated_postal_scan": 0,
                "dated_printer_handoff": 0,
                "customers_reporting": 0,
                "customer_reported_routing": {"unreported": 1},
                "customer_reported_acknowledgment": {"unreported": 1},
                "customer_reported_disposition": {"unreported": 1},
            }
            accept = f"/cases/{case_id}/beta-acceptance"
            body = {"terms_accepted": True, "disclosure_sha256": BETA_DISCLOSURE_SHA256}
            assert (await client.post(accept, headers=ALICE, json=body)).status_code == 409
            body["disclosure_sha256"] = offer["disclosure_sha256"]
            accepted = (await client.post(accept, headers=ALICE, json=body)).json()
            assert accepted["tier"] == "STANDARD"
            assert accepted["funding"] == "NO_CHARGE"
            assert (await client.post(accept, headers=ALICE, json=body)).json() == accepted

        status = real.step(case_id, s.WorkerStep(step_id="research"))
        if status.state == CaseState.NEEDS_CUSTOMER:
            real.answer_question(
                case_id,
                s.AnswerQuestion(
                    question_id="VEHICLE_RELATIONSHIP", answer=VehicleRelationship.REGISTERED_OWNER
                ),
            )
        real.step(case_id, s.WorkerStep(step_id="draft"))
        draft = real.draft(case_id)
        real.adopt_text(case_id, s.AdoptText(text=draft.text, text_sha256=draft.text_sha256))
        real.approve(case_id, approve_with_preview(real, case_id, draft.text_sha256))
        token = real._intake(case_id).case_token
        assert token is not None
        attempt = store.get(token, 1)
        assert attempt is not None
        assert attempt.request.options.mail_class == MailClass.FIRST_CLASS
        assert store.bound_route(token, 1) == (scope, 1)
        if office_moved:
            change_route(store, scope, 1, NOW, status="revoked")
        store.close()
        store = SqliteTransactionStore(tmp_path / "cases.sqlite")
        real = CaseService(replace(deps, store=store))
        for _ in range(3):
            real.dispatch(case_id)
        assert real.status(case_id).payment_state == PaymentState.NO_CHARGE
        assert real.tracking(case_id).delivery_confirmation_title == "ParkingBreaker Mailing Record"
        if office_moved:
            assert not lob.calls
            cursor = store.submission_cursor(token, 1)
            assert cursor is not None
            assert getattr(cursor.outcome, "reason", None) == GATE_REFUSED_REASON
        else:
            assert len(lob.calls) == 1
            assert lob.calls[0][1] == attempt.request
            assert real.mailing_record(case_id).tracking_number is None
            async with AsyncClient(
                transport=ASGITransport(app=create_app(real)), base_url="http://test"
            ) as client:
                response = await client.post(
                    f"/cases/{case_id}/pilot-feedback",
                    headers=ALICE,
                    json={
                        "routing": "wrong_office",
                        "expected_revision": 0,
                        "request_id": "update",
                    },
                )
                assert response.status_code == 200, response.text
            report = _feedback_report(store)
            assert report["accepted_free_offer"] == report["customers_reporting"] == 1
            assert report["customer_reported_routing"] == {"wrong_office": 1}
            assert report["customer_reported_disposition"] == {"unreported": 1}
            assert report["dated_postal_scan"] == report["dated_printer_handoff"] == 0
    finally:
        store.close()
