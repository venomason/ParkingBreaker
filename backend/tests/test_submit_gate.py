from __future__ import annotations

from dataclasses import replace
from datetime import UTC
from functools import cache

from hypothesis import given
from hypothesis import strategies as st

from parkingbreaker.research.run import ResearchResult
from parkingbreaker.session_window import WindowStatus
from parkingbreaker.submit_gate import (
    PASS,
    GateFailure,
    GateResult,
    check_submit_gate,
    hashes_match,
)

F = GateFailure
HASH = "a3f1" * 16
CITATION_NUMBER = "SF-998877"


@cache
def verified_submission() -> tuple[ResearchResult, bytes]:
    from parkingbreaker.appeal.packet import build_packet
    from parkingbreaker.research.questions import PacketPlan
    from test_appeal_packet import AT, CITATION, letter_text
    from test_research_records import result_fixture

    research = replace(
        result_fixture(),
        packet_plan=PacketPlan(customer_fields=(("citation_number", CITATION_NUMBER),)),
    )
    return (
        research,
        build_packet(letter_text(), CITATION, [], AT, citation_number=CITATION_NUMBER).pdf,
    )


def gate(
    *,
    auth_valid: bool = True,
    window_status: WindowStatus = WindowStatus.ACTIVE,
    adoption_hash: str = HASH,
    frozen_letter_hash: str = HASH,
    deadline_feasible_now: bool = True,
    address_verified: bool = True,
) -> GateResult:
    research, frozen_pdf = verified_submission()
    return check_submit_gate(
        auth_valid=auth_valid,
        window_status=window_status,
        adoption_hash=adoption_hash,
        frozen_letter_hash=frozen_letter_hash,
        deadline_feasible_now=deadline_feasible_now,
        address_verified=address_verified,
        research=research,
        frozen_pdf=frozen_pdf,
    )


def test_all_checks_hold_passes() -> None:
    result = gate()
    assert result == PASS
    assert result.passed


def test_warning_window_passes() -> None:
    assert gate(window_status=WindowStatus.WARNING) == PASS


def test_each_check_reports_its_own_failure() -> None:
    assert gate(auth_valid=False).failures == (F.AUTH_INVALID,)
    assert gate(window_status=WindowStatus.EXPIRED).failures == (F.WINDOW_EXPIRED,)
    assert gate(frozen_letter_hash="b" * 64).failures == (F.ADOPTION_MISMATCH,)
    assert gate(deadline_feasible_now=False).failures == (F.NOT_FEASIBLE,)
    assert gate(address_verified=False).failures == (F.ADDRESS_UNVERIFIED,)


def test_all_failures_reported_together_in_order() -> None:
    result = gate(
        auth_valid=False,
        window_status=WindowStatus.EXPIRED,
        adoption_hash="x",
        deadline_feasible_now=False,
        address_verified=False,
    )
    assert not result.passed
    assert result.failures == tuple(
        f
        for f in GateFailure
        if f
        not in (
            F.RESEARCH_UNRESOLVED,
            F.HOLD_HORIZON_TOO_SHORT,
            F.DECLARATION_UNAFFIRMED,
            F.PACKET_APPROVAL_MISMATCH,
        )
    )


def test_required_declaration_must_be_affirmed() -> None:
    assert gate().failures == ()
    result = check_submit_gate(
        auth_valid=True,
        window_status=WindowStatus.ACTIVE,
        adoption_hash=HASH,
        frozen_letter_hash=HASH,
        deadline_feasible_now=True,
        address_verified=True,
        research=verified_submission()[0],
        frozen_pdf=verified_submission()[1],
        required_declaration_ids=("declaration-1",),
    )
    assert result.failures == (F.DECLARATION_UNAFFIRMED,)
    affirmed = check_submit_gate(
        auth_valid=True,
        window_status=WindowStatus.ACTIVE,
        adoption_hash=HASH,
        frozen_letter_hash=HASH,
        deadline_feasible_now=True,
        address_verified=True,
        research=verified_submission()[0],
        frozen_pdf=verified_submission()[1],
        required_declaration_ids=("declaration-1",),
        affirmed_declaration_ids=("declaration-1",),
    )
    assert affirmed == PASS


def test_packet_approval_mismatch_stops_the_send() -> None:
    result = check_submit_gate(
        auth_valid=True,
        window_status=WindowStatus.ACTIVE,
        adoption_hash=HASH,
        frozen_letter_hash=HASH,
        deadline_feasible_now=True,
        address_verified=True,
        research=verified_submission()[0],
        frozen_pdf=verified_submission()[1],
        packet_approval_matched=False,
    )
    assert result.failures == (F.PACKET_APPROVAL_MISMATCH,)


def test_empty_hashes_never_match() -> None:
    assert not hashes_match("", "")
    assert gate(adoption_hash="", frozen_letter_hash="").failures == (F.ADOPTION_MISMATCH,)


def test_hash_match_is_exact() -> None:
    assert hashes_match(HASH, HASH)
    assert not hashes_match(HASH, HASH.upper())
    assert not hashes_match(HASH, HASH + "0")


@given(
    st.booleans(),
    st.sampled_from(WindowStatus),
    st.booleans(),
    st.booleans(),
    st.booleans(),
)
def test_passes_only_when_every_check_holds(
    auth: bool, window: WindowStatus, same_hash: bool, feasible: bool, address: bool
) -> None:
    result = gate(
        auth_valid=auth,
        window_status=window,
        frozen_letter_hash=HASH if same_hash else "0" * 64,
        deadline_feasible_now=feasible,
        address_verified=address,
    )
    all_hold = auth and window is not WindowStatus.EXPIRED and same_hash and feasible and address
    assert result.passed == all_hold
    assert len(set(result.failures)) == len(result.failures)


