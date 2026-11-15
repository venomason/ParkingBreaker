"""Offline provider seams. Page bytes are supplied unchanged from saved test fixtures.

Missing policy in those fixtures stays unknown; fake mode never waives evidence gates.
"""

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import cast
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from parkingbreaker import prices
from parkingbreaker.agencies import LobVerification, LobVerificationStatus, MailingAddress
from parkingbreaker.json_types import JsonValue
from parkingbreaker.llm import ImageAttachment, Usage
from parkingbreaker.lob_submission import (
    CancelOutcome,
    LetterEvidence,
    LetterRequest,
    LobResponse,
    MetadataLookup,
)
from parkingbreaker.prices import CERTIFIED_CENTS
from parkingbreaker.research.fakes import FakeLlmClient
from parkingbreaker.research.procedure import SILENCE_KEYS, ProcedureAssessment, assessment_data
from parkingbreaker.research.sources import html_to_text

ROOT = "https://sfmta.gov/"
CONTEST = ROOT + "contest"
# Sentences that appear verbatim in tests/fixtures/sfmta_contest.html (a synthetic page).
DEADLINE_QUOTE = (
    "You may contest a parking citation within 21 calendar days of the citation issue date."
)
RECEIPT_QUOTE = "Contests sent by mail must be received by the SFMTA by the deadline."
ITEMS_QUOTE = "Include your citation number and a copy of any evidence you want reviewed."
WHERE_QUOTE = (
    "Contest by mail SFMTA Customer Service Center ATTN: Citation Review "
    "11 South Van Ness Avenue San Francisco, CA 94103"
)
DECLARATION_QUOTE = "A signed declaration is not required for a first-level review."


class FixtureLlm(FakeLlmClient):
    def __init__(self, html: str) -> None:
        super().__init__({})
        self.sha = hashlib.sha256(html.encode()).hexdigest()
        self.text = html_to_text(html)

    def complete_json(
        self,
        system: str,
        user: str,
        schema: Mapping[str, JsonValue],
        *,
        max_cost_cents: int | None = None,
    ) -> tuple[JsonValue, Usage]:
        properties = schema.get("properties", {})
        if isinstance(properties, dict) and "candidate_index" in properties:
            quote: JsonValue = {
                "verbatim_quote": "SFMTA Customer Service Center",
                "source_sha256": self.sha,
            }
            self.reply = {
                "candidate_index": 1,
                "issuer_name": "SFMTA",
                "issuer_type": "MUNICIPAL",
                "deciding_body": "SFMTA Customer Service Center",
                "violation_state": "CA",
                "is_private_operator": False,
                "conflicted": False,
                "issuer_evidence": quote,
                "issuer_type_evidence": quote,
                "deciding_body_evidence": {
                    "verbatim_quote": WHERE_QUOTE,
                    "source_sha256": self.sha,
                },
            }
        elif isinstance(properties, dict) and "links" in properties:
            self.reply = {"links": []}
        elif isinstance(properties, dict) and "sentences" in properties:
            notes = user.split("<<<NOTES>>>\n", 1)[-1].split("<<<END NOTES>>>", 1)[0]
            self.reply = {
                "sentences": [
                    {"text": match[3], "support": [f"note:{match[1]}-{match[2]}"], "paragraph": 0}
                    for match in re.finditer(r"\[(\d+)-(\d+)\] (.+)", notes)
                ]
            }
        else:
            deadline = DEADLINE_QUOTE
            # These are the only facts actually stated by the unchanged fixture.
            facts: tuple[tuple[str, JsonValue, str], ...] = (
                (
                    "MAIL_IN_ACCEPTED",
                    True,
                    RECEIPT_QUOTE,
                ),
                ("DEADLINE_DAYS", 21, deadline),
                ("DEADLINE_CLOCK_START", "ISSUE_DATE", deadline),
                (
                    "DEADLINE_RULE",
                    "RECEIPT",
                    RECEIPT_QUOTE,
                ),
                ("DEADLINE_ABSOLUTE", {"applicable": False, "date": None}, deadline),
                (
                    "MAILING_ADDRESS",
                    {
                        "recipient": "SFMTA Customer Service Center",
                        "attention": "ATTN: Citation Review",
                        "line1": "11 South Van Ness Avenue",
                        "line2": None,
                        "city": "San Francisco",
                        "state": "CA",
                        "zip_code": "94103",
                    },
                    self.text,
                ),
                (
                    "REQUIRED_ITEMS",
                    ["citation number", "copy of any evidence"],
                    ITEMS_QUOTE,
                ),
                (
                    "REQUIRED_DECLARATIONS",
                    [],
                    DECLARATION_QUOTE,
                ),
            )
            stated = {key for key, _value, _quote in facts}
            # The fixture is a complete first-level procedure: its silence on the other
            # rule keys is assessed against the whole saved page, as production requires.
            assessment = ProcedureAssessment(
                True,
                "CA:sfmta.gov",
                "first-level review",
                "parking citation",
                self.sha,
                self.text,
                self.text,
                RECEIPT_QUOTE,
                WHERE_QUOTE,
                DEADLINE_QUOTE,
                "The page gives written mail-contest instructions, a destination and a deadline.",
            )
            findings: list[JsonValue] = []
            for key, value, quote in facts:
                findings.append(
                    {
                        "key": key,
                        "value": value,
                        "status": "CONFIRMED",
                        "verbatim_quote": quote,
                        "source_sha256s": [self.sha],
                    }
                )
            for silent in sorted(SILENCE_KEYS):
                if silent.value in stated:
                    continue
                findings.append(
                    {
                        "key": silent.value,
                        "value": [],
                        "status": "CONFIRMED",
                        "verbatim_quote": self.text,
                        "source_sha256s": [self.sha],
                        "evidence_basis": "NONE_STATED_IN_OFFICIAL_PROCEDURE",
                        "procedure_assessment": cast(JsonValue, assessment_data(assessment)),
                    }
                )
            self.reply = {"findings": findings}
        return super().complete_json(system, user, schema, max_cost_cents=max_cost_cents)


