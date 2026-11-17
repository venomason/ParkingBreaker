"""Durable, privacy-safe OCR routing observability and TicketFactReview canary tracking.

This module shares the existing SQLite authority connection via ``pb_store_versions``
(component ``"ticket_observability"``, version ``1``) and links to ``pb_journey_links`` /
``pb_ticket_intake_operations`` without introducing a second database or external bus.

Privacy and authority invariants:
- Closed enums, bounded integer timings/counts, and bounded configured model metadata
  only; no ticket field values, raw prompts/errors, customer names, URLs, emails, or
  provider/payment identifiers are accepted or persisted.
- All three tables are append-only/immutable via SQLite ``BEFORE UPDATE`` and
  ``BEFORE DELETE`` triggers.
- Client review observations are explicitly labeled ``client_observation`` and
  ``elapsed_and_visibility_activity_not_human_attention``; they never constitute
  verified facts or authoritative customer confirmations.
- Review canary eligibility is deterministic from opaque server workspace identity and
  pinned at session creation so reloads/replays never flip mid-edit during rollback.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Final, Protocol, runtime_checkable

from parkingbreaker.journey_events import (
    JOURNEY_ID_PATTERN,
)
from parkingbreaker.journey_events import (
    _assert_initialized as assert_journey_initialized,
)
from parkingbreaker.mail_attempt_store import (
    StorageCorruption,
    check_versions_schema,
    rollback_preserving_error,
)
from parkingbreaker.ticket_ocr import OcrPolicy, RoutedExtractTrace, RoutedTicketReader
from parkingbreaker.ticket_vision import (
    FIELD_ORDER,
    ExtractTrace,
    ReadingState,
    TicketReading,
    TicketVisionReader,
)

COMPONENT: Final[str] = "ticket_observability"
SCHEMA_VERSION: Final[int] = 1

MAX_BACKEND_DURATION_MS: Final[int] = 10**13
MAX_CLIENT_DURATION_MS: Final[int] = 86_400_000
MAX_VISIBILITY_CHANGES: Final[int] = 10_000
MAX_OBSERVATIONS_PER_SESSION: Final[int] = 64
MAX_SESSIONS_PER_WORKSPACE: Final[int] = 64
_CLIENT_NONCE_RE = re.compile(r"^[0-9a-f]{32}$")
MAX_ID_LENGTH: Final[int] = 128
MAX_MODEL_LENGTH: Final[int] = 64
MAX_CANARY_VERSION_LENGTH: Final[int] = 32
TOTAL_LITERAL_FIELDS: Final[int] = len(FIELD_ORDER)

MEASUREMENT_LABEL: Final[str] = "elapsed_and_visibility_activity_not_human_attention"
CLIENT_SOURCE_LABEL: Final[str] = "client_observation"

_OBSERVATION_ID_RE: Final[re.Pattern[str]] = re.compile(r"^obs_[0-9a-f]{32}$")
_SESSION_ID_RE: Final[re.Pattern[str]] = re.compile(r"^rvs_[0-9a-f]{32}$")
_JOURNEY_ID_RE: Final[re.Pattern[str]] = re.compile(JOURNEY_ID_PATTERN)
_OPAQUE_KEY_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_MODEL_NAME_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_CANARY_VERSION_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")
_SHA256_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_CANARY_ELIGIBILITY_DOMAIN: Final[bytes] = b"pb-review-canary-v1\x00"

_OCR_OBSERVATIONS_DDL: Final[str] = """CREATE TABLE pb_ticket_ocr_observations (
    request_id TEXT PRIMARY KEY REFERENCES pb_ticket_intake_operations(request_id),
    observation_id TEXT NOT NULL UNIQUE,
    intake_id TEXT NOT NULL REFERENCES pb_ticket_intake_reservations(intake_id),
    workspace_token TEXT NOT NULL,
    journey_id TEXT NOT NULL REFERENCES pb_journey_links(journey_id),
    operation_status TEXT NOT NULL CHECK (operation_status IN ('success', 'refused', 'unknown')),
    routing_mode TEXT NOT NULL CHECK (routing_mode IN ('routed', 'single_stage')),
    primary_model TEXT CHECK (primary_model IS NULL OR (length(primary_model) BETWEEN 1 AND 64)),
    secondary_model TEXT
        CHECK (secondary_model IS NULL OR (length(secondary_model) BETWEEN 1 AND 64)),
    primary_status TEXT NOT NULL CHECK (primary_status IN (
        'completed', 'refused', 'budget_refused', 'input_error', 'provider_error'
    )),
    escalation_status TEXT NOT NULL CHECK (escalation_status IN (
        'not_needed',
        'not_applicable',
        'unavailable',
        'budget_exhausted',
        'failed',
        'refused',
        'completed'
    )),
    coordinator_duration_ms INTEGER
        CHECK (coordinator_duration_ms IS NULL OR coordinator_duration_ms >= 0),
    primary_duration_ms INTEGER
        CHECK (primary_duration_ms IS NULL OR primary_duration_ms >= 0),
    primary_start_offset_ms INTEGER
        CHECK (primary_start_offset_ms IS NULL OR primary_start_offset_ms >= 0),
    primary_end_offset_ms INTEGER
        CHECK (primary_end_offset_ms IS NULL OR primary_end_offset_ms >= 0),
    escalation_duration_ms INTEGER
        CHECK (escalation_duration_ms IS NULL OR escalation_duration_ms >= 0),
    escalation_start_offset_ms INTEGER
        CHECK (escalation_start_offset_ms IS NULL OR escalation_start_offset_ms >= 0),
    escalation_end_offset_ms INTEGER
        CHECK (escalation_end_offset_ms IS NULL OR escalation_end_offset_ms >= 0),
    shown_suggestion_count INTEGER NOT NULL CHECK (shown_suggestion_count BETWEEN 0 AND 11),
    manual_entry_field_count INTEGER NOT NULL CHECK (manual_entry_field_count BETWEEN 0 AND 11),
    reason_counts_json TEXT NOT NULL CHECK (length(reason_counts_json) <= 2048),
    decision_policy TEXT NOT NULL CHECK (length(decision_policy) BETWEEN 1 AND 96),
    content_sha256 TEXT NOT NULL
        CHECK (length(content_sha256) = 64
            AND content_sha256 NOT GLOB '*[^0-9a-f]*'),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    recorded_at TEXT NOT NULL,
    CHECK (length(observation_id) = 36 AND observation_id GLOB 'obs_*'),
    CHECK (length(workspace_token) BETWEEN 1 AND 128)
)"""

_REVIEW_SESSIONS_DDL: Final[str] = """CREATE TABLE pb_ticket_review_sessions (
    session_id TEXT PRIMARY KEY,
    client_session_key TEXT NOT NULL UNIQUE,
    workspace_token TEXT NOT NULL REFERENCES pb_intake_owners(workspace_token),
    owner TEXT NOT NULL,
    journey_id TEXT REFERENCES pb_journey_links(journey_id),
    policy_mode TEXT NOT NULL CHECK (policy_mode IN ('baseline', 'canary')),
    canary_percentage INTEGER NOT NULL CHECK (canary_percentage BETWEEN 0 AND 100),
    canary_version TEXT NOT NULL CHECK (length(canary_version) BETWEEN 1 AND 32),
    variant TEXT NOT NULL CHECK (variant IN ('baseline', 'manual_guidance_v1')),
    effective_treatment_version TEXT NOT NULL
        CHECK (length(effective_treatment_version) BETWEEN 1 AND 96),
    eligibility_bucket INTEGER NOT NULL CHECK (eligibility_bucket BETWEEN 0 AND 99),
    reading_request_id TEXT,
    workspace_revision INTEGER NOT NULL CHECK (workspace_revision >= 1),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    assigned_at TEXT NOT NULL,
    CHECK (length(session_id) = 36 AND session_id GLOB 'rvs_*'),
    CHECK (length(client_session_key) BETWEEN 1 AND 128),
    CHECK (length(workspace_token) BETWEEN 1 AND 128),
    CHECK (length(owner) BETWEEN 1 AND 128)
)"""

_REVIEW_OBSERVATIONS_DDL: Final[str] = """CREATE TABLE pb_ticket_review_observations (
    observation_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES pb_ticket_review_sessions(session_id),
    client_observation_id TEXT NOT NULL,
    workspace_token TEXT NOT NULL,
    seq INTEGER NOT NULL CHECK (seq >= 1 AND seq <= 64),
    source TEXT NOT NULL CHECK (source = 'client_observation'),
    measurement_label TEXT NOT NULL
        CHECK (measurement_label = 'elapsed_and_visibility_activity_not_human_attention'),
    event_kind TEXT NOT NULL CHECK (event_kind IN (
        'exposure', 'validation_failed', 'save_attempt', 'confirm_attempt', 'timing_checkpoint'
    )),
    treatment_variant TEXT NOT NULL CHECK (treatment_variant IN ('baseline', 'manual_guidance_v1')),
    treatment_version TEXT NOT NULL CHECK (length(treatment_version) BETWEEN 1 AND 96),
    rendered_variant TEXT NOT NULL CHECK (rendered_variant IN ('baseline', 'manual_guidance_v1')),
    outcome TEXT NOT NULL CHECK (outcome IN (
        'observed',
        'client_invalid',
        'submitted_to_server',
        'client_reported_success',
        'client_reported_failure',
        'unknown'
    )),
    visible_elapsed_ms INTEGER
        CHECK (visible_elapsed_ms IS NULL OR visible_elapsed_ms BETWEEN 0 AND 86400000),
    busy_elapsed_ms INTEGER
        CHECK (busy_elapsed_ms IS NULL OR (busy_elapsed_ms >= 0 AND busy_elapsed_ms <= 86400000)),
    hidden_elapsed_ms INTEGER
        CHECK (hidden_elapsed_ms IS NULL OR hidden_elapsed_ms BETWEEN 0 AND 86400000),
    visibility_changes INTEGER NOT NULL CHECK (visibility_changes BETWEEN 0 AND 10000),
    timing_state TEXT NOT NULL CHECK (timing_state IN ('closed', 'open_or_missing_unknown')),
    invalid_field_count INTEGER NOT NULL CHECK (invalid_field_count BETWEEN 0 AND 13),
    customer_supplied INTEGER NOT NULL CHECK (customer_supplied IN (0, 1)),
    reading_status TEXT NOT NULL
        CHECK (reading_status IN ('missing', 'pending', 'success', 'refused', 'unknown')),
    review_status TEXT NOT NULL
        CHECK (review_status IN ('missing', 'pending', 'success', 'refused', 'unknown')),
    content_sha256 TEXT NOT NULL
        CHECK (length(content_sha256) = 64
            AND content_sha256 NOT GLOB '*[^0-9a-f]*'),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    recorded_at TEXT NOT NULL,
    UNIQUE (session_id, client_observation_id),
    UNIQUE (session_id, seq),
    CHECK (length(observation_id) = 36 AND observation_id GLOB 'obs_*'),
    CHECK (length(client_observation_id) BETWEEN 1 AND 128),
    CHECK (length(workspace_token) BETWEEN 1 AND 128)
)"""

_DDL: Final[tuple[tuple[str, str], ...]] = (
    ("pb_ticket_ocr_observations", _OCR_OBSERVATIONS_DDL),
    ("pb_ticket_review_sessions", _REVIEW_SESSIONS_DDL),
    ("pb_ticket_review_observations", _REVIEW_OBSERVATIONS_DDL),
)


class TicketObservabilityConflict(ValueError):
    """Fixed refusal key for replay/limit/session conflicts; never carries customer text."""


class TicketObservabilityAccessError(PermissionError):
    """Fixed refusal key when workspace/session ownership checks fail."""


class OcrPrimaryStatus(StrEnum):
    COMPLETED = "completed"
    REFUSED = "refused"
    BUDGET_REFUSED = "budget_refused"
    INPUT_ERROR = "input_error"
    PROVIDER_ERROR = "provider_error"


class OcrEscalationStatus(StrEnum):
    NOT_NEEDED = "not_needed"
    NOT_APPLICABLE = "not_applicable"
    UNAVAILABLE = "unavailable"
    BUDGET_EXHAUSTED = "budget_exhausted"
    FAILED = "failed"
    REFUSED = "refused"
    COMPLETED = "completed"


class OcrRoutingMode(StrEnum):
    ROUTED = "routed"
    SINGLE_STAGE = "single_stage"


class OcrFieldReason(StrEnum):
    UNROUTED_SUGGESTION = "unrouted_suggestion"
    HIGH_CONFIDENCE_SUGGESTION = "high_confidence_suggestion"
    CORROBORATED_SUGGESTION = "corroborated_suggestion"
    FIELD_MISSING_CONFIDENCE = "field_missing_confidence"
    FIELD_LOW_CONFIDENCE = "field_low_confidence"
    FIELD_NOT_SHOWN = "field_not_shown"
    FIELD_UNREADABLE = "field_unreadable"
    INDEPENDENT_DISAGREEMENT = "independent_disagreement"
    ESCALATION_UNAVAILABLE = "escalation_unavailable"
    ESCALATION_BUDGET_EXHAUSTED = "escalation_budget_exhausted"
    ESCALATION_FAILED = "escalation_failed"
    ESCALATION_REFUSED = "escalation_refused"


class OcrObservationCoverage(StrEnum):
    RECORDED = "recorded"
    PENDING = "pending"
    MISSING = "missing"


class ReviewCanaryPolicy(StrEnum):
    BASELINE = "baseline"
    CANARY = "canary"


class ReviewVariant(StrEnum):
    BASELINE = "baseline"
    MANUAL_GUIDANCE_V1 = "manual_guidance_v1"


class ReviewObservationEventKind(StrEnum):
    EXPOSURE = "exposure"
    VALIDATION_FAILED = "validation_failed"
    SAVE_ATTEMPT = "save_attempt"
    CONFIRM_ATTEMPT = "confirm_attempt"
    TIMING_CHECKPOINT = "timing_checkpoint"


class ReviewObservationOutcome(StrEnum):
    OBSERVED = "observed"
    CLIENT_INVALID = "client_invalid"
    SUBMITTED_TO_SERVER = "submitted_to_server"
    CLIENT_REPORTED_SUCCESS = "client_reported_success"
    CLIENT_REPORTED_FAILURE = "client_reported_failure"
    UNKNOWN = "unknown"


class ReviewTimingState(StrEnum):
    CLOSED = "closed"
    OPEN_OR_MISSING_UNKNOWN = "open_or_missing_unknown"


@runtime_checkable
class _ConnectionOwner(Protocol):
    def _require_open(self) -> sqlite3.Connection: ...


def _to_utc(value: object, field_name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _require_utc(value: object, field_name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be UTC-aware")
    return value


def _bounded_int(value: object, field_name: str, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{field_name} must be an integer in [{minimum}, {maximum}]")
    return value


def _validate_opaque_key(value: object, field_name: str) -> str:
    if type(value) is not str or _OPAQUE_KEY_RE.fullmatch(value) is None:
        raise ValueError(f"invalid {field_name}")
    return value


def _validate_client_nonce(value: object) -> str:
    if type(value) is not str or _CLIENT_NONCE_RE.fullmatch(value) is None:
        raise ValueError("invalid client nonce")
    return value


def _validate_workspace_token(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > MAX_ID_LENGTH
        or "@" in value
        or any(ord(ch) < 32 or ord(ch) == 127 or ch.isspace() for ch in value)
    ):
        raise ValueError("invalid workspace token")
    return value


def _validate_owner(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > MAX_ID_LENGTH
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)
    ):
        raise ValueError("invalid owner")
    return value


def _validate_model_metadata(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str or _MODEL_NAME_RE.fullmatch(value) is None:
        raise ValueError(f"invalid {field_name}")
    return value


def _parse_datetime(value: object, field_name: str) -> datetime:
    if type(value) is datetime:
        return _to_utc(value, field_name)
    if type(value) is str:
        try:
            return _to_utc(datetime.fromisoformat(value), field_name)
        except ValueError as exc:
            raise ValueError(f"invalid {field_name}") from exc
    raise ValueError(f"invalid {field_name}")


def _duration_ms(start: float | None, end: float | None, field_name: str) -> int | None:
    if start is None and end is None:
        return None
    if start is None or end is None:
        raise ValueError(f"{field_name} requires both start and end monotonic readings")
    if (
        type(start) not in (int, float)
        or type(end) not in (int, float)
        or not math.isfinite(start)
        or not math.isfinite(end)
    ):
        raise ValueError(f"{field_name} monotonic readings must be finite")
    if end < start:
        raise ValueError(f"{field_name} monotonic clock moved backwards")
    ms = round((end - start) * 1000)
    return _bounded_int(ms, field_name, minimum=0, maximum=MAX_BACKEND_DURATION_MS)


def _offsets_ms(
    coord_start: float | None,
    coord_end: float | None,
    span_start: float | None,
    span_end: float | None,
) -> tuple[int | None, int | None]:
    if (
        coord_start is None
        or coord_end is None
        or span_start is None
        or span_end is None
        or not all(
            type(v) in (int, float) and math.isfinite(v)
            for v in (coord_start, coord_end, span_start, span_end)
        )
    ):
        return None, None
    if span_start < coord_start or span_end < span_start or coord_end < span_end:
        return None, None
    start_offset = round((span_start - coord_start) * 1000)
    end_offset = round((span_end - coord_start) * 1000)
    return (
        _bounded_int(start_offset, "start_offset_ms", minimum=0, maximum=MAX_BACKEND_DURATION_MS),
        _bounded_int(end_offset, "end_offset_ms", minimum=0, maximum=MAX_BACKEND_DURATION_MS),
    )


@dataclass(frozen=True)
class ReviewCanaryConfig:
    """Validated composition-root configuration for the TicketFactReview canary."""

    policy: ReviewCanaryPolicy = ReviewCanaryPolicy.BASELINE
    canary_percentage: int = 0
    version: str = "review-guidance-v1"

    def __post_init__(self) -> None:
        if type(self.policy) is not ReviewCanaryPolicy:
            raise ValueError("PB_REVIEW_GUIDANCE_CANARY_POLICY must be baseline or canary")
        _bounded_int(
            self.canary_percentage,
            "PB_REVIEW_GUIDANCE_CANARY_PERCENT",
            minimum=0,
            maximum=100,
        )
        if type(self.version) is not str or _CANARY_VERSION_RE.fullmatch(self.version) is None:
            raise ValueError("PB_REVIEW_GUIDANCE_CANARY_VERSION is invalid")


def load_review_canary_config(values: Mapping[str, str]) -> ReviewCanaryConfig:
    """Parse and validate review canary configuration from environment values."""
    raw_policy = values.get("PB_REVIEW_GUIDANCE_CANARY_POLICY", "baseline").strip() or "baseline"
    try:
        policy = ReviewCanaryPolicy(raw_policy)
    except ValueError as exc:
        raise ValueError("PB_REVIEW_GUIDANCE_CANARY_POLICY must be baseline or canary") from exc

    raw_percent = values.get("PB_REVIEW_GUIDANCE_CANARY_PERCENT", "0").strip() or "0"
    if not raw_percent.isdigit() or len(raw_percent) > 3:
        raise ValueError("PB_REVIEW_GUIDANCE_CANARY_PERCENT must be an integer from 0 to 100")
    percent = int(raw_percent)
    if not 0 <= percent <= 100:
        raise ValueError("PB_REVIEW_GUIDANCE_CANARY_PERCENT must be an integer from 0 to 100")

    raw_version = (
        values.get("PB_REVIEW_GUIDANCE_CANARY_VERSION", "review-guidance-v1").strip()
        or "review-guidance-v1"
    )
    if _CANARY_VERSION_RE.fullmatch(raw_version) is None:
        raise ValueError("PB_REVIEW_GUIDANCE_CANARY_VERSION is invalid")

    return ReviewCanaryConfig(policy=policy, canary_percentage=percent, version=raw_version)


def deterministic_eligibility_bucket(opaque_identity: str, canary_version: str) -> int:
    """Stable deterministic bucket in [0, 99] derived from opaque identity and version."""
    identity = _validate_workspace_token(opaque_identity)
    if type(canary_version) is not str or _CANARY_VERSION_RE.fullmatch(canary_version) is None:
        raise ValueError("invalid canary_version")
    digest = hashlib.sha256(
        _CANARY_ELIGIBILITY_DOMAIN + f"{canary_version}:{identity}".encode()
    ).digest()
    return int.from_bytes(digest[:8], "big") % 100


@dataclass(frozen=True)
class OcrObservationInput:
    """Closed, privacy-safe OCR routing and separated monotonic timing observation."""

    routing_mode: OcrRoutingMode
    primary_model: str | None
    secondary_model: str | None
    primary_status: OcrPrimaryStatus
    escalation_status: OcrEscalationStatus
    coordinator_duration_ms: int | None
    primary_duration_ms: int | None
    primary_start_offset_ms: int | None
    primary_end_offset_ms: int | None
    escalation_duration_ms: int | None
    escalation_start_offset_ms: int | None
    escalation_end_offset_ms: int | None
    shown_suggestion_count: int
    manual_entry_field_count: int
    reason_counts: Mapping[OcrFieldReason, int]
    decision_policy: str

    def __post_init__(self) -> None:
        if type(self.routing_mode) is not OcrRoutingMode:
            raise ValueError("routing_mode must be an OcrRoutingMode")
        _validate_model_metadata(self.primary_model, "primary_model")
        _validate_model_metadata(self.secondary_model, "secondary_model")
        if type(self.primary_status) is not OcrPrimaryStatus:
            raise ValueError("primary_status must be an OcrPrimaryStatus")
        if type(self.escalation_status) is not OcrEscalationStatus:
            raise ValueError("escalation_status must be an OcrEscalationStatus")
        for name, value in (
            ("coordinator_duration_ms", self.coordinator_duration_ms),
            ("primary_duration_ms", self.primary_duration_ms),
            ("primary_start_offset_ms", self.primary_start_offset_ms),
            ("primary_end_offset_ms", self.primary_end_offset_ms),
            ("escalation_duration_ms", self.escalation_duration_ms),
            ("escalation_start_offset_ms", self.escalation_start_offset_ms),
            ("escalation_end_offset_ms", self.escalation_end_offset_ms),
        ):
            if value is not None:
                _bounded_int(value, name, minimum=0, maximum=MAX_BACKEND_DURATION_MS)
        if (
            self.primary_start_offset_ms is not None
            and self.primary_end_offset_ms is not None
            and self.primary_end_offset_ms < self.primary_start_offset_ms
        ):
            raise ValueError("primary_end_offset_ms cannot precede primary_start_offset_ms")
        if (
            self.escalation_start_offset_ms is not None
            and self.escalation_end_offset_ms is not None
            and self.escalation_end_offset_ms < self.escalation_start_offset_ms
        ):
            raise ValueError("escalation_end_offset_ms cannot precede escalation_start_offset_ms")
        _bounded_int(
            self.shown_suggestion_count,
            "shown_suggestion_count",
            minimum=0,
            maximum=TOTAL_LITERAL_FIELDS,
        )
        _bounded_int(
            self.manual_entry_field_count,
            "manual_entry_field_count",
            minimum=0,
            maximum=TOTAL_LITERAL_FIELDS,
        )
        if not re.fullmatch(
            r"(?:ocr-routing-v1|single-stage-v1):[0-9.e-]+:[0-9.e-]+", self.decision_policy
        ):
            raise ValueError("invalid decision_policy")
        normalized: dict[OcrFieldReason, int] = {}
        for key, count in self.reason_counts.items():
            if type(key) is not OcrFieldReason:
                raise ValueError("reason_counts keys must be OcrFieldReason")
            checked = _bounded_int(
                count, f"reason_counts[{key.value}]", minimum=0, maximum=TOTAL_LITERAL_FIELDS
            )
            if checked > 0:
                normalized[key] = checked
        if self.primary_status is OcrPrimaryStatus.COMPLETED:
            if sum(normalized.values()) != TOTAL_LITERAL_FIELDS:
                raise ValueError("completed primary observation must classify all 11 fields")
        elif normalized:
            raise ValueError("unsuccessful primary observation must not carry field counts")
        object.__setattr__(self, "reason_counts", normalized)


@dataclass(frozen=True)
class OcrObservation:
    """Persisted safe OCR routing observation linked to opaque journey and request."""

    observation_id: str
    request_id: str
    journey_id: str
    operation_status: str
    routing_mode: OcrRoutingMode
    primary_model: str | None
    secondary_model: str | None
    primary_status: OcrPrimaryStatus
    escalation_status: OcrEscalationStatus
    coordinator_duration_ms: int | None
    primary_duration_ms: int | None
    primary_start_offset_ms: int | None
    primary_end_offset_ms: int | None
    escalation_duration_ms: int | None
    escalation_start_offset_ms: int | None
    escalation_end_offset_ms: int | None
    shown_suggestion_count: int
    manual_entry_field_count: int
    reason_counts: dict[OcrFieldReason, int]
    decision_policy: str
    schema_version: int
    recorded_at: datetime


@dataclass(frozen=True)
class OcrObservationRead:
    """Owner-safe OCR observation status for an intake extract claim."""

    request_id: str
    operation_status: str
    coverage: OcrObservationCoverage
    observation: OcrObservation | None


@dataclass(frozen=True)
class ReviewSession:
    """Persisted pinned TicketFactReview canary/baseline assignment for one session."""

    session_id: str
    client_session_key: str
    journey_id: str | None
    policy_mode: ReviewCanaryPolicy
    canary_percentage: int
    canary_version: str
    variant: ReviewVariant
    effective_treatment_version: str
    eligibility_bucket: int
    reading_request_id: str | None
    workspace_revision: int
    schema_version: int
    assigned_at: datetime


@dataclass(frozen=True)
class ReviewObservationInput:
    """Closed, bounded client review observation payload."""

    client_observation_id: str
    event_kind: ReviewObservationEventKind
    treatment_variant: ReviewVariant
    treatment_version: str
    rendered_variant: ReviewVariant
    outcome: ReviewObservationOutcome
    visible_elapsed_ms: int | None
    busy_elapsed_ms: int | None
    hidden_elapsed_ms: int | None
    visibility_changes: int
    timing_state: ReviewTimingState
    invalid_field_count: int
    customer_supplied: bool
    reading_status: str
    review_status: str

    def __post_init__(self) -> None:
        _validate_client_nonce(self.client_observation_id)
        if type(self.event_kind) is not ReviewObservationEventKind:
            raise ValueError("invalid event_kind")
        if type(self.treatment_variant) is not ReviewVariant:
            raise ValueError("invalid treatment_variant")
        if type(self.treatment_version) is not str or not re.fullmatch(
            r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$", self.treatment_version
        ):
            raise ValueError("invalid treatment_version")
        if type(self.rendered_variant) is not ReviewVariant:
            raise ValueError("invalid rendered_variant")
        if type(self.outcome) is not ReviewObservationOutcome:
            raise ValueError("invalid outcome")
        if type(self.timing_state) is not ReviewTimingState:
            raise ValueError("invalid timing_state")
        for name, value in (
            ("visible_elapsed_ms", self.visible_elapsed_ms),
            ("busy_elapsed_ms", self.busy_elapsed_ms),
            ("hidden_elapsed_ms", self.hidden_elapsed_ms),
        ):
            if value is not None:
                _bounded_int(value, name, minimum=0, maximum=MAX_CLIENT_DURATION_MS)
        _bounded_int(
            self.visibility_changes,
            "visibility_changes",
            minimum=0,
            maximum=MAX_VISIBILITY_CHANGES,
        )
        _bounded_int(self.invalid_field_count, "invalid_field_count", minimum=0, maximum=13)
        if type(self.customer_supplied) is not bool:
            raise ValueError("customer_supplied must be a bool")
        valid_statuses = {"missing", "pending", "success", "refused", "unknown"}
        if self.reading_status not in valid_statuses or self.review_status not in valid_statuses:
            raise ValueError("invalid reading or review status")
        if self.event_kind is ReviewObservationEventKind.EXPOSURE and (
            self.outcome is not ReviewObservationOutcome.OBSERVED
        ):
            raise ValueError("exposure observation must have outcome observed")
        if self.event_kind is ReviewObservationEventKind.VALIDATION_FAILED and (
            self.outcome is not ReviewObservationOutcome.CLIENT_INVALID
            or self.invalid_field_count < 1
        ):
            raise ValueError("validation_failed requires client_invalid and invalid_field_count>=1")
        if (
            self.event_kind is ReviewObservationEventKind.CONFIRM_ATTEMPT
            and self.outcome is ReviewObservationOutcome.CLIENT_REPORTED_SUCCESS
            and self.review_status != "success"
        ):
            raise ValueError(
                "client_reported_success observation requires successful review_status"
            )
        if self.timing_state is ReviewTimingState.CLOSED and (
            self.visible_elapsed_ms is None
            or self.busy_elapsed_ms is None
            or self.hidden_elapsed_ms is None
        ):
            raise ValueError("closed timing_state requires visible, busy, and hidden timings")


@dataclass(frozen=True)
class ReviewObservation:
    """Persisted, explicitly labeled client review observation."""

    observation_id: str
    session_id: str
    client_observation_id: str
    seq: int
    source: str
    measurement_label: str
    event_kind: ReviewObservationEventKind
    treatment_variant: ReviewVariant
    treatment_version: str
    rendered_variant: ReviewVariant
    outcome: ReviewObservationOutcome
    visible_elapsed_ms: int | None
    busy_elapsed_ms: int | None
    hidden_elapsed_ms: int | None
    visibility_changes: int
    timing_state: ReviewTimingState
    invalid_field_count: int
    customer_supplied: bool
    reading_status: str
    review_status: str
    schema_version: int
    recorded_at: datetime


@dataclass(frozen=True)
class TicketObservabilitySnapshot:
    """Owner-safe snapshot of OCR routing observations and review canary sessions."""

    journey_id: str | None
    ocr_observations: tuple[OcrObservationRead, ...]
    review_sessions: tuple[ReviewSession, ...]
    review_observations: tuple[ReviewObservation, ...]


def _field_reasons_for_routed(
    first: TicketReading,
    second: TicketReading | None,
    policy: OcrPolicy,
    esc_status: OcrEscalationStatus,
) -> dict[OcrFieldReason, int]:
    counts: dict[OcrFieldReason, int] = {}

    def bump(reason: OcrFieldReason) -> None:
        counts[reason] = counts.get(reason, 0) + 1

    borderline = {
        f.field
        for f in first.fields
        if f.state is ReadingState.SHOWN
        and f.confidence is not None
        and policy.manual_below <= f.confidence < policy.escalate_below
    }
    corroboration = {f.field: f for f in second.fields} if second and not second.refused else {}
    for field in first.fields:
        if field.state is ReadingState.NOT_SHOWN:
            bump(OcrFieldReason.FIELD_NOT_SHOWN)
            continue
        if field.state is ReadingState.UNREADABLE:
            bump(OcrFieldReason.FIELD_UNREADABLE)
            continue
        independent = corroboration.get(field.field)
        if independent is not None and (
            independent.state is not ReadingState.SHOWN or independent.value != field.value
        ):
            bump(OcrFieldReason.INDEPENDENT_DISAGREEMENT)
            continue
        if field.confidence is not None and field.confidence >= policy.escalate_below:
            bump(OcrFieldReason.HIGH_CONFIDENCE_SUGGESTION)
            continue
        other = corroboration.get(field.field) if field.field in borderline else None
        if (
            other is not None
            and other.state is ReadingState.SHOWN
            and other.value == field.value
            and other.confidence is not None
            and other.confidence >= policy.escalate_below
        ):
            bump(OcrFieldReason.CORROBORATED_SUGGESTION)
            continue
        if field.confidence is None:
            bump(OcrFieldReason.FIELD_MISSING_CONFIDENCE)
        elif field.confidence < policy.manual_below:
            bump(OcrFieldReason.FIELD_LOW_CONFIDENCE)
        elif esc_status is OcrEscalationStatus.UNAVAILABLE:
            bump(OcrFieldReason.ESCALATION_UNAVAILABLE)
        elif esc_status is OcrEscalationStatus.BUDGET_EXHAUSTED:
            bump(OcrFieldReason.ESCALATION_BUDGET_EXHAUSTED)
        elif esc_status is OcrEscalationStatus.FAILED:
            bump(OcrFieldReason.ESCALATION_FAILED)
        elif esc_status is OcrEscalationStatus.REFUSED:
            bump(OcrFieldReason.ESCALATION_REFUSED)
        elif other is not None and other.confidence is None:
            bump(OcrFieldReason.FIELD_MISSING_CONFIDENCE)
        else:
            bump(OcrFieldReason.FIELD_LOW_CONFIDENCE)
    return counts


def _field_reasons_for_single_stage(
    reading: TicketReading, policy: OcrPolicy
) -> dict[OcrFieldReason, int]:
    # Single-stage readers do not apply routing thresholds; do not invent a
    # confidence decision when their shown suggestions were returned unchanged.
    counts: dict[OcrFieldReason, int] = {}
    for field in reading.fields:
        reason = (
            OcrFieldReason.FIELD_NOT_SHOWN
            if field.state is ReadingState.NOT_SHOWN
            else OcrFieldReason.FIELD_UNREADABLE
            if field.state is ReadingState.UNREADABLE
            else OcrFieldReason.UNROUTED_SUGGESTION
        )
        counts[reason] = counts.get(reason, 0) + 1
    return counts


def classify_ocr_observation(
    *,
    routed_trace: RoutedExtractTrace | None = None,
    single_trace: ExtractTrace | None = None,
    reading: TicketReading | None = None,
    coord_start: float | None = None,
    coord_end: float | None = None,
    exception_kind: str | None = None,
    policy: OcrPolicy | None = None,
) -> OcrObservationInput:
    """Build a closed, privacy-safe OCR routing and timing observation from reader traces."""
    active_policy = policy if policy is not None else OcrPolicy()
    coord_ms = _duration_ms(coord_start, coord_end, "coordinator_duration_ms")

    if routed_trace is not None:
        primary_status = OcrPrimaryStatus(routed_trace.primary_status)
        if routed_trace.escalation_exception_kind == "budget_exhausted":
            esc_status = OcrEscalationStatus.BUDGET_EXHAUSTED
        else:
            esc_status = OcrEscalationStatus(routed_trace.escalation_status)
        primary_ms = _duration_ms(
            routed_trace.primary_started_monotonic,
            routed_trace.primary_ended_monotonic,
            "primary_duration_ms",
        )
        p_off_s, p_off_e = _offsets_ms(
            coord_start,
            coord_end,
            routed_trace.primary_started_monotonic,
            routed_trace.primary_ended_monotonic,
        )
        esc_ms = _duration_ms(
            routed_trace.escalation_started_monotonic,
            routed_trace.escalation_ended_monotonic,
            "escalation_duration_ms",
        )
        e_off_s, e_off_e = _offsets_ms(
            coord_start,
            coord_end,
            routed_trace.escalation_started_monotonic,
            routed_trace.escalation_ended_monotonic,
        )
        first = routed_trace.first_reading
        final = routed_trace.final_reading
        if primary_status is OcrPrimaryStatus.COMPLETED and first is not None:
            reason_counts = _field_reasons_for_routed(
                first, routed_trace.second_reading, routed_trace.policy, esc_status
            )
            shown_count = (
                sum(1 for item in final.fields if item.state is ReadingState.SHOWN)
                if final is not None
                else 0
            )
        else:
            reason_counts = {}
            shown_count = 0
        return OcrObservationInput(
            routing_mode=OcrRoutingMode.ROUTED,
            primary_model=routed_trace.primary_model,
            secondary_model=routed_trace.secondary_model,
            primary_status=primary_status,
            escalation_status=esc_status,
            coordinator_duration_ms=coord_ms,
            primary_duration_ms=primary_ms,
            primary_start_offset_ms=p_off_s,
            primary_end_offset_ms=p_off_e,
            escalation_duration_ms=esc_ms,
            escalation_start_offset_ms=e_off_s,
            escalation_end_offset_ms=e_off_e,
            shown_suggestion_count=shown_count,
            manual_entry_field_count=TOTAL_LITERAL_FIELDS - shown_count,
            reason_counts=reason_counts,
            decision_policy=(
                f"ocr-routing-v1:{routed_trace.policy.manual_below}:"
                f"{routed_trace.policy.escalate_below}"
            ),
        )

    if single_trace is not None:
        if single_trace.exception_kind is not None:
            p_status = OcrPrimaryStatus(single_trace.exception_kind)
            counts: dict[OcrFieldReason, int] = {}
            shown_count = 0
        elif single_trace.reading is not None and single_trace.reading.refused:
            p_status = OcrPrimaryStatus.REFUSED
            counts = {}
            shown_count = 0
        elif single_trace.reading is not None:
            p_status = OcrPrimaryStatus.COMPLETED
            counts = _field_reasons_for_single_stage(single_trace.reading, active_policy)
            shown_count = sum(
                1 for item in single_trace.reading.fields if item.state is ReadingState.SHOWN
            )
        else:
            p_status = OcrPrimaryStatus.PROVIDER_ERROR
            counts = {}
            shown_count = 0
        primary_ms = _duration_ms(
            single_trace.started_monotonic, single_trace.ended_monotonic, "primary_duration_ms"
        )
        p_off_s, p_off_e = _offsets_ms(
            coord_start,
            coord_end,
            single_trace.started_monotonic,
            single_trace.ended_monotonic,
        )
        model = single_trace.reading.primary_model if single_trace.reading is not None else None
        return OcrObservationInput(
            routing_mode=OcrRoutingMode.SINGLE_STAGE,
            primary_model=_validate_model_metadata(model, "primary_model"),
            secondary_model=None,
            primary_status=p_status,
            escalation_status=OcrEscalationStatus.NOT_APPLICABLE,
            coordinator_duration_ms=coord_ms,
            primary_duration_ms=primary_ms,
            primary_start_offset_ms=p_off_s,
            primary_end_offset_ms=p_off_e,
            escalation_duration_ms=None,
            escalation_start_offset_ms=None,
            escalation_end_offset_ms=None,
            shown_suggestion_count=shown_count,
            manual_entry_field_count=TOTAL_LITERAL_FIELDS - shown_count,
            reason_counts=counts,
            decision_policy=f"single-stage-v1:{active_policy.manual_below}:{active_policy.escalate_below}",
        )

    # Fallback for custom injected readers without trace attributes.
    if exception_kind is not None:
        p_status = OcrPrimaryStatus(exception_kind)
        counts = {}
        shown_count = 0
        model = None
    elif reading is not None and reading.refused:
        p_status = OcrPrimaryStatus.REFUSED
        counts = {}
        shown_count = 0
        model = reading.primary_model
    elif reading is not None:
        p_status = OcrPrimaryStatus.COMPLETED
        counts = _field_reasons_for_single_stage(reading, active_policy)
        shown_count = sum(1 for item in reading.fields if item.state is ReadingState.SHOWN)
        model = reading.primary_model
    else:
        p_status = OcrPrimaryStatus.PROVIDER_ERROR
        counts = {}
        shown_count = 0
        model = None
    return OcrObservationInput(
        routing_mode=OcrRoutingMode.SINGLE_STAGE,
        primary_model=_validate_model_metadata(model, "primary_model"),
        secondary_model=None,
        primary_status=p_status,
        escalation_status=OcrEscalationStatus.NOT_APPLICABLE,
        coordinator_duration_ms=coord_ms,
        primary_duration_ms=None,
        primary_start_offset_ms=None,
        primary_end_offset_ms=None,
        escalation_duration_ms=None,
        escalation_start_offset_ms=None,
        escalation_end_offset_ms=None,
        shown_suggestion_count=shown_count,
        manual_entry_field_count=TOTAL_LITERAL_FIELDS - shown_count,
        reason_counts=counts,
        decision_policy=f"single-stage-v1:{active_policy.manual_below}:{active_policy.escalate_below}",
    )


def build_ocr_observation_from_reader(
    reader: TicketVisionReader,
    *,
    reading: TicketReading | None = None,
    coord_start: float | None = None,
    coord_end: float | None = None,
    exception_kind: str | None = None,
    policy: OcrPolicy | None = None,
) -> OcrObservationInput:
    """Extract an observation from either RoutedTicketReader or TicketVisionReader."""
    routed_trace = reader.last_routed_trace if isinstance(reader, RoutedTicketReader) else None
    single_trace = (
        reader.last_extract_trace
        if isinstance(reader, TicketVisionReader) and not isinstance(reader, RoutedTicketReader)
        else None
    )
    return classify_ocr_observation(
        routed_trace=routed_trace,
        single_trace=single_trace,
        reading=reading,
        coord_start=coord_start,
        coord_end=coord_end,
        exception_kind=exception_kind,
        policy=policy if policy is not None else getattr(reader, "policy", None),
    )


def _table_exists(database: sqlite3.Connection, name: str) -> bool:
    return (
        database.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _assert_initialized(database: sqlite3.Connection) -> None:
    if not _table_exists(database, "pb_store_versions"):
        raise StorageCorruption("missing ticket observability prerequisites")
    version = database.execute(
        "SELECT version FROM pb_store_versions WHERE component=?", (COMPONENT,)
    ).fetchone()
    if version != (SCHEMA_VERSION,):
        raise StorageCorruption("ticket observability schema is not initialized")
    for name, _ddl in _DDL:
        if not _table_exists(database, name):
            raise StorageCorruption(f"ticket observability table missing: {name}")


def initialize_ticket_observability(database: sqlite3.Connection) -> None:
    """Create or validate the append-only OCR/review observability tables."""
    if not isinstance(database, sqlite3.Connection):
        raise TypeError("ticket observability needs the authority SQLite connection")
    if database.in_transaction:
        raise StorageCorruption("ticket observability initialization needs its own transaction")
    database.execute("BEGIN IMMEDIATE")
    try:
        for req in (
            "pb_store_versions",
            "pb_intake_owners",
            "pb_ticket_intake_reservations",
            "pb_ticket_intake_operations",
            "pb_journey_links",
        ):
            if not _table_exists(database, req):
                raise StorageCorruption("missing ticket observability prerequisites")
        check_versions_schema(database)
        version = database.execute(
            "SELECT version FROM pb_store_versions WHERE component=?", (COMPONENT,)
        ).fetchone()
        tables = {
            str(row[0]): row[1]
            for row in database.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table' "
                "AND name IN ('pb_ticket_ocr_observations', 'pb_ticket_review_sessions', "
                "'pb_ticket_review_observations')"
            )
            if isinstance(row[1], str)
        }
        expected = {name: " ".join(ddl.upper().split()) for name, ddl in _DDL}
        actual = {name: " ".join(sql.upper().split()) for name, sql in tables.items()}
        if version is None:
            if tables:
                raise StorageCorruption("unversioned ticket observability schema")
            for _name, ddl in _DDL:
                database.execute(ddl)
            database.execute(
                "INSERT INTO pb_store_versions (component, version) VALUES (?, ?)",
                (COMPONENT, SCHEMA_VERSION),
            )
        elif version != (SCHEMA_VERSION,) or actual != expected:
            raise StorageCorruption("malformed ticket observability schema")
        for table, message in (
            ("pb_ticket_ocr_observations", "ticket ocr observations are append-only"),
            ("pb_ticket_review_sessions", "ticket review sessions are immutable"),
            ("pb_ticket_review_observations", "ticket review observations are append-only"),
        ):
            for action in ("UPDATE", "DELETE"):
                database.execute(
                    f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} "
                    f"BEFORE {action} ON {table} "
                    f"BEGIN SELECT RAISE(ABORT, '{message}'); END"
                )
        database.execute("COMMIT")
    except Exception:
        if database.in_transaction:
            rollback_preserving_error(database)
        raise


def _ocr_content_sha256(
    request_id: str,
    journey_id: str,
    operation_status: str,
    observation: OcrObservationInput,
) -> str:
    payload = json.dumps(
        {
            "request_id": request_id,
            "journey_id": journey_id,
            "operation_status": operation_status,
            "routing_mode": observation.routing_mode.value,
            "primary_model": observation.primary_model,
            "secondary_model": observation.secondary_model,
            "primary_status": observation.primary_status.value,
            "escalation_status": observation.escalation_status.value,
            "coordinator_duration_ms": observation.coordinator_duration_ms,
            "primary_duration_ms": observation.primary_duration_ms,
            "primary_start_offset_ms": observation.primary_start_offset_ms,
            "primary_end_offset_ms": observation.primary_end_offset_ms,
            "escalation_duration_ms": observation.escalation_duration_ms,
            "escalation_start_offset_ms": observation.escalation_start_offset_ms,
            "escalation_end_offset_ms": observation.escalation_end_offset_ms,
            "shown_suggestion_count": observation.shown_suggestion_count,
            "manual_entry_field_count": observation.manual_entry_field_count,
            "reason_counts": {k.value: v for k, v in sorted(observation.reason_counts.items())},
            "decision_policy": observation.decision_policy,
            "schema_version": SCHEMA_VERSION,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def _decode_reason_counts(raw: object) -> dict[OcrFieldReason, int]:
    if type(raw) is not str:
        raise StorageCorruption("malformed stored reason counts")
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise StorageCorruption("malformed stored reason counts") from exc
    if not isinstance(parsed, dict):
        raise StorageCorruption("malformed stored reason counts")
    decoded: dict[OcrFieldReason, int] = {}
    for key, value in parsed.items():
        try:
            reason = OcrFieldReason(key)
            count = _bounded_int(value, "reason_count", minimum=0, maximum=TOTAL_LITERAL_FIELDS)
        except ValueError as exc:
            raise StorageCorruption(f"malformed stored reason count: {exc}") from exc
        if count > 0:
            decoded[reason] = count
    return decoded


def _decode_ocr_row(row: tuple[Any, ...]) -> OcrObservation:
    if len(row) != 22:
        raise StorageCorruption("malformed stored ocr observation")
    (
        observation_id,
        request_id,
        journey_id,
        operation_status,
        routing_mode,
        primary_model,
        secondary_model,
        primary_status,
        escalation_status,
        coordinator_duration_ms,
        primary_duration_ms,
        primary_start_offset_ms,
        primary_end_offset_ms,
        escalation_duration_ms,
        escalation_start_offset_ms,
        escalation_end_offset_ms,
        shown_suggestion_count,
        manual_entry_field_count,
        reason_counts_json,
        decision_policy,
        schema_version,
        recorded_text,
    ) = row
    try:
        if type(observation_id) is not str or _OBSERVATION_ID_RE.fullmatch(observation_id) is None:
            raise ValueError("invalid observation_id")
        if type(journey_id) is not str or _JOURNEY_ID_RE.fullmatch(journey_id) is None:
            raise ValueError("invalid journey_id")
        _validate_opaque_key(request_id, "request_id")
        if operation_status not in ("success", "refused", "unknown"):
            raise ValueError("invalid operation_status")
        reason_counts = _decode_reason_counts(reason_counts_json)
        obs = OcrObservation(
            observation_id=observation_id,
            request_id=request_id,
            journey_id=journey_id,
            operation_status=operation_status,
            routing_mode=OcrRoutingMode(routing_mode),
            primary_model=_validate_model_metadata(primary_model, "primary_model"),
            secondary_model=_validate_model_metadata(secondary_model, "secondary_model"),
            primary_status=OcrPrimaryStatus(primary_status),
            escalation_status=OcrEscalationStatus(escalation_status),
            coordinator_duration_ms=coordinator_duration_ms,
            primary_duration_ms=primary_duration_ms,
            primary_start_offset_ms=primary_start_offset_ms,
            primary_end_offset_ms=primary_end_offset_ms,
            escalation_duration_ms=escalation_duration_ms,
            escalation_start_offset_ms=escalation_start_offset_ms,
            escalation_end_offset_ms=escalation_end_offset_ms,
            shown_suggestion_count=shown_suggestion_count,
            manual_entry_field_count=manual_entry_field_count,
            reason_counts=reason_counts,
            decision_policy=decision_policy,
            schema_version=_bounded_int(
                schema_version, "schema_version", minimum=SCHEMA_VERSION, maximum=SCHEMA_VERSION
            ),
            recorded_at=_require_utc(_parse_datetime(recorded_text, "recorded_at"), "recorded_at"),
        )
        return obs
    except (TypeError, ValueError) as exc:
        raise StorageCorruption(f"malformed stored ocr observation: {exc}") from exc


def record_ocr_observation_in_transaction(
    database: sqlite3.Connection,
    *,
    request_id: str,
    intake_id: str,
    workspace_token: str,
    journey_id: str,
    operation_status: str,
    observation: OcrObservationInput,
    recorded_at: datetime,
) -> OcrObservation:
    """Persist one OCR observation atomically inside the caller's open transaction."""
    if not database.in_transaction:
        raise StorageCorruption("ocr observation append requires active transaction")
    _assert_initialized(database)
    assert_journey_initialized(database)
    req = _validate_opaque_key(request_id, "request_id")
    subj = _validate_opaque_key(intake_id, "intake_id")
    workspace = _validate_workspace_token(workspace_token)
    if type(journey_id) is not str or _JOURNEY_ID_RE.fullmatch(journey_id) is None:
        raise ValueError("invalid journey_id")
    if operation_status not in ("success", "refused", "unknown"):
        raise ValueError("invalid operation_status")
    if type(observation) is not OcrObservationInput:
        raise ValueError("observation must be an OcrObservationInput")
    recorded = _to_utc(recorded_at, "recorded_at")
    content_sha = _ocr_content_sha256(req, journey_id, operation_status, observation)

    existing = database.execute(
        "SELECT content_sha256, observation_id, request_id, journey_id, operation_status, "
        "routing_mode, primary_model, secondary_model, primary_status, escalation_status, "
        "coordinator_duration_ms, primary_duration_ms, primary_start_offset_ms, "
        "primary_end_offset_ms, escalation_duration_ms, escalation_start_offset_ms, "
        "escalation_end_offset_ms, shown_suggestion_count, manual_entry_field_count, "
        "reason_counts_json, decision_policy, schema_version, recorded_at "
        "FROM pb_ticket_ocr_observations WHERE request_id=?",
        (req,),
    ).fetchone()
    if existing is not None:
        stored_sha = existing[0]
        if type(stored_sha) is not str or _SHA256_RE.fullmatch(stored_sha) is None:
            raise StorageCorruption("malformed stored ocr observation digest")
        stored_obs = _decode_ocr_row(existing[1:])
        if stored_sha != content_sha:
            raise TicketObservabilityConflict("ocr_observation_conflict")
        return stored_obs

    observation_id = f"obs_{uuid.uuid4().hex}"
    reason_json = json.dumps(
        {k.value: v for k, v in sorted(observation.reason_counts.items())},
        separators=(",", ":"),
        sort_keys=True,
    )
    database.execute(
        "INSERT INTO pb_ticket_ocr_observations ("
        "request_id, observation_id, intake_id, workspace_token, journey_id, operation_status, "
        "routing_mode, primary_model, secondary_model, primary_status, escalation_status, "
        "coordinator_duration_ms, primary_duration_ms, primary_start_offset_ms, "
        "primary_end_offset_ms, escalation_duration_ms, escalation_start_offset_ms, "
        "escalation_end_offset_ms, shown_suggestion_count, manual_entry_field_count, "
        "reason_counts_json, decision_policy, content_sha256, schema_version, recorded_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            req,
            observation_id,
            subj,
            workspace,
            journey_id,
            operation_status,
            observation.routing_mode.value,
            observation.primary_model,
            observation.secondary_model,
            observation.primary_status.value,
            observation.escalation_status.value,
            observation.coordinator_duration_ms,
            observation.primary_duration_ms,
            observation.primary_start_offset_ms,
            observation.primary_end_offset_ms,
            observation.escalation_duration_ms,
            observation.escalation_start_offset_ms,
            observation.escalation_end_offset_ms,
            observation.shown_suggestion_count,
            observation.manual_entry_field_count,
            reason_json,
            observation.decision_policy,
            content_sha,
            SCHEMA_VERSION,
            recorded.isoformat(),
        ),
    )
    return OcrObservation(
        observation_id=observation_id,
        request_id=req,
        journey_id=journey_id,
        operation_status=operation_status,
        routing_mode=observation.routing_mode,
        primary_model=observation.primary_model,
        secondary_model=observation.secondary_model,
        primary_status=observation.primary_status,
        escalation_status=observation.escalation_status,
        coordinator_duration_ms=observation.coordinator_duration_ms,
        primary_duration_ms=observation.primary_duration_ms,
        primary_start_offset_ms=observation.primary_start_offset_ms,
        primary_end_offset_ms=observation.primary_end_offset_ms,
        escalation_duration_ms=observation.escalation_duration_ms,
        escalation_start_offset_ms=observation.escalation_start_offset_ms,
        escalation_end_offset_ms=observation.escalation_end_offset_ms,
        shown_suggestion_count=observation.shown_suggestion_count,
        manual_entry_field_count=observation.manual_entry_field_count,
        reason_counts=dict(observation.reason_counts),
        decision_policy=observation.decision_policy,
        schema_version=SCHEMA_VERSION,
        recorded_at=recorded,
    )


