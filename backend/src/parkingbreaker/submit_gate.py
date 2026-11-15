"""Submit gate (architecture v6, section 6.2). Pure: no clocks, no Lob, no Stripe.

At the moment of submission every check must hold, or nothing is mailed and the hold is
released. The caller recomputes the inputs now (authorization, window, feasibility) and
passes them in; this module only decides. Every failing check is reported, not just the
first, so the stop reason shown to the customer and logged is complete.

The address check reflects the freeze-time Lob address verification: an unverified or
undeliverable address is a stop, so the gate refuses it too.
"""

from __future__ import annotations

import hmac
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from parkingbreaker.mail_config import LOB_CANCELLATION_WINDOW
from parkingbreaker.research.resolution import ResearchAnswers
from parkingbreaker.research.run import ResearchResult
from parkingbreaker.session_window import WindowStatus

LOB_CUTOFF_TZ = ZoneInfo("America/Los_Angeles")
LOB_DAILY_CUTOFF_TIME = time(10, 0)  # 10:00 AM PT daily SLA cutoff
MIN_HOLD_REMAINING_BEFORE_SEND = timedelta(hours=48)
CAPTURE_FALLBACK_LEAD = timedelta(hours=6)


class GateFailure(StrEnum):
    AUTH_INVALID = "AUTH_INVALID"
    WINDOW_EXPIRED = "WINDOW_EXPIRED"
    ADOPTION_MISMATCH = "ADOPTION_MISMATCH"
    NOT_FEASIBLE = "NOT_FEASIBLE"
    ADDRESS_UNVERIFIED = "ADDRESS_UNVERIFIED"
    RESEARCH_UNRESOLVED = "RESEARCH_UNRESOLVED"
    HOLD_HORIZON_TOO_SHORT = "HOLD_HORIZON_TOO_SHORT"
    # Documented API stop reasons before any payment or mail provider call. They map to the
    # audit's STOP_DECLARATION_UNAFFIRMED and STOP_PACKET_APPROVAL_MISMATCH (the audit's
    # STOP_DECLARATION_UNMATED spelling is the same condition).
    DECLARATION_UNAFFIRMED = "DECLARATION_UNAFFIRMED"
    PACKET_APPROVAL_MISMATCH = "PACKET_APPROVAL_MISMATCH"


class SubmitGateStatus(StrEnum):
    ALLOWED = "ALLOWED"
    BLOCKED_MISSING_EXPIRY = "BLOCKED_MISSING_EXPIRY"
    BLOCKED_NAIVE_TIMESTAMP = "BLOCKED_NAIVE_TIMESTAMP"
    BLOCKED_HOLD_EXPIRED = "BLOCKED_HOLD_EXPIRED"
    BLOCKED_HOLD_HORIZON_TOO_SHORT = "BLOCKED_HOLD_HORIZON_TOO_SHORT"
    BLOCKED_INDUCTION_EXCEEDS_CAPTURE_LEAD = "BLOCKED_INDUCTION_EXCEEDS_CAPTURE_LEAD"


@dataclass(frozen=True)
class SubmitGateDecision:
    status: SubmitGateStatus
    allowed: bool
    remaining_hold: timedelta | None
    required_horizon: timedelta
    estimated_induction_at: datetime | None
    latest_safe_capture_at: datetime | None
    reason_detail: str


def _is_timezone_aware(dt: datetime) -> bool:
    return dt.tzinfo is not None and dt.tzinfo.utcoffset(dt) is not None


def estimate_lob_induction_utc(
    now_utc: datetime,
    cancellation_window: timedelta = LOB_CANCELLATION_WINDOW,
    usps_holidays: frozenset[date] = frozenset(),
) -> datetime:
    """Calculates when a letter created at `now_utc` will enter Lob production
    and receive an initial USPS induction scan, accounting for the cancellation
    window, 10:00 AM PT daily cutoff, weekends, and USPS holidays.
    """
    release_utc = now_utc + cancellation_window
    release_pt = release_utc.astimezone(LOB_CUTOFF_TZ)

    candidate_date = release_pt.date()
    cutoff_today_pt = datetime.combine(candidate_date, LOB_DAILY_CUTOFF_TIME, tzinfo=LOB_CUTOFF_TZ)

    def _is_non_production_day(d: date) -> bool:
        return d.weekday() >= 5 or d in usps_holidays

    if release_pt > cutoff_today_pt or _is_non_production_day(candidate_date):
        candidate_date += timedelta(days=1)
        while _is_non_production_day(candidate_date):
            candidate_date += timedelta(days=1)

    induction_pt = datetime.combine(candidate_date, time(18, 0), tzinfo=LOB_CUTOFF_TZ)
    return induction_pt.astimezone(UTC)


