"""P03: a Lob-rejected first mailing stops no-charge, and the customer can restart.

The roadmap's P03 gate requires the initial mailing to have "a tested correction or
assisted restart path" before launch, and says to "never mutate a frozen packet or
resubmit when earlier mail is uncertain."  This file drives a real API case to FROZEN,
lets Lob definitively reject the first create (4xx), and proves two things.

``test_rejected_first_mailing_stops_without_charge_and_preserves_workspace_and_packet``
  The truthful stop: ``STOPPED_NO_CHARGE`` with the Lob-rejected stop reason, the card hold
  released and never captured, and then no resubmission when the scheduler runs again.  The
  customer workspace (notes, position, photos, answers, provision selections) and the frozen
  packet plus its approval manifest are unchanged.

``test_assisted_restart_carries_the_customer_into_a_fresh_prefilled_workspace``
  The assisted restart path the stop copy promises ("you can review your saved details and
  try again"): once the first transaction is terminal, ``POST /workspaces`` returns a *fresh*
  workspace carrying the customer's own words, photos and answers but no stale provision
  selections, while a repeated start before submitting replays that same workspace.  A
  corrected intake then creates a distinct case that needs its own authorization, its own
  drafting/adoption and a fresh exact-packet approval, and only then its own single
  successful Lob create.  Case 1, its frozen packet and its approval stay untouched
  (``STOPPED_NO_CHARGE`` remains terminal), and no out-of-flight mail is ever resubmitted.
"""

from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from httpx2 import AsyncClient

from parkingbreaker.appeal.drafting import Draft, DraftSentence
from parkingbreaker.appeal.fakes import FakeDrafter
from parkingbreaker.approval_manifest import ApprovalManifest
from parkingbreaker.case_service import CaseService
from parkingbreaker.case_state import CaseState
from parkingbreaker.lob_fakes import FakeLobClient
from parkingbreaker.lob_submission import LetterEvidence, LobResponse
from parkingbreaker.mail_attempt_store import PreparedMailAttempt
from parkingbreaker.payment_state import PaymentState
from parkingbreaker.transaction_records import WorkspaceRecord
from test_case_service import ALICE, INTAKE, PM, fake_resolution, stripe
from test_effect_dispatch import WORKER, approved, dispatch, payment_fixture
from test_stripe_payment import Transport, fixture

assert fake_resolution
pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("fake_resolution")]
STOP_REASON_KEY = "stop.transaction_failure.lob_rejected"
CORRECTED_STATEMENT = "The meter was broken. I have two photos of the covered sign and the receipt."


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@dataclass(frozen=True)
class StoppedMailing:
    """One case stopped by a definitive Lob rejection, with its pre-stop baselines."""

    client: AsyncClient
    real: CaseService
    case_id: str
    workspace_id: str
    case_token: str
    frozen: PreparedMailAttempt
    manifest: ApprovalManifest
    lob: FakeLobClient
    transport: Transport
    workspace_before: dict[str, Any]
    saved_workspace: WorkspaceRecord


def _fresh_payment_intent(
    status: str = "requires_capture", payment_intent_id: str = "pi_Restart456"
) -> dict[str, Any]:
    """A distinct PaymentIntent payload: one reference may never back two cases."""
    payload: dict[str, Any] = deepcopy(fixture(status))
    payload["id"] = payment_intent_id
    charge = payload.get("latest_charge")
    if isinstance(charge, dict):
        charge["payment_intent"] = payment_intent_id
        charge["id"] = payment_intent_id.replace("pi_", "ch_")
    return payload


async def stopped_by_lob_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> StoppedMailing:
    """Drive a case to FROZEN, then let Lob 4xx the first create and release the hold."""
    lob = FakeLobClient([LobResponse(422, error="provider validation rejected")])
    client, real, case_id = await approved(tmp_path, monkeypatch, lob)
    record = real._intake(case_id)
    assert record.case_token is not None
    token = record.case_token
    workspace_id = record.workspace_token
    frozen = real._store.get(token, 1)
    manifest = real._store.get_approval_manifest(token, 1)
    saved = real._store.get_workspace(workspace_id)
    assert frozen is not None
    assert manifest is not None
    assert saved is not None
    assert (await client.get(f"/cases/{case_id}/status", headers=ALICE)).json()["state"] == "FROZEN"
    workspace_before = (await client.get(f"/workspaces/{workspace_id}", headers=ALICE)).json()
    # One response authorizes; the next two are the pre-mail reads; the last releases.
    payments, transport = stripe(payment_fixture(), payment_fixture(), payment_fixture("canceled"))
    object.__setattr__(real._deps, "payments", payments)
    await dispatch(client)  # pre-mail reads, then the definitive Lob rejection
    await dispatch(client)  # release the authorization hold
    await dispatch(client)  # steady state: the stop is terminal and nothing resubmits
    status = (await client.get(f"/cases/{case_id}/status", headers=ALICE)).json()
    assert status["state"] == "STOPPED_NO_CHARGE"
    assert status["payment_state"] == "RELEASED"
    assert status["stop"]["reason_key"] == STOP_REASON_KEY
    assert len(lob.calls) == 1
    return StoppedMailing(
        client=client,
        real=real,
        case_id=case_id,
        workspace_id=workspace_id,
        case_token=token,
        frozen=frozen,
        manifest=manifest,
        lob=lob,
        transport=transport,
        workspace_before=workspace_before,
        saved_workspace=saved,
    )


