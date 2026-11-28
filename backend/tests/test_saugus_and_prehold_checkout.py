"""Saugus FAQ + official PDF form and CustomerCheckout pre-hold regressions."""

from __future__ import annotations

import base64
import hashlib
import socket
import sqlite3
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from httpx2 import ASGITransport, AsyncClient

from parkingbreaker.api.app import create_app
from parkingbreaker.case_service import CaseServiceDeps, ResearchDeps
from parkingbreaker.case_state import CaseState, HardStop, StopFamily
from parkingbreaker.config import ProviderMode, Settings, build_services
from parkingbreaker.customer_checkout import CustomerCheckout
from parkingbreaker.json_types import JsonValue
from parkingbreaker.lob_fakes import FakeLobClient
from parkingbreaker.lob_submission import LetterEvidence, LobResponse
from parkingbreaker.payment_fakes import FakePaymentClient
from parkingbreaker.payment_records import PaymentOutcome
from parkingbreaker.payment_state import PaymentState
from parkingbreaker.research.extractor import LlmExtractor, parse_findings
from parkingbreaker.research.fakes import FakeFetcher, FakeLlmClient, FakeSearcher, html_response
from parkingbreaker.research.findings import FindingStatus, apply_quote_guard
from parkingbreaker.research.issuer_assessment import LlmIssuerAssessor
from parkingbreaker.research.jurisdiction import DotGovRecord, DotGovSnapshot
from parkingbreaker.research.link_assessment import LlmLinkAssessor
from parkingbreaker.research.payment_effects import (
    PaymentAction,
    PaymentCondition,
    PaymentEffect,
)
from parkingbreaker.research.pdf_text import extract_pdf_text
from parkingbreaker.research.procedure import (
    SILENCE_KEYS,
    ProcedureAssessment,
    assessment_data,
    procedure_sections,
    quote_procedure_stage,
)
from parkingbreaker.research.questions import FactKey
from parkingbreaker.research.search import SearchHit
from parkingbreaker.research.sources import (
    AuthorityClass,
    FetchResponse,
    SourceRecord,
    html_to_text,
)
from parkingbreaker.research.support import value_supported_by_quote
from parkingbreaker.transaction_store import SqliteTransactionStore
from test_beta_admission import campaign
from test_case_service import ALICE, PNG_B64, WORKER

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda _host, port, *_a, **_k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 443))
        ],
    )


ROOT = Path(__file__).resolve().parents[2]
BENCH_DIR = ROOT / "build-kit/evidence/bench-pages"

SAUGUS_FAQ_SHA = "c514380fb9ded25e85a2849c6a4b020176be9759dfee887139b1c64f78488279"
SAUGUS_FORM_SHA = "aa33522df2ed4d5ee26e399f097178d538368c416d6814a5a717f013aa15c70c"
WEST_COVINA_SHA = "4ed8fdadd2f54ad12cb6602bae405ff7e16ac4656a40ffe14064c67654f9f06e"

SAUGUS_URL = "https://www.saugus-ma.gov/m/faq?cat=21"
SAUGUS_FORM_LINK = "https://www.saugus-ma.gov/DocumentCenter/View/1497"
SAUGUS_FORM_URL = (
    "https://www.saugus-ma.gov/DocumentCenter/View/1497/town_of_saugus_parking_appeal_form_0?bidId="
)
WEST_COVINA_URL = "https://www.westcovina.gov/m/faq?cat=19"

NOW = datetime(2026, 11, 27, 18, 0, tzinfo=UTC)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _saugus_sources() -> tuple[bytes, str, bytes]:
    faq_raw = (BENCH_DIR / SAUGUS_FAQ_SHA).read_bytes()
    form_raw = (BENCH_DIR / SAUGUS_FORM_SHA).read_bytes()
    assert hashlib.sha256(faq_raw).hexdigest() == SAUGUS_FAQ_SHA
    assert hashlib.sha256(form_raw).hexdigest() == SAUGUS_FORM_SHA
    return faq_raw, html_to_text(faq_raw.decode()), form_raw


