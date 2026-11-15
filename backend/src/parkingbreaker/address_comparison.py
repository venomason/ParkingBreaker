"""Shared postal comparison keys; callers retain original address wording and byte identity."""

from __future__ import annotations

import re

_ABBREVIATIONS = {
    "street": "st",
    "avenue": "ave",
    "av": "ave",
    "boulevard": "blvd",
    "road": "rd",
    "drive": "dr",
    "lane": "ln",
    "place": "pl",
    "court": "ct",
    "square": "sq",
    "parkway": "pkwy",
    "highway": "hwy",
    "suite": "ste",
    "floor": "fl",
    "room": "rm",
    "department": "dept",
    "north": "n",
    "south": "s",
    "east": "e",
    "west": "w",
}
_NON_ALNUM = re.compile(r"[^0-9a-z]+")
_PO_BOX = re.compile(r"\b(?:post office|p o) box\b")


def normalize_address(address: str) -> str:
    """Ignore existing formatting aliases without dropping destination identifiers."""
    text = _NON_ALNUM.sub(" ", address.casefold().replace(".", "")).strip()
    text = _PO_BOX.sub("po box", text)
    return " ".join(_ABBREVIATIONS.get(word, word) for word in text.split())
