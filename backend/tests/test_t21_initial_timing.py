"""Synthetic J15/J31/J32 through real research, customer API and durable dispatch.

Provider I/O is fake. These cases qualify the existing timing assumptions, not a
provider delivery promise or real-source/model accuracy.
"""

from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx2 import ASGITransport, AsyncClient

import test_t21_payment_scenarios as base
from parkingbreaker.api import schemas as s
from parkingbreaker.api.app import create_app
from parkingbreaker.appeal.drafting import Draft, DraftSentence
from parkingbreaker.appeal.fakes import FakeDrafter
from parkingbreaker.case_service import CaseService, ResearchDeps
from parkingbreaker.lob_fakes import FakeLobClient
from parkingbreaker.lob_run import LOB_DAILY_CUTOFF, pacific, production_date
from parkingbreaker.lob_submission import Accepted, LetterEvidence, LobResponse, Rejected, Uncertain
from parkingbreaker.mail_config import LOB_CANCELLATION_WINDOW, MailClass
from parkingbreaker.mail_timing import (
    DeadlineRule,
    arrival_date,
    latest_safe_send_date,
    mailing_date,
)
from parkingbreaker.research.extractor import LlmExtractor
from parkingbreaker.research.fakes import FakeFetcher, FakeLlmClient, FakeSearcher, html_response
from parkingbreaker.research.questions import FactKey
from parkingbreaker.research.sources import html_to_text
from parkingbreaker.research_records import decode_answers, decode_research
from test_case_service import ALICE, INTAKE, PM, WORKER, Clock, service, stripe, submit
from test_case_service import anyio_backend as anyio_backend
from test_case_service import fake_resolution as fake_resolution
from test_stripe_payment import fixture

pytestmark = pytest.mark.anyio
PERIOD = "Contest within 21 calendar days of the citation issue date."
RECEIPT = "Your mailed contest must be received by the deadline."


@dataclass(frozen=True)
class Boundary:
    name: str
    issue: date
    days: int
    rule: DeadlineRule
    cutoff: bool
    deadline: date
    latest: date


BOUNDARIES = (
    Boundary(
        "J31-receipt",
        date(2026, 10, 1),
        21,
        DeadlineRule.RECEIPT,
        False,
        date(2026, 10, 22),
        date(2026, 10, 8),
    ),
    Boundary(
        "J32-postmark",
        date(2026, 10, 1),
        12,
        DeadlineRule.POSTMARK,
        False,
        date(2026, 10, 13),
        date(2026, 10, 8),
    ),
    Boundary(
        "J15-cutoff",
        date(2026, 10, 22),
        21,
        DeadlineRule.RECEIPT,
        True,
        date(2026, 11, 10),
        date(2026, 10, 28),
    ),
)


def timing_inputs(boundary: Boundary) -> tuple[FakeFetcher, FakeLlmClient]:
    fetcher, model = base._inputs("first-level review", "Payment plans are available.", None)
    untimed_period = f"Contest within {boundary.days} calendar days of the citation issue date."
    period = untimed_period
    if boundary.cutoff:
        period += (
            f" Appeals must be received before 5:00 PM on the {boundary.days}st day "
            "after the citation issue date."
        )
    rule = (
        "Appeals must be postmarked by the deadline."
        if boundary.rule is DeadlineRule.POSTMARK
        else RECEIPT
    )
    html = fetcher.responses[base.PROCEDURE].body.decode().replace(PERIOD, period)
    html = html.replace(RECEIPT, rule)
    text, sha = html_to_text(html), hashlib.sha256(html.encode()).hexdigest()
    reply = copy.deepcopy(model.reply)
    assert isinstance(reply, dict)
    findings = reply["findings"]
    assert isinstance(findings, list)
    for finding in findings:
        assert isinstance(finding, dict)
        finding["source_sha256s"] = [sha]
        if finding["verbatim_quote"] == PERIOD:
            finding["verbatim_quote"] = (
                period if finding["key"] == FactKey.DEADLINE_DAYS.value else untimed_period
            )
        if finding["key"] == FactKey.DEADLINE_DAYS.value:
            finding["value"] = (
                {"days": boundary.days, "unit": "CALENDAR", "time_cutoff": True}
                if boundary.cutoff
                else boundary.days
            )
        if finding["key"] == FactKey.DEADLINE_RULE.value:
            finding["value"], finding["verbatim_quote"] = boundary.rule.name, rule
        assessment = finding.get("procedure_assessment")
        if isinstance(assessment, dict):
            finding["verbatim_quote"] = text
            assessment["source_sha256"], assessment["procedure_text"] = sha, text
            assessment["when_quote"] = period
    fetcher.responses = {**fetcher.responses, base.PROCEDURE: html_response(base.PROCEDURE, html)}
    return fetcher, FakeLlmClient(reply)


