"""Repeatable offline customer API tests for two unfamiliar issuers: Cambridge and Georgia Tech.

Uses the frozen synthetic ticket PNGs under docs/verification/unfamiliar-issuer-fixtures/
and the frozen official HTML/text snapshots under backend/tests/fixtures/unfamiliar_issuers/.
Offline fakes sit strictly at the provider boundary (vision LLM, search, HTTP fetcher,
issuer/link/extraction LLMs, payment client, Lob client); all customer API routes,
ticket-intake confirmations, issuer-resolution guards, quote guards, resolution ladder,
and payment/mailing gates execute real production code.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from httpx2 import ASGITransport, AsyncClient

from parkingbreaker.agencies import IssuerType, SourceScope
from parkingbreaker.api import schemas as s
from parkingbreaker.api.app import create_app
from parkingbreaker.beta_store import BETA_DISCLOSURE_SHA256
from parkingbreaker.blob_store import FakeBlobStore
from parkingbreaker.case_service import CaseService, CaseServiceDeps, ResearchDeps
from parkingbreaker.case_state import CaseState, HardStop
from parkingbreaker.json_types import JsonValue
from parkingbreaker.llm import Usage
from parkingbreaker.lob_fakes import FakeLobClient
from parkingbreaker.lob_submission import LetterEvidence, LobResponse
from parkingbreaker.payment_fakes import FakePaymentClient
from parkingbreaker.payment_state import PaymentState
from parkingbreaker.prehold import PreholdDeps
from parkingbreaker.research.delegation import LinkPurpose
from parkingbreaker.research.extractor import LlmExtractor
from parkingbreaker.research.fakes import (
    FakeFetcher,
    FakeLlmClient,
    FakeSearcher,
    html_response,
)
from parkingbreaker.research.issuer_assessment import (
    IssuerAssessment,
    IssuerQuote,
    LlmIssuerAssessor,
)
from parkingbreaker.research.jurisdiction import DotGovRecord, DotGovSnapshot
from parkingbreaker.research.link_assessment import LinkAssessment, LlmLinkAssessor
from parkingbreaker.research.questions import FactKey as K
from parkingbreaker.research.run import RecoveryConfig
from parkingbreaker.research.search import SearchHit
from parkingbreaker.research.sources import AuthorityClass, SourceRecord, fetch_source
from parkingbreaker.service_phase import ServicePhase
from parkingbreaker.service_policy import SERVICE_CONTROL, STATE_BASIS
from parkingbreaker.ticket_fact_binding import TicketFactBinding
from parkingbreaker.ticket_intake_service import TicketIntakeCoordinator, TicketIntakeDeps
from parkingbreaker.ticket_intake_store import TicketIntakeStore
from parkingbreaker.ticket_vision import FIELD_ORDER, TicketVisionReader
from parkingbreaker.transaction_store import SqliteTransactionStore
from test_research_issuer_assessment import reply as issuer_assessment_reply
from test_research_link_assessment import reply_item as link_reply_item
from test_ticket_intake_service import ScriptedVisionClient, payload, review_payload

pytestmark = pytest.mark.anyio

ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / "docs/verification/unfamiliar-issuer-rehearsal-2026-11-18.json"
IMAGE_DIR = ROOT / "docs/verification/unfamiliar-issuer-fixtures"
SOURCE_DIR = Path(__file__).resolve().parent / "fixtures/unfamiliar_issuers"

ORIGIN = "http://unfamiliar.test"
ALICE = {"Authorization": "Bearer fake-alice"}
WORKER = {"Authorization": "Bearer fake-worker"}
NOW = datetime(2026, 11, 18, 22, 0, tzinfo=UTC)

CAMBRIDGE_URL = "https://www.cambridgema.gov/iwantto/disputeaparkingticket"
GATECH_HOST = "www.pts.gatech.edu"
GATECH_URL = f"https://{GATECH_HOST}/parking/enforcement-citations/"

CAMBRIDGE_ADDRESS: dict[str, JsonValue] = {
    "recipient": "Traffic Parking & Transportation Department, City of Cambridge",
    "attention": "Attn: Hearing Officer",
    "line1": "344 Broadway",
    "line2": None,
    "city": "Cambridge",
    "state": "MA",
    "zip_code": "02139",
}
CAMBRIDGE_ADDRESS_QUOTE = (
    "Mail or deliver your dispute to:\n\n"
    "Attn: Hearing Officer\n\n"
    "Traffic Parking & Transportation Department\n\n"
    "City of Cambridge\n\n"
    "344 Broadway\n\n"
    "Cambridge, MA 02139"
)
CAMBRIDGE_MAIL_QUOTE = "To submit a dispute via mail, you must include:"
CAMBRIDGE_PENALTY_QUOTE = (
    "Please note: you must pay or dispute your parking ticket within 21 days "
    "to avoid late penalties."
)
CAMBRIDGE_ITEMS_QUOTE = (
    "To submit a dispute via mail, you must include:\n\n"
    "your name and address;\n\n"
    "contact information, an email address and/or a phone number;\n\n"
    "the ticket number;\n\n"
    "the license plate (vehicle registration) number;\n\n"
    "the reason for the dispute"
)

GATECH_ONLINE_ONLY_QUOTE = "All citation appeals must be submitted online at\n\nMy Parking Account."
GATECH_PAYMENT_MAIL_QUOTE = "By mail using the envelope included with the citation."
GATECH_DEADLINE_QUOTE = (
    "All appeals must be received no later than ten (10) calendar days after the date "
    "the citation was issued to qualify as a valid appeal."
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _reading_from_literal(literal: Mapping[str, object]) -> dict[str, JsonValue]:
    return {
        field.value: {
            "state": "shown" if literal[field.value] is not None else "not_shown",
            "value": cast(JsonValue, literal[field.value]),
        }
        for field in FIELD_ORDER
    }


def _dotgov_snapshot() -> DotGovSnapshot:
    records = (
        DotGovRecord(
            domain="cambridgema.gov",
            domain_type="City",
            organization="City of Cambridge",
            suborganization=None,
            city="Cambridge",
            state="MA",
        ),
        DotGovRecord(
            domain="georgia.gov",
            domain_type="State or territory",
            organization="State of Georgia",
            suborganization=None,
            city="Atlanta",
            state="GA",
        ),
    )
    return DotGovSnapshot(
        snapshot_date=date(2026, 11, 15),
        records=records,
        skipped_counts={},
        content_sha256="a" * 64,
    )


def _link_assessor_noop() -> LlmLinkAssessor:
    return LlmLinkAssessor(FakeLlmClient({"links": []}, Usage(20, 10, 1)))


class _ScriptedResearchLlm:
    """Offline LLM boundary returning canned JSON findings keyed to the fetched page hash."""

    def __init__(self, builder: Callable[[str], JsonValue]) -> None:
        self._builder = builder
        self.calls = 0

    def complete_json(
        self,
        system: str,
        user: str,
        schema: Mapping[str, JsonValue],
        *,
        max_cost_cents: int | None = None,
    ) -> tuple[JsonValue, Usage]:
        self.calls += 1
        return self._builder(user), Usage(1200, 300, 1)


def _build_api_stack(
    tmp_path: Path,
    *,
    vision_client: ScriptedVisionClient,
    fetcher: FakeFetcher,
    searcher: FakeSearcher,
    issuer_assessor: LlmIssuerAssessor,
    extractor: LlmExtractor,
    link_assessor: LlmLinkAssessor | None = None,
    service_phase: ServicePhase = ServicePhase.PAID,
    known_records: tuple[Mapping[str, object], ...] = (),
) -> tuple[CaseService, SqliteTransactionStore, FakePaymentClient, FakeLobClient]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    store = SqliteTransactionStore(tmp_path / "unfamiliar.sqlite")
    photos = FakeBlobStore()
    payments = FakePaymentClient()
    lob = FakeLobClient([LobResponse(200, "ltr_must_not_send", evidence=LetterEvidence.PRODUCTION)])
    counter = iter(range(1, 5000))

    coordinator = TicketIntakeCoordinator(
        TicketIntakeDeps(
            store=store,
            intake=TicketIntakeStore(store),
            photos=photos,
            clock=lambda: NOW,
            new_id=lambda: f"{next(counter):032x}",
            reader_for_subject=lambda _subject: TicketVisionReader(vision_client),
            required=True,
        )
    )
    prehold_deps = PreholdDeps(
        snapshot=_dotgov_snapshot(),
        searcher=searcher,
        fetcher=fetcher,
        issuer_assessor=issuer_assessor,
        link_assessor=link_assessor or _link_assessor_noop(),
        clock=lambda: NOW,
        budget_cents=25,
        state_basis=STATE_BASIS,
        controls=SERVICE_CONTROL,
    )
    research_deps = ResearchDeps(
        fetcher=fetcher,
        extractor=extractor,
        searcher=searcher,
        wait=lambda _seconds: None,
        known_records=known_records,
        recovery=RecoveryConfig(),
        budget_cents=50,
    )
    case_service = CaseService(
        CaseServiceDeps(
            store=store,
            prehold=prehold_deps,
            clock=lambda: NOW,
            payments=payments,
            photos=photos,
            research=research_deps,
            lob=lob,
            new_id=lambda: f"{next(counter):032x}",
            service_phase=service_phase,
            ticket_intake=coordinator,
        )
    )
    return case_service, store, payments, lob


async def _upload_photo(api: AsyncClient, workspace_id: str, png_bytes: bytes) -> str:
    resp = await api.post(
        f"/workspaces/{workspace_id}/photos",
        headers=ALICE,
        json={"content_type": "image/png", "data_base64": _b64(png_bytes)},
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["photo_ref"])


def test_unfamiliar_issuer_fixtures_match_rehearsal_manifest() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    by_id = {case["id"]: case for case in manifest["cases"]}
    assert set(by_id) == {"cambridge", "gatech"}

    for case_id in ("cambridge", "gatech"):
        entry = by_id[case_id]
        assert _sha256(ROOT / entry["fixture"]) == entry["image_sha256"]
        assert _sha256(SOURCE_DIR / f"{case_id}.html") == entry["source_html_sha256"]
        assert _sha256(SOURCE_DIR / f"{case_id}.txt") == entry["source_text_sha256"]


async def test_cambridge_driver_plate_correction_and_unresolved_deadline_pause_through_api(
    tmp_path: Path,
) -> None:
    cam_png = (IMAGE_DIR / "cambridge.png").read_bytes()
    cam_html = (SOURCE_DIR / "cambridge.html").read_text(encoding="utf-8")

    # Fetch once through the production extractor solely to compute the exact fetched-page sha256
    # that the offline issuer/research LLM fixtures cite verbatim.
    fetched_cam = fetch_source(
        CAMBRIDGE_URL,
        AuthorityClass.OFFICIAL_PROCEDURE,
        (SourceScope("cambridgema.gov"),),
        FakeFetcher({CAMBRIDGE_URL: html_response(CAMBRIDGE_URL, cam_html)}),
        lambda: NOW,
    )
    assert isinstance(fetched_cam, SourceRecord), fetched_cam
    cam_sha = fetched_cam.sha256

    # 1. Offline Vision LLM matches cambridge.png verbatim: license_plate is blank (not_shown).
    cam_literal: dict[str, object] = {field.value: None for field in FIELD_ORDER}
    cam_literal.update(
        {
            "citation_number": "SYN-CAM-1402",
            "license_plate": None,
            "license_plate_state": "MA",
            "issuer_name": "City of Cambridge",
            "issue_date": "2026-11-17",
            "violation_text": "Parking meter expired",
            "issued_city": "Cambridge",
            "issued_state": "MA",
            "stage_text": "Initial parking citation",
        }
    )
    # Driver supplies DEMO417 while preserving literal license_plate=None.
    cam_adopted: dict[str, object] = {
        "citation_number": "SYN-CAM-1402",
        "license_plate": "DEMO417",
        "license_plate_state": "MA",
        "issuer_hint": "City of Cambridge",
        "issuer_kind": "GOVERNMENT",
        "issued_city": "Cambridge",
        "issued_state": "MA",
        "violation_code": "SYN-METER",
        "violation_description": "Parking meter expired",
        "issue_date": "2026-11-17",
        "notice_date": None,
        "printed_deadline": None,
        "stage": "initial parking citation",
    }
    cam_binding = TicketFactBinding.bind(cam_literal, cam_adopted)
    # review_fields omits license_plate because its literal value is None (driver-supplied only).
    assert "license_plate" not in cam_binding.review_fields()

    vision_client = ScriptedVisionClient(
        (_reading_from_literal(cam_literal), Usage(cost_cents=1)),
        (review_payload(cam_binding.review_fields()), Usage(cost_cents=1)),
    )

    # 2. Offline search/fetch/issuer/extraction providers backed by frozen cambridge.html.
    cam_root = "https://cambridgema.gov/"
    fetcher = FakeFetcher(
        {
            cam_root: html_response(cam_root, cam_html),
            CAMBRIDGE_URL: html_response(CAMBRIDGE_URL, cam_html),
        }
    )
    searcher = FakeSearcher(
        lambda req: (
            (SearchHit(CAMBRIDGE_URL),)
            if any("cambridgema.gov" in d for d in req.official_domains) or not req.official_domains
            else ()
        )
    )
    issuer_assessor = LlmIssuerAssessor(
        FakeLlmClient(
            issuer_assessment_reply(
                IssuerAssessment(
                    candidate_index=1,
                    issuer_name="City of Cambridge",
                    issuer_type=IssuerType.MUNICIPAL,
                    deciding_body="Department of Transportation",
                    violation_state="MA",
                    is_private_operator=False,
                    issuer_evidence=IssuerQuote("City of Cambridge", cam_sha),
                    deciding_body_evidence=IssuerQuote(
                        "Dispute a Parking Ticket\n\nContact\n\nDepartment of Transportation",
                        cam_sha,
                    ),
                    issuer_type_evidence=IssuerQuote("City of Cambridge", cam_sha),
                )
            ),
            Usage(200, 80, 1),
        )
    )

    def cam_extraction(_user: str) -> JsonValue:
        # Extractor cites Cambridge's actual mail-in wording & address, and tries to extract
        # deadline rules from the 21-day late-penalty sentence; production quote guards must
        # keep MAIL_IN_ACCEPTED & MAILING_ADDRESS confirmed while rejecting unsupported
        # postmark/receipt/clock-start claims.
        return {
            "findings": [
                {
                    "key": K.MAIL_IN_ACCEPTED.value,
                    "status": "CONFIRMED",
                    "value": True,
                    "verbatim_quote": CAMBRIDGE_MAIL_QUOTE,
                    "source_sha256s": [cam_sha],
                },
                {
                    "key": K.MAILING_ADDRESS.value,
                    "status": "CONFIRMED",
                    "value": CAMBRIDGE_ADDRESS,
                    "verbatim_quote": CAMBRIDGE_ADDRESS_QUOTE,
                    "source_sha256s": [cam_sha],
                },
                {
                    "key": K.REQUIRED_ITEMS.value,
                    "status": "CONFIRMED",
                    "value": [
                        "your name and address",
                        "contact information",
                        "the ticket number",
                        "the license plate number",
                        "the reason for the dispute",
                    ],
                    "verbatim_quote": CAMBRIDGE_ITEMS_QUOTE,
                    "source_sha256s": [cam_sha],
                },
                {
                    "key": K.DEADLINE_DAYS.value,
                    "status": "CONFIRMED",
                    "value": 21,
                    "verbatim_quote": CAMBRIDGE_PENALTY_QUOTE,
                    "source_sha256s": [cam_sha],
                },
                {
                    "key": K.DEADLINE_RULE.value,
                    "status": "CONFIRMED",
                    "value": "RECEIPT",
                    "verbatim_quote": CAMBRIDGE_PENALTY_QUOTE,
                    "source_sha256s": [cam_sha],
                },
                {
                    "key": K.DEADLINE_CLOCK_START.value,
                    "status": "CONFIRMED",
                    "value": "ISSUE_DATE",
                    "verbatim_quote": CAMBRIDGE_PENALTY_QUOTE,
                    "source_sha256s": [cam_sha],
                },
            ]
        }

    extractor = LlmExtractor(_ScriptedResearchLlm(cam_extraction))
    case_service, store, payments, lob = _build_api_stack(
        tmp_path,
        vision_client=vision_client,
        fetcher=fetcher,
        searcher=searcher,
        issuer_assessor=issuer_assessor,
        extractor=extractor,
    )
    app = create_app(case_service)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as api:
            ws_id = (await api.post("/workspaces", headers=ALICE, json={"notes": ""})).json()[
                "workspace_id"
            ]
            photo_ref = await _upload_photo(api, ws_id, cam_png)

            # Save draft with photo only, then request OCR reading.
            saved_initial = await api.put(
                f"/workspaces/{ws_id}/intake-form",
                headers=ALICE,
                json={
                    "expected_revision": 1,
                    "form": s.IntakeForm(
                        values={"story": "Meter display went dark after coin insertion."},
                        ticket_photo_ref=photo_ref,
                    ).model_dump(mode="json"),
                },
            )
            assert saved_initial.status_code == 200, saved_initial.text

            reading = await api.post(
                f"/workspaces/{ws_id}/ticket-reading",
                headers=ALICE,
                json={"request_id": "req-cam-extract", "expected_revision": 2},
            )
            assert reading.status_code == 200, reading.text
            reading_fields = {item["field"]: item for item in reading.json()["fields"]}
            assert reading_fields["license_plate"]["state"] == "not_shown"
            assert reading_fields["license_plate"]["value"] is None
            assert reading_fields["citation_number"]["value"] == "SYN-CAM-1402"
            assert reading_fields["issuer_name"]["value"] == "City of Cambridge"

            # Driver supplies DEMO417 and saves the ticket fact binding; literal plate stays blank.
            saved_corrected = await api.put(
                f"/workspaces/{ws_id}/intake-form",
                headers=ALICE,
                json={
                    "expected_revision": 2,
                    "form": s.IntakeForm(
                        values={"story": "Meter display went dark after coin insertion."},
                        ticket_photo_ref=photo_ref,
                        ticket_fact_binding=payload(cam_binding),
                    ).model_dump(mode="json"),
                },
            )
            assert saved_corrected.status_code == 200, saved_corrected.text
            saved_binding = saved_corrected.json()["intake_form"]["ticket_fact_binding"]
            assert saved_binding["literal"]["license_plate"] is None
            assert saved_binding["adopted"]["license_plate"] == "DEMO417"

            review = await api.post(
                f"/workspaces/{ws_id}/ticket-review",
                headers=ALICE,
                json={"request_id": "req-cam-review", "expected_revision": 3},
            )
            assert review.status_code == 200, review.text
            assert review.json()["status"] == "success"

            confirm = await api.post(
                f"/workspaces/{ws_id}/ticket-confirmation",
                headers=ALICE,
                json={
                    "command_id": "cmd-cam-confirm",
                    "expected_revision": 3,
                    "review_request_id": "req-cam-review",
                    "expected_confirmation_generation": 0,
                    "accuracy_acknowledged": True,
                    "resolved_fields": {},
                },
            )
            assert confirm.status_code == 200, confirm.text
            assert confirm.json()["generation"] == 1

            # Readback verifies original blank plate and current driver confirmation coexist.
            readback = (await api.get(f"/workspaces/{ws_id}/ticket-reading", headers=ALICE)).json()
            rb_fields = {item["field"]: item for item in readback["extraction"]["fields"]}
            assert rb_fields["license_plate"]["state"] == "not_shown"
            assert rb_fields["license_plate"]["value"] is None
            assert readback["confirmation"]["state"] == "current"

            # Submit intake -> resolves City of Cambridge via DotGov + cambridge.html.
            intake_body = {
                "rental_company_paid": "NOT_RENTAL",
                "ticket": {
                    "citation_number": "SYN-CAM-1402",
                    "license_plate": "DEMO417",
                    "license_plate_state": "MA",
                    "issuer_hint": "City of Cambridge",
                    "issuer_kind": "GOVERNMENT",
                    "issued_city": "Cambridge",
                    "issued_state": "MA",
                    "violation_code": "SYN-METER",
                    "violation_description": "Parking meter expired",
                    "issue_date": "2026-11-17",
                    "stage": "initial parking citation",
                },
                "customer_position": {"statement": "Meter display went dark after coin insertion."},
                "contact": {
                    "name": "Jordan Synthetic",
                    "phone": "617-555-0142",
                    "email": "jordan@example.test",
                    "mailing_address": {
                        "line1": "100 Memorial Drive",
                        "city": "Cambridge",
                        "state": "MA",
                        "zip_code": "02139",
                    },
                },
                "photo_refs": [photo_ref],
                "citation_photo_ref": photo_ref,
                "ticket_confirmation_command_id": "cmd-cam-confirm",
            }
            submitted = await api.post(
                f"/workspaces/{ws_id}/intake", headers=ALICE, json=intake_body
            )
            assert submitted.status_code == 200, submitted.text
            case_id = submitted.json()["case_id"]
            assert submitted.json()["state"] == CaseState.INTAKE.value, submitted.text

            prehold_resp = await api.get(f"/cases/{case_id}/pre-hold", headers=ALICE)
            assert prehold_resp.status_code == 200, prehold_resp.text
            offer = prehold_resp.json()
            assert offer["issuer"]["issuer_id"] == "MA:cambridgema.gov"
            assert offer["issuer"]["issuer_name"] == "City of Cambridge"

            tier_resp = await api.put(
                f"/cases/{case_id}/tier", headers=ALICE, json={"tier": "CERTIFIED"}
            )
            assert tier_resp.status_code == 200, tier_resp.text
            auth_resp = await api.post(
                f"/cases/{case_id}/authorization",
                headers=ALICE,
                json={"terms_accepted": True, "payment_method_id": "pm_cam_synth"},
            )
            assert auth_resp.status_code == 200, auth_resp.text
            assert len(payments.authorizations) == 1

            # Run real research on frozen cambridge.html.
            step_resp = await api.post(
                f"/internal/cases/{case_id}/step",
                headers=WORKER,
                json={"step_id": "research"},
            )
            assert step_resp.status_code == 200, step_resp.text
            status = step_resp.json()

            # Trace official mailing rule & address; verify 21-day late-penalty statement alone
            # did NOT confirm DEADLINE_RULE, DEADLINE_CLOCK_START, or DEADLINE_DAYS.
            findings = {f["fact"]: f for f in status["research"]["findings"]}
            assert findings["MAIL_IN_ACCEPTED"]["status"] == "CONFIRMED"
            assert any(
                p["source_url"] == CAMBRIDGE_URL for p in findings["MAIL_IN_ACCEPTED"]["passages"]
            )
            assert findings["MAILING_ADDRESS"]["status"] == "CONFIRMED"
            assert any(
                p["source_url"] == CAMBRIDGE_URL and "344 Broadway" in p["quote"]
                for p in findings["MAILING_ADDRESS"]["passages"]
            )
            assert findings["DEADLINE_RULE"]["status"] == "UNKNOWN"
            assert findings["DEADLINE_CLOCK_START"]["status"] == "UNKNOWN"
            assert findings["DEADLINE_DAYS"]["status"] == "UNKNOWN"

            # Unresolved filing facts must pause/stop the case before drafting or mailing.
            assert status["state"] in {
                CaseState.NEEDS_CUSTOMER.value,
                CaseState.STOPPED_NO_CHARGE.value,
            }
            assert status["state"] != CaseState.DRAFTING.value

            # If paused for a customer question on the unresolved rule, declining it stops with
            # zero charge and releases the hold.
            while status["state"] == CaseState.NEEDS_CUSTOMER.value:
                q_id = status["question"]["question_id"]
                ans = await api.post(
                    f"/cases/{case_id}/answers",
                    headers=ALICE,
                    json={"question_id": q_id, "can_supply": False},
                )
                assert ans.status_code == 200, ans.text
                status = ans.json()

            assert status["state"] == CaseState.STOPPED_NO_CHARGE.value
            assert status["stop"]["no_charge"] is True

            for _ in range(3):
                case_service.advance_due()

            final_status = (await api.get(f"/cases/{case_id}/status", headers=ALICE)).json()
            assert final_status["state"] == CaseState.STOPPED_NO_CHARGE.value
            assert final_status["payment_state"] == PaymentState.RELEASED.value
            record = store.get_intake(case_id)
            assert record is not None
            assert store.get_draft(case_id) is None
            assert store.get(record.case_token or "", 1) is None
            assert store.get_approval_manifest(record.case_token or "", 1) is None
            assert lob.calls == []
            assert payments.captures == 0
    finally:
        store.close()


async def test_gatech_prehold_stop_and_online_only_guard_zero_authorizations_and_letters(
    tmp_path: Path,
) -> None:
    gt_png = (IMAGE_DIR / "gatech.png").read_bytes()
    gt_html = (SOURCE_DIR / "gatech.html").read_text(encoding="utf-8")

    fetched_gt = fetch_source(
        GATECH_URL,
        AuthorityClass.OFFICIAL_PROCEDURE,
        (SourceScope(GATECH_HOST),),
        FakeFetcher({GATECH_URL: html_response(GATECH_URL, gt_html)}),
        lambda: NOW,
    )
    assert isinstance(fetched_gt, SourceRecord), fetched_gt
    gt_sha = fetched_gt.sha256

    gt_literal: dict[str, object] = {field.value: None for field in FIELD_ORDER}
    gt_literal.update(
        {
            "citation_number": "SYN-GT-2407",
            "license_plate": "DEMO520",
            "license_plate_state": "GA",
            "issuer_name": "Georgia Tech Parking and Transportation Services",
            "issue_date": "2026-11-17",
            "violation_text": "Parking meter expired",
            "issued_city": "Atlanta",
            "issued_state": "GA",
            "stage_text": "Initial parking citation",
        }
    )
    gt_adopted: dict[str, object] = {
        "citation_number": "SYN-GT-2407",
        "license_plate": "DEMO520",
        "license_plate_state": "GA",
        "issuer_hint": "Georgia Tech Parking and Transportation Services",
        "issuer_kind": "GOVERNMENT",
        "issued_city": "Atlanta",
        "issued_state": "GA",
        "violation_code": "SYN-METER",
        "violation_description": "Parking meter expired",
        "issue_date": "2026-11-17",
        "notice_date": None,
        "printed_deadline": None,
        "stage": "initial parking citation",
    }
    gt_binding = TicketFactBinding.bind(gt_literal, gt_adopted)

    # Part A: Realistic DotGov snapshot (pts.gatech.edu is .edu, not .gov) stops at pre-hold
    # through the customer API with zero payment authorizations and zero letters.
    vision_a = ScriptedVisionClient(
        (_reading_from_literal(gt_literal), Usage(cost_cents=1)),
        (review_payload(gt_binding.review_fields()), Usage(cost_cents=1)),
    )
    fetcher_a = FakeFetcher({GATECH_URL: html_response(GATECH_URL, gt_html)})
    searcher_a = FakeSearcher(lambda _req: (SearchHit(GATECH_URL),))
    issuer_noop = LlmIssuerAssessor(
        FakeLlmClient(
            issuer_assessment_reply(
                IssuerAssessment(
                    candidate_index=None,
                    issuer_name=None,
                    issuer_type=None,
                    deciding_body=None,
                    violation_state=None,
                    is_private_operator=False,
                    issuer_evidence=None,
                    deciding_body_evidence=None,
                    issuer_type_evidence=None,
                )
            ),
            Usage(50, 20, 1),
        )
    )

    def gt_extraction(_user: str) -> JsonValue:
        # Adversarial + official mix:
        # - Online-only initial appeal quote ("All citation appeals must be submitted online...")
        # - Attempt to pass off the postal payment instruction & campus address as MAILING_ADDRESS
        #   and MAIL_IN_ACCEPTED=True. Production quote guards must reject postal fulfillment and
        #   never allow the payment address to become a confirmed appeal destination.
        return {
            "findings": [
                {
                    "key": K.MAIL_IN_ACCEPTED.value,
                    "status": "CONFIRMED",
                    "value": False,
                    "verbatim_quote": GATECH_ONLINE_ONLY_QUOTE,
                    "source_sha256s": [gt_sha],
                },
                {
                    "key": K.MAILING_ADDRESS.value,
                    "status": "CONFIRMED",
                    "value": {
                        "recipient": "Georgia Tech Parking and Transportation Services",
                        "attention": "Payment Processing",
                        "line1": "North Avenue",
                        "line2": None,
                        "city": "Atlanta",
                        "state": "GA",
                        "zip_code": "30332",
                    },
                    "verbatim_quote": GATECH_PAYMENT_MAIL_QUOTE,
                    "source_sha256s": [gt_sha],
                },
                {
                    "key": K.DEADLINE_DAYS.value,
                    "status": "CONFIRMED",
                    "value": 10,
                    "verbatim_quote": GATECH_DEADLINE_QUOTE,
                    "source_sha256s": [gt_sha],
                },
                {
                    "key": K.DEADLINE_RULE.value,
                    "status": "CONFIRMED",
                    "value": "RECEIPT",
                    "verbatim_quote": GATECH_DEADLINE_QUOTE,
                    "source_sha256s": [gt_sha],
                },
                {
                    "key": K.DEADLINE_CLOCK_START.value,
                    "status": "CONFIRMED",
                    "value": "ISSUE_DATE",
                    "verbatim_quote": GATECH_DEADLINE_QUOTE,
                    "source_sha256s": [gt_sha],
                },
            ]
        }

    extractor_a = LlmExtractor(_ScriptedResearchLlm(gt_extraction))
    service_a, store_a, payments_a, lob_a = _build_api_stack(
        tmp_path / "part_a",
        vision_client=vision_a,
        fetcher=fetcher_a,
        searcher=searcher_a,
        issuer_assessor=issuer_noop,
        extractor=extractor_a,
    )
    app_a = create_app(service_a)
    gt_intake_base = {
        "rental_company_paid": "NOT_RENTAL",
        "ticket": {
            "citation_number": "SYN-GT-2407",
            "license_plate": "DEMO520",
            "license_plate_state": "GA",
            "issuer_hint": "Georgia Tech Parking and Transportation Services",
            "issuer_kind": "GOVERNMENT",
            "issued_city": "Atlanta",
            "issued_state": "GA",
            "violation_code": "SYN-METER",
            "violation_description": "Parking meter expired",
            "issue_date": "2026-11-17",
            "stage": "initial parking citation",
        },
        "customer_position": {"statement": "Meter timer showed paid time remaining."},
        "contact": {
            "name": "Taylor Synthetic",
            "phone": "404-555-0199",
            "email": "taylor@example.test",
            "mailing_address": {
                "line1": "250 Peachtree St NW",
                "city": "Atlanta",
                "state": "GA",
                "zip_code": "30303",
            },
        },
    }

    try:
        async with AsyncClient(transport=ASGITransport(app=app_a), base_url=ORIGIN) as api:
            ws_id = (await api.post("/workspaces", headers=ALICE, json={"notes": ""})).json()[
                "workspace_id"
            ]
            photo_ref = await _upload_photo(api, ws_id, gt_png)
            assert (
                await api.put(
                    f"/workspaces/{ws_id}/intake-form",
                    headers=ALICE,
                    json={
                        "expected_revision": 1,
                        "form": s.IntakeForm(
                            values={"story": "Meter timer showed paid time remaining."},
                            ticket_photo_ref=photo_ref,
                            ticket_fact_binding=payload(gt_binding),
                        ).model_dump(mode="json"),
                    },
                )
            ).status_code == 200
            assert (
                await api.post(
                    f"/workspaces/{ws_id}/ticket-reading",
                    headers=ALICE,
                    json={"request_id": "req-gt-extract", "expected_revision": 2},
                )
            ).status_code == 200
            assert (
                await api.post(
                    f"/workspaces/{ws_id}/ticket-review",
                    headers=ALICE,
                    json={"request_id": "req-gt-review", "expected_revision": 2},
                )
            ).status_code == 200
            assert (
                await api.post(
                    f"/workspaces/{ws_id}/ticket-confirmation",
                    headers=ALICE,
                    json={
                        "command_id": "cmd-gt-confirm",
                        "expected_revision": 2,
                        "review_request_id": "req-gt-review",
                        "expected_confirmation_generation": 0,
                        "accuracy_acknowledged": True,
                        "resolved_fields": {},
                    },
                )
            ).status_code == 200

            submitted = await api.post(
                f"/workspaces/{ws_id}/intake",
                headers=ALICE,
                json={
                    **gt_intake_base,
                    "photo_refs": [photo_ref],
                    "citation_photo_ref": photo_ref,
                    "ticket_confirmation_command_id": "cmd-gt-confirm",
                },
            )
            assert submitted.status_code == 200, submitted.text
            case_id = submitted.json()["case_id"]
            assert submitted.json()["state"] == CaseState.STOPPED_NO_CHARGE.value

            stop = (await api.get(f"/cases/{case_id}/pre-hold", headers=ALICE)).json()
            assert stop["family"] == "NOT_YET_VERIFIED"
            assert stop["hard_stop"] == HardStop.S2.value
            assert stop["no_charge"] is True

            # Attempting tier selection or payment authorization must be refused with zero holds.
            assert (
                await api.put(f"/cases/{case_id}/tier", headers=ALICE, json={"tier": "CERTIFIED"})
            ).status_code == 409
            assert (
                await api.post(
                    f"/cases/{case_id}/authorization",
                    headers=ALICE,
                    json={"terms_accepted": True, "payment_method_id": "pm_gt_must_not_run"},
                )
            ).status_code == 409

            assert payments_a.authorizations == {}
            assert payments_a.captures == 0
            assert lob_a.calls == []
    finally:
        store_a.close()

    # Part B: When Georgia Tech's official page gatech.html IS resolved (e.g., via official
    # state delegation / zero-authorization research), prove:
    # 1. Official online-only appeal route is preserved in research evidence.
    # 2. Postal payment wording/address cannot become a confirmed appeal MAILING_ADDRESS.
    # 3. Case stops in STOPPED_NO_CHARGE (HardStop.S1) with ZERO payment authorizations and
    #    ZERO letters created or mailed.
    vision_b = ScriptedVisionClient(
        (_reading_from_literal(gt_literal), Usage(cost_cents=1)),
        (review_payload(gt_binding.review_fields()), Usage(cost_cents=1)),
    )
    # Synthetic .gov linking page for exercising the downstream delegation path.
    ga_portal_html = (
        "<main>"
        "<h1>Georgia Tech Parking and Transportation Services</h1>"
        "<p>Georgia Tech Parking and Transportation Services is a public state university agency "
        "in Atlanta, Georgia. Initial parking citation appeals are handled by the Georgia Tech "
        "PTS Appeals Board.</p>"
        f'<p><a href="{GATECH_URL}">Contest this parking citation online</a></p>'
        "</main>"
    )
    ga_root = "https://georgia.gov/"
    fetched_ga_root = fetch_source(
        ga_root,
        AuthorityClass.OFFICIAL_PROCEDURE,
        (SourceScope("georgia.gov"),),
        FakeFetcher({ga_root: html_response(ga_root, ga_portal_html)}),
        lambda: NOW,
    )
    assert isinstance(fetched_ga_root, SourceRecord), fetched_ga_root
    ga_root_sha = fetched_ga_root.sha256

    fetcher_b = FakeFetcher(
        {
            ga_root: html_response(ga_root, ga_portal_html),
            GATECH_URL: html_response(GATECH_URL, gt_html),
        }
    )
    searcher_b = FakeSearcher(
        lambda req: (
            (SearchHit(ga_root),)
            if not req.official_domains or "georgia.gov" in req.official_domains
            else (SearchHit(GATECH_URL),)
            if GATECH_HOST in req.official_domains
            else ()
        )
    )
    issuer_b = LlmIssuerAssessor(
        FakeLlmClient(
            issuer_assessment_reply(
                IssuerAssessment(
                    candidate_index=1,
                    issuer_name="Georgia Tech Parking and Transportation Services",
                    issuer_type=IssuerType.STATE_AGENCY,
                    deciding_body="Georgia Tech PTS Appeals Board",
                    violation_state="GA",
                    is_private_operator=False,
                    issuer_evidence=IssuerQuote(
                        "Georgia Tech Parking and Transportation Services", ga_root_sha
                    ),
                    deciding_body_evidence=IssuerQuote(
                        "Initial parking citation appeals are handled by the Georgia Tech "
                        "PTS Appeals Board.",
                        ga_root_sha,
                    ),
                    issuer_type_evidence=IssuerQuote(
                        "public state university agency in Atlanta, Georgia", ga_root_sha
                    ),
                )
            ),
            Usage(150, 60, 1),
        )
    )
    link_b = LlmLinkAssessor(
        FakeLlmClient(
            {
                "links": [
                    link_reply_item(
                        LinkAssessment(
                            index=1,
                            purpose=LinkPurpose.CONTEST,
                            belongs_to_issuer=True,
                            stage="initial parking citation",
                            verbatim_quote="Contest this parking citation online",
                        )
                    )
                ]
            },
            Usage(50, 20, 1),
        )
    )
    extractor_b = LlmExtractor(_ScriptedResearchLlm(gt_extraction))
    service_b, store_b, payments_b, lob_b = _build_api_stack(
        tmp_path / "part_b",
        vision_client=vision_b,
        fetcher=fetcher_b,
        searcher=searcher_b,
        issuer_assessor=issuer_b,
        extractor=extractor_b,
        link_assessor=link_b,
        service_phase=ServicePhase.CONTROLLED_FREE_BETA,
        known_records=(
            {
                "city_id": "us-ga-atlanta",
                "name": "Atlanta",
                "agency_id": "GA:georgia.gov",
                "official_domains": ["georgia.gov", GATECH_HOST],
                "contest_url": GATECH_URL,
            },
        ),
    )
    app_b = create_app(service_b)

    try:
        async with AsyncClient(transport=ASGITransport(app=app_b), base_url=ORIGIN) as api:
            ws_id = (await api.post("/workspaces", headers=ALICE, json={"notes": ""})).json()[
                "workspace_id"
            ]
            photo_ref = await _upload_photo(api, ws_id, gt_png)
            assert (
                await api.put(
                    f"/workspaces/{ws_id}/intake-form",
                    headers=ALICE,
                    json={
                        "expected_revision": 1,
                        "form": s.IntakeForm(
                            values={"story": "Meter timer showed paid time remaining."},
                            ticket_photo_ref=photo_ref,
                            ticket_fact_binding=payload(gt_binding),
                        ).model_dump(mode="json"),
                    },
                )
            ).status_code == 200
            assert (
                await api.post(
                    f"/workspaces/{ws_id}/ticket-reading",
                    headers=ALICE,
                    json={"request_id": "req-gt-b-extract", "expected_revision": 2},
                )
            ).status_code == 200
            assert (
                await api.post(
                    f"/workspaces/{ws_id}/ticket-review",
                    headers=ALICE,
                    json={"request_id": "req-gt-b-review", "expected_revision": 2},
                )
            ).status_code == 200
            assert (
                await api.post(
                    f"/workspaces/{ws_id}/ticket-confirmation",
                    headers=ALICE,
                    json={
                        "command_id": "cmd-gt-b-confirm",
                        "expected_revision": 2,
                        "review_request_id": "req-gt-b-review",
                        "expected_confirmation_generation": 0,
                        "accuracy_acknowledged": True,
                        "resolved_fields": {},
                    },
                )
            ).status_code == 200

            submitted = await api.post(
                f"/workspaces/{ws_id}/intake",
                headers=ALICE,
                json={
                    **gt_intake_base,
                    "photo_refs": [photo_ref],
                    "citation_photo_ref": photo_ref,
                    "ticket_confirmation_command_id": "cmd-gt-b-confirm",
                },
            )
            assert submitted.status_code == 200, submitted.text
            case_id = submitted.json()["case_id"]
            offer = (await api.get(f"/cases/{case_id}/pre-hold", headers=ALICE)).json()
            assert offer["issuer"]["issuer_id"] == "GA:georgia.gov"
            assert offer["issuer"]["delegated_tenant"] == GATECH_HOST

            # Enter RESEARCHING via zero-authorization beta acceptance so we prove zero card
            # authorizations while exercising the full official-page research pipeline on
            # gatech.html.
            assert (
                await api.put(f"/cases/{case_id}/tier", headers=ALICE, json={"tier": "CERTIFIED"})
            ).status_code == 200
            digest = "b" * 64
            store_b.create_beta_campaign(
                "beta-gt",
                NOW - timedelta(days=1),
                NOW + timedelta(days=10),
                "synthetic-id",
                "synthetic-approval",
                NOW,
            )
            store_b.bind_beta_jurisdiction(
                "beta-gt", offer["issuer"]["issuer_id"], "synthetic-jurisdiction", digest, NOW
            )
            store_b.qualify_beta_candidate("beta-gt", case_id, digest, NOW + timedelta(days=1), NOW)
            store_b.select_beta_batch("beta-gt", "batch-gt", "synthetic-release", NOW)
            accept = await api.post(
                f"/cases/{case_id}/beta-acceptance",
                headers=ALICE,
                json={
                    "terms_accepted": True,
                    "disclosure_sha256": BETA_DISCLOSURE_SHA256,
                },
            )
            assert accept.status_code == 200, accept.text

            step_resp = await api.post(
                f"/internal/cases/{case_id}/step",
                headers=WORKER,
                json={"step_id": "research"},
            )
            assert step_resp.status_code == 200, step_resp.text
            status = step_resp.json()

            # Official online-only page was fetched & saved; payment mail wording CANNOT become
            # a confirmed appeal address or confirmed mail-in permission.
            source_urls = {src["url"] for src in status["research"]["sources"]}
            assert GATECH_URL in source_urls
            findings = {f["fact"]: f for f in status["research"]["findings"]}
            assert findings["MAIL_IN_ACCEPTED"]["status"] != "CONFIRMED"
            assert any(
                p["source_url"] == GATECH_URL
                and "submitted online" in p["quote"]
                and "My Parking Account" in p["quote"]
                for p in findings["MAIL_IN_ACCEPTED"]["passages"]
            )
            assert findings["MAILING_ADDRESS"]["status"] == "UNKNOWN"

            # Case stops with no charge, zero authorizations, zero captures, zero letters.
            assert status["state"] == CaseState.STOPPED_NO_CHARGE.value
            assert status["stop"]["hard_stop"] == HardStop.S1.value
            assert status["stop"]["no_charge"] is True

            for _ in range(3):
                service_b.advance_due()

            record_b = store_b.get_intake(case_id)
            assert record_b is not None
            assert store_b.get_draft(case_id) is None
            assert store_b.get(record_b.case_token or "", 1) is None
            assert store_b.get_approval_manifest(record_b.case_token or "", 1) is None
            assert payments_b.authorizations == {}
            assert payments_b.captures == 0
            assert lob_b.calls == []
    finally:
        store_b.close()
