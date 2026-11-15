"""Literal and adopted ticket facts bind explicitly; nothing is defaulted."""

from __future__ import annotations

import ast
import base64
import copy
import dataclasses
import hashlib
import json
from datetime import date, datetime
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

import parkingbreaker
from parkingbreaker.api.schemas import TicketFacts
from parkingbreaker.llm import ImageAttachment
from parkingbreaker.ticket_fact_binding import (
    ADOPTED_FIELD_ORDER,
    LITERAL_FIELD_ORDER,
    TicketFactBinding,
    TicketFactBindingError,
    ordered_images_digest,
    review_binding_digest,
)

LITERAL_BASE: dict[str, object] = {
    "citation_number": "SF-123",
    "license_plate": "ABC123",
    "license_plate_state": "CA",
    "issuer_name": "SFMTA",
    "issue_date": "03/14/2026",
    "notice_date": None,
    "printed_deadline": "04/01/2026",
    "violation_text": "Street cleaning",
    "issued_city": "San Francisco",
    "issued_state": "CA",
    "stage_text": "NOTICE OF VIOLATION",
}

ADOPTED_BASE: dict[str, object] = {
    "citation_number": "SF-123",
    "license_plate": "ABC123",
    "license_plate_state": "CA",
    "issuer_hint": "SFMTA",
    "issued_city": "San Francisco",
    "issued_state": "CA",
    "issuer_kind": "GOVERNMENT",
    "violation_code": "VC-1",
    "violation_description": "Street cleaning",
    "issue_date": "2026-03-14",
    "stage": "first-level review",
    "notice_date": None,
    "printed_deadline": "2026-04-01",
}

MUTATIONS: dict[str, object] = {
    "citation_number": "SF-999",
    "license_plate": "XYZ999",
    "license_plate_state": "NV",
    "issuer_hint": "Other agency",
    "issued_city": "Oakland",
    "issued_state": "NV",
    "issuer_kind": "PRIVATE",
    "violation_code": "VC-2",
    "violation_description": "Other violation",
    "issue_date": date(2026, 3, 15),
    "stage": "second-level review",
    "notice_date": date(2026, 3, 1),
    "printed_deadline": None,
}


def literal_with(**changes: object) -> dict[str, object]:
    values = dict(LITERAL_BASE)
    values.update(changes)
    return values


def adopted_with(**changes: object) -> dict[str, object]:
    values = dict(ADOPTED_BASE)
    values.update(changes)
    return values


def make(
    literal: dict[str, object] | None = None, adopted: dict[str, object] | None = None
) -> TicketFactBinding:
    return TicketFactBinding.bind(
        literal_with() if literal is None else literal,
        adopted_with() if adopted is None else adopted,
    )


def test_every_one_of_the_thirteen_adopted_properties_binds() -> None:
    assert len(ADOPTED_FIELD_ORDER) == 13
    assert set(ADOPTED_FIELD_ORDER) == set(MUTATIONS)
    binding = make()
    facts = TicketFacts.model_validate(ADOPTED_BASE)

    assert binding.matches(facts)
    for key in ADOPTED_FIELD_ORDER:
        assert not binding.matches(facts.model_copy(update={key: MUTATIONS[key]})), key


def test_every_adopted_property_must_be_supplied() -> None:
    for key in ADOPTED_FIELD_ORDER:
        values = adopted_with()
        del values[key]
        with pytest.raises(TicketFactBindingError):
            make(adopted=values)


def test_optional_dates_are_explicit_null_not_omitted_or_defaulted() -> None:
    binding = make(adopted=adopted_with(notice_date=None, printed_deadline=None))
    assert dict(binding.adopted)["notice_date"] is None
    assert dict(binding.adopted)["printed_deadline"] is None
    for key in ("notice_date", "printed_deadline"):
        values = adopted_with()
        del values[key]
        with pytest.raises(TicketFactBindingError):
            make(adopted=values)