def test_submit_rechecks_s3_against_actual_frozen_bytes_after_research_pass() -> None:
    from dataclasses import replace

    from item2_helpers import form_case
    from parkingbreaker.appeal.packet import build_packet
    from test_appeal_packet import AT, CITATION, letter_text

    research, plan, form = form_case()
    from parkingbreaker.appeal.letter import enclosure_line

    form_text = (
        letter_text()
        + "\n\n"
        + str(enclosure_line(True, 0, required_forms=("Parking Citation Initial Review Form",)))
    )
    full = build_packet(form_text, CITATION, [], AT, completed_forms=[form], research=research)
    research = research.reassess(packet_plan=replace(plan, frozen_pdf=full.pdf))
    assert research.resolve().ready
    assert check_submit_gate(
        auth_valid=True,
        deadline_feasible_now=True,
        address_verified=True,
        window_status=WindowStatus.ACTIVE,
        adoption_hash=HASH,
        frozen_letter_hash=HASH,
        research=research,
        frozen_pdf=full.pdf,
    ).passed
    missing = build_packet(letter_text(), CITATION, [], AT)
    assert check_submit_gate(
        auth_valid=True,
        deadline_feasible_now=True,
        address_verified=True,
        window_status=WindowStatus.ACTIVE,
        adoption_hash=HASH,
        frozen_letter_hash=HASH,
        research=research,
        frozen_pdf=missing.pdf,
    ).failures == (F.RESEARCH_UNRESOLVED,)
    assert check_submit_gate(
        auth_valid=True,
        deadline_feasible_now=True,
        address_verified=True,
        window_status=WindowStatus.ACTIVE,
        adoption_hash=HASH,
        frozen_letter_hash=HASH,
        research=research,
    ).failures == (F.RESEARCH_UNRESOLVED,)


def test_hold_gate_blocks_47h59m_and_allows_48h00m() -> None:
    from datetime import datetime, timedelta

    from parkingbreaker.submit_gate import SubmitGateStatus, evaluate_pre_send_hold_gate

    # Wednesday 13:00 UTC (05:00 AM PT) + 4h cancel buffer = 09:00 AM PT (makes 10 AM PT cutoff)
    now = datetime(2026, 11, 11, 13, 0, tzinfo=UTC)

    blocked = evaluate_pre_send_hold_gate(
        now_utc=now,
        authorization_expires_at=now + timedelta(hours=47, minutes=59),
    )
    assert blocked.allowed is False
    assert blocked.status == SubmitGateStatus.BLOCKED_HOLD_HORIZON_TOO_SHORT

    allowed = evaluate_pre_send_hold_gate(
        now_utc=now,
        authorization_expires_at=now + timedelta(hours=48),
    )
    assert allowed.allowed is True
    assert allowed.status == SubmitGateStatus.ALLOWED


def test_hold_gate_blocks_friday_post_cutoff_and_holiday_slip() -> None:
    from datetime import date, datetime, timedelta

    from parkingbreaker.submit_gate import SubmitGateStatus, evaluate_pre_send_hold_gate

    # Friday 19:00 UTC (11:00 AM PT - missed Friday 10 AM cutoff).
    # Hold has 65h left (expires Monday 12:00 UTC), but induction is Monday 18:00 PT
    # (Tuesday 02:00 UTC).
    now_fri = datetime(2026, 11, 13, 19, 0, tzinfo=UTC)
    res_fri = evaluate_pre_send_hold_gate(
        now_utc=now_fri,
        authorization_expires_at=now_fri + timedelta(hours=65),
    )
    assert res_fri.allowed is False
    assert res_fri.status == SubmitGateStatus.BLOCKED_INDUCTION_EXCEEDS_CAPTURE_LEAD

    # Wednesday before Thanksgiving (2026-11-25) at 19:00 UTC (11:00 AM PT - missed Wed cutoff).
    # Thursday 2026-11-26 is a holiday, so induction slips to Friday 18:00 PT (Saturday 02:00 UTC).
    now_wed = datetime(2026, 11, 25, 19, 0, tzinfo=UTC)
    res_hol = evaluate_pre_send_hold_gate(
        now_utc=now_wed,
        authorization_expires_at=now_wed + timedelta(hours=55),
        usps_holidays=frozenset({date(2026, 11, 26)}),
    )
    assert res_hol.allowed is False
    assert res_hol.status == SubmitGateStatus.BLOCKED_INDUCTION_EXCEEDS_CAPTURE_LEAD


def test_check_submit_gate_accumulates_hold_horizon_failure() -> None:
    from datetime import datetime, timedelta

    now = datetime(2026, 11, 11, 13, 0, tzinfo=UTC)
    result = check_submit_gate(
        auth_valid=True,
        window_status=WindowStatus.ACTIVE,
        adoption_hash=HASH,
        frozen_letter_hash=HASH,
        deadline_feasible_now=True,
        address_verified=True,
        now_utc=now,
        authorization_expires_at=now + timedelta(hours=24),
    )
    assert not result.passed
    assert GateFailure.HOLD_HORIZON_TOO_SHORT in result.failures
