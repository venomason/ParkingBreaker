"""T30 piece 5a: the customer-directed destination lane, through the API on real SQLite.

When the official mailing address cannot be verified, the case waits (no paid drafting, no
charge) for the customer to supply an address and explicitly accept the current disclosure,
or to decline. Mailing then goes through the same approval and Lob gate to exactly that
address. Nothing is inferred, defaulted or pre-accepted.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from httpx2 import AsyncClient

from parkingbreaker.address_disclosure import (
    DISCLOSURE_SHA256,
    DISCLOSURE_TEXT,
    DISCLOSURE_VERSION,
)
from parkingbreaker.agencies import MailingAddress
from parkingbreaker.api import schemas as s
from parkingbreaker.appeal.drafting import Draft, DraftSentence
from parkingbreaker.appeal.fakes import FakeDrafter
from parkingbreaker.case_service import CaseService
from parkingbreaker.case_state import CaseState
from parkingbreaker.lob_fakes import FakeLobClient
from parkingbreaker.lob_submission import LetterEvidence, LobResponse
from parkingbreaker.payment_state import PaymentState
from parkingbreaker.research.questions import FactKey
from test_case_service import (
    ALICE,
    BOB,
    INTAKE,
    PM,
    PNG_B64,
    fake_resolution,
    researched_case,
    stripe,
)
from test_stripe_payment import fixture

assert fake_resolution
pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("fake_resolution")]
ADDRESS: dict[str, str] = {
    "recipient": "Parking Review Office",
    "line1": "500 Chosen Street",
    "city": "Elsewhere",
    "state": "CA",
    "zip_code": "94105",
}
CHOSEN = MailingAddress(
    "Parking Review Office", None, "500 Chosen Street", None, "Elsewhere", "CA", "94105"
)
OTHER = {**ADDRESS, "line1": "999 Different Avenue"}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def consent_body(receipt: dict[str, Any], **changes: Any) -> dict[str, Any]:
    body = {
        "address_revision": receipt["address_revision"],
        "address_fingerprint": receipt["address_fingerprint"],
        "disclosure_sha256": DISCLOSURE_SHA256,
        "accepted": True,
    }
    return {**body, **changes}


async def post(
    client: AsyncClient,
    case: str,
    suffix: str,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] = ALICE,
) -> Any:
    return await client.post(f"/cases/{case}/destination/{suffix}", headers=headers, json=body)


async def start_case(client: AsyncClient, real: CaseService) -> str:
    """A second authorized case, owned by another customer (one appeal per customer)."""
    workspace = (await client.post("/workspaces", headers=BOB, json={"notes": ""})).json()
    uploaded = await client.post(
        f"/workspaces/{workspace['workspace_id']}/photos",
        headers=BOB,
        json={"content_type": "image/png", "data_base64": PNG_B64},
    )
    ref = uploaded.json()["photo_ref"]
    body = {**INTAKE, "photo_refs": [ref], "citation_photo_ref": ref}
    receipt = await client.post(
        f"/workspaces/{workspace['workspace_id']}/intake", headers=BOB, json=body
    )
    assert receipt.status_code == 200, receipt.text
    case = str(receipt.json()["case_id"])
    await client.put(f"/cases/{case}/tier", headers=BOB, json={"tier": "CERTIFIED"})
    second = fixture()
    second["id"] = "pi_SecondCase"
    charge = second["latest_charge"]
    assert isinstance(charge, dict)
    charge["payment_intent"] = "pi_SecondCase"
    payments, _transport = stripe(second)
    object.__setattr__(real._deps, "payments", payments)
    assert (
        await client.post(f"/cases/{case}/authorization", headers=BOB, json=PM)
    ).status_code == 200
    return case


async def unverified_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    answer_vehicle: bool = True,
    ask_vehicle: bool = False,
) -> tuple[AsyncClient, CaseService, str, FakeLobClient]:
    """Research succeeded on everything except the official mailing address."""
    from test_tier_acceptance import extract_case, model_answers, policy_page, requirement

    findings = [item for item in model_answers() if str(item.get("key")) != "MAILING_ADDRESS"]
    policy = None
    if ask_vehicle:
        # Same research as the existing vehicle-question fixtures, minus the mailing address.
        quote = "Only the registered owner may contest."
        findings = [f for f in findings if f["key"] != "CONTEST_ELIGIBILITY"]
        findings.append(requirement(FactKey.CONTEST_ELIGIBILITY, quote))
        policy = policy_page(quote)
    result, _llm, _searcher = extract_case(findings, policy=policy)
    client, real, case = await researched_case(tmp_path, monkeypatch, result)
    statement = INTAKE["customer_position"]["statement"]
    object.__setattr__(
        real._deps,
        "drafter",
        FakeDrafter(lambda *_: Draft((DraftSentence(statement, (f"note:0-{len(statement)}",)),))),
    )
    lob = FakeLobClient(
        [
            LobResponse(
                200,
                "ltr_customer_directed",
                tracking_number="940000009",
                evidence=LetterEvidence.PRODUCTION,
            )
        ]
    )
    object.__setattr__(real._deps, "lob", lob)
    status = real.step(case, s.WorkerStep(step_id="research"))
    if answer_vehicle and status.state is CaseState.NEEDS_CUSTOMER:
        status = real.answer_question(
            case,
            s.AnswerQuestion(question_id="VEHICLE_RELATIONSHIP", answer="REGISTERED_OWNER"),  # type: ignore[arg-type]
        )
    return client, real, case, lob


def drafter_calls(real: CaseService) -> int:
    drafter = real._deps.drafter
    assert isinstance(drafter, FakeDrafter)
    return len(drafter.calls)


def decision_history(real: CaseService, case: str) -> int:
    record = real._intake(case)
    return len(real._store.destination_decision_history(case, record.owner))


async def accept(client: AsyncClient, case: str, receipt: dict[str, Any]) -> Any:
    response = await post(client, case, "consent", consent_body(receipt))
    assert response.status_code == 200, response.text
    return response.json()


# Current-policy tests. Historical positive consent/mailing expectations are archived
# in the implementation evidence; no production opt-in exists in these tests.


@pytest.mark.parametrize("ask_vehicle", [False, True])
async def test_unverified_destination_stops_without_offering_a_waiver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ask_vehicle: bool
) -> None:
    client, real, case, lob = await unverified_case(tmp_path, monkeypatch, ask_vehicle=ask_vehicle)
    try:
        status = real.status(case)
        assert status.state is CaseState.STOPPED_NO_CHARGE
        assert status.destination_choice is None
        assert status.stop is not None
        assert status.stop.hard_stop is not None
        assert status.stop.hard_stop.value == "S2"
        assert status.stop.message == (
            "We couldn't confirm the correct address for an appeal at this stage yet. "
            "Nothing was charged. Your work is saved. "
            "You can review your saved details and try again."
        )
        for _ in range(3):
            real.advance_due()
        assert drafter_calls(real) == 0
        assert not lob.calls
        assert real._store.get_draft(case) is None
        record = real._intake(case)
        assert real._store.get(record.case_token or "", 1) is None
        assert decision_history(real, case) == 0
        # A stop emits release through the existing payment effect, never capture.
        assert real.status(case).payment_state in {
            PaymentState.RELEASED,
            PaymentState.AUTHORIZED,
        }
    finally:
        await client.aclose()
        real._store.close()


@pytest.mark.parametrize("suffix", ["address", "consent"])
@pytest.mark.parametrize(
    "variant", ["current", "other_address", "other_case_hash", "old_disclosure"]
)
async def test_no_address_or_consent_variant_authorizes_unverified_mailing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str, variant: str
) -> None:
    client, real, case, lob = await unverified_case(tmp_path, monkeypatch)
    try:
        receipt = {"address_revision": 1, "address_fingerprint": "a" * 64}
        body = (
            dict(OTHER if variant == "other_address" else ADDRESS)
            if suffix == "address"
            else consent_body(receipt)
        )
        if suffix == "consent" and variant == "old_disclosure":
            body["disclosure_sha256"] = "b" * 64
        response = await post(client, case, suffix, body)
        assert response.status_code == 409
        assert response.json() == {"detail": "destination_verification_required"}
        record = real._intake(case)
        assert not real._store.customer_address_revisions(case, record.owner)
        assert not real._store.address_consent_events(case, record.owner)
        assert decision_history(real, case) == 0
        assert real._store.get(record.case_token or "", 1) is None
        assert not lob.calls
        assert drafter_calls(real) == 0
    finally:
        await client.aclose()
        real._store.close()


@pytest.mark.parametrize("suffix", ["address", "consent"])
async def test_closed_address_endpoints_keep_owner_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str
) -> None:
    client, real, case, lob = await unverified_case(tmp_path, monkeypatch)
    try:
        body = (
            ADDRESS
            if suffix == "address"
            else consent_body({"address_revision": 1, "address_fingerprint": "a" * 64})
        )
        response = await post(client, case, suffix, body, BOB)
        assert response.status_code == 404
        assert not lob.calls
    finally:
        await client.aclose()
        real._store.close()


async def test_historical_disclosure_still_available_without_authorizing_consent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, real, case, _lob = await unverified_case(tmp_path, monkeypatch)
    try:
        response = await client.get(f"/cases/{case}/destination/disclosure", headers=ALICE)
        assert response.status_code == 200
        assert response.json() == {
            "version": DISCLOSURE_VERSION,
            "sha256": DISCLOSURE_SHA256,
            "text": DISCLOSURE_TEXT,
        }
        assert real.status(case).destination_choice is None
    finally:
        await client.aclose()
        real._store.close()