def test_defaults_are_never_filled_for_stage_issuer_kind_or_violation_code() -> None:
    for key in ("stage", "issuer_kind", "violation_code"):
        values = adopted_with()
        del values[key]
        with pytest.raises(TicketFactBindingError):
            make(adopted=values)
    defaulted = adopted_with()
    del defaulted["stage"]
    assert TicketFacts.model_validate(defaulted).stage == "first-level review"


@pytest.mark.parametrize(
    "key",
    [
        "reason",
        "customer_reason",
        "reason_text",
        "contact",
        "customer_contact",
        "statement",
        "customer_statement",
        "workspace",
        "rationale",
        "extra",
    ],
)
def test_reasons_contacts_stories_and_extra_keys_never_enter_either_projection(
    key: str,
) -> None:
    literal = literal_with()
    literal[key] = "not allowed"
    with pytest.raises(TicketFactBindingError):
        make(literal=literal)
    adopted = adopted_with()
    adopted[key] = "not allowed"
    with pytest.raises(TicketFactBindingError):
        make(adopted=adopted)


def test_every_literal_key_must_be_supplied() -> None:
    assert len(LITERAL_FIELD_ORDER) == 11
    for key in LITERAL_FIELD_ORDER:
        values = literal_with()
        del values[key]
        with pytest.raises(TicketFactBindingError):
            make(literal=values)


def test_a_literal_is_a_bounded_visible_line_or_an_explicit_null() -> None:
    empty = make(literal=dict.fromkeys(LITERAL_FIELD_ORDER))
    assert empty.review_fields() == {}
    for value in ("", "   ", "line\nbreak", "tab\there", "\x00", "x" * 201):
        with pytest.raises(TicketFactBindingError):
            make(literal=literal_with(citation_number=value))
    for invalid in (3, True, date(2026, 1, 1), b"text"):
        with pytest.raises(TicketFactBindingError):
            make(literal=literal_with(citation_number=invalid))


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("citation_number", 123),
        ("license_plate", True),
        ("stage", 1),
        ("stage", ""),
        ("issuer_kind", True),
        ("issuer_kind", "government"),
        ("violation_code", ""),
        ("violation_description", None),
        ("license_plate_state", "ca"),
        ("issued_state", "California"),
        ("issue_date", 123),
        ("issue_date", True),
        ("issue_date", datetime(2026, 3, 14, 12, 0)),
        ("notice_date", 1),
        ("notice_date", datetime(2026, 3, 14, 12, 0)),
    ],
)
def test_adopted_wrong_types_bools_and_schema_violations_reject(key: str, value: object) -> None:
    with pytest.raises(TicketFactBindingError):
        make(adopted=adopted_with(**{key: value}))


@pytest.mark.parametrize(
    "value",
    [
        "2026/03/14",
        "03/14/2026",
        "3/14/2026",
        "14.03.2026",
        "Mar 14, 2026",
        "2026-3-14",
        "20260314",
        "2026-03-14T00:00:00",
        "2026-03-14T00:00:00Z",
        "2026-03-14 12:00:00",
        "2026-02-30",
        " 2026-03-14",
        "2026-03-14 ",
    ],
)
def test_ambiguous_or_non_iso_adopted_dates_reject(value: str) -> None:
    with pytest.raises(TicketFactBindingError):
        make(adopted=adopted_with(issue_date=value))
    with pytest.raises(TicketFactBindingError):
        make(adopted=adopted_with(notice_date=value))


def test_a_date_value_and_its_iso_string_are_the_same_adopted_fact() -> None:
    as_date = make(adopted=adopted_with(issue_date=date(2026, 3, 14)))
    as_text = make()
    assert as_date.adopted == as_text.adopted
    assert as_date.closed_field_digest == as_text.closed_field_digest
    assert dict(as_date.adopted)["issue_date"] == "2026-03-14"


