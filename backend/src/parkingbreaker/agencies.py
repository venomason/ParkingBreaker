"""Per-ticket agency identity and shared mailing and assistance types. Pure, no I/O."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from enum import StrEnum


@dataclass(frozen=True)
class MailingAddress:
    recipient: str
    attention: str | None
    line1: str
    line2: str | None
    city: str
    state: str
    zip_code: str
    country: str = "US"


class LobVerificationStatus(StrEnum):
    UNVERIFIED = "unverified"
    DELIVERABLE = "deliverable"
    DELIVERABLE_WITH_CORRECTIONS = "deliverable_with_corrections"
    UNDELIVERABLE = "undeliverable"


@dataclass(frozen=True)
class LobVerification:
    status: LobVerificationStatus
    checked_on: date | None = None
    note: str | None = None


class ServiceMode(StrEnum):
    RESTRICTED_CLERICAL = "restricted_clerical"
    EXPANDED = "expanded"


class IssuerType(StrEnum):
    MUNICIPAL = "MUNICIPAL"
    COUNTY = "COUNTY"
    CONSOLIDATED_CITY_COUNTY = "CONSOLIDATED_CITY_COUNTY"
    STATE_AGENCY = "STATE_AGENCY"
    AUTHORITY = "AUTHORITY"
    PUBLIC_EDUCATION = "PUBLIC_EDUCATION"
    TRIBAL = "TRIBAL"
    FEDERAL = "FEDERAL"


@dataclass(frozen=True)
class ApprovedCapability:
    """One capability beyond RESTRICTED_CLERICAL, with the recorded basis for allowing it."""

    name: str
    basis: str

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("capability name must not be blank")
        if not self.basis.strip():
            raise ValueError(f"capability {self.name!r} has no recorded basis")


_DOMAIN = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def validate_basis_key(key: str) -> None:
    """State/DC/territory code, federal key, or a documented tribal identifier."""
    if not re.fullmatch(r"[A-Z]{2}|US-FED|TRIBE:[A-Za-z0-9._-]+", key):
        raise ValueError(
            "service basis key must be a two-letter uppercase code, US-FED, "
            "or TRIBE:<id> with id matching [A-Za-z0-9._-]+"
        )


def validate_domains(domains: tuple[str, ...]) -> None:
    """Bare lowercase host names only: no scheme, path, port, wildcard or duplicate."""
    for domain in domains:
        if not _DOMAIN.fullmatch(domain):
            raise ValueError(f"not a bare lowercase domain: {domain!r}")
    if len(set(domains)) != len(domains):
        raise ValueError(f"duplicate official domain in {domains!r}")


@dataclass(frozen=True)
class SourceScope:
    """An official host tree, or one exact host's delegated tenant path."""

    host: str
    path_prefix: str | None = None

    def __post_init__(self) -> None:
        validate_domains((self.host,))
        if any(label.startswith("xn--") for label in self.host.split(".")):
            raise ValueError("source scope host must not contain punycode")
        prefix = self.path_prefix
        if prefix is not None and (
            not prefix.startswith("/")
            or prefix.endswith("/")
            or any(not segment for segment in prefix[1:].split("/"))
            or any(c in prefix for c in ".%\\?#")
            or not prefix.isascii()
            or any(c.isspace() or not c.isprintable() for c in prefix)
        ):
            raise ValueError(f"invalid source scope path_prefix: {prefix!r}")


@dataclass(frozen=True)
class ResolvedAgency:
    """Per-ticket identity; state is the violation state, never the mailing state."""

    id: str
    name: str
    state: str
    official_domains: tuple[str, ...]
    issuer_type: IssuerType
    deciding_body: str
    basis_key: str
    delegated_scopes: tuple[SourceScope, ...] = ()

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.name.strip():
            raise ValueError("agency id and name must not be blank")
        if not re.fullmatch(r"[A-Z]{2}", self.state):
            raise ValueError("agency state must be a two-letter uppercase code")
        if not isinstance(self.issuer_type, IssuerType):
            raise ValueError("issuer_type must be an IssuerType")
        if not self.deciding_body.strip():
            raise ValueError("agency deciding_body must not be blank")
        validate_basis_key(self.basis_key)
        if self.issuer_type is IssuerType.FEDERAL:
            if self.basis_key != "US-FED":
                raise ValueError("FEDERAL issuer requires basis_key US-FED")
        elif self.issuer_type is IssuerType.TRIBAL:
            if not self.basis_key.startswith("TRIBE:"):
                raise ValueError("TRIBAL issuer requires a TRIBE: basis_key")
        elif self.basis_key != self.state:
            raise ValueError("state and local issuer basis_key must equal the violation state")
        if not self.official_domains:
            raise ValueError(f"{self.id}: no official domain given")
        validate_domains(self.official_domains)
        if any(scope.path_prefix is None for scope in self.delegated_scopes):
            raise ValueError("delegated scopes must have a path_prefix")
        if len(set(self.delegated_scopes)) != len(self.delegated_scopes):
            raise ValueError("duplicate delegated scope")

    def source_scopes(self) -> tuple[SourceScope, ...]:
        """The complete agency allowlist, preserving delegated path restrictions."""
        return tuple(SourceScope(host) for host in self.official_domains) + self.delegated_scopes