def _saugus_model_reply(
    doc: str, *, faq_sha: str = SAUGUS_FAQ_SHA
) -> tuple[dict[str, JsonValue], str]:
    authority = "MA:saugus-ma.gov"
    how_start = doc.index("You can mail your appeal letter")
    how = doc[how_start : doc.index("01906.", how_start) + len("01906.")]
    timing = "Your appeal must be received within 21 days of the ticket date."
    scope_start = doc.index("2 options for parking ticket appeals as shown in the following")
    scope = doc[scope_start : doc.index("Forms are available", scope_start)].strip()
    where: dict[str, JsonValue] = {
        "recipient": "Town of Saugus",
        "attention": "Parking Hearings Officer",
        "line1": "298 Central Street",
        "line2": "Suite 5",
        "city": "Saugus",
        "state": "MA",
        "zip_code": "01906",
    }
    claims: list[tuple[str, JsonValue, str]] = [
        ("MAIL_IN_ACCEPTED", True, how),
        ("MAILING_ADDRESS", where, how),
        ("DEADLINE_DAYS", 21, timing),
        ("DEADLINE_CLOCK_START", "ISSUE_DATE", timing),
        ("DEADLINE_RULE", "RECEIPT", timing),
        ("DEADLINE_ABSOLUTE", {"applicable": False, "date": None}, timing),
        ("REQUIRED_ITEMS", ["copy of your ticket"], how),
        ("TICKET_SPECIFIC_REQUIREMENTS", ["copy of your HP placard (if applicable)"], how),
    ]
    assessment = ProcedureAssessment(
        True,
        authority,
        "first-level review",
        "parking citation",
        faq_sha,
        doc,
        scope,
        how,
        how,
        timing,
        "Saugus publishes two initial appeal options and links its optional appeal form.",
    )
    findings: list[JsonValue] = [
        {
            "key": key,
            "value": val,
            "status": "CONFIRMED",
            "source_sha256s": [faq_sha],
            "verbatim_quote": quote,
            "stage": "first-level review",
            "jurisdiction": authority,
        }
        for key, val, quote in claims
    ]
    claimed_keys = {row[0] for row in claims}
    for silence_key in sorted(SILENCE_KEYS, key=lambda item: item.value):
        if silence_key.value not in claimed_keys:
            findings.append(
                {
                    "key": silence_key.value,
                    "value": [],
                    "status": "CONFIRMED",
                    "verbatim_quote": scope,
                    "source_sha256s": [faq_sha],
                    "stage": "first-level review",
                    "jurisdiction": authority,
                    "evidence_basis": "NONE_STATED_IN_OFFICIAL_PROCEDURE",
                    "procedure_assessment": cast(dict[str, JsonValue], assessment_data(assessment)),
                }
            )
    return {"payment_rules": [], "findings": findings}, how


