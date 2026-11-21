"""Real CaseServices, intake half (Lane C C3): through the API, on a SQLite store."""

from __future__ import annotations

import base64
import copy
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx2 import ASGITransport, AsyncClient

from parkingbreaker import prehold
from parkingbreaker.api import schemas as s
from parkingbreaker.api.app import create_app
from parkingbreaker.api.service import ServiceConflict
from parkingbreaker.appeal.adoption import text_sha256
from parkingbreaker.blob_store import FakeBlobStore
from parkingbreaker.case_service import CaseService, CaseServiceDeps
from parkingbreaker.case_state import CaseState
from parkingbreaker.payment_records import PaymentClient, mutation_key
from parkingbreaker.prehold import PreholdDeps
from parkingbreaker.research.resolution import VehicleRelationship
from parkingbreaker.research.search import SearchError
from parkingbreaker.service_policy import LAUNCH_ALLOWLIST, SERVICE_CONTROL, STATE_BASIS
from parkingbreaker.session_window import DEFAULT_WINDOW
from parkingbreaker.stripe_payment import STRIPE_API_VERSION, StripePaymentClient
from parkingbreaker.transaction_store import SqliteTransactionStore
from samples import SAN_FRANCISCO
from test_blob_store import png
from test_stripe_payment import REF, Transport, fixture

pytestmark = pytest.mark.anyio
PNG_B64 = base64.b64encode(png()).decode()
NOW = datetime(2026, 10, 15, 21, tzinfo=UTC)
ALICE = {"Authorization": "Bearer fake-alice"}
BOB = {"Authorization": "Bearer fake-bob"}
INTAKE: dict[str, Any] = {
    "rental_company_paid": "NOT_RENTAL",
    "ticket": {
        "citation_number": "SYN-0001",
        "license_plate": "SYN123",
        "license_plate_state": "CA",
        "issuer_hint": "San Francisco Municipal Transportation Agency",
        "issued_city": "San Francisco",
        "issued_state": "CA",
        "violation_code": "SYN-METER",
        "violation_description": "Meter expired",
        "issue_date": "2026-10-12",
        "stage": "first-level protest",
    },
    "customer_position": {"statement": "The meter was broken; I have a photo."},
    "contact": {
        "name": "Alex Example",
        "phone": "415-555-0100",
        "email": "alex@example.test",
        "mailing_address": {
            "line1": "123 Example Street",
            "city": "San Francisco",
            "state": "CA",
            "zip_code": "94103",
        },
    },
    "photo_refs": ["upload://meter-photo"],
}
LADOT = copy.deepcopy(INTAKE)
LADOT["ticket"].update(
    issuer_hint="Los Angeles Department of Transportation", issued_city="Los Angeles"
)
PRIVATE = copy.deepcopy(INTAKE)
PRIVATE["ticket"].update(issuer_hint="Synthetic Parking LLC", issuer_kind="PRIVATE")


class Resolved:
    def __init__(self, agency: object, stop: object = None, code: object = None) -> None:
        self.agency, self.stop, self.code = agency, stop, code


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def fake_resolution(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []

    def resolve(query: Any, *_args: object, **_kwargs: object) -> Resolved:
        seen.append(query.printed_issuer)
        if "Los Angeles" in query.printed_issuer:
            from dataclasses import replace

            return Resolved(
                replace(SAN_FRANCISCO, id="CA:lacity.gov", official_domains=("lacity.gov",))
            )
        if "Unfamiliar County" in query.printed_issuer:
            from dataclasses import replace

            return Resolved(
                replace(
                    SAN_FRANCISCO,
                    id="CA:county.gov",
                    name="Unfamiliar County Parking Board",
                    official_domains=("county.gov",),
                )
            )
        if "Unreachable" in query.printed_issuer:
            raise SearchError("rate limited")
        return Resolved(SAN_FRANCISCO)

    monkeypatch.setattr(prehold, "resolve_ticket_issuer", resolve)
    return seen


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def stripe(*responses: object) -> tuple[StripePaymentClient, Transport]:
    transport = Transport(*responses)
    return (
        StripePaymentClient(
            secret_key="sk_test_offline", api_version=STRIPE_API_VERSION, transport=transport
        ),
        transport,
    )


def service(
    path: Path,
    payments: PaymentClient | None = None,
    clock: Callable[[], datetime] = lambda: NOW,
    launch_allowlist: frozenset[str] | None = None,
) -> CaseService:
    unused: Any = object()
    deps = PreholdDeps(
        unused, None, unused, unused, unused, lambda: NOW, 25, STATE_BASIS, SERVICE_CONTROL
    )
    counter = iter(range(1000))
    return CaseService(
        CaseServiceDeps(
            SqliteTransactionStore(path),
            deps,
            clock,
            payments if payments is not None else stripe()[0],
            FakeBlobStore(),
            new_id=lambda: f"{next(counter):032x}",
            launch_allowlist=launch_allowlist,
        )
    )


def approve_with_preview(
    real: CaseService, case_id: str, adopted_sha256: str, signature_ref: str | None = None
) -> s.Approve:
    preview = real.packet_preview(
        case_id,
        s.PacketPreviewRequest(adopted_sha256=adopted_sha256, signature_ref=signature_ref),
    )
    return s.Approve(
        adopted_sha256=adopted_sha256,
        signature_ref=signature_ref,
        packet_sha256=preview.packet_sha256,
        envelope_sha256=preview.envelope_sha256,
    )


def dummy_approval(adopted_sha256: str, signature_ref: str | None = None) -> s.Approve:
    """Shape-valid digests let earlier refusal guards remain the behavior under test."""
    return s.Approve(
        adopted_sha256=adopted_sha256,
        signature_ref=signature_ref,
        packet_sha256="0" * 64,
        envelope_sha256="0" * 64,
    )


@pytest.fixture
async def api(tmp_path: Path) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=create_app(service(tmp_path / "pb.sqlite"))),
        base_url="http://real.test",
    ) as client:
        yield client


