"use client";

import React, { useState } from "react";
import { api } from "../lib/api";
import type { CaseStatus } from "../lib/api";

const ERRORS: Record<string, string> = {
  destination_verification_required: "Mailing is paused until the agency's appeal address is verified. Your saved address, consent and work are preserved; they do not authorize a new mailing.",
  network_unavailable: "We couldn’t confirm the address action. It may have been saved. Check the saved status before trying again.",
  destination_address_incomplete: "The address is incomplete. Check every required line, the two-letter state and the ZIP code, then try again.",
  destination_address_is_payment_address: "That looks like the payment address for this ticket. Please enter the contest or appeal address.",
  destination_disclosure_stale: "The terms above were updated. Read the current text and tick the box again if you still agree. Nothing was accepted.",
  destination_fingerprint_mismatch: "The saved address changed. Check the saved status and address, then tick the box again. Nothing was accepted.",
  destination_address_stale: "The saved address changed. Check the saved status and address, then tick the box again. Nothing was accepted.",
  destination_locked_after_draft: "Your letter has already been drafted, so the mailing address can no longer be chosen or changed here. This choice is closed.",
  destination_choice_not_open: "This choice is no longer open for this appeal. Check the saved status.",
  destination_choice_not_required: "This appeal no longer needs a customer-supplied address. Check the saved status.",
  destination_address_required: "Save a complete address before accepting the terms.",
  destination_consent_not_accepted: "The terms were not accepted. Tick the box only if you accept them for this address.",
  destination_consent_refused: "These terms can't be accepted for this address right now. Check the address and try again, or decline.",
  destination_decision_stale: "Your saved choice changed. Check the saved status before trying again.",
  destination_choice_requires_durable_service: "This preview can't save an address or record a choice. Nothing was saved, accepted or charged.",
  case_not_started: "This appeal hasn't started yet. Nothing was saved or accepted.",
  case_stopped: "This appeal has stopped. No charge was made and your work is saved.",
};
export function destinationError(detail: string): string {
  return ERRORS[detail] ?? "We couldn’t confirm that action. It may have been saved. Check the saved status before trying again.";
}
/** Paragraph-by-paragraph, verbatim. Nothing is trimmed, reworded or summarised. */
export function DisclosureText({ text }: { text: string }) {
  return (
    <div id="destDisclosure" className="dest-disclosure">
      {text.split("\n\n").map((paragraph, index) => (
        <p key={index} style={{ whiteSpace: "pre-wrap" }}>{paragraph}</p>
      ))}
    </div>
  );
}

interface Props {
  caseId: string;
  status: CaseStatus | null;
  onStatus: (status: CaseStatus) => void;
}

/** A stale response must never offer the retired unverified-address consent. */
export function DestinationChoice({ caseId, status, onStatus }: Props) {
  const [error, setError] = useState(false);
  const [busy, setBusy] = useState(false);
  if (!status?.destination_choice) return null;
  return (
    <div className="card dest-panel" id="destChoice" role="status">
      <h3>The agency’s appeal mailing address needs verification</h3>
      <p>Mailing is paused until the agency’s appeal address is verified. Your saved address, consent and work are preserved; they do not authorize a new mailing.</p>
      <button className="btn btn-primary" type="button" disabled={busy} onClick={async () => {
        setBusy(true); setError(false);
        try { onStatus(await api.status(caseId)); } catch { setError(true); }
        finally { setBusy(false); }
      }}>Check saved status</button>
      {error && <p className="err" role="alert">We couldn’t load the saved status. Your saved work is unchanged.</p>}
    </div>
  );
}
