from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from mail_samples import make_request
from parkingbreaker.lob_fakes import FakeLobClient
from parkingbreaker.lob_submission import (
    LOCAL_REFUSAL_REASON,
    Accepted,
    LetterEvidence,
    LobClient,
    LobResponse,
    LookupByMetadata,
    MetadataLookup,
    NotSent,
    Rejected,
    RetryPolicy,
    RetrySameKey,
    SubmissionOutcome,
    Uncertain,
    WebhookAction,
    apply_event,
    backoff,
    classify_response,
    hold_releasable,
    idempotency_key,
    next_action,
    reconcile_with_original,
    resolve_lookup,
)

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
WINDOW = timedelta(hours=24)
POLICY = RetryPolicy(
    base_delay=timedelta(minutes=1),
    max_delay=timedelta(hours=2),
    lookup_margin=timedelta(hours=1),
)
REQUEST = make_request(b"%PDF-frozen", "tok-abc")


def submit(client: LobClient, case_token: str, attempt: int) -> tuple[SubmissionOutcome, int]:
    """Test-only driver: the loop the I/O layer runs, with a simulated clock."""
    key = idempotency_key(case_token, attempt)
    now = T0
    sent = 0
    while True:
        sent += 1
        outcome = classify_response(client.create_letter(REQUEST, key))
        if not isinstance(outcome, Uncertain):
            return outcome, sent
        action = next_action(T0, now, sent, WINDOW, POLICY)
        if isinstance(action, LookupByMetadata):
            found = client.find_by_metadata(REQUEST.submission_token)
            return resolve_lookup(found), sent
        now = action.at


# --- keys --------------------------------------------------------------------------------


def test_key_is_deterministic_and_attempt_specific() -> None:
    assert idempotency_key("case-1", 1) == idempotency_key("case-1", 1)
    assert idempotency_key("case-1", 1) != idempotency_key("case-1", 2)
    assert idempotency_key("case-1", 1) != idempotency_key("case-2", 1)
    assert "case-1" not in idempotency_key("case-1", 1)


@pytest.mark.parametrize(("token", "attempt"), [("", 1), ("c", 0), ("c", -1)])
def test_key_rejects_bad_input(token: str, attempt: int) -> None:
    with pytest.raises(ValueError, match=r"case_token|attempt"):
        idempotency_key(token, attempt)


@given(
    case_token=st.text(min_size=1, max_size=20),
    attempt=st.integers(min_value=1, max_value=2),
    uncertain=st.lists(
        st.one_of(st.none(), st.sampled_from([500, 502, 503, 504, 408, 409, 429])).map(
            lambda s: LobResponse(s, error="flaky")
        ),
        max_size=40,
    ),
)
def test_key_never_changes_across_retries_of_one_attempt(
    case_token: str, attempt: int, uncertain: list[LobResponse]
) -> None:
    client = FakeLobClient(script=list(uncertain))
    submit(client, case_token, attempt)
    keys = {key for key, _ in client.calls}
    assert keys == {idempotency_key(case_token, attempt)}
    assert {request for _, request in client.calls} == {REQUEST}


# --- classification ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (LobResponse(200, "ltr_1", evidence=LetterEvidence.PRODUCTION), Accepted("ltr_1")),
        (LobResponse(422, error="bad address"), Rejected(422, "bad address")),
        (LobResponse(400), Rejected(400)),
        (LobResponse(None, error="timeout"), Uncertain("timeout")),
        (LobResponse(None), Uncertain("no response")),
        (LobResponse(503), Uncertain("http 503")),
        (LobResponse(429), Uncertain("http 429")),
        (LobResponse(409), Uncertain("http 409")),
        (LobResponse(200), Uncertain("200 without letter id")),
    ],
)
def test_classify_response(response: LobResponse, expected: SubmissionOutcome) -> None:
    assert classify_response(response) == expected


def test_resolve_lookup() -> None:
    assert resolve_lookup(MetadataLookup("ltr_9", evidence=LetterEvidence.PRODUCTION)) == Accepted(
        "ltr_9"
    )
    assert isinstance(resolve_lookup(None), Uncertain)
    assert isinstance(resolve_lookup(""), Uncertain)
    assert resolve_lookup(MetadataLookup(authoritative=True)) == NotSent()


