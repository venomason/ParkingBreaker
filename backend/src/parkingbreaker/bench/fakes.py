"""Synthetic offline bench seams; no answer-key expectations become model answers."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import date

from parkingbreaker.bench.cases import KeyRow
from parkingbreaker.json_types import JsonValue
from parkingbreaker.llm import Usage
from parkingbreaker.research.fakes import FakeFetcher, FakeLlmClient, FakeSearcher, html_response
from parkingbreaker.research.jurisdiction import DotGovRecord, DotGovSnapshot
from parkingbreaker.research.search import SearchHit


class BenchLlm(FakeLlmClient):
    def complete_json(
        self,
        system: str,
        user: str,
        schema: Mapping[str, JsonValue],
        *,
        max_cost_cents: int | None = None,
    ) -> tuple[JsonValue, Usage]:
        properties = schema.get("properties")
        if isinstance(properties, dict) and "findings" in properties:
            self.reply = {"findings": []}
        return super().complete_json(system, user, schema, max_cost_cents=max_cost_cents)


def fake_seams(
    row: KeyRow, today: date
) -> tuple[FakeFetcher, FakeSearcher, BenchLlm, DotGovSnapshot]:
    notice = row.notice
    url = "https://bench.gov/"
    quote = (
        f"Synthetic fixture: {notice.printed_issuer}, {notice.customer_state} government. "
        f"{notice.printed_issuer} decides contests of its citations."
    )
    body = f"<p>{quote}</p>"
    sha = hashlib.sha256(body.encode()).hexdigest()
    evidence: JsonValue = {"verbatim_quote": quote, "source_sha256": sha}
    client = BenchLlm(
        {
            "candidate_index": 1,
            "issuer_name": notice.printed_issuer,
            "issuer_type": "MUNICIPAL",
            "deciding_body": notice.printed_issuer,
            "violation_state": notice.customer_state,
            "is_private_operator": False,
            "conflicted": False,
            "issuer_evidence": evidence,
            "issuer_type_evidence": evidence,
            "deciding_body_evidence": evidence,
        }
    )
    snapshot = DotGovSnapshot(
        today,
        (
            DotGovRecord(
                "bench.gov",
                "City",
                notice.printed_issuer,
                None,
                notice.customer_city,
                notice.customer_state,
            ),
        ),
        {},
        hashlib.sha256(b"synthetic offline bench snapshot").hexdigest(),
    )
    return (
        FakeFetcher({url: html_response(url, body)}),
        FakeSearcher(lambda _: (SearchHit(url),)),
        client,
        snapshot,
    )