def _require_workspace_owner(
    database: sqlite3.Connection, workspace_token: str, owner: str | None
) -> str:
    row = database.execute(
        "SELECT owner FROM pb_intake_owners WHERE workspace_token=?",
        (workspace_token,),
    ).fetchone()
    if row is None or type(row[0]) is not str or not row[0]:
        raise TicketObservabilityAccessError("not_found")
    if owner is not None and row[0] != owner:
        raise TicketObservabilityAccessError("not_found")
    return row[0]


def _lookup_journey_id(database: sqlite3.Connection, workspace_token: str) -> str | None:
    row = database.execute(
        "SELECT journey_id FROM pb_journey_links WHERE workspace_token=?",
        (workspace_token,),
    ).fetchone()
    if row is None:
        return None
    if type(row[0]) is not str or _JOURNEY_ID_RE.fullmatch(row[0]) is None:
        raise StorageCorruption("malformed stored journey link")
    return row[0]


def _decode_review_session(row: tuple[Any, ...]) -> ReviewSession:
    if len(row) != 13:
        raise StorageCorruption("malformed stored review session")
    (
        session_id,
        client_session_key,
        journey_id,
        policy_mode,
        canary_percentage,
        canary_version,
        variant,
        effective_treatment_version,
        eligibility_bucket,
        reading_request_id,
        workspace_revision,
        schema_version,
        assigned_text,
    ) = row
    try:
        if type(session_id) is not str or _SESSION_ID_RE.fullmatch(session_id) is None:
            raise ValueError("invalid session_id")
        _validate_client_nonce(client_session_key)
        if journey_id is not None and (
            type(journey_id) is not str or _JOURNEY_ID_RE.fullmatch(journey_id) is None
        ):
            raise ValueError("invalid journey_id")
        if reading_request_id is not None:
            _validate_opaque_key(reading_request_id, "reading_request_id")
        return ReviewSession(
            session_id=session_id,
            client_session_key=client_session_key,
            journey_id=journey_id,
            policy_mode=ReviewCanaryPolicy(policy_mode),
            canary_percentage=_bounded_int(
                canary_percentage, "canary_percentage", minimum=0, maximum=100
            ),
            canary_version=canary_version,
            variant=ReviewVariant(variant),
            effective_treatment_version=effective_treatment_version,
            eligibility_bucket=_bounded_int(
                eligibility_bucket, "eligibility_bucket", minimum=0, maximum=99
            ),
            reading_request_id=reading_request_id,
            workspace_revision=_bounded_int(
                workspace_revision, "workspace_revision", minimum=1, maximum=MAX_BACKEND_DURATION_MS
            ),
            schema_version=_bounded_int(
                schema_version, "schema_version", minimum=SCHEMA_VERSION, maximum=SCHEMA_VERSION
            ),
            assigned_at=_require_utc(_parse_datetime(assigned_text, "assigned_at"), "assigned_at"),
        )
    except (TypeError, ValueError) as exc:
        raise StorageCorruption(f"malformed stored review session: {exc}") from exc