def test_literal_slash_date_is_preserved_and_never_parsed_into_adopted() -> None:
    binding = make()
    assert dict(binding.literal)["issue_date"] == "03/14/2026"
    assert dict(binding.adopted)["issue_date"] == "2026-03-14"
    assert binding.review_fields()["issue_date"] == "03/14/2026"

    changed = make(literal=literal_with(issue_date="2026-03-14"))
    assert dict(changed.literal)["issue_date"] == "2026-03-14"
    assert changed.adopted == binding.adopted
    assert changed.closed_field_digest != binding.closed_field_digest
    assert make(literal=literal_with(issue_date="14/03/2026")).review_fields()["issue_date"] == (
        "14/03/2026"
    )


def test_closed_field_digest_is_stable_and_binds_every_literal_change() -> None:
    base = make()
    assert base.closed_field_digest == make().closed_field_digest
    assert len(base.closed_field_digest) == 64
    for key in LITERAL_FIELD_ORDER:
        current = dict(base.literal)[key]
        replacement = "x" if current is None else current + "x"
        changed = make(literal=literal_with(**{key: replacement}))
        assert changed.closed_field_digest != base.closed_field_digest, key


def test_closed_field_digest_binds_every_adopted_change() -> None:
    base = make()
    for key in ADOPTED_FIELD_ORDER:
        changed = make(adopted=adopted_with(**{key: MUTATIONS[key]}))
        assert changed.closed_field_digest != base.closed_field_digest, key


def test_review_fields_return_only_non_null_literal_values() -> None:
    binding = make(literal=literal_with(citation_number="LIT-1", printed_deadline=None))
    review = binding.review_fields()

    assert set(review) <= set(LITERAL_FIELD_ORDER)
    assert review["citation_number"] == "LIT-1"
    assert dict(binding.adopted)["citation_number"] == "SF-123"
    assert "notice_date" not in review
    assert "printed_deadline" not in review
    for application_only in (
        "issuer_kind",
        "violation_code",
        "stage",
        "issuer_hint",
        "violation_description",
    ):
        assert application_only not in review
    for forbidden in ("reason", "statement", "contact", "workspace", "rationale"):
        assert forbidden not in review

    review["citation_number"] = "mutated"
    assert binding.review_fields()["citation_number"] == "LIT-1"


def test_inputs_are_not_edited_and_the_envelope_is_frozen() -> None:
    literal = literal_with()
    adopted = adopted_with()
    literal_snapshot = copy.deepcopy(literal)
    adopted_snapshot = copy.deepcopy(adopted)
    binding = make(literal=literal, adopted=adopted)
    assert literal == literal_snapshot
    assert adopted == adopted_snapshot

    facts = TicketFacts.model_validate(adopted)
    before = facts.model_dump()
    assert binding.matches(facts)
    assert facts.model_dump() == before

    with pytest.raises(dataclasses.FrozenInstanceError):
        binding.literal = ()  # type: ignore[misc]


def test_direct_construction_cannot_bypass_validation() -> None:
    binding = make()
    with pytest.raises(TicketFactBindingError):
        TicketFactBinding(binding.literal[:-1], binding.adopted)
    with pytest.raises(TicketFactBindingError):
        TicketFactBinding(binding.literal, binding.adopted[:-1])
    with pytest.raises(TicketFactBindingError):
        TicketFactBinding(
            (("citation_number", "line\nbreak"), *binding.literal[1:]), binding.adopted
        )


def test_the_module_has_no_provider_disk_environment_clock_or_store_access() -> None:
    tree = ast.parse((Path(parkingbreaker.__file__).parent / "ticket_fact_binding.py").read_text())
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)

    allowed_roots = {
        "__future__",
        "base64",
        "binascii",
        "collections",
        "dataclasses",
        "datetime",
        "hashlib",
        "json",
        "pydantic",
        "parkingbreaker",
        "re",
        "typing",
    }
    assert {module.split(".")[0] for module in modules} <= allowed_roots
    assert not any("store" in module for module in modules)
    assert "parkingbreaker.config" not in modules
    assert "parkingbreaker.api.app" not in modules
    assert "parkingbreaker.api.service" not in modules

    attribute_calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not attribute_calls & {"now", "today", "time", "open", "read_text", "write_text"}
    name_calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not name_calls & {"open", "eval", "exec", "input"}


