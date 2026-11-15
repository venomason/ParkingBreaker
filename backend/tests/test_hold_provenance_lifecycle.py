"""Independent H3 evaluator: a later sticky reason cannot rewrite past provenance."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from parkingbreaker.case_hold_store import CommandOutcome, HoldCommand
from parkingbreaker.lob_submission import LetterEvidence, LobResponse, MetadataLookup, Uncertain
from parkingbreaker.payment_state import PaymentState
from test_beta_admission import NOW
from test_beta_dispatch_timing import ORIGINAL, TOKEN, Beta, LookupFake
from test_t74_mail_hold_boundary import seed_hold


@pytest.mark.parametrize("permanent_reason", ["cutoff", "known_id"])
def test_temporary_lookup_then_retry_then_permanent_lookup_survives_reopen(
    tmp_path: Path, permanent_reason: str
) -> None:
    beta = Beta(tmp_path / "independent-h3.sqlite")
    absent = LookupFake(MetadataLookup(None, authoritative=False))
    try:
        packet = beta.store.get(beta.token, 1)
        assert packet is not None
        # First transport failure creates no provider object, but commits the original intent.
        beta.lob.script.append(LobResponse(None, error="transport unavailable"))
        beta.dispatch(NOW + timedelta(minutes=1))
        first = beta.cursor
        assert first.sends == 1
        assert isinstance(first.outcome, Uncertain)
        assert first.outcome.letter_id is None
        assert beta.lob.letters == {}
        hold = seed_hold(beta.store, beta.token, at=first.next_at)
        beta.dispatch(first.next_at, lob=absent)
        looked = beta.cursor
        assert looked.sends == 1
        assert looked.lookup_only is False
        assert looked.next_at > first.next_at
        assert absent.lookups == [TOKEN]
        assert absent.calls == []
        assert beta.store.get(beta.token, 1) == packet
        assert (
            beta.store.record_hold_command(
                "independent-resume",
                beta.token,
                HoldCommand.RESUME,
                looked.next_at,
                expected_generation=hold.generation,
            )[0]
            is CommandOutcome.APPLIED
        )
        beta.restart()
        assert beta.cursor == looked
        beta.dispatch(looked.next_at - timedelta(seconds=1))
        assert len(beta.lob.calls) == 1
        if permanent_reason == "known_id":
            beta.lob.script.append(LobResponse(200, ORIGINAL, evidence=LetterEvidence.CREATED))
        else:
            beta.lob.script.append(LobResponse(None, error="transport unavailable again"))
        beta.dispatch(looked.next_at)
        retried = beta.cursor
        assert retried.sends == 2
        assert retried.lookup_only is False
        assert retried.first_sent_at == first.first_sent_at
        assert isinstance(retried.outcome, Uncertain)
        assert retried.outcome.letter_id == (ORIGINAL if permanent_reason == "known_id" else None)
        beta.restart()
        assert beta.cursor == retried
        # This real claim makes the cursor sticky AFTER a legal resumed create.
        at = (
            first.first_sent_at + timedelta(hours=23)
            if permanent_reason == "cutoff"
            else retried.next_at
        )
        lookup_client = absent if permanent_reason == "cutoff" else beta.lob
        beta.dispatch(at, lob=lookup_client)
        permanent = beta.cursor
        assert permanent.lookup_only is True
        assert permanent.sends == 2
        assert permanent.first_sent_at == first.first_sent_at
        assert isinstance(permanent.outcome, Uncertain)
        assert permanent.outcome.letter_id == retried.outcome.letter_id
        assert permanent.owner is None
        beta.restart()
        assert beta.cursor == permanent
        beta.dispatch(permanent.next_at, lob=lookup_client)
        final = beta.cursor
        assert final.lookup_only is True
        assert final.sends == 2
        beta.restart()
        assert beta.cursor == final
        operations = (
            beta.store._require_open()
            .execute(
                "SELECT kind, temporary_lookup FROM pb_submission_operations "
                "WHERE case_token=? AND attempt=1 ORDER BY rowid",
                (beta.token,),
            )
            .fetchall()
        )
        assert operations == [
            ("create", 0),
            ("lookup", 1),
            ("create", 0),
            ("lookup", 0),
            ("lookup", 0),
        ]
        assert len(beta.lob.calls) == 2
        assert {key for key, _ in beta.lob.calls} == {packet.key}
        assert all(request == packet.request for _, request in beta.lob.calls)
        assert all(request.submission_token == TOKEN for _, request in beta.lob.calls)
        assert beta.store.get(beta.token, 1) == packet
        assert beta.case.payment_state is PaymentState.NO_CHARGE
        assert beta.store._require_open().execute(
            "SELECT COUNT(*) FROM pb_payment_operations"
        ).fetchone() == (0,)
        assert beta.lob.cancels == absent.cancels == []
        assert absent.calls == []
        assert beta.lob.letters == ({TOKEN: ORIGINAL} if permanent_reason == "known_id" else {})
        assert beta.lob.by_key == ({packet.key: ORIGINAL} if permanent_reason == "known_id" else {})
        if permanent_reason == "known_id":
            assert beta.lob.lookups == [TOKEN, TOKEN]
        else:
            assert absent.lookups == [TOKEN, TOKEN, TOKEN]
    finally:
        beta.store.close()