def test_hold_releasable_only_for_rejected_and_not_sent() -> None:
    assert hold_releasable(Rejected(422))
    assert hold_releasable(NotSent())
    assert not hold_releasable(Accepted("ltr_1"))
    assert not hold_releasable(Uncertain("timeout"))


@given(
    status=st.one_of(st.none(), st.integers(min_value=100, max_value=599)),
    letter_id=st.one_of(st.none(), st.text(max_size=8)),
)
def test_release_never_allowed_while_uncertain(status: int | None, letter_id: str | None) -> None:
    outcome = classify_response(LobResponse(status, letter_id, evidence=LetterEvidence.PRODUCTION))
    if isinstance(outcome, Uncertain | Accepted):
        assert not hold_releasable(outcome)
    else:
        assert status is not None
        assert 400 <= status < 500


# --- schedule ------------------------------------------------------------------------------


def test_backoff_doubles_and_caps() -> None:
    assert [backoff(n, POLICY) for n in (1, 2, 3)] == [
        timedelta(minutes=1),
        timedelta(minutes=2),
        timedelta(minutes=4),
    ]
    assert backoff(50, POLICY) == timedelta(hours=2)
    with pytest.raises(ValueError, match="attempts"):
        backoff(0, POLICY)


def test_next_action_retries_early_and_looks_up_near_window_end() -> None:
    assert next_action(T0, T0, 1, WINDOW, POLICY) == RetrySameKey(T0 + timedelta(minutes=1))
    late = T0 + WINDOW - timedelta(minutes=61)
    assert next_action(T0, late, 1, WINDOW, POLICY) == LookupByMetadata()
    assert next_action(T0, T0 + timedelta(hours=22), 10, WINDOW, POLICY) == LookupByMetadata()


def test_next_action_validates_input() -> None:
    with pytest.raises(ValueError, match="before first_sent_at"):
        next_action(T0, T0 - timedelta(seconds=1), 1, WINDOW, POLICY)
    with pytest.raises(ValueError, match="lookup_margin"):
        next_action(T0, T0, 1, timedelta(minutes=30), POLICY)
    with pytest.raises(ValueError, match="base_delay"):
        RetryPolicy(base_delay=timedelta(0))


@given(
    elapsed=st.integers(min_value=0, max_value=24 * 3600),
    attempts=st.integers(min_value=1, max_value=60),
)
def test_retry_is_always_before_lookup_point(elapsed: int, attempts: int) -> None:
    now = T0 + timedelta(seconds=elapsed)
    action = next_action(T0, now, attempts, WINDOW, POLICY)
    if isinstance(action, RetrySameKey):
        assert now < action.at < T0 + WINDOW - POLICY.lookup_margin


# --- end to end with the fake --------------------------------------------------------------


def test_accepted_first_time() -> None:
    client = FakeLobClient(script=[LobResponse(200, "ltr_1", evidence=LetterEvidence.PRODUCTION)])
    assert submit(client, "case-1", 1) == (Accepted("ltr_1"), 1)


def test_rejected_releases() -> None:
    client = FakeLobClient(script=[LobResponse(422, error="address")])
    outcome, _ = submit(client, "case-1", 1)
    assert outcome == Rejected(422, "address")
    assert hold_releasable(outcome)


def test_retry_after_lost_response_returns_same_letter() -> None:
    client = FakeLobClient(script=[LobResponse(200, "ltr_1", evidence=LetterEvidence.PRODUCTION)])
    key = idempotency_key("case-1", 1)
    client.create_letter(REQUEST, key)  # response lost in transit
    assert submit(client, "case-1", 1) == (Accepted("ltr_1"), 1)


def test_lookup_finds_letter_created_behind_timeouts() -> None:
    client = FakeLobClient(letters={"tok-abc": "ltr_7"})  # empty script: always timeout
    outcome, sent = submit(client, "case-1", 1)
    assert outcome == Accepted("ltr_7")
    assert sent > 1
    assert client.lookups == ["tok-abc"]


def test_lookup_finds_nothing_so_not_sent() -> None:
    client = FakeLobClient()
    outcome, _ = submit(client, "case-1", 1)
    assert outcome == NotSent()
    assert hold_releasable(outcome)


# --- webhook gate ---------------------------------------------------------------------------


