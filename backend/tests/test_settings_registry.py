"""T49: learned settings move one rung at a time; risk and spend wait for the owner."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from parkingbreaker.policy_record import current_policy
from parkingbreaker.review_queue import OWNER, Resolution, ReviewKind
from parkingbreaker.settings_registry import (
    OWNER_REUSE_HOURS,
    SETTINGS,
    Current,
    Safer,
    Verdict,
    decide,
    outside_fixed_rules,
)
from parkingbreaker.settings_store import AUTOMATIC
from parkingbreaker.transaction_store import SqliteTransactionStore

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
WEEK = timedelta(days=7)


def test_every_setting_has_a_ladder_a_start_and_a_safe_direction() -> None:
    expected = {
        "research_reuse_hours", "postal_buffer_days", "photo_reading_confidence_pct",
        "ad_daily_cap_cents", "ad_click_minimum", "ad_cooldown_hours", "ad_stop_loss_cents",
        "ai_cost_limit_cents", "price_test_min_orders",
    }  # fmt: skip
    assert set(SETTINGS) == expected
    for setting in SETTINGS.values():
        assert setting.start in setting.ladder
        assert setting.min_hold > timedelta(0)
    # Spend and ad guards start at their safest rung: nothing loosens without approval.
    for name in ("ad_daily_cap_cents", "ad_stop_loss_cents", "ai_cost_limit_cents"):
        assert SETTINGS[name].start == min(SETTINGS[name].ladder)
    for name in ("ad_click_minimum", "ad_cooldown_hours"):
        assert SETTINGS[name].start == max(SETTINGS[name].ladder)


def test_planted_two_step_jump_and_off_ladder_values_are_refused() -> None:
    reuse = SETTINGS["research_reuse_hours"]
    at_24 = Current(24, None)
    assert decide(reuse, at_24, 8, T0).verdict is Verdict.REFUSED  # 24 -> 8 skips 12
    assert decide(reuse, at_24, 12, T0).verdict is Verdict.APPLY
    assert decide(reuse, at_24, 6, T0).verdict is Verdict.REFUSED
    cap = SETTINGS["ad_daily_cap_cents"]
    assert decide(cap, Current(0, None), 1000, T0).verdict is Verdict.REFUSED


def test_fixed_owner_rules_sit_outside_and_reuse_can_only_shorten() -> None:
    assert outside_fixed_rules(current_policy())
    reuse = SETTINGS["research_reuse_hours"]
    assert max(reuse.ladder) == OWNER_REUSE_HOURS
    assert reuse.safer is Safer.LOWER
    assert decide(reuse, Current(24, None), 48, T0).verdict is Verdict.REFUSED
    # Back up toward 24 hours is a step toward risk: the owner must approve it.
    assert decide(reuse, Current(12, T0 - WEEK), 24, T0).verdict is Verdict.NEEDS_OWNER


def test_safe_step_applies_at_once_records_everything_and_respects_the_hold(
    tmp_path: Path,
) -> None:
    with SqliteTransactionStore(tmp_path / "s.sqlite") as store:
        decision = store.propose_setting(
            "research_reuse_hours",
            12,
            proposer="research-worker",
            evidence="stale pages",
            now=T0,
            expires_at=T0 + WEEK * 2,
        )
        assert decision.verdict is Verdict.APPLY
        assert store.current_setting("research_reuse_hours") == Current(12, T0)
        row = (
            store._require_open()
            .execute(
                "SELECT name, old_value, new_value, proposer, approver, evidence "
                "FROM pb_setting_changes"
            )
            .fetchone()
        )
        assert row == ("research_reuse_hours", 24, 12, "research-worker", AUTOMATIC, "stale pages")
        # Tightening never waits; loosening waits out the hold before it can even be asked.
        again = store.propose_setting(
            "research_reuse_hours",
            8,
            proposer="research-worker",
            evidence="again",
            now=T0,
            expires_at=T0 + WEEK * 2,
        )
        assert again.verdict is Verdict.APPLY
        too_soon = store.propose_setting(
            "research_reuse_hours",
            12,
            proposer="research-worker",
            evidence="back",
            now=T0,
            expires_at=T0 + WEEK * 2,
        )
        assert too_soon.verdict is Verdict.REFUSED
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            store._require_open().execute("DELETE FROM pb_setting_changes")
        with pytest.raises(ValueError, match="evidence"):
            store.propose_setting("research_reuse_hours", 8, proposer="x", evidence=" ", now=T0)


def test_planted_unapproved_risky_move_changes_nothing_until_the_owner_approves(
    tmp_path: Path,
) -> None:
    with SqliteTransactionStore(tmp_path / "s.sqlite") as store:
        decision = store.propose_setting(
            "ad_daily_cap_cents",
            500,
            proposer="ads-worker",
            evidence="CPA under target",
            now=T0,
            expires_at=T0 + WEEK * 2,
        )
        assert decision.verdict is Verdict.NEEDS_OWNER
        assert store.current_setting("ad_daily_cap_cents").value == 0
        (item,) = [i for i in store.open_review_items() if i.kind is ReviewKind.SETTING_CHANGE]
        (proposal,) = store.open_setting_proposals()
        assert item.item_id == f"setting:{proposal.proposal_id}"
        # A second proposal cannot stack on the pending one.
        stacked = store.propose_setting(
            "ad_daily_cap_cents",
            500,
            proposer="ads-worker",
            evidence="again",
            now=T0,
            expires_at=T0 + WEEK * 2,
        )
        assert stacked.verdict is Verdict.REFUSED
        with pytest.raises(PermissionError):
            store.decide_setting_proposal(
                proposal.proposal_id, approve=True, actor="ads-worker", now=T0
            )
        with pytest.raises(ValueError, match="decide_setting_proposal"):
            store.resolve_review_item(item.item_id, Resolution.ACKNOWLEDGED, OWNER, T0)
        assert store.current_setting("ad_daily_cap_cents").value == 0
        outcome = store.decide_setting_proposal(
            proposal.proposal_id, approve=True, actor=OWNER, now=T0 + timedelta(hours=1)
        )
        assert outcome == "APPROVED"
        assert store.current_setting("ad_daily_cap_cents").value == 500
        assert store.open_setting_proposals() == ()
        assert not [i for i in store.open_review_items() if i.kind is ReviewKind.SETTING_CHANGE]
        row = (
            store._require_open()
            .execute("SELECT proposer, approver FROM pb_setting_changes")
            .fetchone()
        )
        assert row == ("ads-worker", OWNER)


def test_an_approval_after_the_setting_moved_is_stale(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "s.sqlite") as store:
        risky = store.propose_setting(
            "photo_reading_confidence_pct",
            90,
            proposer="photo-worker",
            evidence="few",
            now=T0,
            expires_at=T0 + WEEK * 2,
        )
        assert risky.verdict is Verdict.NEEDS_OWNER
        safe = store.propose_setting(
            "photo_reading_confidence_pct",
            99,
            proposer="photo-worker",
            evidence="misreads",
            now=T0,
            expires_at=T0 + WEEK * 2,
        )
        assert safe.verdict is Verdict.APPLY
        (proposal,) = store.open_setting_proposals()
        later = T0 + WEEK + timedelta(hours=1)
        assert (
            store.decide_setting_proposal(
                proposal.proposal_id, approve=True, actor=OWNER, now=later
            )
            == "STALE"
        )
        assert store.current_setting("photo_reading_confidence_pct").value == 99


@pytest.mark.parametrize("approve", ["false", "true", 1, 0, None])
def test_malformed_approval_starts_no_transaction(tmp_path: Path, approve: object) -> None:
    with SqliteTransactionStore(tmp_path / "s.sqlite") as store:
        store.propose_setting(
            "ad_daily_cap_cents",
            500,
            proposer="ads-worker",
            evidence="measured",
            now=T0,
            expires_at=T0 + WEEK * 2,
        )
        (proposal,) = store.open_setting_proposals()
        database = store._require_open()
        statements: list[str] = []
        database.set_trace_callback(statements.append)
        with pytest.raises(ValueError, match="bool"):
            store.decide_setting_proposal(
                proposal.proposal_id, approve=cast(bool, approve), actor=OWNER, now=T0
            )
        database.set_trace_callback(None)
        assert not statements
        assert store.current_setting(proposal.name) == Current(0, None)
        assert store.open_setting_proposals() == (proposal,)
        assert database.execute("SELECT * FROM pb_setting_changes").fetchall() == []
        assert database.execute("SELECT * FROM pb_setting_proposal_outcomes").fetchall() == []


def test_backdated_safe_step_preserves_history_and_hold(tmp_path: Path) -> None:
    reuse = SETTINGS["research_reuse_hours"]
    with pytest.raises(ValueError, match="timezone-aware"):
        decide(reuse, Current(12, T0), 8, T0.replace(tzinfo=None))
    with pytest.raises(ValueError, match="timezone-aware"):
        decide(reuse, Current(12, T0.replace(tzinfo=None)), 8, T0)
    with SqliteTransactionStore(tmp_path / "s.sqlite") as store:
        store.propose_setting(reuse.name, 12, proposer="research-worker", evidence="stale", now=T0)
        database = store._require_open()
        history = database.execute("SELECT * FROM pb_setting_changes").fetchall()
        assert decide(reuse, Current(12, T0), 8, T0 - WEEK).verdict is Verdict.REFUSED
        result = store.propose_setting(
            reuse.name,
            8,
            proposer="research-worker",
            evidence="older",
            now=T0 - WEEK,
            expires_at=T0 + WEEK * 2,
        )
        assert result.verdict is Verdict.REFUSED
        assert store.current_setting(reuse.name) == Current(12, T0)
        assert database.execute("SELECT * FROM pb_setting_changes").fetchall() == history
        assert store.open_setting_proposals() == ()
        before_hold = T0 + WEEK - timedelta(seconds=1)
        assert decide(reuse, Current(12, T0), 24, before_hold).verdict is Verdict.REFUSED
        assert decide(reuse, Current(12, T0), 24, T0 + WEEK).verdict is Verdict.NEEDS_OWNER
        assert decide(reuse, Current(12, T0), 8, T0).verdict is Verdict.APPLY


@pytest.mark.parametrize("approve", [True, False])
def test_preproposal_decision_preserves_pending_proposal(tmp_path: Path, approve: bool) -> None:
    with SqliteTransactionStore(tmp_path / "s.sqlite") as store:
        store.propose_setting(
            "ad_daily_cap_cents",
            500,
            proposer="ads-worker",
            evidence="measured",
            now=T0,
            expires_at=T0 + WEEK * 2,
        )
        (proposal,) = store.open_setting_proposals()
        with pytest.raises(ValueError, match="precedes the proposal"):
            store.decide_setting_proposal(
                proposal.proposal_id, approve=approve, actor=OWNER, now=T0 - timedelta(seconds=1)
            )
        database = store._require_open()
        assert not database.in_transaction
        assert store.current_setting(proposal.name) == Current(0, None)
        assert store.open_setting_proposals() == (proposal,)
        assert database.execute("SELECT * FROM pb_setting_changes").fetchall() == []
        assert database.execute("SELECT * FROM pb_setting_proposal_outcomes").fetchall() == []
        assert store.decide_setting_proposal(
            proposal.proposal_id, approve=approve, actor=OWNER, now=T0
        ) == ("APPROVED" if approve else "REJECTED")


def test_same_time_approval_survives_reopen_with_append_only_history(tmp_path: Path) -> None:
    path = tmp_path / "s.sqlite"
    with SqliteTransactionStore(path) as store:
        store.propose_setting(
            "ad_daily_cap_cents",
            500,
            proposer="ads-worker",
            evidence="measured",
            now=T0,
            expires_at=T0 + WEEK * 2,
        )
        (proposal,) = store.open_setting_proposals()
        assert (
            store.decide_setting_proposal(proposal.proposal_id, approve=True, actor=OWNER, now=T0)
            == "APPROVED"
        )
        changes = store._require_open().execute("SELECT * FROM pb_setting_changes").fetchall()
        outcomes = (
            store._require_open().execute("SELECT * FROM pb_setting_proposal_outcomes").fetchall()
        )
    with SqliteTransactionStore(path) as store:
        database = store._require_open()
        assert store.current_setting("ad_daily_cap_cents") == Current(500, T0)
        assert store.open_setting_proposals() == ()
        assert len(changes) == len(outcomes) == 1
        assert database.execute("SELECT * FROM pb_setting_changes").fetchall() == changes
        assert database.execute("SELECT * FROM pb_setting_proposal_outcomes").fetchall() == outcomes
        for table in ("pb_setting_changes", "pb_setting_proposals", "pb_setting_proposal_outcomes"):
            with pytest.raises(sqlite3.DatabaseError, match="append-only"):
                database.execute(f"DELETE FROM {table}")