def start_or_resume_review_session(
    database: sqlite3.Connection,
    *,
    workspace_token: str,
    owner: str | None = None,
    client_session_key: str,
    config: ReviewCanaryConfig,
    now: datetime,
    workspace_revision: int = 1,
    reading_request_id: str | None = None,
) -> ReviewSession:
    """Start or idempotently resume a pinned TicketFactReview session.

    Existing sessions stay pinned to their persisted variant and treatment version even
    if the server canary policy subsequently rolls back to baseline. Never modifies
    workspace, case, approval, financial, or mail tables.
    """
    workspace = _validate_workspace_token(workspace_token)
    expected_owner = None if owner is None else _validate_owner(owner)
    client_key = _validate_client_nonce(client_session_key)
    if type(config) is not ReviewCanaryConfig:
        raise ValueError("config must be a ReviewCanaryConfig")
    at = _to_utc(now, "now")
    rev = _bounded_int(workspace_revision, "workspace_revision", minimum=1, maximum=10**9)
    read_req = (
        None
        if reading_request_id is None
        else _validate_opaque_key(reading_request_id, "reading_request_id")
    )
    if database.in_transaction:
        raise StorageCorruption("review session start requires no active caller transaction")
    database.execute("BEGIN IMMEDIATE")
    try:
        _assert_initialized(database)
        actual_owner = _require_workspace_owner(database, workspace, expected_owner)
        existing = database.execute(
            "SELECT workspace_token, owner, session_id, client_session_key, journey_id, "
            "policy_mode, canary_percentage, canary_version, variant, "
            "effective_treatment_version, eligibility_bucket, reading_request_id, "
            "workspace_revision, schema_version, assigned_at "
            "FROM pb_ticket_review_sessions WHERE client_session_key=?",
            (client_key,),
        ).fetchone()
        if existing is not None:
            if existing[0] != workspace or existing[1] != actual_owner:
                raise TicketObservabilityConflict("review_session_conflict")
            session = _decode_review_session(existing[2:])
            database.execute("COMMIT")
            return session

        count = database.execute(
            "SELECT COUNT(*) FROM pb_ticket_review_sessions WHERE workspace_token=?",
            (workspace,),
        ).fetchone()[0]
        if count >= MAX_SESSIONS_PER_WORKSPACE:
            raise TicketObservabilityConflict("review_session_limit_reached")
        journey_id = _lookup_journey_id(database, workspace)
        bucket = deterministic_eligibility_bucket(workspace, config.version)
        if config.policy is ReviewCanaryPolicy.CANARY and bucket < config.canary_percentage:
            variant = ReviewVariant.MANUAL_GUIDANCE_V1
        else:
            variant = ReviewVariant.BASELINE
        effective_version = f"{variant.value}:{config.version}"
        session_id = f"rvs_{uuid.uuid4().hex}"
        database.execute(
            "INSERT INTO pb_ticket_review_sessions ("
            "session_id, client_session_key, workspace_token, owner, journey_id, "
            "policy_mode, canary_percentage, canary_version, variant, "
            "effective_treatment_version, eligibility_bucket, reading_request_id, "
            "workspace_revision, schema_version, assigned_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                client_key,
                workspace,
                actual_owner,
                journey_id,
                config.policy.value,
                config.canary_percentage,
                config.version,
                variant.value,
                effective_version,
                bucket,
                read_req,
                rev,
                SCHEMA_VERSION,
                at.isoformat(),
            ),
        )
        database.execute("COMMIT")
        return ReviewSession(
            session_id=session_id,
            client_session_key=client_key,
            journey_id=journey_id,
            policy_mode=config.policy,
            canary_percentage=config.canary_percentage,
            canary_version=config.version,
            variant=variant,
            effective_treatment_version=effective_version,
            eligibility_bucket=bucket,
            reading_request_id=read_req,
            workspace_revision=rev,
            schema_version=SCHEMA_VERSION,
            assigned_at=at,
        )
    except Exception:
        if database.in_transaction:
            rollback_preserving_error(database)
        raise


