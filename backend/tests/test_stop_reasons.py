"""Exhaustive customer copy checks; existing source fixtures are read unchanged."""

from itertools import product

import pytest

from parkingbreaker.case_state import HardStop, StopFamily, StopReason
from parkingbreaker.research.sources import html_to_text
from parkingbreaker.research.veto_codes import VetoCode
from parkingbreaker.stop_reasons import StopMessage, stop_message
from samples import fixture

# Existing synthetic source wording, used only to test verbatim presentation.
QUOTE = "Contests sent by mail must be received by the SFMTA by the deadline."
POLICY = "Private parking notices are excluded by product scope."

REASONS: dict[StopReason, tuple[StopFamily, str]] = {
    StopReason.PROCESS_INCOMPATIBLE: (StopFamily.PROCESS_INCOMPATIBLE, "process_incompatible"),
    StopReason.NOT_YET_VERIFIED: (StopFamily.NOT_YET_VERIFIED, "not_yet_verified"),
    StopReason.BUSINESS_EXCLUSION: (StopFamily.BUSINESS_EXCLUSION, "business_exclusion"),
    StopReason.UNSUPPORTED: (StopFamily.BUSINESS_EXCLUSION, "unsupported"),
    StopReason.UNRESOLVABLE_FACT: (StopFamily.NOT_YET_VERIFIED, "unresolvable_fact"),
    StopReason.NOT_FEASIBLE: (StopFamily.PROCESS_INCOMPATIBLE, "not_feasible"),
    StopReason.DECLINED: (StopFamily.TRANSACTION_FAILURE, "declined"),
    StopReason.AUTHORIZATION_EXPIRED: (StopFamily.TRANSACTION_FAILURE, "authorization_expired"),
    StopReason.DESTINATION_NOT_CONFIRMED: (
        StopFamily.TRANSACTION_FAILURE,
        "destination_not_confirmed",
    ),
    StopReason.LETTER_NOT_SUBMITTABLE: (
        StopFamily.TRANSACTION_FAILURE,
        "letter_not_submittable",
    ),
    StopReason.WINDOW_EXPIRED: (StopFamily.TRANSACTION_FAILURE, "window_expired"),
    StopReason.ADDRESS_UNDELIVERABLE: (StopFamily.TRANSACTION_FAILURE, "address_undeliverable"),
    StopReason.LOB_REJECTED: (StopFamily.TRANSACTION_FAILURE, "lob_rejected"),
    StopReason.NOT_SENT: (StopFamily.TRANSACTION_FAILURE, "not_sent"),
    StopReason.DRAFTING_UNAVAILABLE: (
        StopFamily.TRANSACTION_FAILURE,
        "drafting_unavailable",
    ),
}


def test_all_stop_families_and_reasons_have_copy() -> None:
    assert set(REASONS) == set(StopReason)
    assert {family for family, _ in REASONS.values()} == set(StopFamily)
    assert QUOTE in " ".join(html_to_text(fixture("sfmta_contest.html")).split())


@pytest.mark.parametrize(
    ("reason", "hard_stop", "veto_code"),
    list(product(StopReason, [None, *HardStop], [None, *VetoCode])),
)
def test_every_enum_combination_maps_or_rejects_inconsistent_context(
    reason: StopReason, hard_stop: HardStop | None, veto_code: VetoCode | None
) -> None:
    family, reason_key = REASONS[reason]
    is_research = reason in {StopReason.PROCESS_INCOMPATIBLE, StopReason.NOT_YET_VERIFIED}
    quote = QUOTE if reason is StopReason.PROCESS_INCOMPATIBLE else None
    policy = POLICY if reason is StopReason.BUSINESS_EXCLUSION else None
    if is_research != (hard_stop is not None) or (
        veto_code is not None and family is not StopFamily.NOT_YET_VERIFIED
    ):
        with pytest.raises(ValueError, match=r"hard_stop|veto_code requires NOT_YET_VERIFIED"):
            stop_message(
                reason, hard_stop, veto_code=veto_code, official_quote=quote, policy_reason=policy
            )
        return

    message = stop_message(
        reason, hard_stop, veto_code=veto_code, official_quote=quote, policy_reason=policy
    )
    expected_key = f"stop.{family.value.lower()}.{reason_key}"
    if hard_stop is not None:
        expected_key += f".{hard_stop.value.lower()}"
    if veto_code is not None:
        expected_key += f".{veto_code.value.lower()}"
        assert veto_code.value not in message.text
    assert message.key == expected_key
    assert "Nothing was charged." in message.text
    assert "Your work is saved." in message.text
    assert "You can " in message.text
    assert not any(
        word in message.text.casefold() for word in ("lawyer", "guarantee", "win", "dismiss")
    )
    if family is StopFamily.NOT_YET_VERIFIED:
        assert "We couldn't confirm " in message.text
        assert " yet." in message.text
        assert "try again" in message.text
    if reason is StopReason.PROCESS_INCOMPATIBLE:
        assert f'"{QUOTE}"' in message.text
        assert "The agency requires " in message.text
        assert "our mailed letter cannot " in message.text
    if reason is StopReason.BUSINESS_EXCLUSION:
        assert POLICY in message.text


