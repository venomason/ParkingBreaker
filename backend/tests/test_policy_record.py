from __future__ import annotations

import ast
from dataclasses import fields, replace
from datetime import date
from pathlib import Path
from typing import Any

import parkingbreaker
from parkingbreaker.policy_record import (
    RECORDED_POLICY_FINGERPRINT,
    PolicyRecord,
    current_policy,
    policy_fingerprint,
)
from parkingbreaker.service_policy import IntakeStop, intake_customer_stop


def test_live_rules_match_the_recorded_owner_policy() -> None:
    policy = current_policy()
    assert policy == PolicyRecord(
        prices_cents=(1999, 2499, 2999),
        price_split="equal_thirds_per_customer",
        certified_only=True,
        max_ticket_age_days=10,
        held_states=("AZ", "NV"),
        nationwide_intake=True,
        charge_trigger="first_usps_scan_or_t17_expiry_fallback",
    )
    assert policy_fingerprint(policy) == RECORDED_POLICY_FINGERPRINT


def test_changing_any_rule_changes_the_fingerprint() -> None:
    policy = current_policy()
    changed: dict[str, Any] = {
        "prices_cents": (2900,),
        "price_split": "all_one_price",
        "certified_only": False,
        "max_ticket_age_days": 11,
        "held_states": ("AZ",),
        "nationwide_intake": False,
        "charge_trigger": "on_lob_acceptance",
    }
    assert set(changed) == {field.name for field in fields(PolicyRecord)}
    for name, value in changed.items():
        assert policy_fingerprint(replace(policy, **{name: value})) != RECORDED_POLICY_FINGERPRINT


def test_production_composes_no_launch_allowlist() -> None:
    source = (Path(parkingbreaker.__file__).parent / "config.py").read_text()
    values = [
        keyword.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg == "launch_allowlist"
    ]
    assert values
    assert all(isinstance(v, ast.Constant) and v.value is None for v in values)


def test_intake_cutoff_reads_the_recorded_age() -> None:
    issued = date(2026, 10, 1)
    age = current_policy().max_ticket_age_days
    assert intake_customer_stop(issued, date(2026, 10, 1 + age), False) is None
    assert intake_customer_stop(issued, date(2026, 10, 2 + age), False) is IntakeStop.TOO_OLD
