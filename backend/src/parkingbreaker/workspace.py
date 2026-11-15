"""Customer workspace (architecture v6, section 5.1). Pure data and functions, no storage.

Everything the customer contributes lives here, separate from any one transaction, so a
stop of any kind leaves it intact. A restart is a new transaction prefilled from the
workspace. Research always re-runs, so provision selections carry over only for
provisions the new research finds again; everything else the customer wrote is kept.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace


@dataclass(frozen=True)
class Workspace:
    notes: str = ""
    position: str = ""
    photo_refs: tuple[str, ...] = ()
    provision_selections: frozenset[str] = frozenset()
    letter_edits: tuple[str, ...] = ()
    answers: Mapping[str, str] = field(default_factory=dict)


def restart_prefill(workspace: Workspace, new_research_provision_ids: Iterable[str]) -> Workspace:
    """The workspace for a restart: selections kept only for provisions found again."""
    found = frozenset(new_research_provision_ids)
    return replace(workspace, provision_selections=workspace.provision_selections & found)
