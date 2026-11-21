"""Validated email-access composition on the same SQLite connection as customer cases."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

from parkingbreaker.access_email import AccessEmailService
from parkingbreaker.access_link_material import build_access_url
from parkingbreaker.api.customer_auth import AuthHttp
from parkingbreaker.config import ConfigError, ProviderMode, Settings
from parkingbreaker.customer_access import AccessService, token_digest
from parkingbreaker.email_client import FakeEmailClient, ResendEmailClient
from parkingbreaker.outcome_follow_up import OutcomeFollowUpService, OutcomeLinkService
from parkingbreaker.tracking_email import TrackingEmailService
from parkingbreaker.transaction_store import SqliteTransactionStore


@dataclass(frozen=True, repr=False)
class AuthSettings:
    origin: str
    link_key: str
    sender: str
    api_key: str | None


def load_auth_settings(settings: Settings) -> AuthSettings | None:
    """Validate before opening databases or constructing any provider clients."""
    mode = settings.values.get("PB_AUTH_MODE", "")
    if mode != "email":
        if settings.mode in {ProviderMode.TEST, ProviderMode.LIVE}:
            raise RuntimeError("customer_auth_not_configured")
        if mode:
            raise ConfigError("PB_AUTH_MODE must be email or unset for local preview")
        return None
    if settings.mode is ProviderMode.PREVIEW:
        raise ConfigError("PB_AUTH_MODE email requires persistent service mode")
    key = settings.values.get("PB_AUTH_LINK_KEY", "")
    try:
        token_digest(key)
    except ValueError:
        raise ConfigError("PB_AUTH_LINK_KEY must be a canonical 32-byte base64url key") from None
    try:
        parsed = urlsplit(build_access_url(settings.values.get("PB_PUBLIC_ORIGIN", ""), key))
        origin = f"{parsed.scheme}://{parsed.netloc}"
    except (ValueError, TypeError):
        raise ConfigError("PB_PUBLIC_ORIGIN must be a public HTTPS origin") from None
    sender = settings.values.get("RESEND_FROM_EMAIL", "")
    api_key = (
        None if settings.mode is ProviderMode.FAKE else settings.values.get("RESEND_API_KEY", "")
    )
    try:
        # Constructor validation is local; it performs no request. Same sender is bound
        # into both provider payloads and the durable message fingerprint below.
        ResendEmailClient("offline-validation", sender)
    except ValueError:
        raise ConfigError("RESEND_FROM_EMAIL must be a nonblank email sender") from None
    if api_key is not None:
        try:
            ResendEmailClient(api_key, sender)
        except ValueError:
            raise ConfigError("RESEND_API_KEY must be configured for email access") from None
    return AuthSettings(origin, key, sender, api_key)


def build_customer_access(
    settings: AuthSettings,
    store: SqliteTransactionStore,
    *,
    clock: Callable[[], datetime],
) -> tuple[AuthHttp, AccessEmailService]:
    # Fake mode never crosses the email boundary and does not invent delivery success.
    client = (
        FakeEmailClient()
        if settings.api_key is None
        else ResendEmailClient(settings.api_key, settings.sender)
    )
    delivery = AccessEmailService(
        store,
        client,
        key=settings.link_key,
        origin=settings.origin,
        sender_identity=settings.sender,
        clock=clock,
        tracking=TrackingEmailService(store, client, clock=clock),
    )
    return AuthHttp(
        AccessService(store, clock=clock), delivery, origin=settings.origin, clock=clock
    ), delivery


def build_outcome_follow_up(
    settings: AuthSettings,
    store: SqliteTransactionStore,
    *,
    clock: Callable[[], datetime],
) -> tuple[OutcomeFollowUpService, OutcomeLinkService]:
    """Compose the J05 outcome follow-up sender and private link verifier."""
    client = (
        FakeEmailClient()
        if settings.api_key is None
        else ResendEmailClient(settings.api_key, settings.sender)
    )
    return (
        OutcomeFollowUpService(
            store,
            client,
            key=settings.link_key,
            origin=settings.origin,
            sender=settings.sender,
            clock=clock,
        ),
        OutcomeLinkService(store, key=settings.link_key, clock=clock),
    )
