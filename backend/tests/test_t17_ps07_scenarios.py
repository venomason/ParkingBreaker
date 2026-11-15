"""PS07 / U10 certified-tracking scenarios: honest customer copy, capture-once money.

Signed Lob tracking webhooks travel through the existing CaseService.webhook path,
reusing the T17 fixtures (test_t17_mail_evidence / test_submission_coordinator). Each
scenario drives one shape of real mail-stream noise -- a scan days after mailing,
attempted delivery then delivery, a return to sender, and delivered arriving before
in-transit with a duplicate replay -- and asserts the three PS07 contracts:

1. the customer-facing copy (case_service tracking() and the API status) is exactly
   the evidence received, in occurrence order;
2. it never claims the agency received or accepted the appeal, never promises a
   result, and never states a delivery without a delivered event;
3. money captures at most once, on the first qualifying USPS scan, never from
   non-qualifying events, and replays are idempotent.

The "accepted" entry is dated at the moment the acceptance evidence was ingested,
the same anchor the transition history uses for submission and return records; every
scan entry keeps the provider's own occurrence time. Duplicate replays resend the
identical signed payload inside its signature window.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from parkingbreaker import case_state as cs
from parkingbreaker import payment_state as ps
from parkingbreaker.api import schemas as s
from parkingbreaker.case_service import CaseService
from parkingbreaker.effect_dispatch import dispatch_once
from parkingbreaker.payment_fakes import FakePaymentClient
from parkingbreaker.prices import CERTIFIED_CENTS
from parkingbreaker.transaction_records import CaseRecord
from parkingbreaker.transaction_store import SqliteTransactionStore
from test_case_service import Clock, service
from test_submission_coordinator import NOW, prepared
from test_t17_mail_evidence import (
    OfflineLob,
    clock_at,
    current_case,
    new_id,
    resource,
    submit,
)
from test_t30_remedy import customer_messages

SECRET = b"secret"
MAILED_AT = NOW + timedelta(hours=2)
TRACKING_NUMBER = "9400000011112222333344"

# The copy must never claim the agency received or accepted the appeal, and never
# promise a result. Agency-side delivery wording ("delivered to the agency's mailing
# address") is honest USPS reporting and is deliberately absent from these lists.
AGENCY_RECEIPT_CLAIMS = (
    "agency received",
    "agency has received",
    "agency accepted",
    "agency has accepted",
    "received by the agency",
    "accepted by the agency",
    "agency has your",
    "appeal was received",
    "appeal has been received",
    "appeal was accepted",
    "appeal has been accepted",
    "we received your appeal",
    "office received",
    "office accepted",
    "clerk received",
    "under review",
    "is reviewing",
    "being reviewed",
)
RESULT_PROMISES = (
    "will be dismissed",
    "will be granted",
    "will be approved",
    "will be overturned",
    "will win",
    "will succeed",
    "we promise",
    "we guarantee",
    "guaranteed dismissal",
    "guaranteed result",
    "promised result",
)


@dataclass
class World:
    app: CaseService
    case: CaseRecord
    case_id: str
    payments: FakePaymentClient
    clock: Clock
    wire: OfflineLob

    def sweep(self, now: datetime) -> None:
        for _ in range(3):
            dispatch_once(self.app._store, self.wire.client, self.payments, clock_at(now), new_id)

    def close(self) -> None:
        self.app._store.close()


def prehold_offer(case_id: str) -> s.Offer:
    """The stored pre-hold shape the customer-facing status path parses."""
    return s.Offer(
        case_id=case_id,
        issuer=s.ResolvedIssuer(
            issuer_id="iss_test_agency",
            issuer_name="Test Agency",
            deciding_body="Appeal Review",
            state="CA",
            stage="initial_review",
            service_basis="mail_in",
            evidence_ref="synthetic-quote",
        ),
        tiers=[
            s.TierOffer(
                tier=s.MailTier.CERTIFIED,
                amount_cents=CERTIFIED_CENTS,
                description="Certified mail with tracking; no promised arrival date.",
            )
        ],
    )


def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    """One mailed case, submitted through the real coordinator, awaiting scan evidence."""
    wire = OfflineLob(monkeypatch, lambda *_: (200, resource()))
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        case = prepared(store)
        intake = store.intake_for_workspace(case.workspace_token)
        assert intake is not None
        # The low-level mailing fixture records only a placeholder pre-hold. The real
        # intake path saves a full offer there; restore that shape so the customer-facing
        # status and tracking paths see what a production intake carries.
        store._require_open().execute(
            "UPDATE pb_intake_records SET prehold_json=? WHERE intake_id=?",
            (prehold_offer(intake.intake_id).model_dump_json(), intake.intake_id),
        )
        submit(store, wire, case, NOW)
        case_id = intake.intake_id
    payments = FakePaymentClient(case.authorization_ref)
    clock = Clock()
    app = service(path, payments, clock=clock)
    object.__setattr__(app._deps, "lob_webhook_secret", "secret")
    object.__setattr__(app._deps, "new_id", new_id)
    return World(app, case, case_id, payments, clock, wire)


def usps_event(
    name: str, kind: str, at: datetime, substatus: str | None = None
) -> list[dict[str, object]]:
    event: dict[str, object] = {"type": kind, "name": name, "time": at.isoformat()}
    if substatus is not None:
        event["details"] = {"event": substatus}
    return [event]


def signed_event(
    body: dict[str, object],
    event_type: str,
    *,
    event_id: str,
    occurred_at: datetime,
    received_at: datetime,
) -> tuple[bytes, dict[str, str]]:
    """One Lob webhook envelope: occurrence in date_created, signature at receipt."""
    payload = json.dumps(
        {
            "id": event_id,
            "event_type": {"id": event_type},
            "body": body,
            "date_created": occurred_at.isoformat(),
        }
    ).encode()
    stamp = str(int(received_at.timestamp()))
    signature = hmac.new(SECRET, stamp.encode() + b"." + payload, hashlib.sha256).hexdigest()
    return payload, {"lob-signature": signature, "lob-signature-timestamp": stamp}


def send(
    received_world: World, payload: bytes, headers: dict[str, str], received_at: datetime
) -> bool:
    received_world.clock.now = received_at
    return received_world.app.webhook("lob", payload, headers)


def assert_no_capture(received_world: World) -> None:
    assert received_world.payments.captures == 0
    assert not any(call[0] == "capture" for call in received_world.payments.calls)
    effects = received_world.app._store.effects(received_world.case.case_token)
    assert not any(row.effect is ps.PaymentEffect.CAPTURE_PAYMENT for row in effects)
    saved = current_case(received_world.app._store, received_world.case)
    assert saved.payment_state is ps.PaymentState.AUTHORIZED


def assert_one_capture(received_world: World) -> None:
    assert received_world.payments.captures == 1
    assert received_world.payments.releases == 0
    assert sum(1 for call in received_world.payments.calls if call[0] == "capture") == 1
    effects = received_world.app._store.effects(received_world.case.case_token)
    assert sum(row.effect is ps.PaymentEffect.CAPTURE_PAYMENT for row in effects) == 1
    assert not any(row.effect is ps.PaymentEffect.RELEASE_HOLD for row in effects)
    saved = current_case(received_world.app._store, received_world.case)
    assert saved.payment_state is ps.PaymentState.CAPTURED


def assert_honest_copy(
    received_world: World,
    *,
    state: cs.CaseState,
    accepted: bool = True,
    delivered_received: bool = False,
) -> s.Tracking:
    """Copy is evidence only: no agency receipt/acceptance, no promised result."""
    tracking_copy = received_world.app.tracking(received_world.case_id)
    status = received_world.app.status(received_world.case_id)
    text = " ".join(customer_messages(tracking_copy.model_dump())).lower()
    text += " " + " ".join(customer_messages(status.model_dump())).lower()
    for phrase in AGENCY_RECEIPT_CLAIMS:
        assert phrase not in text
    for phrase in RESULT_PROMISES:
        assert phrase not in text
    accepted_events = [event for event in tracking_copy.events if event.event == "accepted"]
    assert len(accepted_events) == (1 if accepted else 0)
    if accepted_events:
        assert "this is not delivery confirmation" in accepted_events[0].description
    for event in tracking_copy.events:
        if event.event != "accepted":
            assert "not stored delivery proof" in event.description
    delivered = [event for event in tracking_copy.events if "delivered" in event.event]
    if delivered_received:
        assert delivered
    else:
        assert not delivered
        assert "delivered" not in text
    assert status.state is state
    assert status.stop is None
    return tracking_copy


def assert_evidence(tracking_copy: s.Tracking, expected: list[tuple[str, datetime]]) -> None:
    assert [(event.event, event.occurred_at) for event in tracking_copy.events] == expected


def test_delayed_scan_keeps_its_own_date_and_captures_only_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scan days after mailing is shown at its own occurrence, never at mailing time."""
    mailed_world = world(tmp_path, monkeypatch)
    try:
        mailed = signed_event(
            resource(tracking_events=usps_event("Mailed", "certified", MAILED_AT)),
            "letter.mailed",
            event_id="evt_mailed",
            occurred_at=MAILED_AT,
            received_at=MAILED_AT,
        )
        assert send(mailed_world, mailed[0], mailed[1], MAILED_AT) is True
        mailed_world.sweep(MAILED_AT)
        assert_no_capture(mailed_world)
        pending = assert_honest_copy(mailed_world, state=cs.CaseState.SUBMITTING, accepted=False)
        assert_evidence(pending, [("letter.mailed", MAILED_AT)])

        scanned_at = NOW + timedelta(days=3)
        scan = signed_event(
            resource(
                tracking_number=TRACKING_NUMBER,
                tracking_events=usps_event("In Transit", "normal", scanned_at),
            ),
            "letter.in_transit",
            event_id="evt_delayed_scan",
            occurred_at=scanned_at,
            received_at=scanned_at,
        )
        assert send(mailed_world, scan[0], scan[1], scanned_at) is True
        mailed_world.sweep(scanned_at)
        assert_one_capture(mailed_world)

        assert send(mailed_world, scan[0], scan[1], scanned_at) is False
        mailed_world.sweep(scanned_at)
        assert_one_capture(mailed_world)

        tracking_copy = assert_honest_copy(mailed_world, state=cs.CaseState.IN_TRANSIT)
        assert_evidence(
            tracking_copy,
            [
                ("letter.mailed", MAILED_AT),
                ("accepted", scanned_at),
                ("letter.in_transit", scanned_at),
            ],
        )
        # The scan is dated at its own occurrence days after mailing, not at mailing.
        assert tracking_copy.events[-1].occurred_at == NOW + timedelta(days=3)
        assert tracking_copy.tracking_number == TRACKING_NUMBER
    finally:
        mailed_world.close()