async def test_unprintable_statement_can_be_corrected_before_intake(api: AsyncClient) -> None:
    workspace = (await api.post("/workspaces", headers=ALICE, json={"notes": ""})).json()
    path = f"/workspaces/{workspace['workspace_id']}"
    photo = await api.post(
        f"{path}/photos",
        headers=ALICE,
        json={"content_type": "image/png", "data_base64": PNG_B64},
    )
    ref = photo.json()["photo_ref"]
    body = {**INTAKE, "photo_refs": [ref], "citation_photo_ref": ref}
    refused = await api.post(
        f"{path}/intake",
        headers=ALICE,
        json={**body, "customer_position": {"statement": "I didn\u2019t see the sign."}},
    )
    assert refused.status_code == 409
    assert refused.json()["detail"] == "customer_statement_not_printable"
    assert (await api.get(path, headers=ALICE)).json().get("intake") is None
    corrected = await api.post(f"{path}/intake", headers=ALICE, json=body)
    assert corrected.status_code == 200, corrected.text


async def submit(client: AsyncClient, body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    workspace = (await client.post("/workspaces", headers=ALICE, json={"notes": ""})).json()
    uploaded = await client.post(
        f"/workspaces/{workspace['workspace_id']}/photos",
        headers=ALICE,
        json={"content_type": "image/png", "data_base64": PNG_B64},
    )
    assert uploaded.status_code == 201, uploaded.text
    ref = uploaded.json()["photo_ref"]
    body = {**body, "photo_refs": [ref], "citation_photo_ref": ref}
    receipt = await client.post(
        f"/workspaces/{workspace['workspace_id']}/intake", headers=ALICE, json=body
    )
    assert receipt.status_code == 200, receipt.text
    return workspace["workspace_id"], receipt.json()


async def test_sfmta_intake_gets_the_certified_offer(api: AsyncClient) -> None:
    _workspace, receipt = await submit(api, INTAKE)
    assert receipt["state"] == "INTAKE"
    offer = (await api.get(f"/cases/{receipt['case_id']}/pre-hold", headers=ALICE)).json()
    assert offer["kind"] == "offer"
    assert offer["issuer"]["issuer_name"] == SAN_FRANCISCO.name
    assert [t["tier"] for t in offer["tiers"]] == ["CERTIFIED"]
    chosen = await api.put(
        f"/cases/{receipt['case_id']}/tier", headers=ALICE, json={"tier": "CERTIFIED"}
    )
    assert chosen.json()["selected_tier"] == "CERTIFIED"
    standard = await api.put(
        f"/cases/{receipt['case_id']}/tier", headers=ALICE, json={"tier": "STANDARD"}
    )
    assert standard.status_code == 409


async def test_ladot_intake_reaches_resolution_and_offer_by_default(
    api: AsyncClient, fake_resolution: list[str]
) -> None:
    _workspace, receipt = await submit(api, LADOT)
    assert receipt["state"] == "INTAKE"
    stop = (await api.get(f"/cases/{receipt['case_id']}/pre-hold", headers=ALICE)).json()
    assert stop["kind"] == "offer"
    assert stop["issuer"]["issuer_id"] == "CA:lacity.gov"
    assert [tier["tier"] for tier in stop["tiers"]] == ["CERTIFIED"]
    assert fake_resolution == ["Los Angeles Department of Transportation"]


async def test_unfamiliar_government_without_city_json_reaches_offer_by_default(
    api: AsyncClient, fake_resolution: list[str]
) -> None:
    body = copy.deepcopy(INTAKE)
    body["ticket"].update(
        issuer_hint="Unfamiliar County Parking Board", issued_city="Unknown County"
    )
    _workspace, receipt = await submit(api, body)
    offer = (await api.get(f"/cases/{receipt['case_id']}/pre-hold", headers=ALICE)).json()
    assert offer["kind"] == "offer"
    assert offer["issuer"]["issuer_id"] == "CA:county.gov"
    assert [tier["tier"] for tier in offer["tiers"]] == ["CERTIFIED"]
    assert fake_resolution == ["Unfamiliar County Parking Board"]


async def test_explicit_launch_allowlist_can_pause_ladot(
    tmp_path: Path, fake_resolution: list[str]
) -> None:
    client = AsyncClient(
        transport=ASGITransport(
            app=create_app(service(tmp_path / "paused.sqlite", launch_allowlist=LAUNCH_ALLOWLIST))
        ),
        base_url="http://real.test",
    )
    async with client:
        _workspace, receipt = await submit(client, LADOT)
        stop = (await client.get(f"/cases/{receipt['case_id']}/pre-hold", headers=ALICE)).json()
    assert stop["family"] == "BUSINESS_EXCLUSION"
    assert stop["no_charge"] is True
    assert fake_resolution == ["Los Angeles Department of Transportation"]


async def test_private_lot_stops_without_resolution(
    api: AsyncClient, fake_resolution: list[str]
) -> None:
    _workspace, receipt = await submit(api, PRIVATE)
    stop = (await api.get(f"/cases/{receipt['case_id']}/pre-hold", headers=ALICE)).json()
    assert stop["family"] == "BUSINESS_EXCLUSION"
    assert fake_resolution == []


async def test_provider_failure_is_a_plain_verification_stop(api: AsyncClient) -> None:
    body = copy.deepcopy(INTAKE)
    body["ticket"]["issuer_hint"] = "Unreachable Agency"
    _workspace, receipt = await submit(api, body)
    stop = (await api.get(f"/cases/{receipt['case_id']}/pre-hold", headers=ALICE)).json()
    assert (stop["family"], stop["hard_stop"]) == ("NOT_YET_VERIFIED", "S2")
    assert "rate limited" not in stop["message"]


async def test_replay_is_idempotent_and_ownership_is_enforced(api: AsyncClient) -> None:
    workspace, receipt = await submit(api, INTAKE)
    stored = (await api.get(f"/workspaces/{workspace}", headers=ALICE)).json()["intake"]
    again = await api.post(f"/workspaces/{workspace}/intake", headers=ALICE, json=stored)
    assert again.json() == receipt
    changed = copy.deepcopy(stored)
    changed["ticket"]["citation_number"] = "SYN-0002"
    conflict = await api.post(f"/workspaces/{workspace}/intake", headers=ALICE, json=changed)
    assert conflict.status_code == 409
    assert (await api.get(f"/cases/{receipt['case_id']}/pre-hold", headers=BOB)).status_code == 404
    assert (await api.get(f"/workspaces/{workspace}", headers=BOB)).status_code == 404


async def test_views_survive_restart(tmp_path: Path) -> None:
    path = tmp_path / "pb.sqlite"
    async with AsyncClient(
        transport=ASGITransport(app=create_app(service(path))), base_url="http://real.test"
    ) as first:
        workspace, receipt = await submit(first, INTAKE)
        await first.put(
            f"/cases/{receipt['case_id']}/tier", headers=ALICE, json={"tier": "CERTIFIED"}
        )
    async with AsyncClient(
        transport=ASGITransport(app=create_app(service(path))), base_url="http://real.test"
    ) as second:
        view = (await second.get(f"/workspaces/{workspace}", headers=ALICE)).json()
        assert view["case_id"] == receipt["case_id"]
        assert view["intake"]["ticket"]["citation_number"] == "SYN-0001"
        offer = (await second.get(f"/cases/{receipt['case_id']}/pre-hold", headers=ALICE)).json()
        assert offer["selected_tier"] == "CERTIFIED"


PM = {"terms_accepted": True, "payment_method_id": "pm_card_visa"}


async def authorized_case(
    tmp_path: Path, *responses: object
) -> tuple[AsyncClient, CaseService, Clock, Transport, str]:
    clock = Clock()
    payments, transport = stripe(*responses)
    real = service(tmp_path / "pb.sqlite", payments, clock)
    client = AsyncClient(transport=ASGITransport(app=create_app(real)), base_url="http://real.test")
    await client.__aenter__()
    _workspace, receipt = await submit(client, INTAKE)
    case = receipt["case_id"]
    await client.put(f"/cases/{case}/tier", headers=ALICE, json={"tier": "CERTIFIED"})
    return client, real, clock, transport, case


async def test_authorization_holds_39_and_starts_the_window(tmp_path: Path) -> None:
    client, _real, _clock, transport, case = await authorized_case(tmp_path, fixture())
    try:
        response = await client.post(f"/cases/{case}/authorization", headers=ALICE, json=PM)
        body = response.json()
        assert response.status_code == 200, response.text
        assert (body["status"], body["amount_cents"], body["fake"]) == ("authorized", 2999, False)
        assert (await client.get(f"/cases/{case}/status", headers=ALICE)).json()[
            "amount_cents"
        ] == 2999
        assert body["payment_intent_id"] == REF
        started = datetime.fromisoformat(body["window_started_at"])
        assert datetime.fromisoformat(body["window_expires_at"]) - started == DEFAULT_WINDOW
        replay = await client.post(f"/cases/{case}/authorization", headers=ALICE, json=PM)
        assert replay.json() == body
        assert len(transport.calls) == 1
        params = transport.calls[0][2]
        assert params["payment_method"] == ["pm_card_visa"]
        assert params["capture_method"] == ["manual"]
        assert transport.calls[0][3] == mutation_key(
            "authorization", case, "CERTIFIED:2999", "pm_card_visa"
        )
        locked = await client.put(f"/cases/{case}/tier", headers=ALICE, json={"tier": "STANDARD"})
        assert locked.status_code == 409
    finally:
        await client.aclose()


async def test_tier_must_be_chosen_first(tmp_path: Path) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=create_app(service(tmp_path / "pb.sqlite"))),
        base_url="http://real.test",
    ) as client:
        _workspace, receipt = await submit(client, INTAKE)
        response = await client.post(
            f"/cases/{receipt['case_id']}/authorization", headers=ALICE, json=PM
        )
        assert response.json() == {"detail": "choose_tier_first"}


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("requires_action", "card_not_authorized"),
        ("requires_payment_method", "card_not_authorized"),
    ],
)
async def test_unfinished_hold_starts_no_case(tmp_path: Path, status: str, reason: str) -> None:
    client, real, _clock, _transport, case = await authorized_case(tmp_path, fixture(status))
    try:
        response = await client.post(f"/cases/{case}/authorization", headers=ALICE, json=PM)
        assert (response.status_code, response.json()["detail"]) == (409, reason)
        record = real._store.get_intake(case)
        assert record is not None
        assert record.case_token is None
    finally:
        await client.aclose()


