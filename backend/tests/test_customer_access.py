from __future__ import annotations

import base64
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from parkingbreaker.customer_access import AccessService, token_digest
from parkingbreaker.identity_store import IdentitySession
from parkingbreaker.transaction_store import SqliteTransactionStore

NOW = datetime(2026, 10, 17, 12, tzinfo=UTC)


def credential(value: int) -> str:
    return base64.urlsafe_b64encode(bytes([value]) * 32).decode("ascii").rstrip("=")


def issue_link(
    store: SqliteTransactionStore,
    raw_link: str,
    customer_id: str,
    email: str,
    *,
    issued_at: datetime = NOW,
    lifetime: timedelta = timedelta(minutes=10),
) -> None:
    store.issue_identity_link(
        customer_id=customer_id,
        email=email,
        token_digest=token_digest(raw_link),
        issued_at=issued_at,
        expires_at=issued_at + lifetime,
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SqliteTransactionStore]:
    result = SqliteTransactionStore(tmp_path / "parkingbreaker.sqlite")
    yield result
    result.close()


def test_token_digest_accepts_only_canonical_token_urlsafe_32() -> None:
    raw = credential(1)
    assert len(raw) == 43
    assert token_digest(raw) == token_digest(raw)
    for invalid in ("", "short", raw + "A", "é" * 43, raw + "=", "A" * 42 + "B"):
        with pytest.raises(ValueError, match=r"^invalid credential$"):
            token_digest(invalid)


def test_confirm_persists_two_owner_isolated_sessions_and_hides_raw_credentials(
    store: SqliteTransactionStore,
) -> None:
    link_a, link_b = credential(1), credential(2)
    session_a, csrf_a, session_b, csrf_b = map(credential, (3, 4, 5, 6))
    issue_link(store, link_a, "customer-a", "a@example.test")
    issue_link(store, link_b, "customer-b", "b@example.test")
    generated = iter((session_a, csrf_a, session_b, csrf_b))
    clock_calls: list[int] = []

    def clock() -> datetime:
        clock_calls.append(1)
        return NOW

    access = AccessService(store, clock=clock, token_factory=lambda: next(generated))

    credentials_a = access.confirm(link_a)
    credentials_b = access.confirm(link_b)
    assert credentials_a is not None
    assert credentials_b is not None
    assert credentials_a.customer_id == "customer-a"
    assert credentials_b.customer_id == "customer-b"
    assert credentials_a.expires_at == credentials_b.expires_at == NOW + timedelta(days=30)
    authenticated_a = access.authenticate(session_a)
    authenticated_b = access.authenticate(session_b)
    assert authenticated_a is not None
    assert authenticated_a.customer_id == "customer-a"
    assert authenticated_b is not None
    assert authenticated_b.customer_id == "customer-b"
    session_record = access.authenticate(session_a)
    assert isinstance(session_record, IdentitySession)
    assert access.csrf_valid(session_record, csrf_a)
    assert not access.csrf_valid(session_record, csrf_b)
    assert len(clock_calls) == 5  # once for each confirmation and authentication
    assert session_a not in repr(credentials_a)
    assert csrf_a not in repr(credentials_a)

    rows = (
        store._require_open()
        .execute(
            "SELECT token_digest FROM pb_identity_links UNION ALL "
            "SELECT session_digest FROM pb_identity_sessions UNION ALL "
            "SELECT csrf_digest FROM pb_identity_sessions"
        )
        .fetchall()
    )
    persisted = {row[0] for row in rows}
    assert persisted == {
        token_digest(link_a),
        token_digest(link_b),
        token_digest(session_a),
        token_digest(session_b),
        token_digest(csrf_a),
        token_digest(csrf_b),
    }
    for raw in (link_a, link_b, session_a, session_b, csrf_a, csrf_b):
        assert raw not in persisted


def test_confirmation_is_one_use_and_expiration_is_exclusive(
    store: SqliteTransactionStore,
) -> None:
    valid_link, expired_link = credential(11), credential(12)
    issue_link(store, valid_link, "customer-a", "a@example.test")
    issue_link(
        store,
        expired_link,
        "customer-b",
        "b@example.test",
        issued_at=NOW - timedelta(minutes=10),
        lifetime=timedelta(minutes=10),
    )
    generated = iter(map(credential, (13, 14, 15, 16, 17, 18)))
    clock = [NOW]
    access = AccessService(store, clock=lambda: clock[0], token_factory=lambda: next(generated))
    first = access.confirm(valid_link)
    assert first is not None
    assert access.confirm(valid_link) is None
    assert access.confirm(expired_link) is None
    expired_digest = token_digest(expired_link)
    consumed = (
        store._require_open()
        .execute(
            "SELECT consumed_at FROM pb_identity_links WHERE token_digest=?", (expired_digest,)
        )
        .fetchone()[0]
    )
    assert consumed is None

    clock[0] = first.expires_at - timedelta(microseconds=1)
    assert access.authenticate(first.session_token) is not None
    clock[0] = first.expires_at
    assert access.authenticate(first.session_token) is None


