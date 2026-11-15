"""Tests for answer-key evidence verification, missing-page fetch, and atomic sign-off."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from parkingbreaker.bench.__main__ import main, parse_arguments
from parkingbreaker.bench.cases import Evidence, parse_answer_key
from parkingbreaker.bench.key_review import (
    EvidenceStatus,
    audit_key_row,
    load_saved_pages,
    sign_key_rows,
    write_answer_key_atomic,
)
from parkingbreaker.research.fakes import FakeFetcher, fixed_clock, html_response

NOW = datetime(2026, 10, 27, 18, 0, tzinfo=UTC)


def test_verify_keys_fetches_and_saves_missing_page(tmp_path: Path) -> None:
    key_path = Path(__file__).resolve().parents[2] / "docs/bench/sf-answer-key.jsonl"
    s01 = parse_answer_key(key_path.read_text(encoding="utf-8"))[0]
    url = "https://www.sfmta.com/contest"
    quote = "Parking or Transit Violation Citations must be protested within 21 days."
    html = f"<html><body><p>{quote}</p></body></html>"
    sha = hashlib.sha256(html.encode("utf-8")).hexdigest()
    row = replace(
        s01,
        evidence=(
            Evidence(
                url=url,
                retrieved_at="2026-10-27T12:00:00+00:00",
                sha256=sha,
                quote=quote,
                supports=("expected.deadline",),
            ),
        ),
    )
    fetcher = FakeFetcher({url: html_response(url, html)})
    pages: dict[str, str] = {}
    audit = audit_key_row(row, pages, fetcher=fetcher, clock=fixed_clock(NOW), save_dir=tmp_path)

    assert audit.evidence_complete is True
    assert audit.ready_for_signoff is False  # S01 still has unresolved semantic fields.
    assert audit.evidence_audits[0].status is EvidenceStatus.FETCHED_VERIFIED
    assert (tmp_path / f"{sha}.txt").is_file()
    loaded = load_saved_pages((tmp_path,))
    assert sha in loaded


def test_verify_keys_reports_hash_mismatch_when_live_page_drifted(tmp_path: Path) -> None:
    key_path = Path(__file__).resolve().parents[2] / "docs/bench/sf-answer-key.jsonl"
    s01 = parse_answer_key(key_path.read_text(encoding="utf-8"))[0]
    url = "https://www.sfmta.com/contest"
    quote = "Parking or Transit Violation Citations must be protested within 21 days."
    new_html = f"<html><body><p>{quote} Updated banner.</p></body></html>"
    new_sha = hashlib.sha256(new_html.encode("utf-8")).hexdigest()
    old_sha = "a" * 64
    row = replace(
        s01,
        evidence=(
            Evidence(
                url=url,
                retrieved_at="2026-10-15T12:00:00+00:00",
                sha256=old_sha,
                quote=quote,
                supports=("expected.deadline",),
            ),
        ),
    )
    fetcher = FakeFetcher({url: html_response(url, new_html)})
    audit = audit_key_row(row, {}, fetcher=fetcher, clock=fixed_clock(NOW), save_dir=tmp_path)

    assert audit.ready_for_signoff is False
    ev_audit = audit.evidence_audits[0]
    assert ev_audit.status is EvidenceStatus.HASH_MISMATCH
    assert ev_audit.actual_sha256 == new_sha
    assert ev_audit.quote_in_drifted_page is True
    assert any("hash mismatch" in r for r in audit.blocking_reasons)


def test_sign_key_refuses_unverified_or_missing_evidence_rows_and_writes_atomically(
    tmp_path: Path,
) -> None:
    sf_path = Path(__file__).resolve().parents[2] / "docs/bench/sf-answer-key.jsonl"
    b_path = Path(__file__).resolve().parents[2] / "docs/bench/answer-key.jsonl"
    sf_rows = parse_answer_key(sf_path.read_text(encoding="utf-8"))
    b_rows = parse_answer_key(b_path.read_text(encoding="utf-8"))

    # B06's partial evidence is unavailable in this test's empty page store.
    b06 = next(r for r in b_rows if r.case_id == "B06")
    with pytest.raises(ValueError, match="sign-key refused for B06"):
        sign_key_rows((b06,), {}, case_ids=("B06",), owner="Amir", signed_at=NOW)

    # S01 with unverified/missing page is refused
    with pytest.raises(ValueError, match="sign-key refused for S01"):
        sign_key_rows(sf_rows, {}, case_ids=("S01",), owner="Amir", signed_at=NOW)

    # Verified S01 and S05 (private operator BUSINESS_EXCLUSION with empty evidence) succeed.
    s01 = sf_rows[0]
    pages = {
        ev.sha256: "\n\n".join(e.quote for e in s01.evidence if e.sha256 == ev.sha256)
        for ev in s01.evidence
    }
    signed = sign_key_rows(
        sf_rows,
        pages,
        case_ids=("S01", "S05"),
        owner="Amir (owner)",
        signed_at=NOW,
        clear_uncertain=True,
    )
    target_file = tmp_path / "sf-answer-key.jsonl"
    write_answer_key_atomic(target_file, signed)
    reparsed = {r.case_id: r for r in parse_answer_key(target_file.read_text(encoding="utf-8"))}
    assert reparsed["S01"].owner_signed_at == "2026-10-27T18:00:00+00:00"
    assert reparsed["S01"].status == "certain"
    assert reparsed["S05"].owner_signed_at == "2026-10-27T18:00:00+00:00"
    assert reparsed["S02"].owner_signed_at is None


def test_keep_uncertain_preserves_only_existing_named_field() -> None:
    sf_path = Path(__file__).resolve().parents[2] / "docs/bench/sf-answer-key.jsonl"
    row = parse_answer_key(sf_path.read_text(encoding="utf-8"))[0]
    pages = {
        ev.sha256: "\n\n".join(e.quote for e in row.evidence if e.sha256 == ev.sha256)
        for ev in row.evidence
    }
    field = "expected.deadline.counting"
    # The live key may resolve this field; pin it so the test does not depend on key data.
    row = replace(row, uncertain_fields=(field,), status="uncertain")
    args = parse_arguments(
        [
            "sign-key",
            "--cases",
            str(sf_path),
            "--pages-dir",
            "./pages",
            "--case",
            row.case_id,
            "--owner",
            "Amir",
            "--clear-uncertain",
            "--keep-uncertain",
            field,
        ]
    )
    (signed,) = sign_key_rows(
        (row,),
        pages,
        case_ids=args.case,
        owner=args.owner,
        signed_at=NOW,
        clear_uncertain=args.clear_uncertain,
        keep_uncertain_fields=args.keep_uncertain,
    )
    assert signed.uncertain_fields == (field,)
    assert signed.status == "uncertain"
    assert signed.owner_signed_at == NOW.isoformat()
    with pytest.raises(
        ValueError, match="sign-key refused for S01: kept fields were not uncertain"
    ):
        sign_key_rows(
            (row,),
            pages,
            case_ids=(row.case_id,),
            owner="Amir",
            signed_at=NOW,
            clear_uncertain=True,
            keep_uncertain_fields=("expected.required_items",),
        )


def test_cli_sign_key_round_trip_and_atomic_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = Path(__file__).resolve().parents[2]
    key_path = tmp_path / "sf-answer-key.jsonl"
    original = (root / "docs/bench/sf-answer-key.jsonl").read_bytes()
    key_path.write_bytes(original)
    rows = parse_answer_key(original.decode("utf-8"))
    pages_dir = tmp_path / "saved"
    pages_dir.mkdir()
    for ev in rows[0].evidence:
        page = "\n\n".join(e.quote for e in rows[0].evidence if e.sha256 == ev.sha256)
        (pages_dir / f"{ev.sha256}.txt").write_text(page, encoding="utf-8")
    monkeypatch.setattr(
        "parkingbreaker.bench.__main__.HttpFetcher",
        lambda: pytest.fail("offline key commands must not construct a fetcher"),
    )
    packet_path = tmp_path / "review.md"
    assert (
        main(
            [
                "verify-keys",
                "--cases",
                str(key_path),
                "--pages-dir",
                str(tmp_path / "missing"),
                "--save-dir",
                str(pages_dir),
                "--out",
                str(packet_path),
            ]
        )
        == 0
    )
    packet = packet_path.read_text(encoding="utf-8")
    assert all(f"### {row.case_id} " in packet for row in rows)
    assert "[VERIFIED]" in packet
    assert key_path.read_bytes() == original
    assert main(["verify-keys", "--cases", str(key_path), "--out", str(key_path)]) == 2
    assert "must not overwrite the answer key" in capsys.readouterr().err
    assert key_path.read_bytes() == original
    assert (
        main(
            [
                "sign-key",
                "--cases",
                str(key_path),
                "--pages-dir",
                str(pages_dir),
                "--pages-dir",
                str(tmp_path / "missing"),
                "--case",
                "S01",
                "--case",
                "S05",
                "--owner",
                "Amir",
                "--clear-uncertain",
            ]
        )
        == 0
    )
    signed = parse_answer_key(key_path.read_text(encoding="utf-8"))
    for index in (0, 4):
        assert signed[index].key_version == rows[index].key_version + 1
        assert signed[index].owner_signed_at is not None
        assert signed[index].reviewed_by == "Amir"
        assert signed[index].status == "certain"
    assert signed[1] == rows[1]
    b_rows = parse_answer_key((root / "docs/bench/answer-key.jsonl").read_text(encoding="utf-8"))
    b06 = next(row for row in b_rows if row.case_id == "B06")
    write_answer_key_atomic(key_path, (*signed, b06))
    before_refusal = key_path.read_bytes()
    assert (
        main(
            [
                "sign-key",
                "--cases",
                str(key_path),
                "--pages-dir",
                str(pages_dir),
                "--case",
                "S05",
                "--case",
                "B06",
                "--owner",
                "Amir",
                "--status",
                "certain",
            ]
        )
        == 2
    )
    assert "sign-key refused for B06" in capsys.readouterr().err
    assert key_path.read_bytes() == before_refusal


def test_load_saved_pages_raw_html(tmp_path: Path) -> None:
    key_path = Path(__file__).resolve().parents[2] / "docs/bench/sf-answer-key.jsonl"
    row = parse_answer_key(key_path.read_text(encoding="utf-8"))[0]
    quote = row.evidence[0].quote
    html = f"<html><body><p>{quote}</p></body></html>".encode()
    sha = hashlib.sha256(html).hexdigest()
    (tmp_path / sha).write_bytes(html)
    pages = load_saved_pages((tmp_path,))
    assert quote in pages[sha]
    assert "<html>" not in pages[sha]
    assert "<p>" not in pages[sha]


@pytest.mark.parametrize("status", ["uncertain", "excluded"])
def test_matching_quotes_do_not_make_an_unqualified_row_ready(status: str) -> None:
    root = Path(__file__).resolve().parents[2]
    row = parse_answer_key((root / "docs/bench/answer-key.jsonl").read_text())[2]
    # Synthetic completeness: every quote matches, but this does not adjudicate facts.
    pages = {
        ev.sha256: "\n\n".join(e.quote for e in row.evidence if e.sha256 == ev.sha256)
        for ev in row.evidence
    }
    row = replace(row, status="excluded" if status == "excluded" else "uncertain")
    audit = audit_key_row(row, pages)
    assert audit.evidence_complete
    assert not audit.ready_for_signoff
    assert any("semantic review" in reason for reason in audit.blocking_reasons)


def test_certain_status_cannot_hide_unresolved_fields() -> None:
    root = Path(__file__).resolve().parents[2]
    row = parse_answer_key((root / "docs/bench/answer-key.jsonl").read_text())[2]
    pages = {
        ev.sha256: "\n\n".join(e.quote for e in row.evidence if e.sha256 == ev.sha256)
        for ev in row.evidence
    }
    row = replace(row, status="certain", uncertain_fields=("expected.destination",))
    audit = audit_key_row(row, pages)
    assert audit.evidence_complete
    assert not audit.ready_for_signoff
    assert any("expected.destination" in reason for reason in audit.blocking_reasons)
    # This is only a mechanical candidate; a real reviewer still owns field support.
    assert audit_key_row(replace(row, uncertain_fields=()), pages).ready_for_signoff
