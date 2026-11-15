"""Application permissions from the state basis and service controls (v7 R18). Pure, no I/O."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from parkingbreaker.agencies import ResolvedAgency
from parkingbreaker.service_policy import (
    SERVICE_CONTROL,
    STATE_BASIS,
    ServiceControlEntry,
    StateBasis,
    recorded_basis,
    service_control_entry,
)


class Operation(StrEnum):
    RESEARCH = "research"
    DRAFT = "draft"
    VERBATIM_REFERENCES = "verbatim_references"
    ADOPTION = "adoption"


@dataclass(frozen=True)
class ModePermission:
    allowed: bool
    reason: str | None = None


def mode_permission(
    agency: ResolvedAgency,
    operation: Operation,
    capability: str | None = None,
    *,
    state_basis: Mapping[str, StateBasis] = STATE_BASIS,
    controls: Sequence[ServiceControlEntry] = SERVICE_CONTROL,
) -> ModePermission:
    """Permit implemented clerical operations only with a recorded, uncontrolled basis."""
    entry = service_control_entry(agency, controls)
    if entry is not None:
        return ModePermission(False, entry.detail)
    if recorded_basis(agency, state_basis) is None:
        return ModePermission(
            False, f"basis key {agency.basis_key!r} has no recorded service basis"
        )
    if capability is not None:
        return ModePermission(False, f"capability {capability!r} is not implemented")
    return ModePermission(True)
