import { NONMAILING_REFUND, PAID_CAPTURE_TERMS } from "./legal";
import type { CaseStatus, MailTier, Tracking } from "./api";

type SubmissionStatus = Pick<CaseStatus, "case_id" | "state" | "payment_state">;
type SubmissionTracking = Pick<Tracking, "case_id" | "state">;

/** A paid mailing record requires the saved original and a confirmed charge. */
const PAID_ACCEPTED_STATES: readonly SubmissionStatus["state"][] = ["IN_TRANSIT", "DELIVERED"];

/** A free beta mailing has no payment to capture, so its original saved mailing record is
 *  the whole record. It is offered only while both the case and its matching postal record
 *  still report a mailing state, including a recorded return. */
const FREE_BETA_RECORD_STATES: readonly SubmissionStatus["state"][] = ["IN_TRANSIT", "DELIVERED", "RETURNED", "CLOSED_RETURNED"];

export const FIRST_CLASS_LIMITS = "Ordinary First-Class Mail comes without a tracking number, proof of delivery or a delivery guarantee.";

export function mailClassName(tier: MailTier): string {
  return tier === "STANDARD" ? "Ordinary First-Class Mail" : "Certified Mail";
}

export function dispatchSettled(status: Pick<CaseStatus, "state" | "payment_state">): boolean {
  return status.payment_state === "CAPTURED" || (
    status.payment_state === "NO_CHARGE" && FREE_BETA_RECORD_STATES.includes(status.state)
  );
}

export function matchingTracking(status: CaseStatus, tracking: Tracking | null): Tracking | null {
  if (tracking?.case_id !== status.case_id) return null;
  return status.selected_tier && status.selected_tier !== tracking.tier ? null : tracking;
}

/**
 * Confirmation always requires the tracking record to belong to this exact case.
 * CAPTURED keeps the original paid behavior: the case and its matching tracking must both
 * be IN_TRANSIT or DELIVERED. For NO_CHARGE the original mailing record is confirmed only
 * when both are IN_TRANSIT, DELIVERED, RETURNED or CLOSED_RETURNED. Every other state and
 * every mismatched case is refused.
 */
export function mailingConfirmed(status: SubmissionStatus, tracking: SubmissionTracking): boolean {
  if (status.case_id !== tracking.case_id) return false;
  if (status.payment_state === "NO_CHARGE") {
    return FREE_BETA_RECORD_STATES.includes(status.state)
      && FREE_BETA_RECORD_STATES.includes(tracking.state);
  }
  return PAID_ACCEPTED_STATES.includes(status.state)
    && PAID_ACCEPTED_STATES.includes(tracking.state)
    && status.payment_state === "CAPTURED";
}

/** Synthetic cards are available only in explicitly enabled offline development. */
export function offlinePaymentAllowed(providerMode: string, optedIn: boolean, development: boolean): boolean {
  return providerMode === "fake" && optedIn && development;
}

/** Resume from durable case state, independently of card capture or browser history. */
export function customerStage(status: CaseStatus): number {
  switch (status.state) {
    case "STOPPED_NO_CHARGE": return 8;
    case "FROZEN": case "SUBMITTING": case "IN_TRANSIT": case "DELIVERED":
    case "RETURNED": case "CLOSED_RETURNED": return 7;
    case "AWAITING_ADOPTION": return 6;
    default: return status.window_started_at || status.state !== "INTAKE" ? 3 : 4;
  }
}

