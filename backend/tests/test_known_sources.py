"""2.x city files only suggest where to start; they never supply facts."""

import json
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

import pytest

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


@pytest.mark.parametrize("city", ["az-mesa", "az-phoenix", "az-tucson", "nv-las_vegas"])
def test_document_preparer_hold_excludes_shipped_city_seeds(city: str) -> None:
    record = json.loads((DATA / f"us-{city}.json").read_text())
    host = urlsplit(record["mail_acceptance_policy"]["official_source_url"]).hostname
    assert host is not None
    agency = replace(SAN_FRANCISCO, official_domains=(host,))
    assert record["status"] == "blocked"
    assert known_seeds(agency, [record]) == ()
    assert known_seeds(agency, [{**record, "status": "active"}])


def test_boston_record_names_the_published_contest_recipient() -> None:
    record = json.loads((DATA / "us-ma-boston.json").read_text())
    assert record["appeal_mail_address"] == {
        "status": "complete",
        "department": "City of Boston",
        "address1": "P.O. Box 55800",
        "address2": None,
        "city": "Boston",
        "state": "MA",
        "zip": "02205",
        "country": "US",
    }
    assert "Roxbury" not in json.dumps(record)
    assert "2300 Washington" not in json.dumps(record)
    assert "P.O. Box 55800" in record["mail_acceptance_policy"]["official_excerpt"]


def test_sacramento_research_starts_at_the_city_contest_instructions() -> None:
    agency = replace(SAN_FRANCISCO, official_domains=("cityofsacramento.gov",))
    seeds = known_seeds(agency, RECORDS)
    root = "https://www.cityofsacramento.gov/public-works/parking/"
    assert {seed.url for seed in seeds} == {
        root + "citation-services",
        root + "contact-parking-services/parking-enforcement",
    }


def test_chicago_uses_the_initial_window_and_the_official_contest_faq() -> None:
    record = json.loads((DATA / "us-il-chicago.json").read_text())
    assert record["status"] == "paused"
    assert record["appeal_mail_address"]["status"] == "unverified"
    assert record["appeal_deadline_days"] == 7
    assert "21 days" in record["confirmation_note"]
    agency = replace(SAN_FRANCISCO, official_domains=("chicago.gov",))
    assert [seed.url for seed in known_seeds(agency, [record])] == [
        "https://parkingtickets.chicago.gov/EHearingWeb/FrequentlyAskedQuestions"
    ]


def test_columbus_lead_preserves_the_condition_for_mail() -> None:
    record = json.loads((DATA / "us-oh-columbus.json").read_text())
    agency = replace(SAN_FRANCISCO, official_domains=("columbus.gov",))
    assert [s.url.rsplit("/", 1)[-1] for s in known_seeds(agency, [record])] == ["Contest-a-Ticket"]
    assert (
        "unable to submit an appeal online" in record["mail_acceptance_policy"]["official_excerpt"]
    )
    assert record.get("appeal_deadline_days") is None


def test_oakland_lead_preserves_the_original_ticket_requirement() -> None:
    record = json.loads((DATA / "us-ca-oakland.json").read_text())
    agency = replace(SAN_FRANCISCO, official_domains=("oaklandca.gov",))
    assert [s.url.rsplit("/", 1)[-1] for s in known_seeds(agency, [record])] == [
        "Challenge-a-Parking-Ticket"
    ]
    assert record["appeal_mail_address"]["address1"] == "270 Frank H. Ogawa Plaza"
    assert "original parking ticket" in record["special_requirements"]["required_attachments"]