class FakeStripeTransport:
    """Stateful HTTP fake used through the real Stripe SDK boundary; never networks."""

    def __init__(self) -> None:
        self.objects: dict[str, dict[str, object]] = {}
        self.keys: dict[str, str] = {}
        self.refunds: dict[str, dict[str, object]] = {}
        self.refund_keys: dict[str, str] = {}

    def request(
        self, method: str, url: str, headers: Mapping[str, str] | None, post_data: object = None
    ) -> tuple[str, int, Mapping[str, str]]:
        path = urlsplit(url).path
        parameters = parse_qs(post_data if isinstance(post_data, str) else urlsplit(url).query)
        key = (headers or {}).get("Idempotency-Key", "")
        if path.endswith("/refunds"):
            charge_ref = parameters.get("charge", [""])[0]
            if method.lower() == "get":
                return (
                    json.dumps(
                        {
                            "object": "list",
                            "has_more": False,
                            "data": [r for r in self.refunds.values() if r["charge"] == charge_ref],
                        }
                    ),
                    200,
                    {},
                )
            original = next(
                (
                    obj
                    for obj in self.objects.values()
                    if isinstance(obj["latest_charge"], dict)
                    and obj["latest_charge"]["id"] == charge_ref
                ),
                None,
            )
            amount = int(parameters.get("amount", ["0"])[0])
            if (
                original is None
                or original["status"] != "succeeded"
                or type(original["amount"]) is not int
                or original["amount"] not in prices.ALLOWED_PRICE_CENTS
                or not 1 <= amount <= original["amount"]
            ):
                return "{}", 400, {}
            refund_ref = self.refund_keys.get(key)
            if refund_ref is None:
                refunded = sum(
                    int(str(r["amount"]))
                    for r in self.refunds.values()
                    if r["charge"] == charge_ref
                )
                if refunded + amount > original["amount"]:
                    return "{}", 400, {}
                refund_ref = "re_" + uuid4().hex
                self.refund_keys[key] = refund_ref
                self.refunds[refund_ref] = {
                    "id": refund_ref,
                    "object": "refund",
                    "payment_intent": original["id"],
                    "charge": charge_ref,
                    "amount": amount,
                    "currency": "usd",
                    "status": "succeeded",
                }
            refund = self.refunds[refund_ref]
            if refund["amount"] != amount or refund["charge"] != charge_ref:
                return "{}", 409, {}
            return json.dumps(refund), 200, {}
        if path.endswith("/payment_intents") and method.lower() == "post":
            ref = self.keys.get(key, "pi_" + uuid4().hex)
            self.keys[key] = ref
            if ref not in self.objects:
                amount = int(parameters.get("amount", [str(CERTIFIED_CENTS)])[0])
                if amount not in prices.ALLOWED_PRICE_CENTS:
                    return "{}", 400, {}
                self.objects[ref] = {
                    "id": ref,
                    "object": "payment_intent",
                    "status": "requires_capture",
                    "amount": amount,
                    "currency": "usd",
                    "capture_method": "manual",
                    "amount_capturable": amount,
                    "amount_received": 0,
                    "latest_charge": {
                        "id": "ch_" + uuid4().hex,
                        "object": "charge",
                        "payment_intent": ref,
                        "amount": amount,
                        "currency": "usd",
                        "amount_captured": 0,
                        "captured": False,
                        "paid": True,
                        "status": "succeeded",
                        "created": int(datetime.now(UTC).timestamp()),
                        "payment_method_details": {
                            "type": "card",
                            "card": {
                                "capture_before": int(
                                    (datetime.now(UTC) + timedelta(days=5)).timestamp()
                                )
                            },
                        },
                    },
                }
        else:
            parts = path.split("/")
            ref = parts[-2] if parts[-1] in {"capture", "cancel"} else parts[-1]
            if ref not in self.objects:
                return "{}", 404, {}
            obj = self.objects[ref]
            if method.lower() == "post" and path.endswith("/capture"):
                if (
                    obj["amount"] not in prices.ALLOWED_PRICE_CENTS
                    or int(parameters.get("amount_to_capture", ["0"])[0]) != obj["amount"]
                    or parameters.get("final_capture") != ["true"]
                ):
                    return "{}", 400, {}
                obj.update(status="succeeded", amount_received=obj["amount"], amount_capturable=0)
                charge = obj["latest_charge"]
                assert isinstance(charge, dict)
                charge.update(captured=True, amount_captured=obj["amount"])
            elif method.lower() == "post" and path.endswith("/cancel"):
                obj.update(status="canceled", amount_capturable=0)
        return json.dumps(self.objects[ref]), 200, {}


