"""Immutable workspace, case lifecycle, history and outbox records.

This module contains only value records and a strict workspace JSON codec. It does not
interpret customer text or perform storage, clock, or provider operations.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime

from parkingbreaker.case_state import CaseState, Effect, Event, Stopped
from parkingbreaker.case_status import CaseStatus, case_status
from parkingbreaker.payment_state import PaymentEffect, PaymentEvent, PaymentState
from parkingbreaker.workspace import Workspace


@dataclass(frozen=True)
class WorkspaceRecord:
    workspace_token: str
    workspace: Workspace
    updated_at: datetime
    revision: int


@dataclass(frozen=True)
class CaseRecord:
    case_token: str
    workspace_token: str
    authorization_ref: str
    authorized_at: datetime
    authorization_expires_at: datetime
    amount_cents: int
    letter_state: CaseState
    payment_state: PaymentState
    attempt: int
    revision: int

    @property
    def case_status(self) -> CaseStatus:
        return case_status(self.letter_state, self.payment_state)


@dataclass(frozen=True)
class TransitionRecord:
    case_token: str
    revision: int
    machine: str
    source: str
    target: str
    event_name: str
    event_json: str
    occurred_at: datetime


@dataclass(frozen=True)
class PendingEffect:
    effect_id: str
    case_token: str
    revision: int
    machine: str
    effect: Effect | PaymentEffect
    created_at: datetime
    claimed_at: datetime | None = None
    outcome: str | None = None
    completed_at: datetime | None = None


_WORKSPACE_KEYS = {
    "answers",
    "letter_edits",
    "notes",
    "photo_refs",
    "position",
    "provision_selections",
}


def _workspace_payload(workspace: Workspace) -> dict[str, object]:
    if not isinstance(workspace, Workspace):
        raise ValueError("workspace must be a Workspace")
    if not isinstance(workspace.notes, str) or not isinstance(workspace.position, str):
        raise ValueError("workspace notes and position must be strings")

    for name, value in (
        ("photo_refs", workspace.photo_refs),
        ("letter_edits", workspace.letter_edits),
    ):
        if not isinstance(value, (tuple, list)) or any(not isinstance(item, str) for item in value):
            raise ValueError(f"workspace {name} must contain only strings")

    provisions = workspace.provision_selections
    if not isinstance(provisions, (set, frozenset, tuple, list)) or any(
        not isinstance(item, str) for item in provisions
    ):
        raise ValueError("workspace provision_selections must contain only strings")
    if len(set(provisions)) != len(provisions):
        raise ValueError("workspace provision_selections must be unique")

    answers = workspace.answers
    if not isinstance(answers, Mapping):
        raise ValueError("workspace answers must be a string-to-string mapping")
    answer_items = list(answers.items())
    if any(not isinstance(key, str) or not isinstance(value, str) for key, value in answer_items):
        raise ValueError("workspace answers must be a string-to-string mapping")

    return {
        "answers": dict(answer_items),
        "letter_edits": list(workspace.letter_edits),
        "notes": workspace.notes,
        "photo_refs": list(workspace.photo_refs),
        "position": workspace.position,
        "provision_selections": sorted(provisions),
    }


def workspace_json(workspace: Workspace) -> str:
    """Encode the six workspace fields as canonical compact sorted-key JSON."""
    try:
        return json.dumps(
            _workspace_payload(workspace),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid workspace: {exc}") from exc


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-JSON constant: {value}")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def decode_workspace(text: str) -> Workspace:
    """Decode a workspace, rejecting duplicate, unknown, or malformed fields."""
    if not isinstance(text, str):
        raise ValueError("workspace JSON must be a string")
    try:
        payload = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
        if not isinstance(payload, dict) or set(payload) != _WORKSPACE_KEYS:
            raise ValueError("workspace JSON must contain exactly the six workspace fields")
        notes = payload["notes"]
        position = payload["position"]
        photo_refs = payload["photo_refs"]
        letter_edits = payload["letter_edits"]
        provisions = payload["provision_selections"]
        answers = payload["answers"]
        if not isinstance(notes, str) or not isinstance(position, str):
            raise ValueError("workspace notes and position must be strings")
        if not isinstance(photo_refs, list) or any(
            not isinstance(item, str) for item in photo_refs
        ):
            raise ValueError("workspace photo_refs must be a list of strings")
        if not isinstance(letter_edits, list) or any(
            not isinstance(item, str) for item in letter_edits
        ):
            raise ValueError("workspace letter_edits must be a list of strings")
        if (
            not isinstance(provisions, list)
            or any(not isinstance(item, str) for item in provisions)
            or len(set(provisions)) != len(provisions)
        ):
            raise ValueError("workspace provision_selections must be a unique list of strings")
        if not isinstance(answers, dict) or any(
            not isinstance(key, str) or not isinstance(value, str) for key, value in answers.items()
        ):
            raise ValueError("workspace answers must be a string-to-string object")
        return Workspace(
            notes=notes,
            position=position,
            photo_refs=tuple(photo_refs),
            provision_selections=frozenset(provisions),
            letter_edits=tuple(letter_edits),
            answers=answers,
        )
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid workspace JSON: {exc}") from exc


def lifecycle_event_json(event: Event | PaymentEvent) -> str:
    """Canonical event payload; additive stop field leaves historical bytes unchanged."""
    payload = asdict(event)
    if isinstance(event, Stopped) and event.hard_stop is None:
        del payload["hard_stop"]
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))