export function paymentCopy(status: CaseStatus): string {
  const amount = status.amount_cents == null ? "" : `$${(status.amount_cents / 100).toFixed(2)} `;
  let captureExplanation: string;
  switch (status.original_capture_basis) {
    case "usps_scan":
      captureExplanation = "We charged after a qualifying USPS scan for your original letter.";
      break;
    case "hold_expiry_fallback":
      captureExplanation = "We charged as your card hold neared expiry. At that time, there was no qualifying USPS scan. Our mailing provider had prepared the exact PDF, and its scheduled processing time had passed without a reported failure, deletion or cancellation.";
      break;
    default:
      captureExplanation = "The saved record does not show what triggered the charge.";
  }
  switch (status.payment_state) {
    case "NO_CHARGE": return status.selected_tier === "STANDARD"
      ? "Your selected beta appeal costs $0. No card hold or payment is required."
      : "Your selected beta appeal is free. No card hold or payment is required.";
    case "AUTHORIZED": return `${amount}card hold authorized. ${PAID_CAPTURE_TERMS} ${NONMAILING_REFUND}`;
    case "CAPTURE_PENDING": return "We’re checking your existing payment. There is no need to authorize another payment.";
    case "CAPTURE_RETRYING": return "We’re still checking your existing card payment. No new payment is requested.";
    case "CAPTURED": {
      const payment = `${amount}card payment confirmed. ${captureExplanation}`;
      if (status.nonmailing_refund === "pending") return `${payment} We confirmed the letter was never mailed and are processing a full refund.`;
      if (status.nonmailing_refund === "confirmed") return `${payment} We confirmed the letter was never mailed and are checking the original charge for your full refund.`;
      if (status.nonmailing_refund === "evidence_conflict") return `${payment} We’re reviewing conflicting records for the original mailing.`;
      return payment;
    }
    case "RELEASED": return "Your card hold has been released.";
    case "REFUNDED": return status.nonmailing_refund === "refunded"
      ? `Your full original charge was refunded. ${captureExplanation}`
      : `Your original card payment is recorded as refunded. ${captureExplanation}`;
    case "WRITTEN_OFF": return "Payment collection has closed. We will not request another card payment for this mailing.";
    default: return "Payment status is not available yet. Check the saved status before trying another payment.";
  }
}

export function fulfillmentCopy(status: CaseStatus, tracking: Tracking | null): {title: string; detail: string} {
  const matched = matchingTracking(status, tracking);
  const firstClass = (status.selected_tier ?? matched?.tier) === "STANDARD";
  if (status.nonmailing_refund === "evidence_conflict") {
    return {title:"Checking conflicting mailing records.", detail:"We’re reviewing the evidence for your original letter."};
  }
  if (status.nonmailing_refund) {
    return {title:"Your original letter was never mailed.", detail:"The verified mailing record shows your original letter was never mailed. See Payment for your refund status."};
  }
  const fallbackObserved = status.original_capture_basis === "hold_expiry_fallback"
    || matched?.events.some(event => event.event === "processing_cutoff_passed");
  if (status.state === "IN_TRANSIT" && fallbackObserved) {
    if (status.original_mailing_evidence === "city_receipt") {
      return {title:"The agency confirmed receipt.", detail:"We verified that the agency received your original letter. Check with the agency for its decision."};
    }
    if (status.original_mailing_evidence !== "usps_scan") {
      return {title:"Your packet was prepared.", detail:"The scheduled processing time passed. We’re waiting for independent evidence that your letter was mailed."};
    }
    return {title:"USPS reported a scan.", detail:"A postal scan for your original letter was recorded after the hold-expiry check."};
  }
  switch (status.state) {
    case "FROZEN": return {title:"Mailing authorized.",detail:"Your approved packet is saved for mailing. Submission has not been confirmed yet."};
    case "SUBMITTING": return {title:"Your mailing is being confirmed.",detail:"We are checking the original mailing request. Please keep this appeal’s link; you do not need to approve or pay again."};
    case "RETURNED": case "CLOSED_RETURNED": return {title:"Mail returned.",detail:"The saved postal record reports a return. Any available remedy and its current status appear below."};
    case "DELIVERED": case "IN_TRANSIT":
      if (firstClass) return {title:"Mailing recorded.",detail:"Your mail provider accepted the packet for mailing. The agency can tell you whether it received the appeal."};
      if (status.state === "DELIVERED") {
        if (matched?.state === "DELIVERED") return {title:"USPS reports delivery.",detail:"This postal record does not establish when the agency opened, reviewed or decided your appeal."};
        return {title:"Checking the delivery record.",detail:"The saved case has a delivery update. Matching postal details are not available yet."};
      }
      if (matched?.events.length) return {title:"Your letter has postal updates.",detail:"Only reported postal events appear below. The agency’s decision comes separately from the agency."};
      return {title:"Waiting for postal updates.",detail:"Your mailing request was accepted. A USPS scan has not been reported here yet. Scan updates can be delayed."};
    default:
      return {title:"Checking the saved mailing.",detail:"The saved appeal does not show a mailing confirmation yet."};
  }
}
