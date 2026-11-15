"""Lob raw-body signature verification (Lob webhook contract)."""

import hashlib
import hmac
import json
from datetime import datetime
from typing import cast

from parkingbreaker.lob_submission import UspsScan
from parkingbreaker.returned_mail import UspsReturnReason, parse_usps_reason

# Sources: provider-qualification-2026-10-17/lob-production.md; lob-python 5.2.1
# model/tracking_event_{normal,certified}.py; Lob's official event table:
# https://lob.github.io/lob-openapi/ (Letters and Tracking Events).
# USPS possession: https://help.lob.com/print-and-mail/getting-data-and-results/tracking-your-mail
# Mailed is a printer handoff signal, not a USPS scan. Issue is ambiguous. Neither
# qualifies. Return-envelope events concern a different mailpiece and never qualify.
# T26 must verify actual plan delivery and pinned SDK parsing for all enabled events.
USPS_SCAN_NAMES = frozenset(
    {
        "In Transit",
        "In Local Area",
        "Processed for Delivery",
        "Delivered",
        "Re-Routed",
        "Returned to Sender",
        "International Exit",
    }
)
USPS_SCAN_WEBHOOKS = frozenset(
    f"letter.{prefix}{suffix}"
    for prefix in ("", "certified.")
    for suffix in (
        "in_transit",
        "in_local_area",
        "processed_for_delivery",
        "delivered",
        "re-routed",
        "returned_to_sender",
    )
) | {"letter.international_exit", "letter.certified.pickup_available"}


def has_usps_scan(events: object) -> bool:
    """SDK models or webhook dicts, with explicit type/name; unknown values fail closed."""
    if not isinstance(events, list):
        return False
    for event in events:
        kind = event.get("type") if isinstance(event, dict) else getattr(event, "type", None)
        name = event.get("name") if isinstance(event, dict) else getattr(event, "name", None)
        if (
            isinstance(kind, str)
            and isinstance(name, str)
            and (
                (kind == "normal" and name in USPS_SCAN_NAMES)
                or (
                    kind == "certified"
                    and (
                        (name in USPS_SCAN_NAMES and name != "International Exit")
                        or name == "Pickup Available"
                    )
                )
            )
        ):
            return True
    return False


def scan_webhook_qualifies(event: str, body: dict[str, object], token: str) -> bool:
    metadata = body.get("metadata")
    return (
        event in USPS_SCAN_WEBHOOKS
        and isinstance(metadata, dict)
        and metadata.get("submission_token") == token
        and (body.get("deleted") is None or body.get("deleted") is False)
        and body.get("status") in (None, "processed", "rendered")
        and body.get("failure_reason") is None
    )


FAILURE_WEBHOOKS = frozenset({"letter.failed", "letter.deleted"})


def failure_webhook_qualifies(event: str, body: dict[str, object], token: str) -> bool:
    """Exact-letter identity checks for failure evidence (same seam as the scan path).

    A failed/deleted event counts as mailing evidence only for the exact frozen
    request: the same submission-token check as the scan path. A mismatched failure
    is recorded but ignored as evidence (§20.19, 2026-10-22).
    """
    metadata = body.get("metadata")
    return (
        event in FAILURE_WEBHOOKS
        and isinstance(metadata, dict)
        and metadata.get("submission_token") == token
    )


def scan_proof(events: object) -> tuple[UspsScan, ...]:
    """The provider's USPS scan events kept exactly as sent (type/name/time/details).

    Same predicate as has_usps_scan: a printer handoff such as "Mailed" is not USPS
    possession, so it is never presented as a scan.
    """
    if not isinstance(events, list):
        return ()
    scans: list[UspsScan] = []
    for event in events:
        if not isinstance(event, dict) or not has_usps_scan([event]):
            continue
        name = event.get("name")
        time = event.get("time")
        details = event.get("details")
        scans.append(
            UspsScan(
                cast("str", event["type"]),
                cast("str", name),
                time if isinstance(time, str) else None,
                cast("dict[str, object]", details) if isinstance(details, dict) else None,
            )
        )
    return tuple(scans)


def has_returned_scan(scans: tuple[UspsScan, ...]) -> bool:
    """Retained exact-letter carrier evidence, independent of webhook delivery."""
    return any(
        scan.event_type in {"normal", "certified"} and scan.name == "Returned to Sender"
        for scan in scans
    )


def usps_return_reason(body: object) -> UspsReturnReason | None:
    """The USPS return reason a signed return webhook carries, when recognizable.

    Lob reports the carrier substatus in the returned-to-sender tracking event's
    `details.event` (TrackingEventDetails); anything else is no reason, never a guess.
    T26 must qualify the exact production field and mapping.
    """
    if not isinstance(body, dict):
        return None
    events = body.get("tracking_events")
    if not isinstance(events, list):
        return None
    for event in events:
        if (
            not isinstance(event, dict)
            or event.get("type") not in ("normal", "certified")
            or event.get("name") != "Returned to Sender"
        ):
            continue
        details = event.get("details")
        if not isinstance(details, dict):
            continue
        reason = parse_usps_reason(details.get("event"))
        if reason is not None:
            return reason
    return None


def verify_webhook(
    payload: bytes, signature: str, timestamp: str, secret: str, now: datetime
) -> dict[str, object]:
    """HMAC-SHA256(timestamp + '.' + raw bytes), with five-minute tolerance.

    https://help.lob.com/print-and-mail/getting-data-and-results/using-webhooks
    """
    if not secret or not secret.strip():
        raise ValueError("webhook_not_configured")
    try:
        if abs(now.timestamp() - int(timestamp)) > 300:
            raise ValueError("stale signature")
        expected = hmac.new(
            secret.encode(), timestamp.encode() + b"." + payload, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise ValueError("signature mismatch")
        data = json.loads(payload)
        if not isinstance(data, dict):
            raise ValueError("invalid event")
        return cast(dict[str, object], data)
    except (ValueError, TypeError):
        raise ValueError("invalid_signature") from None
