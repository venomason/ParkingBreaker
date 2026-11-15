"""T30: a customer-directed address that is the ticket's PAYMENT address is refused.

The commonest customer mistake is typing the payment lockbox instead of the appeal address.
Payment addresses are read mechanically from the case's saved official research; a match is
refused with a fixed code before consent. Pure matcher tests, then the API on real SQLite.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from parkingbreaker.address_policy import (
    payment_address_matches,
    payment_addresses_in_text,
)
from parkingbreaker.agencies import MailingAddress
from parkingbreaker.case_service import CaseService
from parkingbreaker.research_records import decode_research, research_json
from test_case_service import fake_resolution
from test_t30_customer_directed import (
    ADDRESS,
    decision_history,
    post,
    unverified_case,
)

assert fake_resolution

CODE = "destination_address_is_payment_address"
PAYMENT_PAGE = (
    "Pay your citation online or mail payments to: City Parking, PO Box 7426, "
    "San Francisco, CA 94120-7426. "
    "To appeal, mail your appeal to: Citation Review, 500 Chosen Street, "
    "San Francisco, CA 94105."
)


def addr(line1: str, zip_code: str = "94120") -> MailingAddress:
    return MailingAddress("Someone", None, line1, None, "San Francisco", "CA", zip_code)


# ------------------------------------------------------------------------ pure matcher


def test_payment_addresses_read_from_payment_wording_only() -> None:
    known = payment_addresses_in_text(PAYMENT_PAGE)
    assert known == {("po box 7426", "94120")}


@pytest.mark.parametrize(
    ("line1", "zip_code"),
    [
        ("PO Box 7426", "94120"),
        ("P.O. Box 7426", "94120"),
        ("p o box 7426", "94120"),
        ("PO BOX 7426", "94120-7426"),
        ("Post Office Box 7426", "94120"),
        ("P.O.Box 7426", "94120-0001"),
    ],
)
def test_formatting_variants_match(line1: str, zip_code: str) -> None:
    known = payment_addresses_in_text(PAYMENT_PAGE)
    assert payment_address_matches(addr(line1, zip_code), known)


def test_other_box_other_zip_or_appeal_address_do_not_match() -> None:
    known = payment_addresses_in_text(PAYMENT_PAGE)
    assert not payment_address_matches(addr("PO Box 7427"), known)
    assert not payment_address_matches(addr("PO Box 7426", "94121"), known)
    assert not payment_address_matches(addr("500 Chosen St", "94105"), known)
    assert not payment_address_matches(addr("PO Box 7426"), frozenset())


def test_street_payment_address_matches_with_suffix_abbreviation() -> None:
    text = (
        "Make payments in person at: Cashier, 1 Dr Carlton B Goodlett Place, "
        "San Francisco, CA 94102."
    )
    known = payment_addresses_in_text(text)
    assert payment_address_matches(addr("1 Dr. Carlton B. Goodlett Pl.", "94102-4000"), known)


def test_address_shared_by_payment_and_appeal_is_not_a_payment_address() -> None:
    text = "Mail payments or appeals to: PO Box 99, Oakland, CA 94601."
    assert payment_addresses_in_text(text) == frozenset()


def test_contest_address_listed_first_is_not_marked_by_a_later_payment_cue() -> None:
    text = (
        "Mail your appeal to PO Box 11, Oakland, CA 94601. "
        "Mail payments to PO Box 22, Oakland, CA 94601."
    )
    assert payment_addresses_in_text(text) == {("po box 22", "94601")}


# ----------------------------------------------------------------------------- API


def with_payment_page(real: CaseService, case: str, page: str = PAYMENT_PAGE) -> None:
    record = real._intake(case)
    saved = real._store.get_research(record.intake_id)
    assert saved is not None
    result = decode_research(saved.research_json)
    first = result.sources[0]
    sources = (replace(first, text=f"{first.text}\n{page}"), *result.sources[1:])
    updated = replace(result, sources=sources)
    real._store.save_research(
        record.intake_id,
        research_json(updated),
        saved.answers_json,
        saved.updated_at,
        saved.revision,
    )


def revisions(real: CaseService, case: str) -> int:
    record = real._intake(case)
    return len(real._store.customer_address_revisions(record.intake_id, record.owner))


PAYMENT = {
    **ADDRESS,
    "recipient": "Me",
    "line1": "PO Box 7426",
    "city": "San Francisco",
    "zip_code": "94120",
}


@pytest.mark.anyio
@pytest.mark.parametrize("variant", [{}, {"line1": "P.O. BOX 7426"}, {"zip_code": "94120-7426"}])
async def test_payment_address_refused_before_consent_and_nothing_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variant: dict[str, str], anyio_backend: str
) -> None:
    client, real, case, _lob = await unverified_case(tmp_path, monkeypatch)
    try:
        with_payment_page(real, case)
        body = {**PAYMENT, **variant}
        response = await post(client, case, "address", body)
        assert response.status_code == 409
        assert "destination_verification_required" in response.text
        assert "7426" not in response.text
        assert "Me" not in response.json().get("detail", "")
        assert revisions(real, case) == 0
        assert decision_history(real, case) == 0
        alternative = await post(client, case, "address", ADDRESS)
        assert alternative.status_code == 409
        assert real.status(case).destination_choice is None
    finally:
        await client.aclose()
        real._store.close()


@pytest.mark.anyio
async def test_unrelated_case_without_payment_address_is_unaffected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, anyio_backend: str
) -> None:
    client, real, case, _lob = await unverified_case(tmp_path, monkeypatch)
    try:
        response = await post(client, case, "address", PAYMENT)
        assert response.status_code == 409
        assert response.json() == {"detail": "destination_verification_required"}
        assert revisions(real, case) == 0
    finally:
        await client.aclose()
        real._store.close()


@pytest.mark.anyio
async def test_consent_path_refuses_a_payment_address_saved_earlier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, anyio_backend: str
) -> None:
    client, real, case, _lob = await unverified_case(tmp_path, monkeypatch)
    try:
        with_payment_page(real, case)
        response = await post(
            client,
            case,
            "consent",
            {
                "address_revision": 1,
                "address_fingerprint": "a" * 64,
                "disclosure_sha256": "b" * 64,
                "accepted": True,
            },
        )
        assert response.status_code == 409
        assert response.json() == {"detail": "destination_verification_required"}
        assert revisions(real, case) == 0
        assert decision_history(real, case) == 0
    finally:
        await client.aclose()
        real._store.close()
