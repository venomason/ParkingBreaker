from __future__ import annotations

from pathlib import Path

import pytest
from httpx2 import ASGITransport, AsyncClient

from parkingbreaker.api.app import create_app
from parkingbreaker.config import build_services, load_settings
from test_config import SECRET, WORKER

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.parametrize(("role", "served"), [("customer", False), ("worker", True), ("all", True)])
async def test_only_worker_roles_serve_internal_routes(
    tmp_path: Path, role: str, served: bool
) -> None:
    settings = load_settings(
        {"PB_PROVIDER_MODE": "fake", "PB_WORKER_SECRET": SECRET, "PB_DATA_DIR": str(tmp_path)},
        tmp_path / "no.env",
    )
    app = create_app(
        build_services(settings),
        worker_secret=settings.worker_secret,
        role=role,  # type: ignore[arg-type]
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://role.test") as c:
        sweep = await c.post("/internal/dispatch", headers=WORKER)
        step = await c.post(
            "/internal/cases/case_x/step", headers=WORKER, json={"step_id": "research"}
        )
        health = await c.get("/health")
    assert (sweep.status_code == 200) is served
    if not served:
        assert (sweep.status_code, step.status_code) == (404, 404)
    assert health.status_code == 200
    with pytest.raises(RuntimeError, match="unknown_api_role"):
        create_app(build_services(settings), role="admin")  # type: ignore[arg-type]


@pytest.mark.parametrize("role", ["customer", "worker"])
def test_fake_providers_refuse_the_split(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    from parkingbreaker.api.app import app_from_environment

    for name, value in {
        "PB_PROVIDER_MODE": "fake",
        "PB_WORKER_SECRET": SECRET,
        "PB_DATA_DIR": str(tmp_path),
        "PB_API_ROLE": role,
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("PB_FAKE_SPLIT_FOR_MEASUREMENT", raising=False)
    with pytest.raises(RuntimeError, match="needs real providers"):
        app_from_environment()
    monkeypatch.setenv("PB_API_ROLE", "all")
    assert app_from_environment() is not None
