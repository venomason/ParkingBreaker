"""2.x city files only suggest where to start; they never supply facts."""

import json
from dataclasses import replace
from pathlib import Path

from parkingbreaker.agencies import SourceScope
from parkingbreaker.research.known_sources import known_seeds
from samples import SAN_FRANCISCO

DATA = Path(__file__).parents[1] / "data" / "cities_2x"
RECORDS = [json.loads(p.read_text()) for p in sorted(DATA.glob("us-*.json"))]


def test_sfmta_gets_its_verified_contest_page() -> None:
    seeds = known_seeds(SAN_FRANCISCO, RECORDS)
    assert [s.url for s in seeds] == [
        "https://www.sfmta.com/getting-around/drive-park/citations/contest-citation"
    ]


def test_delegated_host_counts_and_other_agencies_get_nothing() -> None:
    delegated = replace(
        SAN_FRANCISCO,
        official_domains=("sf.gov",),
        delegated_scopes=(SourceScope("sfmta.com", "/getting-around"),),
    )
    assert known_seeds(delegated, RECORDS)
    elsewhere = replace(SAN_FRANCISCO, official_domains=("example.gov",))
    assert known_seeds(elsewhere, RECORDS) == ()


def test_blocked_or_unverified_records_are_never_seeds() -> None:
    record = {"status": "blocked", "online_appeal_url": "https://www.sfmta.com/x"}
    assert known_seeds(SAN_FRANCISCO, [record]) == ()
    insecure = {"status": "active", "online_appeal_url": "http://www.sfmta.com/x"}
    assert known_seeds(SAN_FRANCISCO, [insecure]) == ()


def test_every_copied_record_parses() -> None:
    assert len(RECORDS) == 45
    assert all(isinstance(r.get("city_id"), str) for r in RECORDS)