def _build_saugus_checkout(
    tmp_path: Path,
    *,
    clock_ref: list[datetime] | None = None,
    paid_cohort_cap: int | None = None,
    city: str = "Saugus",
    payment_rule_quote: str | None = None,
) -> tuple[CustomerCheckout, FakePaymentClient, FakeLobClient]:
    now_box = clock_ref if clock_ref is not None else [NOW]
    settings = Settings(
        ProviderMode.FAKE,
        tmp_path / "data",
        "worker-secret-0123456789",
        {"PB_PAID_COHORT_CAP": str(paid_cohort_cap)} if paid_cohort_cap is not None else {},
    )
    base_svc = build_services(settings)
    assert isinstance(base_svc, CustomerCheckout)
    assert base_svc._deps.prehold is not None
    payments = FakePaymentClient()
    lob = FakeLobClient(
        script=[
            LobResponse(status=200, letter_id="ltr_saugus_01", evidence=LetterEvidence.PRODUCTION)
        ]
    )

    urls: tuple[str, ...]
    if city == "Saugus":
        faq_raw, doc, form_raw = _saugus_sources()
        faq_sha = SAUGUS_FAQ_SHA
        reply, how = _saugus_model_reply(doc, faq_sha=faq_sha)
        extra_responses: dict[str, FetchResponse] = {}
        extra_urls: tuple[str, ...] = ()
        if payment_rule_quote:
            policy_url = "https://www.saugus-ma.gov/parking/conditions"
            policy_html = (
                "<h1>Saugus MA Parking Hearings Officer Initial Review.</h1>"
                f"<p>{payment_rule_quote}</p>"
            )
            policy_sha = hashlib.sha256(policy_html.encode()).hexdigest()
            extra_responses[policy_url] = html_response(policy_url, policy_html)
            extra_urls = (policy_url,)
            reply["payment_rules"] = [
                {
                    "action": PaymentAction.FINE_PAYMENT.value,
                    "effect": PaymentEffect.EXPLICIT_REVIEW_BAR.value,
                    "condition": PaymentCondition.ACTION_OCCURRED.value,
                    "scope": {
                        "issuer": "MA:saugus-ma.gov",
                        "deciding_body": "Parking Hearings Officer",
                        "stage": "first-level review",
                    },
                    "verbatim_quote": payment_rule_quote,
                    "scope_quote": html_to_text(policy_html),
                    "source_sha256s": [policy_sha],
                    "status": "CONFIRMED",
                }
            ]
        fetcher = FakeFetcher(
            {
                SAUGUS_URL: html_response(SAUGUS_URL, faq_raw.decode()),
                SAUGUS_FORM_LINK: FetchResponse(
                    SAUGUS_FORM_LINK, 302, {"location": SAUGUS_FORM_URL}, b""
                ),
                SAUGUS_FORM_URL: FetchResponse(
                    SAUGUS_FORM_URL, 200, {"content-type": "application/pdf"}, form_raw
                ),
                **extra_responses,
            }
        )
        urls = (SAUGUS_URL, SAUGUS_FORM_LINK, *extra_urls)
        snapshot = DotGovSnapshot(
            snapshot_date=date(2026, 11, 26),
            records=(
                DotGovRecord(
                    domain="saugus-ma.gov",
                    domain_type="City",
                    organization="Town of Saugus",
                    suborganization=None,
                    city="Saugus",
                    state="MA",
                ),
            ),
            skipped_counts={},
            content_sha256="a" * 64,
        )
        issuer_reply: JsonValue = {
            "candidate_index": 1,
            "issuer_name": "Town of Saugus",
            "issuer_type": "MUNICIPAL",
            "deciding_body": "Parking Hearings Officer",
            "violation_state": "MA",
            "is_private_operator": False,
            "conflicted": False,
            "issuer_evidence": {"verbatim_quote": "Town of Saugus", "source_sha256": faq_sha},
            "issuer_type_evidence": {"verbatim_quote": "Town of Saugus", "source_sha256": faq_sha},
            "deciding_body_evidence": {"verbatim_quote": how, "source_sha256": faq_sha},
        }
    else:
        wc_raw = (BENCH_DIR / WEST_COVINA_SHA).read_bytes()
        wc_doc = html_to_text(wc_raw.decode())
        fetcher = FakeFetcher({WEST_COVINA_URL: html_response(WEST_COVINA_URL, wc_raw.decode())})
        urls = (WEST_COVINA_URL,)
        snapshot = DotGovSnapshot(
            snapshot_date=date(2026, 11, 26),
            records=(
                DotGovRecord(
                    domain="westcovina.gov",
                    domain_type="City",
                    organization="City of West Covina",
                    suborganization=None,
                    city="West Covina",
                    state="CA",
                ),
            ),
            skipped_counts={},
            content_sha256="b" * 64,
        )
        how = wc_doc[
            wc_doc.index(
                "If you feel you have been issued a parking citation in error"
            ) : wc_doc.index("why you feel the citation was issued in error.")
            + len("why you feel the citation was issued in error.")
        ]
        passage = wc_doc[
            wc_doc.index("A review of the citation is made by") : wc_doc.index("employee.")
            + len("employee.")
        ]
        span = next(
            line
            for line in wc_doc.splitlines()
            if "Reviews must be requested in writing within 21 days of the violation" in line
        )
        issuer_reply = {
            "candidate_index": 1,
            "issuer_name": "City of West Covina",
            "issuer_type": "MUNICIPAL",
            "deciding_body": "City of West Covina Police Department",
            "violation_state": "CA",
            "is_private_operator": False,
            "conflicted": False,
            "issuer_evidence": {
                "verbatim_quote": "City of West Covina",
                "source_sha256": WEST_COVINA_SHA,
            },
            "issuer_type_evidence": {
                "verbatim_quote": "City of West Covina",
                "source_sha256": WEST_COVINA_SHA,
            },
            "deciding_body_evidence": {
                "verbatim_quote": passage,
                "source_sha256": WEST_COVINA_SHA,
            },
        }
        wc_claims: list[tuple[str, JsonValue, str]] = [
            ("MAIL_IN_ACCEPTED", True, how),
            (
                "MAILING_ADDRESS",
                {
                    "recipient": "City of West Covina",
                    "attention": "Adjudication Services",
                    "line1": "P.O. Box 2081",
                    "line2": None,
                    "city": "Tustin",
                    "state": "CA",
                    "zip_code": "92781-2081",
                },
                how,
            ),
            ("DEADLINE_DAYS", 21, span),
            ("DEADLINE_CLOCK_START", "ISSUE_DATE", span),
            ("DEADLINE_RULE", "POSTMARK", span),
        ]
        reply = {
            "payment_rules": [],
            "findings": [
                {
                    "key": k,
                    "value": v,
                    "status": "CONFIRMED",
                    "source_sha256s": [WEST_COVINA_SHA],
                    "verbatim_quote": q,
                    "stage": "first-level review",
                    "jurisdiction": "CA:westcovina.gov",
                }
                for k, v, q in wc_claims
            ],
        }

    search = FakeSearcher(lambda _: tuple(SearchHit(u) for u in urls))
    pre = replace(
        base_svc._deps.prehold,
        clock=lambda: now_box[0],
        snapshot=snapshot,
        fetcher=fetcher,
        searcher=search,
        issuer_assessor=LlmIssuerAssessor(FakeLlmClient(issuer_reply)),
        link_assessor=LlmLinkAssessor(FakeLlmClient({"links": []})),
        candidate_urls=urls,
    )
    research = ResearchDeps(
        fetcher=fetcher,
        extractor=LlmExtractor(FakeLlmClient(reply)),
        searcher=search,
        wait=lambda _: None,
        known_records=tuple({"status": "active", "online_appeal_url": u} for u in urls),
    )
    deps: CaseServiceDeps = replace(
        base_svc._deps,
        clock=lambda: now_box[0],
        payments=payments,
        lob=lob,
        prehold=pre,
        prehold_for_subject=None,
        research=research,
        research_for_subject=None,
        paid_cohort_cap=paid_cohort_cap,
    )
    return CustomerCheckout(deps), payments, lob


