"""Offline-tested synthetic post-deploy fidelity verifier (audit A065, A066).

Verifies `/health` build/policy identity and active customer-authored mode, extracts
rendered text from a synthetic preview PDF, and confirms deterministic customer authorship
without authorizing charges, live mail, production customer access, or provider spend.
"""

from __future__ import annotations

import base64
import io
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from pypdf import PdfReader

from parkingbreaker.appeal.packet import (
    ATTACHMENT_NAME,
    LETTER_MAX_PAGES,
    PHOTO_DATE_LABEL,
    _citation_reference,
    _PacketPdf,
)
from parkingbreaker.research.fulfilment import (
    CITATION_BACK_PAGE_TITLE,
    CITATION_PAGE_TITLE,
    EVIDENCE_PAGE_TITLE,
    UPLOADED_LABEL,
)

_FORBIDDEN_ADVOCACY = re.compile(
    r"respectfully\s+request\s+that\s+this\s+citation\s+be\s+dismissed",
    re.IGNORECASE,
)
_SPACE = re.compile(r"\s+")
_EXACT_EXHIBIT_LINES = frozenset(
    {
        CITATION_PAGE_TITLE,
        CITATION_BACK_PAGE_TITLE,
        UPLOADED_LABEL,
    }
)


class SyntheticVerificationError(RuntimeError):
    """Raised when a synthetic deployment or preview PDF fails fidelity checks."""


class HealthResponse(Protocol):
    status_code: int

    def json(self) -> object: ...


class HealthClient(Protocol):
    def get(self, url: str) -> HealthResponse: ...


@dataclass(frozen=True)
class SyntheticCheckResult:
    build_commit: str | None
    policy_hashes: dict[str, str]
    customer_authored_mode: bool
    customer_authored_mode_version: str | None
    extracted_pdf_chars: int


def extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extract normalized text from rendered PDF bytes."""
    reader = PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def verify_preview_pdf_authorship(
    *,
    expected_customer_text: str,
    expected_letter_text: str | None = None,
    expected_citation_number: str | None = None,
    pdf_bytes: bytes | None = None,
    pdf_base64: str | None = None,
) -> int:
    """Verify exact customer text is rendered in the preview PDF without unapproved additions."""
    raw_pdf = pdf_bytes
    if raw_pdf is None:
        if not pdf_base64:
            raise SyntheticVerificationError("missing_preview_pdf")
        raw_pdf = base64.b64decode(pdf_base64, validate=True)
    reader = PdfReader(io.BytesIO(raw_pdf))
    extracted = "\n".join((page.extract_text() or "") for page in reader.pages)
    normalized_extracted = _SPACE.sub(" ", extracted).strip()
    normalized_expected = _SPACE.sub(" ", expected_customer_text).strip()
    if not normalized_expected:
        raise SyntheticVerificationError("customer_text_missing_from_pdf")
    expected_letter = (
        expected_customer_text if expected_letter_text is None else expected_letter_text
    )
    normalized_letter = _SPACE.sub(" ", expected_letter).strip()
    if normalized_expected not in normalized_letter:
        raise SyntheticVerificationError("customer_text_missing_from_expected_letter")
    if _FORBIDDEN_ADVOCACY.search(normalized_extracted) and not _FORBIDDEN_ADVOCACY.search(
        normalized_letter
    ):
        raise SyntheticVerificationError("prohibited_stock_advocacy_in_pdf")
    attached = reader.attachments.get(ATTACHMENT_NAME, [])
    if len(attached) != 1:
        raise SyntheticVerificationError("missing_or_ambiguous_adopted_letter_attachment")
    if attached[0] != expected_letter.encode("utf-8"):
        attached_text = _SPACE.sub(" ", attached[0].decode("utf-8", errors="replace")).strip()
        if normalized_expected not in attached_text:
            raise SyntheticVerificationError("customer_text_missing_from_pdf")
        raise SyntheticVerificationError("unapproved_text_in_pdf")
    # Render only the trusted expected letter, including its explicitly supplied
    # citation header. Compare each page, rather than stripping header-like prose.
    expected_pdf = _PacketPdf(_citation_reference(expected_citation_number))
    expected_pdf.set_creation_date(datetime(2026, 1, 1, tzinfo=UTC))
    expected_pdf.add_page()
    expected_pdf.text_block(expected_letter)
    expected_pages = PdfReader(io.BytesIO(bytes(expected_pdf.output()))).pages
    if not 1 <= len(expected_pages) <= LETTER_MAX_PAGES or len(reader.pages) < len(expected_pages):
        raise SyntheticVerificationError("unapproved_text_in_pdf")
    for actual, expected in zip(reader.pages[: len(expected_pages)], expected_pages, strict=True):
        if (
            _SPACE.sub(" ", actual.extract_text() or "").strip()
            != _SPACE.sub(" ", expected.extract_text() or "").strip()
        ):
            raise SyntheticVerificationError("unapproved_text_in_pdf")
    # Only known exhibit captions may follow the complete expected letter pages.
    tail = _SPACE.sub(
        " ", "\n".join(page.extract_text() or "" for page in reader.pages[len(expected_pages) :])
    ).strip()
    caption_parts = [re.escape(line) for line in _EXACT_EXHIBIT_LINES]
    citation_reference = _citation_reference(expected_citation_number)
    if citation_reference is not None:
        caption_parts.append(re.escape(citation_reference))
    caption_parts.extend(
        [
            rf"{re.escape(EVIDENCE_PAGE_TITLE)}\s+\d+\s+of\s+\d+",
            rf"{re.escape(PHOTO_DATE_LABEL.strip())}\s*\d{{4}}-\d{{2}}-\d{{2}}(?:\s+\d{{2}}:\d{{2}}:\d{{2}})?",
        ]
    )
    if tail and re.fullmatch(r"(?:" + "|".join(caption_parts) + r"|\s)+", tail) is None:
        raise SyntheticVerificationError("unapproved_text_in_pdf")
    return len(extracted)


def verify_health_identity(
    client: HealthClient,
    *,
    expected_commit: str | None = None,
    expected_policy_hashes: Mapping[str, str] | None = None,
    require_non_live_mode: bool = True,
) -> tuple[str | None, dict[str, str], bool, str | None]:
    """Check `/health` for safe build identity, policy hashes, and customer-authored mode."""
    response = client.get("/health")
    if response.status_code != 200:
        raise SyntheticVerificationError(f"health_status_{response.status_code}")
    payload = response.json()
    if not isinstance(payload, dict):
        raise SyntheticVerificationError("health_payload_not_object")
    if payload.get("status") != "ok":
        raise SyntheticVerificationError("health_not_ok")
    if require_non_live_mode and payload.get("mode") not in {"preview", "fake", "test"}:
        raise SyntheticVerificationError("synthetic_check_refused_live_mode")
    if payload.get("customer_authored_mode") is not True or not payload.get(
        "customer_authored_mode_version"
    ):
        raise SyntheticVerificationError("customer_authored_mode_inactive")
    commit = payload.get("build_commit")
    if expected_commit is not None and commit != expected_commit:
        raise SyntheticVerificationError("build_commit_mismatch")
    policy_hashes = payload.get("policy_hashes")
    if not isinstance(policy_hashes, dict):
        raise SyntheticVerificationError("policy_hashes_missing")
    if expected_policy_hashes is not None:
        for key, expected_hash in expected_policy_hashes.items():
            if policy_hashes.get(key) != expected_hash:
                raise SyntheticVerificationError(f"policy_hash_mismatch:{key}")
    return (
        None if commit is None else str(commit),
        {str(k): str(v) for k, v in policy_hashes.items()},
        True,
        str(payload["customer_authored_mode_version"]),
    )


def verify_synthetic_deployment(
    client: HealthClient,
    *,
    expected_customer_text: str,
    expected_letter_text: str | None = None,
    expected_citation_number: str | None = None,
    preview_pdf_bytes: bytes | None = None,
    preview_pdf_base64: str | None = None,
    preview_case_id: str | None = None,
    preview_adopted_sha256: str | None = None,
    preview_headers: Mapping[str, str] | None = None,
    expected_commit: str | None = None,
    expected_policy_hashes: Mapping[str, str] | None = None,
    require_non_live_mode: bool = True,
) -> SyntheticCheckResult:
    """Run the full offline/synthetic health + preview-PDF authorship verification."""
    commit, policy_hashes, mode_active, mode_version = verify_health_identity(
        client,
        expected_commit=expected_commit,
        expected_policy_hashes=expected_policy_hashes,
        require_non_live_mode=require_non_live_mode,
    )
    if preview_pdf_bytes is None and preview_pdf_base64 is None and preview_case_id is not None:
        if not preview_adopted_sha256:
            raise SyntheticVerificationError("missing_preview_adopted_sha256")
        post_fn = getattr(client, "post", None)
        if not callable(post_fn):
            raise SyntheticVerificationError("preview_client_missing_post")
        preview_response = post_fn(
            f"/cases/{preview_case_id}/packet-preview",
            json={"adopted_sha256": preview_adopted_sha256},
            headers=dict(preview_headers) if preview_headers is not None else None,
        )
        if not isinstance(getattr(preview_response, "status_code", None), int):
            raise SyntheticVerificationError("preview_response_invalid")
        if preview_response.status_code != 200:
            raise SyntheticVerificationError(f"preview_status_{preview_response.status_code}")
        preview_payload = preview_response.json()
        if not isinstance(preview_payload, dict) or not isinstance(
            preview_payload.get("pdf_base64"), str
        ):
            raise SyntheticVerificationError("preview_payload_missing_pdf")
        preview_pdf_base64 = preview_payload["pdf_base64"]
    chars = verify_preview_pdf_authorship(
        expected_customer_text=expected_customer_text,
        expected_letter_text=expected_letter_text,
        expected_citation_number=expected_citation_number,
        pdf_bytes=preview_pdf_bytes,
        pdf_base64=preview_pdf_base64,
    )
    return SyntheticCheckResult(
        build_commit=commit,
        policy_hashes=policy_hashes,
        customer_authored_mode=mode_active,
        customer_authored_mode_version=mode_version,
        extracted_pdf_chars=chars,
    )