def _review_obs_content_sha256(session_id: str, obs: ReviewObservationInput) -> str:
    payload = json.dumps(
        {
            "session_id": session_id,
            "client_observation_id": obs.client_observation_id,
            "event_kind": obs.event_kind.value,
            "treatment_variant": obs.treatment_variant.value,
            "treatment_version": obs.treatment_version,
            "rendered_variant": obs.rendered_variant.value,
            "outcome": obs.outcome.value,
            "visible_elapsed_ms": obs.visible_elapsed_ms,
            "busy_elapsed_ms": obs.busy_elapsed_ms,
            "hidden_elapsed_ms": obs.hidden_elapsed_ms,
            "visibility_changes": obs.visibility_changes,
            "timing_state": obs.timing_state.value,
            "invalid_field_count": obs.invalid_field_count,
            "customer_supplied": obs.customer_supplied,
            "reading_status": obs.reading_status,
            "review_status": obs.review_status,
            "schema_version": SCHEMA_VERSION,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def _decode_review_observation(row: tuple[Any, ...]) -> ReviewObservation:
    if len(row) != 21:
        raise StorageCorruption("malformed stored review observation")
    (
        observation_id,
        session_id,
        client_observation_id,
        seq,
        source,
        measurement_label,
        event_kind,
        treatment_variant,
        treatment_version,
        rendered_variant,
        outcome,
        visible_elapsed_ms,
        busy_elapsed_ms,
        hidden_elapsed_ms,
        visibility_changes,
        timing_state,
        invalid_field_count,
        customer_supplied_int,
        reading_status,
        review_status,
        recorded_text,
    ) = row
    try:
        if type(observation_id) is not str or _OBSERVATION_ID_RE.fullmatch(observation_id) is None:
            raise ValueError("invalid observation_id")
        if type(session_id) is not str or _SESSION_ID_RE.fullmatch(session_id) is None:
            raise ValueError("invalid session_id")
        if source != CLIENT_SOURCE_LABEL or measurement_label != MEASUREMENT_LABEL:
            raise ValueError("invalid observation labeling")
        if customer_supplied_int not in (0, 1):
            raise ValueError("invalid customer_supplied flag")
        return ReviewObservation(
            observation_id=observation_id,
            session_id=session_id,
            client_observation_id=client_observation_id,
            seq=_bounded_int(seq, "seq", minimum=1, maximum=MAX_OBSERVATIONS_PER_SESSION),
            source=source,
            measurement_label=measurement_label,
            event_kind=ReviewObservationEventKind(event_kind),
            treatment_variant=ReviewVariant(treatment_variant),
            treatment_version=treatment_version,
            rendered_variant=ReviewVariant(rendered_variant),
            outcome=ReviewObservationOutcome(outcome),
            visible_elapsed_ms=visible_elapsed_ms,
            busy_elapsed_ms=busy_elapsed_ms,
            hidden_elapsed_ms=hidden_elapsed_ms,
            visibility_changes=visibility_changes,
            timing_state=ReviewTimingState(timing_state),
            invalid_field_count=invalid_field_count,
            customer_supplied=bool(customer_supplied_int),
            reading_status=reading_status,
            review_status=review_status,
            schema_version=SCHEMA_VERSION,
            recorded_at=_require_utc(_parse_datetime(recorded_text, "recorded_at"), "recorded_at"),
        )
    except (TypeError, ValueError) as exc:
        raise StorageCorruption(f"malformed stored review observation: {exc}") from exc


def record_review_observation(
    database: sqlite3.Connection,
    *,
    workspace_token: str,
    owner: str | None = None,
    session_id: str,
    observation: ReviewObservationInput,
    now: datetime,
) -> ReviewObservation:
    """Persist one bounded, deduplicated client observation for an owned review session."""
    workspace = _validate_workspace_token(workspace_token)
    expected_owner = None if owner is None else _validate_owner(owner)
    if type(session_id) is not str or _SESSION_ID_RE.fullmatch(session_id) is None:
        raise TicketObservabilityAccessError("not_found")
    if type(observation) is not ReviewObservationInput:
        raise ValueError("observation must be a ReviewObservationInput")
    at = _to_utc(now, "now")
    if database.in_transaction:
        raise StorageCorruption("review observation write requires no active caller transaction")
    database.execute("BEGIN IMMEDIATE")
    try:
        _assert_initialized(database)
        actual_owner = _require_workspace_owner(database, workspace, expected_owner)
        session_row = database.execute(
            "SELECT workspace_token, owner, session_id, client_session_key, journey_id, "
            "policy_mode, canary_percentage, canary_version, variant, "
            "effective_treatment_version, eligibility_bucket, reading_request_id, "
            "workspace_revision, schema_version, assigned_at "
            "FROM pb_ticket_review_sessions WHERE session_id=?",
            (session_id,),
        ).fetchone()
        if session_row is None or session_row[0] != workspace or session_row[1] != actual_owner:
            raise TicketObservabilityAccessError("not_found")
        session = _decode_review_session(session_row[2:])
        if (
            observation.treatment_variant is not session.variant
            or observation.treatment_version != session.effective_treatment_version
        ):
            raise TicketObservabilityConflict("review_observation_variant_mismatch")
        if (
            observation.rendered_variant is ReviewVariant.MANUAL_GUIDANCE_V1
            and session.variant is not ReviewVariant.MANUAL_GUIDANCE_V1
        ):
            raise TicketObservabilityConflict("review_observation_variant_mismatch")

        content_sha = _review_obs_content_sha256(session_id, observation)
        existing = database.execute(
            "SELECT content_sha256, observation_id, session_id, client_observation_id, seq, "
            "source, measurement_label, event_kind, treatment_variant, "
            "treatment_version, rendered_variant, "
            "outcome, visible_elapsed_ms, busy_elapsed_ms, hidden_elapsed_ms, "
            "visibility_changes, timing_state, invalid_field_count, customer_supplied, "
            "reading_status, review_status, recorded_at "
            "FROM pb_ticket_review_observations WHERE session_id=? AND client_observation_id=?",
            (session_id, observation.client_observation_id),
        ).fetchone()
        if existing is not None:
            stored_sha = existing[0]
            if type(stored_sha) is not str or _SHA256_RE.fullmatch(stored_sha) is None:
                raise StorageCorruption("malformed stored review observation digest")
            decoded = _decode_review_observation(existing[1:])
            if stored_sha != content_sha:
                raise TicketObservabilityConflict("review_observation_conflict")
            database.execute("COMMIT")
            return decoded

        seq_row = database.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM pb_ticket_review_observations "
            "WHERE session_id=?",
            (session_id,),
        ).fetchone()
        if seq_row is None or type(seq_row[0]) is not int or seq_row[0] < 1:
            raise StorageCorruption("invalid stored review observation sequence")
        next_seq = seq_row[0]
        if next_seq > MAX_OBSERVATIONS_PER_SESSION:
            raise TicketObservabilityConflict("review_session_observation_limit_reached")

        observation_id = f"obs_{uuid.uuid4().hex}"
        database.execute(
            "INSERT INTO pb_ticket_review_observations ("
            "observation_id, session_id, client_observation_id, workspace_token, seq, "
            "source, measurement_label, event_kind, treatment_variant, "
            "treatment_version, rendered_variant, "
            "outcome, visible_elapsed_ms, busy_elapsed_ms, hidden_elapsed_ms, "
            "visibility_changes, timing_state, invalid_field_count, customer_supplied, "
            "reading_status, review_status, content_sha256, schema_version, recorded_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                observation_id,
                session_id,
                observation.client_observation_id,
                workspace,
                next_seq,
                CLIENT_SOURCE_LABEL,
                MEASUREMENT_LABEL,
                observation.event_kind.value,
                observation.treatment_variant.value,
                observation.treatment_version,
                observation.rendered_variant.value,
                observation.outcome.value,
                observation.visible_elapsed_ms,
                observation.busy_elapsed_ms,
                observation.hidden_elapsed_ms,
                observation.visibility_changes,
                observation.timing_state.value,
                observation.invalid_field_count,
                1 if observation.customer_supplied else 0,
                observation.reading_status,
                observation.review_status,
                content_sha,
                SCHEMA_VERSION,
                at.isoformat(),
            ),
        )
        database.execute("COMMIT")
        return ReviewObservation(
            observation_id=observation_id,
            session_id=session_id,
            client_observation_id=observation.client_observation_id,
            seq=next_seq,
            source=CLIENT_SOURCE_LABEL,
            measurement_label=MEASUREMENT_LABEL,
            event_kind=observation.event_kind,
            treatment_variant=observation.treatment_variant,
            treatment_version=observation.treatment_version,
            rendered_variant=observation.rendered_variant,
            outcome=observation.outcome,
            visible_elapsed_ms=observation.visible_elapsed_ms,
            busy_elapsed_ms=observation.busy_elapsed_ms,
            hidden_elapsed_ms=observation.hidden_elapsed_ms,
            visibility_changes=observation.visibility_changes,
            timing_state=observation.timing_state,
            invalid_field_count=observation.invalid_field_count,
            customer_supplied=observation.customer_supplied,
            reading_status=observation.reading_status,
            review_status=observation.review_status,
            schema_version=SCHEMA_VERSION,
            recorded_at=at,
        )
    except Exception:
        if database.in_transaction:
            rollback_preserving_error(database)
        raise


