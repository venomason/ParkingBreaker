"""Synthetic frozen mailing requests shared by tests; no customer records."""

from datetime import datetime

from parkingbreaker.address_policy import (
    AddressProvenance,
    DestinationDecision,
    EvidenceKind,
    EvidenceRef,
    address_fingerprint,
)
from parkingbreaker.agencies import MailingAddress
from parkingbreaker.lob_submission import LetterOptions, LetterRequest
from parkingbreaker.mail_config import MailClass
from parkingbreaker.transaction_records import CaseRecord
from parkingbreaker.transaction_store import SqliteTransactionStore

SENDER = MailingAddress("Test Customer", None, "123 Example Street", None, "Oakland", "CA", "94612")
RECIPIENT = MailingAddress(
    "Test Agency", "Appeal Review", "345 Example Avenue", "Suite 2", "Oakland", "CA", "94612"
)
OPTIONS = LetterOptions(MailClass.CERTIFIED)


def make_request(
    pdf: bytes = b"%PDF-1.4\nfrozen synthetic packet", token: str = "synthetic-random-token-1"
) -> LetterRequest:
    return LetterRequest(pdf, token, SENDER, RECIPIENT, OPTIONS)


def seed_destination(
    store: SqliteTransactionStore,
    case: CaseRecord,
    now: datetime,
    address: MailingAddress = RECIPIENT,
) -> DestinationDecision:
    """Give low-level mailing fixtures the explicit intake instruction production requires."""
    intake = store.intake_for_workspace(case.workspace_token)
    if intake is None:
        owner = store.workspace_owner(case.workspace_token) or "synthetic-owner"
        store.set_workspace_owner(case.workspace_token, owner, now)
        intake = store.record_intake(
            f"intake-{case.case_token}", case.workspace_token, "{}", "{}", now
        )
    if intake.case_token is None:
        intake = store.update_intake(
            intake.intake_id,
            intake.revision,
            selected_tier=intake.selected_tier,
            case_token=case.case_token,
        )
    saved = store.latest_destination_decision(intake.intake_id, intake.owner)
    decision = DestinationDecision(
        intake.intake_id,
        intake.owner,
        address,
        address_fingerprint(address),
        AddressProvenance.SYSTEM_VERIFIED,
        "address-responsibility-v1",
        now,
        verification_evidence=(
            EvidenceRef("synthetic-quote", "a" * 64, EvidenceKind.OFFICIAL_SOURCE_QUOTE),
        ),
    )
    store.save_destination_decision(
        intake.intake_id, intake.owner, decision, None if saved is None else saved.revision
    )
    return decision


def seed_approval(store: SqliteTransactionStore, case_token: str, attempt: int = 1) -> None:
    """Explicit approval for handcrafted send-ready fixtures, never production bypass."""
    from parkingbreaker.approval_manifest import (
        build_approval_manifest,
        build_remail_approval_manifest,
    )

    prepared = store.get(case_token, attempt)
    assert prepared is not None
    if attempt == 2:
        original = store.get_approval_manifest(case_token, 1)
        assert original is not None
        manifest = build_remail_approval_manifest(
            original, prepared.request, approved_at=prepared.prepared_at
        )
    else:
        case = store.get_case(case_token)
        assert case is not None
        intake = store.intake_for_workspace(case.workspace_token)
        manifest = build_approval_manifest(
            case_id=case_token if intake is None else intake.intake_id,
            case_token=case_token,
            attempt=attempt,
            approved_at=prepared.prepared_at,
            build_commit=None,
            raw_customer_text="Synthetic customer account.",
            approved_text="Synthetic customer account.",
            request=prepared.request,
            affirmations=(),
        )
    store.record_approval_manifest(manifest)
