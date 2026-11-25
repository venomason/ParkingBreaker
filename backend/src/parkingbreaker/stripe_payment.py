"""The sole Stripe SDK boundary: original-object payments only (v6 note 19).

No clock, retry, queue, free-text evidence or credential repr.
The HTTP wrapper removes secrets/PII before SDK response logging and deserialization.
"""

import json
import re
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from typing import Protocol, cast

import stripe

from parkingbreaker import prices
from parkingbreaker.payment_records import (
    AuthorizationInspection,
    PaymentOutcome,
    RefundEvidence,
    outcome_json,
)
from parkingbreaker.prices import CERTIFIED_CENTS

STRIPE_API_VERSION = "2026-09-30.endive"  # Pinned to the installed Stripe 16.0.0 SDK.


class StripeTransport(Protocol):
    """Injectable single-request HTTP transport; implementations must not retry/log bodies."""

    def request(
        self, method: str, url: str, headers: Mapping[str, str] | None, post_data: object = None
    ) -> tuple[str, int, Mapping[str, str]]: ...


class _BoundaryFailure(Exception):
    def __init__(self, classification: str, authorization_ref: str | None = None) -> None:
        self.classification = classification
        self.authorization_ref = authorization_ref
        super().__init__(classification)


# Only these structural fields can reach the SDK's response logger or our parser.
_FIELDS = frozenset(
    {
        "data",
        "has_more",
        "created",
        "id",
        "object",
        "status",
        "amount",
        "currency",
        "capture_method",
        "amount_received",
        "amount_capturable",
        "latest_charge",
        "payment_intent",
        "amount_captured",
        "captured",
        "paid",
        "payment_method_details",
        "type",
        "card",
        "capture_before",
        "multicapture",
        "payment_method_options",
        "request_multicapture",
        "last_payment_error",
        "code",
        "charge",
    }
)


def _safe_object(value: object) -> object:
    if isinstance(value, dict):
        return {k: _safe_object(v) for k, v in value.items() if k in _FIELDS}
    if isinstance(value, list):
        return [_safe_object(v) for v in value]
    return value


def _unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate response field")
        result[key] = value
    return result


class _HTTPBoundary(stripe.HTTPClient):
    name = "parkingbreaker"

    def __init__(self, transport: StripeTransport | stripe.HTTPClient) -> None:
        super().__init__()
        self._transport = transport

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str] | None,
        post_data: object = None,
        *,
        _usage: list[str] | None = None,
    ) -> tuple[str, int, Mapping[str, str]]:
        try:
            body, status, _response_headers = self._transport.request(
                method, url, headers, post_data
            )
        except Exception:
            raise _BoundaryFailure("transport") from None
        if not 200 <= status < 300:
            classification = (
                "server"
                if status >= 500
                else {
                    429: "rate_limit",
                    409: "conflict",
                    401: "authentication",
                    403: "authentication",
                    404: "not_found",
                }.get(status, "ambiguous_error")
            )
            # HTTP errors, even embedded objects and cached 500s, are not money evidence.
            ref = None
            if status == 402:
                try:
                    error = _mapping(
                        _mapping(json.loads(body, object_pairs_hook=_unique_fields)).get("error")
                    )
                    if error.get("type") == "card_error":
                        # Identity hint only: the original object must be retrieved later.
                        ref = _reference(_mapping(error.get("payment_intent")).get("id"), "pi_")
                except (ValueError, TypeError, RecursionError):
                    pass
            raise _BoundaryFailure(classification, ref)
        try:
            sanitized = json.dumps(
                _safe_object(json.loads(body, object_pairs_hook=_unique_fields)), allow_nan=False
            )
        except (ValueError, TypeError, RecursionError):
            raise _BoundaryFailure("invalid_object") from None
        # No provider headers (request ids etc.) enter logs or persisted records.
        return sanitized, status, {}


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("invalid object")
    return value


def _reference(value: object, prefix: str) -> str:
    if not isinstance(value, str) or re.fullmatch(prefix + r"[A-Za-z0-9]+", value) is None:
        raise ValueError("invalid reference")
    return value


def _integer(value: object) -> int:
    if type(value) is not int:
        raise ValueError("invalid integer")
    return value