def last_safe_moment(send: date) -> datetime:
    """One minute before Lob's effective cutoff (10 a.m. Pacific minus the window)."""
    local_cutoff = datetime.combine(send, LOB_DAILY_CUTOFF) - LOB_CANCELLATION_WINDOW
    for offset in (7, 8):  # PDT, then PST
        moment = (local_cutoff + timedelta(hours=offset, minutes=-1)).replace(tzinfo=UTC)
        if pacific(moment) == local_cutoff - timedelta(minutes=1):
            assert production_date(moment) == send
            assert production_date(moment + timedelta(minutes=1)) > send
            return moment
    raise AssertionError("no Pacific offset matched")


async def timing_case(
    tmp_path: Path,
    boundary: Boundary,
    send: date,
    *,
    accepted: bool = False,
    first_preflight: bool = False,
) -> tuple[base.Scenario, Clock, FakeLobClient]:
    clock = Clock()
    # The last safe moment: one minute before Lob's effective cutoff on the send date.
    clock.now = last_safe_moment(send)
    auth = fixture(expiry=int((clock.now + timedelta(days=6)).timestamp()))
    charge = auth["latest_charge"]
    assert isinstance(charge, dict)
    charge["created"] = int(clock.now.timestamp())
    final = fixture(
        "succeeded" if accepted else "canceled",
        expiry=int((clock.now + timedelta(days=6)).timestamp()),
    )
    final_charge = final["latest_charge"]
    assert isinstance(final_charge, dict)
    final_charge["created"] = int(clock.now.timestamp())
    checks = [copy.deepcopy(auth)] if accepted or first_preflight else []
    payments, transport = stripe(auth, *checks, copy.deepcopy(auth), final)
    database = tmp_path / "timing.sqlite"
    real = service(database, payments, clock)
    fetcher, model = timing_inputs(boundary)
    statement = INTAKE["customer_position"]["statement"]
    lob = FakeLobClient([LobResponse(200, "ltr_timing", evidence=LetterEvidence.PRODUCTION)])
    real = CaseService(
        replace(
            real._deps,
            research=ResearchDeps(
                fetcher,
                LlmExtractor(model),
                FakeSearcher(lambda _: ()),
                lambda _: None,
                known_records=(
                    {
                        "status": "active",
                        "online_appeal_url": base.POLICY,
                        "mail_acceptance_policy": {"official_source_url": base.PROCEDURE},
                    },
                ),
            ),
            drafter=FakeDrafter(
                lambda *_: Draft((DraftSentence(statement, (f"note:0-{len(statement)}",)),))
            ),
            lob=lob,
        )
    )
    body = copy.deepcopy(INTAKE)
    body["ticket"].update(issue_date=boundary.issue.isoformat(), stage="first-level review")
    async with AsyncClient(
        transport=ASGITransport(app=create_app(real)), base_url="http://timing.test"
    ) as client:
        _, receipt = await submit(client, body)
        case = receipt["case_id"]
        chosen = await client.put(f"/cases/{case}/tier", headers=ALICE, json={"tier": "CERTIFIED"})
        assert chosen.status_code == 200, chosen.text
        authorized = await client.post(f"/cases/{case}/authorization", headers=ALICE, json=PM)
        assert authorized.status_code == 200, authorized.text
        response = await client.post(
            f"/internal/cases/{case}/step", headers=WORKER, json={"step_id": "research"}
        )
        assert response.status_code == 200, response.text
    current = base.Scenario(real, case, model, transport, database)
    saved = real._store.get_research(case)
    assert saved is not None
    result = decode_research(saved.research_json)
    resolution = result.resolve(decode_answers(saved.answers_json))
    assert resolution.ready
    assert resolution.deadline is not None
    assert resolution.deadline.deadline == boundary.deadline
    assert resolution.counting is not None
    assert resolution.counting.rule.name == boundary.rule.name
    assert (
        latest_safe_send_date(boundary.deadline, boundary.rule, MailClass.CERTIFIED)
        == boundary.latest
    )
    source = next(page for page in result.sources if page.final_url == base.PROCEDURE)
    assert source.sha256 == hashlib.sha256(fetcher.responses[base.PROCEDURE].body).hexdigest()
    token = real._intake(case).case_token
    assert token is not None
    transaction = real._store.get_case(token)
    assert transaction is not None
    assert clock.now < transaction.authorization_expires_at - timedelta(days=5)
    assert not lob.calls
    return current, clock, lob