def read_ticket_observability(
    database: sqlite3.Connection,
    *,
    workspace_token: str,
    owner: str | None = None,
) -> TicketObservabilitySnapshot:
    """Read safe OCR observations and review session/client observations for an owned workspace.

    Legacy extract operations without a ``pb_ticket_ocr_observations`` row remain readable
    and explicitly report ``coverage=missing``. Pending operations report ``coverage=pending``.
    """
    workspace = _validate_workspace_token(workspace_token)
    expected_owner = None if owner is None else _validate_owner(owner)
    owns_tx = not database.in_transaction
    if owns_tx:
        database.execute("BEGIN")
    try:
        _assert_initialized(database)
        _require_workspace_owner(database, workspace, expected_owner)
        journey_id = _lookup_journey_id(database, workspace)

        op_rows = database.execute(
            "SELECT request_id, status FROM pb_ticket_intake_operations "
            "WHERE workspace_token=? AND operation='extract' ORDER BY rowid ASC",
            (workspace,),
        ).fetchall()
        obs_rows = database.execute(
            "SELECT observation_id, request_id, journey_id, operation_status, "
            "routing_mode, primary_model, secondary_model, primary_status, escalation_status, "
            "coordinator_duration_ms, primary_duration_ms, primary_start_offset_ms, "
            "primary_end_offset_ms, escalation_duration_ms, escalation_start_offset_ms, "
            "escalation_end_offset_ms, shown_suggestion_count, manual_entry_field_count, "
            "reason_counts_json, decision_policy, schema_version, recorded_at "
            "FROM pb_ticket_ocr_observations WHERE workspace_token=? ORDER BY rowid ASC",
            (workspace,),
        ).fetchall()
        obs_by_req = {
            decoded.request_id: decoded for decoded in (_decode_ocr_row(row) for row in obs_rows)
        }
        ocr_reads: list[OcrObservationRead] = []
        for req_id, op_status in op_rows:
            if type(req_id) is not str or type(op_status) is not str:
                raise StorageCorruption("malformed stored extract operation")
            if op_status == "pending":
                ocr_reads.append(
                    OcrObservationRead(
                        request_id=req_id,
                        operation_status="pending",
                        coverage=OcrObservationCoverage.PENDING,
                        observation=None,
                    )
                )
            elif req_id in obs_by_req:
                ocr_reads.append(
                    OcrObservationRead(
                        request_id=req_id,
                        operation_status=op_status,
                        coverage=OcrObservationCoverage.RECORDED,
                        observation=obs_by_req[req_id],
                    )
                )
            else:
                ocr_reads.append(
                    OcrObservationRead(
                        request_id=req_id,
                        operation_status=op_status,
                        coverage=OcrObservationCoverage.MISSING,
                        observation=None,
                    )
                )

        session_rows = database.execute(
            "SELECT session_id, client_session_key, journey_id, policy_mode, "
            "canary_percentage, canary_version, variant, effective_treatment_version, "
            "eligibility_bucket, reading_request_id, workspace_revision, schema_version, "
            "assigned_at FROM pb_ticket_review_sessions "
            "WHERE workspace_token=? ORDER BY rowid ASC",
            (workspace,),
        ).fetchall()
        sessions = tuple(_decode_review_session(row) for row in session_rows)

        client_obs_rows = database.execute(
            "SELECT observation_id, session_id, client_observation_id, seq, "
            "source, measurement_label, event_kind, treatment_variant, "
            "treatment_version, rendered_variant, "
            "outcome, visible_elapsed_ms, busy_elapsed_ms, hidden_elapsed_ms, "
            "visibility_changes, timing_state, invalid_field_count, customer_supplied, "
            "reading_status, review_status, recorded_at "
            "FROM pb_ticket_review_observations WHERE workspace_token=? ORDER BY rowid ASC",
            (workspace,),
        ).fetchall()
        review_obs = tuple(_decode_review_observation(row) for row in client_obs_rows)
        if owns_tx:
            database.execute("COMMIT")
        return TicketObservabilitySnapshot(
            journey_id=journey_id,
            ocr_observations=tuple(ocr_reads),
            review_sessions=sessions,
            review_observations=review_obs,
        )
    except Exception:
        if owns_tx and database.in_transaction:
            rollback_preserving_error(database)
        raise


