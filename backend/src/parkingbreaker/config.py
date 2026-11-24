"""Service configuration from the environment: the only module besides smoke.py that reads it.

PB_PROVIDER_MODE selects every provider at once:
- preview: the volatile in-memory preview fake (contract transcript, demos).
- fake: the real CaseService on SQLite with offline provider fakes; no network, no keys.
- test: real adapters with Stripe sk_test_ and Lob test_ keys only.
- live: real adapters with live keys; refuses to start unless PB_ALLOW_LIVE=1, and refuses
  mixed test/live keys. Errors name a variable, never its value.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import cast
from uuid import uuid4

from parkingbreaker.ai_spend_store import WORKER_CAPS_CENTS
from parkingbreaker.api.service import CaseServices
from parkingbreaker.appeal.drafting import CustomerTextDrafter
from parkingbreaker.blob_store import LocalBlobStore
from parkingbreaker.case_service import CaseService, CaseServiceDeps, ResearchDeps
from parkingbreaker.config_fakes import (
    CONTEST,
    ROOT,
    SYNTHETIC_VISION_MODEL,
    AcceptingLobClient,
    FakeStripeTransport,
    FixtureLlm,
    SyntheticVisionLlm,
)
from parkingbreaker.dotgov_source import load_dotgov_snapshot
from parkingbreaker.http_fetcher import HttpFetcher
from parkingbreaker.journey_context import JourneyBuildIdentity
from parkingbreaker.llm import LlmClient, MultimodalLlmClient
from parkingbreaker.llm_gateway import CallLoggingLlmClient, SpendLedger
from parkingbreaker.lob_client import LobSdkClient
from parkingbreaker.oplog.ai_journal import AiWorker
from parkingbreaker.prehold import PreholdDeps
from parkingbreaker.prices import assigned_price
from parkingbreaker.provider_credentials import valid_header_credential
from parkingbreaker.research.extractor import LlmExtractor
from parkingbreaker.research.fakes import FakeFetcher, FakeSearcher, html_response
from parkingbreaker.research.issuer_assessment import LlmIssuerAssessor
from parkingbreaker.research.jurisdiction import DotGovRecord, DotGovSnapshot
from parkingbreaker.research.link_assessment import LlmLinkAssessor
from parkingbreaker.research.search import Searcher, SearchHit
from parkingbreaker.research.sources import Fetcher
from parkingbreaker.service_phase import ServicePhase, parse_service_phase
from parkingbreaker.service_policy import SERVICE_CONTROL, STATE_BASIS
from parkingbreaker.smoke import LOB_API_VERSION, load_environment
from parkingbreaker.stripe_payment import STRIPE_API_VERSION, StripePaymentClient
from parkingbreaker.ticket_intake_service import TicketIntakeCoordinator, TicketIntakeDeps
from parkingbreaker.ticket_intake_store import TicketIntakeStore
from parkingbreaker.ticket_observability import load_review_canary_config
from parkingbreaker.ticket_ocr import OcrPolicy, RoutedTicketReader
from parkingbreaker.ticket_vision import TicketVisionReader
from parkingbreaker.transaction_store import SqliteTransactionStore
from parkingbreaker.tricks import Trick, parse_catalog

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE = REPO_ROOT / "backend/tests/fixtures/sfmta_contest.html"


class ProviderMode(StrEnum):
    PREVIEW = "preview"
    FAKE = "fake"
    TEST = "test"
    LIVE = "live"


class ConfigError(ValueError):
    """Names the offending variable; never carries a value."""


@dataclass(frozen=True)
class Settings:
    mode: ProviderMode
    data_dir: Path
    worker_secret: str
    values: Mapping[str, str]
    # None means "not yet resolved": fake/test/preview default to PAID, live refuses it.
    service_phase: ServicePhase | None = None


PHASE_VARIABLE = "PB_SERVICE_PHASE"
_PHASE_ERROR = f"{PHASE_VARIABLE} must be paid or controlled_free_beta"


def _paid_cohort_cap(mode: ProviderMode, values: Mapping[str, str]) -> int | None:
    raw = values.get("PB_PAID_COHORT_CAP")
    if raw is None:
        return 0 if mode is ProviderMode.LIVE else None
    if mode is ProviderMode.PREVIEW:
        raise ConfigError("PB_PAID_COHORT_CAP requires a durable provider mode")
    if not raw.isascii() or not raw.isdecimal() or len(raw) > 19 or int(raw) > 2**63 - 1:
        raise ConfigError("PB_PAID_COHORT_CAP must be a nonnegative SQLite integer")
    return int(raw)


def resolve_service_phase(settings: Settings) -> ServicePhase:
    """Resolve the frozen process phase before any data or adapter is opened.

    A programmatic ``Settings`` is validated here too, so constructing one cannot bypass
    the live-mode requirement to state the phase explicitly.
    """
    if settings.service_phase is None:
        if settings.mode is ProviderMode.LIVE:
            raise ConfigError(f"{_PHASE_ERROR} in live mode")
        return ServicePhase.PAID
    if not isinstance(settings.service_phase, ServicePhase):
        raise ConfigError(_PHASE_ERROR)
    return settings.service_phase


def journey_identity_from_settings(settings: Settings) -> JourneyBuildIdentity | None:
    """Optional declared nonsecret manifest digests, not a deployment attestation.

    Never hash the whole environment, infer a build from Git or assign this identity
    to old rows. Blank/unconfigured values remain unknown independently.
    """
    digests: list[str | None] = []
    for name in ("PB_JOURNEY_BUILD_SHA256", "PB_JOURNEY_CONFIG_SHA256"):
        raw = settings.values.get(name, "").strip()
        if not raw:
            digests.append(None)
            continue
        try:
            JourneyBuildIdentity(build_sha256=raw)
        except ValueError:
            raise ConfigError(
                f"{name} must be a nonsecret lowercase SHA-256 manifest digest"
            ) from None
        digests.append(raw)
    if digests == [None, None]:
        return None
    return JourneyBuildIdentity(digests[0], digests[1])


TICKET_READING_PREVIEW_VARIABLE = "PB_TICKET_READING_PREVIEW"


def ticket_reading_required(mode: ProviderMode, values: Mapping[str, str]) -> bool:
    """Whether this process must require the actual ticket-reading confirmation path.

    Real provider modes always require it; fake/preview stay legacy-compatible unless
    the explicit synthetic-acceptance setting enables it with fake multimodal clients.
    """
    if mode in {ProviderMode.TEST, ProviderMode.LIVE}:
        return True
    return values.get(TICKET_READING_PREVIEW_VARIABLE) == "1"


def _key(values: Mapping[str, str], name: str, prefixes: tuple[str, ...]) -> str:
    value = values.get(name, "")
    if not valid_header_credential(value) or not any(
        value.startswith(p) and value[len(p) :].strip() for p in prefixes
    ):
        raise ConfigError(f"{name} is missing or not a key for this mode")
    return value


def validate_worker_secret(secret: str, mode: ProviderMode) -> None:
    if mode not in {ProviderMode.TEST, ProviderMode.LIVE}:
        return
    if (
        not secret.isascii()
        or len(secret) < 16
        or secret
        in {"fake-worker", "local-worker-secret-0123456789", "dev-worker-secret-0123456789"}
        or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in secret)
    ):
        raise ConfigError("PB_WORKER_SECRET must be a private nondefault secret")


def load_settings(
    environment: Mapping[str, str] | None = None, env_path: Path | None = None
) -> Settings:
    values = load_environment(
        env_path if env_path is not None else REPO_ROOT / ".env",
        os.environ if environment is None else environment,
    )
    try:
        mode = ProviderMode(values.get("PB_PROVIDER_MODE", "preview"))
    except ValueError:
        raise ConfigError("PB_PROVIDER_MODE must be preview, fake, test or live") from None
    secret = values.get("PB_WORKER_SECRET", "")
    if mode is ProviderMode.PREVIEW:
        secret = secret or "fake-worker"
    elif len(secret) < 16:
        raise ConfigError("PB_WORKER_SECRET must be set (16+ characters)")
    if mode is ProviderMode.TEST:
        _key(values, "STRIPE_SECRET_KEY", ("sk_test_",))
        _key(values, "LOB_API_KEY", ("test_",))
    if mode is ProviderMode.LIVE:
        if values.get("PB_ALLOW_LIVE") != "1":
            raise ConfigError("PB_ALLOW_LIVE must be 1 to start in live mode")
        _key(values, "STRIPE_SECRET_KEY", ("sk_live_",))
        _key(values, "LOB_API_KEY", ("live_",))
    if mode in {ProviderMode.TEST, ProviderMode.LIVE}:
        validate_worker_secret(secret, mode)
        _key(
            values,
            "NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY",
            ("pk_test_" if mode is ProviderMode.TEST else "pk_live_",),
        )
        # A real provider mode never starts unable to verify its webhooks (K03).
        _key(values, "STRIPE_WEBHOOK_SECRET", ("whsec_",))
        _key(values, "LOB_WEBHOOK_SECRET", ("",))
    raw_phase = values.get(PHASE_VARIABLE, "").strip()
    phase: ServicePhase | None = None
    if raw_phase:
        try:
            phase = parse_service_phase(raw_phase)
        except ValueError:
            raise ConfigError(_PHASE_ERROR) from None
    if mode is ProviderMode.LIVE and phase is None:
        raise ConfigError(f"{_PHASE_ERROR} in live mode")
    _paid_cohort_cap(mode, values)
    try:
        load_review_canary_config(values)
    except ValueError as exc:
        raise ConfigError(str(exc)) from None
    data_dir = Path(values.get("PB_DATA_DIR", str(REPO_ROOT / ".pb-data")))
    return Settings(mode, data_dir, secret, values, phase)


def _fake_snapshot(today: date) -> DotGovSnapshot:
    record = DotGovRecord(
        "sfmta.gov",
        "City",
        "San Francisco Municipal Transportation Agency (SFMTA)",
        None,
        "San Francisco",
        "CA",
    )
    return DotGovSnapshot(
        today, (record,), {}, hashlib.sha256(b"offline fake-mode snapshot").hexdigest()
    )


def known_city_records() -> tuple[dict[str, object], ...]:
    """Legacy city files and the shared unserviceable list supply candidate starting pages."""
    from parkingbreaker.unserviceable import parse_entries, seed_records

    folder = REPO_ROOT / "backend" / "data"
    cities = tuple(
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted((folder / "cities_2x").glob("us-*.json"))
    )
    listed = parse_entries(
        (folder / "unserviceable_jurisdictions.json").read_text(encoding="utf-8")
    )
    return cities + seed_records(listed)


def book_of_tricks() -> tuple[Trick, ...]:
    """The shared Book of Tricks the hunter looks for after each research run."""
    path = REPO_ROOT / "backend" / "data" / "book_of_tricks.json"
    return parse_catalog(path.read_text(encoding="utf-8"))


def require_recovery_gates(settings: Settings) -> None:
    """Refuse restored data until reconciled; call before the database is opened."""
    if (settings.data_dir / "RESTORE_QUARANTINE.json").exists():
        raise ConfigError("restore requires original-provider reconciliation")
    if (
        settings.data_dir / "FAKE_RECOVERY_ONLY"
    ).exists() and settings.mode is not ProviderMode.FAKE:
        raise ConfigError("restored fake proof cannot enable real providers")


def build_services(
    settings: Settings, *, store: SqliteTransactionStore | None = None
) -> CaseServices | None:
    """The real CaseService for fake/test/live; None means the preview fake."""
    require_recovery_gates(settings)
    # Resolve and validate the frozen phase before any data directory or adapter exists.
    service_phase = resolve_service_phase(settings)
    paid_cohort_cap = _paid_cohort_cap(settings.mode, settings.values)
    journey_identity = journey_identity_from_settings(settings)
    try:
        review_canary = load_review_canary_config(settings.values)
    except ValueError as exc:
        raise ConfigError(str(exc)) from None
    if settings.mode is ProviderMode.PREVIEW:
        return None
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    store = (
        store
        if store is not None
        else SqliteTransactionStore(
            settings.data_dir / "parkingbreaker.sqlite", journey_identity=journey_identity
        )
    )
    if journey_identity is not None:
        try:
            store.bind_journey_identity(journey_identity)
        except ValueError:
            raise ConfigError(
                "journey runtime identity conflicts with configured manifest"
            ) from None
    photos = LocalBlobStore(settings.data_dir / "photos")

    def clock() -> datetime:
        return datetime.now(UTC)

    if settings.mode is ProviderMode.FAKE:
        html = FIXTURE.read_text(encoding="utf-8")
        llm: LlmClient = FixtureLlm(html)
        intake_llm: LlmClient = llm
        research_llm: LlmClient = llm
        preset = "offline-fixture"
        intake_preset = preset
        research_preset = preset
        fetcher: Fetcher = FakeFetcher(
            {ROOT: html_response(ROOT, html), CONTEST: html_response(CONTEST, html)}
        )
        searcher: Searcher = FakeSearcher(lambda _: (SearchHit(CONTEST),))
        snapshot = _fake_snapshot(clock().date())
        payments = StripePaymentClient(
            secret_key="sk_test_offline",
            api_version=STRIPE_API_VERSION,
            transport=FakeStripeTransport(),
        )
        lob: object = AcceptingLobClient()
        vision_llm: MultimodalLlmClient = SyntheticVisionLlm()
    else:
        from parkingbreaker.brave_search import BraveSearcher
        from parkingbreaker.llm_config import build_llm_client, resolve_role_preset

        values = settings.values
        role_routing_enabled = (
            values.get("PB_LLM_ROLE_ROUTING") == "1"
            or "PB_LLM_INTAKE_PRESET" in values
            or "PB_LLM_RESEARCH_PRESET" in values
            or "PB_LLM_DRAFTING_PRESET" in values
        )
        if role_routing_enabled:
            intake_preset = resolve_role_preset(AiWorker.INTAKE_READING, values)
            research_preset = resolve_role_preset(AiWorker.RESEARCH, values)

            intake_llm = build_llm_client(intake_preset, values)
            research_llm = build_llm_client(research_preset, values)
            preset = values.get("PB_LLM_PRESET", intake_preset)
        else:
            preset = values.get("PB_LLM_PRESET", "minimax-m3.1")
            intake_preset = research_preset = preset
            intake_llm = research_llm = build_llm_client(preset, values)

        llm = intake_llm
        fetcher = HttpFetcher()
        searcher = BraveSearcher(values)
        snapshot = load_dotgov_snapshot(Path(values.get("PB_DOTGOV_SNAPSHOT", "")))
        payments = StripePaymentClient(
            secret_key=values["STRIPE_SECRET_KEY"], api_version=STRIPE_API_VERSION
        )
        lob = LobSdkClient(values["LOB_API_KEY"], LOB_API_VERSION, timeout=20)

    def recorded_price_for(owner: str) -> int:
        assignment = store.pricing_for_scope(owner)
        return assigned_price(owner) if assignment is None else assignment.amount_cents

    base_prehold = PreholdDeps(
        snapshot,
        searcher,
        fetcher,
        LlmIssuerAssessor(intake_llm),
        LlmLinkAssessor(intake_llm),
        clock,
        25,
        STATE_BASIS,
        SERVICE_CONTROL,
    )

    # The store's AI spend mixin is the ledger (its methods take a typed self).
    spend_ledger = cast(SpendLedger, store)

    required_ticket_reading = ticket_reading_required(settings.mode, settings.values)

    if settings.mode is not ProviderMode.FAKE:
        # The role-routed intake client is the configured image client in production
        # role mode; it is wrapped per subject below with the existing intake cap.
        vision_llm = cast(MultimodalLlmClient, intake_llm)
        vision_preset = settings.values.get("PB_TICKET_OCR_PRESET", intake_preset)
        if "PB_TICKET_OCR_PRESET" in settings.values and vision_preset not in {
            "deepseek-flash",
            "minimax-m3.1",
            "gemini-3.8-flash",
            "gemini-3.5-flash-lite",
        }:
            raise ValueError("Unsupported ticket OCR preset")
        if vision_preset != intake_preset:
            vision_llm = cast(MultimodalLlmClient, build_llm_client(vision_preset, settings.values))
    else:
        vision_preset = SYNTHETIC_VISION_MODEL

    secondary_llm: LlmClient | None = None
    secondary_preset = settings.values.get("PB_TICKET_OCR_ESCALATION_PRESET", "gemini-3.8-flash")
    if secondary_preset not in {"none", "gemini-3.8-flash", "gemini-3.5-flash-lite"}:
        raise ValueError("Ticket OCR escalation must use Gemini or none")
    policy = OcrPolicy(
        float(settings.values.get("PB_TICKET_OCR_MANUAL_BELOW", "0.80")),
        float(settings.values.get("PB_TICKET_OCR_ESCALATE_BELOW", "0.90")),
    )
    if (
        settings.mode is not ProviderMode.FAKE
        and secondary_preset != "none"
        and not vision_preset.startswith("gemini-")
        and (settings.values.get("GEMINI_API_KEY") or settings.values.get("GOOGLE_API_KEY"))
    ):
        secondary_llm = build_llm_client(secondary_preset, settings.values)

    def vision_reader_for_subject(subject: str) -> TicketVisionReader:
        gateway = CallLoggingLlmClient(
            cast(LlmClient, vision_llm),
            subject,
            AiWorker.INTAKE_READING,
            vision_preset,
            store.try_append_ai_call,
            clock,
            spend=spend_ledger,
            cap_cents=WORKER_CAPS_CENTS[AiWorker.INTAKE_READING],
        )
        if settings.mode is ProviderMode.FAKE:
            return TicketVisionReader(gateway)
        secondary = None
        if secondary_llm is not None:
            secondary_gateway = CallLoggingLlmClient(
                secondary_llm,
                subject,
                AiWorker.INTAKE_READING,
                secondary_preset,
                store.try_append_ai_call,
                clock,
                spend=spend_ledger,
                cap_cents=WORKER_CAPS_CENTS[AiWorker.INTAKE_READING],
            )
            secondary = TicketVisionReader(secondary_gateway, confidence=True)
        return RoutedTicketReader(
            TicketVisionReader(gateway, confidence=True),
            vision_preset,
            secondary,
            policy,
            secondary_model=secondary_preset if secondary is not None else None,
        )

    ticket_intake: TicketIntakeCoordinator | None = None
    if settings.mode is not ProviderMode.FAKE or required_ticket_reading:
        ticket_intake = TicketIntakeCoordinator(
            TicketIntakeDeps(
                store=store,
                intake=TicketIntakeStore(store),
                photos=photos,
                clock=clock,
                new_id=lambda: uuid4().hex,
                reader_for_subject=vision_reader_for_subject,
                required=required_ticket_reading,
                synthetic=settings.mode is ProviderMode.FAKE,
                ocr_policy=policy,
                review_canary=review_canary,
            )
        )

    def prehold_for_subject(subject: str) -> PreholdDeps:
        gateway = CallLoggingLlmClient(
            intake_llm,
            subject,
            AiWorker.INTAKE_READING,
            intake_preset,
            store.try_append_ai_call,
            clock,
            spend=spend_ledger,
            cap_cents=WORKER_CAPS_CENTS[AiWorker.INTAKE_READING],
        )
        return replace(
            base_prehold,
            issuer_assessor=LlmIssuerAssessor(gateway),
            link_assessor=LlmLinkAssessor(gateway),
        )

    base_research = ResearchDeps(
        fetcher,
        LlmExtractor(research_llm),
        searcher,
        time.sleep,
        known_records=known_city_records(),
        model_label=research_preset,
        tricks=book_of_tricks(),
    )

    def research_for_subject(subject: str) -> ResearchDeps:
        gateway = CallLoggingLlmClient(
            research_llm,
            subject,
            AiWorker.RESEARCH,
            research_preset,
            store.try_append_ai_call,
            clock,
            spend=spend_ledger,
            cap_cents=WORKER_CAPS_CENTS[AiWorker.RESEARCH],
        )
        return replace(base_research, extractor=LlmExtractor(gateway))

    def drafter_for_subject(subject: str) -> CustomerTextDrafter:
        # Customer-authored: the draft is the customer's own text, so there is no
        # drafting provider, preset, spend cap or call log for this role.
        del subject
        return CustomerTextDrafter()

    return CaseService(
        CaseServiceDeps(
            store,
            base_prehold,
            clock,
            payments,
            photos,
            research=base_research,
            drafter=CustomerTextDrafter(),
            lob=lob,  # type: ignore[arg-type]
            stripe_webhook_secret=settings.values.get("STRIPE_WEBHOOK_SECRET", ""),
            lob_webhook_secret=settings.values.get("LOB_WEBHOOK_SECRET", ""),
            launch_allowlist=None,
            price_for=recorded_price_for,
            prehold_for_subject=prehold_for_subject,
            research_for_subject=research_for_subject,
            drafter_for_subject=drafter_for_subject,
            service_phase=service_phase,
            paid_cohort_cap=paid_cohort_cap,
            ticket_intake=ticket_intake,
        )
    )
