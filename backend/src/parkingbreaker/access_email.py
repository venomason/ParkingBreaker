"""One link requester and bounded email dispatcher; no intake or case creation.

The scheduler calls dispatch under the existing database ownership lock. Provider
acceptance records an accepted email API request, never inbox delivery or sign-in.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from parkingbreaker.access_link_material import (
    build_access_url,
    derive_link_token,
    verify_link_token,
)
from parkingbreaker.customer_access import token_digest
from parkingbreaker.email_client import EmailClient, EmailMessage, EmailOutcome, EmailReason
from parkingbreaker.identity_store import _canonical_email, _utc
from parkingbreaker.mail_attempt_store import StorageCorruption
from parkingbreaker.notification_store import NotificationConflict
from parkingbreaker.transaction_store import RevisionConflict, SqliteTransactionStore

if TYPE_CHECKING:
    from parkingbreaker.tracking_email import TrackingEmailService

LOG = logging.getLogger(__name__)


class AccessEmailService:
    def __init__(
        self,
        store: SqliteTransactionStore,
        client: EmailClient,
        *,
        key: str,
        origin: str,
        sender_identity: str,
        clock: Callable[[], datetime],
        new_id: Callable[[], str] = lambda: str(uuid.uuid4()),
        new_nonce: Callable[[], str] = lambda: secrets.token_urlsafe(32),
        tracking: TrackingEmailService | None = None,
    ) -> None:
        token_digest(key)
        build_access_url(origin, key)
        if not sender_identity.strip() or any(
            ord(c) < 32 or ord(c) == 127 for c in sender_identity
        ):
            raise ValueError("invalid email sender")
        self._store, self._client, self._key = store, client, key
        self._origin, self._sender = origin, sender_identity
        self._clock, self._new_id, self._new_nonce = clock, new_id, new_nonce
        self._tracking = tracking

    def __repr__(self) -> str:
        return "AccessEmailService(<redacted>)"

    def queue_link(
        self,
        *,
        email: str,
        request_id: str,
        scope_id: str | None = None,
        workspace_id: str | None = None,
    ) -> None:
        if workspace_id is not None:
            owner = self._store.workspace_owner(workspace_id)
            row = (
                self._store._require_open()
                .execute("SELECT email FROM pb_identity_accounts WHERE customer_id=?", (owner,))
                .fetchone()
            )
            # Knowing a case/workspace ID never authenticates. Recovery sends only
            # to the previously bound contact, with the same generic response.
            if row is None:
                return
            if (
                self._store._require_open()
                .execute("SELECT count(*) FROM pb_intake_owners WHERE owner=?", (owner,))
                .fetchone()[0]
                != 1
            ):
                return
            contact = row[0]
            if contact == "":
                record = self._store.intake_for_workspace(workspace_id)
                if record is None:
                    return
                contact = json.loads(record.intake_json)["contact"]["email"]
            if _canonical_email(contact) != _canonical_email(email):
                return
            if scope_id is not None and owner != scope_id:
                raise NotificationConflict("notification conflict")
            scope_id = owner
        previous = self._store.access_notification(request_id)
        if previous is not None:
            if previous.email != _canonical_email(email) or (
                scope_id is not None and previous.customer_id != scope_id
            ):
                raise NotificationConflict("notification conflict")
            return
        nonce = self._new_nonce()
        raw = derive_link_token(self._key, nonce)
        now = _utc(self._clock())
        self._store.queue_access_link(
            request_id,
            self._new_id(),
            scope_id if scope_id is not None else self._new_id(),
            email,
            nonce,
            token_digest(raw),
            now,
            now + timedelta(minutes=15),
            bound_scope=scope_id,
        )

    def dispatch(self, *, limit: int = 10) -> int:
        processed = 0
        try:
            with self._store.submission_ownership():
                requests = self._store.due_access_emails(self._clock(), limit=limit)
                for request_id in requests:
                    try:
                        note = self._store.access_notification(request_id)
                        if note is None:
                            continue
                        if not verify_link_token(self._key, note.nonce, note.token_digest):
                            # A changed/missing derivation key cannot mint a different credential.
                            self._store.claim_access_email(
                                request_id,
                                operation_id=self._new_id(),
                                message_sha256="0" * 64,
                                now=self._clock(),
                                configuration_valid=False,
                            )
                            continue
                        raw = derive_link_token(self._key, note.nonce)
                        message = EmailMessage(
                            note.email,
                            "Your ParkingBreaker appeal",
                            "Use this private link to access this ParkingBreaker appeal:\n"
                            + build_access_url(self._origin, raw)
                            + "\n\nThis link expires 15 minutes after you requested it. "
                            "Open it and choose Open this appeal. "
                            "If you did not request it, ignore it.",
                        )
                        digest = hashlib.sha256(
                            json.dumps(
                                [self._sender, message.to, message.subject, message.text],
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ).encode()
                        ).hexdigest()
                        call = self._store.claim_access_email(
                            request_id,
                            operation_id=self._new_id(),
                            message_sha256=digest,
                            now=self._clock(),
                        )
                        if call is None:
                            continue
                        try:
                            outcome = self._client.send(message, "pb-access-" + note.effect_id)
                            if not isinstance(outcome, EmailOutcome):
                                raise ValueError("invalid email outcome")
                        except Exception:
                            outcome = EmailOutcome("uncertain", reason=EmailReason.TRANSPORT_ERROR)
                        self._store.finish_access_email(call, outcome, now=self._clock())
                        processed += 1
                    except (NotificationConflict, StorageCorruption):
                        # Preserve faulty rows for diagnosis while independent requests proceed.
                        continue
                if self._tracking is not None:
                    # Sign-in links go first; a tracking-email fault never blocks them.
                    try:
                        processed += self._tracking.dispatch(limit=limit)
                    except Exception:
                        LOG.warning("tracking email dispatch failed")
        except RevisionConflict:
            # A live transaction coordinator owns this same SQLite database.
            return processed
        return processed