def test_attempted_delivery_is_not_delivery_until_the_delivered_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Attempted delivery is its own honest state; only a delivered event may show one."""
    mailed_world = world(tmp_path, monkeypatch)
    try:
        mailed = signed_event(
            resource(tracking_events=usps_event("Mailed", "certified", MAILED_AT)),
            "letter.mailed",
            event_id="evt_mailed",
            occurred_at=MAILED_AT,
            received_at=MAILED_AT,
        )
        assert send(mailed_world, mailed[0], mailed[1], MAILED_AT) is True
        mailed_world.sweep(MAILED_AT)
        assert_no_capture(mailed_world)
        pending = assert_honest_copy(mailed_world, state=cs.CaseState.SUBMITTING, accepted=False)
        assert_evidence(pending, [("letter.mailed", MAILED_AT)])

        attempted_at = NOW + timedelta(days=1)
        attempted = signed_event(
            resource(
                tracking_number=TRACKING_NUMBER,
                tracking_events=usps_event(
                    "Processed for Delivery", "certified", attempted_at, "delivery_attempted"
                ),
            ),
            "letter.certified.processed_for_delivery",
            event_id="evt_attempted_delivery",
            occurred_at=attempted_at,
            received_at=attempted_at,
        )
        assert send(mailed_world, attempted[0], attempted[1], attempted_at) is True
        mailed_world.sweep(attempted_at)
        assert_one_capture(mailed_world)

        midway = assert_honest_copy(mailed_world, state=cs.CaseState.IN_TRANSIT)
        assert_evidence(
            midway,
            [
                ("letter.mailed", MAILED_AT),
                ("accepted", attempted_at),
                ("letter.certified.processed_for_delivery", attempted_at),
            ],
        )

        delivered_at = NOW + timedelta(days=2)
        delivered = signed_event(
            resource(
                tracking_number=TRACKING_NUMBER,
                tracking_events=usps_event("Delivered", "certified", delivered_at, "delivered"),
            ),
            "letter.certified.delivered",
            event_id="evt_delivered",
            occurred_at=delivered_at,
            received_at=delivered_at,
        )
        assert send(mailed_world, delivered[0], delivered[1], delivered_at) is True
        mailed_world.sweep(delivered_at)
        assert_one_capture(mailed_world)

        assert send(mailed_world, delivered[0], delivered[1], delivered_at) is False
        mailed_world.sweep(delivered_at)
        assert_one_capture(mailed_world)

        # A delivered scan is postal delivery evidence, never stored delivery proof.
        tracking_copy = assert_honest_copy(
            mailed_world, state=cs.CaseState.IN_TRANSIT, delivered_received=True
        )
        assert_evidence(
            tracking_copy,
            [
                ("letter.mailed", MAILED_AT),
                ("accepted", attempted_at),
                ("letter.certified.processed_for_delivery", attempted_at),
                ("letter.certified.delivered", delivered_at),
            ],
        )
    finally:
        mailed_world.close()


def test_returned_to_sender_copy_reports_the_return_without_delivery_claims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A return is shown as a return: no delivery claim, no agency receipt, one capture."""
    mailed_world = world(tmp_path, monkeypatch)
    try:
        mailed = signed_event(
            resource(tracking_events=usps_event("Mailed", "certified", MAILED_AT)),
            "letter.mailed",
            event_id="evt_mailed",
            occurred_at=MAILED_AT,
            received_at=MAILED_AT,
        )
        assert send(mailed_world, mailed[0], mailed[1], MAILED_AT) is True
        mailed_world.sweep(MAILED_AT)
        assert_no_capture(mailed_world)
        pending = assert_honest_copy(mailed_world, state=cs.CaseState.SUBMITTING, accepted=False)
        assert_evidence(pending, [("letter.mailed", MAILED_AT)])

        transit_at = NOW + timedelta(days=1)
        transit = signed_event(
            resource(
                tracking_number=TRACKING_NUMBER,
                tracking_events=usps_event("In Transit", "normal", transit_at),
            ),
            "letter.in_transit",
            event_id="evt_in_transit",
            occurred_at=transit_at,
            received_at=transit_at,
        )
        assert send(mailed_world, transit[0], transit[1], transit_at) is True
        mailed_world.sweep(transit_at)
        assert_one_capture(mailed_world)

        returned_at = NOW + timedelta(days=2)
        returned = signed_event(
            resource(
                tracking_number=TRACKING_NUMBER,
                tracking_events=usps_event(
                    "Returned to Sender", "certified", returned_at, "no_such_number"
                ),
            ),
            "letter.certified.returned_to_sender",
            event_id="evt_returned",
            occurred_at=returned_at,
            received_at=returned_at,
        )
        assert send(mailed_world, returned[0], returned[1], returned_at) is True
        mailed_world.sweep(returned_at)
        assert_one_capture(mailed_world)

        assert send(mailed_world, returned[0], returned[1], returned_at) is False
        mailed_world.sweep(returned_at)
        assert_one_capture(mailed_world)

        tracking_copy = assert_honest_copy(mailed_world, state=cs.CaseState.RETURNED)
        assert_evidence(
            tracking_copy,
            [
                ("letter.mailed", MAILED_AT),
                ("accepted", transit_at),
                ("letter.in_transit", transit_at),
                ("letter.certified.returned_to_sender", returned_at),
            ],
        )
    finally:
        mailed_world.close()


