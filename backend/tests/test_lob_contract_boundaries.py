"""Observable mail/recovery contracts with no eager provider scenario construction."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

T0 = datetime(2026, 10, 23, 12, tzinfo=UTC)


def test_default_retry_schedule_and_lookup_cutoff() -> None:
    from parkingbreaker import lob_submission as lob

    assert lob.next_action(T0, T0, 1, timedelta(hours=24)) == lob.RetrySameKey(
        T0 + timedelta(seconds=30)
    )
    assert lob.next_action(T0, T0, 9, timedelta(hours=24)) == lob.RetrySameKey(
        T0 + timedelta(minutes=30)
    )
    now = T0 + timedelta(hours=22, minutes=15)
    assert lob.next_action(T0, now, 1, timedelta(hours=24)) == lob.RetrySameKey(
        now + timedelta(seconds=30)
    )
    assert isinstance(
        lob.next_action(T0, T0 + timedelta(hours=23), 1, timedelta(hours=24)), lob.LookupByMetadata
    )


def test_equal_retry_delays_and_zero_lookup_margin_are_supported() -> None:
    from parkingbreaker import lob_submission as lob

    policy = lob.RetryPolicy(timedelta(minutes=1), timedelta(minutes=1), timedelta(0))
    assert lob.next_action(T0, T0, 1, timedelta(hours=1), policy) == lob.RetrySameKey(
        T0 + timedelta(minutes=1)
    )
    assert lob.backoff(100, policy) == timedelta(minutes=1)


def test_lookup_margin_must_be_strictly_shorter_than_window() -> None:
    from parkingbreaker import lob_submission as lob

    policy = lob.RetryPolicy(lookup_margin=timedelta(hours=1))
    with pytest.raises(ValueError, match="lookup_margin must be shorter"):
        lob.next_action(T0, T0, 1, timedelta(hours=1), policy)


def test_backoff_stops_exponential_growth_even_with_large_configured_cap() -> None:
    from parkingbreaker import lob_submission as lob

    policy = lob.RetryPolicy(timedelta(microseconds=1), timedelta(days=1))
    assert lob.backoff(31, policy) == timedelta(microseconds=1_073_741_824)
    assert lob.backoff(32, policy) == lob.backoff(31, policy)


def test_timeout_never_invents_a_rendered_letter() -> None:
    from parkingbreaker import lob_submission as lob

    result = lob.classify_response(lob.LobResponse(None, error="timeout"))
    assert isinstance(result, lob.Uncertain)
    assert result.rendered is False
    assert result.letter_id is None
    assert not lob.hold_releasable(result)


@pytest.mark.parametrize("status", [300, 408, 425, 500])
def test_nondefinitive_http_status_never_accepts_or_releases(status: int) -> None:
    from parkingbreaker import lob_submission as lob

    result = lob.classify_response(
        lob.LobResponse(status, "ltr_Original", evidence=lob.LetterEvidence.PRODUCTION)
    )
    assert isinstance(result, lob.Uncertain)
    assert not lob.hold_releasable(result)


def test_same_original_reconciliation_retains_rendering_and_tracking() -> None:
    from parkingbreaker import lob_submission as lob

    result = lob.reconcile_with_original(
        lob.Uncertain("previous", "ltr_Original", True, "original-tracking"),
        lob.Uncertain("current", "ltr_Original", False, None),
    )
    assert result == lob.Uncertain("current", "ltr_Original", True, "original-tracking")
    assert not lob.hold_releasable(result)


def test_unqualified_or_empty_fallback_keeps_hold_without_crashing() -> None:
    from parkingbreaker import lob_submission as lob

    for found in (None, lob.MetadataLookup()):
        result = lob.fallback_resolution(found, "ltr_Original", T0, T0 + timedelta(days=1))
        assert isinstance(result, lob.Uncertain)
        assert not lob.hold_releasable(result)


def test_send_date_equal_to_now_cannot_authorize_capture() -> None:
    from parkingbreaker import lob_submission as lob

    found = lob.MetadataLookup("ltr_Original", evidence=lob.LetterEvidence.RENDERED, send_date=T0)
    result = lob.fallback_resolution(found, "ltr_Original", T0, T0 + timedelta(days=1))
    assert result == lob.CancelRequest("ltr_Original")


@pytest.mark.parametrize("answer", ["not_cancelable", "unknown"])
def test_cancellation_refusal_does_not_invent_rendering(answer: str) -> None:
    from parkingbreaker import lob_submission as lob

    result = lob.cancel_resolution(lob.CancelOutcome(answer), "ltr_Original", "original-tracking")
    assert result == lob.Uncertain(
        "cancellation outcome not confirmed", "ltr_Original", False, "original-tracking"
    )
    assert not lob.hold_releasable(result)


def test_default_mail_options_reach_the_real_sdk_without_paid_extras() -> None:
    from mail_samples import make_request
    from test_lob_client import _client, _letter, _parse_multipart, _WireServer

    server = _WireServer(lambda *_: (200, _letter()))
    try:
        _client(server).create_letter(make_request(), "original-key")
    finally:
        server.close()
    assert len(server.requests) == 1
    method, path, headers, body = server.requests[0]
    assert (method, path) == ("POST", "/v1/letters")
    parts = _parse_multipart(headers["content-type"], body)
    assert parts["color"] == b"False"
    assert parts["double_sided"] == b"False"
    assert parts["extra_service"] == b"certified"


def test_no_scan_fallback_does_not_cancel_half_hour_before_six_hour_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from parkingbreaker import case_state as cs
    from parkingbreaker import payment_state as ps
    from parkingbreaker.transaction_store import SqliteTransactionStore
    from test_t17_capture_policy import cancel_requests, fallback_world, run_dispatches
    from test_t17_mail_evidence import current_case, resource

    wire, payments, path, case, _ = fallback_world(
        tmp_path,
        monkeypatch,
        resource(send_date=(T0 + timedelta(days=400)).isoformat()),
        "confirmed",
    )
    at = case.authorization_expires_at - timedelta(hours=7)
    run_dispatches(path, wire, payments, at)
    with SqliteTransactionStore(path) as store:
        saved = current_case(store, case)
        assert saved.letter_state is cs.CaseState.SUBMITTING
        assert saved.payment_state is ps.PaymentState.AUTHORIZED
    assert cancel_requests(wire) == []
    assert payments.captures == payments.releases == 0