async def test_window_expiry_stops_the_letter_and_queues_release(tmp_path: Path) -> None:
    client, real, clock, _transport, case = await authorized_case(tmp_path, fixture())
    try:
        await client.post(f"/cases/{case}/authorization", headers=ALICE, json=PM)
        clock.now = NOW + DEFAULT_WINDOW + timedelta(minutes=1)
        await client.get(f"/cases/{case}/pre-hold", headers=ALICE)
        record = real._store.get_intake(case)
        assert record is not None
        assert record.case_token is not None
        stored = real._store.get_case(record.case_token)
        assert stored is not None
        assert stored.letter_state is CaseState.STOPPED_NO_CHARGE
        assert real._store.pending_effects(record.case_token)
    finally:
        await client.aclose()


async def test_window_deadline_survives_restart(tmp_path: Path) -> None:
    client, _real, _clock, _transport, case = await authorized_case(tmp_path, fixture())
    try:
        first = (await client.post(f"/cases/{case}/authorization", headers=ALICE, json=PM)).json()
    finally:
        await client.aclose()
    reopened = service(tmp_path / "pb.sqlite")
    async with AsyncClient(
        transport=ASGITransport(app=create_app(reopened)), base_url="http://real.test"
    ) as again:
        replay = await again.post(f"/cases/{case}/authorization", headers=ALICE, json=PM)
        assert replay.json()["window_expires_at"] == first["window_expires_at"]