@pytest.mark.anyio
async def test_rejected_first_mailing_stops_without_charge_and_preserves_workspace_and_packet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stopped = await stopped_by_lob_rejection(tmp_path, monkeypatch)
    client, real = stopped.client, stopped.real
    try:
        status = (await client.get(f"/cases/{stopped.case_id}/status", headers=ALICE)).json()
        assert status["state"] == "STOPPED_NO_CHARGE"
        assert status["payment_state"] == "RELEASED"
        assert status["stop"] == {
            "kind": "stop",
            "case_id": stopped.case_id,
            "family": "TRANSACTION_FAILURE",
            "hard_stop": None,
            "reason_key": STOP_REASON_KEY,
            "message": (
                "The mailing provider did not accept this letter for mailing. "
                "Nothing was charged. Your work is saved. "
                "You can review your saved details and try again."
            ),
            "no_charge": True,
            "work_preserved": True,
            "official_quote": None,
        }
        assert real.status(stopped.case_id).state is CaseState.STOPPED_NO_CHARGE
        assert real.status(stopped.case_id).payment_state is PaymentState.RELEASED
        # The hold was released, never captured.
        assert len(stopped.transport.calls) == 3
        assert stopped.transport.calls[-1][0] == "post"
        assert stopped.transport.calls[-1][1].endswith("/cancel")
        assert not any(call[1].endswith("/capture") for call in stopped.transport.calls)

        # A later scheduler run does not resubmit the frozen packet.
        await dispatch(client)
        assert len(stopped.lob.calls) == 1
        assert real.status(stopped.case_id).state is CaseState.STOPPED_NO_CHARGE

        # The workspace survives the stop, including the customer words and photos.
        workspace = (await client.get(f"/workspaces/{stopped.workspace_id}", headers=ALICE)).json()
        assert workspace == stopped.workspace_before
        assert workspace["case_id"] == stopped.case_id
        intake = workspace["intake"]
        assert intake["customer_position"]["statement"] == INTAKE["customer_position"]["statement"]
        assert intake["photo_refs"]
        assert intake["contact"]["name"] == INTAKE["contact"]["name"]
        assert (
            intake["contact"]["mailing_address"]["line1"]
            == INTAKE["contact"]["mailing_address"]["line1"]
        )
        saved = real._store.get_workspace(stopped.workspace_id)
        assert saved is not None
        assert saved == stopped.saved_workspace
        assert saved.workspace.notes == stopped.saved_workspace.workspace.notes
        assert saved.workspace.position == INTAKE["customer_position"]["statement"]
        assert saved.workspace.photo_refs == tuple(intake["photo_refs"])
        assert saved.workspace.answers == stopped.saved_workspace.workspace.answers
        assert saved.workspace.provision_selections == frozenset()
        assert saved.workspace.letter_edits == ()

        # The frozen packet and its approval manifest are byte-for-byte untouched.
        packet = real._store.get(stopped.case_token, 1)
        manifest = real._store.get_approval_manifest(stopped.case_token, 1)
        assert packet == stopped.frozen
        assert manifest == stopped.manifest
        packet_sha256 = hashlib.sha256(stopped.frozen.request.pdf).hexdigest()
        assert stopped.manifest.packet_pdf_sha256 == packet_sha256
    finally:
        await client.aclose()