def adopt(current: base.Scenario) -> str:
    real, case = current.real, current.case
    real.step(case, s.WorkerStep(step_id="draft"))
    draft = real.draft(case)
    receipt = real.adopt_text(case, s.AdoptText(text=draft.text, text_sha256=draft.text_sha256))
    return receipt.adopted_sha256


@pytest.mark.parametrize("boundary", BOUNDARIES, ids=lambda b: b.name)
@pytest.mark.parametrize("late", [False, True], ids=["safe", "unsafe"])
async def test_initial_timing_boundary(tmp_path: Path, boundary: Boundary, late: bool) -> None:
    send = boundary.latest + timedelta(days=int(late))
    current, _, lob = await timing_case(tmp_path, boundary, send, accepted=not late)
    try:
        current.reopen()
        if late:
            status = current.real.status(current.case)
            assert status.state == "STOPPED_NO_CHARGE"
            for _ in range(2):
                current.real.dispatch(current.case)
            assert current.real.status(current.case).payment_state == "RELEASED"
            assert status.stop is not None
            assert status.stop.reason_key == "stop.process_incompatible.not_feasible"
            assert not lob.calls
        else:
            assert current.real.status(current.case).state == "DRAFTING"
            decisive = (
                mailing_date(send, MailClass.CERTIFIED)
                if boundary.rule is DeadlineRule.POSTMARK
                else arrival_date(send, MailClass.CERTIFIED)
            )
            assert decisive == boundary.deadline
            adopted = adopt(current)
            from test_case_service import approve_with_preview

            current.real.approve(
                current.case, approve_with_preview(current.real, current.case, adopted)
            )
            current.reopen()
            current.real.dispatch(current.case)
            assert len(lob.calls) == 1
            assert current.real.status(current.case).state == "IN_TRANSIT"
        current.real.dispatch(current.case)
        captures = [path for _, path, *_ in current.transport.calls if path.endswith("/capture")]
        assert len(captures) == (0 if late else 1)
        if not late:
            assert current.real.status(current.case).payment_state == "CAPTURED"
    finally:
        current.real._store.close()


@pytest.mark.parametrize("boundary", BOUNDARIES, ids=lambda b: b.name)
@pytest.mark.parametrize("stage", ["preview", "approval", "dispatch"])
async def test_timing_is_rechecked_after_customer_delay(
    tmp_path: Path, boundary: Boundary, stage: str
) -> None:
    current, clock, lob = await timing_case(tmp_path, boundary, boundary.latest)
    try:
        adopted = adopt(current)
        body = {"adopted_sha256": adopted}
        preview = current.real.packet_preview(
            current.case, s.PacketPreviewRequest(adopted_sha256=adopted)
        )
        if stage != "preview":
            body.update(
                packet_sha256=preview.packet_sha256, envelope_sha256=preview.envelope_sha256
            )
        if stage == "dispatch":
            current.real.approve(
                current.case,
                s.Approve(
                    adopted_sha256=adopted,
                    packet_sha256=preview.packet_sha256,
                    envelope_sha256=preview.envelope_sha256,
                ),
            )
        clock.now += timedelta(minutes=1)  # the first unsafe moment: the cutoff
        current.reopen()
        async with AsyncClient(
            transport=ASGITransport(app=create_app(current.real)), base_url="http://timing.test"
        ) as client:
            if stage == "dispatch":
                response = await client.post(
                    f"/internal/cases/{current.case}/step",
                    headers=WORKER,
                    json={"step_id": "dispatch"},
                )
                assert response.status_code == 200, response.text
            else:
                route = "packet-preview" if stage == "preview" else "approval"
                response = await client.post(
                    f"/cases/{current.case}/{route}", headers=ALICE, json=body
                )
                assert response.status_code == 409, response.text
        assert not lob.calls
        stopped = current.real.status(current.case)
        assert stopped.state == "STOPPED_NO_CHARGE"
        assert stopped.stop is not None
        assert stopped.stop.reason_key == "stop.process_incompatible.not_feasible"
        assert all("capture" not in path for _, path, *_ in current.transport.calls)
        current.reopen()
        current.real.step(current.case, s.WorkerStep(step_id="research"))
        current.real.dispatch(current.case)
        current.real.dispatch(current.case)
        assert current.real.status(current.case).payment_state == "RELEASED"
        assert not lob.calls
    finally:
        current.real._store.close()


