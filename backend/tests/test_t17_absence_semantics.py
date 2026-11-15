"""T17 absence semantics: nothing short of authoritative absence is "not sent".

An empty result list, a partial search, an error, a timeout or a bare string is never
proof that the letter was not sent. Every such lookup outcome stays uncertain: the hold
stays, no resend is allowed and capture eligibility is unchanged. Only an explicit
authoritative absence (or a confirmed cancellation) establishes "not sent" and frees the
hold. Whether the provider can ever make a lookup authoritative is unverified — see
docs/t26-provider-questions-t17.md.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from parkingbreaker import payment_state as ps
from parkingbreaker.lob_submission import (
    LetterEvidence,
    LobResponse,
    MetadataLookup,
    NotSent,
    Uncertain,
    classify_response,
    hold_releasable,
    idempotency_key,
    pending_evidence,
    reconcile_with_original,
    resolve_lookup,
)
from parkingbreaker.payment_fakes import FakePaymentClient
from parkingbreaker.submission_coordinator import run_submission_step
from parkingbreaker.transaction_store import SqliteTransactionStore
from test_submission_coordinator import NOW, LookupClient, prepared, step
from test_t17_mail_evidence import (
    ORIGINAL,
    OfflineLob,
    assert_nonaccepted,
    clock_at,
    cursor_for,
    list_response,
    new_id,
    resource,
    submit,
    unavailable,
)


def _broken(**changes: object) -> MetadataLookup:
    """Runtime-invalid lookup shapes the typed constructor refuses to build."""
    found = MetadataLookup()
    for name, value in changes.items():
        object.__setattr__(found, name, value)
    return found


# Every lookup outcome short of explicit authoritative absence. All of these must stay
# uncertain with the original anchor: empty lists, incomplete/partial searches, bare
# strings and objects that only claim absence without establishing it.
SHORT_OF_ABSENCE = [
    pytest.param(None, id="no-result-record"),
    pytest.param("", id="empty-bare-string"),
    pytest.param("   ", id="blank-bare-string"),
    pytest.param(ORIGINAL, id="bare-matching-id-string"),
    pytest.param("ltr_Other", id="bare-other-id-string"),
    pytest.param(MetadataLookup(), id="empty-list-incomplete"),
    pytest.param(MetadataLookup(None, authoritative=False), id="empty-list-not-authoritative"),
    pytest.param(MetadataLookup("", authoritative=True), id="empty-string-id-claims-absence"),
    pytest.param(
        MetadataLookup(ORIGINAL, authoritative=True, evidence=LetterEvidence.CREATED),
        id="created-is-not-absence",
    ),
    pytest.param(
        MetadataLookup(ORIGINAL, authoritative=True, evidence=LetterEvidence.RENDERED),
        id="rendered-is-not-absence",
    ),
    pytest.param(
        MetadataLookup(ORIGINAL, authoritative=True, evidence=LetterEvidence.VOIDED),
        id="voided-needs-cancellation-confirmation",
    ),
    pytest.param(
        MetadataLookup("ltr_Other", authoritative=True, evidence=LetterEvidence.PRODUCTION),
        id="other-letter-than-anchor",
    ),
    pytest.param(_broken(authoritative="yes"), id="incomplete-completeness-flag"),
    pytest.param(_broken(letter_id=123, authoritative=True), id="non-string-letter-id"),
]


def test_only_explicit_authoritative_absence_is_not_sent() -> None:
    for anchor in (None, ORIGINAL):
        assert resolve_lookup(MetadataLookup(None, authoritative=True), anchor) == NotSent()
    assert hold_releasable(NotSent())


@pytest.mark.parametrize("found", SHORT_OF_ABSENCE)
def test_short_of_authoritative_absence_is_never_not_sent(
    found: str | MetadataLookup | None,
) -> None:
    outcome = resolve_lookup(found, ORIGINAL)
    assert isinstance(outcome, Uncertain), (found, outcome)
    assert not hold_releasable(outcome)
    assert pending_evidence(outcome)


@pytest.mark.parametrize(
    "found",
    [
        pytest.param(None, id="no-result-record"),
        pytest.param("", id="empty-bare-string"),
        pytest.param(ORIGINAL, id="bare-matching-id-string"),
        pytest.param(MetadataLookup(), id="empty-list-incomplete"),
        pytest.param(MetadataLookup(None, authoritative=False), id="empty-list-not-authoritative"),
        pytest.param(_broken(authoritative="yes"), id="incomplete-completeness-flag"),
    ],
)
def test_unanchored_lookups_short_of_absence_are_never_not_sent_or_accepted(
    found: str | MetadataLookup | None,
) -> None:
    outcome = resolve_lookup(found)
    assert isinstance(outcome, Uncertain), (found, outcome)
    assert not hold_releasable(outcome)
    assert pending_evidence(outcome)


def test_recorded_original_letter_refuses_later_absence() -> None:
    prior = Uncertain("call intent committed; outcome unknown", ORIGINAL)
    outcome = reconcile_with_original(prior, NotSent())
    assert isinstance(outcome, Uncertain)
    assert outcome.letter_id == ORIGINAL
    assert not hold_releasable(outcome)
    assert pending_evidence(outcome)


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(LobResponse(None, error="timeout"), id="timeout"),
        pytest.param(LobResponse(None, error="provider call failed"), id="provider-call-failed"),
        pytest.param(LobResponse(None), id="no-response"),
        pytest.param(LobResponse(503), id="http-503"),
        pytest.param(LobResponse(200), id="http-200-without-letter-id"),
        pytest.param(
            LobResponse(200, ORIGINAL, evidence=LetterEvidence.CREATED), id="created-only"
        ),
        pytest.param(
            LobResponse(200, ORIGINAL, evidence=LetterEvidence.RENDERED), id="rendered-only"
        ),
        pytest.param(
            LobResponse(200, ORIGINAL, tracking_number="9407111899223197428490"),
            id="bare-tracking-number-only",
        ),
    ],
)
def test_response_errors_and_timeouts_are_never_not_sent(response: LobResponse) -> None:
    outcome = classify_response(response)
    assert isinstance(outcome, Uncertain), (response, outcome)
    assert not hold_releasable(outcome)
    assert pending_evidence(outcome)


@pytest.mark.parametrize(
    ("lookup", "failed"),
    [
        pytest.param("empty-result", False, id="empty-result-list"),
        pytest.param("partial-incomplete", False, id="partial-incomplete-search"),
        pytest.param("bare-string", False, id="bare-string"),
        pytest.param("legacy-none", False, id="legacy-no-result"),
        pytest.param("lookup-error-or-timeout", True, id="lookup-error-or-timeout"),
    ],
)
def test_lookup_short_of_absence_keeps_hold_forbids_resend_and_capture(
    tmp_path: Path, lookup: str, failed: bool
) -> None:
    results: dict[str, str | MetadataLookup | None] = {
        "empty-result": MetadataLookup(),
        "partial-incomplete": MetadataLookup(None, authoritative=False),
        "bare-string": ORIGINAL,
        "legacy-none": None,
        "lookup-error-or-timeout": None,
    }
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = prepared(store)
        client = LookupClient()
        cursor = step(store, client, case)
        assert isinstance(cursor.outcome, Uncertain)
        frozen_key = idempotency_key(case.case_token, case.attempt)
        assert [key for key, _request in client.calls] == [frozen_key]
        client.result = results[lookup]
        client.failed = failed
        now = NOW + timedelta(hours=23)
        cursor = step(store, client, case, cursor, now)
        outcome = cursor.outcome
        assert isinstance(outcome, Uncertain), (lookup, outcome)
        assert not hold_releasable(outcome)
        assert pending_evidence(outcome)
        saved = store.get_case(case.case_token)
        assert saved is not None
        assert saved == case
        assert saved.payment_state is ps.PaymentState.AUTHORIZED
        assert not any(
            row.effect in {ps.PaymentEffect.RELEASE_HOLD, ps.PaymentEffect.CAPTURE_PAYMENT}
            for row in store.effects(case.case_token)
        )
        # Recovery stays on lookup: no create, no new key, no second letter attempt.
        cursor = step(store, client, case, cursor, cursor.next_at)
        assert isinstance(cursor.outcome, Uncertain)
        assert cursor.lookup_only
        assert [key for key, _request in client.calls] == [frozen_key]
        with pytest.raises(ValueError, match="matching approved attempt"):
            run_submission_step(
                store,
                client,
                case.case_token,
                2,
                expected_case_revision=saved.revision,
                expected_cursor_revision=None,
                operation_id=new_id(),
                clock=clock_at(cursor.next_at),
            )
        assert [key for key, _request in client.calls] == [frozen_key]
        assert cursor_for(store, case).outcome == outcome


def test_authoritative_absence_is_the_only_lookup_that_releases(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = prepared(store)
        client = LookupClient()
        cursor = step(store, client, case)
        client.result = MetadataLookup(None, authoritative=True)
        cursor = step(store, client, case, cursor, NOW + timedelta(hours=23), ("route", "release"))
        assert cursor.outcome == NotSent()
        assert hold_releasable(cursor.outcome)
        saved = store.get_case(case.case_token)
        assert saved is not None
        assert saved.payment_state is ps.PaymentState.RELEASED
        assert any(
            row.effect is ps.PaymentEffect.RELEASE_HOLD for row in store.effects(case.case_token)
        )


@pytest.mark.parametrize("listing", ["empty", "partial-page", "ambiguous-ids", "http-error"])
def test_real_lookup_adapter_never_invents_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, listing: str
) -> None:
    def respond(method: str, path: str) -> tuple[int, object]:
        if method == "POST":
            return 200, resource()
        if listing == "http-error":
            return unavailable(method, path)
        if listing == "ambiguous-ids":
            return list_response([resource(), resource(id="ltr_Second")])
        if listing == "partial-page":
            # A first page with a match, but a search that cannot be completed.
            return 200, {
                "object": "list",
                "data": [resource()],
                "count": 1,
                "next_url": "https://evil.invalid/v1/letters?after=partial",
            }
        return list_response([])

    wire = OfflineLob(monkeypatch, respond)
    payments = FakePaymentClient()
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        case = prepared(store)
        first = submit(store, wire, case, NOW)
        assert isinstance(first.outcome, Uncertain)
    now = NOW + timedelta(hours=23)
    with SqliteTransactionStore(path) as store:
        cursor = submit(store, wire, case, now)
        outcome = cursor.outcome
        assert isinstance(outcome, Uncertain), (listing, outcome)
        assert not hold_releasable(outcome)
        assert_nonaccepted(store, case, payments, now)
    assert len(wire.creates) == 1