class AcceptingLobClient:
    """Offline Lob: accepts every letter once per key, with a certified tracking number."""

    def __init__(self) -> None:
        self.by_key: dict[str, str] = {}
        self.by_token: dict[str, str] = {}

    def create_letter(self, request: LetterRequest, idempotency_key: str) -> LobResponse:
        letter = self.by_key.get(idempotency_key)
        if letter is None:
            letter = "ltr_" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:24]
            self.by_key[idempotency_key] = letter
            self.by_token[request.submission_token] = letter
        tracking = "9407" + hashlib.sha256(letter.encode()).hexdigest()[:18].upper()
        return LobResponse(
            200, letter, tracking_number=tracking, evidence=LetterEvidence.PRODUCTION
        )

    def find_by_metadata(self, submission_token: str) -> MetadataLookup:
        return MetadataLookup(
            self.by_token.get(submission_token),
            authoritative=True,
            evidence=LetterEvidence.PRODUCTION,
        )

    def lookup_letter(self, letter_id: str, submission_token: str) -> MetadataLookup:
        if self.by_token.get(submission_token) != letter_id:
            raise RuntimeError("unknown letter")
        return MetadataLookup(letter_id, authoritative=True, evidence=LetterEvidence.PRODUCTION)

    def cancel_letter(self, letter_id: str) -> CancelOutcome:
        # Every letter this offline fake accepts is already in the mail stream.
        return CancelOutcome.NOT_CANCELABLE

    def verify_address(self, address: MailingAddress) -> LobVerification:
        return LobVerification(LobVerificationStatus.DELIVERABLE)


SYNTHETIC_VISION_MODEL = "synthetic-vision-fixture"


class SyntheticVisionLlm:
    """Offline synthetic multimodal fake for preview acceptance.

    Its output is explicitly synthetic and never a provider reading, comparison or
    verified fact: every field is reported not shown, so the customer must explicitly
    resolve each absence. It spends nothing and never networks.
    """

    def complete_json(
        self,
        system: str,
        user: str,
        schema: Mapping[str, JsonValue],
        *,
        max_cost_cents: int | None = None,
        images: Sequence[ImageAttachment] | None = None,
    ) -> tuple[JsonValue, Usage]:
        properties = schema.get("properties")
        if not isinstance(properties, dict) or not properties or not images:
            raise ValueError("synthetic vision needs images and a closed field schema")
        reading = False
        for entry in properties.values():
            if isinstance(entry, dict):
                required = entry.get("required")
                if isinstance(required, list) and "state" in required:
                    reading = True
                break
        payload: dict[str, JsonValue] = {}
        for name in properties:
            if reading:
                payload[name] = {"state": "not_shown", "value": None}
            else:
                payload[name] = {"comparison": "not_shown", "visible_text": None}
        return payload, Usage()