def _saugus_intake(
    photo_ref: str,
    *,
    citation_number: str = "SAU-2026-1127",
    violation_code: str = "METER",
    violation_description: str = "Meter expired",
    city: str = "Saugus",
) -> dict[str, JsonValue]:
    is_saugus = city == "Saugus"
    return {
        "rental_company_paid": "NOT_RENTAL",
        "ticket": {
            "citation_number": citation_number,
            "license_plate": "SIM273",
            "license_plate_state": "MA" if is_saugus else "CA",
            "issuer_hint": "Town of Saugus" if is_saugus else "City of West Covina",
            "issuer_kind": "GOVERNMENT",
            "issued_city": city,
            "issued_state": "MA" if is_saugus else "CA",
            "violation_code": violation_code,
            "violation_description": violation_description,
            "issue_date": "2026-11-27",
            "stage": "first-level review",
        },
        "customer_position": {
            "statement": "The meter display showed time remaining when I returned to my vehicle."
        },
        "contact": {
            "name": "Jordan Sample",
            "phone": "781-555-0114",
            "email": "jordan.saugus@example.test",
            "mailing_address": {
                "line1": "10 Central Street",
                "line2": "",
                "city": "Saugus",
                "state": "MA",
                "zip_code": "01906",
            },
        },
        "citation_photo_ref": photo_ref,
        "photo_refs": [],
    }


def test_saugus_faq_and_form_quote_guard_and_negative_controls() -> None:
    _faq_raw, faq_text, form_raw = _saugus_sources()
    form_text = extract_pdf_text(form_raw)
    assert form_text is not None
    assert "".join(s.text for s in procedure_sections(faq_text)) == faq_text
    assert "".join(s.text for s in procedure_sections(form_text)) == form_text

    # Payment-mixed sentence is never accepted as a contest mail route or contest address.
    mixed = (
        "In Writing - Mail Payments and Appeals to: Town of Saugus - "
        "298 Central Street, Suite 5, Saugus, MA 01906 - You can make a hand written appeal."
    )
    assert not value_supported_by_quote(FactKey.MAIL_IN_ACCEPTED, True, mixed)
    assert not value_supported_by_quote(
        FactKey.MAILING_ADDRESS,
        {
            "recipient": "Town of Saugus",
            "attention": None,
            "line1": "298 Central Street",
            "line2": "Suite 5",
            "city": "Saugus",
            "state": "MA",
            "zip_code": "01906",
        },
        mixed,
    )

    # Post-decision Essex Superior Court Judicial Review blocks stay out of first-level review.
    court_faq = (
        "If you are not satisfied with your appeal result, you must request a Judicial Court "
        "Review at the Essex Superior Court to further appeal."
    )
    court_form = "Once this has been reviewed, and a judgement issued"
    assert quote_procedure_stage(faq_text, court_faq) == "administrative hearing"
    assert quote_procedure_stage(form_text, court_form) == "administrative hearing"

    # Without the linked OFFICIAL_FORM fetched, the multi-topic FAQ cannot establish silence.
    faq_source = SourceRecord(
        SAUGUS_URL,
        SAUGUS_URL,
        NOW,
        200,
        SAUGUS_FAQ_SHA,
        "text/html",
        faq_text,
        AuthorityClass.OFFICIAL_PROCEDURE,
        linked_urls=(SAUGUS_FORM_LINK,),
    )
    reply, _how = _saugus_model_reply(faq_text)
    parsed = parse_findings(reply)
    without_form = apply_quote_guard(parsed, (faq_source,))
    assert (
        next(f for f in without_form if f.key is FactKey.MANDATORY_FORMS).status
        is FindingStatus.UNKNOWN
    )