def _parse(value: object, ref: str) -> PaymentOutcome:
    obj = _mapping(value)
    amount = _integer(obj.get("amount"))
    if (
        obj.get("object") != "payment_intent"
        or obj.get("id") != ref
        or obj.get("capture_method") != "manual"
        or obj.get("currency") != "usd"
        or amount not in prices.ALLOWED_PRICE_CENTS
    ):
        raise ValueError("invalid original object")
    options = _mapping(obj.get("payment_method_options", {}))
    card_options = _mapping(options.get("card", {}))
    if card_options.get("capture_method") not in {None, "manual"} or card_options.get(
        "request_multicapture"
    ) not in {None, "never"}:
        raise ValueError("unsupported capture options")
    status = obj.get("status")
    received = _integer(obj.get("amount_received"))
    capturable = _integer(obj.get("amount_capturable"))
    if status not in {"requires_capture", "succeeded", "canceled", "requires_payment_method"}:
        raise ValueError("unresolved status")
    if received != (amount if status == "succeeded" else 0) or capturable != (
        amount if status == "requires_capture" else 0
    ):
        raise ValueError("contradictory amounts")
    livemode = obj.get("livemode")
    if livemode is not None and type(livemode) is not bool:
        raise ValueError("invalid payment environment")
    charge_ref: str | None = None
    expiry: int | None = None
    charge = obj.get("latest_charge")
    if charge is not None:
        ch = _mapping(charge)
        charge_ref = _reference(ch.get("id"), "ch_")
        if (
            (ch.get("livemode") is not None and ch.get("livemode") is not livemode)
            or ch.get("object") != "charge"
            or ch.get("payment_intent") != ref
            or ch.get("currency") != "usd"
            or _integer(ch.get("amount")) != amount
            or _integer(ch.get("amount_captured")) != received
            or ch.get("captured") is not (status == "succeeded")
        ):
            raise ValueError("contradictory original charge")
        details = _mapping(ch.get("payment_method_details"))
        card = _mapping(details.get("card"))
        if details.get("type") != "card":
            raise ValueError("not a card authorization")
        multi = card.get("multicapture")
        if multi is not None and _mapping(multi).get("status") != "unavailable":
            raise ValueError("multicapture prohibited")
        before = card.get("capture_before")
        if before is not None:
            expiry = _integer(before)
            if not 0 < _integer(ch.get("created")) < expiry < 253402300800:
                raise ValueError("invalid expiry")
        if status in {"requires_capture", "succeeded"} and (
            ch.get("paid") is not True or ch.get("status") != "succeeded"
        ):
            raise ValueError("unconfirmed charge")
        if status == "canceled" and not (
            (ch.get("paid") is True and ch.get("status") == "succeeded")
            or (ch.get("paid") is False and ch.get("status") == "failed")
        ):
            raise ValueError("contradictory canceled charge")
        if status == "requires_payment_method":
            error = _mapping(obj.get("last_payment_error"))
            if (
                ch.get("paid") is not False
                or ch.get("status") != "failed"
                or error.get("type") != "card_error"
                or error.get("code") != "card_declined"
                or error.get("charge") != charge_ref
            ):
                raise ValueError("unsupported decline evidence")
    if (
        status in {"requires_capture", "succeeded", "requires_payment_method"}
        and charge_ref is None
    ):
        raise ValueError("missing expanded original charge")
    if status == "requires_capture" and expiry is None:
        raise ValueError("missing actual expiry")
    result = PaymentOutcome(
        "declined" if status == "requires_payment_method" else str(status),
        ref,
        amount_cents=amount,
        charge_ref=charge_ref if status == "succeeded" else None,
        currency="usd",
        capture_method="manual",
        amount_received=received,
        amount_capturable=capturable,
        evidence_version=1,
        original_charge_ref=charge_ref,
        capture_before=expiry,
        classification="verified_object",
        livemode=livemode,
    )
    return result


