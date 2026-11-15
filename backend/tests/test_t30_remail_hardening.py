"""T30 re-mail hardening: signature page-boundary drift and the bounded returned-mail sweep.

Each regression test was written first and confirmed failing against the reviewed code.

Re-mailing a frozen packet recovers the customer's ink by re-rendering the frozen adopted
text to find the letter-page boundary. If the renderer's pagination has drifted since the
packet was frozen, that boundary can end before our signature widget's page and the signed
packet would be re-mailed without the ink (or, wider, with an agency form's field mistaken
for it). The boundary must be confirmed against the frozen PDF itself and fail closed with
PacketError (one full refund, no second letter) whenever it cannot be.

The scheduler sweep visited every CLOSED_RETURNED case forever. Only a case with attempt 2,
a definitive failed re-mail submission, a captured payment and no returned-mail refund
intent yet can still act; those are the only closed cases the sweep may load. The refund
itself is unchanged: one intent, one refund, idempotent across restarts.
"""

from __future__ import annotations

import io
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from PIL import Image

from parkingbreaker import case_state as cs
from parkingbreaker.appeal import packet as packet_module
from parkingbreaker.appeal.packet import PacketError, build_packet, frozen_signature_artwork
from parkingbreaker.lob_submission import LobResponse, Rejected
from parkingbreaker.payment_state import PaymentState
from parkingbreaker.submission_records import outcome_json
from parkingbreaker.transaction_records import workspace_json
from parkingbreaker.transaction_store import SqliteTransactionStore
from parkingbreaker.workspace import Workspace
from test_appeal_packet import AT, CITATION, EVIDENCE, letter_text
from test_case_service import fake_resolution
from test_t30_remedy import Mailing, assert_one_charge, mailed_case
from test_t30_remedy_review import signature_image

assert fake_resolution
pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("fake_resolution")]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def signed_packet(sentences: int) -> bytes:
    return build_packet(
        letter_text(sentences),
        CITATION,
        EVIDENCE,
        AT,
        customer_signature=signature_image("RGB", "PNG"),
    ).pdf


# The letter-page boundary is recomputed with the renderer that is current at re-mail
# time. A changed line height is the same drift: the same adopted text paginates
# differently than the pages the frozen PDF actually contains.


def test_longer_pagination_drift_fails_closed_instead_of_loose_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A one-page signed letter re-rendered as two pages never confirms its boundary."""
    pdf = signed_packet(sentences=20)
    monkeypatch.setattr(packet_module, "_LINE_HEIGHT", 20.0)
    with pytest.raises(PacketError, match="not recoverable"):
        frozen_signature_artwork(pdf)


def test_shorter_pagination_drift_fails_closed_instead_of_unsigned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A signed two-page letter re-rendered as one page is never treated as unsigned."""
    pdf = signed_packet(sentences=60)
    monkeypatch.setattr(packet_module, "_LINE_HEIGHT", 6.0)
    with pytest.raises(PacketError, match="not recoverable"):
        frozen_signature_artwork(pdf)


def test_unchanged_pagination_still_recovers_the_exact_ink() -> None:
    ink = signature_image("RGB", "PNG")
    packet = build_packet(letter_text(), CITATION, EVIDENCE, AT, customer_signature=ink)
    recovered = frozen_signature_artwork(packet.pdf)
    assert recovered is not None
    expected = Image.open(io.BytesIO(ink)).convert("RGBA")
    actual = Image.open(io.BytesIO(recovered)).convert("RGBA")
    assert actual.size == expected.size
    assert actual.tobytes() == expected.tobytes()


class _CursorSpy:
    def __init__(self, cursor: sqlite3.Cursor, owner: _ConnectionSpy) -> None:
        self._cursor = cursor
        self._owner = owner

    def fetchall(self) -> list[tuple[Any, ...]]:
        rows = self._cursor.fetchall()
        self._owner.record(rows)
        return rows

    def fetchone(self) -> tuple[Any, ...] | None:
        row = self._cursor.fetchone()
        self._owner.record(() if row is None else (row,))
        return cast(tuple[Any, ...] | None, row)

    def __iter__(self) -> _CursorSpy:
        return self

    def __next__(self) -> tuple[Any, ...]:
        row = next(self._cursor)
        self._owner.record((row,))
        return cast(tuple[Any, ...], row)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)