async def researched_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, result: object, *, free_beta: bool = False
) -> tuple[AsyncClient, CaseService, str]:
    from parkingbreaker import case_service
    from parkingbreaker.case_service import ResearchDeps

    calls: list[object] = []

    def run(*args: object, **kwargs: object) -> object:
        calls.append(kwargs.get("remaining_time"))
        if isinstance(result, Exception):
            raise result
        from parkingbreaker.research.run import ResearchResult
        from prepared_packet_support import fetched_at

        return (
            fetched_at(result, real._deps.clock()) if isinstance(result, ResearchResult) else result
        )

    monkeypatch.setattr(case_service, "run_research", run)
    unused: Any = object()
    # One response authorizes; the second is the fresh original pre-mail read.
    payments, _transport = stripe(fixture(), fixture())
    real = service(tmp_path / "pb.sqlite", payments)
    object.__setattr__(real._deps, "research", ResearchDeps(unused, unused, None, lambda _s: None))
    client = AsyncClient(transport=ASGITransport(app=create_app(real)), base_url="http://real.test")
    await client.__aenter__()
    _workspace, receipt = await submit(client, INTAKE)
    case = receipt["case_id"]
    await client.put(f"/cases/{case}/tier", headers=ALICE, json={"tier": "CERTIFIED"})
    if free_beta:
        from parkingbreaker.beta_store import BETA_DISCLOSURE_SHA256
        from parkingbreaker.service_phase import ServicePhase

        store = real._store
        offer = real.pre_hold(case)
        assert isinstance(offer, s.Offer)
        issuer = offer.issuer.issuer_id
        digest = "a" * 64
        store.create_beta_campaign(
            "beta-1",
            NOW - timedelta(days=1),
            NOW + timedelta(days=10),
            "synthetic-identity",
            "synthetic-approval",
            NOW,
        )
        store.bind_beta_jurisdiction("beta-1", issuer, "synthetic-jurisdiction", digest, NOW)
        store.qualify_beta_candidate("beta-1", case, digest, NOW + timedelta(days=1), NOW)
        store.select_beta_batch("beta-1", "batch-1", "synthetic-release", NOW)
        object.__setattr__(real._deps, "service_phase", ServicePhase.CONTROLLED_FREE_BETA)
        real.accept_beta(
            case, s.AcceptBeta(terms_accepted=True, disclosure_sha256=BETA_DISCLOSURE_SHA256)
        )
    else:
        await client.post(f"/cases/{case}/authorization", headers=ALICE, json=PM)
    return client, real, case