class TicketObservabilityStoreMixin:
    """Typed observability surface for SqliteTransactionStore."""

    def start_or_resume_review_session(
        self: _ConnectionOwner,
        *,
        workspace_token: str,
        owner: str | None = None,
        client_session_key: str,
        config: ReviewCanaryConfig,
        now: datetime,
        workspace_revision: int = 1,
        reading_request_id: str | None = None,
    ) -> ReviewSession:
        return start_or_resume_review_session(
            self._require_open(),
            workspace_token=workspace_token,
            owner=owner,
            client_session_key=client_session_key,
            config=config,
            now=now,
            workspace_revision=workspace_revision,
            reading_request_id=reading_request_id,
        )

    def record_review_observation(
        self: _ConnectionOwner,
        *,
        workspace_token: str,
        owner: str | None = None,
        session_id: str,
        observation: ReviewObservationInput,
        now: datetime,
    ) -> ReviewObservation:
        return record_review_observation(
            self._require_open(),
            workspace_token=workspace_token,
            owner=owner,
            session_id=session_id,
            observation=observation,
            now=now,
        )

    def ticket_observability(
        self: _ConnectionOwner,
        *,
        workspace_token: str,
        owner: str | None = None,
    ) -> TicketObservabilitySnapshot:
        return read_ticket_observability(
            self._require_open(),
            workspace_token=workspace_token,
            owner=owner,
        )