@pytest.mark.parametrize("hard_stop", list(HardStop))
def test_hard_stops_have_distinct_unverified_and_process_messages(hard_stop: HardStop) -> None:
    unverified = stop_message(StopReason.NOT_YET_VERIFIED, hard_stop)
    incompatible = stop_message(StopReason.PROCESS_INCOMPATIBLE, hard_stop, official_quote=QUOTE)
    for other in set(HardStop) - {hard_stop}:
        assert unverified != stop_message(StopReason.NOT_YET_VERIFIED, other)
        assert unverified.text != stop_message(StopReason.NOT_YET_VERIFIED, other).text
        assert (
            incompatible.text
            != stop_message(StopReason.PROCESS_INCOMPATIBLE, other, official_quote=QUOTE).text
        )


@pytest.mark.parametrize("quote", [None, "", " ", "\n\t"])
@pytest.mark.parametrize("hard_stop", list(HardStop))
def test_process_incompatible_requires_saved_official_quote(
    hard_stop: HardStop, quote: str | None
) -> None:
    with pytest.raises(ValueError, match="saved official_quote"):
        stop_message(StopReason.PROCESS_INCOMPATIBLE, hard_stop, official_quote=quote)


@pytest.mark.parametrize("policy", [None, "", " ", "\n\t"])
def test_business_exclusion_requires_recorded_policy_reason(policy: str | None) -> None:
    with pytest.raises(ValueError, match="recorded policy_reason"):
        stop_message(StopReason.BUSINESS_EXCLUSION, policy_reason=policy)


def test_quote_and_policy_are_verbatim_data_and_do_not_change_message_keys() -> None:
    # Preserve whitespace and punctuation from existing source data without rewriting it.
    raw_quote = f" \n{QUOTE}\n "
    plain = stop_message(StopReason.PROCESS_INCOMPATIBLE, HardStop.S4, official_quote=QUOTE)
    padded = stop_message(StopReason.PROCESS_INCOMPATIBLE, HardStop.S4, official_quote=raw_quote)
    assert f'"{raw_quote}"' in padded.text
    assert padded.key == plain.key == "stop.process_incompatible.process_incompatible.s4"
    plain_policy = stop_message(StopReason.BUSINESS_EXCLUSION, policy_reason=POLICY)
    padded_policy = stop_message(StopReason.BUSINESS_EXCLUSION, policy_reason=f" {POLICY} ")
    assert f" {POLICY} " in padded_policy.text
    assert plain_policy.key == padded_policy.key
    assert plain == stop_message(StopReason.PROCESS_INCOMPATIBLE, HardStop.S4, official_quote=QUOTE)


def test_veto_keys_are_unique_and_do_not_change_the_stop_family() -> None:
    messages = [
        stop_message(StopReason.NOT_YET_VERIFIED, HardStop.S2, veto_code=code) for code in VetoCode
    ]
    assert len({message.key for message in messages}) == len(VetoCode)
    assert all(
        message.key.startswith("stop.not_yet_verified.not_yet_verified.s2.") for message in messages
    )
    assert (
        stop_message(StopReason.NOT_YET_VERIFIED, HardStop.S2, veto_code=VetoCode.PRIVATE_CLAIM).key
        == "stop.not_yet_verified.not_yet_verified.s2.private_claim"
    )


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (StopReason.DECLINED, "You chose not to continue"),
        (StopReason.WINDOW_EXPIRED, "This session ended"),
        (StopReason.ADDRESS_UNDELIVERABLE, "address could not be verified"),
        (StopReason.LOB_REJECTED, "provider did not accept this letter"),
        (StopReason.NOT_SENT, "confirmed that this letter was not sent"),
    ],
)
def test_transaction_failures_explain_what_happened(reason: StopReason, expected: str) -> None:
    assert expected in stop_message(reason).text


def test_irrelevant_evidence_is_rejected_instead_of_being_customer_copy() -> None:
    with pytest.raises(ValueError, match="official_quote requires PROCESS_INCOMPATIBLE"):
        stop_message(StopReason.NOT_YET_VERIFIED, HardStop.S2, official_quote=QUOTE)
    with pytest.raises(ValueError, match="policy_reason requires BUSINESS_EXCLUSION"):
        stop_message(StopReason.NOT_SENT, policy_reason=POLICY)
    with pytest.raises(ValueError, match="policy_reason must not be blank"):
        stop_message(StopReason.UNSUPPORTED, policy_reason=" ")


def test_enum_strings_are_rejected_at_the_presentation_boundary() -> None:
    with pytest.raises(ValueError, match="reason must be a StopReason"):
        stop_message("NOT_SENT")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="hard_stop must be a HardStop"):
        stop_message(StopReason.NOT_YET_VERIFIED, "S2")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="veto_code must be a VetoCode"):
        stop_message(StopReason.NOT_YET_VERIFIED, HardStop.S2, veto_code="NO_PAGES")  # type: ignore[arg-type]


def test_legacy_reasons_do_not_invent_official_requirements() -> None:
    assert "agency requires" not in stop_message(StopReason.NOT_FEASIBLE).text
    assert "official instructions say" not in stop_message(StopReason.UNSUPPORTED).text
    assert isinstance(stop_message(StopReason.UNRESOLVABLE_FACT), StopMessage)