WORKER = {"Authorization": "Bearer fake-worker"}


async def test_research_saves_evidence_and_asks_the_vehicle_question(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_tier_acceptance import extract_case, model_answers

    result, _client, _searcher = extract_case(model_answers())
    client, real, case = await researched_case(tmp_path, monkeypatch, result)
    try:
        step = await client.post(
            f"/internal/cases/{case}/step", headers=WORKER, json={"step_id": "research"}
        )
        status = step.json()
        assert step.status_code == 200, step.text
        assert status["research"]["sources"], status
        assert {f["fact"] for f in status["research"]["findings"]}
        assert real._store.get_research(case) is not None
        if status["state"] == "NEEDS_CUSTOMER":
            assert status["question"]["question_id"] == "VEHICLE_RELATIONSHIP"
            answered = await client.post(
                f"/cases/{case}/answers",
                headers=ALICE,
                json={"question_id": "VEHICLE_RELATIONSHIP", "answer": "REGISTERED_OWNER"},
            )
            status = answered.json()
        assert status["state"] == "DRAFTING", status
        again = await client.post(
            f"/internal/cases/{case}/step", headers=WORKER, json={"step_id": "research"}
        )
        assert again.json()["state"] == status["state"]
    finally:
        await client.aclose()


async def test_failed_research_is_a_no_charge_verification_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _real, case = await researched_case(tmp_path, monkeypatch, SearchError("rate limited"))
    try:
        status = (
            await client.post(
                f"/internal/cases/{case}/step", headers=WORKER, json={"step_id": "research"}
            )
        ).json()
        assert status["state"] == "STOPPED_NO_CHARGE"
        assert (status["stop"]["family"], status["stop"]["hard_stop"]) == ("NOT_YET_VERIFIED", "S1")
        assert "rate limited" not in status["stop"]["message"]
        assert status["stop"]["no_charge"] is True
    finally:
        await client.aclose()


async def test_unknown_or_foreign_photo_refs_are_refused(api: AsyncClient) -> None:
    workspace = (await api.post("/workspaces", headers=ALICE, json={"notes": ""})).json()
    body = {**INTAKE, "photo_refs": ["photo_" + "0" * 64]}
    response = await api.post(
        f"/workspaces/{workspace['workspace_id']}/intake", headers=ALICE, json=body
    )
    assert (response.status_code, response.json()["detail"]) == (409, "unknown_photo_ref")


async def test_photo_upload_validates_images(api: AsyncClient) -> None:
    workspace = (await api.post("/workspaces", headers=ALICE, json={"notes": ""})).json()
    path = f"/workspaces/{workspace['workspace_id']}/photos"
    bad = base64.b64encode(b"not an image").decode()
    rejected = await api.post(
        path, headers=ALICE, json={"content_type": "image/png", "data_base64": bad}
    )
    assert rejected.json()["detail"] == "photo_not_readable"
    foreign = await api.post(
        path, headers=BOB, json={"content_type": "image/png", "data_base64": PNG_B64}
    )
    assert foreign.status_code == 404


async def drafting_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, free_beta: bool = False
) -> tuple[AsyncClient, CaseService, str]:
    from parkingbreaker.appeal.drafting import Draft, DraftSentence
    from parkingbreaker.appeal.fakes import FakeDrafter
    from test_tier_acceptance import extract_case, model_answers

    result, _llm, _searcher = extract_case(model_answers())
    client, real, case = await researched_case(tmp_path, monkeypatch, result, free_beta=free_beta)
    statement = INTAKE["customer_position"]["statement"]
    object.__setattr__(
        real._deps,
        "drafter",
        FakeDrafter(lambda *_: Draft((DraftSentence(statement, (f"note:0-{len(statement)}",)),))),
    )
    status = real.step(case, s.WorkerStep(step_id="research"))
    if status.state is CaseState.NEEDS_CUSTOMER:
        status = real.answer_question(
            case,
            s.AnswerQuestion(
                question_id="VEHICLE_RELATIONSHIP", answer=VehicleRelationship.REGISTERED_OWNER
            ),
        )
    assert status.state is CaseState.DRAFTING
    return client, real, case


async def test_draft_step_saves_letter_and_ai_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, real, case = await drafting_case(tmp_path, monkeypatch)
    try:
        status = real.step(case, s.WorkerStep(step_id="draft"))
        assert status.state is CaseState.AWAITING_ADOPTION
        draft = real.draft(case)
        for value in ("SYN-0001", "SYN123", "Alex Example", "415-555-0100", "alex@example.test"):
            assert value in draft.text
        assert draft.spans == status.draft_spans
        assert draft.spans[0].author.value == "ai"
        assert draft.flags == []
        wire_draft = (await client.get(f"/cases/{case}/draft", headers=ALICE)).json()
        wire_status = (await client.get(f"/cases/{case}/status", headers=ALICE)).json()
        assert wire_draft["spans"][0]["author"] == "ai"
        assert wire_status["draft_spans"] == wire_draft["spans"]
        real.step(case, s.WorkerStep(step_id="draft"))
        assert real.draft(case) == draft
    finally:
        await client.aclose()