def b64(payload: bytes) -> str:
    return base64.b64encode(payload).decode("ascii")


def png_bytes(size: tuple[int, int] = (8, 8), colour: tuple[int, int, int] = (10, 20, 30)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


def jpeg_bytes(
    size: tuple[int, int] = (8, 8), colour: tuple[int, int, int] = (40, 50, 60)
) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="JPEG")
    return buffer.getvalue()


def image(media_type: str = "image/png", payload: bytes | None = None) -> ImageAttachment:
    if payload is None:
        payload = png_bytes() if media_type == "image/png" else jpeg_bytes()
    return ImageAttachment(media_type=media_type, data_base64=b64(payload))


def test_ordered_images_digest_is_stable_and_binds_content_order_and_mime() -> None:
    first = image(payload=png_bytes(colour=(1, 2, 3)))
    second = image(payload=png_bytes(colour=(4, 5, 6)))
    base = ordered_images_digest([first, second])

    assert base == ordered_images_digest([first, second])
    assert len(base) == 64
    assert ordered_images_digest([second, first]) != base
    assert ordered_images_digest([first, image(payload=png_bytes(colour=(7, 8, 9)))]) != base
    assert ordered_images_digest([first]) != ordered_images_digest([image(media_type="image/jpeg")])
    assert ordered_images_digest([first]) == ordered_images_digest(
        [image(payload=png_bytes(colour=(1, 2, 3)))]
    )


def test_unsupported_or_malformed_media_refuses_before_any_gateway() -> None:
    good = image()
    cases: list[list[ImageAttachment]] = [
        [],
        [good, good, good],
        [ImageAttachment(media_type="image/webp", data_base64=b64(b"webp bytes"))],
        [ImageAttachment(media_type="application/pdf", data_base64=b64(b"%PDF-1.4"))],
        [ImageAttachment(media_type="image/png", data_base64="not base64 !!")],
        [ImageAttachment(media_type="image/png", data_base64="")],
        [ImageAttachment(media_type="image/jpeg", data_base64=b64(png_bytes()))],
        [ImageAttachment(media_type="image/png", data_base64=b64(b"offline ticket"))],
    ]
    for images in cases:
        with pytest.raises(TicketFactBindingError):
            ordered_images_digest(images)


def test_review_binding_digest_is_stable_and_domain_separated() -> None:
    images_digest = ordered_images_digest([image()])
    fields_digest = make().closed_field_digest
    combined = review_binding_digest(images_digest, fields_digest)

    assert combined == review_binding_digest(images_digest, fields_digest)
    assert len(combined) == 64
    assert combined not in {images_digest, fields_digest}
    assert (
        review_binding_digest(
            ordered_images_digest([image(payload=png_bytes(colour=(9, 9, 9)))]), fields_digest
        )
        != combined
    )
    assert (
        review_binding_digest(
            images_digest,
            make(adopted=adopted_with(stage="second-level review")).closed_field_digest,
        )
        != combined
    )
    with pytest.raises(TicketFactBindingError):
        review_binding_digest("bad", fields_digest)
    with pytest.raises(TicketFactBindingError):
        review_binding_digest(images_digest, "bad")


def test_review_binding_digest_uses_the_agreed_store_array_protocol() -> None:
    images_digest = "a" * 64
    fields_digest = "b" * 64
    expected = hashlib.sha256(
        json.dumps(
            ["ticket-review-binding-v1", images_digest, fields_digest], separators=(",", ":")
        ).encode("ascii")
    ).hexdigest()

    assert review_binding_digest(images_digest, fields_digest) == expected
    assert expected == "938f5339e1cb89ce5168bd7bfb0e850ea822523261199636de4a342343c26cf3"
