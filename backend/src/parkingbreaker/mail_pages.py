"""Lob's physical page rules for letters, shared by form completion and the packet. Pure.

Lob Letters API reference (checked 2026-11-03): "All pages of a supplied PDF file must be
sized at 8.5"x11"", and a domestic letter holds at most 60 single-sided pages. Our own
request adds a blank address page (address_placement=insert_blank_page), and Lob's help
center says certified mail adds one more sheet at the front; it does not say whether
that sheet counts toward the 60, so our own pages stop at 58.
"""

from __future__ import annotations

from pypdf import PageObject

LETTER_POINTS = (612.0, 792.0)
MAX_OWN_PAGES = 58
_TOLERANCE = 0.5


def _letter_box(box: object) -> bool:
    left, bottom, right, top = (float(v) for v in box)  # type: ignore[attr-defined]
    return (
        abs(left) <= _TOLERANCE
        and abs(bottom) <= _TOLERANCE
        and abs(right - left - LETTER_POINTS[0]) <= _TOLERANCE
        and abs(top - bottom - LETTER_POINTS[1]) <= _TOLERANCE
    )


def prints_as_letter(page: PageObject) -> bool:
    """The page prints on US Letter as is: no other size, crop, rotation or scaling."""
    return (
        _letter_box(page.mediabox)
        # Read /CropBox only when present: pypdf's cropbox property writes a default.
        and ("/CropBox" not in page or _letter_box(page["/CropBox"]))
        and int(page.get("/Rotate", 0)) % 360 == 0
        and float(page.get("/UserUnit", 1)) == 1
    )
