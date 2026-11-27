"""Explicitly isolated, offline SQLite API factory for synthetic browser rehearsals."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path


def _plain(path: Path) -> None:
    if path.is_symlink():
        raise RuntimeError("synthetic rehearsal refuses symlink runtime files")


class RehearsalClock:
    """Consume bounded clock commands once, retaining offsets across API restarts."""

    def __init__(self, runtime: Path):
        self.runtime = runtime
        self.lock = threading.Lock()

    def __call__(self) -> datetime:
        with self.lock:
            state_path = self.runtime / "clock-state.json"
            command_path = self.runtime / "clock-command"
            _plain(state_path)
            _plain(command_path)
            state = {"total": 0, "last_mtime_ns": None}
            if state_path.exists():
                state = json.loads(state_path.read_text())
                if (
                    set(state) != {"total", "last_mtime_ns"}
                    or type(state["total"]) is not int
                    or state["total"] < 0
                    or (
                        state["last_mtime_ns"] is not None
                        and type(state["last_mtime_ns"]) is not int
                    )
                ):
                    raise RuntimeError("invalid synthetic clock state")
            if command_path.exists():
                with command_path.open() as command:
                    stamp = os.fstat(command.fileno()).st_mtime_ns
                    value = command.read()
                if not value.strip().isascii() or not value.strip().isdigit():
                    raise RuntimeError(
                        "synthetic clock command must be an integer 0..3600"
                    )
                advance = int(value.strip())
                if not 0 <= advance <= 3600:
                    raise RuntimeError(
                        "synthetic clock command must be an integer 0..3600"
                    )
                if stamp != state["last_mtime_ns"]:
                    state = {"total": state["total"] + advance, "last_mtime_ns": stamp}
                    temporary = self.runtime / "clock-state.tmp"
                    _plain(temporary)
                    temporary.write_text(json.dumps(state) + "\n")
                    temporary.replace(state_path)
            return datetime.now(UTC) + timedelta(seconds=state["total"])


def create_rehearsal_app():
    if os.environ.get("SYNTHETIC_REHEARSAL") != "1":
        raise RuntimeError("explicit SYNTHETIC_REHEARSAL=1 required")
    if os.environ.get("PB_PROVIDER_MODE", "fake") != "fake":
        raise RuntimeError("synthetic rehearsal requires fake provider mode")
    runtime_value = os.environ.get("PB_SYNTHETIC_RUNTIME", "")
    if not runtime_value:
        raise RuntimeError("PB_SYNTHETIC_RUNTIME required")
    runtime = Path(runtime_value)
    if not runtime.is_absolute() or any(
        p.is_symlink() for p in (runtime, *runtime.parents)
    ):
        raise RuntimeError("synthetic runtime must be absolute and contain no symlinks")
    if not runtime.is_dir() or stat.S_IMODE(runtime.stat().st_mode) != 0o700:
        raise RuntimeError(
            "synthetic runtime must be an existing private mode0700 directory"
        )
    marker = runtime / "SYNTHETIC_ONLY"
    _plain(marker)
    if (
        not marker.is_file()
        or marker.read_text() != "ParkingBreaker synthetic rehearsal\n"
    ):
        raise RuntimeError("synthetic runtime marker missing or invalid")
    from urllib.parse import urlsplit

    origin = os.environ.get("PB_SYNTHETIC_ORIGIN", "")
    parsed = urlsplit(origin)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not parsed.hostname.endswith(".example.test")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.netloc != parsed.hostname
    ):
        raise RuntimeError(
            "synthetic origin must be a reserved HTTPS *.example.test origin"
        )

    worker = os.environ.get("PB_SYNTHETIC_WORKER_SECRET", "")
    lob = os.environ.get("PB_SYNTHETIC_LOB_SECRET", "")
    if not worker or not lob:
        raise RuntimeError("synthetic worker and Lob secrets required")

    from unittest.mock import patch

    from parkingbreaker import config
    from parkingbreaker.config import ProviderMode, Settings, build_services
    from parkingbreaker.service_phase import parse_service_phase

    # api.app constructs an incidental global app at import time. Force its config
    # to explicit preview without allowing load_settings to read any .env file.
    with patch.object(
        config,
        "load_settings",
        return_value=Settings(ProviderMode.PREVIEW, runtime / "data", worker, {}),
    ):
        from parkingbreaker.api.app import create_app
    from parkingbreaker.address_disclosure import DISCLOSURE_SHA256
    from parkingbreaker.auth_config import build_customer_access, load_auth_settings
    from parkingbreaker.customer_checkout import CustomerCheckout
    from parkingbreaker.research.findings import FactKey, Finding, FindingStatus, UnknownReason
    from parkingbreaker.research.sources import AuthorityClass, SourceRecord
    from parkingbreaker.research_records import answers_json, research_json
    from parkingbreaker.transaction_store import SqliteTransactionStore

    class SyntheticCaseService(CustomerCheckout):
        def _resolve(self, case_id, case_token, result, answers, revision):
            record = self._intake(case_id)
            citation = json.loads(record.intake_json)["ticket"]["citation_number"]
            if citation.startswith("SYN-COPIES-"):
                # Explicit synthetic requirement, never attributed to a real issuer.
                quote = "You must include a copy of registration."
                text = "Parking Citation Initial Review\n" + quote
                page = SourceRecord(
                    url="https://policy.example.test/required-copies",
                    final_url="https://policy.example.test/required-copies",
                    retrieved_at=self._deps.clock(),
                    http_status=200,
                    sha256=hashlib.sha256(text.encode()).hexdigest(),
                    content_type="text/html",
                    text=text,
                    authority_class=AuthorityClass.OFFICIAL_PROCEDURE,
                )
                finding = Finding(
                    FactKey.REQUIRED_ITEMS,
                    ["copy of registration"],
                    FindingStatus.CONFIRMED,
                    quote,
                    (page.sha256,),
                    jurisdiction=result.ticket.jurisdiction,
                    stage=result.ticket.stage,
                )
                result = replace(
                    result,
                    sources=(*(item for item in result.sources if item.url != page.url), page),
                    findings=tuple(
                        finding if item.key is FactKey.REQUIRED_ITEMS else item
                        for item in result.findings
                    ),
                )
                revision = self._store.save_research(
                    case_id, research_json(result), answers_json(answers),
                    self._deps.clock(), revision,
                ).revision
            if citation.startswith("SYN-T30-"):
                # Explicit counterfactual input for consent tests, not a discovered
                # official-source fact. Every other case retains normal fake research.
                findings = tuple(
                    replace(
                        item,
                        value=None,
                        status=FindingStatus.UNKNOWN,
                        verbatim_quote=None,
                        source_sha256s=(),
                        evidence=(),
                        unknown_reason=UnknownReason.UNANSWERABLE,
                        notes="Synthetic counterfactual: unavailable mailing address",
                        evidence_basis=None,
                        scope_quote=None,
                    )
                    if item.key == FactKey.MAILING_ADDRESS
                    else item
                    for item in result.findings
                )
                result = replace(
                    result,
                    findings=findings,
                    # This counterfactual represents an exhausted synthetic lookup.
                    progress=replace(
                        result.progress,
                        re_extraction_done=True,
                        broadened_search_done=True,
                    ),
                    candidates=tuple(
                        item
                        for item in result.candidates
                        if item.key != FactKey.MAILING_ADDRESS
                    ),
                ).reassess()
                revision = self._store.save_research(
                    case_id,
                    research_json(result),
                    answers_json(answers),
                    self._deps.clock(),
                    revision,
                ).revision
            return super()._resolve(case_id, case_token, result, answers, revision)

    data = runtime / "data"
    _plain(data)
    data.mkdir(exist_ok=True, mode=0o700)
    # Reusing the factory must never follow injected database/blob symlinks.
    if any(path.is_symlink() for path in data.rglob("*")):
        raise RuntimeError("synthetic data must contain no symlinks")
    key_path = runtime / "auth-link-key"
    _plain(key_path)
    if not key_path.exists():
        with key_path.open("x") as key_file:
            os.chmod(key_path, 0o600)
            key_file.write(secrets.token_urlsafe(32))
    settings = Settings(
        ProviderMode.FAKE,
        data,
        worker,
        {
            "LOB_WEBHOOK_SECRET": lob,
            "PB_AUTH_MODE": "email",
            "PB_AUTH_LINK_KEY": key_path.read_text(),
            "PB_PUBLIC_ORIGIN": origin,
            "RESEND_FROM_EMAIL": "rehearsal@example.test",
            "PB_TICKET_READING_PREVIEW": os.environ.get("PB_TICKET_READING_PREVIEW", ""),
        },
        service_phase=parse_service_phase(os.environ.get("PB_SERVICE_PHASE", "paid")),
    )
    auth_settings = load_auth_settings(settings)
    assert auth_settings is not None
    clock = RehearsalClock(runtime)
    clock()  # Fail invalid persisted state/commands before serving any customer request.
    store = SqliteTransactionStore(data / "parkingbreaker.sqlite")
    services = build_services(settings, store=store)
    assert services is not None
    services = SyntheticCaseService(replace(services._deps, clock=clock))
    auth, notifications = build_customer_access(auth_settings, store, clock=clock)
    app = create_app(
        services,
        provider_mode="fake",
        worker_secret=worker,
        customer_auth=auth,
        notifications=notifications,
    )

    @app.get("/__harness__/proof/{case_id}")
    async def durable_proof(case_id: str) -> dict[str, object]:
        """Private synthetic evidence only; Next ingress does not expose this route."""
        intake = services._intake(case_id)
        owner = intake.owner
        token = intake.case_token
        manifest = None if token is None else store.get_approval_manifest(token, 1)
        addresses = store.customer_address_revisions(case_id, owner)
        consents = store.address_consent_events(case_id, owner)
        decisions = store.destination_decision_history(case_id, owner)
        decision = decisions[-1].decision if decisions else None
        return {
            "case_id": case_id,
            "prepared_attempts": 0 if token is None else sum(store.get(token, attempt) is not None for attempt in (1, 2)),
            "dispatch_manifest": None if manifest is None else {
                "original_statement_sha256": manifest.raw_customer_text_sha256,
                "approved_pdf_sha256": manifest.packet_pdf_sha256,
                "approved_envelope_sha256": manifest.envelope_sha256,
                "dispatched_pdf_sha256": manifest.dispatched_pdf_sha256,
            },
            "address_revisions": len(addresses),
            "consent_events": len(consents),
            "decision_history": len(decisions),
            "latest_decision": None
            if decision is None
            else {
                "provenance": decision.provenance.value,
                "refund_policy_version": decision.refund_policy_version,
                "address_revision": decision.customer_address_revision,
                "address_fingerprint": decision.fingerprint,
                "disclosure_sha256": None
                if decision.disclosure is None
                else decision.disclosure.sha256,
                "consent_case_matches": decision.consent is not None
                and decision.consent.case_id == case_id,
                "consent_owner_matches": decision.consent is not None
                and decision.consent.owner == owner,
                "consent_is_explicit": decision.consent is not None
                and decision.consent.accepted is True,
            },
            "consent_events_match_decision": bool(
                decision is not None
                and decision.consent is not None
                and decision.consent in consents
                and decision.disclosure is not None
                and decision.disclosure.sha256 == DISCLOSURE_SHA256
            ),
        }

    return app
