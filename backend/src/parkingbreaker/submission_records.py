"""Submission cursor values and strict durable outcome codec. No I/O."""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from parkingbreaker.lob_submission import (
    NON_DEFINITIVE_4XX,
    Accepted,
    Canceled,
    LetterEvidence,
    NotSent,
    Rejected,
    SubmissionOutcome,
    Uncertain,
    UspsScan,
)


@dataclass(frozen=True)
class SubmissionCursor:
    case_token: str
    attempt: int
    first_sent_at: datetime
    sends: int
    next_at: datetime
    lookup_only: bool
    revision: int
    owner: str | None
    case_revision: int
    outcome: SubmissionOutcome


def _scan_json(scan: UspsScan) -> dict[str, object]:
    if not isinstance(scan.event_type, str) or not scan.event_type.strip():
        raise ValueError("invalid scan event type")
    for value in (scan.name, scan.time):
        if value is not None and not isinstance(value, str):
            raise ValueError("invalid scan field")
    if scan.details is not None and not isinstance(scan.details, dict):
        raise ValueError("invalid scan details")
    return scan.as_sent()


def _decode_scans(value: object) -> tuple[UspsScan, ...]:
    """Scan proof is provider data kept as sent; anything else fails closed."""
    if not isinstance(value, list):
        raise ValueError("invalid scan proof")
    scans: list[UspsScan] = []
    for record in value:
        if not isinstance(record, dict):
            raise ValueError("invalid scan record")
        allowed = {"type", "name", "time", "details"}
        if any(key not in allowed for key in record) or not isinstance(record.get("type"), str):
            raise ValueError("invalid scan record")
        name = record.get("name")
        time = record.get("time")
        details = record.get("details")
        if (name is not None and not isinstance(name, str)) or (
            time is not None and not isinstance(time, str)
        ):
            raise ValueError("invalid scan record")
        if details is not None and not isinstance(details, dict):
            raise ValueError("invalid scan details")
        scans.append(
            UspsScan(
                record["type"],
                name,
                time,
                None if details is None else cast("dict[str, object]", details),
            )
        )
    return tuple(scans)


def outcome_json(outcome: SubmissionOutcome) -> str:
    data: dict[str, object]
    match outcome:
        case Accepted(letter_id):
            if not isinstance(letter_id, str) or not letter_id.strip():
                raise ValueError("blank letter id")
            data = {"kind": "accepted", "letter_id": letter_id}
            if outcome.evidence is not None:
                if outcome.evidence not in {
                    LetterEvidence.PRODUCTION,
                    LetterEvidence.EXPIRY_FALLBACK,
                }:
                    raise ValueError("acceptance requires capture evidence")
                data["evidence"] = outcome.evidence.value
            if outcome.tracking_number is not None:
                data["tracking_number"] = outcome.tracking_number
            if outcome.scans:
                data["scans"] = [_scan_json(scan) for scan in outcome.scans]
            if type(outcome.verified_scan_webhook) is not bool:
                raise ValueError("invalid scan webhook marker")
            if outcome.verified_scan_webhook:
                if outcome.evidence is not LetterEvidence.PRODUCTION:
                    raise ValueError("scan webhook requires scan acceptance")
                data["verified_scan_webhook"] = True
        case Rejected(status, reason):
            if (
                type(status) is not int
                or not 400 <= status < 500
                or status in NON_DEFINITIVE_4XX
                or not isinstance(reason, str)
            ):
                raise ValueError("invalid rejection")
            data = {"kind": "rejected", "status": status, "reason": reason}
        case Uncertain(reason):
            if not isinstance(reason, str):
                raise ValueError("invalid uncertainty")
            data = {"kind": "uncertain", "reason": reason}
            if outcome.letter_id is not None:
                if not isinstance(outcome.letter_id, str) or not outcome.letter_id.strip():
                    raise ValueError("blank letter id")
                data["letter_id"] = outcome.letter_id
            if type(outcome.rendered) is not bool or (outcome.rendered and not outcome.letter_id):
                raise ValueError("invalid rendered record")
            if outcome.rendered:
                data["rendered"] = True
            if outcome.tracking_number is not None:
                if not isinstance(outcome.tracking_number, str) or not outcome.letter_id:
                    raise ValueError("invalid tracking number")
                data["tracking_number"] = outcome.tracking_number
        case NotSent():
            data = {"kind": "not_sent"}
        case Canceled(letter_id):
            if not isinstance(letter_id, str) or not letter_id.strip():
                raise ValueError("blank letter id")
            data = {"kind": "canceled", "letter_id": letter_id}
        case _:
            raise ValueError("unknown submission outcome")
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def decode_outcome(text: str) -> SubmissionOutcome:
    data = json.loads(text)
    match data:
        case {"kind": "accepted", "letter_id": letter_id}:
            evidence = data.get("evidence")
            result: SubmissionOutcome = Accepted(
                letter_id,
                data.get("tracking_number"),
                None if evidence is None else LetterEvidence(evidence),
                scans=_decode_scans(data.get("scans", [])),
                verified_scan_webhook=data.get("verified_scan_webhook", False),
            )
        case {"kind": "rejected", "status": status, "reason": reason}:
            result = Rejected(status, reason)
        case {"kind": "uncertain", "reason": reason}:
            result = Uncertain(
                reason,
                data.get("letter_id"),
                data.get("rendered", False),
                data.get("tracking_number"),
            )
        case {"kind": "not_sent"}:
            result = NotSent()
        case {"kind": "canceled", "letter_id": letter_id}:
            result = Canceled(letter_id)
        case _:
            raise ValueError("unknown outcome")
    if outcome_json(result) != text:
        raise ValueError("noncanonical outcome")
    return result
