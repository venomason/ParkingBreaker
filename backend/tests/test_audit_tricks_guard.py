"""Tests for audit A018: procedural-only Book of Tricks guard."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from parkingbreaker.tricks import (
    Proposal,
    SavedPage,
    hunt,
    parse_catalog,
    prohibited_trick_reason,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
_SHA = "a" * 64


def _catalog_item(
    *,
    trick_id: str = "TEST_TRICK",
    title: str = "Sample procedural rule",
    what_goes_wrong: str = "A procedural deadline is missed.",
    signals: list[str] | None = None,
) -> str:
    return json.dumps(
        [
            {
                "trick_id": trick_id,
                "title": title,
                "what_goes_wrong": what_goes_wrong,
                "signals": signals or ["submit within 21 calendar days"],
                "first_seen": {"place": "Test, CA", "sha256": _SHA},
            }
        ]
    )


def test_real_book_of_tricks_passes_procedural_guard() -> None:
    raw = (REPO_ROOT / "backend" / "data" / "book_of_tricks.json").read_text(encoding="utf-8")
    tricks = parse_catalog(raw)
    assert len(tricks) >= 8


@pytest.mark.parametrize(
    ("field", "value", "expected_reason"),
    [
        ("title", "Best defense for meter citations", "defense_recommendation"),
        (
            "what_goes_wrong",
            "Recommend arguing broken meter as strongest argument.",
            "defense_recommendation",
        ),
        (
            "what_goes_wrong",
            "Predicts dismissal odds and win rate for the customer.",
            "outcome_grading",
        ),
        ("title", "Automatically select plea for Seattle citations", "automatic_plea_choice"),
        ("signals", ["you should plead mitigated on this form"], "automatic_plea_choice"),
    ],
)
def test_parse_catalog_rejects_non_procedural_tricks(
    field: str, value: object, expected_reason: str
) -> None:
    kwargs: dict[str, object] = {field: value}
    raw = _catalog_item(**kwargs)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match=f"prohibited_non_procedural_trick:{expected_reason}"):
        parse_catalog(raw)


def test_hunt_filters_out_non_procedural_proposals_but_keeps_routing_warnings() -> None:
    page = SavedPage(
        url="https://example.gov/parking",
        sha256=_SHA,
        text=(
            "Submit within 21 calendar days of the citation date. "
            "Use this best defense for missing signage. "
            "Online appeals only for this stage."
        ),
    )

    class _Proposer:
        def propose(self, place: str, pages: object) -> list[Proposal]:
            return [
                Proposal(
                    place=place,
                    title="Best defense strategy",
                    url=page.url,
                    sha256=page.sha256,
                    excerpt="Use this best defense for missing signage.",
                ),
                Proposal(
                    place=place,
                    title="Online appeals only",
                    url=page.url,
                    sha256=page.sha256,
                    excerpt="Online appeals only for this stage.",
                ),
            ]

    report = hunt("Test, CA", [page], [], proposer=_Proposer())
    assert [p.title for p in report.proposals] == ["Online appeals only"]
    assert prohibited_trick_reason("Do not pay your citation if you wish to protest it") is None
