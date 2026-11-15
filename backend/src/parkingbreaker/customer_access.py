"""One-use email-link confirmation and opaque browser-session orchestration.

HTTP, email delivery, cookie policy, and CSRF request handling live in a later slice.
This module accepts only canonical opaque 32-byte base64url credentials and delegates
durability and atomic link consumption to the existing transaction store.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast

from parkingbreaker.identity_store import IdentityConflict, IdentitySession
from parkingbreaker.prices import PRICE_EXPERIMENT_ID
from parkingbreaker.pricing_store import PricingAssignment

_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")


class IdentitySessionStore(Protocol):
    """The identity operations already implemented by SqliteTransactionStore."""

    def issue_appeal_start(
        self,
        *,
        scope_id: str,
        link_digest: str,
        now: datetime,
        expires_at: datetime,
        pricing_digest: str | None = None,
    ) -> None: ...

    def consume_identity_link(
        self,
        token_digest: str,
        *,
        session_digest: str,
        csrf_digest: str,
        now: datetime,
        session_expires_at: datetime,
    ) -> IdentitySession | None: ...

    def identity_session(self, session_digest: str, *, now: datetime) -> IdentitySession | None: ...

    def revoke_identity_session(self, session_digest: str, *, now: datetime) -> None: ...


class PricingSessionStore(IdentitySessionStore, Protocol):
    """Optional visitor-pricing extension; legacy access-only fakes keep their contract."""

    def pricing_assignment(self, digest: str) -> PricingAssignment | None: ...
    def pricing_for_scope(self, scope_id: str) -> PricingAssignment | None: ...
    def create_pricing_assignment(self, digest: str, *, now: datetime) -> PricingAssignment: ...
    def issue_pricing_credential(self, visitor_id: str, digest: str) -> PricingAssignment: ...


@dataclass(frozen=True)
class AccessCredentials:
    """Raw browser credentials are returned once and omitted from repr output."""

    customer_id: str
    session_token: str = field(repr=False)
    csrf_token: str = field(repr=False)
    expires_at: datetime


def token_digest(raw_token: str) -> str:
    """Return the SHA-256 hex digest of one canonical token_urlsafe(32) credential."""
    if not isinstance(raw_token, str) or _TOKEN.fullmatch(raw_token) is None:
        raise ValueError("invalid credential")
    try:
        encoded = raw_token.encode("ascii")
        decoded = base64.b64decode(encoded + b"=", altchars=b"-_", validate=True)
    except (UnicodeEncodeError, ValueError):
        raise ValueError("invalid credential") from None
    if len(decoded) != 32 or base64.urlsafe_b64encode(decoded).rstrip(b"=") != encoded:
        raise ValueError("invalid credential")
    return hashlib.sha256(encoded).hexdigest()


class AccessService:
    """Turn a valid one-use link into a persisted opaque session and CSRF secret."""

    def __init__(
        self,
        store: IdentitySessionStore,
        *,
        clock: Callable[[], datetime],
        token_factory: Callable[[], str] | None = None,
        session_lifetime: timedelta = timedelta(days=30),
    ) -> None:
        if not isinstance(session_lifetime, timedelta) or session_lifetime <= timedelta(0):
            raise ValueError("session_lifetime must be positive")
        self._store = store
        self._clock = clock
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(32))
        self._session_lifetime = session_lifetime

    def browser_pricing(
        self, raw_token: str | None, *, scope_id: str | None = None
    ) -> tuple[PricingAssignment, str | None]:
        """Recognize a price without granting access; only callers authenticate scope_id."""
        store = cast(PricingSessionStore, self._store)
        current: PricingAssignment | None = None
        if raw_token is not None:
            with suppress(ValueError):
                current = store.pricing_assignment(token_digest(raw_token))
        bound = store.pricing_for_scope(scope_id) if scope_id is not None else None
        if bound is not None and bound.experiment_id == PRICE_EXPERIMENT_ID:
            if current is not None and current.visitor_id == bound.visitor_id:
                return bound, None
            token = self._token_factory()
            assignment = store.issue_pricing_credential(bound.visitor_id, token_digest(token))
            return assignment, token
        if current is not None and current.experiment_id == PRICE_EXPERIMENT_ID:
            return current, None
        token = self._token_factory()
        assignment = store.create_pricing_assignment(token_digest(token), now=_utc(self._clock()))
        return assignment, token

    def has_scope_pricing(self, scope_id: str) -> bool:
        assignment = cast(PricingSessionStore, self._store).pricing_for_scope(scope_id)
        return assignment is not None and assignment.experiment_id == PRICE_EXPERIMENT_ID

    def start_appeal(self, *, pricing_token: str | None = None) -> AccessCredentials:
        """Create a fresh anonymous appeal scope; no customer account or email required."""
        now = _utc(self._clock())
        link = self._token_factory()
        self._store.issue_appeal_start(
            scope_id="appeal_" + uuid.uuid4().hex,
            link_digest=token_digest(link),
            now=now,
            expires_at=now + timedelta(minutes=1),
            pricing_digest=token_digest(pricing_token) if pricing_token is not None else None,
        )
        credentials = self.confirm(link)
        if credentials is None:
            raise IdentityConflict("appeal access unavailable")
        return credentials

    def confirm(self, raw_link: str) -> AccessCredentials | None:
        """Consume a well-formed link once; malformed entropy fails before store access."""
        try:
            link_hash = token_digest(raw_link)
        except ValueError:
            return None

        try:
            now = _utc(self._clock())
            session_token = self._token_factory()
            csrf_token = self._token_factory()
            session_hash = token_digest(session_token)
            csrf_hash = token_digest(csrf_token)
            expires_at = now + self._session_lifetime
        except Exception:
            # Do not leak a credential, email, or injected entropy/clock exception.
            return None
        if session_hash in (csrf_hash, link_hash) or csrf_hash == link_hash:
            return None

        try:
            session = self._store.consume_identity_link(
                link_hash,
                session_digest=session_hash,
                csrf_digest=csrf_hash,
                now=now,
                session_expires_at=expires_at,
            )
        except IdentityConflict:
            # The store transaction rolls back a generated-ID collision, leaving the link usable.
            return None
        if session is None:
            return None
        return AccessCredentials(
            customer_id=session.customer_id,
            session_token=session_token,
            csrf_token=csrf_token,
            expires_at=session.expires_at,
        )

    def authenticate(self, raw_session: str) -> IdentitySession | None:
        try:
            session_hash = token_digest(raw_session)
            now = _utc(self._clock())
            return self._store.identity_session(session_hash, now=now)
        except (IdentityConflict, ValueError):
            return None

    def csrf_valid(self, session: IdentitySession, raw_csrf: str) -> bool:
        try:
            csrf_hash = token_digest(raw_csrf)
            return hmac.compare_digest(session.csrf_digest, csrf_hash)
        except (AttributeError, TypeError, ValueError):
            return False

    def revoke(self, raw_session: str) -> None:
        try:
            session_hash = token_digest(raw_session)
            now = _utc(self._clock())
            self._store.revoke_identity_session(session_hash, now=now)
        except (IdentityConflict, ValueError):
            return


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("invalid access time")
    try:
        return value.astimezone(UTC)
    except (OverflowError, ValueError):
        raise ValueError("invalid access time") from None
