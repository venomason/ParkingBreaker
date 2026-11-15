"""The Book of Tricks hunter runs on real saved official pages only, never invented text."""

import ast
import json
import re
from pathlib import Path

import pytest

import parkingbreaker
from parkingbreaker.tricks import Proposal, SavedPage, hunt, parse_catalog

ROOT = Path(__file__).parents[2]
PAGES = ROOT / "build-kit/evidence/bench-pages"
CATALOG = parse_catalog((Path(__file__).parents[1] / "data/book_of_tricks.json").read_text())


def _page(sha: str) -> SavedPage:
    urls = {}
    for index in PAGES.glob("*.jsonl"):
        for line in index.read_text().splitlines():
            row = json.loads(line)
            urls[row["sha256"]] = row["final_url"]
    return SavedPage(urls.get(sha, ""), sha, (PAGES / f"{sha}.txt").read_text(errors="replace"))


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


ITHACA = "47651915aa7f368ee22d6ef9de2a1330766b25796c85a33d241764d16b773d1a"
BAINBRIDGE = "d903065db41ae6f9495bd6465e47854dd844c1fc8cd73c9c7448efefdb9d0bb0"
SFMTA = "2f253f6ebf2f34f01af93102dd0f1063fab9abc80b80afcbd438c4424905de2a"


def test_every_signal_comes_from_a_saved_official_page() -> None:
    corpus = [_flat(p.read_text(errors="replace")).lower() for p in PAGES.glob("*.txt")]
    for trick in CATALOG:
        first = _flat(_page(trick.first_seen_sha256).text).lower()
        assert any(signal.lower() in first for signal in trick.signals), trick.trick_id
        for signal in trick.signals:
            assert any(signal.lower() in page for page in corpus), (trick.trick_id, signal)


def test_hunter_reports_known_tricks_with_the_exact_sentence() -> None:
    pages = [_page(ITHACA), _page(BAINBRIDGE), _page(SFMTA)]
    report = hunt("saved pages", pages, CATALOG)
    seen = {(s.trick_id, s.sha256) for s in report.sightings}
    assert {
        ("ONLINE_ONLY_APPEAL", ITHACA),
        ("CONTEST_NEEDS_APPEARANCE", BAINBRIDGE),
        ("DO_NOT_PAY_WHILE_PROTESTING", SFMTA),
        ("LATER_STAGE_DEPOSIT", SFMTA),
        ("STOLEN_VEHICLE_FULL_REPORT", SFMTA),
    } <= seen
    texts = {p.sha256: _flat(p.text) for p in pages}
    for sighting in report.sightings:
        assert sighting.excerpt in texts[sighting.sha256]
        assert sighting.url.startswith("https://")
    assert len(seen) == len(report.sightings)  # one sighting per trick and page


class _Proposer:
    def __init__(self, proposals: list[Proposal]) -> None:
        self.proposals = proposals

    def propose(self, place: str, pages: object) -> list[Proposal]:
        return self.proposals


def test_proposals_must_quote_a_scanned_page_and_never_change_the_catalog() -> None:
    page = _page(SFMTA)
    quote = "You may also submit your protest by mail."  # saved SFMTA text (dossier appendix)
    assert quote in _flat(page.text)
    keep = Proposal("San Francisco, CA", "Mail route", page.url, page.sha256, quote)
    unquoted = Proposal("San Francisco, CA", "Unsupported", page.url, page.sha256, "x" * 20)
    elsewhere = Proposal("San Francisco, CA", "Wrong page", page.url, ITHACA, quote)
    report = hunt("San Francisco, CA", [page], CATALOG, _Proposer([keep, unquoted, elsewhere]))
    assert report.proposals == (keep,)
    on_disk = (Path(__file__).parents[1] / "data/book_of_tricks.json").read_text()
    assert parse_catalog(on_disk) == CATALOG


@pytest.mark.parametrize(
    "module",
    [
        "case_service.py",
        "prehold.py",
        "service_policy.py",
        "research/run.py",
        "research/support.py",
    ],
)
def test_the_hunter_never_drives_research_or_intake(module: str) -> None:
    tree = ast.parse((Path(parkingbreaker.__file__).parent / module).read_text())
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    reads = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    # case_service only runs the hunter after research is saved and stores what it sees;
    # no intake, research or case decision reads the sightings.
    assert "trick_sightings" not in reads
    if module != "case_service.py":
        assert "parkingbreaker.tricks" not in imported
