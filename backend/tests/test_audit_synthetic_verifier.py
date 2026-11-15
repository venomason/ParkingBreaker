"""Tests for audit A064, A065, A066: /health build/policy identity and synthetic verifier."""

from __future__ import annotations

import base64
import hashlib
import io
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from parkingbreaker.api.app import create_app
from parkingbreaker.api.fakes import FakeServices
from parkingbreaker.appeal.drafting import CustomerTextDrafter
from parkingbreaker.appeal.packet import build_packet
from parkingbreaker.synthetic_verifier import (
    SyntheticVerificationError,
    verify_preview_pdf_authorship,
    verify_synthetic_deployment,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _tiny_png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (10, 10), color=(255, 255, 255)).save(buf, format="PNG")
    return buf.getvalue()


@dataclass
class _FakeResponse:
    status_code: int
    _payload: object

    def json(self) -> object:
        return self._payload


class _FakeClient:
    def __init__(
        self,
        payload: object,
        status_code: int = 200,
        *,
        preview_payload: object = None,
        preview_status: int = 200,
    ) -> None:
        self._payload = payload
        self._status_code = status_code
        self._preview_payload = preview_payload
        self._preview_status = preview_status
        self.preview_requests: list[tuple[str, Mapping[str, object] | None]] = []

    def get(self, url: str) -> _FakeResponse:
        assert url == "/health"
        return _FakeResponse(self._status_code, self._payload)

    def post(
        self,
        url: str,
        *,
        json: Mapping[str, object] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> _FakeResponse:
        self.preview_requests.append((url, json))
        return _FakeResponse(self._preview_status, self._preview_payload)


def test_health_exposes_build_commit_and_policy_hashes_without_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PB_BUILD_COMMIT", "ee3aa4601234")
    services = FakeServices()
    services._deps = type("_Deps", (), {"drafter": CustomerTextDrafter()})()  # type: ignore[attr-defined]
    app = create_app(services, provider_mode="fake")
    client = TestClient(app)

    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["build_commit"] == "ee3aa4601234"
    assert data["customer_authored_mode"] is True
    assert data["customer_authored_mode_version"]
    expected_policy_sha = hashlib.sha256(
        (REPO_ROOT / "build-kit" / "state" / "free-beta-policy.json").read_bytes()
    ).hexdigest()
    assert data["policy_hashes"]["free_beta_policy"] == expected_policy_sha

    customer_text = "The meter display was completely blank at 9:15 AM."
    expected_letter = f"Citation #12345\n\n{customer_text}\n\nSincerely,\nJane Doe"
    pdf_bytes = build_packet(
        expected_letter,
        _tiny_png(),
        [],
        datetime(2026, 11, 12, 22, 0, 0, tzinfo=UTC),
    ).pdf
    result = verify_synthetic_deployment(
        client,
        expected_customer_text=customer_text,
        preview_pdf_bytes=pdf_bytes,
        expected_letter_text=expected_letter,
        expected_commit="ee3aa4601234",
        expected_policy_hashes={"free_beta_policy": expected_policy_sha},
    )
    assert result.build_commit == "ee3aa4601234"
    assert result.extracted_pdf_chars > len(customer_text)


def test_synthetic_verifier_refuses_live_mode_and_advocacy_leakage() -> None:
    live_client = _FakeClient(
        {
            "status": "ok",
            "mode": "live",
            "customer_authored_mode": True,
            "customer_authored_mode_version": "deterministic-v1",
            "build_commit": "ee3aa46",
            "policy_hashes": {},
        }
    )
    with pytest.raises(SyntheticVerificationError, match="synthetic_check_refused_live_mode"):
        verify_synthetic_deployment(
            live_client,
            expected_customer_text="Meter was broken.",
            preview_pdf_bytes=b"%PDF-1.4\n",
        )

    advocacy_pdf = build_packet(
        "Meter was broken.\n\nI respectfully request that this citation be dismissed.",
        _tiny_png(),
        [],
        datetime(2026, 11, 12, 22, 0, 0, tzinfo=UTC),
    ).pdf
    with pytest.raises(SyntheticVerificationError, match="prohibited_stock_advocacy_in_pdf"):
        verify_preview_pdf_authorship(
            expected_customer_text="Meter was broken.",
            pdf_bytes=advocacy_pdf,
        )


def test_synthetic_verifier_rejects_added_unapproved_claim_and_omission() -> None:
    customer_text = "The meter screen failed to register my coins at 9:15 AM."
    unapproved_pdf = build_packet(
        f"{customer_text}\n\nI admit full responsibility and waive my hearing.",
        _tiny_png(),
        [],
        datetime(2026, 11, 12, 22, 0, 0, tzinfo=UTC),
    ).pdf
    with pytest.raises(SyntheticVerificationError, match="unapproved_text_in_pdf"):
        verify_preview_pdf_authorship(
            expected_customer_text=customer_text,
            pdf_bytes=unapproved_pdf,
        )

    truncated_pdf = build_packet(
        "The meter screen failed.",
        _tiny_png(),
        [],
        datetime(2026, 11, 12, 22, 0, 0, tzinfo=UTC),
    ).pdf
    with pytest.raises(SyntheticVerificationError, match="customer_text_missing_from_pdf"):
        verify_preview_pdf_authorship(
            expected_customer_text=customer_text,
            pdf_bytes=truncated_pdf,
        )


def test_synthetic_verifier_fetches_packet_preview_via_endpoint() -> None:
    customer_text = "The meter display was blank when I parked at 9:15 AM."
    expected_letter = f"Citation #98765\n\n{customer_text}\n\nSincerely,\nAlex Example"
    packet_pdf = build_packet(
        expected_letter,
        _tiny_png(),
        [],
        datetime(2026, 11, 12, 22, 0, 0, tzinfo=UTC),
    ).pdf
    encoded = base64.b64encode(packet_pdf).decode("ascii")
    client = _FakeClient(
        {
            "status": "ok",
            "mode": "preview",
            "customer_authored_mode": True,
            "customer_authored_mode_version": "customer-authored-v1",
            "build_commit": "ee3aa4601234",
            "policy_hashes": {},
        },
        preview_payload={"pdf_base64": encoded},
    )
    result = verify_synthetic_deployment(
        client,
        expected_customer_text=customer_text,
        expected_letter_text=expected_letter,
        preview_case_id="case_prev_1",
        preview_adopted_sha256="a" * 64,
        preview_headers={"Authorization": "Bearer fake-alice"},
        expected_commit="ee3aa4601234",
    )
    assert client.preview_requests == [
        ("/cases/case_prev_1/packet-preview", {"adopted_sha256": "a" * 64})
    ]
    assert result.extracted_pdf_chars > len(customer_text)

    failing_client = _FakeClient(
        {
            "status": "ok",
            "mode": "preview",
            "customer_authored_mode": True,
            "customer_authored_mode_version": "customer-authored-v1",
            "build_commit": None,
            "policy_hashes": {},
        },
        preview_payload={"detail": "conflict"},
        preview_status=409,
    )
    with pytest.raises(SyntheticVerificationError, match="preview_status_409"):
        verify_synthetic_deployment(
            failing_client,
            expected_customer_text=customer_text,
            expected_letter_text=expected_letter,
            preview_case_id="case_prev_1",
            preview_adopted_sha256="a" * 64,
        )