async def test_omitted_customer_statement_blocks_adoption_until_actual_text_is_restored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from parkingbreaker.appeal.drafting import Draft, DraftSentence
    from parkingbreaker.appeal.fakes import FakeDrafter

    client, real, case = await drafting_case(tmp_path, monkeypatch)
    try:
        record = real._intake(case)
        workspace = real._store.get_workspace(record.workspace_token)
        assert workspace is not None
        extra = "The meter was broken."
        statement = INTAKE["customer_position"]["statement"]
        real._store.save_workspace(
            record.workspace_token,
            replace(workspace.workspace, notes=extra),
            NOW,
            expected_revision=workspace.revision,
        )
        start = len(extra) + 2
        object.__setattr__(
            real._deps,
            "drafter",
            FakeDrafter(
                lambda *_: Draft(
                    (DraftSentence(statement, (f"note:{start}-{start + len(statement)}",)),)
                )
            ),
        )
        real.step(case, s.WorkerStep(step_id="draft"))
        draft = real.draft(case)
        assert any(
            "omitted customer text" in detail for flag in draft.flags for detail in flag.details
        )
        with pytest.raises(ServiceConflict, match="draft_needs_review"):
            real.adopt_text(case, s.AdoptText(text=draft.text, text_sha256=draft.text_sha256))
        unrelated = real.edit(
            case,
            s.EditDraft(text=draft.text + "\nPlease review.", expected_sha256=draft.text_sha256),
        )
        assert unrelated.flags
        restored = real.edit(
            case,
            s.EditDraft(
                text=unrelated.text.replace(statement, extra + "\n\n" + statement),
                expected_sha256=unrelated.text_sha256,
            ),
        )
        assert not restored.flags
        real.adopt_text(case, s.AdoptText(text=restored.text, text_sha256=restored.text_sha256))
        assert real.draft(case).adopted_sha256 == restored.text_sha256
    finally:
        await client.aclose()


async def test_stale_edit_refused_and_edit_clears_adoption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, real, case = await drafting_case(tmp_path, monkeypatch)
    try:
        real.step(case, s.WorkerStep(step_id="draft"))
        draft = real.draft(case)
        real.adopt_text(case, s.AdoptText(text=draft.text, text_sha256=draft.text_sha256))
        with pytest.raises(ServiceConflict, match=r"^stale_draft$"):
            real.edit(case, s.EditDraft(text=draft.text + " ", expected_sha256="0" * 64))
        edited = real.edit(
            case, s.EditDraft(text=draft.text + " ", expected_sha256=draft.text_sha256)
        )
        assert edited.text == draft.text + " "
        assert edited.adopted_sha256 is None
        assert edited.text_sha256 == text_sha256(edited.text)
        assert any(span.author.value == "ai" for span in edited.spans)
        assert any(span.author.value == "customer" for span in edited.spans)
        record = real._intake(case)
        workspace = real._store.get_workspace(record.workspace_token)
        assert workspace is not None
        assert workspace.workspace.letter_edits == (edited.text,)
        with pytest.raises(ServiceConflict, match=r"^adopt_current_text_first$"):
            real.approve(case, dummy_approval(draft.text_sha256))
    finally:
        await client.aclose()


async def test_approval_requires_adoption_and_freezes_exact_embedded_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib
    import io

    from pypdf import PdfReader

    from parkingbreaker.mail_config import MailClass

    client, real, case = await drafting_case(tmp_path, monkeypatch)
    try:
        real.step(case, s.WorkerStep(step_id="draft"))
        draft = real.draft(case)
        with pytest.raises(ServiceConflict, match=r"^adopt_current_text_first$"):
            real.approve(case, dummy_approval(draft.text_sha256))
        real.adopt_text(case, s.AdoptText(text=draft.text, text_sha256=draft.text_sha256))
        approval = approve_with_preview(real, case, draft.text_sha256)
        status = real.approve(case, approval)
        assert status.state is CaseState.FROZEN
        record = real._intake(case)
        assert record.case_token is not None
        prepared = real._store.get(record.case_token, 1)
        assert prepared is not None
        packet = prepared.request.pdf
        assert PdfReader(io.BytesIO(packet)).attachments["adopted-letter.txt"] == [
            draft.text.encode()
        ]
        saved = real._store.get_draft(case)
        assert saved is not None
        assert saved.packet_sha256 == hashlib.sha256(packet).hexdigest()
        stored_sha = (
            real._store._require_open()
            .execute(
                "SELECT pdf_sha256 FROM pb_mail_attempts WHERE case_token=?", (record.case_token,)
            )
            .fetchone()[0]
        )
        assert stored_sha == saved.packet_sha256
        assert prepared.request.options.mail_class is MailClass.CERTIFIED
        assert prepared.request.sender.recipient == INTAKE["contact"]["name"]
        assert real.approve(case, approval) == status
    finally:
        await client.aclose()


