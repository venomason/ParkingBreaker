"""After research, the hunter stores per-place sightings from the run's real pages only."""

from dataclasses import replace
from pathlib import Path

import pytest

from parkingbreaker.config import book_of_tricks
from parkingbreaker.research.run import ResearchResult
from parkingbreaker.research_records import decode_agency
from test_case_service import WORKER, researched_case
from test_case_service import anyio_backend as anyio_backend
from test_case_service import fake_resolution as fake_resolution

PAGES = Path(__file__).parents[2] / "build-kit/evidence/bench-pages"
ITHACA = "47651915aa7f368ee22d6ef9de2a1330766b25796c85a33d241764d16b773d1a"

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("fake_resolution")]


def _with_saved_ithaca_page(result: ResearchResult) -> ResearchResult:
    """Add a real saved official page to the run's pages (no invented text)."""
    url = "https://www.cityofithacany.gov/410/Make-a-Payment-View-Bills"
    saved = replace(
        result.sources[0],
        url=url,
        final_url=url,
        sha256=ITHACA,
        text=(PAGES / f"{ITHACA}.txt").read_text(errors="replace"),
    )
    return replace(result, sources=(*result.sources, saved))


async def _research(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, broken: bool
) -> tuple[object, str]:
    from test_tier_acceptance import extract_case, model_answers

    result, _client, _searcher = extract_case(model_answers())
    client, real, case = await researched_case(
        tmp_path, monkeypatch, _with_saved_ithaca_page(result)
    )
    research = real._deps.research
    assert research is not None
    object.__setattr__(real._deps, "research", replace(research, tricks=book_of_tricks()))
    if broken:

        def fail(*_args: object) -> int:
            raise RuntimeError("sighting store unavailable")

        monkeypatch.setattr(real._store, "record_trick_sightings", fail)
    try:
        for _ in range(2):
            step = await client.post(
                f"/internal/cases/{case}/step", headers=WORKER, json={"step_id": "research"}
            )
            assert step.status_code == 200, step.text
        state = step.json()["state"]
    finally:
        await client.aclose()
    record = real._intake(case)
    assert record.agency_json is not None
    return real, decode_agency(record.agency_json).id if state else ""


async def test_research_stores_each_trick_once_per_place_and_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real, place = await _research(tmp_path, monkeypatch, broken=False)
    sightings = real._store.trick_sightings(place)  # type: ignore[attr-defined]
    assert [(s.trick_id, s.sha256) for s in sightings] == [("ONLINE_ONLY_APPEAL", ITHACA)]
    assert "Only online appeals are accepted" in sightings[0].excerpt


async def test_a_failing_sighting_store_cannot_stop_research(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real, place = await _research(tmp_path, monkeypatch, broken=True)
    assert real._store.trick_sightings(place) == ()  # type: ignore[attr-defined]
