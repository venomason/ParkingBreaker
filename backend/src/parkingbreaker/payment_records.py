"""Durable payment values and strict canonical codecs; no I/O (v6 notes 17/18)."""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Protocol

from parkingbreaker import prices
from parkingbreaker.prices import CERTIFIED_CENTS


@dataclass(frozen=True)
class RefundEvidence:
    refund_ref: str
    authorization_ref: str
    charge_ref: str
    amount_cents: int
    currency: str
    status: str


@dataclass(frozen=True)
class PaymentOutcome:
    """Adapter-confirmed original PaymentIntent status, or explicit uncertainty.

    transient means a confirmed unsuccessful mutation, never a timeout. expired and
    declined require definitive provider evidence. succeeded includes its charge id.
    """

    status: str
    authorization_ref: str
    # Defaults to the default price for scripted fakes and bare uncertain outcomes. Verified
    # Stripe outcomes carry the PaymentIntent's own amount, and every caller compares each
    # outcome with the case's recorded amount before trusting it.
    amount_cents: int = CERTIFIED_CENTS
    charge_ref: str | None = None
    currency: str | None = None
    amount_capturable: int | None = None
    amount_received: int | None = None
    capture_method: str | None = None
    evidence_version: int | None = None
    original_charge_ref: str | None = None
    capture_before: int | None = None
    classification: str | None = None

    # None means unavailable/incomplete. Only retrieve_refunds supplies a complete list;
    # mutation responses retain their single object and cannot settle the cursor.
    refunds: tuple[RefundEvidence, ...] | None = None

    @property
    def full_refund(self) -> bool:
        return self.amount_cents in prices.ALLOWED_PRICE_CENTS and self.refund_settled(
            self.amount_cents
        )

    def refund_settled(self, amount_cents: int) -> bool:
        """Only complete, quiescent original-charge evidence settles an exact intent."""
        return (
            self.refunds is not None
            and sum(r.amount_cents for r in self.refunds if r.status == "succeeded") == amount_cents
            and all(r.status in {"succeeded", "failed", "canceled"} for r in self.refunds)
        )

    @property
    def refundable(self) -> bool:
        return self.refunds is not None and all(
            r.status in {"failed", "canceled"} for r in self.refunds
        )

    @property
    def authorization_inspection(self) -> "AuthorizationInspection | None":
        if (
            self.status == "requires_capture"
            and self.full_object
            and self.evidence_version == 1
            and self.original_charge_ref is not None
            and self.capture_before is not None
        ):
            return AuthorizationInspection(
                self.authorization_ref,
                self.original_charge_ref,
                datetime.fromtimestamp(self.capture_before, UTC),
            )
        return None

    @property
    def full_object(self) -> bool:
        return (
            type(self.amount_cents) is int
            and self.amount_cents in prices.ALLOWED_PRICE_CENTS
            and self.currency == "usd"
            and self.capture_method == "manual"
            and type(self.amount_capturable) is int
            and type(self.amount_received) is int
            and self.amount_capturable
            == (self.amount_cents if self.status == "requires_capture" else 0)
            and self.amount_received == (self.amount_cents if self.status == "succeeded" else 0)
        )


@dataclass(frozen=True)
class AuthorizationInspection:
    """Verified card hold expiry for start_case; never resets caller authorized_at."""

    authorization_ref: str
    original_charge_ref: str
    expires_at: datetime


class PaymentClient(Protocol):
    """Only the original manual-capture USD service PaymentIntent is addressable.

    Retrieve verifies identity, full amount, capturable and received amounts.
    requires_capture proves zero received and the full service amount capturable. Capture uses
    exactly the service amount with final_capture=True; no multicapture/replacement/reauth.
    Cached HTTP 500 and transport failures remain uncertain until retrieval.
    Refund addresses only the immutable original charge for up to the service amount.
    retrieve_refunds verifies that PI/charge and returns all pages, validated and deduped;
    unavailable/incomplete evidence is uncertain, never an empty list. A create response
    is not completion; only a later complete lookup can confirm the exact requested total.
    """

    authorization_evidence_required: bool

    def authorize(
        self, amount_cents: int, payment_method_id: str, key: str, metadata: Mapping[str, str]
    ) -> PaymentOutcome: ...

    def retrieve(self, authorization_ref: str) -> PaymentOutcome: ...

    def capture(
        self, authorization_ref: str, amount_cents: int, key: str, *, final_capture: bool
    ) -> PaymentOutcome: ...

    def release(self, authorization_ref: str, key: str) -> PaymentOutcome: ...

    def retrieve_refunds(self, authorization_ref: str, charge_ref: str) -> PaymentOutcome: ...

    def refund(
        self,
        authorization_ref: str,
        charge_ref: str,
        amount_cents: int,
        key: str,
        *,
        captured_cents: int,
    ) -> PaymentOutcome: ...