def evaluate_pre_send_hold_gate(
    *,
    now_utc: datetime,
    authorization_expires_at: datetime | None,
    min_remaining: timedelta = MIN_HOLD_REMAINING_BEFORE_SEND,
    cancellation_window: timedelta = LOB_CANCELLATION_WINDOW,
    usps_holidays: frozenset[date] = frozenset(),
) -> SubmitGateDecision:
    """Pure hold-horizon helper (A04-F01 / T16).
    Verifies both the minimum remaining hold floor AND that estimated
    USPS induction occurs before `authorization_expires_at - CAPTURE_FALLBACK_LEAD`.
    """
    if not _is_timezone_aware(now_utc):
        raise ValueError("now_utc must be timezone-aware")

    if authorization_expires_at is None:
        return SubmitGateDecision(
            status=SubmitGateStatus.BLOCKED_MISSING_EXPIRY,
            allowed=False,
            remaining_hold=None,
            required_horizon=min_remaining,
            estimated_induction_at=None,
            latest_safe_capture_at=None,
            reason_detail="Case is missing authorization_expires_at.",
        )

    if not _is_timezone_aware(authorization_expires_at):
        return SubmitGateDecision(
            status=SubmitGateStatus.BLOCKED_NAIVE_TIMESTAMP,
            allowed=False,
            remaining_hold=None,
            required_horizon=min_remaining,
            estimated_induction_at=None,
            latest_safe_capture_at=None,
            reason_detail="authorization_expires_at must be timezone-aware UTC.",
        )

    now_norm = now_utc.astimezone(UTC)
    exp_norm = authorization_expires_at.astimezone(UTC)
    remaining = exp_norm - now_norm

    if remaining <= timedelta(0):
        return SubmitGateDecision(
            status=SubmitGateStatus.BLOCKED_HOLD_EXPIRED,
            allowed=False,
            remaining_hold=remaining,
            required_horizon=min_remaining,
            estimated_induction_at=None,
            latest_safe_capture_at=exp_norm - CAPTURE_FALLBACK_LEAD,
            reason_detail="Stripe authorization hold has already expired.",
        )

    estimated_induction = estimate_lob_induction_utc(now_norm, cancellation_window, usps_holidays)
    latest_safe_capture = exp_norm - CAPTURE_FALLBACK_LEAD

    if remaining < min_remaining:
        return SubmitGateDecision(
            status=SubmitGateStatus.BLOCKED_HOLD_HORIZON_TOO_SHORT,
            allowed=False,
            remaining_hold=remaining,
            required_horizon=min_remaining,
            estimated_induction_at=estimated_induction,
            latest_safe_capture_at=latest_safe_capture,
            reason_detail=(
                f"Remaining hold ({remaining}) is below safety floor ({min_remaining})."
            ),
        )

    if estimated_induction > latest_safe_capture:
        return SubmitGateDecision(
            status=SubmitGateStatus.BLOCKED_INDUCTION_EXCEEDS_CAPTURE_LEAD,
            allowed=False,
            remaining_hold=remaining,
            required_horizon=min_remaining,
            estimated_induction_at=estimated_induction,
            latest_safe_capture_at=latest_safe_capture,
            reason_detail=(
                f"Estimated USPS induction ({estimated_induction.isoformat()}) "
                f"exceeds latest safe capture window ({latest_safe_capture.isoformat()})."
            ),
        )

    return SubmitGateDecision(
        status=SubmitGateStatus.ALLOWED,
        allowed=True,
        remaining_hold=remaining,
        required_horizon=min_remaining,
        estimated_induction_at=estimated_induction,
        latest_safe_capture_at=latest_safe_capture,
        reason_detail="Hold horizon verified safe for Lob dispatch.",
    )


@dataclass(frozen=True)
class GateResult:
    """PASS when failures is empty; otherwise every check that did not hold, in order."""

    failures: tuple[GateFailure, ...]

    @property
    def passed(self) -> bool:
        return not self.failures


PASS = GateResult(failures=())


def hashes_match(adoption_hash: str, frozen_letter_hash: str) -> bool:
    """Constant-time equality. An empty hash never matches: it means nothing was adopted."""
    if not adoption_hash or not frozen_letter_hash:
        return False
    return hmac.compare_digest(adoption_hash.encode(), frozen_letter_hash.encode())


def check_submit_gate(
    *,
    auth_valid: bool,
    window_status: WindowStatus,
    adoption_hash: str,
    frozen_letter_hash: str,
    deadline_feasible_now: bool,
    address_verified: bool,
    research: ResearchResult | None = None,
    frozen_pdf: bytes | None = None,
    research_answers: ResearchAnswers | None = None,
    authorization_expires_at: datetime | None = None,
    now_utc: datetime | None = None,
    usps_holidays: frozenset[date] = frozenset(),
    required_declaration_ids: Sequence[str] = (),
    affirmed_declaration_ids: Sequence[str] = (),
    packet_approval_matched: bool | None = None,
) -> GateResult:
    failures: list[GateFailure] = []
    if not auth_valid:
        failures.append(GateFailure.AUTH_INVALID)
    if window_status is WindowStatus.EXPIRED:
        failures.append(GateFailure.WINDOW_EXPIRED)
    if not hashes_match(adoption_hash, frozen_letter_hash):
        failures.append(GateFailure.ADOPTION_MISMATCH)
    if not deadline_feasible_now:
        failures.append(GateFailure.NOT_FEASIBLE)
    if not address_verified:
        failures.append(GateFailure.ADDRESS_UNVERIFIED)
    # Every required declaration must be explicitly affirmed; anything missing fails closed.
    if set(required_declaration_ids) - set(affirmed_declaration_ids):
        failures.append(GateFailure.DECLARATION_UNAFFIRMED)
    if packet_approval_matched is False:
        failures.append(GateFailure.PACKET_APPROVAL_MISMATCH)
    # Research is mandatory and re-evaluated against the bytes being submitted,
    # never against an earlier caller inventory assertion or an omitted record.
    if (
        research is None
        or frozen_pdf is None
        or not research.resolve(
            research_answers,
            packet_plan=replace(
                research.packet_plan, frozen_pdf=frozen_pdf, fulfilled_items=frozenset()
            ),
        ).ready
    ):
        failures.append(GateFailure.RESEARCH_UNRESOLVED)

    if now_utc is not None or authorization_expires_at is not None:
        effective_now = now_utc or datetime.now(UTC)
        hold_decision = evaluate_pre_send_hold_gate(
            now_utc=effective_now,
            authorization_expires_at=authorization_expires_at,
            usps_holidays=usps_holidays,
        )
        if not hold_decision.allowed:
            failures.append(GateFailure.HOLD_HORIZON_TOO_SHORT)

    return GateResult(failures=tuple(failures)) if failures else PASS
