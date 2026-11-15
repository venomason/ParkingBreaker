"""T21 trap catalogue contract tests.

The catalogue is pure data transcribed from
build-kit/evidence/task-T21-2026-10-22/trap-catalogue-table-mimo.md. These tests pin the
transcription to this tree: ids are well formed and unique, and every fact key, guard
symbol, J-case and test node id the table names still exists.
"""

from __future__ import annotations

import importlib
import json
import re
from pathlib import Path
from typing import cast

import pytest

from parkingbreaker.research.questions import FactKey
from parkingbreaker.research.trap_catalog import GUARDED, UNGUARDED, by_id

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = Path(__file__).resolve().parents[1]
_CASES_PATH = REPO_ROOT / "build-kit" / "fixtures" / "jurisdiction-cases.json"
_SNAKE_CASE = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*")
_CASE_ID = re.compile(r"[A-Z][A-Z]?[0-9]+")
_J_ID = re.compile(r"\bJ[0-9]+\b")


def _guard_resolves(name: str) -> bool:
    """True when a dotted guard names an existing attribute chain under its module."""
    parts = name.split(".")
    for split in range(len(parts) - 1, 0, -1):
        try:
            module = importlib.import_module(".".join(parts[:split]))
        except ImportError:
            continue
        target: object = module
        try:
            for attr in parts[split:]:
                target = getattr(target, attr)
        except AttributeError:
            return False
        return True
    return False


def _case_ids() -> set[str]:
    payload = cast("dict[str, object]", json.loads(_CASES_PATH.read_text(encoding="utf-8")))
    cases = cast("list[dict[str, object]]", payload["cases"])
    return {cast("str", case["id"]) for case in cases}


def test_class_ids_are_unique_snake_case() -> None:
    ids = [entry.class_id for entry in GUARDED]
    ids += [entry.class_id for entry in UNGUARDED]
    assert [class_id for class_id in ids if _SNAKE_CASE.fullmatch(class_id) is None] == []
    assert len(ids) == len(set(ids))


def test_every_fact_key_is_an_enum_member() -> None:
    members = set(FactKey)
    assert [key for entry in GUARDED for key in entry.fact_keys if key not in members] == []


def test_every_guard_dotted_name_resolves() -> None:
    broken = [name for entry in GUARDED for name in entry.guards if not _guard_resolves(name)]
    assert broken == []


def test_every_case_id_exists_in_jurisdiction_cases() -> None:
    known = _case_ids()
    named = {
        fixture for entry in GUARDED for fixture in entry.fixtures if _CASE_ID.fullmatch(fixture)
    }
    for entry in UNGUARDED:
        named.update(_J_ID.findall(entry.where_named))
        named.update(_J_ID.findall(entry.planned_unit))
    assert sorted(named - known) == []


def test_every_test_node_id_names_a_test_def() -> None:
    broken: list[str] = []
    for entry in GUARDED:
        for fixture in entry.fixtures:
            if "::" not in fixture:
                continue
            rel_path, _, name_part = fixture.partition("::")
            name = name_part.split("[", 1)[0]
            path = BACKEND_ROOT / rel_path
            if not path.is_file():
                broken.append(fixture)
                continue
            text = path.read_text(encoding="utf-8")
            if re.search(rf"\bdef {re.escape(name)}\(", text) is None:
                broken.append(fixture)
    assert broken == []


def test_every_guarded_class_has_a_fixture() -> None:
    assert [entry.class_id for entry in GUARDED if not entry.fixtures] == []


def test_no_class_id_is_guarded_and_unguarded() -> None:
    guarded = {entry.class_id for entry in GUARDED}
    unguarded = {entry.class_id for entry in UNGUARDED}
    assert guarded & unguarded == set()


def test_by_id_finds_guarded_and_unguarded_rows() -> None:
    assert by_id("payment_address_as_contest") is GUARDED[0]
    assert by_id("payment_forfeits_review") is UNGUARDED[0]
    with pytest.raises(KeyError):
        by_id("not_a_trap_class")
