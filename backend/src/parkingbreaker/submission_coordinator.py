"""One durable submission step through an injected Lob seam (v6 §6.3).

Consumes an already approved SUBMITTING case; never authorizes a mailing.
Caller supplies a clock, optimistic revisions and ids; pure policy reads no clock.
"""

import sqlite3
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime

from parkingbreaker.address_disclosure import DISCLOSURE_SHA256
from parkingbreaker.address_policy import address_fingerprint, mailing_gate
from parkingbreaker.address_store import prepared_route_scope, research_route_scope
from parkingbreaker.approval_manifest import (
    ApprovalIntegrityError,
    verify_dispatch_integrity,
    verify_remail_approval,
)
from parkingbreaker.case_state import CaseState
from parkingbreaker.intake_store import IntakeConflict, IntakeRecord
from parkingbreaker.lob_submission import (
    FALLBACK_LEAD,
    LOCAL_REFUSAL_REASON,
    Accepted,
    CancelableLobClient,
    Canceled,
    CancelOutcome,
    CancelRequest,
    LetterEvidence,
    LetterRequest,
    LobResponse,
    MetadataLookup,
    Rejected,
    SubmissionOutcome,
    Uncertain,
    cancel_resolution,
    classify_response,
    create_allowed,
    fallback_resolution,
    pending_evidence,
    reconcile_with_original,
    resolve_lookup,
)
from parkingbreaker.mail_attempt_store import PreparedMailAttempt, StorageCorruption
from parkingbreaker.payment_state import EXPIRY_MARGIN, PaymentState
from parkingbreaker.research.run import ResearchResult
from parkingbreaker.research_records import decode_agency, decode_answers, decode_research
from parkingbreaker.submission_records import SubmissionCursor
from parkingbreaker.transaction_store import (
    GATE_REFUSED_REASON,
    RevisionConflict,
    SqliteTransactionStore,
)

# Fixed, sanitized reason for a NO_CHARGE retry refusal that follows an earlier call
# intent: the provider's 4xx text is not kept and the refusal cannot prove a non-send.
RETRY_REJECTION_REASON = "retry refused after an earlier call intent"

type CreateGuard = Callable[[CancelableLobClient], CancelableLobClient]


class _RouteRefused(Exception):
    """The final route check refused locally before calling Lob."""


@dataclass(frozen=True)
class _MailRouteGuard:
    client: CancelableLobClient
    allowed: Callable[[], bool]

    def create_letter(self, request: LetterRequest, idempotency_key: str) -> LobResponse:
        if not self.allowed():
            raise _RouteRefused
        return self.client.create_letter(request, idempotency_key)

    def find_by_metadata(self, submission_token: str) -> str | MetadataLookup | None:
        return self.client.find_by_metadata(submission_token)

    def lookup_letter(self, letter_id: str, submission_token: str) -> MetadataLookup:
        return self.client.lookup_letter(letter_id, submission_token)

    def cancel_letter(self, letter_id: str) -> CancelOutcome:
        return self.client.cancel_letter(letter_id)


def _route_allows_create(
    store: SqliteTransactionStore,
    prepared: PreparedMailAttempt,
    intake: IntakeRecord | None,
    research: ResearchResult | None,
    now: datetime,
) -> bool:
    """Require the same current verified route and exact frozen address for monitored mail."""
    try:
        scope = (
            None
            if intake is None
            else prepared_route_scope(
                intake, prepared.request, None if research is None else research.ticket
            )
        )
        if scope is None and intake is not None and intake.agency_json is not None:
            issuer = decode_agency(intake.agency_json).id
            mail_class = prepared.request.options.mail_class.value
            if any(
                route.scope.issuer == issuer and route.scope.mail_class == mail_class
                for route in store.monitored_routes()
            ):
                # An unknown legacy stage cannot identify which enrolled route applies.
                return False
        bound = store.bound_route(prepared.case_token, prepared.attempt)
        current = None if scope is None else store.current_route(scope)
        if intake is not None:
            entitlement = store.beta_entitlement(intake.intake_id)
            pilot = entitlement.pilot_scope if entitlement is not None else None
            if pilot is not None and (pilot != scope or current is None):
                return False
        fingerprint = address_fingerprint(prepared.request.recipient)
        if current is None:
            return (bound is None or bound == (scope, 0)) and not store.withdrawn_address(
                fingerprint, now
            )
        return (
            bound == (scope, current.revision)
            and current.address_fingerprint == fingerprint
            and research is not None
            and research_route_scope(research.ticket, prepared.request) == scope
            and store.route_check_current(current, now)
        )
    except (StorageCorruption, ValueError, sqlite3.Error):
        return False


