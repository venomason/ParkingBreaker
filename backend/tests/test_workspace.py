from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from parkingbreaker.workspace import Workspace, restart_prefill

WS = Workspace(
    notes="Sign was covered by a tree branch.",
    position="The restriction was not posted visibly.",
    photo_refs=("photo-1", "photo-2"),
    provision_selections=frozenset({"prov-a", "prov-b", "prov-c"}),
    letter_edits=("Removed the second paragraph.",),
    answers={"was_vehicle_moved": "yes"},
)


def test_restart_keeps_only_selections_found_again() -> None:
    restarted = restart_prefill(WS, ["prov-b", "prov-c", "prov-new"])
    assert restarted.provision_selections == frozenset({"prov-b", "prov-c"})


def test_restart_keeps_everything_else_the_customer_wrote() -> None:
    restarted = restart_prefill(WS, [])
    assert restarted.provision_selections == frozenset()
    assert restarted.notes == WS.notes
    assert restarted.position == WS.position
    assert restarted.photo_refs == WS.photo_refs
    assert restarted.letter_edits == WS.letter_edits
    assert restarted.answers == WS.answers


def test_restart_leaves_original_workspace_intact() -> None:
    restart_prefill(WS, ["prov-a"])
    assert WS.provision_selections == frozenset({"prov-a", "prov-b", "prov-c"})


def test_empty_workspace_defaults() -> None:
    ws = Workspace()
    assert restart_prefill(ws, ["prov-a"]) == ws


ids = st.frozensets(st.sampled_from(["p1", "p2", "p3", "p4", "p5"]))


@given(ids, ids)
def test_restart_selections_are_the_intersection(
    selected: frozenset[str], found: frozenset[str]
) -> None:
    restarted = restart_prefill(Workspace(provision_selections=selected), found)
    assert restarted.provision_selections == selected & found
    assert restarted.provision_selections <= selected