class StripePaymentClient:
    """Caller supplies secret/API version. Only this class imports/owns the SDK.

    SDK 16.0.0's v1 service methods take (intent, params=None, options=None).
    HTTP injection exercises that SDK serialization without any account calls.
    """

    authorization_evidence_required = True

    def __init__(
        self,
        *,
        secret_key: str,
        api_version: str,
        transport: StripeTransport | None = None,
    ) -> None:
        if not secret_key.strip() or not api_version.strip():
            raise ValueError("explicit Stripe secret and API version required")
        self._sdk = stripe.StripeClient(
            secret_key,
            stripe_version=api_version,
            max_network_retries=0,
            http_client=_HTTPBoundary(
                transport if transport is not None else stripe.RequestsClient()
            ),
        )

    def authorize(
        self, amount_cents: int, payment_method_id: str, key: str, metadata: Mapping[str, str]
    ) -> PaymentOutcome:
        """Create the original service card hold; ambiguous creation is never retried here."""
        if type(amount_cents) is not int or amount_cents not in prices.ALLOWED_PRICE_CENTS:
            raise ValueError("full service authorization required")
        if not payment_method_id.startswith("pm_") or not payment_method_id[3:]:
            raise ValueError("payment method id required")
        if not key.strip() or len(key) > 255:
            raise ValueError("valid mutation key required")
        ref = ""
        try:
            obj = self._sdk.v1.payment_intents.create(
                params={
                    "amount": amount_cents,
                    "currency": "usd",
                    "capture_method": "manual",
                    "confirm": True,
                    "payment_method": payment_method_id,
                    "automatic_payment_methods": {"enabled": True, "allow_redirects": "never"},
                    "metadata": dict(metadata),
                    "expand": ["latest_charge"],
                },
                options={"idempotency_key": key, "max_network_retries": 0},
            ).to_dict()
            ref = _reference(obj.get("id"), "pi_")
            outcome = _parse(obj, ref)
            if outcome.amount_cents != amount_cents:
                # A PaymentIntent for another price is never this authorization's hold.
                return self._uncertain(ref, "invalid_object")
            return outcome
        except _BoundaryFailure as exc:
            return self._uncertain(exc.authorization_ref or ref, exc.classification)
        except (ValueError, TypeError, KeyError, OverflowError):
            return self._uncertain(ref, "invalid_object")
        except Exception:
            return self._uncertain(ref, "ambiguous_error")

    def retrieve(self, authorization_ref: str) -> PaymentOutcome:
        return self._call("retrieve", authorization_ref)

    def inspect_authorization(self, authorization_ref: str) -> AuthorizationInspection | None:
        return self.retrieve(authorization_ref).authorization_inspection

    def capture(
        self,
        authorization_ref: str,
        amount_cents: int,
        key: str,
        *,
        final_capture: bool,
    ) -> PaymentOutcome:
        if (
            type(amount_cents) is not int
            or amount_cents not in prices.ALLOWED_PRICE_CENTS
            or final_capture is not True
        ):
            raise ValueError("full final capture required")
        outcome = self._call("capture", authorization_ref, key, amount_cents)
        if outcome.status != "uncertain" and outcome.amount_cents != amount_cents:
            # A PaymentIntent for another amount is never this capture's evidence.
            return self._uncertain(authorization_ref, "invalid_object")
        return outcome

    def release(self, authorization_ref: str, key: str) -> PaymentOutcome:
        return self._call("cancel", authorization_ref, key)

    def retrieve_refunds(self, authorization_ref: str, charge_ref: str) -> PaymentOutcome:
        """Complete original-object reconciliation; bounded pagination fails closed."""
        _reference(charge_ref, "ch_")
        original = self.retrieve(authorization_ref)
        if original.status != "succeeded" or original.original_charge_ref != charge_ref:
            return self._uncertain(authorization_ref, "invalid_object")
        try:
            refunds: dict[str, RefundEvidence] = {}
            after: str | None = None
            for _ in range(100):
                params: stripe.params.RefundListParams = {"charge": charge_ref, "limit": 100}
                if after is not None:
                    params["starting_after"] = after
                page = self._sdk.v1.refunds.list(
                    params=params, options={"max_network_retries": 0}
                ).to_dict()
                if page.get("object") != "list" or type(page.get("has_more")) is not bool:
                    raise ValueError("invalid refund page")
                data = page.get("data")
                if not isinstance(data, list) or len(data) > 100:
                    raise ValueError("invalid refund page data")
                last: str | None = None
                new = 0
                for obj in data:
                    refund = self._parse_refund(
                        obj, authorization_ref, charge_ref, original.amount_cents
                    )
                    last = refund.refund_ref
                    if last in refunds and refunds[last] != refund:
                        raise ValueError("contradictory duplicate refund")
                    new += int(last not in refunds)
                    refunds[last] = refund
                if page["has_more"] is False:
                    result = replace(original, refunds=tuple(refunds.values()))
                    outcome_json(result)
                    return result
                if last is None or last == after or not new:
                    raise ValueError("incomplete refund pagination")
                after = last
            raise ValueError("refund pagination bound")
        except _BoundaryFailure as exc:
            return self._uncertain(authorization_ref, exc.classification)
        except Exception:
            return self._uncertain(authorization_ref, "invalid_object")

    def refund(
        self,
        authorization_ref: str,
        charge_ref: str,
        amount_cents: int,
        key: str,
        *,
        captured_cents: int,
    ) -> PaymentOutcome:
        _reference(authorization_ref, "pi_")
        _reference(charge_ref, "ch_")
        if (
            type(amount_cents) is not int
            or type(captured_cents) is not int
            or captured_cents not in prices.ALLOWED_PRICE_CENTS
            or not 1 <= amount_cents <= captured_cents
        ):
            raise ValueError("refund must be within captured service amount")
        if not isinstance(key, str) or not key.strip() or len(key) > 255:
            raise ValueError("valid mutation key required")
        try:
            obj = self._sdk.v1.refunds.create(
                params={"charge": charge_ref, "amount": amount_cents},
                options={"idempotency_key": key, "max_network_retries": 0},
            )
            refund = self._parse_refund(
                obj.to_dict(), authorization_ref, charge_ref, captured_cents
            )
            if refund.amount_cents != amount_cents:
                raise ValueError("refund response differs from intent")
            # Original captured-money evidence is supplied by the durable intent.
            # This request's refund object is retained, but never completes compensation.
            result = PaymentOutcome(
                "succeeded",
                authorization_ref,
                amount_cents=captured_cents,
                charge_ref=charge_ref,
                currency="usd",
                amount_capturable=0,
                amount_received=captured_cents,
                capture_method="manual",
                evidence_version=1,
                original_charge_ref=charge_ref,
                classification="verified_object",
                refunds=(refund,),
            )
            outcome_json(result)
            return result
        except _BoundaryFailure as exc:
            return replace(
                self._uncertain(authorization_ref, exc.classification), amount_cents=captured_cents
            )
        except Exception:
            return replace(
                self._uncertain(authorization_ref, "invalid_object"), amount_cents=captured_cents
            )

    @staticmethod
    def _parse_refund(obj: object, ref: str, charge: str, captured_cents: int) -> RefundEvidence:
        value = _mapping(obj)
        if (
            value.get("object") != "refund"
            or value.get("charge") != charge
            or value.get("payment_intent") != ref
            or value.get("currency") != "usd"
            or value.get("status")
            not in {"succeeded", "failed", "canceled", "pending", "requires_action"}
            or type(captured_cents) is not int
            or captured_cents not in prices.ALLOWED_PRICE_CENTS
            or not 0 < _integer(value.get("amount")) <= captured_cents
        ):
            raise ValueError("invalid original refund")
        return RefundEvidence(
            _reference(value.get("id"), "re_"),
            ref,
            charge,
            _integer(value.get("amount")),
            "usd",
            str(value["status"]),
        )

    def _call(
        self, kind: str, ref: str, key: str | None = None, amount_cents: int = CERTIFIED_CENTS
    ) -> PaymentOutcome:
        _reference(ref, "pi_")
        if kind != "retrieve" and (not isinstance(key, str) or not key.strip() or len(key) > 255):
            raise ValueError("valid mutation key required")
        try:
            service = self._sdk.v1.payment_intents
            if kind == "retrieve":
                obj = service.retrieve(ref, params={"expand": ["latest_charge"]})
            elif kind == "capture":
                # Single-capture manual holds forbid multicapture; Stripe rejects
                # final_capture on PaymentIntents without multicapture enabled.
                obj = service.capture(
                    ref,
                    params={
                        "amount_to_capture": amount_cents,
                        "expand": ["latest_charge"],
                    },
                    options={"idempotency_key": key, "max_network_retries": 0},
                )
            else:
                obj = service.cancel(
                    ref,
                    params={"expand": ["latest_charge"]},
                    options={"idempotency_key": key, "max_network_retries": 0},
                )
            result = _parse(obj.to_dict(), ref)
            if kind != "retrieve" and result.status == "requires_capture":
                return self._uncertain(ref, "unconfirmed_mutation")
            return result
        except _BoundaryFailure as exc:
            return self._uncertain(ref, exc.classification)
        except (ValueError, TypeError, KeyError, OverflowError):
            return self._uncertain(ref, "invalid_object")
        except Exception:
            return self._uncertain(ref, "ambiguous_error")

    @staticmethod
    def _uncertain(ref: str, classification: str) -> PaymentOutcome:
        return PaymentOutcome(
            "uncertain",
            ref,
            evidence_version=1,
            classification=classification,
        )


def verify_webhook(payload: bytes, signature: str, secret: str, now: datetime) -> dict[str, object]:
    """Verify raw bytes with the SDK; use the caller's clock for replay tolerance."""
    if not secret or not secret.strip():
        raise ValueError("webhook_not_configured")
    try:
        stripe.WebhookSignature.verify_header(payload, signature, secret, tolerance=0)
        event = json.loads(payload)
        if not isinstance(event, dict):
            raise ValueError("invalid event")
        timestamp = int(signature.split("t=", 1)[1].split(",", 1)[0])
        if abs(now.timestamp() - timestamp) > 300:
            raise ValueError("stale signature")
        return cast(dict[str, object], event)
    except Exception:
        raise ValueError("invalid_signature") from None
