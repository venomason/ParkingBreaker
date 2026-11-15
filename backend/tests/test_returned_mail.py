from __future__ import annotations

import itertools

import pytest
from hypothesis import given
from hypothesis import strategies as st

from parkingbreaker.returned_mail import (
    AGENCY_SIDE_REASONS,
    MAX_REMAILS,
    Remedy,
    RemedyKind,
    ReturnCause,
    UspsReturnReason,
    classify_return,
    decide_remedy,
    normalize_address,
)

C = ReturnCause
K = RemedyKind
R = UspsReturnReason

OFFICIAL = "Parking Adjudication, 100 North Main Street, Suite 200, Springfield, IL 62701"
SAME = "PARKING ADJUDICATION 100 N MAIN ST STE 200 SPRINGFIELD IL 62701"
DIFFERENT = "Parking Adjudication, 101 North Main Street, Suite 200, Springfield, IL 62701"

PRICE = 2999
MAILING = 1150


def test_normalisation_ignores_case_punctuation_and_abbreviations() -> None:
    assert normalize_address(OFFICIAL) == normalize_address(SAME)
    assert normalize_address("  p.o. box   12 ") == normalize_address("PO Box 12")
    assert normalize_address(OFFICIAL) != normalize_address(DIFFERENT)


@pytest.mark.parametrize("reason", [*R, None])
def test_production_fault_is_always_our_error(reason: UspsReturnReason | None) -> None:
    for official in (OFFICIAL, None):
        for mailed in (SAME, DIFFERENT):
            assert classify_return(mailed, official, reason, True) is C.OUR_ERROR


@pytest.mark.parametrize("reason", [*R, None])
def test_mismatch_with_official_source_is_our_error(reason: UspsReturnReason | None) -> None:
    assert classify_return(DIFFERENT, OFFICIAL, reason, False) is C.OUR_ERROR


@pytest.mark.parametrize("reason", [*R, None])
def test_match_is_agency_side_only_when_refused_or_unclaimed(
    reason: UspsReturnReason | None,
) -> None:
    expected = C.AGENCY_SIDE if reason in AGENCY_SIDE_REASONS else C.UNKNOWN
    assert classify_return(SAME, OFFICIAL, reason, False) is expected


def test_agency_side_reasons_are_exactly_refused_and_unclaimed() -> None:
    assert {R.REFUSED, R.UNCLAIMED} == AGENCY_SIDE_REASONS


@pytest.mark.parametrize("reason", [*R, None])
def test_no_saved_official_source_is_unknown(reason: UspsReturnReason | None) -> None:
    assert classify_return(SAME, None, reason, False) is C.UNKNOWN


FREE = Remedy(K.FREE_REMAIL)
FULL = Remedy(K.FULL_REFUND, refund_cents=PRICE)
PROOF = Remedy(K.PROOF_PACKET)
PROOF_REMAIL = Remedy(K.PROOF_PACKET, remail_to_other_address=True)
PARTIAL = Remedy(K.PARTIAL_REFUND, refund_cents=PRICE - MAILING)

# (cause, feasible, other_address, remails_used) -> remedy; every combination listed.
TABLE: dict[tuple[ReturnCause, bool, bool, int], Remedy] = {
    (C.OUR_ERROR, True, True, 0): FREE,
    (C.OUR_ERROR, True, False, 0): FREE,
    (C.OUR_ERROR, False, True, 0): FULL,
    (C.OUR_ERROR, False, False, 0): FULL,
    (C.OUR_ERROR, True, True, 1): FULL,
    (C.OUR_ERROR, True, False, 1): FULL,
    (C.OUR_ERROR, False, True, 1): FULL,
    (C.OUR_ERROR, False, False, 1): FULL,
    (C.AGENCY_SIDE, True, True, 0): PROOF_REMAIL,
    (C.AGENCY_SIDE, True, False, 0): PROOF,
    (C.AGENCY_SIDE, False, True, 0): PROOF,
    (C.AGENCY_SIDE, False, False, 0): PROOF,
    (C.AGENCY_SIDE, True, True, 1): PROOF,
    (C.AGENCY_SIDE, True, False, 1): PROOF,
    (C.AGENCY_SIDE, False, True, 1): PROOF,
    (C.AGENCY_SIDE, False, False, 1): PROOF,
    (C.UNKNOWN, True, True, 0): FREE,
    (C.UNKNOWN, True, False, 0): FREE,
    (C.UNKNOWN, False, True, 0): PARTIAL,
    (C.UNKNOWN, False, False, 0): PARTIAL,
    (C.UNKNOWN, True, True, 1): PARTIAL,
    (C.UNKNOWN, True, False, 1): PARTIAL,
    (C.UNKNOWN, False, True, 1): PARTIAL,
    (C.UNKNOWN, False, False, 1): PARTIAL,
}


def test_table_is_exhaustive() -> None:
    combos = set(itertools.product(C, (True, False), (True, False), range(MAX_REMAILS + 1)))
    assert set(TABLE) == combos


@pytest.mark.parametrize(("key", "expected"), list(TABLE.items()))
def test_remedy_table(key: tuple[ReturnCause, bool, bool, int], expected: Remedy) -> None:
    cause, feasible, other, used = key
    assert decide_remedy(cause, feasible, other, PRICE, MAILING, used) == expected


def test_partial_refund_never_negative() -> None:
    remedy = decide_remedy(C.UNKNOWN, False, False, 500, 900, 0)
    assert remedy == Remedy(K.PARTIAL_REFUND, refund_cents=0)


def test_remails_property() -> None:
    assert FREE.remails
    assert PROOF_REMAIL.remails
    assert not FULL.remails
    assert not PROOF.remails
    assert not PARTIAL.remails


@pytest.mark.parametrize(
    ("price", "mailing", "used"),
    [(-1, 0, 0), (0, -1, 0), (100, 0, -1), (100, 0, MAX_REMAILS + 1)],
)
def test_rejects_invalid_inputs(price: int, mailing: int, used: int) -> None:
    with pytest.raises(ValueError, match="must"):
        decide_remedy(C.UNKNOWN, True, True, price, mailing, used)


@given(
    cause=st.sampled_from(C),
    feasible=st.booleans(),
    other=st.booleans(),
    price=st.integers(min_value=0, max_value=10_000_000),
    mailing=st.integers(min_value=0, max_value=10_000_000),
    used=st.integers(min_value=0, max_value=MAX_REMAILS),
)
def test_refund_never_exceeds_price(
    cause: ReturnCause, feasible: bool, other: bool, price: int, mailing: int, used: int
) -> None:
    remedy = decide_remedy(cause, feasible, other, price, mailing, used)
    assert isinstance(remedy.refund_cents, int)
    assert 0 <= remedy.refund_cents <= price
    if used >= MAX_REMAILS:
        assert not remedy.remails
    if cause is C.AGENCY_SIDE:
        assert remedy.refund_cents == 0