def test_out_of_order_arrival_shows_occurrence_order_and_replays_stay_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Delivered may arrive before in-transit; the copy orders by occurrence, once each."""
    mailed_world = world(tmp_path, monkeypatch)
    try:
        mailed = signed_event(
            resource(tracking_events=usps_event("Mailed", "certified", MAILED_AT)),
            "letter.mailed",
            event_id="evt_mailed",
            occurred_at=MAILED_AT,
            received_at=MAILED_AT,
        )
        assert send(mailed_world, mailed[0], mailed[1], MAILED_AT) is True
        mailed_world.sweep(MAILED_AT)
        assert_no_capture(mailed_world)
        pending = assert_honest_copy(mailed_world, state=cs.CaseState.SUBMITTING, accepted=False)
        assert_evidence(pending, [("letter.mailed", MAILED_AT)])

        transit_at = NOW + timedelta(days=1)
        delivered_at = NOW + timedelta(days=2)
        # The delivered scan occurs second but its webhook arrives first; the in-transit
        # webhook arrives a day later and must be dated at its own earlier occurrence.
        transit_arrived_at = delivered_at + timedelta(days=1)
        delivered = signed_event(
            resource(
                tracking_number=TRACKING_NUMBER,
                tracking_events=usps_event("Delivered", "normal", delivered_at),
            ),
            "letter.delivered",
            event_id="evt_delivered_first",
            occurred_at=delivered_at,
            received_at=delivered_at,
        )
        transit = signed_event(
            resource(
                tracking_number=TRACKING_NUMBER,
                tracking_events=usps_event("In Transit", "normal", transit_at),
            ),
            "letter.in_transit",
            event_id="evt_transit_late",
            occurred_at=transit_at,
            received_at=transit_arrived_at,
        )
        assert send(mailed_world, delivered[0], delivered[1], delivered_at) is True
        mailed_world.sweep(delivered_at)
        assert_one_capture(mailed_world)
        assert send(mailed_world, transit[0], transit[1], transit_arrived_at) is True
        mailed_world.sweep(transit_arrived_at)
        assert_one_capture(mailed_world)

        assert send(mailed_world, delivered[0], delivered[1], delivered_at) is False
        assert send(mailed_world, transit[0], transit[1], transit_arrived_at) is False
        mailed_world.sweep(transit_arrived_at)
        assert_one_capture(mailed_world)

        tracking_copy = assert_honest_copy(
            mailed_world, state=cs.CaseState.IN_TRANSIT, delivered_received=True
        )
        assert_evidence(
            tracking_copy,
            [
                ("letter.mailed", MAILED_AT),
                ("letter.in_transit", transit_at),
                ("accepted", delivered_at),
                ("letter.delivered", delivered_at),
            ],
        )
        order = [event.event for event in tracking_copy.events]
        assert order.index("letter.in_transit") < order.index("letter.delivered")
    finally:
        mailed_world.close()