def test_revoke_is_idempotent_and_uses_the_injected_clock(store: SqliteTransactionStore) -> None:
    raw_link = credential(21)
    session_token, csrf_token = credential(22), credential(23)
    issue_link(store, raw_link, "customer-a", "a@example.test")
    now = [NOW]
    access = AccessService(
        store,
        clock=lambda: now[0],
        token_factory=iter((session_token, csrf_token)).__next__,
    )
    credentials = access.confirm(raw_link)
    assert credentials is not None
    now[0] += timedelta(minutes=1)
    access.revoke(session_token)
    access.revoke(session_token)
    assert access.authenticate(session_token) is None
    revoked_at = (
        store._require_open()
        .execute(
            "SELECT revoked_at FROM pb_identity_sessions WHERE session_digest=?",
            (token_digest(session_token),),
        )
        .fetchone()[0]
    )
    assert revoked_at == (NOW + timedelta(minutes=1)).isoformat(timespec="microseconds")


def test_malformed_or_reused_generated_credentials_leave_link_usable(
    store: SqliteTransactionStore,
) -> None:
    raw_link = credential(31)
    issue_link(store, raw_link, "customer-a", "a@example.test")
    malformed = AccessService(
        store,
        clock=lambda: NOW,
        token_factory=iter(("short", credential(32))).__next__,
    )
    assert malformed.confirm(raw_link) is None
    duplicate = credential(33)
    duplicated = AccessService(store, clock=lambda: NOW, token_factory=lambda: duplicate)
    assert duplicated.confirm(raw_link) is None
    assert (
        store._require_open()
        .execute(
            "SELECT consumed_at FROM pb_identity_links WHERE token_digest=?",
            (token_digest(raw_link),),
        )
        .fetchone()[0]
        is None
    )

    valid = AccessService(
        store,
        clock=lambda: NOW,
        token_factory=iter((credential(34), credential(35))).__next__,
    )
    assert valid.confirm(raw_link) is not None


def test_session_digest_collision_rolls_back_link_consumption(
    store: SqliteTransactionStore,
) -> None:
    first_link, second_link = credential(41), credential(42)
    shared_session = credential(43)
    issue_link(store, first_link, "customer-a", "a@example.test")
    issue_link(store, second_link, "customer-b", "b@example.test")
    first = AccessService(
        store,
        clock=lambda: NOW,
        token_factory=iter((shared_session, credential(44))).__next__,
    )
    assert first.confirm(first_link) is not None

    collision = AccessService(
        store,
        clock=lambda: NOW,
        token_factory=iter((shared_session, credential(45))).__next__,
    )
    assert collision.confirm(second_link) is None
    second_digest = token_digest(second_link)
    assert (
        store._require_open()
        .execute("SELECT consumed_at FROM pb_identity_links WHERE token_digest=?", (second_digest,))
        .fetchone()[0]
        is None
    )
    assert (
        store._require_open()
        .execute("SELECT verified_at FROM pb_identity_accounts WHERE customer_id='customer-b'")
        .fetchone()[0]
        is None
    )

    retry = AccessService(
        store,
        clock=lambda: NOW,
        token_factory=iter((credential(46), credential(47))).__next__,
    )
    assert retry.confirm(second_link) is not None


@pytest.mark.parametrize("raw", ["fake-alice", "fake-bob", "", "x" * 44, "é" * 43])
def test_invalid_credentials_fail_closed_without_store_or_clock_access(
    raw: str, store: SqliteTransactionStore
) -> None:
    calls: list[str] = []

    class SpyStore:
        def issue_appeal_start(self, **_kwargs: object) -> None:
            raise AssertionError("unexpected appeal start")

        def consume_identity_link(self, *_args: object, **_kwargs: object) -> None:
            calls.append("consume")
            raise AssertionError("invalid credential reached store")

        def identity_session(self, *_args: object, **_kwargs: object) -> None:
            calls.append("authenticate")
            raise AssertionError("invalid credential reached store")

        def revoke_identity_session(self, *_args: object, **_kwargs: object) -> None:
            calls.append("revoke")
            raise AssertionError("invalid credential reached store")

    access = AccessService(
        SpyStore(),
        clock=lambda: (_ for _ in ()).throw(AssertionError("clock must not be read")),
        token_factory=lambda: (_ for _ in ()).throw(AssertionError("factory must not be called")),
    )
    assert access.confirm(raw) is None
    assert access.authenticate(raw) is None
    access.revoke(raw)
    assert not calls
    assert not access.csrf_valid(
        IdentitySession("0" * 64, "customer-a", "0" * 64, NOW, NOW + timedelta(days=1), None), raw
    )