async def test_saugus_customer_checkout_end_to_end_and_backup_parity(tmp_path: Path) -> None:
    svc, payments, lob = _build_saugus_checkout(tmp_path)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=create_app(svc)), base_url="http://saugus.test"
        ) as api:
            ws = (await api.post("/workspaces", headers=ALICE, json={"notes": "Meter note"})).json()
            ws_id = ws["workspace_id"]
            photo = (
                await api.post(
                    f"/workspaces/{ws_id}/photos",
                    headers=ALICE,
                    json={"content_type": "image/png", "data_base64": PNG_B64},
                )
            ).json()["photo_ref"]

            submitted = await api.post(
                f"/workspaces/{ws_id}/intake",
                headers=ALICE,
                json=_saugus_intake(photo),
            )
            assert submitted.status_code == 200, submitted.text
            receipt = submitted.json()
            case_id = receipt["case_id"]
            # Saugus's official PDF form asks WAS THIS YOUR VEHICLE? before payment.
            assert receipt["state"] == CaseState.NEEDS_CUSTOMER.value

            status_before = (await api.get(f"/cases/{case_id}/status", headers=ALICE)).json()
            assert status_before["state"] == CaseState.NEEDS_CUSTOMER.value
            assert status_before["question"]["question_id"] == "VEHICLE_RELATIONSHIP"

            # Attempting card authorization before answering is refused with zero holds.
            premature = await api.post(
                f"/cases/{case_id}/authorization",
                headers=ALICE,
                json={"terms_accepted": True, "payment_method_id": "pm_saugus_early"},
            )
            assert premature.status_code == 409
            assert premature.json()["detail"] == "case_stopped"
            assert len(payments.authorizations) == 0

            # Answering REGISTERED_OWNER unlocks the pre-hold paid offer.
            answered = await api.post(
                f"/cases/{case_id}/answers",
                headers=ALICE,
                json={"question_id": "VEHICLE_RELATIONSHIP", "answer": "REGISTERED_OWNER"},
            )
            assert answered.status_code == 200, answered.text
            assert answered.json()["state"] == CaseState.INTAKE.value

            offer = (await api.get(f"/cases/{case_id}/pre-hold", headers=ALICE)).json()
            assert offer["kind"] == "offer", offer
            assert offer["issuer"]["issuer_name"] == "Town of Saugus"
            assert offer["issuer"]["deciding_body"] == "Parking Hearings Officer"

            chosen = await api.put(
                f"/cases/{case_id}/tier", headers=ALICE, json={"tier": "CERTIFIED"}
            )
            assert chosen.status_code == 200, chosen.text
            authorized = await api.post(
                f"/cases/{case_id}/authorization",
                headers=ALICE,
                json={"terms_accepted": True, "payment_method_id": "pm_saugus_ok"},
            )
            assert authorized.status_code == 200, authorized.text
            assert len(payments.authorizations) == 1

            # Post-hold research reuses the saved answer and transitions straight to DRAFTING.
            step_res = await api.post(
                f"/internal/cases/{case_id}/step",
                headers=WORKER,
                json={"step_id": "research"},
            )
            assert step_res.status_code == 200, step_res.text
            assert step_res.json()["state"] == CaseState.DRAFTING.value

            step_draft = await api.post(
                f"/internal/cases/{case_id}/step",
                headers=WORKER,
                json={"step_id": "draft"},
            )
            assert step_draft.status_code == 200, step_draft.text
            assert step_draft.json()["state"] == CaseState.AWAITING_ADOPTION.value

            draft = (await api.get(f"/cases/{case_id}/draft", headers=ALICE)).json()
            adopted = await api.post(
                f"/cases/{case_id}/adoption",
                headers=ALICE,
                json={"text": draft["text"], "text_sha256": draft["text_sha256"]},
            )
            assert adopted.status_code == 200, adopted.text

            preview = (
                await api.post(
                    f"/cases/{case_id}/packet-preview",
                    headers=ALICE,
                    json={"adopted_sha256": draft["text_sha256"]},
                )
            ).json()
            pdf_bytes = base64.b64decode(preview["pdf_base64"])
            assert hashlib.sha256(pdf_bytes).hexdigest() == preview["packet_sha256"]

            approved = await api.post(
                f"/cases/{case_id}/approval",
                headers=ALICE,
                json={
                    "adopted_sha256": draft["text_sha256"],
                    "packet_sha256": preview["packet_sha256"],
                    "envelope_sha256": preview["envelope_sha256"],
                    "affirmations": [],
                },
            )
            assert approved.status_code == 200, approved.text

            final_status = (
                await api.post(
                    f"/internal/cases/{case_id}/step",
                    headers=WORKER,
                    json={"step_id": "dispatch"},
                )
            ).json()
            if final_status["payment_state"] == PaymentState.CAPTURE_PENDING.value:
                final_status = (
                    await api.post(
                        f"/internal/cases/{case_id}/step",
                        headers=WORKER,
                        json={"step_id": "dispatch"},
                    )
                ).json()
            assert final_status["state"] == CaseState.IN_TRANSIT.value
            assert final_status["payment_state"] == PaymentState.CAPTURED.value
            assert len(payments.authorizations) == 1
            assert payments.captures == 1
            assert len(lob.calls) == 1
            recipient = lob.calls[0][1].recipient
            assert recipient.recipient == "Town of Saugus"
            assert recipient.attention == "Parking Hearings Officer"
            assert recipient.line1 == "298 Central Street"
            assert recipient.line2 == "Suite 5"
            assert recipient.city == "Saugus"
            assert recipient.state == "MA"
            assert recipient.zip_code == "01906"

        # Backup + reopen parity check: zero duplicate captures or letters on replay.
        live_db = tmp_path / "data/parkingbreaker.sqlite"
        backup_db = tmp_path / "backup.sqlite"
        with (
            sqlite3.connect(live_db) as src_conn,
            sqlite3.connect(backup_db) as dst_conn,
        ):
            src_conn.backup(dst_conn)
            assert dst_conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        with SqliteTransactionStore(backup_db) as restored:
            intake_rec = restored.get_intake(case_id)
            assert intake_rec is not None
            assert intake_rec.case_token is not None
            restored_case = restored.get_case(intake_rec.case_token)
            assert restored_case is not None
            assert restored_case.letter_state is CaseState.IN_TRANSIT
            assert restored_case.payment_state is PaymentState.CAPTURED
    finally:
        svc._store.close()


