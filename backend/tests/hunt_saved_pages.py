"""Run the Book of Tricks hunter over every saved official page and write a report.

Run: uv run python tests/hunt_saved_pages.py --out <file.json>
Places are taken from each page's host; pages come from build-kit/evidence/bench-pages.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

from parkingbreaker.tricks import SavedPage, hunt, parse_catalog

ROOT = Path(__file__).parents[2]
PAGES = ROOT / "build-kit/evidence/bench-pages"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    catalog = parse_catalog((Path(__file__).parents[1] / "data/book_of_tricks.json").read_text())
    urls: dict[str, str] = {}
    for index in sorted(PAGES.glob("*.jsonl")):
        for line in index.read_text().splitlines():
            row = json.loads(line)
            urls.setdefault(row["sha256"], row["final_url"])
    by_place: dict[str, list[SavedPage]] = defaultdict(list)
    for page in sorted(PAGES.glob("*.txt")):
        sha = page.stem
        url = urls.get(sha, "")
        host = (urlsplit(url).hostname or "unknown").removeprefix("www.")
        by_place[host].append(SavedPage(url, sha, page.read_text(errors="replace")))
    reports = [hunt(place, pages, catalog) for place, pages in sorted(by_place.items())]
    sightings = [asdict(s) for r in reports for s in r.sightings]
    summary = {
        "pages": sum(len(p) for p in by_place.values()),
        "places": len(by_place),
        "tricks_in_catalog": len(catalog),
        "sightings": len(sightings),
        "by_trick": {
            t.trick_id: sorted({s["place"] for s in sightings if s["trick_id"] == t.trick_id})
            for t in catalog
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"summary": summary, "sightings": sightings}, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
