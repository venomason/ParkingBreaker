from __future__ import annotations

from dataclasses import fields, replace

import pytest

from parkingbreaker.agencies import IssuerType, ResolvedAgency, SourceScope, validate_basis_key
from parkingbreaker.research.sources import is_allowed
from samples import SAN_FRANCISCO


def test_per_ticket_identity_has_no_remembered_research_or_activation() -> None:
    assert {field.name for field in fields(ResolvedAgency)} == {
        "id",
        "name",
        "state",
        "official_domains",
        "issuer_type",
        "deciding_body",
        "basis_key",
        "delegated_scopes",
    }
    assert SAN_FRANCISCO.issuer_type is IssuerType.MUNICIPAL
    assert SAN_FRANCISCO.deciding_body == SAN_FRANCISCO.name
    assert SAN_FRANCISCO.basis_key == SAN_FRANCISCO.state
    assert SAN_FRANCISCO.official_domains == ("sfmta.com",)
    assert is_allowed("https://www.sfmta.com/contest", SAN_FRANCISCO.source_scopes())
    other = replace(SAN_FRANCISCO, id="unfamiliar-agency", official_domains=("new-city.gov",))
    assert is_allowed("https://new-city.gov/contest", other.source_scopes())
    assert not is_allowed("https://sfmta.com/contest", other.source_scopes())


@pytest.mark.parametrize(
    "domains",
    [
        (),
        ("SFMTA.com",),
        ("https://sfmta.com",),
        ("sfmta.com/x",),
        ("*.sfmta.com",),
        ("a.com", "a.com"),
        ("sfmta.com:443",),
    ],
)
def test_official_domains_are_validated(domains: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="domain"):
        replace(SAN_FRANCISCO, official_domains=domains)


@pytest.mark.parametrize("state", ["", "ca", "California", "C1"])
def test_state_is_resolved_uppercase_code(state: str) -> None:
    with pytest.raises(ValueError, match="state"):
        replace(SAN_FRANCISCO, state=state)


@pytest.mark.parametrize("field", ["id", "name", "deciding_body"])
def test_identity_cannot_be_blank(field: str) -> None:
    with pytest.raises(ValueError, match="blank"):
        replace(SAN_FRANCISCO, **{field: "  "})  # type: ignore[arg-type]


@pytest.mark.parametrize("key", ["CA", "DC", "PR", "GU", "US-FED", "TRIBE:123", "TRIBE:Ab.c_0-9"])
def test_service_basis_key_shapes(key: str) -> None:
    validate_basis_key(key)


@pytest.mark.parametrize(
    "key",
    [
        "",
        "ca",
        "C1",
        "California",
        " CA",
        "CA\n",
        "US-fed",
        "US-FED ",
        "US-FED:x",
        "TRIBE:",
        "tribe:123",
        "TRIBE:a/b",
        "TRIBE:a b",
        "TRIBE:é",
        "TRIBE:123\n",
    ],
)
def test_invalid_basis_key_rejected_by_validator_and_agency(key: str) -> None:
    with pytest.raises(ValueError, match="basis key"):
        validate_basis_key(key)
    with pytest.raises(ValueError, match="basis key"):
        replace(SAN_FRANCISCO, basis_key=key)


@pytest.mark.parametrize("issuer_type", list(IssuerType))
def test_issuer_type_requires_its_own_basis(issuer_type: IssuerType) -> None:
    key = (
        "US-FED"
        if issuer_type is IssuerType.FEDERAL
        else "TRIBE:Ab.c_0-9"
        if issuer_type is IssuerType.TRIBAL
        else "CA"
    )
    agency = replace(SAN_FRANCISCO, issuer_type=issuer_type, basis_key=key)
    assert agency.state == "CA"
    assert agency.basis_key == key
    for other_key in {"CA", "NV", "US-FED", "TRIBE:123"} - {key}:
        if issuer_type is IssuerType.TRIBAL and other_key.startswith("TRIBE:"):
            continue
        with pytest.raises(ValueError, match="basis_key"):
            replace(agency, basis_key=other_key)


def test_issuer_type_must_be_enum_and_deciding_body_may_differ() -> None:
    with pytest.raises(ValueError, match="issuer_type"):
        replace(SAN_FRANCISCO, issuer_type="MUNICIPAL")  # type: ignore[arg-type]
    assert replace(SAN_FRANCISCO, deciding_body="County court").deciding_body == "County court"


@pytest.mark.parametrize(
    "host",
    [
        "",
        "com",
        "Portal.com",
        " portal.com",
        "portal.com.",
        "https://portal.com",
        "portal.com/sf",
        "portal.com:443",
        "*.portal.com",
        "127.0.0.1",
        "[::1]",
        "xn--portal-9ua.com",
        "pórtal.com",
        "portal..com",
        "-portal.com",
        "portal.com\n",
    ],
)
def test_source_scope_rejects_invalid_hosts(host: str) -> None:
    with pytest.raises(ValueError, match=r"domain|punycode"):
        SourceScope(host)


@pytest.mark.parametrize(
    "prefix",
    [
        "",
        "/",
        "sf",
        "/sf/",
        "//sf",
        "/sf//contest",
        "/sf/.",
        "/sf/..",
        "/sf/../contest",
        "/sf/./contest",
        "/sf.other",
        "/sf%2fcontest",
        "/sf\\contest",
        "/sf?x",
        "/sf#x",
        "/s f",
        "/sf\n",
        "/sƒ",
    ],
)
def test_source_scope_rejects_invalid_prefixes(prefix: str) -> None:
    with pytest.raises(ValueError, match="path_prefix"):
        SourceScope("portal.com", prefix)


def test_source_scopes_are_frozen_and_preserve_path_case() -> None:
    scope = SourceScope("portal.com", "/SF/contest")
    assert scope.path_prefix == "/SF/contest"
    with pytest.raises(AttributeError):
        scope.host = "evil.com"  # type: ignore[misc]


def test_agency_combines_official_and_delegated_scopes() -> None:
    delegated = (SourceScope("portal.com", "/sf"), SourceScope("portal.com", "/sf-forms"))
    agency = replace(SAN_FRANCISCO, delegated_scopes=delegated)
    assert agency.source_scopes() == (SourceScope("sfmta.com"), *delegated)
    assert SAN_FRANCISCO.delegated_scopes == ()


@pytest.mark.parametrize(
    "delegated",
    [
        (SourceScope("portal.com"),),
        (SourceScope("portal.com", "/sf"), SourceScope("portal.com", "/sf")),
    ],
)
def test_agency_rejects_unscoped_or_duplicate_delegations(
    delegated: tuple[SourceScope, ...],
) -> None:
    with pytest.raises(ValueError, match="delegated"):
        replace(SAN_FRANCISCO, delegated_scopes=delegated)