async def test_saugus_non_owner_and_hp_violation_stop_without_charge_and_allow_restart(
    tmp_path: Path,
) -> None:
    svc, payments, lob = _build_saugus_checkout(tmp_path)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=create_app(svc)), base_url="http://saugus-neg.test"
        ) as api:
            ws1 = (
                await api.post("/workspaces", headers=ALICE, json={"notes": "Keep my story"})
            ).json()["workspace_id"]
            photo = (
                await api.post(
                    f"/workspaces/{ws1}/photos",
                    headers=ALICE,
                    json={"content_type": "image/png", "data_base64": PNG_B64},
                )
            ).json()["photo_ref"]
            await api.put(
                f"/workspaces/{ws1}/intake-form",
                headers=ALICE,
                json={
                    "expected_revision": 1,
                    "form": {"ticket_photo_ref": photo, "step": 2},
                },
            )
            sub1 = (
                await api.post(
                    f"/workspaces/{ws1}/intake",
                    headers=ALICE,
                    json=_saugus_intake(photo, citation_number="SAU-NONOWNER-1"),
                )
            ).json()
            case1 = sub1["case_id"]
            assert sub1["state"] == CaseState.NEEDS_CUSTOMER.value

            # Non-owner triggers Saugus's owner permission-letter requirement -> stops pre-hold.
            ans = await api.post(
                f"/cases/{case1}/answers",
                headers=ALICE,
                json={"question_id": "VEHICLE_RELATIONSHIP", "answer": "DRIVER_NOT_OWNER"},
            )
            assert ans.status_code == 200, ans.text
            stopped = ans.json()
            assert stopped["state"] == CaseState.STOPPED_NO_CHARGE.value
            assert stopped["stop"]["family"] == StopFamily.PROCESS_INCOMPATIBLE.value
            assert stopped["stop"]["hard_stop"] == HardStop.S3.value
            assert "VEHICLE OWNER GIVING YOU PERMISSION" in stopped["stop"]["official_quote"]
            assert stopped["stop"]["official_url"] == SAUGUS_URL
            assert not payments.authorizations
            assert not lob.calls

            # Assisted restart ("Update my details" / POST /workspaces) unlocks and carries photo.
            restarted = await api.post("/workspaces", headers=ALICE, json={"notes": ""})
            assert restarted.status_code == 201, restarted.text
            ws2 = restarted.json()
            assert ws2["workspace_id"] != ws1
            assert ws2["notes"] == "Keep my story"
            assert ws2["intake_form"]["ticket_photo_ref"] == photo

            # On the restarted workspace, an HP/handicap violation cannot skip the HP placard copy.
            sub_hp = (
                await api.post(
                    f"/workspaces/{ws2['workspace_id']}/intake",
                    headers=ALICE,
                    json=_saugus_intake(
                        photo,
                        citation_number="SAU-HP-2",
                        violation_code="HP PARKING",
                        violation_description="Handicap accessible space without placard",
                    ),
                )
            ).json()
            assert sub_hp["state"] == CaseState.STOPPED_NO_CHARGE.value
            pre_hp = (await api.get(f"/cases/{sub_hp['case_id']}/pre-hold", headers=ALICE)).json()
            assert pre_hp["kind"] == "stop"
            assert pre_hp["family"] == StopFamily.PROCESS_INCOMPATIBLE.value
            assert pre_hp["hard_stop"] == HardStop.S3.value
            assert "HP placard" in pre_hp["official_quote"]
            assert not payments.authorizations
    finally:
        svc._store.close()


