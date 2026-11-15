"""Independent approval-identity regressions at storage and crash boundaries."""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from parkingbreaker.api import schemas as s
from parkingbreaker.approval_manifest import ApprovalManifest, build_approval_manifest
from parkingbreaker.case_state import CaseState, CustomerApproved, Event
from parkingbreaker.transaction_store import SqliteTransactionStore
from test_audit_approval_manifest import _sample_request
from test_case_service import (
    NOW,
    approve_with_preview,
    drafting_case,
    fake_resolution,
    service,
)

assert fake_resolution


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.parametrize("dispatched", [False, True])
@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("case_token", "other"),
        ("attempt", 2),
        ("case_id", "other"),
        ("approved_at_utc", "2026-11-15T00:00:00+00:00"),
        ("build_commit", "abcdef0"),
        ("raw_customer_text_sha256", "0" * 64),
        ("approved_text_sha256", "0" * 64),
        ("packet_pdf_sha256", "0" * 64),
        ("envelope_sha256", "0" * 64),
        ("affirmations_json", '[["other","hash"]]'),
        ("affirmations_sha256", "0" * 64),
        ("manifest_sha256", "0" * 64),
    ],
)
def test_approval_identity_cannot_be_changed(
    tmp_path: Path, column: str, value: str | int, dispatched: bool
) -> None:
    req = _sample_request()
    manifest = build_approval_manifest(
        case_id="synthetic_case",
        case_token="synthetic_token",
        attempt=1,
        approved_at=datetime(2026, 11, 14, tzinfo=UTC),
        build_commit="ee3aa46",
        raw_customer_text="My words.",
        approved_text="My words.",
        request=req,
        affirmations=[],
    )
    with SqliteTransactionStore(tmp_path / "immutable.sqlite") as store:
        store.record_approval_manifest(manifest)
        if dispatched:
            store.record_dispatch_manifest(
                "synthetic_token",
                1,
                provider_letter_id="ltr_synthetic",
                expected_delivery=None,
                dispatched_pdf=req.pdf,
                dispatched_at=datetime(2026, 11, 14, 0, 1, tzinfo=UTC),
            )
        before = store.get_approval_manifest("synthetic_token", 1)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            # Column names come only from the fixed parametrization above.
            store._require_open().execute(
                f"UPDATE pb_approval_manifests SET {column} = ? WHERE case_token = ?",
                (value, "synthetic_token"),
            )
        assert store.get_approval_manifest("synthetic_token", 1) == before


def test_replace_cannot_delete_and_reinsert_an_approval(tmp_path: Path) -> None:
    req = _sample_request()
    manifest = build_approval_manifest(
        case_id="synthetic_case",
        case_token="synthetic_token",
        attempt=1,
        approved_at=NOW,
        build_commit=None,
        raw_customer_text="Account",
        approved_text="Account",
        request=req,
        affirmations=[],
    )
    with SqliteTransactionStore(tmp_path / "replacement.sqlite") as store:
        store.record_approval_manifest(manifest)
        db = store._require_open()
        assert db.execute("PRAGMA recursive_triggers").fetchone() == (0,)
        row = list(db.execute("SELECT * FROM pb_approval_manifests").fetchone())
        row[2] = "replacement_case"
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute(
                "INSERT OR REPLACE INTO pb_approval_manifests VALUES ("
                + ",".join("?" for _ in row)
                + ")",
                row,
            )
        assert store.get_approval_manifest("synthetic_token", 1) == manifest


@pytest.mark.anyio
@pytest.mark.usefixtures("fake_resolution")
@pytest.mark.parametrize("boundary", ["before_manifest", "after_manifest"])
async def test_crash_replay_preserves_approval_across_time_and_build_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    monkeypatch.setenv("PB_BUILD_COMMIT", "ee3aa46")
    client, real, case = await drafting_case(tmp_path, monkeypatch)
    try:
        real.step(case, s.WorkerStep(step_id="draft"))
        draft = real.draft(case)
        real.adopt_text(case, s.AdoptText(text=draft.text, text_sha256=draft.text_sha256))
        approval = approve_with_preview(real, case, draft.text_sha256)
        if boundary == "before_manifest":

            def crash_before(manifest: ApprovalManifest) -> ApprovalManifest:
                raise RuntimeError("synthetic crash")

            monkeypatch.setattr(real._store, "record_approval_manifest", crash_before)
        else:
            original_apply = real._apply

            def crash_after(token: str, event: Event) -> None:
                if isinstance(event, CustomerApproved):
                    raise RuntimeError("synthetic crash")
                original_apply(token, event)

            monkeypatch.setattr(real, "_apply", crash_after)
        with pytest.raises(RuntimeError, match="synthetic crash"):
            real.approve(case, approval)
        token = real._intake(case).case_token
        assert token is not None
        prepared = real._store.get(token, 1)
        assert prepared is not None
        prior = real._store.get_approval_manifest(token, 1)
        monkeypatch.setenv("PB_BUILD_COMMIT", "abcdef0")
        restarted = service(tmp_path / "pb.sqlite", clock=lambda: NOW + timedelta(seconds=5))
        object.__setattr__(restarted._deps, "photos", real._deps.photos)
        assert restarted.approve(case, approval).state is CaseState.FROZEN
        actual = restarted._store.get_approval_manifest(token, 1)
        assert actual is not None
        assert actual.approved_at_utc == prepared.prepared_at.astimezone(UTC).isoformat()
        if prior is not None:
            assert actual == prior
        assert restarted._store.get(token, 1) == prepared
    finally:
        await client.aclose()
