"""The shared unserviceable list only says where research should look first."""

import ast
import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

import parkingbreaker
from parkingbreaker.config import known_city_records
from parkingbreaker.research.known_sources import known_seeds
from parkingbreaker.unserviceable import UnserviceableReason, parse_entries, seed_records
from samples import SAN_FRANCISCO

DATA = Path(__file__).parents[1] / "data" / "unserviceable_jurisdictions.json"
GOOD: dict[str, str] = {
    "place": "Example, WA",
    "state": "WA",
    "reason": "IN_PERSON_ONLY",
    "official_source_url": "https://www.example.gov/court",
    "quote": "Contested hearings are held in person.",
    "saved_page_sha256": "0" * 64,
    "summary": "Contests are heard in person.",
    "found_on": "2026-10-30",
}


def test_shipped_list_is_valid_and_seeds_research_on_the_agencys_own_host() -> None:
    entries = parse_entries(DATA.read_text())
    assert {(e.place, e.reason) for e in entries} >= {
        ("Bainbridge Island, WA", UnserviceableReason.IN_PERSON_ONLY),
        ("Ithaca, NY", UnserviceableReason.ONLINE_ONLY),
    }
    assert all(e.found_on <= date(2026, 10, 30) for e in entries)
    # Each quote is the exact admitted text saved with that page in the T21 evidence.
    dossier = (
        Path(__file__).parents[2] / "docs/bench/deep-city-dossiers-2026-10-27.md"
    ).read_text()
    for entry in entries:
        assert entry.saved_page_sha256 in dossier
        assert f"> {entry.quote}" in dossier
    records = known_city_records()
    assert all(record in records for record in seed_records(entries))
    bainbridge = replace(SAN_FRANCISCO, official_domains=("bainbridgewa.gov",), delegated_scopes=())
    assert [s.url for s in known_seeds(bainbridge, records)] == ["https://bainbridgewa.gov/m/faq"]
    elsewhere = replace(SAN_FRANCISCO, official_domains=("example.gov",), delegated_scopes=())
    assert known_seeds(elsewhere, records) == ()


@pytest.mark.parametrize(
    "change",
    [
        {"official_source_url": "http://www.example.gov/court"},
        {"state": "ZZ"},
        {"reason": "TOO_FAR"},
        {"summary": " "},
        {"note": "extra"},
        {"found_on": "yesterday"},
        {"quote": "in person"},
        {"saved_page_sha256": "d903065db41a"},
    ],
)
def test_malformed_entries_reject_the_whole_list(change: dict[str, str]) -> None:
    assert parse_entries(json.dumps([GOOD]))
    with pytest.raises(ValueError):  # noqa: PT011 - each variant fails a different check
        parse_entries(json.dumps([GOOD, GOOD | change]))


def test_the_list_is_never_a_gate() -> None:
    root = Path(parkingbreaker.__file__).parent
    for name in ("prehold.py", "service_policy.py", "case_service.py", "serviceability.py"):
        tree = ast.parse((root / name).read_text())
        modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        assert "parkingbreaker.unserviceable" not in modules, name