async def test_missing_citation_photo_refuses_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from parkingbreaker.blob_store import FakeBlobStore

    client, real, case = await drafting_case(tmp_path, monkeypatch)
    try:
        real.step(case, s.WorkerStep(step_id="draft"))
        draft = real.draft(case)
        real.adopt_text(case, s.AdoptText(text=draft.text, text_sha256=draft.text_sha256))
        object.__setattr__(real._deps, "photos", FakeBlobStore())
        with pytest.raises(ServiceConflict, match=r"^citation_photo_required$"):
            real.approve(case, dummy_approval(draft.text_sha256))
        assert real.status(case).state is CaseState.AWAITING_ADOPTION
        record = real._intake(case)
        assert record.case_token is not None
        assert real._store.get(record.case_token, 1) is None
    finally:
        await client.aclose()


async def test_restart_mid_adoption_preserves_exact_draft_and_adoption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, real, case = await drafting_case(tmp_path, monkeypatch)
    try:
        real.step(case, s.WorkerStep(step_id="draft"))
        draft = real.draft(case)
        restarted = service(tmp_path / "pb.sqlite")
        assert restarted.draft(case) == draft
        adoption = restarted.adopt_text(
            case, s.AdoptText(text=draft.text, text_sha256=draft.text_sha256)
        )
        again = service(tmp_path / "pb.sqlite")
        assert again.draft(case).adopted_sha256 == adoption.adopted_sha256
        assert (
            again.adopt_text(case, s.AdoptText(text=draft.text, text_sha256=draft.text_sha256))
            == adoption
        )
        assert again.status(case).draft_spans == draft.spans
    finally:
        await client.aclose()


async def test_tier_one_gap_refuses_approval_without_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from parkingbreaker.research.findings import FindingStatus
    from parkingbreaker.research.questions import FactKey
    from parkingbreaker.research_records import decode_research, research_json

    client, real, case = await drafting_case(tmp_path, monkeypatch)
    try:
        real.step(case, s.WorkerStep(step_id="draft"))
        draft = real.draft(case)
        real.adopt_text(case, s.AdoptText(text=draft.text, text_sha256=draft.text_sha256))
        saved = real._store.get_research(case)
        assert saved is not None
        result = decode_research(saved.research_json)
        result = replace(
            result,
            findings=tuple(
                replace(
                    f,
                    status=FindingStatus.UNKNOWN,
                    value=None,
                    verbatim_quote=None,
                    source_sha256s=(),
                )
                if f.key is FactKey.SIGNATURE_REQUIREMENT
                else f
                for f in result.findings
            ),
        )
        real._store.save_research(
            case, research_json(result), saved.answers_json, NOW, saved.revision
        )
        with pytest.raises(ServiceConflict, match=r"^approval_refused$"):
            real.approve(case, dummy_approval(draft.text_sha256))
        record = real._intake(case)
        assert record.case_token is not None
        assert real._store.get(record.case_token, 1) is None
    finally:
        await client.aclose()


async def test_drafting_masks_known_customer_identifiers_before_llm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from parkingbreaker.appeal.drafting import LlmDrafter
    from prompt_capture import PromptCapture
    from test_ai_privacy import echo_notes

    client, real, case = await drafting_case(tmp_path, monkeypatch)
    try:
        record = real._intake(case)
        workspace = real._store.get_workspace(record.workspace_token)
        assert workspace is not None
        private = (
            "Alex Example",
            "123 Example Street",
            "415-555-0100",
            "alex@example.test",
            "SYN-0001",
            "SYN123",
        )
        notes = "\n".join((*private, "Alex described the covered sign."))
        real._store.save_workspace(
            record.workspace_token,
            replace(workspace.workspace, notes=notes),
            NOW,
            expected_revision=workspace.revision,
        )
        llm = PromptCapture((*private, "Alex"), echo_notes)
        object.__setattr__(real._deps, "drafter", LlmDrafter(llm))
        real.step(case, s.WorkerStep(step_id="draft"))
        assert llm.calls
        for system, prompt in llm.calls:
            for value in private:
                assert value not in system + prompt
            assert "Alex" not in system + prompt
        assert real.draft(case).flags == []
        assert "Alex Example" in real.draft(case).text
    finally:
        await client.aclose()


