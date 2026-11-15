"""Actual owner-scoped upload HTTP boundary; synthetic bytes, SQLite, no providers."""

from __future__ import annotations

import base64
import io
import sqlite3
from pathlib import Path

import pytest
from httpx2 import ASGITransport, AsyncClient
from PIL import Image

from test_ticket_intake_api import ALICE, ORIGIN, app_for
from test_ticket_intake_service import ScriptedVisionClient

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def image_bytes(kind: str) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (16, 16), "navy").save(output, kind)
    return output.getvalue()


def row_counts(path: Path) -> dict[str, int]:
    with sqlite3.connect(path) as db:
        names = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {name: db.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for name in names}


@pytest.mark.parametrize(
    ("media", "data", "status"),
    [
        ("application/pdf", b"%PDF-1.4\nsynthetic", 422),
        ("image/heic", b"synthetic HEIC", 422),
        ("image/gif", image_bytes("GIF"), 422),
        ("image/svg+xml", b"<svg xmlns='http://www.w3.org/2000/svg'/>", 422),
        ("application/octet-stream", image_bytes("PNG"), 422),
        ("image/jpeg", b"%PDF-1.4\nsynthetic disguised PDF", 409),
        ("image/png", image_bytes("GIF"), 409),
        ("image/png", b"\x89PNG\r\n\x1a\ncorrupt", 409),
        ("image/jpeg", image_bytes("JPEG")[:100], 409),
        ("image/png", b"", 422),
        ("image/jpeg", b"x" * 10_000_001, 409),
    ],
    ids=[
        "pdf",
        "heic",
        "gif",
        "svg",
        "unknown-type",
        "disguised-pdf",
        "disguised-gif",
        "corrupt-png",
        "truncated-jpeg",
        "empty",
        "oversize",
    ],
)
async def test_refused_upload_has_no_persistence_or_downstream_work(
    tmp_path: Path, media: str, data: bytes, status: int
) -> None:
    provider = ScriptedVisionClient()
    app, harnessed = app_for(tmp_path, provider)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as api:
        workspace = (await api.post("/workspaces", headers=ALICE, json={"notes": ""})).json()
        initial = (await api.get(f"/workspaces/{workspace['workspace_id']}", headers=ALICE)).json()
        before = row_counts(tmp_path / "api.sqlite")
        response = await api.post(
            f"/workspaces/{workspace['workspace_id']}/photos",
            headers=ALICE,
            json={"content_type": media, "data_base64": base64.b64encode(data).decode()},
        )
        assert response.status_code == status, response.text
        assert "photo_ref" not in response.json()
        assert harnessed.photos.items == {}
        assert provider.calls == []
        # No intake, model reservation/call, charge or mailing row can be created by rejection.
        assert row_counts(tmp_path / "api.sqlite") == before
        saved = await api.get(f"/workspaces/{workspace['workspace_id']}", headers=ALICE)
        assert saved.json() == initial


@pytest.mark.parametrize(("kind", "media"), [("JPEG", "image/jpeg"), ("PNG", "image/png")])
async def test_valid_image_upload_is_stored_without_starting_reading(
    tmp_path: Path, kind: str, media: str
) -> None:
    provider = ScriptedVisionClient()
    app, harnessed = app_for(tmp_path, provider)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as api:
        workspace = (await api.post("/workspaces", headers=ALICE, json={"notes": ""})).json()
        before = row_counts(tmp_path / "api.sqlite")
        response = await api.post(
            f"/workspaces/{workspace['workspace_id']}/photos",
            headers=ALICE,
            json={
                "content_type": media,
                "data_base64": base64.b64encode(image_bytes(kind)).decode(),
            },
        )
        assert response.status_code == 201, response.text
        stored = harnessed.photos.get("alice", response.json()["photo_ref"])
        assert stored is not None
        with Image.open(io.BytesIO(stored)) as image:
            image.load()
            assert image.format == kind
        assert provider.calls == []
        assert row_counts(tmp_path / "api.sqlite") == before
