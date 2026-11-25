import React from "react";
import type { MailTier } from "../lib/api";
import { NONMAILING_REFUND, PAID_CAPTURE_TERMS } from "../lib/legal";
import { FIRST_CLASS_LIMITS, mailClassName } from "../lib/fulfillment";
import type { FundingState, PhaseState } from "./CustomerAccess";
import { Icon } from "./Icons";

interface DesktopAsideProps {
  step?: number;
  citation?: string;
  onEvidenceClick: () => void;
  onRulesClick: () => void;
  /** Resolved immutable server posture; never guess it. */
  servicePhase?: PhaseState;
  /** Resolved per-case funding; separate from the server phase. */
  funding?: FundingState;
  /**
   * Authoritative saved case status: this linked case has its own paid payment
   * AND a launched window. Distinct from a new paid context.
   */
  paidCase?: boolean;
  /** This linked case's original payment is still being confirmed. */
  savedPending?: boolean;
  /** Actual beta funding (accepted free beta) for this case. */
  betaCase?: boolean;
  betaTier?: MailTier | null;
  /** A paid-order refusal latch; removes every new-paid promise immediately. */
  newPaidBlocked?: boolean;
}

const STEP_NOTES: Record<number, { kicker: string; title: string; body: string }> = {
  2: {
    kicker: "Your own words",
    title: "Plain facts. No invented claims.",
    body: "Tell us what happened as you saw it. Your return address goes on the mailed envelope and identifies your appeal to the agency.",
  },
  3: {
    kicker: "Official agency rules",
    title: "Every rule shows its source.",
    body: "We quote the agency’s published filing pages word for word so you can verify the mailing address, deadline rule, and required enclosures.",
  },
  4: {
    kicker: "Mailing & billing rules",
    title: "Nothing pre-selected.",
    body: "You choose how your appeal is mailed after seeing the confirmed agency and exact terms. You still inspect the exact PDF before anything is sent.",
  },
  5: {
    kicker: "Review window",
    title: "Unrushed review.",
    body: "Keep your case ID handy for support. Next we check the agency’s official filing pages and prepare a draft from your exact account.",
  },
  6: {
    kicker: "Exact packet control",
    title: "You approve the actual PDF.",
    body: "Edit any wording, adopt the text as your own, inspect every page plus the envelope addresses, and approve only when it matches what you want mailed.",
  },
  7: {
    kicker: "Mailing & payment ledger",
    title: "Separate, factual records.",
    body: "Payment status and postal events are tracked independently from actual provider records. Keep a PDF copy of your accepted packet.",
  },
  8: {
    kicker: "Safe stop",
    title: "Stopped before mailing.",
    body: "When an appeal falls outside clerical mail scope or cannot meet verified agency rules, we stop rather than guess—and keep your words saved.",
  },
};

export function DesktopAside({
  step = 2,
  citation,
  onEvidenceClick,
  onRulesClick,
  servicePhase = "unknown",
  funding = "unknown",
  paidCase = false,
  savedPending = false,
  betaCase = false,
  betaTier = null,
  newPaidBlocked = false,
}: DesktopAsideProps) {
  const newCharge = !newPaidBlocked && !paidCase && !savedPending && !betaCase && servicePhase === "paid" && funding === "paid";
  const note = STEP_NOTES[step] ?? STEP_NOTES[2];

  return (
    <aside className="desk-aside" aria-label="Clerk principles and live evidence">
      <p className="aside-kicker">{note.kicker}</p>
      {citation && (
        <p className="aside-citation mono">
          <Icon id="doc" size={14} /> Citation {citation}
        </p>
      )}
      <h3>{note.title}</h3>
      <p>{note.body}</p>

      <div className="aside-ledger-box">
        <p>
          <b>Every fact, sourced.</b> Each agency rule we use shows the official page and the exact words.
        </p>
        <p id="asideCharge">
          {paidCase ? (
            <><b>Your existing payment stays the same.</b> This appeal keeps its own saved mailing choice, payment status and window; no new card or payment is requested.</>
          ) : savedPending ? (
            <><b>We’re checking your existing payment.</b> No new card or replacement payment is requested; use Check my appeal for the latest update.</>
          ) : newCharge ? (
            <><b>Card hold first.</b> {PAID_CAPTURE_TERMS} {NONMAILING_REFUND}</>
          ) : betaCase ? (
            <><b>No card and no payment in this selected free beta.</b> {betaTier && <>{mailClassName(betaTier)} for $0. </>}{betaTier === "STANDARD" && FIRST_CLASS_LIMITS}</>
          ) : (
            <><b>No new card or payment is requested now.</b> This appeal’s service phase, funding or any earlier saved payment is still being confirmed. Use Check my appeal for the latest update before choosing a mailing option.</>
          )}
        </p>
      </div>

      <div className="aside-route" aria-label="Your appeal process">
        <span><i>01</i> Share your ticket &amp; your words</span>
        <span><i>02</i> Review the research &amp; letter</span>
        <span><i>03</i> Approve the exact mailing packet</span>
      </div>

      <div className="d-ledger">
        <p className="eyebrow" style={{ marginBottom: "8px" }}>
          Quick Reference
        </p>
        <div className="row">
          <button className="chip" type="button" onClick={onEvidenceClick}>
            Evidence Checklist
          </button>
          <button className="chip" type="button" onClick={onRulesClick}>
            What we never do
          </button>
        </div>
      </div>
    </aside>
  );
}