class _ConnectionSpy:
    """Records every intake row the store reads back (as in test_t18_history_streaming)."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self.intakes_read: set[str] = set()

    def record(self, rows: list[tuple[Any, ...]] | tuple[tuple[Any, ...], ...]) -> None:
        self.intakes_read.update(str(row[0]) for row in rows)

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> _CursorSpy | sqlite3.Cursor:
        cursor = self._connection.execute(sql, parameters)
        if sql.lstrip().upper().startswith("SELECT") and "FROM pb_intake_records" in sql:
            return _CursorSpy(cursor, self)
        return cursor

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


def failed_remail_case(mailing: Mailing) -> None:
    """Drive a paid, mailed case to CLOSED_RETURNED with a failed free re-mail.

    One sweep freezes the free re-mail and submits it; the refund intent is only
    requested on a later sweep, so this stops before any of that work.
    """
    mailing.confirm_return()
    mailing.correct_destination()
    mailing.lob.script = [LobResponse(422, error="synthetic refusal")]
    mailing.sweep(1)
    assert mailing.case.letter_state is cs.CaseState.CLOSED_RETURNED
    cursor = mailing.store.submission_cursor(mailing.token, 2)
    assert cursor is not None
    assert isinstance(cursor.outcome, Rejected)
    assert mailing.store.returned_mail_refund(mailing.token) is None


async def test_sweep_does_not_load_closed_cases_that_need_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with mailed_case(tmp_path, monkeypatch) as mailing:
        failed_remail_case(mailing)
        mailing.sweep(12)
        assert mailing.case.payment_state is PaymentState.REFUNDED
        assert mailing.store.returned_mail_refund(mailing.token) is not None
        # Settled: attempt 2's re-mail failed, the payment is refunded and the
        # returned-mail refund intent is already recorded. Nothing can still act.
        spy = _ConnectionSpy(mailing.store._require_open())
        mailing.store._database = spy  # type: ignore[assignment]

        result = mailing.real.advance_due()

        assert result.failures == ()
        assert mailing.case_id not in spy.intakes_read, "settled closed case was loaded"


async def test_failed_remail_refund_still_happens_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with mailed_case(tmp_path, monkeypatch) as mailing:
        failed_remail_case(mailing)

        mailing.sweep(12)

        assert mailing.case.payment_state is PaymentState.REFUNDED
        assert mailing.payments.refund_calls == 1
        refunds = [r for r in mailing.payments.refunds if r.status == "succeeded"]
        assert len(refunds) == 1
        assert refunds[0].amount_cents == mailing.case.amount_cents
        intent = mailing.store.returned_mail_refund(mailing.token)
        assert intent is not None
        assert intent.attempt == 2
        history = mailing.store.transition_history(mailing.token)
        assert len([r for r in history if r.event_name == "ReturnedMailRefundRequested"]) == 1
        assert len([r for r in history if r.event_name == "RefundIssued"]) == 1
        assert_one_charge(mailing)

        mailing.sweep(4)

        assert len(mailing.payments.refunds) == 1
        assert mailing.store.returned_mail_refund(mailing.token) == intent


NOW = datetime(2026, 10, 11, 12, tzinfo=UTC)


def seed_closed_returned(
    store: SqliteTransactionStore,
    name: str,
    *,
    attempt: int,
    payment_state: str,
    submission: str | None,
    refund_intent: bool = False,
) -> str:
    """One CLOSED_RETURNED case row plus the tables the bounded query may join.

    These rows are only ever read by that query (no lifecycle is replayed), so only
    the columns its conditions filter on need to be realistic.
    """
    database = store._require_open()
    stamp = (NOW + timedelta(minutes=1)).isoformat()
    workspace = f"workspace-{name}"
    token = uuid4().hex
    intake_id = f"intake-{name}"
    database.execute(
        "INSERT INTO pb_workspaces VALUES (?, ?, ?, ?)",
        (workspace, workspace_json(Workspace()), stamp, 1),
    )
    database.execute("INSERT INTO pb_intake_owners VALUES (?, ?, ?)", (workspace, name, stamp))
    database.execute(
        "INSERT INTO pb_intake_records "
        "(intake_id, workspace_token, intake_json, prehold_json, selected_tier, case_token, "
        "created_at, revision) VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
        (intake_id, workspace, "{}", "{}", "standard", token, stamp),
    )
    database.execute(
        "INSERT INTO pb_cases "
        "(case_token, workspace_token, authorization_ref, authorized_at, "
        "authorization_expires_at, amount_cents, letter_state, payment_state, attempt, revision) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
        (
            token,
            workspace,
            f"auth-{name}",
            stamp,
            (NOW + timedelta(days=5)).isoformat(),
            2999,
            cs.CaseState.CLOSED_RETURNED.value,
            payment_state,
            attempt,
        ),
    )
    if submission is not None:
        database.execute(
            "INSERT INTO pb_mail_attempts "
            "(case_token, attempt, pdf, submission_token, idempotency_key, pdf_sha256, "
            "prepared_at, envelope_json, request_sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                token,
                attempt,
                b"%PDF-1.4\nsynthetic",
                f"submission-{name}",
                f"key-{name}",
                "0" * 64,
                stamp,
                "{}",
                "0" * 64,
            ),
        )
        database.execute(
            "INSERT INTO pb_submission_cursors "
            "(case_token, attempt, first_sent_at, sends, next_at, lookup_only, revision, owner, "
            "case_revision, outcome_json) VALUES (?, ?, ?, 1, ?, 0, 1, NULL, 1, ?)",
            (token, attempt, stamp, stamp, submission),
        )
    if refund_intent:
        database.execute(
            "INSERT INTO pb_case_history VALUES (?, 2, 'payment', ?, ?, "
            "'ReturnedMailRefundRequested', ?, ?)",
            (
                token,
                PaymentState.CAPTURED.value,
                PaymentState.CAPTURED.value,
                json.dumps(
                    {"amount_cents": 2999, "attempt": attempt, "kind": "FULL_REFUND"},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                stamp,
            ),
        )
    return intake_id


def test_bounded_query_reads_only_the_cases_that_can_still_act(tmp_path: Path) -> None:
    rejected = outcome_json(Rejected(422, "synthetic refusal"))
    accepted = json.dumps(
        {"kind": "accepted", "letter_id": "ltr_synthetic"}, sort_keys=True, separators=(",", ":")
    )
    uncertain = json.dumps(
        {"kind": "uncertain", "reason": "synthetic"}, sort_keys=True, separators=(",", ":")
    )
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        actionable = seed_closed_returned(
            store, "actionable", attempt=2, payment_state="CAPTURED", submission=rejected
        )
        # Every one of these closed cases misses at least one condition and needs nothing.
        seed_closed_returned(
            store, "first-attempt", attempt=1, payment_state="CAPTURED", submission=accepted
        )
        seed_closed_returned(
            store,
            "capture-pending",
            attempt=2,
            payment_state="CAPTURE_PENDING",
            submission=rejected,
        )
        seed_closed_returned(
            store, "refund-issued", attempt=2, payment_state="REFUNDED", submission=rejected
        )
        seed_closed_returned(
            store, "accepted-remail", attempt=2, payment_state="CAPTURED", submission=accepted
        )
        seed_closed_returned(
            store, "uncertain-remail", attempt=2, payment_state="CAPTURED", submission=uncertain
        )
        seed_closed_returned(
            store, "no-submission", attempt=2, payment_state="CAPTURED", submission=None
        )
        seed_closed_returned(
            store,
            "intent-recorded",
            attempt=2,
            payment_state="CAPTURED",
            submission=rejected,
            refund_intent=True,
        )
        spy = _ConnectionSpy(store._require_open())
        store._database = spy  # type: ignore[assignment]

        assert tuple(store.failed_remail_refund_candidates()) == (actionable,)

        assert spy.intakes_read == {actionable}, "closed cases that need nothing were loaded"
