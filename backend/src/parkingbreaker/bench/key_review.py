"""Answer-key evidence verification, missing-page fetch, and atomic owner sign-off.

Before bench/score.py can produce final qualification scores, each KeyRow in
answer-key.jsonl and sf-answer-key.jsonl must have its evidence verified against saved
official page text and be signed by the owner (owner_signed_at).
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit

from parkingbreaker.agencies import SourceScope
from parkingbreaker.bench.adjudicate import save_page as save_adjudicated_page
from parkingbreaker.bench.cases import KeyRow, KeyStatus, parse_answer_key
from parkingbreaker.research.findings import verify_quote
from parkingbreaker.research.sources import (
    AuthorityClass,
    Clock,
    Fetcher,
    FetchFailure,
    SourceRecord,
    fetch_source,
    html_to_text,
    plain_to_text,
)


class EvidenceStatus(StrEnum):
    VERIFIED = "VERIFIED"
    FETCHED_VERIFIED = "FETCHED_VERIFIED"
    HASH_MISMATCH = "HASH_MISMATCH"
    QUOTE_MISMATCH = "QUOTE_MISMATCH"
    PAGE_MISSING = "PAGE_MISSING"
    FETCH_FAILED = "FETCH_FAILED"


@dataclass(frozen=True)
class EvidenceAudit:
    url: str
    expected_sha256: str
    actual_sha256: str | None
    status: EvidenceStatus
    quote_verified: bool
    quote_in_drifted_page: bool | None
    supports: tuple[str, ...]
    detail: str = ""


@dataclass(frozen=True)
class RowAudit:
    case_id: str
    key_version: int
    printed_issuer: str
    city_state: str
    stage_text: str
    expected_outcome: str
    status: KeyStatus
    uncertain_fields: tuple[str, ...]
    owner_signed_at: str | None
    evidence_audits: tuple[EvidenceAudit, ...]
    ready_for_signoff: bool
    blocking_reasons: tuple[str, ...]

    @property
    def evidence_complete(self) -> bool:
        """Mechanical quote completeness only, not semantic or capability approval."""
        if not self.evidence_audits:
            return self.expected_outcome == "BUSINESS_EXCLUSION"
        return all(
            item.status in {EvidenceStatus.VERIFIED, EvidenceStatus.FETCHED_VERIFIED}
            and item.quote_verified
            for item in self.evidence_audits
        )


def _scope_for_url(url: str) -> tuple[SourceScope, ...]:
    host = (urlsplit(url).hostname or "").lower().removesuffix(".")
    if not host:
        return ()
    return (SourceScope(host),)


def audit_key_row(
    row: KeyRow,
    saved_pages: dict[str, str],
    *,
    fetcher: Fetcher | None = None,
    clock: Clock | None = None,
    save_dir: Path | None = None,
) -> RowAudit:
    """Verify every Evidence entry on `row`, optionally fetching missing pages.

    - If the expected sha256 is in `saved_pages`, checks `verify_quote` directly.
    - If missing and `fetcher` is provided, fetches `item.url` through `fetch_source`:
      * If fetched `record.sha256 == item.sha256`: saves page text to `save_dir` (if given),
        records it in `saved_pages`, and checks `verify_quote`.
      * If fetched `record.sha256 != item.sha256`: saves the new page under its own sha256,
        reports `EvidenceStatus.HASH_MISMATCH` with `actual_sha256` and whether the quote
        still appears in the new page text, and refuses automatic sign-off readiness.
    """
    audits: list[EvidenceAudit] = []
    blockers: list[str] = []

    for item in row.evidence:
        page_text = saved_pages.get(item.sha256)
        if page_text is not None:
            verified = verify_quote(item.quote, page_text)
            status = EvidenceStatus.VERIFIED if verified else EvidenceStatus.QUOTE_MISMATCH
            if not verified:
                blockers.append(f"quote mismatch on saved page {item.sha256[:12]}")
            audits.append(
                EvidenceAudit(
                    url=item.url,
                    expected_sha256=item.sha256,
                    actual_sha256=item.sha256,
                    status=status,
                    quote_verified=verified,
                    quote_in_drifted_page=None,
                    supports=item.supports,
                )
            )
            continue

        if fetcher is None or clock is None:
            blockers.append(f"missing saved page {item.sha256[:12]} for {item.url}")
            audits.append(
                EvidenceAudit(
                    url=item.url,
                    expected_sha256=item.sha256,
                    actual_sha256=None,
                    status=EvidenceStatus.PAGE_MISSING,
                    quote_verified=False,
                    quote_in_drifted_page=None,
                    supports=item.supports,
                )
            )
            continue

        scopes = _scope_for_url(item.url)
        fetched = fetch_source(item.url, AuthorityClass.OFFICIAL_PROCEDURE, scopes, fetcher, clock)
        if isinstance(fetched, FetchFailure):
            blockers.append(f"fetch failed for {item.url}: {fetched.reason}")
            audits.append(
                EvidenceAudit(
                    url=item.url,
                    expected_sha256=item.sha256,
                    actual_sha256=None,
                    status=EvidenceStatus.FETCH_FAILED,
                    quote_verified=False,
                    quote_in_drifted_page=None,
                    supports=item.supports,
                    detail=fetched.reason,
                )
            )
            continue

        assert isinstance(fetched, SourceRecord)
        if save_dir is not None:
            save_adjudicated_page(fetched, save_dir)
        saved_pages[fetched.sha256] = fetched.text

        if fetched.sha256 != item.sha256:
            in_drifted = verify_quote(item.quote, fetched.text)
            blockers.append(
                f"hash mismatch for {item.url}: key has {item.sha256[:12]}, "
                f"live page is {fetched.sha256[:12]} (quote_present={in_drifted})"
            )
            audits.append(
                EvidenceAudit(
                    url=item.url,
                    expected_sha256=item.sha256,
                    actual_sha256=fetched.sha256,
                    status=EvidenceStatus.HASH_MISMATCH,
                    quote_verified=False,
                    quote_in_drifted_page=in_drifted,
                    supports=item.supports,
                    detail=f"live sha256={fetched.sha256}; quote_present={in_drifted}",
                )
            )
            continue

        verified = verify_quote(item.quote, fetched.text)
        status = EvidenceStatus.FETCHED_VERIFIED if verified else EvidenceStatus.QUOTE_MISMATCH
        if not verified:
            blockers.append(f"quote mismatch on fetched page {fetched.sha256[:12]}")
        audits.append(
            EvidenceAudit(
                url=item.url,
                expected_sha256=item.sha256,
                actual_sha256=fetched.sha256,
                status=status,
                quote_verified=verified,
                quote_in_drifted_page=None,
                supports=item.supports,
            )
        )

    expected_outcome = (
        f"{row.expected.outcome_family}:{row.expected.hard_stop}"
        if row.expected.hard_stop is not None
        else str(row.expected.outcome_family)
    )
    # Only a scope-level BUSINESS_EXCLUSION without a hard stop (e.g. S05 private operator)
    # may legitimately have zero official evidence rows. Government cases (including B03/B07
    # BUSINESS_EXCLUSION:S3 and B06/B08/B14/B15) require verified official evidence.
    private_scope_exclusion = (
        row.expected.outcome_family == "BUSINESS_EXCLUSION" and row.expected.hard_stop is None
    )
    if not row.evidence and not private_scope_exclusion:
        blockers.append("no official evidence recorded (adjudicate official page before signing)")

    # A matched quotation can support only part of an unresolved route. Never advertise
    # those rows as ready merely because their available evidence now matches.
    if row.status != "certain":
        blockers.append(f"row status is {row.status}; semantic review remains required")
    if row.uncertain_fields:
        blockers.append("unresolved fields: " + ", ".join(row.uncertain_fields))
    ready = not blockers and (private_scope_exclusion or bool(audits))
    return RowAudit(
        case_id=row.case_id,
        key_version=row.key_version,
        printed_issuer=row.notice.printed_issuer,
        city_state=f"{row.notice.customer_city}, {row.notice.customer_state}",
        stage_text=row.notice.stage_text,
        expected_outcome=expected_outcome,
        status=row.status,
        uncertain_fields=row.uncertain_fields,
        owner_signed_at=row.owner_signed_at,
        evidence_audits=tuple(audits),
        ready_for_signoff=ready,
        blocking_reasons=tuple(blockers),
    )


def render_review_packet(audits: Sequence[RowAudit], rows: Sequence[KeyRow]) -> str:
    """Render a human-readable Markdown checklist for owner inspection and sign-off."""
    by_id = {r.case_id: r for r in rows}
    lines = [
        "# ParkingBreaker — Answer-Key Review & Owner Sign-Off Packet",
        "",
        "Matching quotations establish evidence completeness only. Readiness also requires "
        "a certain row with no unresolved fields; field support, stage and fulfilment still "
        "require owner review. This report does not sign or qualify a row.",
        "",
        "| Case | Issuer | Stage | Expected Outcome | Status | Ready? "
        "| Evidence Verified | Owner Signed |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for audit in audits:
        ev_summary = (
            f"{sum(a.quote_verified for a in audit.evidence_audits)}/{len(audit.evidence_audits)}"
            if audit.evidence_audits
            else "0/0 (no page)"
        )
        ready_label = "YES" if audit.ready_for_signoff else "NO"
        signed = audit.owner_signed_at or "UNSIGNED"
        lines.append(
            f"| {audit.case_id} (v{audit.key_version}) "
            f"| {audit.printed_issuer} ({audit.city_state}) "
            f"| {audit.stage_text} | `{audit.expected_outcome}` | `{audit.status}` "
            f"| {ready_label} | {ev_summary} | {signed} |"
        )
    lines.append("")
    lines.append("## Detailed Case Breakdown")
    lines.append("")
    for audit in audits:
        row = by_id[audit.case_id]
        dest = ", ".join((*row.expected.destination.lines, row.expected.destination.zip5))
        dl = row.expected.deadline
        lines.extend(
            [
                f"### {row.case_id} — {row.notice.printed_issuer} "
                f"({row.notice.customer_city}, {row.notice.customer_state})",
                "",
                f"- **Stage:** `{row.notice.stage_text}`",
                f"- **Deciding body:** `{row.expected.deciding_body}` "
                f"(`{row.expected.issuer_type}`)",
                f"- **Expected outcome:** `{audit.expected_outcome}` "
                f"(safe set: `{', '.join(row.expected.safe_set)}`)",
                f"- **Destination:** `{dest}` (purpose: `{row.expected.destination.purpose}`)",
                f"- **Deadline:** `{dl.days}` days from `{dl.trigger}` (`{dl.counting}`)",
                f"- **Required items:** `{', '.join(row.expected.required_items) or 'none'}`",
                "- **Required declarations:** "
                f"`{', '.join(row.expected.required_declarations) or 'none'}`",
                "- **Unfulfillable items:** "
                f"`{', '.join(row.expected.unfulfillable_items) or 'none'}`",
                f"- **Current status:** `{row.status}` | **Uncertain fields:** "
                f"`{', '.join(row.uncertain_fields) or 'none'}`",
                f"- **Ready for sign-off:** `{'YES' if audit.ready_for_signoff else 'NO'}`",
                f"- **Quote evidence complete:** `{'YES' if audit.evidence_complete else 'NO'}`",
                f"- **Notes:** {row.notes}",
            ]
        )
        if audit.blocking_reasons:
            lines.append("- **Blocking Findings:**")
            for reason in audit.blocking_reasons:
                lines.append(f"  - `{reason}`")
        lines.append("")
        if row.evidence:
            lines.append("**Quoted Official Evidence:**")
            for ev, ev_audit in zip(row.evidence, audit.evidence_audits, strict=True):
                detail_suffix = f" — `{ev_audit.detail}`" if ev_audit.detail else ""
                lines.append(
                    f"- [{ev_audit.status.value}] `{ev.url}` (`{ev.sha256[:12]}...`) "
                    f"supports `{', '.join(ev.supports)}`{detail_suffix}:\n"
                    f"  > {ev.quote.replace(chr(10), ' ')}"
                )
            lines.append("")
    return "\n".join(lines)


def sign_key_rows(
    rows: Sequence[KeyRow],
    saved_pages: dict[str, str],
    *,
    case_ids: Sequence[str],
    owner: str,
    signed_at: datetime,
    set_status: KeyStatus | None = None,
    clear_uncertain: bool = False,
    keep_uncertain_fields: Sequence[str] = (),
) -> tuple[KeyRow, ...]:
    """Verify and stamp owner_signed_at on selected case_ids; refuse any unverified row."""
    if not owner.strip():
        raise ValueError("owner must not be blank")
    if signed_at.tzinfo is None or signed_at.utcoffset() is None:
        raise ValueError("signed_at must be timezone-aware")
    by_id = {r.case_id: r for r in rows}
    targets = tuple(dict.fromkeys(case_ids))
    if not targets or any(cid not in by_id for cid in targets):
        raise ValueError("unknown or empty case_ids for sign-off")

    for cid in targets:
        unknown_fields = set(keep_uncertain_fields) - set(by_id[cid].uncertain_fields)
        if unknown_fields:
            raise ValueError(
                f"sign-key refused for {cid}: kept fields were not uncertain: "
                + ", ".join(sorted(unknown_fields))
            )
        audit = audit_key_row(by_id[cid], saved_pages)
        # Preserve the explicit owner's existing adjudication interface (including
        # --clear-uncertain/--keep-uncertain). An offline readiness report must not
        # substitute for that decision, or silently clear any uncertainty itself.
        if not audit.evidence_complete:
            reasons = "; ".join(audit.blocking_reasons) or "evidence not verified"
            raise ValueError(f"sign-key refused for {cid}: {reasons}")

    stamp = signed_at.astimezone(UTC).isoformat()
    target_set = set(targets)
    updated: list[KeyRow] = []
    for row in rows:
        if row.case_id not in target_set:
            updated.append(row)
            continue
        new_uncertain = tuple(keep_uncertain_fields) if clear_uncertain else row.uncertain_fields
        new_status: KeyStatus = (
            set_status
            if set_status is not None
            else ("certain" if clear_uncertain and not new_uncertain else row.status)
        )
        updated.append(
            replace(
                row,
                key_version=row.key_version + 1,
                reviewed_by=owner.strip(),
                reviewed_at=stamp,
                owner_signed_at=stamp,
                status=new_status,
                uncertain_fields=new_uncertain,
            )
        )
    serialized = serialize_answer_key(updated)
    return parse_answer_key(serialized)


def serialize_answer_key(rows: Sequence[KeyRow]) -> str:
    return (
        "\n".join(
            json.dumps(asdict(row), separators=(",", ":"), ensure_ascii=False) for row in rows
        )
        + "\n"
    )


def write_answer_key_atomic(path: Path, rows: Sequence[KeyRow]) -> None:
    """Write validated KeyRows to `path` via temp file + fsync + atomic rename."""
    if path.is_symlink():
        raise ValueError("answer key path must be a regular file, not a symlink")
    text = serialize_answer_key(rows)
    # Parse once more before touching disk so a malformed line can never be written.
    parse_answer_key(text)
    temp_path: Path | None = None
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=f".{path.name}-",
            dir=path.parent,
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            os.fchmod(handle.fileno(), mode)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def load_saved_pages(pages_dirs: Sequence[Path]) -> dict[str, str]:
    """Load saved page texts from `<sha256>.txt` or raw HTML/text `<sha256>` files."""
    pages: dict[str, str] = {}
    for directory in pages_dirs:
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            if not path.is_file():
                continue
            name = path.name
            is_txt = name.endswith(".txt")
            stem = name.removesuffix(".txt") if is_txt else name
            if len(stem) != 64 or any(c not in "0123456789abcdef" for c in stem):
                continue
            raw_bytes = path.read_bytes()
            if is_txt:
                pages[stem] = raw_bytes.decode("utf-8", errors="replace")
            elif stem not in pages and not raw_bytes.startswith(b"%PDF-"):
                decoded = raw_bytes.decode("utf-8", errors="replace")
                pages[stem] = (
                    html_to_text(decoded)
                    if "<html" in decoded.lower()
                    or "<body" in decoded.lower()
                    or "<p" in decoded.lower()
                    else plain_to_text(decoded)
                )
    return pages