async def test_customer_review_flags_survive_restart_and_block_adoption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from parkingbreaker.appeal.drafting import Draft, DraftSentence
    from parkingbreaker.appeal.fakes import FakeDrafter

    client, real, case = await drafting_case(tmp_path, monkeypatch)
    try:
        statement = INTAKE["customer_position"]["statement"]
        drafter = FakeDrafter(lambda *_: Draft((DraftSentence(statement, ()),)))
        object.__setattr__(real._deps, "drafter", drafter)
        assert real.step(case, s.WorkerStep(step_id="draft")).state is CaseState.AWAITING_ADOPTION
        assert len(drafter.calls) == 3
        draft = real.draft(case)
        assert draft.flags
        restarted = service(tmp_path / "pb.sqlite")
        assert restarted.draft(case).flags == draft.flags
        with pytest.raises(ServiceConflict, match=r"^draft_needs_review$"):
            restarted.adopt_text(case, s.AdoptText(text=draft.text, text_sha256=draft.text_sha256))
        edited = restarted.edit(
            case, s.EditDraft(text="The meter was broken.", expected_sha256=draft.text_sha256)
        )
        assert edited.flags == []
        assert all(span.author.value == "customer" for span in edited.spans)
        restarted.adopt_text(case, s.AdoptText(text=edited.text, text_sha256=edited.text_sha256))
    finally:
        await client.aclose()


async def test_restart_after_preparation_reuses_frozen_bytes_and_submission_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from parkingbreaker.case_state import CustomerApproved, Event

    client, real, case = await drafting_case(tmp_path, monkeypatch)
    try:
        real.step(case, s.WorkerStep(step_id="draft"))
        draft = real.draft(case)
        real.adopt_text(case, s.AdoptText(text=draft.text, text_sha256=draft.text_sha256))
        original = real._apply

        def crash(token: str, event: Event) -> None:
            if isinstance(event, CustomerApproved):
                raise RuntimeError("simulated crash")
            original(token, event)

        monkeypatch.setattr(real, "_apply", crash)
        approval = approve_with_preview(real, case, draft.text_sha256)
        with pytest.raises(RuntimeError, match="simulated crash"):
            real.approve(case, approval)
        record = real._intake(case)
        assert record.case_token is not None
        prepared = real._store.get(record.case_token, 1)
        assert prepared is not None
        restarted = service(tmp_path / "pb.sqlite")
        object.__setattr__(restarted._deps, "photos", real._deps.photos)
        with pytest.raises(ServiceConflict, match=r"^draft_not_editable$"):
            restarted.edit(
                case, s.EditDraft(text=draft.text + " ", expected_sha256=draft.text_sha256)
            )
        assert restarted.approve(case, approval).state is CaseState.FROZEN
        assert restarted._store.get(record.case_token, 1) == prepared
    finally:
        await client.aclose()


async def test_window_expiry_during_drafting_retains_text_and_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from parkingbreaker.appeal.drafting import Draft, DraftSentence
    from parkingbreaker.appeal.fakes import FakeDrafter

    client, real, case = await drafting_case(tmp_path, monkeypatch)
    try:
        clock = Clock()
        statement = INTAKE["customer_position"]["statement"]

        def late_draft(*_args: object) -> Draft:
            clock.now += DEFAULT_WINDOW + timedelta(minutes=1)
            return Draft((DraftSentence(statement, (f"note:0-{len(statement)}",)),))

        object.__setattr__(real._deps, "clock", clock)
        object.__setattr__(real._deps, "drafter", FakeDrafter(late_draft))
        assert real.step(case, s.WorkerStep(step_id="draft")).state is CaseState.STOPPED_NO_CHARGE
        draft = real.draft(case)
        assert statement in draft.text
        with pytest.raises(ServiceConflict, match=r"^draft_not_adoptable$"):
            real.adopt_text(case, s.AdoptText(text=draft.text, text_sha256=draft.text_sha256))
    finally:
        await client.aclose()


async def test_draft_store_refuses_stale_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from parkingbreaker.intake_store import IntakeConflict

    client, real, case = await drafting_case(tmp_path, monkeypatch)
    try:
        real.step(case, s.WorkerStep(step_id="draft"))
        saved = real._store.get_draft(case)
        assert saved is not None
        updated = real._store.save_draft(
            case, replace(saved, letter_text=saved.letter_text + " "), saved.revision
        )
        with pytest.raises(IntakeConflict, match=r"^draft_revision_stale$"):
            real._store.save_draft(case, saved, saved.revision)
        assert real._store.get_draft(case) == updated
    finally:
        await client.aclose()


async def test_drawn_signature_is_loaded_from_the_owners_uploads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import io

    from PIL import Image, ImageDraw

    client, real, case = await drafting_case(tmp_path, monkeypatch)
    try:
        real.step(case, s.WorkerStep(step_id="draft"))
        draft = real.draft(case)
        real.adopt_text(case, s.AdoptText(text=draft.text, text_sha256=draft.text_sha256))
        with pytest.raises(ServiceConflict, match=r"^unknown_signature_ref$"):
            real.approve(case, dummy_approval(draft.text_sha256, "photo_" + "0" * 64))
        ink = Image.new("RGB", (120, 28), "white")
        ImageDraw.Draw(ink).line((5, 20, 110, 6), fill="black", width=3)
        out = io.BytesIO()
        ink.save(out, "PNG")
        owner = real._intake(case).owner
        ref = real._deps.photos.put(owner, out.getvalue())
        status = real.approve(case, approve_with_preview(real, case, draft.text_sha256, ref))
        assert status.state is CaseState.FROZEN
    finally:
        await client.aclose()
