"""Per-declaration identity and affirmation (audit A020-A028, A084, A092)."""

from __future__ import annotations

from datetime import date

import pytest

from parkingbreaker.declarations import (
    Declaration,
    DeclarationError,
    affirmation_map,
    declarations_for,
    unaffirmed_required,
    unknown_affirmations,
)
from parkingbreaker.research.findings import Finding, FindingStatus
from parkingbreaker.research.questions import FactKey, Ticket
from samples import source

TICKET = Ticket("us-ca-san_francisco", "TC 7.2.22", "Street cleaning", date(2026, 1, 5))
QUOTE = (
    "Any contest must include a signed declaration: "
    '"I declare under penalty of perjury that the foregoing is true and correct."'
)


def findings_and_sources() -> tuple[tuple[Finding, ...], tuple[object, ...]]:
    page = source(QUOTE)
    findings = (
        Finding(
            FactKey.REQUIRED_DECLARATIONS,
            ["signed declaration"],
            FindingStatus.CONFIRMED,
            QUOTE,
            (page.sha256,),
            jurisdiction=TICKET.city_id,
        ),
        Finding(
            FactKey.SIGNATURE_REQUIREMENT,
            ["signature required"],
            FindingStatus.CONFIRMED,
            "Any contest must include a signature.",
            (page.sha256,),
            jurisdiction=TICKET.city_id,
        ),
    )
    return findings, (page,)


def declarations() -> tuple[Declaration, ...]:
    findings, sources = findings_and_sources()
    return declarations_for(findings, sources, TICKET)  # type: ignore[arg-type]


def test_each_declaration_carries_identity_text_and_source() -> None:
    result = declarations()
    assert len(result) == 1
    declaration = result[0]
    assert declaration.declaration_id == "declaration-1"
    assert declaration.field_name == "REQUIRED_DECLARATIONS"
    assert declaration.verbatim_text.startswith("I declare under penalty of perjury")
    assert declaration.required is True
    assert declaration.signature_required is True
    assert declaration.source_sha256s
    assert len(declaration.text_sha256) == 64


def test_no_verified_declaration_means_none_required() -> None:
    assert declarations_for((), (), TICKET) == ()


def test_unverified_wording_fails_closed() -> None:
    page = source("You must include a declaration, but no wording is printed.")
    finding = Finding(
        FactKey.REQUIRED_DECLARATIONS,
        ["a declaration"],
        FindingStatus.CONFIRMED,
        page.text,
        (page.sha256,),
        jurisdiction=TICKET.city_id,
    )
    with pytest.raises(DeclarationError):
        declarations_for((finding,), (page,), TICKET)


def test_unaffirmed_required_declaration_is_reported() -> None:
    declaration = declarations()[0]
    assert unaffirmed_required((declaration,), {}) == ("declaration-1",)
    assert unaffirmed_required((declaration,), {"declaration-1": "0" * 64}) == ("declaration-1",)
    assert unaffirmed_required((declaration,), {"declaration-1": declaration.text_sha256}) == ()


def test_unknown_affirmation_is_reported_not_ignored() -> None:
    declaration = declarations()[0]
    affirmed = {declaration.declaration_id: declaration.text_sha256, "declaration-9": "x"}
    assert unknown_affirmations((declaration,), affirmed) == ("declaration-9",)


def test_duplicate_affirmation_is_refused() -> None:
    with pytest.raises(DeclarationError):
        affirmation_map([("declaration-1", "a"), ("declaration-1", "b")])


def test_affirmation_matches_only_the_exact_wording() -> None:
    declaration = declarations()[0]
    same = Declaration(
        declaration_id=declaration.declaration_id,
        field_name=declaration.field_name,
        verbatim_text=declaration.verbatim_text,
        required=True,
        source_sha256s=declaration.source_sha256s,
        signature_required=False,
    )
    assert same.affirmed_by(declaration.text_sha256)
    changed = Declaration(
        declaration_id=declaration.declaration_id,
        field_name=declaration.field_name,
        verbatim_text=declaration.verbatim_text + " ",
        required=True,
        source_sha256s=declaration.source_sha256s,
        signature_required=False,
    )
    assert not changed.affirmed_by(declaration.text_sha256)
