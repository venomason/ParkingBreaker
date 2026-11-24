"""Municipal-route decisions protect the frozen packet and original provider intent."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from parkingbreaker import prehold, recovery
from parkingbreaker.address_policy import address_fingerprint
from parkingbreaker.agencies import MailingAddress
from parkingbreaker.api import schemas as s
from parkingbreaker.case_service import CaseService
from parkingbreaker.case_state import CaseState
from parkingbreaker.lob_fakes import FakeLobClient
from parkingbreaker.lob_submission import (
    Accepted,
    LetterEvidence,
    LobResponse,
    MetadataLookup,
    Rejected,
    Uncertain,
)
from parkingbreaker.payment_records import PaymentClient
from parkingbreaker.payment_state import PaymentState
from parkingbreaker.route_checks import MAX_ROUTE_CHECK_AGE
from parkingbreaker.route_registry import RouteScope
from parkingbreaker.submission_coordinator import run_submission_step
from parkingbreaker.transaction_store import GATE_REFUSED_REASON, SqliteTransactionStore
from samples import SAN_FRANCISCO
from test_beta_dispatch_timing import ForbiddenPayments
from test_beta_recovery import _beta_ready
from test_case_service import INTAKE, Clock, Resolved, approve_with_preview, drafting_case, stripe
from test_effect_dispatch import payment_fixture
from test_t30_remedy import assert_full_refund, mailed_case

pytestmark = pytest.mark.anyio
SCOPE = RouteScope("CA:sandiego.gov", "first-level review", "certified")
OTHER_SCOPE = RouteScope("CA:separate.gov", SCOPE.stage, SCOPE.mail_class)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def resolved_issuer_matches_saved_research(monkeypatch: pytest.MonkeyPatch) -> None:
    # The shared research fixture concerns San Diego; make its resolved intake agree.
    issuer = replace(SAN_FRANCISCO, id=SCOPE.issuer, official_domains=("sandiego.gov",))
    monkeypatch.setattr(prehold, "resolve_ticket_issuer", lambda *_a, **_k: Resolved(issuer))
    monkeypatch.setitem(INTAKE["ticket"], "stage", "Initial Review")


def change_route(
    store: SqliteTransactionStore,
    scope: RouteScope,
    revision: int,
    now: datetime,
    *,
    address: MailingAddress | None = None,
    status: str = "verified",
    validity: timedelta = timedelta(hours=3),
) -> None:
    store.change_route(
        scope=scope,
        expected_revision=revision,
        command_id=f"{scope.issuer}-revision-{revision + 1}",
        status=status,
        address_fingerprint=None if address is None else address_fingerprint(address),
        source_url=f"https://{scope.issuer.split(':')[-1]}/parking/contests",
        source_quote="Synthetic official mailing instruction.",
        evidence_sha256="a" * 64,
        valid_until=now + validity if status == "verified" else None,
        now=now,
    )


@dataclass
class ReadyCase:
    service: CaseService
    case_id: str
    token: str
    recipient: MailingAddress
    clock: Clock

    @property
    def store(self) -> SqliteTransactionStore:
        return self.service._store

    def approve(self) -> None:
        draft = self.service.draft(self.case_id)
        self.service.adopt_text(
            self.case_id, s.AdoptText(text=draft.text, text_sha256=draft.text_sha256)
        )
        assert (
            self.service.approve(
                self.case_id, approve_with_preview(self.service, self.case_id, draft.text_sha256)
            ).state
            is CaseState.FROZEN
        )

    def restart(self) -> None:
        path = self.store.path
        self.store.close()
        self.service = CaseService(replace(self.service._deps, store=SqliteTransactionStore(path)))

    def dispatch(self) -> None:
        self.service.dispatch(self.case_id)


@asynccontextmanager
async def ready_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, free_beta: bool = True
) -> AsyncIterator[ReadyCase]:
    client, service, case_id = await drafting_case(tmp_path, monkeypatch, free_beta=free_beta)
    ready = None
    try:
        service.step(case_id, s.WorkerStep(step_id="draft"))
        record = service._intake(case_id)
        decision = service._store.latest_destination_decision(case_id, record.owner)
        assert record.case_token is not None
        assert decision is not None
        clock = Clock()
        object.__setattr__(service._deps, "clock", clock)
        if free_beta:
            object.__setattr__(service._deps, "payments", cast(PaymentClient, ForbiddenPayments()))
        ready = ReadyCase(service, case_id, record.case_token, decision.decision.address, clock)
        yield ready
    finally:
        await client.aclose()
        (service._store if ready is None else ready.store).close()


@pytest.mark.parametrize(
    "scenario",
    [
        "current",
        "revoked",
        "revoked_paid",
        "suspended",
        "replaced",
        "reverified",
        "expired",
        "stale_check",
        "new_enrollment",
        "legacy_unbound",
        "address_mismatch",
        "unrelated",
        "issuer_alias",
        "independent_scope",
    ],
)
async def test_approval_binding_controls_new_create_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    paid = scenario == "revoked_paid"
    async with ready_case(tmp_path, monkeypatch, free_beta=not paid) as ready:
        now = ready.clock()
        other_destination = replace(ready.recipient, line1="987 Separate Example Avenue")
        if scenario in {"unrelated", "issuer_alias", "independent_scope"}:
            change_route(
                ready.store,
                OTHER_SCOPE,
                0,
                now,
                address=other_destination if scenario == "unrelated" else ready.recipient,
            )
            change_route(ready.store, OTHER_SCOPE, 1, now, status="revoked")
        enrolled = scenario not in {"new_enrollment", "unrelated", "issuer_alias"}
        if enrolled:
            change_route(
                ready.store,
                SCOPE,
                0,
                now,
                address=other_destination if scenario == "address_mismatch" else ready.recipient,
                validity=timedelta(minutes=10) if scenario == "expired" else timedelta(hours=3),
            )
        with monkeypatch.context() as approval_patch:
            if scenario == "legacy_unbound":
                # Reproduce an approval stored before freeze began writing route bindings.
                approval_patch.setattr(ready.store, "_bind_prepared_route", lambda *_: None)
            ready.approve()
        prepared = ready.store.get(ready.token, 1)
        manifest = ready.store.get_approval_manifest(ready.token, 1)
        assert prepared is not None
        assert manifest is not None
        bound = None if scenario == "legacy_unbound" else (SCOPE, int(enrolled))
        assert ready.store.bound_route(ready.token, 1) == bound
        if scenario in {"revoked", "revoked_paid", "suspended"}:
            change_route(ready.store, SCOPE, 1, now, status=scenario.removesuffix("_paid"))
        elif scenario in {"replaced", "reverified"}:
            change_route(
                ready.store,
                SCOPE,
                1,
                now,
                address=other_destination if scenario == "replaced" else ready.recipient,
            )
        elif scenario == "new_enrollment":
            change_route(ready.store, SCOPE, 0, now, address=ready.recipient)
        elif scenario == "expired":
            ready.clock.now += timedelta(minutes=10)
        elif scenario == "stale_check":
            ready.clock.now += MAX_ROUTE_CHECK_AGE
        route_expected_to_send = scenario in {"current", "unrelated", "independent_scope"}
        lob = FakeLobClient([LobResponse(200, "ltr_original", evidence=LetterEvidence.PRODUCTION)])
        object.__setattr__(ready.service._deps, "lob", lob)
        payments, transport = stripe(
            payment_fixture(), payment_fixture(), payment_fixture("canceled")
        )
        if paid:
            object.__setattr__(ready.service._deps, "payments", payments)
        ready.restart()
        for _ in range(3):
            ready.dispatch()
        status = ready.service.status(ready.case_id)
        assert status.payment_state is (PaymentState.RELEASED if paid else PaymentState.NO_CHARGE)
        assert sum(call[1].endswith("/cancel") for call in transport.calls) == int(paid)
        assert not any(call[1].endswith("/capture") for call in transport.calls)
        assert ready.store.bound_route(ready.token, 1) == bound
        assert ready.store.get(ready.token, 1) == prepared
        if route_expected_to_send:
            assert lob.calls == [(prepared.key, prepared.request)]
            assert status.state is CaseState.IN_TRANSIT
        else:
            assert not lob.calls
            assert not lob.lookups
            assert status.state is CaseState.STOPPED_NO_CHARGE
            assert status.stop is not None
            assert status.stop.reason_key == "stop.transaction_failure.destination_not_confirmed"
            assert ready.store.get_approval_manifest(ready.token, 1) == manifest
            cursor = ready.store.submission_cursor(ready.token, 1)
            assert cursor is not None
            assert cursor.outcome == Rejected(422, GATE_REFUSED_REASON)


async def test_route_expiry_after_timing_guard_blocks_physical_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with ready_case(tmp_path, monkeypatch) as ready:
        validity = timedelta(seconds=1)
        change_route(
            ready.store, SCOPE, 0, ready.clock(), address=ready.recipient, validity=validity
        )
        ready.approve()
        lob = FakeLobClient()
        object.__setattr__(ready.service._deps, "lob", lob)
        feasible = ready.service._mailing_deadline_feasible

        def slow_timing_guard(token: str, now: datetime) -> bool:
            result = feasible(token, now)
            case = ready.store.get_case(token)
            if case is not None and case.letter_state is CaseState.SUBMITTING:
                ready.clock.now += validity
            return result

        monkeypatch.setattr(ready.service, "_mailing_deadline_feasible", slow_timing_guard)
        ready.dispatch()
        assert not lob.calls
        assert ready.service.status(ready.case_id).state is CaseState.STOPPED_NO_CHARGE


def test_legacy_case_with_unknown_stage_of_enrolled_issuer_cannot_create(tmp_path: Path) -> None:
    data, _, case, packet = _beta_ready(tmp_path)
    scope = RouteScope("CA:issuer-1.gov", "administrative hearing", "certified")
    lob = FakeLobClient()
    with SqliteTransactionStore(data / recovery.DATABASE) as store:
        assert store.bound_route(case.case_token, 1) is None
        # An active route and different address cannot trigger withdrawn-address fallback.
        address = replace(packet.request.recipient, line1="987 Separate Example Avenue")
        change_route(store, scope, 0, packet.prepared_at, address=address)
        cursor = run_submission_step(
            store,
            lob,
            case.case_token,
            1,
            expected_case_revision=case.revision,
            expected_cursor_revision=None,
            operation_id="legacy-unknown-stage",
            clock=lambda: packet.prepared_at,
        )
        assert cursor.outcome == Rejected(422, GATE_REFUSED_REASON)
        assert not lob.calls


@pytest.mark.parametrize("free_beta", [False, True])
async def test_revoked_route_looks_up_the_original_intent_with_no_second_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, free_beta: bool
) -> None:
    async with ready_case(tmp_path, monkeypatch, free_beta=free_beta) as ready:
        change_route(ready.store, SCOPE, 0, ready.clock(), address=ready.recipient)
        ready.approve()
        prepared = ready.store.get(ready.token, 1)
        assert prepared is not None
        lob = FakeLobClient([LobResponse(None)])
        payments, transport = stripe(
            payment_fixture(), payment_fixture(), payment_fixture("succeeded")
        )
        object.__setattr__(ready.service._deps, "lob", lob)
        if not free_beta:
            object.__setattr__(ready.service._deps, "payments", payments)
        ready.dispatch()
        cursor = ready.store.submission_cursor(ready.token, 1)
        assert cursor is not None
        assert isinstance(cursor.outcome, Uncertain)
        assert lob.calls == [(prepared.key, prepared.request)]
        change_route(ready.store, SCOPE, 1, ready.clock(), status="revoked")
        ready.restart()
        ready.clock.now = cursor.next_at

        def inconclusive_lookup(token: str) -> MetadataLookup:
            lob.lookups.append(token)
            return MetadataLookup(authoritative=False)

        with monkeypatch.context() as lookup_patch:
            if not free_beta:
                # The paid fixture is already in its capture fallback interval.
                lookup_patch.setattr(lob, "find_by_metadata", inconclusive_lookup)
            ready.dispatch()
        cursor = ready.store.submission_cursor(ready.token, 1)
        assert cursor is not None
        assert isinstance(cursor.outcome, Uncertain)
        assert lob.lookups == [prepared.request.submission_token]
        assert not any(call[0] == "post" for call in transport.calls)

        # The original Lob response was lost. Later production evidence must still settle it.
        ready.clock.now = cursor.next_at
        lob.letters[prepared.request.submission_token] = "ltr_original"
        for _ in range(3):
            ready.dispatch()
        settled = ready.service.status(ready.case_id)
        assert settled.state is CaseState.IN_TRANSIT
        assert settled.payment_state is (
            PaymentState.NO_CHARGE if free_beta else PaymentState.CAPTURED
        )
        cursor = ready.store.submission_cursor(ready.token, 1)
        assert cursor is not None
        assert cursor.outcome == Accepted("ltr_original")
        assert lob.calls == [(prepared.key, prepared.request)]
        assert lob.lookups == [prepared.request.submission_token] * 2
        assert sum(call[1].endswith("/capture") for call in transport.calls) == int(not free_beta)


async def test_revoked_corrected_route_blocks_remail_and_preserves_refund(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with mailed_case(tmp_path, monkeypatch) as mailing:
        original = mailing.store.get(mailing.token, 1)
        mailing.confirm_return()
        corrected = mailing.correct_destination()
        mailing.clock.now += timedelta(minutes=1)
        change_route(mailing.store, SCOPE, 0, mailing.clock(), address=corrected.address)
        mailing.approve_remail()
        second = mailing.store.get(mailing.token, 2)
        assert second is not None
        assert second.request.recipient == corrected.address
        assert mailing.store.bound_route(mailing.token, 2) == (SCOPE, 1)
        change_route(mailing.store, SCOPE, 1, mailing.clock(), status="revoked")
        mailing.restart()
        mailing.sweep()
        assert_full_refund(mailing)
        assert len(mailing.lob.calls) == 1
        assert mailing.store.get(mailing.token, 1) == original
        assert mailing.store.get(mailing.token, 2) == second
        remedy = mailing.real.status(mailing.case_id).return_remedy
        assert remedy is not None
        assert remedy.reason_code == "SERVICE_FAILURE"
