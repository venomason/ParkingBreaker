"""Original-object fake with Stripe v1 response caching, including failed 500s."""

from collections.abc import Mapping
from dataclasses import replace

from parkingbreaker import prices
from parkingbreaker.payment_records import PaymentOutcome, RefundEvidence


def full_outcome(value: PaymentOutcome) -> PaymentOutcome:
    """Supply fake object evidence for a scripted status (never provider evidence)."""
    amount = value.amount_cents
    return replace(
        value,
        amount_cents=amount,
        currency="usd",
        capture_method="manual",
        amount_capturable=amount if value.status == "requires_capture" else 0,
        amount_received=amount if value.status == "succeeded" else 0,
    )


class FakePaymentClient:
    authorization_evidence_required = False

    def __init__(self, authorization_ref: str = "auth") -> None:
        self.objects: dict[str, PaymentOutcome] = {}
        self._refunds: dict[str, tuple[RefundEvidence, ...]] = {}
        self.object = full_outcome(PaymentOutcome("requires_capture", authorization_ref))
        self.calls: list[tuple[str, str, int, str]] = []
        self.responses: list[PaymentOutcome] = []
        self.captures = 0
        self.releases = 0
        self.refund_calls = 0
        self.refunds: tuple[RefundEvidence, ...] = ()
        self.refund_responses: list[PaymentOutcome] = []
        self.cache: dict[
            str,
            tuple[tuple[str, str, int, bool] | tuple[str, str, str, int, bool], PaymentOutcome],
        ] = {}
        self.authorizations: dict[str, tuple[int, str, dict[str, str], PaymentOutcome]] = {}

    def authorize(
        self, amount_cents: int, payment_method_id: str, key: str, metadata: Mapping[str, str]
    ) -> PaymentOutcome:
        if type(amount_cents) is not int or amount_cents not in prices.ALLOWED_PRICE_CENTS:
            raise ValueError("full service authorization required")
        if not payment_method_id.startswith("pm_") or not payment_method_id[3:]:
            raise ValueError("payment method id required")
        if not key.strip() or len(key) > 255:
            raise ValueError("valid mutation key required")
        self.calls.append(("authorize", payment_method_id, amount_cents, key))
        parameters = (amount_cents, payment_method_id, dict(metadata))
        if key in self.cache:
            raise ValueError("idempotency parameters changed")
        if key in self.authorizations:
            previous = self.authorizations[key]
            if previous[:3] != parameters:
                raise ValueError("idempotency parameters changed")
            return previous[3]
        ref = self.object.authorization_ref
        if any(a[3].authorization_ref == ref for a in self.authorizations.values()):
            ordinal = len(self.authorizations)
            while f"pi_Fake{ordinal}" in self.objects:
                ordinal += 1
            ref = f"pi_Fake{ordinal}"
        self.object = full_outcome(PaymentOutcome("requires_capture", ref, amount_cents))
        self.authorizations[key] = (*parameters, self.object)
        return self.object

    @property
    def object(self) -> PaymentOutcome:
        return self._object

    @object.setter
    def object(self, value: PaymentOutcome) -> None:
        self._object = value
        self.objects[value.authorization_ref] = value

    @property
    def refunds(self) -> tuple[RefundEvidence, ...]:
        return self._refunds.get(self.object.authorization_ref, ())

    @refunds.setter
    def refunds(self, value: tuple[RefundEvidence, ...]) -> None:
        self._refunds[self.object.authorization_ref] = value

    def _select(self, authorization_ref: str) -> None:
        if authorization_ref in self.objects and any(
            a[3].authorization_ref == authorization_ref for a in self.authorizations.values()
        ):
            self._object = self.objects[authorization_ref]

    def retrieve(self, authorization_ref: str) -> PaymentOutcome:
        self._select(authorization_ref)
        self.calls.append(("retrieve", authorization_ref, self.object.amount_cents, ""))
        if authorization_ref != self.object.authorization_ref:
            raise ValueError("original reference mismatch")
        return self.object

    def capture(
        self, authorization_ref: str, amount_cents: int, key: str, *, final_capture: bool
    ) -> PaymentOutcome:
        self._select(authorization_ref)
        if (
            type(amount_cents) is not int
            or amount_cents not in prices.ALLOWED_PRICE_CENTS
            or amount_cents != self.object.amount_cents
            or final_capture is not True
        ):
            raise ValueError("requires full final capture")
        return self._mutate("capture", authorization_ref, key)

    def release(self, authorization_ref: str, key: str) -> PaymentOutcome:
        return self._mutate("release", authorization_ref, key)

    def retrieve_refunds(self, authorization_ref: str, charge_ref: str) -> PaymentOutcome:
        self._select(authorization_ref)
        self.calls.append(("retrieve_refunds", authorization_ref, self.object.amount_cents, ""))
        if (
            self.object.authorization_ref != authorization_ref
            or self.object.charge_ref != charge_ref
        ):
            raise ValueError("original refund target mismatch")
        return replace(self.object, refunds=self.refunds)

    def refund(
        self,
        authorization_ref: str,
        charge_ref: str,
        amount_cents: int,
        key: str,
        *,
        captured_cents: int,
    ) -> PaymentOutcome:
        self._select(authorization_ref)
        if (
            type(amount_cents) is not int
            or type(captured_cents) is not int
            or captured_cents not in prices.ALLOWED_PRICE_CENTS
            or captured_cents != self.object.amount_cents
            or not 1 <= amount_cents <= captured_cents
            or self.object.status != "succeeded"
            or not self.object.full_object
            or self.object.charge_ref != charge_ref
            or self.object.authorization_ref != authorization_ref
        ):
            raise ValueError("original full refund required")
        if not isinstance(key, str) or not key.strip() or len(key) > 255:
            raise ValueError("valid mutation key required")
        self.calls.append(("refund", authorization_ref, amount_cents, key))
        parameters = ("refund", authorization_ref, charge_ref, amount_cents, True)
        if key in self.authorizations:
            raise ValueError("idempotency parameters changed")
        if key in self.cache:
            previous, response = self.cache[key]
            if previous != parameters:
                raise ValueError("idempotency parameters changed")
            return response
        if self.refund_responses:
            response = self.refund_responses.pop(0)
        elif (
            amount_cents
            + sum(r.amount_cents for r in self.refunds if r.status not in {"failed", "canceled"})
            > captured_cents
        ):
            response = PaymentOutcome("uncertain", authorization_ref, captured_cents)
        else:
            refund = RefundEvidence(
                f"re_Fake{self.refund_calls}",
                authorization_ref,
                charge_ref,
                amount_cents,
                "usd",
                "succeeded",
            )
            self.refunds += (refund,)
            response = replace(self.object, refunds=(refund,))
            self.refund_calls += 1
        self.cache[key] = parameters, response
        return response

    def _mutate(self, kind: str, authorization_ref: str, key: str) -> PaymentOutcome:
        self._select(authorization_ref)
        if self.object.amount_cents not in prices.ALLOWED_PRICE_CENTS:
            raise ValueError("requires allowed original amount")
        if authorization_ref != self.object.authorization_ref:
            raise ValueError("original reference mismatch")
        if self.object.status == "requires_capture" and not self.object.full_object:
            raise ValueError("requires full original object")
        self.calls.append((kind, authorization_ref, self.object.amount_cents, key))
        parameters = (kind, authorization_ref, self.object.amount_cents, True)
        if key in self.authorizations:
            raise ValueError("idempotency parameters changed")
        if key in self.cache:
            previous, response = self.cache[key]
            if parameters != previous:
                raise ValueError("idempotency parameters changed")
            return response
        if self.object.status != "requires_capture":
            raise ValueError(f"cannot {kind} PaymentIntent in {self.object.status} state")
        if self.responses:
            result = self.responses.pop(0)
            if result.status not in {"uncertain", "transient"}:
                result = full_outcome(result)
        else:
            result = full_outcome(
                PaymentOutcome(
                    "succeeded", authorization_ref, self.object.amount_cents, "charge-original"
                )
                if kind == "capture"
                else PaymentOutcome("canceled", authorization_ref, self.object.amount_cents)
            )
        self.cache[key] = parameters, result
        if result.status in {"succeeded", "expired", "declined", "canceled"}:
            if self.object.status == "requires_capture":
                self.captures += int(result.status == "succeeded")
                self.releases += int(kind == "release" and result.status == "canceled")
            self.object = result
        return result