def _fallback_lookup(
    client: CancelableLobClient, token: str, original_id: str | None
) -> str | MetadataLookup | None:
    """Fresh evidence for the fallback: the token search, then the known original by id.

    The direct retrieval also sees a letter the search no longer lists (failed/deleted).
    Provider failures propagate: they are uncertain and keep the hold.
    """
    try:
        found = client.find_by_metadata(token)
    except Exception:
        if original_id is None:
            raise
        found = None
    if original_id is not None and not (
        isinstance(found, MetadataLookup) and found.letter_id is not None
    ):
        return client.lookup_letter(original_id, token)
    return found


def _beta_evidence_resolution(
    found: str | MetadataLookup | None, original_letter_id: str | None
) -> SubmissionOutcome:
    """Resolve original-letter evidence for a NO_CHARGE case without paid policy.

    A production letter may be accepted, trustworthy absence may stop and explicit VOIDED
    evidence may resolve as Canceled. A created or rendered letter may still be mailed, so
    it stays uncertain: with no capture, cancellation window or financial fallback there is
    nothing that could safely cancel it here. Never sends a cancellation request.
    """
    resolved = resolve_lookup(found, original_letter_id)
    if not isinstance(found, MetadataLookup) or type(found.authoritative) is not bool:
        return resolved
    letter_id = found.letter_id
    if not isinstance(letter_id, str) or not letter_id.strip():
        return resolved
    if original_letter_id is not None and letter_id != original_letter_id:
        return resolved
    if found.evidence is LetterEvidence.VOIDED:
        return Canceled(letter_id)
    if isinstance(resolved, Accepted):
        return resolved
    # Same attempt token and same letter id, but not production evidence yet: keep the
    # identity anchored so a later lookup or voided answer speaks about this exact letter.
    return Uncertain(
        f"lookup found letter {found.evidence.value}; no production evidence",
        letter_id,
        found.evidence is LetterEvidence.RENDERED,
        found.tracking_number,
    )


