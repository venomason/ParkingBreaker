"""Pure deterministic stratified sampling for the T21 random-sample audit.

A draw is offline machinery only: no clock, file, environment, network or model is
consulted, and the same snapshot, seed and size always return the same records in the
same order whatever order the snapshot lists them in. Strata are the T21.md:43-44
audit strata — large cities, small cities, counties, universities, airports and
processors — classified from each record's own registry fields; a record matching
none of them is counted in ``unstratified`` and never drawn. The registry carries no
population, so large/small cities follow the municipal form of the organization name
("City of …" versus town, village, borough, township or hamlet forms): a sampling
label only, never a size claim about a place. Airport authorities and ports are drawn
as the airport stratum (v7 §9 groups airport/port).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Literal

from parkingbreaker.research.jurisdiction import DotGovRecord, DotGovSnapshot

type Stratum = Literal["large_city", "small_city", "county", "university", "airport", "processor"]

STRATA: tuple[Stratum, ...] = (
    "large_city",
    "small_city",
    "county",
    "university",
    "airport",
    "processor",
)

_PROCESSOR = re.compile(r"\bprocessors?\b|\block ?-?box(?:es)?\b")
_AIRPORT = re.compile(r"\bairports?\b|\bport of\b|\bport authority\b")
_UNIVERSITY = re.compile(r"\buniversit(?:y|ies)\b|\bcolleges?\b")
_MUNICIPAL_LARGE = re.compile(r"\bcity (?:and county )?of\b")
_MUNICIPAL_SMALL = re.compile(
    r"\b(?:town|village|borough|township|hamlet) of\b|\b(?:town|village|borough|township|hamlet)\b"
)


@dataclass(frozen=True)
class StratumDraw:
    """One stratum's slice of a draw: what was asked for and what was available."""

    stratum: Stratum
    requested: int
    records: tuple[DotGovRecord, ...]


@dataclass(frozen=True)
class SampleDraw:
    """A stratified draw over a snapshot frame.

    ``draws`` follow STRATA order so a report can show every declared stratum even
    when one is empty; ``unstratified`` counts frame records that fit no stratum.
    """

    seed: str
    draws: tuple[StratumDraw, ...]
    unstratified: int


def stratum_of(record: DotGovRecord) -> Stratum | None:
    """Classify one registry record into a T21 audit stratum, or None if it fits none.

    Pure: only the record's own fields decide. Name signals run before the domain
    type so an airport registered under a city domain still lands in the airport
    stratum, and municipal name forms run before university tokens so "City of
    College Park" is a city, not a college. A city-type record whose name carries no
    municipal form falls into the small-city catch-all.
    """
    name = f"{record.organization} {record.suborganization or ''}".lower()
    if _PROCESSOR.search(name):
        return "processor"
    if _AIRPORT.search(name):
        return "airport"
    if _MUNICIPAL_LARGE.search(name):
        return "large_city"
    if _MUNICIPAL_SMALL.search(name):
        return "small_city"
    if _UNIVERSITY.search(name):
        return "university"
    if record.domain_type == "County":
        return "county"
    if record.domain_type == "City":
        return "small_city"
    return None


def stratified_draw(snapshot: DotGovSnapshot, *, seed: str, size: int) -> SampleDraw:
    """Draw ``size`` records from ``snapshot``, split as evenly as possible across STRATA.

    Pure and deterministic: each stratum's records are ordered by
    ``sha256(seed, domain)`` and the first slots are taken, so the seed chooses the
    members without any RNG and snapshot record order never matters. Quotas are
    ``size // len(STRATA)`` each, with the remainder going to the leading strata in
    STRATA order; a short stratum keeps what it has and its unused slots are not
    refilled from other strata.
    """
    if size < 0:
        raise ValueError("size must be >= 0")
    by_stratum: dict[Stratum, list[DotGovRecord]] = {s: [] for s in STRATA}
    unstratified = 0
    for record in snapshot.records:
        stratum = stratum_of(record)
        if stratum is None:
            unstratified += 1
        else:
            by_stratum[stratum].append(record)
    base, remainder = divmod(size, len(STRATA))
    draws: list[StratumDraw] = []
    for index, stratum in enumerate(STRATA):
        requested = base + (1 if index < remainder else 0)
        ordered = sorted(
            by_stratum[stratum],
            key=lambda r: (hashlib.sha256(f"{seed}\0{r.domain}".encode()).hexdigest(), r.domain),
        )
        draws.append(StratumDraw(stratum, requested, tuple(ordered[:requested])))
    return SampleDraw(seed, tuple(draws), unstratified)
