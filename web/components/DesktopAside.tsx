import React from "react";
import type { FundingState, PhaseState } from "./CustomerAccess";

interface DesktopAsideProps {
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
  /** A paid-order refusal latch; removes every new-paid promise immediately. */
  newPaidBlocked?: boolean;
}

export function DesktopAside({
  onEvidenceClick,
  onRulesClick,
  servicePhase = "unknown",
  funding = "unknown",
  paidCase = false,
  savedPending = false,
  betaCase = false,
  newPaidBlocked = false,
}: DesktopAsideProps) {
  // Only a resolved paid phase plus resolved paid funding may promise a new card
  // charge. An existing linked paid case keeps its own saved paid wording under
  // either phase and is never relabeled free. A NO_CHARGE promise belongs only to
  // actual beta funding; every generic unselected/pending state must not claim
  // there was no earlier financial activity.
  const newCharge = !newPaidBlocked && !paidCase && !savedPending && !betaCase && servicePhase === "paid" && funding === "paid";
  return (
    <aside className="desk-aside" aria-label="Clerk principles and live evidence">
      <details className="guide-notes"><summary>Mailing and service details</summary>
      <h3>Your words.<br />The details handled.</h3>
      <p>
        We prepare and mail your letter in your own words. We don’t judge your case
        or predict how the agency will decide.
      </p>
      <p>
        <b>Every fact, sourced.</b> Each agency rule we use shows the official page
        and the exact words.
      </p>
      <p id="asideCharge">
        {paidCase ? (
          <><b>Your existing payment stays the same.</b> This appeal keeps its own saved mailing choice, payment status and window; no new card or payment is requested.</>
        ) : savedPending ? (
          <><b>We’re checking your existing payment.</b> No new card or replacement payment is requested; use Check my appeal for the latest update.</>
        ) : newCharge ? (
          <><b>No charge unless mailed.</b> Your card is charged only after the first USPS scan, or confirmed mailing production if scans are delayed.</>
        ) : betaCase ? (
          <><b>No card and no payment in this selected free beta.</b> This appeal is funded by the beta and is not charged.</>
        ) : (
          <><b>No new card or payment is requested now.</b> This appeal’s service phase, funding or any earlier saved payment is still being confirmed. Use Check my appeal for the latest update before choosing a mailing option.</>
        )}
      </p>
      <div className="aside-route" aria-label="Your appeal process">
        <span><i>01</i> Share your ticket &amp; your words</span>
        <span><i>02</i> Review the research &amp; letter</span>
        <span><i>03</i> Approve the exact mailing packet</span>
      </div>
      <div className="d-ledger">
        <p className="eyebrow" style={{ marginBottom: "6px" }}>
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
      </details>
    </aside>
  );
}
