"""Tests for audit A031, A032, A034, A035, A085: approval & dispatch manifest."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from mail_samples import seed_destination
from parkingbreaker import case_state as cs
from parkingbreaker import retention_plan as rp
from parkingbreaker import retention_purge as rpurge
from parkingbreaker import retention_state as rs
from parkingbreaker.address_policy import (
    Responsibility,
    ResponsibilityDecision,
)
from parkingbreaker.agencies import MailingAddress
from parkingbreaker.approval_manifest import (
    ApprovalIntegrityError,
    ApprovalManifest,
    build_approval_manifest,
    build_remail_approval_manifest,
    verify_dispatch_integrity,
)
from parkingbreaker.beta_contact_store import initialize_beta_contact
from parkingbreaker.lob_fakes import FakeLobClient
from parkingbreaker.lob_submission import LetterEvidence, LetterOptions, LetterRequest, LobResponse
from parkingbreaker.mail_config import MailClass
from parkingbreaker.retention_policy import ClosureKind, PolicyAdoption, UnresolvedBlockers
from parkingbreaker.returned_mail import Remedy, RemedyKind, ReturnCause
from parkingbreaker.transaction_store import SqliteTransactionStore
from parkingbreaker.workspace import Workspace
from test_case_service import ALICE, NOW, fake_resolution
from test_effect_dispatch import WORKER, approved

assert fake_resolution

_PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def _sample_request(
    pdf: bytes = _PDF,
    to_line1: str = "123 City Hall",
    submission_token: str = "sub_test_1",
) -> LetterRequest:
    return LetterRequest(
        submission_token=submission_token,
        pdf=pdf,
        sender=MailingAddress("Jane Doe", None, "456 Oak St", None, "San Francisco", "CA", "94102"),
        recipient=MailingAddress(
            "Citation Processing", None, to_line1, None, "San Francisco", "CA", "94103"
        ),
        options=LetterOptions(MailClass.CERTIFIED),
    )


def test_build_manifest_records_utc_timestamp_hashes_and_build_commit() -> None:
    pst = timezone(timedelta(hours=-8))
    approved_at = datetime(2026, 11, 12, 14, 0, 0, tzinfo=pst)
    req = _sample_request()
    manifest = build_approval_manifest(
        case_id="case_123",
        case_token="tok_123",
        attempt=1,
        approved_at=approved_at,
        build_commit="ee3aa46",
        raw_customer_text="Meter was broken.",
        approved_text="Meter was broken.",
        request=req,
        affirmations=[("declaration-1", hashlib.sha256(b"I declare").hexdigest())],
    )
    assert manifest.approved_at_utc == "2026-11-12T22:00:00+00:00"
    assert manifest.build_commit == "ee3aa46"
    assert manifest.case_id == "case_123"
    assert manifest.packet_pdf_sha256 == hashlib.sha256(_PDF).hexdigest()
    verify_dispatch_integrity(
        manifest, req, [("declaration-1", hashlib.sha256(b"I declare").hexdigest())]
    )


def test_naive_datetime_rejected_for_approval_timestamp() -> None:
    req = _sample_request()
    with pytest.raises(ValueError, match="UTC"):
        build_approval_manifest(
            case_id="case_123",
            case_token="tok_123",
            attempt=1,
            approved_at=datetime(2026, 11, 12, 14, 0, 0),
            build_commit=None,
            raw_customer_text="Note",
            approved_text="Note",
            request=req,
            affirmations=[],
        )


def test_one_byte_pdf_address_or_affirmation_mutation_fails_verification() -> None:
    req = _sample_request()
    affs = [("declaration-1", hashlib.sha256(b"I declare").hexdigest())]
    manifest = build_approval_manifest(
        case_id="case_123",
        case_token="tok_123",
        attempt=1,
        approved_at=datetime(2026, 11, 12, 22, 0, 0, tzinfo=UTC),
        build_commit="ee3aa46",
        raw_customer_text="Note",
        approved_text="Note",
        request=req,
        affirmations=affs,
    )

    mutated_pdf = _PDF[:-1] + b"X"
    with pytest.raises(ApprovalIntegrityError, match="packet_pdf_mismatch"):
        verify_dispatch_integrity(manifest, _sample_request(pdf=mutated_pdf), affs)

    with pytest.raises(ApprovalIntegrityError, match="envelope_mismatch"):
        verify_dispatch_integrity(manifest, _sample_request(to_line1="124 City Hall"), affs)

    with pytest.raises(ApprovalIntegrityError, match="affirmations_mismatch"):
        verify_dispatch_integrity(
            manifest, req, [("declaration-1", hashlib.sha256(b"I declare!").hexdigest())]
        )


def test_store_persists_approval_and_dispatch_manifest(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    req = _sample_request()
    affs = [("declaration-1", hashlib.sha256(b"I declare").hexdigest())]
    manifest = build_approval_manifest(
        case_id="case_123",
        case_token="tok_123",
        attempt=1,
        approved_at=datetime(2026, 11, 12, 22, 0, 0, tzinfo=UTC),
        build_commit="ee3aa46",
        raw_customer_text="Note",
        approved_text="Note",
        request=req,
        affirmations=affs,
    )
    with SqliteTransactionStore(db_path) as store:
        store.record_approval_manifest(manifest)
        loaded = store.get_approval_manifest("tok_123", 1)
        assert loaded == manifest

        dispatched = store.record_dispatch_manifest(
            "tok_123",
            1,
            provider_letter_id="ltr_abc",
            expected_delivery="2026-11-18",
            dispatched_pdf=_PDF,
            dispatched_at=datetime(2026, 11, 12, 22, 5, 0, tzinfo=UTC),
        )
        assert dispatched.provider_letter_id == "ltr_abc"
        assert dispatched.dispatched_pdf_sha256 == hashlib.sha256(_PDF).hexdigest()
        assert dispatched.dispatched_at_utc == "2026-11-12T22:05:00+00:00"

        with pytest.raises(ApprovalIntegrityError, match="dispatched_pdf_mismatch"):
            store.record_dispatch_manifest(
                "tok_123",
                1,
                provider_letter_id="ltr_bad",
                expected_delivery=None,
                dispatched_pdf=_PDF + b"!",
                dispatched_at=datetime(2026, 11, 12, 22, 6, 0, tzinfo=UTC),
            )


def test_approval_manifest_append_only_triggers_and_dispatch_idempotency(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "append_only.db"
    req = _sample_request()
    affs = [("declaration-1", hashlib.sha256(b"I declare").hexdigest())]
    manifest = build_approval_manifest(
        case_id="case_append",
        case_token="tok_append",
        attempt=1,
        approved_at=datetime(2026, 11, 12, 22, 0, 0, tzinfo=UTC),
        build_commit="ee3aa46",
        raw_customer_text="Note",
        approved_text="Note",
        request=req,
        affirmations=affs,
    )
    with SqliteTransactionStore(db_path) as store:
        store.record_approval_manifest(manifest)
        db = store._require_open()
        with pytest.raises(sqlite3.IntegrityError, match="approval manifests are append-only"):
            db.execute("DELETE FROM pb_approval_manifests WHERE case_token = ?", ("tok_append",))

        first = store.record_dispatch_manifest(
            "tok_append",
            1,
            provider_letter_id="ltr_1",
            expected_delivery="2026-11-18",
            dispatched_pdf=_PDF,
            dispatched_at=datetime(2026, 11, 12, 22, 5, 0, tzinfo=UTC),
        )
        replay = store.record_dispatch_manifest(
            "tok_append",
            1,
            provider_letter_id="ltr_1",
            expected_delivery="2026-11-18",
            dispatched_pdf=_PDF,
            dispatched_at=datetime(2026, 11, 12, 22, 6, 0, tzinfo=UTC),
        )
        assert replay == first

        with pytest.raises(ApprovalIntegrityError, match="dispatch_manifest_conflict"):
            store.record_dispatch_manifest(
                "tok_append",
                1,
                provider_letter_id="ltr_other",
                expected_delivery="2026-11-18",
                dispatched_pdf=_PDF,
                dispatched_at=datetime(2026, 11, 12, 22, 6, 0, tzinfo=UTC),
            )

        with pytest.raises(
            sqlite3.IntegrityError, match="approval manifest dispatch already recorded"
        ):
            db.execute(
                "UPDATE pb_approval_manifests SET provider_letter_id = 'ltr_bad' "
                "WHERE case_token = ?",
                ("tok_append",),
            )


def test_remail_manifest_preserves_customer_hashes_and_updates_envelope(tmp_path: Path) -> None:
    req1 = _sample_request()
    affs = [("declaration-1", hashlib.sha256(b"I declare").hexdigest())]
    t0 = datetime(2026, 11, 12, 22, 0, 0, tzinfo=UTC)
    manifest1 = build_approval_manifest(
        case_id="case_remail",
        case_token="tok_remail",
        attempt=1,
        approved_at=t0,
        build_commit="ee3aa46",
        raw_customer_text="Raw text",
        approved_text="Approved text",
        request=req1,
        affirmations=affs,
    )
    req2 = _sample_request(
        pdf=_PDF + b"%remail\n",
        to_line1="456 Appeal Ave",
        submission_token="sub_test_2",
    )
    t1 = t0 + timedelta(days=2)
    manifest2 = build_remail_approval_manifest(manifest1, req2, approved_at=t1)
    assert manifest2.attempt == 2
    assert manifest2.case_id == manifest1.case_id
    assert manifest2.raw_customer_text_sha256 == manifest1.raw_customer_text_sha256
    assert manifest2.approved_text_sha256 == manifest1.approved_text_sha256
    assert manifest2.affirmations_sha256 == manifest1.affirmations_sha256
    assert manifest2.packet_pdf_sha256 != manifest1.packet_pdf_sha256
    assert manifest2.envelope_sha256 != manifest1.envelope_sha256
    assert manifest2.approved_at_utc == t1.isoformat()
    verify_dispatch_integrity(manifest2, req2, affs)

    db_path = tmp_path / "remail.db"
    with SqliteTransactionStore(db_path) as store:
        store.create_owned_workspace("alice", "ws_r", Workspace(notes="Raw text"), t0)
        case = store.start_case("ws_r", "auth_r", t0, t0 + timedelta(days=1))
        seed_destination(store, case, t0, address=req1.recipient)
        store._apply_event(
            case.case_token, cs.ResearchComplete(), 1, True, t0, (), manage_transaction=True
        )
        store._apply_event(
            case.case_token, cs.DraftReady(), 2, True, t0, (), manage_transaction=True
        )
        store.prepare(case.case_token, 1, req1, t0)
        m1 = build_approval_manifest(
            case_id=case.case_token,
            case_token=case.case_token,
            attempt=1,
            approved_at=t0,
            build_commit="ee3aa46",
            raw_customer_text="Raw text",
            approved_text="Approved text",
            request=req1,
            affirmations=affs,
        )
        store.record_approval_manifest(m1)
        store._apply_event(
            case.case_token, cs.CustomerApproved(), 3, True, t0, (), manage_transaction=True
        )
        store._apply_event(
            case.case_token,
            cs.SubmitRequested(),
            4,
            True,
            t0,
            (),
            manage_transaction=True,
            new_effect_id=lambda: uuid4().hex,
        )
        store._apply_event(
            case.case_token,
            cs.SubmitConfirmed("ltr_1"),
            5,
            True,
            t0,
            (),
            manage_transaction=True,
            new_effect_id=lambda: uuid4().hex,
        )
        store._apply_event(
            case.case_token,
            cs.ReturnConfirmed("VACANT"),
            6,
            True,
            t0 + timedelta(days=1),
            (),
            manage_transaction=True,
        )
        returned_case = store.get_case(case.case_token)
        assert returned_case is not None
        seed_destination(store, returned_case, t1, address=req2.recipient)
        resp = ResponsibilityDecision(
            Responsibility.OUR_ERROR_FULL_REFUND, "OUR_ERROR", ReturnCause.OUR_ERROR
        )
        remedy = Remedy(RemedyKind.FREE_REMAIL)
        store.resolve_returned_mail(
            case.case_token,
            returned_case.revision,
            resp,
            remedy,
            req2,
            now=t1,
        )
        loaded_m2 = store.get_approval_manifest(case.case_token, 2)
        assert loaded_m2 is not None
        assert loaded_m2.attempt == 2
        verify_dispatch_integrity(loaded_m2, req2, affs)


@pytest.mark.anyio
async def test_dispatch_verifies_envelope_and_records_acceptance_and_crash_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ok_dir = tmp_path / "ok"
    ok_dir.mkdir()
    lob_ok = FakeLobClient([LobResponse(200, "ltr_verified_1", evidence=LetterEvidence.PRODUCTION)])
    client, real, case = await approved(ok_dir, monkeypatch, lob_ok)
    try:
        db = real._store._require_open()
        row = db.execute(
            "SELECT case_token FROM pb_approval_manifests WHERE case_id = ?", (case,)
        ).fetchone()
        assert row is not None
        case_token = str(row[0])
        manifest_before = real._store.get_approval_manifest(case_token, 1)
        assert manifest_before is not None
        # Advancing clock and replaying approve recovers the identical manifest.
        object.__setattr__(real._deps, "clock", lambda: NOW + timedelta(minutes=5))
        draft = (await client.get(f"/cases/{case}/draft", headers=ALICE)).json()
        replay_resp = await client.post(
            f"/cases/{case}/approval",
            headers=ALICE,
            json={
                "adopted_sha256": draft["adopted_sha256"],
                "packet_sha256": manifest_before.packet_pdf_sha256,
                "envelope_sha256": manifest_before.envelope_sha256,
                "affirmations": [
                    {
                        "declaration_id": decl_id,
                        "text_sha256": text_sha256,
                        "affirmed": True,
                    }
                    for decl_id, text_sha256 in manifest_before.affirmations
                ],
            },
        )
        assert replay_resp.status_code == 200
        assert real._store.get_approval_manifest(case_token, 1) == manifest_before

        # Dispatch records provider_letter_id and dispatched_pdf_sha256 on acceptance.
        dispatch = await client.post(
            f"/internal/cases/{case}/step", headers=WORKER, json={"step_id": "dispatch"}
        )
        assert dispatch.status_code == 200
        assert len(lob_ok.calls) == 1
        dispatched_manifest = real._store.get_approval_manifest(case_token, 1)
        assert dispatched_manifest is not None
        assert dispatched_manifest.provider_letter_id == "ltr_verified_1"
        assert dispatched_manifest.dispatched_pdf_sha256 == manifest_before.packet_pdf_sha256
        assert dispatched_manifest.dispatched_at_utc is not None
        assert db.execute(
            "SELECT dispatched_pdf_sha256 FROM pb_approval_manifests WHERE case_token = ?",
            (case_token,),
        ).fetchone() == (manifest_before.packet_pdf_sha256,)
    finally:
        await client.aclose()

    tamper_dir = tmp_path / "tamper"
    tamper_dir.mkdir()
    lob_blocked = FakeLobClient(
        [LobResponse(200, "ltr_should_not_run", evidence=LetterEvidence.PRODUCTION)]
    )
    client2, real2, case2 = await approved(tamper_dir, monkeypatch, lob_blocked)
    try:
        # Model a corrupted read independently of SQL immutability protections.
        original_get = real2._store.get_approval_manifest

        def corrupt_read(token: str, attempt: int) -> ApprovalManifest | None:
            manifest = original_get(token, attempt)
            return None if manifest is None else replace(manifest, envelope_sha256="f" * 64)

        monkeypatch.setattr(real2._store, "get_approval_manifest", corrupt_read)
        blocked = await client2.post(
            f"/internal/cases/{case2}/step", headers=WORKER, json={"step_id": "dispatch"}
        )
        assert blocked.status_code == 200
        assert not lob_blocked.calls
    finally:
        await client2.aclose()


class _NoopBlobDeleteStore:
    def get(self, owner: str, ref: str) -> bytes | None:
        return None

    def delete(self, owner: str, ref: str) -> None:
        return None


@pytest.mark.parametrize("dispatch_after_plan", [False, True])
def test_retention_plan_purge_and_restore_cover_approval_manifests(
    tmp_path: Path, dispatch_after_plan: bool
) -> None:
    t0 = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
    evaluated_at = datetime(2028, 1, 1, 0, 0, tzinfo=UTC)
    db_path = tmp_path / "retention.sqlite"
    with SqliteTransactionStore(db_path) as store:
        db = store._require_open()
        initialize_beta_contact(db)
        rs.initialize_retention_state(db)
        store.create_owned_workspace("alice", "ws_ret", Workspace(notes="Story"), t0)
        case = store.start_case("ws_ret", "auth_ret", t0, t0 + timedelta(days=1))
        db.execute(
            "UPDATE pb_cases SET letter_state = 'DELIVERED', payment_state = 'CAPTURED' "
            "WHERE case_token = ?",
            (case.case_token,),
        )
        db.execute(
            "INSERT INTO pb_intake_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "intake_ret",
                "ws_ret",
                json.dumps({"ticket": {"citation_number": "C1"}}),
                json.dumps({"stop": "none"}),
                None,
                "STANDARD",
                case.case_token,
                t0.isoformat(),
                1,
            ),
        )
        manifest = build_approval_manifest(
            case_id=case.case_token,
            case_token=case.case_token,
            attempt=1,
            approved_at=t0,
            build_commit="ee3aa46",
            raw_customer_text="Story",
            approved_text="Story",
            request=_sample_request(),
            affirmations=[],
        )
        store.record_approval_manifest(manifest)
        rs.record_policy(
            db,
            command_id="pol-1",
            adoption=PolicyAdoption.ADOPTED,
            timezone=ZoneInfo("UTC"),
            at=t0,
            adoption_evidence_ref="ev-1",
        )
        rs.record_closure(
            db,
            command_id="clo-1",
            case_token=case.case_token,
            kind=ClosureKind.CLOSED,
            anchor_at=t0 + timedelta(days=2),
            source_ref="src-1",
            at=t0,
        )
        plan = rp.plan_service_data_retention(
            db,
            case_token=case.case_token,
            evaluated_at=evaluated_at,
            blockers=UnresolvedBlockers(),
        )
        assert plan.summary.unknown_table_count == 0
        assert plan.summary.eligible is True

        targets = [t for t in plan.targets if t.category is rp.TargetCategory.APPROVAL_MANIFEST]
        assert len(targets) == 1
        assert targets[0].content_sha256
        if dispatch_after_plan:
            store.record_dispatch_manifest(
                case.case_token,
                1,
                provider_letter_id="ltr_synthetic",
                expected_delivery=None,
                dispatched_pdf=_sample_request().pdf,
                dispatched_at=t0 + timedelta(hours=1),
            )
            with pytest.raises(rpurge.RetentionPurgeRefused, match="content_drift"):
                rpurge.execute_retention_purge(
                    db,
                    plan,
                    synthetic=True,
                    planned_at=evaluated_at,
                    evaluated_at=evaluated_at,
                    blockers=UnresolvedBlockers(),
                    blob_store=_NoopBlobDeleteStore(),
                    batch_size=1,
                )
            assert store.get_approval_manifest(case.case_token, 1) is not None
            plan = rp.plan_service_data_retention(
                db,
                case_token=case.case_token,
                evaluated_at=evaluated_at,
                blockers=UnresolvedBlockers(),
            )

        # Snapshot pre-purge DB into a separate in-memory backup before purging authority.
        backup = sqlite3.connect(":memory:")
        db.backup(backup)
        try:
            receipt = rpurge.execute_retention_purge(
                db,
                plan,
                synthetic=True,
                planned_at=evaluated_at,
                evaluated_at=evaluated_at,
                blockers=UnresolvedBlockers(),
                blob_store=_NoopBlobDeleteStore(),
                batch_size=1,
            )
            while receipt.status == "database_pending":
                receipt = rpurge.execute_retention_purge(
                    db,
                    plan,
                    synthetic=True,
                    planned_at=evaluated_at,
                    evaluated_at=evaluated_at,
                    blockers=UnresolvedBlockers(),
                    blob_store=_NoopBlobDeleteStore(),
                    batch_size=1,
                )
            assert receipt.status == "scoped_complete"
            assert store.get_approval_manifest(case.case_token, 1) is None

            # Purged case cannot re-insert an approval manifest.
            with pytest.raises(sqlite3.IntegrityError, match="purged case is fenced"):
                store.record_approval_manifest(manifest)

            # A backup smuggling an orphan pb_approval_manifests row is rejected on restore.
            backup.execute(
                "DELETE FROM pb_cases WHERE case_token = ?",
                (case.case_token,),
            )
            backup.commit()
            with pytest.raises(rpurge.RetentionPurgeRefused, match="restore_excluded"):
                rpurge.validate_retention_restore(backup, db, evaluated_at=evaluated_at)
        finally:
            backup.close()
