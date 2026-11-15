"""Customer copy for no-charge stops. Pure: callers supply all recorded context.

Keys depend only on stable enums, never on evidence or policy wording. Official
quotes and policy reasons must come from the saved record, not generated prose.
This module presents a stop; it does not decide it or verify its evidence.
"""

from dataclasses import dataclass

from parkingbreaker.case_state import HardStop, StopFamily, StopReason, stop_family
from parkingbreaker.research.veto_codes import VetoCode
from parkingbreaker.service_policy import MAX_TICKET_AGE_DAYS, IntakeStop


@dataclass(frozen=True)
class StopMessage:
    key: str
    text: str


_REASSURANCE = "Nothing was charged. Your work is saved."
_RETRY = "You can review your saved details and try again."
_OFFICIAL_NEXT = "You can check the agency's official instructions for other ways to proceed."

_UNVERIFIED: dict[HardStop, str] = {
    HardStop.S1: "a mail-in appeal route for this ticket and stage",
    HardStop.S2: "the correct address for an appeal at this stage",
    HardStop.S3: "the procedural requirements for this appeal",
    HardStop.S4: "the applicable deadline and whether mailing can meet it",
    HardStop.S5: "the mailing requirements for this appeal",
    HardStop.S6: "the requirements for our service to prepare this appeal",
    HardStop.S7: "the official information needed to prepare this appeal",
}

_PROCESS: dict[HardStop, str] = {
    HardStop.S1: "The agency requires a submission method our mailed letter cannot provide.",
    HardStop.S2: "The agency requires an appeal channel our mailed letter cannot provide.",
    HardStop.S3: (
        "The agency requires a condition for this review that our mailed letter cannot meet."
    ),
    HardStop.S4: "The agency requires timing our mailed letter cannot meet.",
    HardStop.S5: "The agency requires a mailing service our mailed letter cannot provide.",
    HardStop.S6: "The agency requires a service condition our mailed letter cannot meet.",
    HardStop.S7: "The agency requires a condition our mailed letter cannot meet.",
}

# Internal guard names are deliberately translated into the unresolved customer fact.
_VETO: dict[VetoCode, str] = {
    VetoCode.CITATION_OFF_DOMAIN: "that the supporting information comes from an official source",
    VetoCode.DELEGATED_ROOT: "the official source for this appeal",
    VetoCode.ISSUER_NOT_NAMED: "which agency the appeal page serves",
    VetoCode.UNSAFE_URL: "an official page we can safely access",
    VetoCode.PURPOSE_UNRESOLVED: "that the address accepts appeals at this stage",
    VetoCode.PURPOSE_NOT_CONTEST: "an address for appeals rather than other correspondence",
    VetoCode.QUERY_TENANT: "that the appeal page belongs to your agency",
    VetoCode.ROOT_NOT_DOTGOV: "the agency's official website",
    VetoCode.UNSUPPORTED_ROOT: "the agency's official website",
    VetoCode.ROOT_PATH: "the agency's official website",
    VetoCode.MALFORMED_ASSESSMENT: "the agency details from the information reviewed",
    VetoCode.NO_ASSESSMENT: "the agency details from the information reviewed",
    VetoCode.QUOTE_MISSING: "the official wording supporting the appeal details",
    VetoCode.QUOTE_NOT_ON_LINK: "the supporting wording on the linked official page",
    VetoCode.NOT_ISSUER: "that the page serves the agency on your ticket",
    VetoCode.PAYMENT_AS_CONTEST: "an appeal address separate from a payment address",
    VetoCode.AI_GUARD_DISAGREE: "that the appeal details match the official source",
    VetoCode.STAGE_MISMATCH: "that the official instructions apply to your appeal stage",
    VetoCode.NO_CANDIDATE: "which official agency issued your ticket",
    VetoCode.PRIVATE_CLAIM: "whether the issuer is a public agency or a private operator",
    VetoCode.NO_OFFICIAL_DOMAINS: "the agency's official website",
    VetoCode.PAGE_NOT_SAVED: "the supporting information from a saved official page",
    VetoCode.PAGE_HASH_CONFLICT: "a consistent saved copy of the official information",
    VetoCode.QUOTE_NOT_ON_PAGE: "the supporting wording on the saved official page",
    VetoCode.AI_CONFLICT: "consistent official information for this appeal",
    VetoCode.ISSUER_ITEMS_MISSING: "all the agency details needed for this appeal",
    VetoCode.TYPE_INCOMPATIBLE: "the type of agency that issued your ticket",
    VetoCode.STATE_MISMATCH: "which state the issuing agency serves",
    VetoCode.TRIBAL_BASIS: "the requirements for our service to prepare this tribal appeal",
    VetoCode.NAME_MISMATCH: "that the official agency matches the name on your ticket",
    VetoCode.SNAPSHOT_STALE: "the agency's current official website",
    VetoCode.BUDGET_EXHAUSTED: "the remaining details within this attempt's research limit",
    VetoCode.NO_PAGES: "the appeal details from accessible official pages",
    VetoCode.UNREAD_LINKED_ARTIFACT: "the forms and instructions linked from the appeal page",
    VetoCode.STATUTE_ONLY_ADDRESS: "an appeal address published by the agency itself",
}

