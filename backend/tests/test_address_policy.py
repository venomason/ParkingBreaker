"""Tests for the shared address provenance, consent and responsibility policy (T30).

Each row of the ADDRESS-RESPONSIBILITY.md decision table and each scenario A01-A13 in
build-kit/fixtures/jurisdiction-cases.json has a named test below.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from datetime import UTC, datetime, timedelta, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st

from parkingbreaker.address_policy import (
    AI_ONLY_VERIFICATION_REJECTED,
    AddressFaultFinding,
    AddressProvenance,
    ConsentEvent,
    DestinationDecision,
    DestinationFacts,
    Disclosure,
    EvidenceKind,
    EvidenceRef,
    FailureFacts,
    OutcomeKind,
    OutcomeReason,
    ResearchClaim,
    ResearchVerification,
    Responsibility,
    ResponsibilityDecision,
    address_fingerprint,
    address_is_complete,
    classify_address_failure,
    decide_destination,
    remedy_for_responsibility,
)
from parkingbreaker.agencies import MailingAddress
from parkingbreaker.returned_mail import RemedyKind, ReturnCause

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
EARLIER = NOW - timedelta(hours=1)
POLICY = "refund-policy-1"
SHA_A = hashlib.sha256(b"disclosure-v1").hexdigest()
SHA_B = hashlib.sha256(b"disclosure-v2").hexdigest()

OFFICIAL = MailingAddress("Parking Appeals", None, "1 Main St", None, "Springfield", "IL", "62701")
CUSTOMER = MailingAddress(
    "Parking Appeals", "Hearings", "9 Oak Ave", "Ste 4", "Peoria", "IL", "61602"
)
OTHER_CUSTOMER = dataclasses.replace(CUSTOMER, line1="10 Oak Ave")
DISCLOSURE = Disclosure("disc-1", SHA_A)

DETERMINISTIC = EvidenceRef(
    "ev-quote-1", hashlib.sha256(b"quote").hexdigest(), EvidenceKind.OFFICIAL_SOURCE_QUOTE
)
LLM_ONLY = EvidenceRef("ev-llm-1", hashlib.sha256(b"llm").hexdigest(), EvidenceKind.LLM_SUMMARY)

VERIFIED = ResearchVerification(
    ResearchClaim.CLAIMS_VERIFIED, verified_address=OFFICIAL, evidence=(DETERMINISTIC,)
)
UNVERIFIED = ResearchVerification(ResearchClaim.NOT_VERIFIED, failure_code="S2_ADDRESS_UNVERIFIED")


def consent_for(
    address: MailingAddress = CUSTOMER,
    *,
    case_id: str = "case-1",
    owner: str = "owner-1",
    disclosure_sha: str = SHA_A,
    revision: int = 1,
    at: datetime = EARLIER,
) -> ConsentEvent:
    return ConsentEvent(
        case_id=case_id,
        owner=owner,
        address_fingerprint=address_fingerprint(address),
        disclosure_sha256=disclosure_sha,
        address_revision=revision,
        accepted=True,
        accepted_at=at,
    )


def facts(**overrides: object) -> DestinationFacts:
    base: dict[str, object] = {
        "case_id": "case-1",
        "owner": "owner-1",
        "decided_at": NOW,
        "refund_policy_version": POLICY,
        "research": UNVERIFIED,
        "other_gates_passed": True,
        "customer_address": CUSTOMER,
        "customer_address_revision": 1,
        "consent": consent_for(),
        "current_disclosure": DISCLOSURE,
        "approved_packet_fingerprint": address_fingerprint(CUSTOMER),
    }
    base.update(overrides)
    return DestinationFacts(**base)  # type: ignore[arg-type]


def verified_facts(**overrides: object) -> DestinationFacts:
    base: dict[str, object] = {
        "research": VERIFIED,
        "customer_address": None,
        "customer_address_revision": None,
        "consent": None,
        "current_disclosure": None,
        "approved_packet_fingerprint": address_fingerprint(OFFICIAL),
    }
    base.update(overrides)
    return facts(**base)


def customer_decision() -> DestinationDecision:
    outcome = decide_destination(facts())
    assert outcome.decision is not None
    return outcome.decision


def verified_decision() -> DestinationDecision:
    outcome = decide_destination(verified_facts())
    assert outcome.decision is not None
    return outcome.decision


def failure(**overrides: object) -> FailureFacts:
    base: dict[str, object] = {"case_id": "case-1"}
    base.update(overrides)
    return FailureFacts(**base)  # type: ignore[arg-type]


def assert_unconditional_full_refund(result: ResponsibilityDecision) -> None:
    """Full refund of the price even when a re-mail is feasible and none was used."""
    for feasible, remails_used in ((True, 0), (False, 0), (True, 1), (False, 1)):
        remedy = remedy_for_responsibility(
            result,
            deadline_feasible=feasible,
            other_verified_address_exists=True,
            price_cents=2999,
            mailing_cost_cents=800,
            remails_used=remails_used,
        )
        assert remedy is not None
        assert remedy.kind is RemedyKind.FULL_REFUND
        assert remedy.refund_cents == 2999


def assert_existing_service_error_remedy(result: ResponsibilityDecision) -> None:
    """Other our-errors keep decide_remedy(OUR_ERROR): free re-mail first, else full refund."""
    for deadline_feasible, remails_used, kind in (
        (True, 0, RemedyKind.FREE_REMAIL),
        (False, 0, RemedyKind.FULL_REFUND),
        (True, 1, RemedyKind.FULL_REFUND),
    ):
        remedy = remedy_for_responsibility(
            result,
            deadline_feasible=deadline_feasible,
            other_verified_address_exists=False,
            price_cents=2999,
            mailing_cost_cents=800,
            remails_used=remails_used,
        )
        assert remedy is not None
        assert remedy.kind is kind
        assert remedy.refund_cents == (2999 if kind is RemedyKind.FULL_REFUND else 0)


# ------------------------------------------------------------------ table rows


def test_row1_verified_destination_all_gates_pass_sends_through_normal_coordinator() -> None:
    outcome = decide_destination(verified_facts())
    assert outcome.kind is OutcomeKind.SEND_SYSTEM_VERIFIED
    assert outcome.may_send
    assert outcome.capture_allowed
    assert not outcome.release_hold
    assert outcome.decision is not None
    assert outcome.decision.provenance is AddressProvenance.SYSTEM_VERIFIED
    assert outcome.decision.fingerprint == address_fingerprint(OFFICIAL)
    assert outcome.decision.verification_evidence == (DETERMINISTIC,)


def test_row1_system_verified_wrong_destination_is_our_error_full_refund() -> None:
    result = classify_address_failure(
        verified_decision(),
        failure(address_finding=AddressFaultFinding.DESTINATION_WRONG_SOLE_CAUSE),
    )
    assert result.responsibility is Responsibility.OUR_ERROR_FULL_REFUND
    assert_unconditional_full_refund(result)


@pytest.mark.parametrize("customer_address", [None, CUSTOMER])
def test_row2_unverifiable_and_nothing_or_declined_stops_without_capture(
    customer_address: MailingAddress | None,
) -> None:
    outcome = decide_destination(
        facts(
            customer_address=customer_address,
            customer_declined=customer_address is not None,
            consent=None,
            approved_packet_fingerprint=None,
        )
    )
    assert outcome.kind is OutcomeKind.STOP_NO_CAPTURE_RELEASE_HOLD
    assert not outcome.capture_allowed
    assert outcome.release_hold
    assert outcome.decision is None
    assert outcome.failed_verification_code == "S2_ADDRESS_UNVERIFIED"
    assert outcome.reason in {OutcomeReason.NO_CUSTOMER_ADDRESS, OutcomeReason.CUSTOMER_DECLINED}


def test_row2_decline_beats_a_valid_looking_consent() -> None:
    outcome = decide_destination(facts(customer_declined=True))
    assert outcome.kind is OutcomeKind.STOP_NO_CAPTURE_RELEASE_HOLD
    assert outcome.reason is OutcomeReason.CUSTOMER_DECLINED


def test_row3_complete_address_other_gates_pass_specific_consent_is_customer_directed() -> None:
    outcome = decide_destination(facts())
    assert outcome.kind is OutcomeKind.SEND_CUSTOMER_DIRECTED
    decision = outcome.decision
    assert decision is not None
    assert decision.provenance is AddressProvenance.CUSTOMER_DIRECTED_UNVERIFIED
    assert decision.failed_verification_code == "S2_ADDRESS_UNVERIFIED"
    assert decision.customer_address_revision == 1
    assert decision.disclosure == DISCLOSURE
    assert decision.consent == consent_for()
    assert decision.refund_policy_version == POLICY
    assert decision.decided_at == NOW


def test_row3_failure_caused_solely_by_supplied_address_is_excluded() -> None:
    result = classify_address_failure(
        customer_decision(),
        failure(
            mailed_address_fingerprint=address_fingerprint(CUSTOMER),
            address_finding=AddressFaultFinding.DESTINATION_WRONG_SOLE_CAUSE,
            finding_evidence_ids=("usps-return-1",),
        ),
    )
    assert result.responsibility is Responsibility.CUSTOMER_DIRECTED_ADDRESS_EXCLUSION
    assert result.exclusion_applies
    assert result.existing_cause is None


def test_row3_exclusion_without_mailing_evidence_is_not_granted() -> None:
    result = classify_address_failure(
        customer_decision(),
        failure(
            mailed_address_fingerprint=address_fingerprint(CUSTOMER),
            address_finding=AddressFaultFinding.DESTINATION_WRONG_SOLE_CAUSE,
        ),
    )
    assert not result.exclusion_applies
    assert result.responsibility is Responsibility.UNKNOWN_CAUSE_EXISTING_REMEDY


@pytest.mark.parametrize("consent", [None, "other-case", "other-owner", "bytes", "disclosure"])
def test_row4_consent_absent_stale_replayed_or_other_bytes_does_not_send(
    consent: str | None,
) -> None:
    candidates = {
        None: None,
        "other-case": consent_for(case_id="case-2"),
        "other-owner": consent_for(owner="owner-2"),
        "bytes": consent_for(OTHER_CUSTOMER),
        "disclosure": consent_for(disclosure_sha=SHA_B),
    }
    outcome = decide_destination(facts(consent=candidates[consent]))
    assert not outcome.may_send
    assert outcome.requires_new_consent
    assert outcome.kind in {OutcomeKind.REQUEST_CURRENT_CONSENT, OutcomeKind.INVALIDATE_AND_REBUILD}
    assert outcome.decision is None
    assert outcome.release_hold


def test_row4_no_disclosure_shown_means_no_consent_can_be_current() -> None:
    outcome = decide_destination(facts(current_disclosure=None))
    assert outcome.kind is OutcomeKind.REQUEST_CURRENT_CONSENT
    assert outcome.reason is OutcomeReason.NO_DISCLOSURE_SHOWN


def test_row4_incomplete_customer_address_requests_a_complete_one() -> None:
    incomplete = dataclasses.replace(CUSTOMER, zip_code="")
    outcome = decide_destination(facts(customer_address=incomplete, consent=None))
    assert outcome.kind is OutcomeKind.REQUEST_COMPLETE_ADDRESS
    assert not outcome.may_send


def test_row5_customer_address_changed_after_consent_invalidates_and_rebuilds() -> None:
    outcome = decide_destination(
        facts(customer_address=OTHER_CUSTOMER, customer_address_revision=2)
    )
    assert outcome.kind is OutcomeKind.INVALIDATE_AND_REBUILD
    assert outcome.reason is OutcomeReason.CONSENT_BOUND_TO_DIFFERENT_ADDRESS
    assert outcome.decision is None


def test_row5_customer_address_changed_after_packet_approval_invalidates_and_rebuilds() -> None:
    outcome = decide_destination(
        facts(
            customer_address=OTHER_CUSTOMER,
            customer_address_revision=2,
            consent=consent_for(OTHER_CUSTOMER, revision=2),
        )
    )
    assert outcome.kind is OutcomeKind.INVALIDATE_AND_REBUILD
    assert outcome.reason is OutcomeReason.APPROVAL_BOUND_TO_DIFFERENT_ADDRESS


def test_row6_we_mailed_bytes_different_from_customer_directed_address_is_our_error() -> None:
    result = classify_address_failure(
        customer_decision(),
        failure(
            mailed_address_fingerprint=address_fingerprint(OTHER_CUSTOMER),
            address_finding=AddressFaultFinding.DESTINATION_WRONG_SOLE_CAUSE,
            finding_evidence_ids=("usps-return-1",),
        ),
    )
    assert result.responsibility is Responsibility.OUR_ERROR_FULL_REFUND
    assert not result.exclusion_applies


@pytest.mark.parametrize(
    "flag", ["omitted_required_contents", "duplicate_charge", "service_failure"]
)
def test_row6_omitted_contents_duplicate_charge_or_service_failure_is_our_error(flag: str) -> None:
    result = classify_address_failure(
        customer_decision(),
        failure(
            mailed_address_fingerprint=address_fingerprint(CUSTOMER),
            address_finding=AddressFaultFinding.DESTINATION_WRONG_SOLE_CAUSE,
            finding_evidence_ids=("usps-return-1",),
            **{flag: True},
        ),
    )
    assert result.responsibility is Responsibility.OUR_ERROR_FULL_REFUND
    assert result.existing_cause is ReturnCause.OUR_ERROR
    assert not result.unconditional_full_refund
    assert_existing_service_error_remedy(result)


def test_row7_system_claimed_verified_wrong_destination_mailed_is_our_error_even_if_approved() -> (
    None
):
    decision = verified_decision()  # built with approved_packet_fingerprint == our address
    result = classify_address_failure(
        decision,
        failure(
            mailed_address_fingerprint=decision.fingerprint,
            address_finding=AddressFaultFinding.DESTINATION_WRONG_WITH_OTHER_CAUSES,
        ),
    )
    assert result.responsibility is Responsibility.OUR_ERROR_FULL_REFUND
    assert result.reason == "SYSTEM_VERIFIED_DESTINATION_WRONG"
    assert_unconditional_full_refund(result)


def test_system_verified_service_failure_keeps_existing_service_error_remedy() -> None:
    result = classify_address_failure(verified_decision(), failure(service_failure=True))
    assert result.responsibility is Responsibility.OUR_ERROR_FULL_REFUND
    assert not result.unconditional_full_refund
    assert_existing_service_error_remedy(result)


def test_row8_other_mandatory_gate_unresolved_waits_even_with_valid_consent() -> None:
    outcome = decide_destination(facts(other_gates_passed=False))
    assert outcome.kind is OutcomeKind.WAIT_OTHER_GATES
    assert outcome.reason is OutcomeReason.OTHER_GATES_UNRESOLVED
    assert not outcome.may_send


def test_row8_other_gate_unresolved_waits_for_system_verified_too() -> None:
    outcome = decide_destination(verified_facts(other_gates_passed=False))
    assert outcome.kind is OutcomeKind.WAIT_OTHER_GATES


def test_unapproved_packet_does_not_send() -> None:
    outcome = decide_destination(facts(approved_packet_fingerprint=None))
    assert outcome.kind is OutcomeKind.WAIT_OTHER_GATES
    assert outcome.reason is OutcomeReason.PACKET_NOT_APPROVED
    assert outcome.decision is not None


# ------------------------------------------------------------------ scenarios A01-A13


def test_A01_verified_destination_and_all_other_gates_pass() -> None:
    assert decide_destination(verified_facts()).kind is OutcomeKind.SEND_SYSTEM_VERIFIED


def test_A02_only_destination_unverified_no_customer_alternative() -> None:
    outcome = decide_destination(
        facts(customer_address=None, customer_address_revision=None, consent=None)
    )
    assert outcome.kind is OutcomeKind.STOP_NO_CAPTURE_RELEASE_HOLD
    assert outcome.release_hold
    assert not outcome.capture_allowed


def test_A03_customer_supplies_complete_address_and_accepts_current_disclosure() -> None:
    assert decide_destination(facts()).kind is OutcomeKind.SEND_CUSTOMER_DIRECTED


def test_A04_customer_address_without_specific_affirmative_consent() -> None:
    outcome = decide_destination(facts(consent=None))
    assert outcome.kind is OutcomeKind.REQUEST_CURRENT_CONSENT
    assert outcome.reason is OutcomeReason.CONSENT_ABSENT
    assert not outcome.may_send


def test_A04_consent_cannot_be_a_non_explicit_default() -> None:
    for accepted in (False, 1, None):
        with pytest.raises(ValueError, match="explicit acceptance"):
            dataclasses.replace(consent_for(), accepted=accepted)  # type: ignore[arg-type]


def test_A05_consent_refers_to_earlier_address_revision() -> None:
    outcome = decide_destination(
        facts(
            customer_address_revision=2, approved_packet_fingerprint=address_fingerprint(CUSTOMER)
        )
    )
    assert outcome.kind is OutcomeKind.INVALIDATE_AND_REBUILD
    assert outcome.reason is OutcomeReason.CONSENT_STALE_REVISION
    assert outcome.requires_new_consent


def test_A06_consent_belongs_to_another_case() -> None:
    outcome = decide_destination(facts(consent=consent_for(case_id="case-2")))
    assert outcome.kind is OutcomeKind.REQUEST_CURRENT_CONSENT
    assert outcome.reason is OutcomeReason.CONSENT_OTHER_CASE
    assert not outcome.may_send


def test_A06_consent_belongs_to_another_owner() -> None:
    outcome = decide_destination(facts(consent=consent_for(owner="owner-2")))
    assert outcome.kind is OutcomeKind.REQUEST_CURRENT_CONSENT
    assert outcome.reason is OutcomeReason.CONSENT_OTHER_OWNER
    assert not outcome.may_send


def test_A07_system_said_verified_address_later_proved_wrong() -> None:
    result = classify_address_failure(
        verified_decision(),
        failure(address_finding=AddressFaultFinding.DESTINATION_WRONG_SOLE_CAUSE),
    )
    assert result.responsibility is Responsibility.OUR_ERROR_FULL_REFUND
    assert_unconditional_full_refund(result)


def test_A08_customer_approved_system_verified_address_it_was_wrong() -> None:
    outcome = decide_destination(verified_facts())  # approval fingerprint is the verified address
    assert outcome.decision is not None
    assert outcome.decision.provenance is AddressProvenance.SYSTEM_VERIFIED
    result = classify_address_failure(
        outcome.decision,
        failure(address_finding=AddressFaultFinding.DESTINATION_WRONG_SOLE_CAUSE),
    )
    assert result.responsibility is Responsibility.OUR_ERROR_FULL_REFUND
    assert not result.exclusion_applies
    assert_unconditional_full_refund(result)


def test_A08_customer_consent_supplied_for_verified_destination_does_not_change_provenance() -> (
    None
):
    outcome = decide_destination(
        verified_facts(
            customer_address=CUSTOMER, customer_address_revision=1, consent=consent_for()
        )
    )
    assert outcome.decision is not None
    assert outcome.decision.provenance is AddressProvenance.SYSTEM_VERIFIED
    assert outcome.decision.consent is None
    assert outcome.decision.fingerprint == address_fingerprint(OFFICIAL)


def test_A09_customer_directed_address_alone_caused_failed_mailing() -> None:
    result = classify_address_failure(
        customer_decision(),
        failure(
            mailed_address_fingerprint=address_fingerprint(CUSTOMER),
            address_finding=AddressFaultFinding.DESTINATION_WRONG_SOLE_CAUSE,
            finding_evidence_ids=("lob-event-7",),
        ),
    )
    assert result.responsibility is Responsibility.CUSTOMER_DIRECTED_ADDRESS_EXCLUSION
    assert (
        remedy_for_responsibility(
            result,
            deadline_feasible=True,
            other_verified_address_exists=False,
            price_cents=2999,
            mailing_cost_cents=800,
            remails_used=0,
        )
        is None
    )


@pytest.mark.parametrize(
    "finding",
    [
        AddressFaultFinding.DESTINATION_WRONG_WITH_OTHER_CAUSES,
        AddressFaultFinding.DESTINATION_NOT_AT_FAULT,
        AddressFaultFinding.NOT_ESTABLISHED,
    ],
)
def test_A09_exclusion_requires_the_address_to_be_the_sole_cause(
    finding: AddressFaultFinding,
) -> None:
    result = classify_address_failure(
        customer_decision(),
        failure(
            mailed_address_fingerprint=address_fingerprint(CUSTOMER),
            address_finding=finding,
            finding_evidence_ids=("lob-event-7",),
        ),
    )
    assert not result.exclusion_applies


def test_A10_we_altered_customer_directed_address() -> None:
    result = classify_address_failure(
        customer_decision(),
        failure(
            mailed_address_fingerprint=address_fingerprint(OFFICIAL),
            address_finding=AddressFaultFinding.DESTINATION_WRONG_SOLE_CAUSE,
            finding_evidence_ids=("lob-event-7",),
        ),
    )
    assert result.responsibility is Responsibility.OUR_ERROR_FULL_REFUND
    assert result.reason == "MAILED_BYTES_DIFFER_FROM_DECISION"


def test_A10_we_omitted_required_contents() -> None:
    result = classify_address_failure(
        customer_decision(),
        failure(
            mailed_address_fingerprint=address_fingerprint(CUSTOMER),
            omitted_required_contents=True,
            address_finding=AddressFaultFinding.DESTINATION_WRONG_SOLE_CAUSE,
            finding_evidence_ids=("lob-event-7",),
        ),
    )
    assert result.responsibility is Responsibility.OUR_ERROR_FULL_REFUND
    assert result.reason == "OMITTED_REQUIRED_CONTENTS"


def test_A10_returned_mail_classified_our_error_cannot_be_excused() -> None:
    result = classify_address_failure(
        customer_decision(),
        failure(
            return_cause=ReturnCause.OUR_ERROR,
            mailed_address_fingerprint=address_fingerprint(CUSTOMER),
            address_finding=AddressFaultFinding.DESTINATION_WRONG_SOLE_CAUSE,
            finding_evidence_ids=("lob-event-7",),
        ),
    )
    assert result.responsibility is Responsibility.OUR_ERROR_FULL_REFUND


def test_A11_address_accepted_but_deadline_or_form_requirement_unresolved() -> None:
    outcome = decide_destination(facts(other_gates_passed=False))
    assert outcome.kind is OutcomeKind.WAIT_OTHER_GATES
    assert not outcome.capture_allowed


def test_A12_legacy_case_without_consent_or_provenance_record_has_no_exclusion() -> None:
    result = classify_address_failure(
        None,
        failure(
            mailed_address_fingerprint=address_fingerprint(CUSTOMER),
            address_finding=AddressFaultFinding.DESTINATION_WRONG_SOLE_CAUSE,
            finding_evidence_ids=("lob-event-7",),
        ),
    )
    assert result.responsibility is Responsibility.LEGACY_NO_RECORD
    assert not result.exclusion_applies
    assert result.existing_cause is ReturnCause.UNKNOWN


def test_A12_legacy_case_with_our_failure_is_still_our_error() -> None:
    result = classify_address_failure(None, failure(service_failure=True))
    assert result.responsibility is Responsibility.OUR_ERROR_FULL_REFUND


def test_A13_returned_mail_cause_unknown_follows_existing_remedy() -> None:
    result = classify_address_failure(
        customer_decision(), failure(mailed_address_fingerprint=address_fingerprint(CUSTOMER))
    )
    assert result.responsibility is Responsibility.UNKNOWN_CAUSE_EXISTING_REMEDY
    assert result.existing_cause is ReturnCause.UNKNOWN
    remedy = remedy_for_responsibility(
        result,
        deadline_feasible=False,
        other_verified_address_exists=False,
        price_cents=2999,
        mailing_cost_cents=800,
        remails_used=0,
    )
    assert remedy is not None
    assert remedy.kind is RemedyKind.PARTIAL_REFUND
    assert remedy.refund_cents == 2199
    feasible = remedy_for_responsibility(
        result,
        deadline_feasible=True,
        other_verified_address_exists=False,
        price_cents=2999,
        mailing_cost_cents=800,
        remails_used=0,
    )
    assert feasible is not None
    assert feasible.kind is RemedyKind.FREE_REMAIL


def test_A13_agency_side_return_keeps_existing_proof_packet_remedy() -> None:
    result = classify_address_failure(
        customer_decision(),
        failure(
            return_cause=ReturnCause.AGENCY_SIDE,
            mailed_address_fingerprint=address_fingerprint(CUSTOMER),
            address_finding=AddressFaultFinding.DESTINATION_WRONG_SOLE_CAUSE,
            finding_evidence_ids=("lob-event-7",),
        ),
    )
    assert result.responsibility is Responsibility.AGENCY_SIDE_EXISTING_REMEDY
    remedy = remedy_for_responsibility(
        result,
        deadline_feasible=True,
        other_verified_address_exists=False,
        price_cents=2999,
        mailing_cost_cents=800,
        remails_used=0,
    )
    assert remedy is not None
    assert remedy.kind is RemedyKind.PROOF_PACKET


def test_unmatched_mailed_fingerprint_unknown_does_not_grant_exclusion() -> None:
    result = classify_address_failure(
        customer_decision(),
        failure(
            address_finding=AddressFaultFinding.DESTINATION_WRONG_SOLE_CAUSE,
            finding_evidence_ids=("lob-event-7",),
        ),
    )
    assert not result.exclusion_applies


def test_decision_from_another_case_is_rejected_by_classifier() -> None:
    with pytest.raises(ValueError, match="another case"):
        classify_address_failure(customer_decision(), failure(case_id="case-2"))


# ------------------------------------------------------------------ AI-only verification


@pytest.mark.parametrize("evidence", [(), (LLM_ONLY,)])
def test_ai_only_verification_claim_never_becomes_system_verified(
    evidence: tuple[EvidenceRef, ...],
) -> None:
    claim = ResearchVerification(
        ResearchClaim.CLAIMS_VERIFIED, verified_address=OFFICIAL, evidence=evidence
    )
    outcome = decide_destination(
        facts(research=claim, customer_address=None, customer_address_revision=None, consent=None)
    )
    assert outcome.kind is OutcomeKind.STOP_NO_CAPTURE_RELEASE_HOLD
    assert outcome.failed_verification_code == AI_ONLY_VERIFICATION_REJECTED
    assert outcome.decision is None


def test_ai_only_verification_falls_back_to_customer_directed_not_system_verified() -> None:
    claim = ResearchVerification(
        ResearchClaim.CLAIMS_VERIFIED, verified_address=OFFICIAL, evidence=(LLM_ONLY,)
    )
    outcome = decide_destination(facts(research=claim))
    assert outcome.decision is not None
    assert outcome.decision.provenance is AddressProvenance.CUSTOMER_DIRECTED_UNVERIFIED
    assert outcome.decision.failed_verification_code == AI_ONLY_VERIFICATION_REJECTED


def test_llm_evidence_alongside_deterministic_evidence_is_dropped_from_record() -> None:
    claim = ResearchVerification(
        ResearchClaim.CLAIMS_VERIFIED,
        verified_address=OFFICIAL,
        evidence=(LLM_ONLY, DETERMINISTIC),
    )
    outcome = decide_destination(verified_facts(research=claim))
    assert outcome.decision is not None
    assert outcome.decision.verification_evidence == (DETERMINISTIC,)


def test_system_verified_record_cannot_be_constructed_without_deterministic_evidence() -> None:
    base = verified_decision()
    for evidence in ((), (LLM_ONLY,), (LLM_ONLY, DETERMINISTIC)):
        with pytest.raises(ValueError, match="evidence"):
            dataclasses.replace(base, verification_evidence=evidence)


def test_research_claim_requires_address_and_non_claim_requires_failure_code() -> None:
    with pytest.raises(ValueError, match="verified address"):
        ResearchVerification(ResearchClaim.CLAIMS_VERIFIED)
    with pytest.raises(ValueError, match="failure_code"):
        ResearchVerification(ResearchClaim.NOT_VERIFIED)


def test_evidence_ref_validation() -> None:
    with pytest.raises(ValueError, match="evidence_id"):
        EvidenceRef(" ", DETERMINISTIC.sha256, EvidenceKind.OFFICIAL_SOURCE_QUOTE)
    with pytest.raises(ValueError, match="sha256"):
        EvidenceRef("ev", "abc", EvidenceKind.OFFICIAL_SOURCE_QUOTE)


# ------------------------------------------------------------------ consent replay / binding


def test_cross_owner_consent_replay_is_refused_for_every_case() -> None:
    replay = consent_for(owner="owner-2")
    outcome = decide_destination(facts(consent=replay))
    assert outcome.kind is OutcomeKind.REQUEST_CURRENT_CONSENT
    assert outcome.decision is None


def test_cross_case_consent_replay_is_refused() -> None:
    replay = consent_for(case_id="case-2")
    outcome = decide_destination(facts(consent=replay))
    assert outcome.kind is OutcomeKind.REQUEST_CURRENT_CONSENT


def test_consent_bound_to_different_bytes_is_refused_even_with_same_revision() -> None:
    near = dataclasses.replace(CUSTOMER, recipient=CUSTOMER.recipient.upper())
    outcome = decide_destination(facts(consent=consent_for(near)))
    assert not outcome.may_send
    assert outcome.reason is OutcomeReason.CONSENT_BOUND_TO_DIFFERENT_ADDRESS


def test_consent_bound_to_different_revision_is_refused() -> None:
    outcome = decide_destination(facts(consent=consent_for(revision=0)))
    assert not outcome.may_send
    assert outcome.reason is OutcomeReason.CONSENT_STALE_REVISION


def test_consent_to_older_disclosure_is_stale() -> None:
    outcome = decide_destination(facts(current_disclosure=Disclosure("disc-2", SHA_B)))
    assert outcome.kind is OutcomeKind.REQUEST_CURRENT_CONSENT
    assert outcome.reason is OutcomeReason.CONSENT_DISCLOSURE_STALE


def test_consent_dated_after_the_decision_is_refused() -> None:
    outcome = decide_destination(facts(consent=consent_for(at=NOW + timedelta(seconds=1))))
    assert not outcome.may_send
    assert outcome.reason is OutcomeReason.CONSENT_POSTDATES_DECISION


def test_changed_address_after_approval_for_system_verified_invalidates() -> None:
    outcome = decide_destination(
        verified_facts(approved_packet_fingerprint=address_fingerprint(CUSTOMER))
    )
    assert outcome.kind is OutcomeKind.INVALIDATE_AND_REBUILD
    assert outcome.reason is OutcomeReason.APPROVAL_BOUND_TO_DIFFERENT_ADDRESS


def test_consent_never_waives_unrelated_gates_with_valid_consent_and_approval() -> None:
    outcome = decide_destination(facts(other_gates_passed=False))
    assert outcome.kind is not OutcomeKind.SEND_CUSTOMER_DIRECTED


def test_customer_directed_decision_rejects_inconsistent_combinations() -> None:
    base = customer_decision()
    with pytest.raises(ValueError, match="disclosure and consent"):
        dataclasses.replace(base, consent=None)
    with pytest.raises(ValueError, match="disclosure and consent"):
        dataclasses.replace(base, disclosure=None)
    with pytest.raises(ValueError, match="another case or owner"):
        dataclasses.replace(base, consent=consent_for(case_id="case-2"))
    with pytest.raises(ValueError, match="another case or owner"):
        dataclasses.replace(base, consent=consent_for(owner="owner-2"))
    with pytest.raises(ValueError, match=r"different address$"):
        dataclasses.replace(base, consent=consent_for(OTHER_CUSTOMER))
    with pytest.raises(ValueError, match="revision"):
        dataclasses.replace(base, customer_address_revision=2)
    with pytest.raises(ValueError, match="revision"):
        dataclasses.replace(base, customer_address_revision=None)
    with pytest.raises(ValueError, match="different disclosure"):
        dataclasses.replace(base, disclosure=Disclosure("disc-2", SHA_B))
    with pytest.raises(ValueError, match="failed_verification_code"):
        dataclasses.replace(base, failed_verification_code=None)
    with pytest.raises(ValueError, match="no verification evidence"):
        dataclasses.replace(base, verification_evidence=(DETERMINISTIC,))
    with pytest.raises(ValueError, match="postdates"):
        dataclasses.replace(base, decided_at=EARLIER - timedelta(seconds=1))


def test_system_verified_decision_rejects_customer_directed_fields() -> None:
    base = verified_decision()
    with pytest.raises(ValueError, match="no customer-directed fields"):
        dataclasses.replace(base, consent=consent_for())
    with pytest.raises(ValueError, match="no customer-directed fields"):
        dataclasses.replace(base, customer_address_revision=1)
    with pytest.raises(ValueError, match="no customer-directed fields"):
        dataclasses.replace(base, failed_verification_code="X")
    with pytest.raises(ValueError, match="no customer-directed fields"):
        dataclasses.replace(base, disclosure=DISCLOSURE)


def test_decision_fingerprint_must_match_address() -> None:
    with pytest.raises(ValueError, match="fingerprint"):
        dataclasses.replace(verified_decision(), fingerprint=address_fingerprint(CUSTOMER))


def test_provenance_enum_has_exactly_the_two_kinds() -> None:
    assert {member.name for member in AddressProvenance} == {
        "SYSTEM_VERIFIED",
        "CUSTOMER_DIRECTED_UNVERIFIED",
    }


# ------------------------------------------------------------------ timestamps


def test_tz_naive_datetimes_are_rejected_everywhere() -> None:
    naive = datetime(2026, 10, 20, 12, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        consent_for(at=naive)
    with pytest.raises(ValueError, match="timezone-aware"):
        facts(decided_at=naive)
    with pytest.raises(ValueError, match="timezone-aware"):
        dataclasses.replace(verified_decision(), decided_at=naive)


def test_non_utc_aware_datetimes_are_compared_correctly() -> None:
    plus_five = timezone(timedelta(hours=5))
    consent_at = datetime(2026, 10, 20, 17, 30, tzinfo=plus_five)  # 12:30 UTC, after NOW
    outcome = decide_destination(facts(consent=consent_for(at=consent_at)))
    assert outcome.reason is OutcomeReason.CONSENT_POSTDATES_DECISION


def test_strict_bool_and_revision_inputs() -> None:
    with pytest.raises(ValueError, match="other_gates_passed"):
        facts(other_gates_passed=1)
    with pytest.raises(ValueError, match="revision"):
        facts(customer_address_revision=True)
    with pytest.raises(ValueError, match="revision"):
        facts(customer_address_revision=None)
    with pytest.raises(ValueError, match="approved_packet_fingerprint"):
        facts(approved_packet_fingerprint="not-a-hash")


# ------------------------------------------------------------------ immutability


def test_records_are_frozen() -> None:
    decision = customer_decision()
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.fingerprint = "x"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.provenance = AddressProvenance.SYSTEM_VERIFIED  # type: ignore[misc]
    assert decision.consent is not None
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.consent.accepted = False  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        DISCLOSURE.version = "x"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        decide_destination(facts()).kind = OutcomeKind.WAIT_OTHER_GATES  # type: ignore[misc]


# ------------------------------------------------------------------ fingerprint


def test_fingerprint_is_stable_and_canonical() -> None:
    canonical = (
        '{"attention":null,"city":"Springfield","country":"US","line1":"1 Main St",'
        '"line2":null,"recipient":"Parking Appeals","state":"IL","zip_code":"62701"}'
    )
    assert address_fingerprint(OFFICIAL) == hashlib.sha256(canonical.encode()).hexdigest()
    assert address_fingerprint(OFFICIAL) == address_fingerprint(dataclasses.replace(OFFICIAL))
    assert json.loads(canonical) == dataclasses.asdict(OFFICIAL)


@pytest.mark.parametrize(
    "change",
    [
        {"recipient": "parking appeals"},
        {"attention": ""},
        {"line1": "1 Main St "},
        {"line1": "1 Main Street"},
        {"line2": ""},
        {"city": "SPRINGFIELD"},
        {"state": "il"},
        {"zip_code": "62701-0001"},
        {"country": "CA"},
    ],
)
def test_fingerprint_is_sensitive_to_every_byte_no_normalization(change: dict[str, str]) -> None:
    assert address_fingerprint(dataclasses.replace(OFFICIAL, **change)) != address_fingerprint(
        OFFICIAL
    )


@given(st.text(max_size=30), st.text(max_size=30))
def test_fingerprint_distinguishes_distinct_inputs(first: str, second: str) -> None:
    left = dataclasses.replace(OFFICIAL, line1=first)
    right = dataclasses.replace(OFFICIAL, line1=second)
    assert (address_fingerprint(left) == address_fingerprint(right)) == (first == second)


def test_address_completeness() -> None:
    assert address_is_complete(CUSTOMER)
    for change in (
        {"recipient": " "},
        {"line1": ""},
        {"city": ""},
        {"state": "Illinois"},
        {"zip_code": "6270"},
        {"country": ""},
    ):
        assert not address_is_complete(dataclasses.replace(CUSTOMER, **change))