@pytest.mark.parametrize("boundary", BOUNDARIES, ids=lambda b: b.name)
async def test_midnight_after_committed_first_intent_stops_before_provider_call(
    tmp_path: Path, boundary: Boundary, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_case_service import approve_with_preview

    current, clock, lob = await timing_case(
        tmp_path, boundary, boundary.latest, first_preflight=True
    )
    try:
        adopted = adopt(current)
        current.real.approve(
            current.case, approve_with_preview(current.real, current.case, adopted)
        )
        original_claim = current.real._store.claim_submission

        def cross_midnight(*args: Any, **kwargs: Any) -> object:
            claimed = original_claim(*args, **kwargs)
            clock.now += timedelta(minutes=1)  # the first unsafe moment: the cutoff
            return claimed

        monkeypatch.setattr(current.real._store, "claim_submission", cross_midnight)
        current.real.dispatch(current.case)
        assert not lob.calls
        token = current.real._intake(current.case).case_token
        assert token is not None
        cursor = current.real._store.submission_cursor(token, 1)
        assert cursor is not None
        assert isinstance(cursor.outcome, Rejected)
        assert current.real.status(current.case).state == "STOPPED_NO_CHARGE"
        stop = current.real.status(current.case).stop
        assert stop is not None
        assert stop.reason_key == "stop.process_incompatible.not_feasible"
        assert any(
            item.event_name == "TimingRefused"
            for item in current.real._store.transition_history(token)
        )
        current.reopen()
        current.real.dispatch(current.case)
        assert current.real.status(current.case).payment_state == "RELEASED"
        assert all("capture" not in path for _, path, *_ in current.transport.calls)
        assert not lob.calls
    finally:
        current.real._store.close()


@pytest.mark.parametrize("rendered", [False, True], ids=["lost-response", "rendered"])
async def test_late_retry_preserves_possible_original_until_authoritative_evidence(
    tmp_path: Path,
    rendered: bool,
) -> None:
    from test_case_service import approve_with_preview

    boundary = BOUNDARIES[0]
    current, clock, lob = await timing_case(tmp_path, boundary, boundary.latest, accepted=True)
    lob.script = [
        LobResponse(200, "ltr_rendered", evidence=LetterEvidence.CREATED)
        if rendered
        else LobResponse(None, error="synthetic lost response")
    ]
    try:
        adopted = adopt(current)
        current.real.approve(
            current.case, approve_with_preview(current.real, current.case, adopted)
        )
        current.real.dispatch(current.case)
        assert len(lob.calls) == 1
        token = current.real._intake(current.case).case_token
        assert token is not None
        cursor = current.real._store.submission_cursor(token, 1)
        assert cursor is not None
        assert isinstance(cursor.outcome, Uncertain)
        first_sent = cursor.first_sent_at
        clock.now += timedelta(minutes=1)  # the first unsafe moment: the cutoff
        current.reopen()
        current.real.dispatch(current.case)
        assert len(lob.calls) == 1
        assert current.real.status(current.case).state == "SUBMITTING"
        assert current.real.status(current.case).payment_state == "AUTHORIZED"
        assert all(
            "capture" not in path and "cancel" not in path
            for _, path, *_ in current.transport.calls
        )
        request = lob.calls[0][1]
        lob.letters[request.submission_token] = "ltr_rendered"
        lob.evidence_by_token[request.submission_token] = LetterEvidence.PRODUCTION
        clock.now = first_sent + timedelta(hours=25)
        current.reopen()
        current.real.dispatch(current.case)
        current.real.dispatch(current.case)
        assert len(lob.calls) == 1
        assert lob.lookups == [request.submission_token] * (2 if rendered else 1)
        final = current.real._store.submission_cursor(token, 1)
        assert final is not None
        assert isinstance(final.outcome, Accepted)
        assert final.outcome.letter_id == "ltr_rendered"
        assert final.first_sent_at == first_sent
        assert current.real.status(current.case).payment_state == "CAPTURED"
    finally:
        current.real._store.close()