# Explicit entries keep legacy stored reasons readable without inventing evidence.
_REASON: dict[StopReason, tuple[str, str]] = {
    StopReason.PROCESS_INCOMPATIBLE: ("", _OFFICIAL_NEXT),
    StopReason.NOT_YET_VERIFIED: ("", _RETRY),
    StopReason.BUSINESS_EXCLUSION: (
        "This request is outside our current service scope.",
        _OFFICIAL_NEXT,
    ),
    StopReason.UNSUPPORTED: ("This saved request was outside our service scope.", _OFFICIAL_NEXT),
    StopReason.UNRESOLVABLE_FACT: (
        "We couldn't confirm the information needed for this appeal yet.",
        _RETRY,
    ),
    StopReason.NOT_FEASIBLE: (
        "This saved attempt stopped because the mailing timing could not be met.",
        _OFFICIAL_NEXT,
    ),
    StopReason.DECLINED: ("You chose not to continue this attempt.", _RETRY),
    StopReason.AUTHORIZATION_EXPIRED: (
        "Your payment hold expired before mailing; you were not charged.",
        _RETRY,
    ),
    StopReason.DESTINATION_NOT_CONFIRMED: (
        "We stopped before mailing because the mailing address could not be confirmed "
        "for this letter. You were not charged.",
        _RETRY,
    ),
    StopReason.LETTER_NOT_SUBMITTABLE: (
        "We stopped before mailing because this letter could not be prepared for mailing. "
        "You were not charged.",
        _RETRY,
    ),
    StopReason.WINDOW_EXPIRED: ("This session ended before mailing was submitted.", _RETRY),
    StopReason.ADDRESS_UNDELIVERABLE: (
        "The mailing address could not be verified for delivery.",
        "You can check the address against the agency's official appeal instructions, then retry.",
    ),
    StopReason.LOB_REJECTED: (
        "The mailing provider did not accept this letter for mailing.",
        _RETRY,
    ),
    StopReason.NOT_SENT: ("We confirmed that this letter was not sent.", _RETRY),
    StopReason.DRAFTING_UNAVAILABLE: (
        "We could not prepare your letter right now.",
        _RETRY,
    ),
}


def stop_message(
    reason: StopReason,
    hard_stop: HardStop | None = None,
    *,
    veto_code: VetoCode | None = None,
    official_quote: str | None = None,
    policy_reason: str | None = None,
    intake_stop: IntakeStop | None = None,
    issuer_name: str | None = None,
) -> StopMessage:
    """Translate a recorded no-charge stop into a stable key and complete message.

    The family comes from case_state.stop_family, preventing contradictory inputs.
    Current research reasons require S1-S7, just like case_state.Stopped. Legacy
    reasons retain their own copy and need no new evidence. Vetoes refine only
    NOT_YET_VERIFIED stops. Provider errors and internal detail are never rendered.
    """
    if not isinstance(reason, StopReason):
        raise ValueError("reason must be a StopReason")
    if hard_stop is not None and not isinstance(hard_stop, HardStop):
        raise ValueError("hard_stop must be a HardStop")
    if veto_code is not None and not isinstance(veto_code, VetoCode):
        raise ValueError("veto_code must be a VetoCode")
    research_reason = reason in {StopReason.PROCESS_INCOMPATIBLE, StopReason.NOT_YET_VERIFIED}
    if research_reason != (hard_stop is not None):
        raise ValueError("only current research reasons require a hard_stop")
    family = stop_family(reason)
    if veto_code is not None and family is not StopFamily.NOT_YET_VERIFIED:
        raise ValueError("veto_code requires NOT_YET_VERIFIED")
    if official_quote is not None and reason is not StopReason.PROCESS_INCOMPATIBLE:
        raise ValueError("official_quote requires PROCESS_INCOMPATIBLE")
    if policy_reason is not None and family is not StopFamily.BUSINESS_EXCLUSION:
        raise ValueError("policy_reason requires BUSINESS_EXCLUSION")

    happened, next_step = _REASON[reason]
    key = f"stop.{family.value.lower()}.{reason.value.lower()}"
    if hard_stop is not None:
        key += f".{hard_stop.value.lower()}"
        if reason is StopReason.PROCESS_INCOMPATIBLE:
            if official_quote is None or not official_quote.strip():
                raise ValueError("PROCESS_INCOMPATIBLE requires a saved official_quote")
            happened = f'{_PROCESS[hard_stop]} The official instructions say: "{official_quote}"'
        else:
            happened = f"We couldn't confirm {_UNVERIFIED[hard_stop]} yet."
    if reason is StopReason.BUSINESS_EXCLUSION and (
        policy_reason is None or not policy_reason.strip()
    ):
        raise ValueError("BUSINESS_EXCLUSION requires a recorded policy_reason")
    if policy_reason is not None:
        if not policy_reason.strip():
            raise ValueError("policy_reason must not be blank")
        happened += f" Service policy: {policy_reason}"
    if veto_code is not None:
        key += f".{veto_code.value.lower()}"
        happened += f" We couldn't confirm {_VETO[veto_code]} yet."
    if issuer_name is not None and reason is StopReason.NOT_YET_VERIFIED:
        happened = (
            f"We can't confirm how {issuer_name} handles mailed appeals, "
            "so we won't take your payment."
        )
    if intake_stop is not None:
        key += f".{intake_stop.value}"
        happened, next_step = {
            IntakeStop.TOO_OLD: (
                f"ParkingBreaker accepts tickets up to {MAX_TICKET_AGE_DAYS} days old. "
                "This ticket is outside our service's age limit.",
                _OFFICIAL_NEXT,
            ),
            IntakeStop.RENTAL_PAID: (
                "The rental company has already paid this ticket. "
                "A paid ticket usually cannot be appealed.",
                "Contact the rental company about the payment.",
            ),
            IntakeStop.RENTAL_PAYMENT_UNKNOWN: (
                "We need to confirm whether the rental company has already paid this ticket "
                "before we can take your payment.",
                "Contact the rental company to check, then return with its answer.",
            ),
        }[intake_stop]
    return StopMessage(key, f"{happened} {_REASSURANCE} {next_step}")
