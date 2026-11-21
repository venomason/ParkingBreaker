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

``test_assisted_restart_starts_a_new_approved_transaction_in_the_same_workspace``
  The assisted restart path: after that stop, a corrected
  ``POST /workspaces/{workspace_id}/intake`` starts a *distinct* new case in the same
  workspace.  The new case is not authorized, mailed or approved by inheritance: it needs its
  own authorization, fresh drafting/adoption and a fresh exact-packet approval, and only then
  its own single successful Lob create.  Case 1, its frozen packet and its approval stay
  untouched (``STOPPED_NO_CHARGE`` remains terminal).

The restart half currently fails at the second-intake call; that is the P03 capability gap
this file reports honestly rather than faking.
"""

from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from httpx2 import AsyncClient

from parkingbreaker.api import schemas as s
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
from test_stripe_payment import Transport

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
@pytest.mark.xfail(
    strict=True,
    reason=(
        "P03 capability gap: the assisted restart path is not implemented. The store keeps one "
        "intake per workspace (`intake_already_submitted_create_new_workspace`) and one workspace "
        "per owner (`appeal_already_started`), so a stopped appeal cannot be restarted in place or "
        "moved to a fresh workspace. Needs an owner design decision before implementation."
    ),
)
async def test_assisted_restart_starts_a_new_approved_transaction_in_the_same_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stopped = await stopped_by_lob_rejection(tmp_path, monkeypatch)
    client, real = stopped.client, stopped.real
    try:
        # The customer corrects their statement and submits again in the same workspace.
        restart_body = deepcopy(stopped.workspace_before["intake"])
        restart_body["customer_position"]["statement"] = CORRECTED_STATEMENT
        restart = await client.post(
            f"/workspaces/{stopped.workspace_id}/intake",
            headers=ALICE,
            json=restart_body,
        )
        assert restart.status_code == 200, (
            "P03 capability gap: the assisted restart path does not exist. "
            f"POST /workspaces/{stopped.workspace_id}/intake was refused after the first "
            f"mailing stopped on a Lob rejection: {restart.status_code} {restart.text}"
        )
        receipt = restart.json()
        assert receipt["workspace_id"] == stopped.workspace_id
        new_case = receipt["case_id"]
        assert new_case != stopped.case_id
        assert receipt["state"] == "INTAKE"

        # Restarting leaves the stopped case, its frozen packet and approval untouched.
        old_status = (await client.get(f"/cases/{stopped.case_id}/status", headers=ALICE)).json()
        assert old_status["state"] == "STOPPED_NO_CHARGE"
        assert old_status["stop"]["reason_key"] == STOP_REASON_KEY
        assert real._store.get(stopped.case_token, 1) == stopped.frozen
        assert real._store.get_approval_manifest(stopped.case_token, 1) == stopped.manifest
        new_record = real._intake(new_case)
        assert new_record.workspace_token == stopped.workspace_id
        assert new_record.case_token is not None
        new_token = new_record.case_token
        assert new_token != stopped.case_token

        # The new transaction inherits no approval, no authorization and no mailing.
        new_status = (await client.get(f"/cases/{new_case}/status", headers=ALICE)).json()
        assert new_status["state"] == "INTAKE"
        assert new_status["payment_state"] is None
        assert new_status["stop"] is None
        await dispatch(client)
        assert len(stopped.lob.calls) == 1  # only case 1's rejected create
        new_status = (await client.get(f"/cases/{new_case}/status", headers=ALICE)).json()
        assert new_status["state"] == "INTAKE"

        # The new case needs its own tier choice and its own card authorization.
        payments, transport = stripe(
            payment_fixture(),
            payment_fixture(),
            payment_fixture(),
            payment_fixture("succeeded"),
            payment_fixture("succeeded"),
        )
        object.__setattr__(real._deps, "payments", payments)
        chosen = await client.put(
            f"/cases/{new_case}/tier", headers=ALICE, json={"tier": "CERTIFIED"}
        )
        assert chosen.status_code == 200, chosen.text
        authorization = await client.post(
            f"/cases/{new_case}/authorization", headers=ALICE, json=PM
        )
        assert authorization.status_code == 200, authorization.text
        assert authorization.json()["fake"] is False
        assert transport.calls[0][:2] == ("post", "/v1/payment_intents")
        new_status = (await client.get(f"/cases/{new_case}/status", headers=ALICE)).json()
        assert new_status["state"] == "RESEARCHING"
        assert new_status["payment_state"] == "AUTHORIZED"

        # The corrected statement is drafted, adopted and approved as a new exact packet.
        object.__setattr__(
            real._deps,
            "drafter",
            FakeDrafter(
                lambda *_: Draft(
                    (
                        DraftSentence(
                            CORRECTED_STATEMENT,
                            (f"note:0-{len(CORRECTED_STATEMENT)}",),
                        ),
                    )
                )
            ),
        )
        research = await client.post(
            f"/internal/cases/{new_case}/step",
            headers=WORKER,
            json={"step_id": "research"},
        )
        assert research.status_code == 200, research.text
        if research.json()["state"] == "NEEDS_CUSTOMER":
            answered = await client.post(
                f"/cases/{new_case}/answers",
                headers=ALICE,
                json={"question_id": "VEHICLE_RELATIONSHIP", "answer": "REGISTERED_OWNER"},
            )
            assert answered.status_code == 200, answered.text
            research = answered
        assert research.json()["state"] == "DRAFTING"
        drafted = await client.post(
            f"/internal/cases/{new_case}/step",
            headers=WORKER,
            json={"step_id": "draft"},
        )
        assert drafted.status_code == 200, drafted.text
        assert drafted.json()["state"] == "AWAITING_ADOPTION"
        draft = (await client.get(f"/cases/{new_case}/draft", headers=ALICE)).json()
        adoption = await client.post(
            f"/cases/{new_case}/adoption",
            headers=ALICE,
            json={"text": draft["text"], "text_sha256": draft["text_sha256"]},
        )
        assert adoption.status_code == 200, adoption.text
        preview = await client.post(
            f"/cases/{new_case}/packet-preview",
            headers=ALICE,
            json={"adopted_sha256": adoption.json()["adopted_sha256"]},
        )
        assert preview.status_code == 200, preview.text
        approval = await client.post(
            f"/cases/{new_case}/approval",
            headers=ALICE,
            json={
                "adopted_sha256": adoption.json()["adopted_sha256"],
                "packet_sha256": preview.json()["packet_sha256"],
                "envelope_sha256": preview.json()["envelope_sha256"],
            },
        )
        assert approval.json()["state"] == "FROZEN"
        new_frozen = real._store.get(new_token, 1)
        new_manifest = real._store.get_approval_manifest(new_token, 1)
        assert new_frozen is not None
        assert new_manifest is not None
        assert hashlib.sha256(new_frozen.request.pdf).hexdigest() == preview.json()["packet_sha256"]
        assert new_manifest.packet_pdf_sha256 == preview.json()["packet_sha256"]
        assert len(stopped.lob.calls) == 1  # approval alone never mails

        # Only the new frozen packet is mailed, once, after its own explicit dispatch.
        stopped.lob.script.append(
            LobResponse(200, "ltr_restarted", evidence=LetterEvidence.PRODUCTION)
        )
        await dispatch(client)  # submit the new frozen packet
        await dispatch(client)  # capture
        new_status = (await client.get(f"/cases/{new_case}/status", headers=ALICE)).json()
        assert new_status["state"] == "IN_TRANSIT"
        assert new_status["payment_state"] == "CAPTURED"
        assert len(stopped.lob.calls) == 2
        assert stopped.lob.calls[0][0] != stopped.lob.calls[1][0]
        assert stopped.lob.calls[1][1].pdf == new_frozen.request.pdf
        assert list(stopped.lob.letters) == [new_frozen.request.submission_token]
        assert stopped.frozen.request.submission_token not in stopped.lob.letters
        assert transport.calls[-1][1].endswith("/capture")
        # Case 1 was never mutated or resubmitted by any of this.
        assert real._store.get(stopped.case_token, 1) == stopped.frozen
        assert real._store.get_approval_manifest(stopped.case_token, 1) == stopped.manifest
        old_status = (await client.get(f"/cases/{stopped.case_id}/status", headers=ALICE)).json()
        assert old_status["state"] == "STOPPED_NO_CHARGE"
        assert old_status["payment_state"] == "RELEASED"
        assert old_status["stop"]["reason_key"] == STOP_REASON_KEY
        assert new_status["selected_tier"] == s.MailTier.CERTIFIED
    finally:
        await client.aclose()
