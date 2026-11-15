from dataclasses import replace
from datetime import timedelta
from itertools import permutations
from pathlib import Path

import pytest

from parkingbreaker.disputes import DisputeFact, DisputeStatus, dispute_fact, latest_disputes
from parkingbreaker.ledger import LedgerLine, LineKind, LineStatus, appeal_ledger
from parkingbreaker.oplog.appeal_events import AppealEventKind
from test_case_service import anyio_backend as anyio_backend
from test_case_service import fake_resolution as fake_resolution
from test_disputes_ledger import CREATED, _dispute, _events


def _fact(status: str = "won") -> DisputeFact:
    return dispute_fact("charge.dispute.closed", _dispute(status), CREATED)


@pytest.mark.parametrize("variant", ["status", "amount", "payment", "category"])
def test_same_rank_contradictions_are_unresolved_in_every_order(variant: str) -> None:
    original = _fact()
    conflicting = {
        "status": replace(original, status=DisputeStatus.LOST),
        "amount": replace(original, amount_cents=1000),
        "payment": replace(original, payment_intent="pi_Other123"),
        "category": replace(original, category="duplicate"),
    }[variant]
    ledgers = []
    for facts in permutations((original, conflicting)):
        assert latest_disputes(facts) == {original.object_ref: None}
        ledger = appeal_ledger(2999, _events(AppealEventKind.CHARGED), None, facts)
        assert LedgerLine(LineKind.DISPUTE_OUTCOME, LineStatus.PENDING, None) in ledger.lines
        assert LedgerLine(LineKind.DISPUTE_FEE, LineStatus.PENDING, None) in ledger.lines
        assert ledger.known_net_cents == 2999
        assert ledger.complete is False
        ledgers.append(ledger)
    assert ledgers[0] == ledgers[1]


def test_equivalent_duplicates_collapse_deterministically() -> None:
    fact = _fact()
    equivalent = replace(fact, event_type="charge.dispute.updated")
    results = [latest_disputes(facts) for facts in permutations((fact, equivalent, fact))]
    assert all(result == results[0] for result in results)
    assert results[0][fact.object_ref] in (fact, equivalent)


def test_later_event_resolves_conflict_and_older_facts_never_overwrite() -> None:
    won = _fact()
    lost = _fact("lost")
    later = replace(lost, event_created=CREATED + 1)
    for facts in permutations((won, lost, later)):
        assert latest_disputes(facts) == {later.object_ref: later}


def test_final_over_open_tie_and_legacy_time_ranking_are_preserved() -> None:
    final = _fact()
    opened = replace(final, status=DisputeStatus.UNDER_REVIEW)
    legacy = replace(final, status=DisputeStatus.LOST, event_created=None)
    for facts in permutations((final, opened, legacy)):
        assert latest_disputes(facts) == {final.object_ref: final}
    legacy_conflict = replace(legacy, status=DisputeStatus.WON)
    for facts in permutations((legacy, legacy_conflict)):
        assert latest_disputes(facts) == {final.object_ref: None}


@pytest.mark.parametrize("price", [1999, 2499, 2999])
@pytest.mark.parametrize("status", ["needs_response", "lost", "won"])
def test_recorded_partial_dispute_refund_arithmetic_and_unknown_fees(
    price: int, status: str
) -> None:
    fact = replace(_fact(status), amount_cents=400)
    ledger = appeal_ledger(
        price, _events(AppealEventKind.CHARGED, AppealEventKind.REFUNDED), 200, (fact,)
    )
    assert ledger.known_net_cents == price - 200 - (0 if status == "won" else 400)
    assert ledger.complete is False
    for line in ledger.lines:
        if line.kind in {LineKind.PROCESSOR_FEE, LineKind.DISPUTE_FEE, LineKind.MAIL_COST}:
            assert (line.status, line.amount_cents) == (LineStatus.PENDING, None)


@pytest.mark.anyio
@pytest.mark.usefixtures("fake_resolution")
async def test_ingress_binds_original_payment_and_keeps_other_case_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from parkingbreaker.api.service import ServiceConflict
    from parkingbreaker.lob_fakes import FakeLobClient
    from parkingbreaker.workspace import Workspace
    from test_disputes_ledger import _event, _warning
    from test_effect_dispatch import approved

    api, real, case = await approved(tmp_path, monkeypatch, FakeLobClient())
    object.__setattr__(real._deps, "stripe_webhook_secret", "secret")
    try:
        token = real._intake(case).case_token
        now = real._deps.clock()
        # Seed a second recorded authorization; the accountless HTTP fixture deliberately
        # resumes the same appeal when its existing identity submits again.
        real._store.save_workspace("t39_other_workspace", Workspace(), now)
        other_case = real._store.start_case(
            "t39_other_workspace", "pi_T39Other123", now, now + timedelta(days=7)
        )
        other_token = other_case.case_token
        assert token is not None
        assert other_token is not None
        assert token != other_token
        history = real._store.transition_history(token)
        other_history = real._store.transition_history(other_token)
        for event_id, status in (("evt_t39_won", "won"), ("evt_t39_lost", "lost")):
            payload, headers = _event(event_id, "charge.dispute.closed", _dispute(status))
            assert real.webhook("stripe", payload, headers) is True
            assert real.webhook("stripe", payload, headers) is False
        warning, headers = _event(
            "evt_t39_warning", "radar.early_fraud_warning.created", _warning()
        )
        assert real.webhook("stripe", warning, headers) is True
        foreign, headers = _event(
            "evt_t39_foreign", "charge.dispute.closed", _dispute(payment_intent="pi_Foreign123")
        )
        with pytest.raises(ServiceConflict, match=r"^webhook_case_not_ready$"):
            real.webhook("stripe", foreign, headers)
        facts = real._store.dispute_facts(token)
        assert len(facts) == 3
        assert latest_disputes(facts) == {"dp_Dispute123": None}
        assert real._store.dispute_facts(other_token) == ()
        assert real._store.transition_history(token) == history
        assert real._store.transition_history(other_token) == other_history
    finally:
        await api.aclose()