@dataclass(frozen=True)
class PaymentCursor:
    """Refund amount is the exact remedy; outcome money remains the original capture."""

    case_token: str
    effect_id: str
    kind: str
    authorization_ref: str
    amount_cents: int
    key: str
    first_at: datetime
    failed_attempts: int
    next_at: datetime
    revision: int
    case_revision: int
    owner: str | None
    call_kind: str
    outcome: PaymentOutcome
    done: bool
    mutation_key: str | None = None
    final_capture: bool | None = None
    contract_version: int = 1
    authorization_charge_ref: str | None = None
    compensation_started: bool = False


def outcome_json(value: PaymentOutcome) -> str:
    if (
        value.status
        not in {
            "requires_capture",
            "succeeded",
            "canceled",
            "expired",
            "declined",
            "transient",
            "uncertain",
        }
        or not isinstance(value.authorization_ref, str)
        or not value.authorization_ref.strip()
        or type(value.amount_cents) is not int
        or value.amount_cents not in prices.ALLOWED_PRICE_CENTS
        or (
            value.status == "succeeded"
            and (not isinstance(value.charge_ref, str) or not value.charge_ref.strip())
        )
        or (value.status != "succeeded" and value.charge_ref is not None)
    ):
        raise ValueError("invalid payment outcome")
    data = asdict(value)
    if value.refunds is None:
        del data["refunds"]
    else:
        if not (
            value.status == "succeeded"
            and value.full_object
            and value.evidence_version == 1
            and value.original_charge_ref == value.charge_ref
        ):
            raise ValueError("refund lookup requires authoritative original capture")
        seen: set[str] = set()
        for refund in value.refunds:
            if (
                not refund.refund_ref.startswith("re_")
                or not refund.refund_ref[3:].isalnum()
                or refund.refund_ref in seen
                or refund.authorization_ref != value.authorization_ref
                or refund.charge_ref != value.charge_ref
                or refund.currency != "usd"
                or type(refund.amount_cents) is not int
                or type(value.amount_cents) is not int
                or not 0 < refund.amount_cents <= value.amount_cents
                or refund.status
                not in {"succeeded", "failed", "canceled", "pending", "requires_action"}
            ):
                raise ValueError("invalid original refund evidence")
            seen.add(refund.refund_ref)
        if (
            type(value.amount_cents) is not int
            or sum(r.amount_cents for r in value.refunds if r.status not in {"failed", "canceled"})
            > value.amount_cents
        ):
            raise ValueError("excess refund evidence")
    evidence_fields = (
        "evidence_version",
        "original_charge_ref",
        "capture_before",
        "classification",
    )
    if value.evidence_version is None:
        if any(data[field] is not None for field in evidence_fields):
            raise ValueError("unversioned payment evidence")
        for field in evidence_fields:
            del data[field]
    else:
        if type(value.evidence_version) is not int or value.evidence_version != 1:
            raise ValueError("invalid payment evidence version")
        if value.classification not in {
            "verified_object",
            "invalid_object",
            "transport",
            "server",
            "rate_limit",
            "conflict",
            "authentication",
            "configuration",
            "not_found",
            "ambiguous_error",
            "expiry_mismatch",
            "unconfirmed_mutation",
            "charge_mismatch",
        }:
            raise ValueError("invalid response classification")
        if (value.status == "uncertain") == (value.classification == "verified_object"):
            raise ValueError("contradictory response classification")
        if value.original_charge_ref is not None and (
            not isinstance(value.original_charge_ref, str)
            or not value.original_charge_ref.startswith("ch_")
            or not value.original_charge_ref[3:].isalnum()
        ):
            raise ValueError("invalid original charge reference")
        if value.capture_before is not None and (
            type(value.capture_before) is not int
            or not 0 < value.capture_before < 253402300800
            or value.original_charge_ref is None
        ):
            raise ValueError("invalid authorization expiry evidence")
        if value.status == "requires_capture" and value.authorization_inspection is None:
            raise ValueError("missing authorization evidence")
        if value.status == "succeeded" and value.original_charge_ref != value.charge_ref:
            raise ValueError("mismatched original charge")
    if all(
        data[field] is None
        for field in ("currency", "amount_capturable", "amount_received", "capture_method")
    ):
        for field in ("currency", "amount_capturable", "amount_received", "capture_method"):
            del data[field]
    elif not value.full_object:
        raise ValueError("invalid full payment object")
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def cursor_json(value: PaymentCursor) -> str:
    outcome_json(value.outcome)
    for text in (value.case_token, value.effect_id, value.authorization_ref, value.key):
        if not isinstance(text, str) or not text.strip():
            raise ValueError("blank payment anchor")
    if value.owner is not None and (not isinstance(value.owner, str) or not value.owner.strip()):
        raise ValueError("invalid payment owner")
    if value.kind not in {"capture", "release", "refund"} or value.call_kind not in {
        "capture",
        "release",
        "retrieve",
        "refund",
    }:
        raise ValueError("invalid payment kind")
    if value.call_kind != "retrieve" and value.call_kind != value.kind:
        raise ValueError("mismatched payment call")
    for number, minimum in (
        (value.revision, 1),
        (value.case_revision, 1),
        (value.failed_attempts, 0),
    ):
        if type(number) is not int or number < minimum:
            raise ValueError("invalid payment count")
    if (
        type(value.done) is not bool
        or type(value.amount_cents) is not int
        or type(value.outcome.amount_cents) is not int
        or not (
            1 <= value.amount_cents <= value.outcome.amount_cents
            if value.kind == "refund"
            else value.amount_cents == value.outcome.amount_cents
        )
    ):
        raise ValueError("invalid payment amount or completion")
    if value.outcome.authorization_ref != value.authorization_ref:
        raise ValueError("mismatched payment reference")
    for at in (value.first_at, value.next_at):
        if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("invalid payment time")
    if value.next_at < value.first_at or (value.done and value.owner is not None):
        raise ValueError("invalid payment timing/ownership")
    if type(value.contract_version) is not int or value.contract_version not in {1, 2, 3}:
        raise ValueError("invalid payment contract version")
    if value.contract_version == 1 and (
        value.mutation_key is not None or value.final_capture is not None
    ):
        raise ValueError("new parameters on legacy payment intent")
    if value.contract_version >= 2 and value.call_kind != "retrieve" and value.mutation_key is None:
        raise ValueError("missing mutation key")
    if value.authorization_charge_ref is not None and (
        value.contract_version != 3
        or not isinstance(value.authorization_charge_ref, str)
        or not value.authorization_charge_ref.startswith("ch_")
        or not value.authorization_charge_ref[3:].isalnum()
    ):
        raise ValueError("invalid authorization charge anchor")
    if type(value.compensation_started) is not bool or (
        value.compensation_started
        and (
            value.kind != "release"
            or value.owner is not None
            or value.done
            or value.outcome.status != "succeeded"
            or value.outcome.evidence_version != 1
            or not value.outcome.full_object
        )
    ):
        raise ValueError("invalid compensation reconciliation")
    if value.kind == "refund" and (
        value.contract_version != 3 or value.authorization_charge_ref is None
    ):
        raise ValueError("refund requires immutable original charge evidence")
    data = asdict(value)
    if not value.compensation_started:
        del data["compensation_started"]
    if value.authorization_charge_ref is None:
        del data["authorization_charge_ref"]
    if value.contract_version == 1:
        del data["contract_version"]
    if value.final_capture is not None and (
        value.kind != "capture" or value.final_capture is not True
    ):
        raise ValueError("invalid final capture parameter")
    if (
        value.mutation_key is not None
        and value.kind == "capture"
        and value.final_capture is not True
    ):
        raise ValueError("missing final capture parameter")
    data["outcome"] = json.loads(outcome_json(value.outcome))
    if value.final_capture is None:
        del data["final_capture"]
    if value.mutation_key is None:
        del data["mutation_key"]
    elif not isinstance(value.mutation_key, str) or not value.mutation_key.strip():
        raise ValueError("invalid mutation key")
    data["first_at"] = value.first_at.isoformat()
    data["next_at"] = value.next_at.isoformat()
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def decode_cursor(text: str) -> PaymentCursor:
    data = json.loads(text)
    data["first_at"] = datetime.fromisoformat(data["first_at"])
    data["next_at"] = datetime.fromisoformat(data["next_at"])
    if "refunds" in data["outcome"]:
        data["outcome"]["refunds"] = tuple(RefundEvidence(**r) for r in data["outcome"]["refunds"])
    data["outcome"] = PaymentOutcome(**data["outcome"])
    result = PaymentCursor(**data)
    if cursor_json(result) != text:
        raise ValueError("noncanonical payment cursor")
    return result


def mutation_key(namespace: str, case_id: str, kind: str, operation_id: str) -> str:
    """Non-PII key, unambiguously derived from the immutable durable intent."""
    encoded = json.dumps([namespace, case_id, kind, operation_id], separators=(",", ":"))
    return "pb-payment-" + hashlib.sha256(encoded.encode()).hexdigest()
