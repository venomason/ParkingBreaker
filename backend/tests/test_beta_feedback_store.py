"""Immutable accepted-beta notice records: ownership, replay, correction and paging."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest

from mail_samples import make_request, seed_destination
from parkingbreaker import case_state as cs
from parkingbreaker.beta_feedback_store import (
    BetaFeedbackConflict,
    NoticePage,
    NoticeReceipt,
)
from parkingbreaker.beta_store import BETA_DISCLOSURE_SHA256
from parkingbreaker.case_hold_store import HoldCommand, HoldReason
from parkingbreaker.intake_store import IntakeConflict
from parkingbreaker.mail_attempt_store import StorageCorruption
from parkingbreaker.payment_state import PaymentState
from parkingbreaker.transaction_records import CaseRecord
from parkingbreaker.transaction_store import SqliteTransactionStore
from parkingbreaker.workspace import Workspace
from test_beta_admission import NOW, campaign, candidate

REF_A = "photo_" + "a" * 64
REF_B = "photo_" + "b" * 64
REF_C = "photo_" + "c" * 64


def pages(*items: tuple[str, str]) -> tuple[NoticePage, ...]:
    return tuple(NoticePage(role, ref) for role, ref in items)


def accepted_one(store: SqliteTransactionStore) -> CaseRecord:
    campaign(store)
    candidate(store, 1)
    store.select_beta_batch("beta-1", "batch-1", "approval-1", NOW)
    return store.start_beta_case("case_1", BETA_DISCLOSURE_SHA256, NOW)


def accepted_two(store: SqliteTransactionStore) -> None:
    campaign(store)
    candidate(store, 1)
    candidate(store, 2)
    store.select_beta_batch("beta-1", "batch-1", "approval-1", NOW)
    store.start_beta_case("case_1", BETA_DISCLOSURE_SHA256, NOW)
    store.start_beta_case("case_2", BETA_DISCLOSURE_SHA256, NOW)


def test_owner_case_and_acceptance_matrix(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        campaign(store)
        candidate(store, 1)
        candidate(store, 2)
        # Selected but not accepted: notice capture is refused before any row exists.
        with pytest.raises(BetaFeedbackConflict, match="accepted_beta_required"):
            store.record_beta_feedback_notice(
                "case_1", "owner-1", "req-early", pages(("page", REF_A)), NOW
            )
        store.select_beta_batch("beta-1", "batch-1", "approval-1", NOW)
        case_1 = store.start_beta_case("case_1", BETA_DISCLOSURE_SHA256, NOW)
        case_2 = store.start_beta_case("case_2", BETA_DISCLOSURE_SHA256, NOW)
        assert case_1.payment_state is case_2.payment_state is PaymentState.NO_CHARGE

        first = store.record_beta_feedback_notice(
            "case_1", "owner-1", "req-1", pages(("envelope", REF_A)), NOW
        )
        second = store.record_beta_feedback_notice(
            "case_2", "owner-2", "req-1", pages(("page", REF_B)), NOW
        )
        assert first.case_id == "case_1"
        assert second.case_id == "case_2"
        assert first.notice_id != second.notice_id

        with pytest.raises(IntakeConflict, match="not_found"):
            store.record_beta_feedback_notice(
                "case_1", "owner-2", "req-wrong", pages(("page", REF_C)), NOW
            )
        with pytest.raises(IntakeConflict, match="not_found"):
            store.beta_feedback_notice("case_2", "owner-1", second.notice_id)
        with pytest.raises(IntakeConflict, match="not_found"):
            store.list_beta_feedback_notices("case_1", "owner-2")

        # A real paid intake/case refuses, and the refusal writes nothing.
        store.create_owned_workspace("owner-9", "ws_9", Workspace(), NOW)
        paid = store.start_case("ws_9", "paid-authorization", NOW, NOW + timedelta(days=5))
        intake = store.record_intake(
            "case_9",
            "ws_9",
            json.dumps({"fixture": 9}),
            json.dumps({"kind": "offer", "issuer": {"issuer_id": "CA:issuer-9.gov"}, "tiers": []}),
            NOW,
        )
        store.update_intake(
            "case_9", intake.revision, selected_tier="CERTIFIED", case_token=paid.case_token
        )
        with pytest.raises(BetaFeedbackConflict, match="no_charge_required"):
            store.record_beta_feedback_notice(
                "case_9", "owner-9", "req-paid", pages(("page", REF_C)), NOW
            )
        assert store._require_open().execute(
            "SELECT COUNT(*) FROM pb_beta_feedback_notices"
        ).fetchone() == (2,)


def test_exact_replay_restart_conflict_and_immutable_correction(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        accepted_one(store)
        original = store.record_beta_feedback_notice(
            "case_1",
            "owner-1",
            "req-original",
            pages(("envelope", REF_A), ("page", REF_B)),
            NOW,
            customer_seen_on="2026-11-01",
            printed_on="2026-11-02",
            stated_deadline_text="  Appeal by 11/20/2026, says the ticket.  ",
        )
        assert original.needs_confirmation is True
        assert original.review_state == "unreviewed"
        assert original.pages == pages(("envelope", REF_A), ("page", REF_B))
        assert original.stated_deadline_text == "  Appeal by 11/20/2026, says the ticket.  "

        replay = store.record_beta_feedback_notice(
            "case_1",
            "owner-1",
            "req-original",
            pages(("envelope", REF_A), ("page", REF_B)),
            NOW + timedelta(days=3),
            customer_seen_on="2026-11-01",
            printed_on="2026-11-02",
            stated_deadline_text="  Appeal by 11/20/2026, says the ticket.  ",
        )
        assert replay == original
        for changed in (
            pages(("envelope", REF_A), ("page", REF_C)),
            pages(("page", REF_B), ("envelope", REF_A)),
            pages(("envelope", REF_A), ("page", REF_B)),
        ):
            if changed == original.pages:
                continue
            with pytest.raises(BetaFeedbackConflict, match="request_conflict"):
                store.record_beta_feedback_notice("case_1", "owner-1", "req-original", changed, NOW)
        with pytest.raises(BetaFeedbackConflict, match="request_conflict"):
            store.record_beta_feedback_notice(
                "case_1",
                "owner-1",
                "req-original",
                pages(("envelope", REF_A), ("page", REF_B)),
                NOW,
                stated_deadline_text="different literal text",
            )

        correction = store.record_beta_feedback_notice(
            "case_1",
            "owner-1",
            "req-correction",
            pages(("envelope", REF_A), ("page", REF_C)),
            NOW + timedelta(hours=1),
            correction_of=original.notice_id,
        )
        assert correction.correction_of == original.notice_id
        assert store.beta_feedback_notice("case_1", "owner-1", original.notice_id) == original
        with pytest.raises(BetaFeedbackConflict, match="correction_not_found"):
            store.record_beta_feedback_notice(
                "case_1",
                "owner-1",
                "req-unknown-correction",
                pages(("page", REF_C)),
                NOW,
                correction_of="f" * 32,
            )

    with SqliteTransactionStore(path) as reopened:
        assert (
            reopened.record_beta_feedback_notice(
                "case_1",
                "owner-1",
                "req-original",
                pages(("envelope", REF_A), ("page", REF_B)),
                NOW + timedelta(days=30),
                customer_seen_on="2026-11-01",
                printed_on="2026-11-02",
                stated_deadline_text="  Appeal by 11/20/2026, says the ticket.  ",
            )
            == original
        )
        listing = reopened.list_beta_feedback_notices("case_1", "owner-1")
        assert [item.notice_id for item in listing.notices] == [
            original.notice_id,
            correction.notice_id,
        ]
        with pytest.raises(BetaFeedbackConflict, match="correction_not_found"):
            reopened.record_beta_feedback_notice(
                "case_1",
                "owner-1",
                "req-other-case-correction",
                pages(("page", REF_C)),
                NOW,
                correction_of="e" * 32,
            )
        # Immutability is enforced by storage, not merely by the absence of methods.
        database = reopened._require_open()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            database.execute(
                "UPDATE pb_beta_feedback_notices SET review_state='reviewed' WHERE notice_id=?",
                (original.notice_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            database.execute(
                "DELETE FROM pb_beta_feedback_notices WHERE notice_id=?", (original.notice_id,)
            )


def test_terminal_held_and_admissions_paused_accepted_cases_remain_readable(
    tmp_path: Path,
) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = accepted_one(store)
        store.record_hold_command(
            "hold-1",
            case.case_token,
            HoldCommand.HOLD,
            NOW,
            expected_generation=None,
            reason=HoldReason.INCIDENT,
            review_due_at=NOW + timedelta(days=1),
        )
        held = store.record_beta_feedback_notice(
            "case_1", "owner-1", "req-held", pages(("page", REF_A)), NOW
        )
        case = store.apply_letter_event(
            case.case_token,
            cs.Stopped(cs.StopReason.NOT_YET_VERIFIED, hard_stop=cs.HardStop.S2),
            case.revision,
            occurred_at=NOW,
        )
        assert case.letter_state is cs.CaseState.STOPPED_NO_CHARGE
        # The campaign admission window is now closed; accepted service records continue.
        with pytest.raises(ValueError, match="campaign_window_closed"):
            store.select_beta_batch("beta-1", "batch-2", "approval-2", NOW + timedelta(days=60))
        terminal = store.record_beta_feedback_notice(
            "case_1",
            "owner-1",
            "req-terminal",
            pages(("page", REF_B)),
            NOW + timedelta(days=60),
        )
        assert store.beta_feedback_notice("case_1", "owner-1", held.notice_id) == held
        listed = store.list_beta_feedback_notices("case_1", "owner-1")
        assert [item.notice_id for item in listed.notices] == [
            held.notice_id,
            terminal.notice_id,
        ]


def test_syntax_refusals_are_fixed_and_write_nothing(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        accepted_one(store)
        bad = (
            pages(("signature", REF_A)),
            pages(("page", "photo_" + "A" * 64)),
            pages(("page", "photo_short")),
            pages(("page", REF_A), ("envelope", REF_A)),
            pages(*(("page", "photo_" + f"{index:064x}") for index in range(21))),
        )
        for value in bad:
            with pytest.raises(ValueError, match="beta feedback"):
                store.record_beta_feedback_notice("case_1", "owner-1", "req-bad", value, NOW)
        with pytest.raises(ValueError, match="customer seen date"):
            store.record_beta_feedback_notice(
                "case_1",
                "owner-1",
                "req-bad",
                pages(("page", REF_A)),
                NOW,
                customer_seen_on="2026-02-30",
            )
        with pytest.raises(ValueError, match="printed date"):
            store.record_beta_feedback_notice(
                "case_1",
                "owner-1",
                "req-bad",
                pages(("page", REF_A)),
                NOW,
                printed_on=datetime(2026, 11, 1, tzinfo=UTC),  # type: ignore[arg-type]
            )
        with pytest.raises(ValueError, match="stated deadline"):
            store.record_beta_feedback_notice(
                "case_1",
                "owner-1",
                "req-bad",
                pages(("page", REF_A)),
                NOW,
                stated_deadline_text="x" * 1001,
            )
        with pytest.raises(ValueError, match="limit"):
            store.list_beta_feedback_notices("case_1", "owner-1", limit=101)
        assert store._require_open().execute(
            "SELECT COUNT(*) FROM pb_beta_feedback_notices"
        ).fetchone() == (0,)


def test_two_connections_identical_request_yield_one_exact_winner(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        accepted_one(store)
    ready = Barrier(2)

    def record(number: int) -> NoticeReceipt:
        with SqliteTransactionStore(path) as store:
            ready.wait(timeout=10)
            return store.record_beta_feedback_notice(
                "case_1",
                "owner-1",
                "req-concurrent",
                pages(("envelope", REF_A), ("page", REF_B)),
                NOW + timedelta(seconds=number),
            )

    with ThreadPoolExecutor(max_workers=2) as workers:
        receipts = list(workers.map(record, (1, 2)))
    assert receipts[0] == receipts[1]
    with SqliteTransactionStore(path) as store:
        assert store._require_open().execute(
            "SELECT COUNT(*) FROM pb_beta_feedback_notices"
        ).fetchone() == (1,)
        assert store.list_beta_feedback_notices("case_1", "owner-1").notices == (receipts[0],)


def _seed_bulk(store: SqliteTransactionStore, count: int) -> list[str]:
    database = store._require_open()
    rows = []
    request_ids = []
    received = NOW.isoformat()
    database.execute("BEGIN IMMEDIATE")
    try:
        for index in range(count):
            request_id = f"bulk_{index:05d}"
            request_ids.append(request_id)
            payload = {
                "case_id": "case_1",
                "owner": "owner-1",
                "request_id": request_id,
                "pages": [{"role": "page", "photo_ref": "photo_" + f"{index:064x}"}],
                "customer_seen_on": None,
                "printed_on": None,
                "stated_deadline_text": None,
                "correction_of": None,
            }
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            rows.append(
                (
                    "case_1",
                    "owner-1",
                    f"{index:032x}",
                    request_id,
                    received,
                    encoded,
                    hashlib.sha256(encoded.encode()).hexdigest(),
                    None,
                )
            )
        database.executemany(
            "INSERT INTO pb_beta_feedback_notices "
            "(case_id, owner, notice_id, request_id, received_at, payload_json, "
            "payload_sha256, correction_of, needs_confirmation, review_state) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'unreviewed')",
            rows,
        )
        database.execute("COMMIT")
    except Exception:
        if database.in_transaction:
            database.execute("ROLLBACK")
        raise
    return request_ids


def test_pagination_streams_5001_in_durable_order_and_refuses_foreign_cursors(
    tmp_path: Path,
) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        accepted_two(store)
        request_ids = _seed_bulk(store, 5001)
        seen: list[str] = []
        cursor = None
        pages_seen = 0
        while True:
            page = store.list_beta_feedback_notices("case_1", "owner-1", limit=100, cursor=cursor)
            assert len(page.notices) <= 100
            seen.extend(item.request_id for item in page.notices)
            pages_seen += 1
            cursor = page.next_cursor
            if cursor is None:
                break
        assert pages_seen == 51
        assert seen == request_ids
        assert len(seen) == 5001

        first_page = store.list_beta_feedback_notices("case_1", "owner-1", limit=1)
        assert first_page.next_cursor is not None
        with pytest.raises(BetaFeedbackConflict, match="cursor"):
            store.list_beta_feedback_notices("case_2", "owner-2", cursor=first_page.next_cursor)
        with pytest.raises(BetaFeedbackConflict, match="cursor"):
            store.list_beta_feedback_notices("case_1", "owner-2", cursor=first_page.next_cursor)
        with pytest.raises(BetaFeedbackConflict, match="cursor"):
            store.list_beta_feedback_notices("case_1", "owner-1", cursor="not-a-cursor")


def test_malformed_schema_and_data_fail_closed_without_repair(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        accepted_one(store)
        receipt = store.record_beta_feedback_notice(
            "case_1", "owner-1", "req-1", pages(("page", REF_A)), NOW
        )
        assert store._require_open().execute(
            "SELECT version FROM pb_store_versions WHERE component='transactions'"
        ).fetchone() == (5,)
    db = sqlite3.connect(path, isolation_level=None)
    db.execute("DROP TRIGGER pb_beta_feedback_notices_no_update")
    db.execute(
        "UPDATE pb_beta_feedback_notices SET payload_sha256=? WHERE notice_id=?",
        ("0" * 64, receipt.notice_id),
    )
    # Restore the canonical immutability trigger so the schema stays valid; the
    # corrupted stored payload must still fail closed on read and list.
    db.execute(
        "CREATE TRIGGER pb_beta_feedback_notices_no_update "
        "BEFORE UPDATE ON pb_beta_feedback_notices "
        "BEGIN SELECT RAISE(ABORT, 'beta feedback notice is immutable'); END"
    )
    db.close()
    with SqliteTransactionStore(path) as store:
        with pytest.raises(StorageCorruption):
            store.beta_feedback_notice("case_1", "owner-1", receipt.notice_id)
        with pytest.raises(StorageCorruption):
            store.list_beta_feedback_notices("case_1", "owner-1")

    for index, mutate in enumerate(
        (
            "DELETE FROM pb_store_versions WHERE component='beta_feedback'",
            "ALTER TABLE pb_beta_feedback_notices ADD COLUMN injected TEXT",
            "DROP TABLE pb_beta_feedback_notices",
        )
    ):
        target = tmp_path / f"broken-{index}.sqlite"
        with SqliteTransactionStore(target) as store:
            accepted_one(store)
        db = sqlite3.connect(target, isolation_level=None)
        db.execute(mutate)
        db.close()
        with pytest.raises(StorageCorruption):
            SqliteTransactionStore(target)


def test_notice_capture_leaves_case_packet_payment_window_entitlement_and_effects_unchanged(
    tmp_path: Path,
) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = accepted_one(store)
        seed_destination(store, case, NOW)
        for event in (
            cs.ResearchComplete(),
            cs.DraftReady(),
            cs.CustomerApproved(),
            cs.SubmitRequested(),
        ):
            case = store.apply_letter_event(
                case.case_token,
                event,
                case.revision,
                occurred_at=NOW,
                new_effect_id=lambda: uuid4().hex,
            )
        packet = store.prepare(case.case_token, 1, make_request(token="beta-token-1"), NOW)
        before = (
            store.get_case(case.case_token),
            store.transition_history(case.case_token),
            store.effects(case.case_token),
            store.payment_cursor(case.case_token),
            store.beta_entitlement("case_1"),
            store.case_hold(case.case_token),
            store.prepare(case.case_token, 1, make_request(token="beta-token-1"), NOW),
        )
        store.record_beta_feedback_notice(
            "case_1",
            "owner-1",
            "req-1",
            pages(("envelope", REF_A), ("page", REF_B)),
            NOW,
            stated_deadline_text="the literal customer words",
        )
        after = (
            store.get_case(case.case_token),
            store.transition_history(case.case_token),
            store.effects(case.case_token),
            store.payment_cursor(case.case_token),
            store.beta_entitlement("case_1"),
            store.case_hold(case.case_token),
            store.prepare(case.case_token, 1, make_request(token="beta-token-1"), NOW),
        )
        assert after == before
        assert before[0] == case
        assert before[6] == packet
        snapshotted_case = before[0]
        assert snapshotted_case is not None
        assert snapshotted_case.payment_state is PaymentState.NO_CHARGE


def _corrupt(database: sqlite3.Connection, statement: str, parameters: tuple[object, ...]) -> None:
    database.execute("BEGIN IMMEDIATE")
    database.execute(statement, parameters)
    database.execute("COMMIT")


def test_malformed_accepted_at_matches_accessor_and_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        accepted_one(store)
        kept = store.record_beta_feedback_notice(
            "case_1", "owner-1", "req-kept", pages(("page", REF_A)), NOW
        )
        entitlement = store.beta_entitlement("case_1")
        assert entitlement is not None
        assert entitlement.accepted_at is not None
    raw = sqlite3.connect(path, isolation_level=None)
    _corrupt(
        raw,
        "UPDATE pb_beta_entitlements SET accepted_at=? WHERE intake_id=?",
        ("not-a-canonical-timestamp", "case_1"),
    )
    raw.close()
    with SqliteTransactionStore(path) as store:
        with pytest.raises(StorageCorruption):
            store.beta_entitlement("case_1")
        with pytest.raises(StorageCorruption):
            store.record_beta_feedback_notice(
                "case_1", "owner-1", "req-new", pages(("page", REF_B)), NOW
            )
        with pytest.raises(StorageCorruption):
            store.beta_feedback_notice("case_1", "owner-1", kept.notice_id)
        with pytest.raises(StorageCorruption):
            store.list_beta_feedback_notices("case_1", "owner-1")
        rows = (
            store._require_open()
            .execute("SELECT notice_id FROM pb_beta_feedback_notices")
            .fetchall()
        )
        assert rows == [(kept.notice_id,)]
        assert store._require_open().execute(
            "SELECT accepted_at FROM pb_beta_entitlements WHERE intake_id='case_1'"
        ).fetchone() == ("not-a-canonical-timestamp",)


def test_durable_case_workspace_must_match_admitted_intake_and_owner(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        accepted_two(store)
        kept = store.record_beta_feedback_notice(
            "case_1", "owner-1", "req-kept", pages(("envelope", REF_A)), NOW
        )
    raw = sqlite3.connect(path, isolation_level=None)
    case_token, admitted_workspace = raw.execute(
        "SELECT case_token, workspace_token FROM pb_intake_records WHERE intake_id='case_1'"
    ).fetchone()
    other_workspace = raw.execute(
        "SELECT workspace_token FROM pb_intake_records WHERE intake_id='case_2'"
    ).fetchone()[0]
    assert admitted_workspace != other_workspace
    _corrupt(
        raw,
        "UPDATE pb_cases SET workspace_token=? WHERE case_token=?",
        (other_workspace, case_token),
    )
    corrupted = raw.execute(
        "SELECT workspace_token FROM pb_cases WHERE case_token=?", (case_token,)
    ).fetchone()[0]
    assert corrupted == other_workspace
    raw.close()
    with SqliteTransactionStore(path) as store:
        with pytest.raises(StorageCorruption):
            store.record_beta_feedback_notice(
                "case_1", "owner-1", "req-new", pages(("page", REF_B)), NOW
            )
        with pytest.raises(StorageCorruption):
            store.beta_feedback_notice("case_1", "owner-1", kept.notice_id)
        with pytest.raises(StorageCorruption):
            store.list_beta_feedback_notices("case_1", "owner-1")
        # The other real owner cannot adopt the corrupted case either.
        with pytest.raises(IntakeConflict):
            store.record_beta_feedback_notice(
                "case_1", "owner-2", "req-other", pages(("page", REF_C)), NOW
            )
        assert store._require_open().execute(
            "SELECT COUNT(*) FROM pb_beta_feedback_notices"
        ).fetchone() == (1,)
        assert store._require_open().execute(
            "SELECT workspace_token FROM pb_cases WHERE case_token=?", (case_token,)
        ).fetchone() == (other_workspace,)


def test_same_name_noop_or_missing_immutability_trigger_fails_closed(tmp_path: Path) -> None:
    for action in ("UPDATE", "DELETE"):
        target = tmp_path / f"trigger-{action.lower()}.sqlite"
        with SqliteTransactionStore(target) as store:
            accepted_one(store)
            store.record_beta_feedback_notice(
                "case_1", "owner-1", "req-1", pages(("page", REF_A)), NOW
            )
        raw = sqlite3.connect(target, isolation_level=None)
        name = f"pb_beta_feedback_notices_no_{action.lower()}"
        raw.execute(f"DROP TRIGGER {name}")
        raw.execute(
            f"CREATE TRIGGER {name} BEFORE {action} ON pb_beta_feedback_notices BEGIN SELECT 1; END"
        )
        noop_sql = raw.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)
        ).fetchone()[0]
        raw.close()
        with pytest.raises(StorageCorruption, match="immutability trigger"):
            SqliteTransactionStore(target)
        raw = sqlite3.connect(target, isolation_level=None)
        assert raw.execute("SELECT COUNT(*) FROM pb_beta_feedback_notices").fetchone() == (1,)
        assert (
            raw.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)
            ).fetchone()[0]
            == noop_sql
        )
        raw.close()

    missing = tmp_path / "trigger-missing.sqlite"
    with SqliteTransactionStore(missing) as store:
        accepted_one(store)
        store.record_beta_feedback_notice("case_1", "owner-1", "req-1", pages(("page", REF_A)), NOW)
    raw = sqlite3.connect(missing, isolation_level=None)
    raw.execute("DROP TRIGGER pb_beta_feedback_notices_no_delete")
    raw.close()
    with pytest.raises(StorageCorruption, match="immutability trigger"):
        SqliteTransactionStore(missing)
    raw = sqlite3.connect(missing, isolation_level=None)
    assert raw.execute("SELECT COUNT(*) FROM pb_beta_feedback_notices").fetchone() == (1,)
    assert raw.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' "
        "AND name='pb_beta_feedback_notices_no_delete'"
    ).fetchone() == (0,)
    raw.close()


def test_public_notice_cursor_validates_owned_record_and_resumes_order(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        accepted_two(store)
        first = store.record_beta_feedback_notice(
            "case_1", "owner-1", "req-1", pages(("envelope", REF_A)), NOW
        )
        second = store.record_beta_feedback_notice(
            "case_1", "owner-1", "req-2", pages(("page", REF_B)), NOW
        )
        foreign = store.record_beta_feedback_notice(
            "case_2", "owner-2", "req-1", pages(("page", REF_C)), NOW
        )
        count = (
            store._require_open()
            .execute("SELECT COUNT(*) FROM pb_beta_feedback_notices")
            .fetchone()
        )
        assert count == (3,)

        internal = store.beta_feedback_notice_cursor("case_1", "owner-1", first.notice_id)
        # The public notice id translates to the unchanged internal sequence schema.
        assert internal.startswith("bfc_")
        resumed = store.list_beta_feedback_notices("case_1", "owner-1", limit=1, cursor=internal)
        assert [item.notice_id for item in resumed.notices] == [second.notice_id]
        assert resumed.next_cursor is None
        # Reading a public cursor is strictly read-only.
        assert (
            store._require_open()
            .execute("SELECT COUNT(*) FROM pb_beta_feedback_notices")
            .fetchone()
            == count
        )

        malformed = (
            "not-a-notice",
            "",
            first.notice_id.upper(),
            first.notice_id[:31],
            first.notice_id + "0",
        )
        for bad in malformed:
            with pytest.raises(BetaFeedbackConflict, match="cursor"):
                store.beta_feedback_notice_cursor("case_1", "owner-1", bad)
        with pytest.raises(IntakeConflict, match="not_found"):
            store.beta_feedback_notice_cursor("case_1", "owner-1", "0" * 32)
        with pytest.raises(IntakeConflict, match="not_found"):
            store.beta_feedback_notice_cursor("case_2", "owner-2", first.notice_id)
        with pytest.raises(IntakeConflict, match="not_found"):
            store.beta_feedback_notice_cursor("case_1", "owner-2", first.notice_id)
        with pytest.raises(IntakeConflict, match="not_found"):
            store.beta_feedback_notice_cursor("case_1", "owner-1", foreign.notice_id)