async def test_prehold_payment_question_refusal_precedence_restart_and_recovery(
    tmp_path: Path,
) -> None:
    # 1. West Covina stops pre-hold with official_url AND unlocks assisted restart (2.3).
    wc_svc, wc_pay, wc_lob = _build_saugus_checkout(tmp_path / "wc", city="West Covina")
    try:
        async with AsyncClient(
            transport=ASGITransport(app=create_app(wc_svc)), base_url="http://wc.test"
        ) as api:
            ws = (await api.post("/workspaces", headers=ALICE, json={"notes": "WC note"})).json()[
                "workspace_id"
            ]
            photo = (
                await api.post(
                    f"/workspaces/{ws}/photos",
                    headers=ALICE,
                    json={"content_type": "image/png", "data_base64": PNG_B64},
                )
            ).json()["photo_ref"]
            sub = (
                await api.post(
                    f"/workspaces/{ws}/intake",
                    headers=ALICE,
                    json=_saugus_intake(photo, city="West Covina"),
                )
            ).json()
            wc_case = sub["case_id"]
            assert sub["state"] == CaseState.STOPPED_NO_CHARGE.value
            stop_view = (await api.get(f"/cases/{wc_case}/pre-hold", headers=ALICE)).json()
            assert stop_view["kind"] == "stop"
            assert stop_view["reason_key"] == "filing_evidence_unverified"
            assert stop_view["official_url"] == WEST_COVINA_URL

            # Repeated tier/authorization retries create zero payment or mail calls (2.1).
            for _ in range(2):
                choose_refused = await api.put(
                    f"/cases/{wc_case}/tier", headers=ALICE, json={"tier": "CERTIFIED"}
                )
                assert choose_refused.status_code == 409
                assert choose_refused.json()["detail"] == "pre_hold_stopped"
                auth_refused = await api.post(
                    f"/cases/{wc_case}/authorization",
                    headers=ALICE,
                    json={"terms_accepted": True, "payment_method_id": "pm_wc_blocked"},
                )
                assert auth_refused.status_code == 409
                assert auth_refused.json()["detail"] == "case_stopped"
            assert not wc_pay.authorizations
            assert not wc_lob.calls

            # Assisted restart works after filing_evidence_unverified stop.
            restarted = await api.post("/workspaces", headers=ALICE, json={"notes": ""})
            assert restarted.status_code == 201, restarted.text
            assert restarted.json()["workspace_id"] != ws
            assert restarted.json()["notes"] == "WC note"
    finally:
        wc_svc._store.close()

    # 2. Pre-hold PAID_FINE question (2.4), refusal precedence (2.1), and recovery (2.2).
    clock_box = [NOW]
    rule_quote = "Paying the fine waives your right to request an initial review."
    svc, payments, _lob = _build_saugus_checkout(
        tmp_path / "payq",
        clock_ref=clock_box,
        paid_cohort_cap=1,
        payment_rule_quote=rule_quote,
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=create_app(svc)), base_url="http://payq.test"
        ) as api:
            # First customer answers REGISTERED_OWNER + PAID_FINE=True -> stops pre-hold.
            ws_stop = (await api.post("/workspaces", headers=ALICE, json={"notes": ""})).json()[
                "workspace_id"
            ]
            photo = (
                await api.post(
                    f"/workspaces/{ws_stop}/photos",
                    headers=ALICE,
                    json={"content_type": "image/png", "data_base64": PNG_B64},
                )
            ).json()["photo_ref"]
            case_stop = (
                await api.post(
                    f"/workspaces/{ws_stop}/intake",
                    headers=ALICE,
                    json=_saugus_intake(photo, citation_number="SAU-PAID-STOP"),
                )
            ).json()["case_id"]
            ans1 = (
                await api.post(
                    f"/cases/{case_stop}/answers",
                    headers=ALICE,
                    json={"question_id": "VEHICLE_RELATIONSHIP", "answer": "REGISTERED_OWNER"},
                )
            ).json()
            assert ans1["state"] == CaseState.NEEDS_CUSTOMER.value
            assert ans1["question"]["question_id"] == "PAID_FINE"

            ans2 = (
                await api.post(
                    f"/cases/{case_stop}/answers",
                    headers=ALICE,
                    json={"question_id": "PAID_FINE", "answer": True},
                )
            ).json()
            assert ans2["state"] == CaseState.STOPPED_NO_CHARGE.value
            assert ans2["stop"]["family"] == StopFamily.PROCESS_INCOMPATIBLE.value
            assert not payments.authorizations

            # Restarted workspace answers REGISTERED_OWNER + PAID_FINE=False -> Offer unlocked!
            ws_live = (await api.post("/workspaces", headers=ALICE, json={"notes": ""})).json()[
                "workspace_id"
            ]
            assert ws_live != ws_stop
            case_live = (
                await api.post(
                    f"/workspaces/{ws_live}/intake",
                    headers=ALICE,
                    json=_saugus_intake(photo, citation_number="SAU-PAID-OK"),
                )
            ).json()["case_id"]
            await api.post(
                f"/cases/{case_live}/answers",
                headers=ALICE,
                json={"question_id": "VEHICLE_RELATIONSHIP", "answer": "REGISTERED_OWNER"},
            )
            ready = (
                await api.post(
                    f"/cases/{case_live}/answers",
                    headers=ALICE,
                    json={"question_id": "PAID_FINE", "answer": False},
                )
            ).json()
            assert ready["state"] == CaseState.INTAKE.value
            assert (await api.get(f"/cases/{case_live}/pre-hold", headers=ALICE)).json()[
                "kind"
            ] == ("offer")
            await api.put(f"/cases/{case_live}/tier", headers=ALICE, json={"tier": "CERTIFIED"})

            # Simulate an interrupted/uncertain first card authorization call, then advance time
            # past the filing deadline and verify /authorization/recovery still resolves the
            # original attempt idempotently without creating a second hold (2.2).
            real_authorize = payments.authorize
            seen_keys: list[str] = []

            def flaky_authorize(
                amount_cents: int,
                payment_method_id: str,
                key: str,
                metadata: Mapping[str, str],
            ) -> PaymentOutcome:
                seen_keys.append(key)
                res = real_authorize(amount_cents, payment_method_id, key, metadata)
                if len(seen_keys) == 1:
                    return PaymentOutcome("uncertain", res.authorization_ref, amount_cents)
                return res

            payments.authorize = flaky_authorize  # type: ignore[method-assign]
            uncertain = await api.post(
                f"/cases/{case_live}/authorization",
                headers=ALICE,
                json={"terms_accepted": True, "payment_method_id": "pm_recover_1"},
            )
            assert uncertain.status_code == 409
            assert uncertain.json()["detail"] == "authorization_outcome_unresolved"
            assert len(seen_keys) == 1

            # Advance clock 25 days so a new intake would fail deadline_feasible, yet the
            # pending attempt recovers using its original idempotency key.
            clock_box[0] = NOW + timedelta(days=25)
            recovered = await api.post(f"/cases/{case_live}/authorization/recovery", headers=ALICE)
            assert recovered.status_code == 200, recovered.text
            assert recovered.json()["payment_state"] == PaymentState.AUTHORIZED.value
            assert len(seen_keys) == 1
            assert len(payments.authorizations) == 1

            # Second customer ("bob") hits paid_cohort_full precedence (cap == 1) AND
            # paid_cohort_full does NOT mark bob's live offer finished (2.1 & 2.3).
            clock_box[0] = NOW
            bob = {"Authorization": "Bearer fake-bob"}
            ws_bob = (await api.post("/workspaces", headers=bob, json={"notes": "Bob"})).json()[
                "workspace_id"
            ]
            photo_bob = (
                await api.post(
                    f"/workspaces/{ws_bob}/photos",
                    headers=bob,
                    json={"content_type": "image/png", "data_base64": PNG_B64},
                )
            ).json()["photo_ref"]
            case_bob = (
                await api.post(
                    f"/workspaces/{ws_bob}/intake",
                    headers=bob,
                    json=_saugus_intake(photo_bob, citation_number="SAU-COHORT-FULL"),
                )
            ).json()["case_id"]
            cohort_stop = (await api.get(f"/cases/{case_bob}/pre-hold", headers=bob)).json()
            assert cohort_stop["reason_key"] == "paid_cohort_full"
            cohort_auth = await api.post(
                f"/cases/{case_bob}/authorization",
                headers=bob,
                json={"terms_accepted": True, "payment_method_id": "pm_bob"},
            )
            assert cohort_auth.status_code == 409
            assert cohort_auth.json()["detail"] == "paid_cohort_full"
            # Re-POSTing /workspaces replays ws_bob instead of forking it.
            assert (await api.post("/workspaces", headers=bob, json={"notes": "Bob"})).json()[
                "workspace_id"
            ] == ws_bob

            # Beta entitlement takes top precedence ("beta_no_payment_required") on authorize,
            # even when the paid cohort cap is full and service_phase returns to PAID.
            campaign(svc._store)
            qual_digest = "c" * 64
            svc._store.bind_beta_jurisdiction(
                "beta-1", "MA:saugus-ma.gov", "jur-saugus", qual_digest, NOW
            )
            svc._store.qualify_beta_candidate(
                "beta-1", case_bob, qual_digest, NOW + timedelta(days=1), NOW
            )
            svc._store.select_beta_batch("beta-1", "batch-1", "approval-1", NOW)
            assert svc._store.beta_entitlement(case_bob) is not None
            beta_auth = await api.post(
                f"/cases/{case_bob}/authorization",
                headers=bob,
                json={"terms_accepted": True, "payment_method_id": "pm_bob"},
            )
            assert beta_auth.status_code == 409
            assert beta_auth.json()["detail"] == "beta_no_payment_required"
            assert len(payments.authorizations) == 1
    finally:
        svc._store.close()
