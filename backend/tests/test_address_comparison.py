"""Shared postal comparison fixtures exercise research selection and return classification."""

from __future__ import annotations

from dataclasses import replace

import pytest

from parkingbreaker.address_policy import address_fingerprint, classify_address_failure
from parkingbreaker.research.ladder import match_address_choice
from parkingbreaker.research.ladder import normalize_address as research_key
from parkingbreaker.returned_mail import ReturnCause, UspsReturnReason, classify_return
from parkingbreaker.returned_mail import normalize_address as return_key
from test_address_policy import failure, verified_decision


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("PO Box 12", "Post Office Box 12"),
        ("PO Box 12", "P. O. Box 12"),
        ("PO Box 12", "p.o. box 12"),
        ("12 West Parkway", "12 W Pkwy"),
        ("Department 4", "Dept 4"),
        ("12 Main Av", "12 Main Avenue"),
        ("12 Main Square", "12 Main Sq"),
        ("12 Main Highway", "12 Main Hwy"),
        ("  12 North Main Street, Suite 2  ", "12 N Main St Ste 2"),
    ],
)
def test_same_destination_uses_one_comparison_in_both_paths(left: str, right: str) -> None:
    assert research_key(left) == research_key(right)
    assert return_key(left) == return_key(right)
    assert research_key(left).upper() == return_key(left)
    assert match_address_choice(left, (right,)) == right
    for reason in (UspsReturnReason.REFUSED, UspsReturnReason.UNCLAIMED):
        assert classify_return(left, right, reason, False) is ReturnCause.AGENCY_SIDE
    assert classify_return(left, right, None, False) is ReturnCause.UNKNOWN
    assert classify_return(left, right, UspsReturnReason.REFUSED, True) is ReturnCause.OUR_ERROR


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("PO Box 12", "PO Box 13"),
        ("12 Main Street", "13 Main Street"),
        ("12 Main St Suite 1", "12 Main St Suite 2"),
        ("12 Main St Room 1", "12 Main St Room 2"),
        ("12 Main St North", "12 Main St South"),
        ("12 Main St Example CA 90001", "12 Main St Elsewhere CA 90001"),
        ("12 Main St Example CA 90001", "12 Main St Example NY 90001"),
        ("12 Main St Example CA 90001", "12 Main St Example CA 90002"),
        ("12 Main St Example CA 90001-1234", "12 Main St Example CA 90001-1235"),
    ],
)
def test_distinct_destinations_stay_distinct(left: str, right: str) -> None:
    assert research_key(left) != research_key(right)
    assert return_key(left) != return_key(right)
    assert classify_return(left, right, UspsReturnReason.REFUSED, False) is ReturnCause.OUR_ERROR


def test_comparison_alias_never_substitutes_for_exact_mailed_address_identity() -> None:
    decision = verified_decision()
    alias = replace(decision.address, line1="1 Main Street")
    assert research_key(alias.line1) == research_key(decision.address.line1)
    assert address_fingerprint(alias) != decision.fingerprint
    result = classify_address_failure(
        decision,
        failure(mailed_address_fingerprint=address_fingerprint(alias)),
    )
    assert result.reason == "MAILED_BYTES_DIFFER_FROM_DECISION"


@pytest.mark.parametrize(
    ("address", "expected"),
    [("12 İstiklal Street", "12 i stiklal st"), ("12 Straße Avenue", "12 strasse ave")],
)
def test_both_entry_points_use_the_existing_research_casefold(address: str, expected: str) -> None:
    assert research_key(address) == expected
    assert return_key(address) == expected.upper()