def run_submission_step(
    store: SqliteTransactionStore,
    client: CancelableLobClient,
    case_id: str,
    attempt: int,
    *,
    expected_case_revision: int,
    expected_cursor_revision: int | None,
    operation_id: str,
    clock: Callable[[], datetime],
    new_effect_id: Callable[[], str] | None = None,
    already_owned: bool = False,
    effect_ids: tuple[str, ...] = (),
    validate_prepared: Callable[[PreparedMailAttempt], None] | None = None,
    create_guards: tuple[CreateGuard, ...] = (),
) -> SubmissionCursor:
    """Perform at most one due call. No sleeps, network setup or implicit clock.

    Exceptions from the seam are uncertain. Process death/BaseException or failed
    outcome commits leave the committed call intent for same-key recovery.
    """
    if already_owned and not store._submission_locked:
        raise RevisionConflict("exclusive ownership required")
    with nullcontext() if already_owned else store.submission_ownership():
        previous = store.submission_cursor(case_id, attempt)
        if (None if previous is None else previous.revision) != expected_cursor_revision:
            raise RevisionConflict("submission cursor revision is stale")
        if previous is not None and not pending_evidence(previous.outcome):
            return previous
        now = clock()
        claimed, kind = store.claim_submission(
            case_id,
            attempt,
            expected_case_revision,
            expected_cursor_revision,
            now=now,
            operation_id=operation_id,
        )
        prepared = store.get(case_id, attempt)
        if prepared is None:
            raise RuntimeError("committed frozen request disappeared")
        # Address records use the stable intake id, not the transaction token.
        # These are lookups only; legacy/unlinked cases acquire no implicit decision.
        gate = None
        integrity_error: str | None = None
        research = None
        intake = None
        if kind == "create":
            # The one free re-mail follows a return: it needs a SYSTEM_VERIFIED destination
            # that differs from the address the returned letter went to.
            first_attempt = store.get(case_id, 1) if attempt == 2 else None
            if attempt == 2 and first_attempt is None:
                raise RuntimeError("re-mail without the returned attempt's frozen request")
            returned_address = None if first_attempt is None else first_attempt.request.recipient
            case = store.get_case(case_id)
            intake = None if case is None else store.intake_for_workspace(case.workspace_token)
            if intake is not None and intake.case_token != case_id:
                intake = None
            manifest = store.get_approval_manifest(case_id, attempt)
            try:
                if attempt == 2:
                    verify_remail_approval(
                        store.get_approval_manifest(case_id, 1), manifest, prepared.prepared_at
                    )
                if manifest is None:
                    if intake is not None:
                        raise ApprovalIntegrityError("missing_approval_manifest")
                else:
                    verify_dispatch_integrity(manifest, prepared.request, manifest.affirmations)
            except ApprovalIntegrityError as exc:
                integrity_error = str(exc) or "approval_manifest_integrity_failed"
            try:
                saved = (
                    None
                    if intake is None
                    else store.latest_destination_decision(intake.intake_id, intake.owner)
                )
                frozen = store.frozen_destination(case_id, attempt)
                consents = (
                    ()
                    if intake is None
                    else store.address_consent_events(intake.intake_id, intake.owner)
                )
                addresses = (
                    ()
                    if intake is None
                    else store.customer_address_revisions(intake.intake_id, intake.owner)
                )
            except (IntakeConflict, StorageCorruption):
                # Invalid address records cannot authorize a create.
                saved, frozen, consents, addresses = None, None, (), ()
            gate = mailing_gate(
                frozen if saved is not None and saved.decision == frozen else None,
                consents,
                case_id=case_id if intake is None else intake.intake_id,
                owner="" if intake is None else intake.owner,
                mailed_address=prepared.request.recipient,
                latest_customer_address_revision=None if not addresses else addresses[-1].revision,
                now=now,
                current_disclosure_sha256=DISCLOSURE_SHA256,
                returned_address=returned_address,
            )
            # Recheck saved runtime research under current capabilities. Preserve
            # historical intents: refusal follows the existing no-create/reconcile
            # branch below, never a bulk state or frozen-packet rewrite.
            if gate.allowed:
                try:
                    research_record = (
                        None if intake is None else store.get_research(intake.intake_id)
                    )
                    if research_record is not None:
                        research = decode_research(research_record.research_json)
                        resolution = research.resolve(decode_answers(research_record.answers_json))
                        if not resolution.research_complete or not research.evidence_current_at(
                            now
                        ):
                            raise ValueError("current research is unresolved or stale")
                    if validate_prepared is not None:
                        # CaseService always supplies its approval-context validator.
                        # Standalone coordinator callers have no photo/intake services;
                        # their existing durable identity gates remain independent.
                        validate_prepared(prepared)
                except Exception:
                    # Missing/corrupt application context or validator failure refuses
                    # create; a prior intent still follows uncertainty below.
                    integrity_error = "submission_requirements_not_verified"

        outcome: SubmissionOutcome
        prior = None if previous is None else previous.outcome
        original_id = prior.letter_id if isinstance(prior, Uncertain | Accepted) else None
        case = store.get_case(case_id)
        assert case is not None
        # A NO_CHARGE case has no Stripe authorization: its stored window is the existing
        # customer-work window, so the paid capture-fallback schedule does not apply.
        financial = case.payment_state is not PaymentState.NO_CHARGE
        last_safe_capture = case.authorization_expires_at - EXPIRY_MARGIN
        at_fallback = (
            financial
            and previous is not None
            and attempt == 1
            and now >= last_safe_capture - FALLBACK_LEAD
        )
        try:
            call_at = clock()
            if call_at.tzinfo is None or call_at < now:
                raise ValueError("invalid caller call time")
            if research is not None and not research.evidence_current_at(call_at):
                integrity_error = "submission_requirements_not_verified"
            # The fallback bounds an existing mailing-evidence wait. A first create
            # still uses the existing pre-send authorization guard and safety margin.
            at_fallback = (
                financial
                and previous is not None
                and attempt == 1
                and call_at >= last_safe_capture - FALLBACK_LEAD
            )
            if at_fallback:
                # Always obtain fresh evidence, even after a long outage; cached render
                # evidence cannot win. A letter that may still be mailed is cancelled or
                # captured, never released: only Lob's confirmation frees the hold.
                token = prepared.request.submission_token
                found = _fallback_lookup(client, token, original_id)
                step = fallback_resolution(found, original_id, call_at, last_safe_capture)
                if isinstance(step, CancelRequest):
                    if case.letter_state is CaseState.SUBMITTING:
                        tracking = (
                            found.tracking_number if isinstance(found, MetadataLookup) else None
                        )
                        step = cancel_resolution(
                            client.cancel_letter(step.letter_id),
                            step.letter_id,
                            tracking,
                        )
                    else:
                        # Historical mailed-state cursors can never be released.
                        step = Uncertain("legacy letter needs mailing evidence", step.letter_id)
                outcome = step
            elif kind == "create" and integrity_error is not None:
                # Tampered or missing approval manifest refuses before any provider call.
                outcome = (
                    Rejected(422, LOCAL_REFUSAL_REASON)
                    if previous is None
                    else Uncertain(
                        "approval manifest integrity check failed after earlier call intent"
                    )
                )
            elif gate is not None and not gate.allowed:
                # A pre-claim cursor carries a committed first_sent_at/create intent.
                # Only a first intent can be refused as a definite local non-send.
                if previous is None:
                    outcome = Rejected(422, gate.reason)
                else:
                    found_outcome = resolve_lookup(
                        client.find_by_metadata(prepared.request.submission_token), original_id
                    )
                    # This is still a create operation: absence cannot free the hold.
                    outcome = (
                        found_outcome
                        if isinstance(found_outcome, Accepted)
                        else Uncertain("gate refused retry; awaiting authoritative lookup")
                    )
            elif kind == "create" and not create_allowed(claimed.first_sent_at, call_at):
                outcome = Uncertain("create cutoff reached before call")
            elif kind == "lookup":
                if financial:
                    found = client.find_by_metadata(prepared.request.submission_token)
                    outcome = resolve_lookup(found, original_id)
                else:
                    # Metadata search plus the exact known-ID retrieval, resolved without
                    # any expiry, capture-fallback or cancellation policy.
                    outcome = _beta_evidence_resolution(
                        _fallback_lookup(client, prepared.request.submission_token, original_id),
                        original_id,
                    )
            else:
                # Install the route check closest to Lob, after any application guards
                # that read the clock or revalidate the original ticket and deadline.
                def route_allowed() -> bool:
                    return _route_allows_create(store, prepared, intake, research, clock())

                create_client: CancelableLobClient = _MailRouteGuard(client, route_allowed)
                for guard in create_guards:
                    create_client = guard(create_client)
                try:
                    response = create_client.create_letter(prepared.request, prepared.key)
                except _RouteRefused:
                    if previous is None:
                        outcome = Rejected(422, GATE_REFUSED_REASON)
                    else:
                        found = _fallback_lookup(
                            client, prepared.request.submission_token, original_id
                        )
                        resolved = (
                            resolve_lookup(found, original_id)
                            if financial
                            else _beta_evidence_resolution(found, original_id)
                        )
                        # This operation reserved a create rather than a lookup. Keep
                        # absence/cancellation for the existing dedicated lookup policy.
                        outcome = (
                            resolved
                            if isinstance(resolved, Accepted | Uncertain)
                            else Uncertain("route refused retry; awaiting authoritative lookup")
                        )
                else:
                    # A call intent already exists, so a request may have reached the
                    # provider earlier; only a first intent is a definite non-send.
                    if response.local_refusal and previous is not None:
                        outcome = Uncertain("local refusal after an earlier call intent")
                    else:
                        outcome = classify_response(response)
                        if not financial and previous is not None and isinstance(outcome, Rejected):
                            # A free-beta retry can be refused by validation after an earlier
                            # intent may already have created the original. A 4xx now does not
                            # reach back and prove that earlier request was never sent, so keep
                            # the hold; existing lookup still resolves it.
                            outcome = Uncertain(RETRY_REJECTION_REASON)
        except Exception:
            # Do not store potentially identifying provider exception text. The hold stays.
            outcome = classify_response(LobResponse(None, error="provider call failed"))
        outcome = reconcile_with_original(prior, outcome)
        # Historical failure notifications never override this exact-letter lookup.
        # A current USPS scan proves possession; unverified/conflicting identities
        # stay uncertain. Release requires fresh authoritative absence, voiding or
        # confirmed cancellation through the existing fallback policy (H1).
        finished_at = clock()
        if (
            isinstance(outcome, Accepted)
            and store.get_approval_manifest(case_id, attempt) is not None
        ):
            store.record_dispatch_manifest(
                case_id,
                attempt,
                provider_letter_id=outcome.letter_id,
                expected_delivery=None,
                dispatched_pdf=prepared.request.pdf,
                dispatched_at=finished_at,
            )
        return store.finish_submission(
            claimed, outcome, now=finished_at, effect_ids=effect_ids, new_effect_id=new_effect_id
        )