@pytest.mark.anyio
@pytest.mark.anyio
async def test_assisted_restart_carries_the_customer_into_a_fresh_prefilled_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stopped = await stopped_by_lob_rejection(tmp_path, monkeypatch)
    client, real = stopped.client, stopped.real
    try:
        # The stop copy promises a retry; starting again now yields a fresh workspace.
        restarted = (await client.post("/workspaces", headers=ALICE, json={"notes": ""})).json()
        new_workspace = restarted["workspace_id"]
        assert new_workspace != stopped.workspace_id
        before = real._store.get_workspace(stopped.workspace_id)
        after = real._store.get_workspace(new_workspace)
        assert before is not None
        assert after is not None
        assert after.workspace.position == before.workspace.position
        assert after.workspace.photo_refs == before.workspace.photo_refs
        assert after.workspace.answers == before.workspace.answers
        assert after.workspace.provision_selections == frozenset()
        assert after.workspace.letter_edits == ()

        # Repeating the start before any intake replays the same fresh workspace.
        replay = (await client.post("/workspaces", headers=ALICE, json={"notes": ""})).json()
        assert replay["workspace_id"] == new_workspace

        # The corrected intake creates a distinct case in the fresh workspace.
        body = deepcopy(stopped.workspace_before["intake"])
        body["customer_position"]["statement"] = CORRECTED_STATEMENT
        receipt = await client.post(f"/workspaces/{new_workspace}/intake", headers=ALICE, json=body)
        assert receipt.status_code == 200, receipt.text
        new_case = receipt.json()["case_id"]
        assert new_case != stopped.case_id
        assert receipt.json()["workspace_id"] == new_workspace
        assert receipt.json()["state"] == "INTAKE"

        # The stopped case, its frozen packet and its approval manifest are untouched.
        old_status = (await client.get(f"/cases/{stopped.case_id}/status", headers=ALICE)).json()
        assert old_status["state"] == "STOPPED_NO_CHARGE"
        assert real._store.get(stopped.case_token, 1) == stopped.frozen
        assert real._store.get_approval_manifest(stopped.case_token, 1) == stopped.manifest
        assert len(stopped.lob.calls) == 1

        # The fresh case inherits nothing: its own tier and authorization come first.
        assert real.status(new_case).state is CaseState.INTAKE
        tier = await client.put(
            f"/cases/{new_case}/tier", headers=ALICE, json={"tier": "CERTIFIED"}
        )
        assert tier.status_code == 200, tier.text
        payments, _transport = stripe(_fresh_payment_intent())
        object.__setattr__(real._deps, "payments", payments)
        authorized = await client.post(f"/cases/{new_case}/authorization", headers=ALICE, json=PM)
        assert authorized.status_code == 200, authorized.text

        research = await client.post(
            f"/internal/cases/{new_case}/step", headers=WORKER, json={"step_id": "research"}
        )
        assert research.status_code == 200, research.text
        if research.json()["state"] == "NEEDS_CUSTOMER":
            research = await client.post(
                f"/cases/{new_case}/answers",
                headers=ALICE,
                json={"question_id": "VEHICLE_RELATIONSHIP", "answer": "REGISTERED_OWNER"},
            )
        assert research.json()["state"] == "DRAFTING"
        object.__setattr__(
            real._deps,
            "drafter",
            FakeDrafter(
                lambda *_: Draft(
                    (DraftSentence(CORRECTED_STATEMENT, (f"note:0-{len(CORRECTED_STATEMENT)}",)),)
                )
            ),
        )
        drafted = await client.post(
            f"/internal/cases/{new_case}/step", headers=WORKER, json={"step_id": "draft"}
        )
        assert drafted.json()["state"] == "AWAITING_ADOPTION"
        draft = (await client.get(f"/cases/{new_case}/draft", headers=ALICE)).json()
        adoption = await client.post(
            f"/cases/{new_case}/adoption",
            headers=ALICE,
            json={"text": draft["text"], "text_sha256": draft["text_sha256"]},
        )
        assert adoption.status_code == 200, adoption.text
        packet = await client.post(
            f"/cases/{new_case}/packet-preview",
            headers=ALICE,
            json={"adopted_sha256": adoption.json()["adopted_sha256"]},
        )
        assert packet.status_code == 200, packet.text
        approved_new = await client.post(
            f"/cases/{new_case}/approval",
            headers=ALICE,
            json={
                "adopted_sha256": adoption.json()["adopted_sha256"],
                "packet_sha256": packet.json()["packet_sha256"],
                "envelope_sha256": packet.json()["envelope_sha256"],
            },
        )
        assert approved_new.json()["state"] == "FROZEN"
        new_record = real._intake(new_case)
        assert new_record.case_token is not None
        assert new_record.case_token != stopped.case_token
        assert real._store.get_approval_manifest(new_record.case_token, 1) is not None
        assert len(stopped.lob.calls) == 1  # still nothing mailed for the fresh case

        # Its own single successful letter, captured only after Lob accepts.
        stopped.lob.script.append(
            LobResponse(
                200,
                "ltr_restart",
                tracking_number="940000002",
                evidence=LetterEvidence.PRODUCTION,
            )
        )
        payments, _transport = stripe(
            _fresh_payment_intent(),
            _fresh_payment_intent(),
            _fresh_payment_intent("succeeded"),
        )
        object.__setattr__(real._deps, "payments", payments)
        stepped = await client.post(
            f"/internal/cases/{new_case}/step", headers=WORKER, json={"step_id": "dispatch"}
        )
        assert stepped.json()["state"] == "IN_TRANSIT"
        await dispatch(client)
        status = (await client.get(f"/cases/{new_case}/status", headers=ALICE)).json()
        assert (status["state"], status["payment_state"]) == ("IN_TRANSIT", "CAPTURED")
        assert len(stopped.lob.calls) == 2
        # Capture applies to the fresh case only; the stopped case stays released.
        assert real.status(stopped.case_id).payment_state is PaymentState.RELEASED
    finally:
        await client.aclose()