def test_webhook_gate_forward_only_and_dedup() -> None:
    first = apply_event(1, 2, frozenset(), "evt_1")
    assert first.action is WebhookAction.APPLY
    assert first.seen_event_ids == {"evt_1"}
    again = apply_event(2, 2, first.seen_event_ids, "evt_1")
    assert again.action is WebhookAction.DUPLICATE
    assert again.seen_event_ids == {"evt_1"}
    older = apply_event(3, 1, first.seen_event_ids, "evt_0")
    assert older.action is WebhookAction.RECORD_ONLY
    assert older.seen_event_ids == {"evt_0", "evt_1"}
    with pytest.raises(ValueError, match="event_id"):
        apply_event(1, 2, frozenset(), "")


@given(
    events=st.lists(
        st.tuples(st.integers(min_value=0, max_value=5), st.sampled_from("abcdefgh")),
        max_size=30,
    )
)
def test_webhook_rank_never_decreases(events: list[tuple[int, str]]) -> None:
    rank, seen = 0, frozenset[str]()
    for event_rank, event_id in events:
        decision = apply_event(rank, event_rank, seen, event_id)
        if decision.action is WebhookAction.APPLY:
            assert event_rank > rank
            rank = event_rank
        seen = decision.seen_event_ids
        assert event_id in seen


@pytest.mark.parametrize("evidence", [LetterEvidence.CREATED, LetterEvidence.RENDERED])
def test_created_or_rendered_letter_is_recorded_but_never_accepted(
    evidence: LetterEvidence,
) -> None:
    outcome = classify_response(
        LobResponse(200, "ltr_1", tracking_number="9407" * 5, evidence=evidence)
    )
    assert outcome == Uncertain(
        f"letter {evidence.value}; production evidence not established",
        "ltr_1",
        evidence is LetterEvidence.RENDERED,
        "9407" * 5,
    )
    assert not hold_releasable(outcome)


def test_local_refusal_is_a_definite_rejection() -> None:
    outcome = classify_response(LobResponse(None, error="local", local_refusal=True))
    assert outcome == Rejected(422, LOCAL_REFUSAL_REASON)
    assert hold_releasable(outcome)


def test_lookup_adopts_only_the_recorded_original_and_never_rendered_evidence() -> None:
    rendered = MetadataLookup("ltr_1", evidence=LetterEvidence.RENDERED)
    assert resolve_lookup(rendered, "ltr_1") == Uncertain(
        "lookup found letter rendered; no production evidence", "ltr_1", True
    )
    unanchored = resolve_lookup(rendered)
    assert isinstance(unanchored, Uncertain)
    assert unanchored.letter_id is None
    assert not unanchored.rendered
    other = resolve_lookup(MetadataLookup("ltr_2"), "ltr_1")
    assert other == Uncertain("lookup letter differs from the original create response", "ltr_1")
    proven = MetadataLookup("ltr_1", evidence=LetterEvidence.PRODUCTION)
    assert resolve_lookup(proven, "ltr_1") == Accepted("ltr_1")


def test_reconcile_keeps_first_original_and_refuses_other_letters_or_absence() -> None:
    first = Uncertain("created", "ltr_1", True, "track")
    assert reconcile_with_original(None, NotSent()) == NotSent()
    assert reconcile_with_original(Uncertain("x"), Accepted("ltr_9")) == Accepted("ltr_9")
    assert reconcile_with_original(first, Uncertain("503")) == Uncertain(
        "503", "ltr_1", True, "track"
    )
    assert reconcile_with_original(first, Uncertain("c", "ltr_2")) == Uncertain(
        "letter differs from the original create response", "ltr_1", True, "track"
    )
    for conflicting in (NotSent(), Rejected(422, "r"), Accepted("ltr_2")):
        assert isinstance(reconcile_with_original(first, conflicting), Uncertain)
    assert reconcile_with_original(first, Accepted("ltr_1")) == Accepted("ltr_1")


def test_evidence_defaults_fail_closed_so_a_forgotten_field_cannot_capture() -> None:
    outcome = classify_response(LobResponse(200, "ltr_1"))
    assert isinstance(outcome, Uncertain)
    assert not isinstance(outcome, Accepted)
    assert outcome.letter_id == "ltr_1"
    assert LobResponse(200, "ltr_1").evidence is LetterEvidence.CREATED
    looked_up = resolve_lookup(MetadataLookup("ltr_1"), "ltr_1")
    assert isinstance(looked_up, Uncertain)
    assert MetadataLookup("ltr_1").evidence is LetterEvidence.CREATED
