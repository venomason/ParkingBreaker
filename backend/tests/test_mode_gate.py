from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest
from hypothesis import given
from hypothesis import strategies as st

from parkingbreaker.agencies import ApprovedCapability, IssuerType, ServiceMode
from parkingbreaker.mode_gate import Operation, mode_permission
from parkingbreaker.service_policy import ServiceControlCategory, ServiceControlEntry
from samples import SAN_FRANCISCO, TEST_BASIS


@pytest.mark.parametrize("mode", list(ServiceMode))
@pytest.mark.parametrize("operation", list(Operation))
def test_recorded_state_basis_allows_clerical_operations(
    mode: ServiceMode, operation: Operation
) -> None:
    basis = replace(TEST_BASIS, service_mode=mode)
    permission = mode_permission(SAN_FRANCISCO, operation, state_basis={"CA": basis})
    assert permission.allowed
    assert permission.reason is None


@pytest.mark.parametrize("operation", list(Operation))
@pytest.mark.parametrize("missing", ["absent", "incomplete", "wrong-key"])
def test_missing_basis_blocks_every_operation(operation: Operation, missing: str) -> None:
    basis = (
        {}
        if missing == "absent"
        else {
            "CA": replace(
                TEST_BASIS,
                checked_by="" if missing == "incomplete" else "test",
                key="NV" if missing == "wrong-key" else "CA",
            )
        }
    )
    assert not mode_permission(SAN_FRANCISCO, operation, state_basis=basis).allowed
    assert not mode_permission(SAN_FRANCISCO, operation, state_basis={}).allowed


@pytest.mark.parametrize(
    ("issuer_type", "key"),
    [
        (IssuerType.MUNICIPAL, "CA"),
        (IssuerType.FEDERAL, "US-FED"),
        (IssuerType.TRIBAL, "TRIBE:123"),
    ],
)
def test_missing_basis_message_names_key_and_recorded_basis_allows_operation(
    issuer_type: IssuerType, key: str
) -> None:
    agency = replace(SAN_FRANCISCO, issuer_type=issuer_type, basis_key=key)
    permission = mode_permission(agency, Operation.RESEARCH, state_basis={})
    assert not permission.allowed
    assert permission.reason == f"basis key {key!r} has no recorded service basis"
    assert mode_permission(
        agency, Operation.RESEARCH, state_basis={key: replace(TEST_BASIS, key=key)}
    ).allowed


@pytest.mark.parametrize("operation", list(Operation))
def test_production_california_basis_allows_clerical_operations(operation: Operation) -> None:
    assert mode_permission(SAN_FRANCISCO, operation).allowed


@pytest.mark.parametrize("operation", list(Operation))
@pytest.mark.parametrize("scope", [SAN_FRANCISCO.id, "CA"])
def test_control_blocks_every_operation(operation: Operation, scope: str) -> None:
    entry = ServiceControlEntry(
        scope, ServiceControlCategory.PROVIDER, "Provider problem", "owner", date(2026, 10, 1)
    )
    permission = mode_permission(
        SAN_FRANCISCO, operation, state_basis={"CA": TEST_BASIS}, controls=(entry,)
    )
    assert not permission.allowed
    assert permission.reason == entry.detail


@pytest.mark.parametrize("mode", list(ServiceMode))
@pytest.mark.parametrize("operation", list(Operation))
@given(capability=st.text())
def test_any_named_capability_is_blocked(
    mode: ServiceMode, operation: Operation, capability: str
) -> None:
    caps = (
        (ApprovedCapability("recommend_defense", "Synthetic basis"),)
        if mode is ServiceMode.EXPANDED
        else ()
    )
    basis = replace(TEST_BASIS, service_mode=mode, approved_capabilities=caps)
    assert not mode_permission(
        SAN_FRANCISCO, operation, capability, state_basis={"CA": basis}
    ).allowed
