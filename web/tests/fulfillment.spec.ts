import { expect, test } from "@playwright/test";
import { mailingConfirmed, offlinePaymentAllowed } from "../lib/fulfillment";
import type { CaseStatus } from "../lib/api";

const states: CaseStatus["state"][] = [
  "INTAKE", "RESEARCHING", "NEEDS_CUSTOMER", "DRAFTING", "AWAITING_ADOPTION",
  "FROZEN", "SUBMITTING", "IN_TRANSIT", "DELIVERED", "RETURNED", "CLOSED_RETURNED", "STOPPED_NO_CHARGE",
];
const payments: CaseStatus["payment_state"][] = [
  null, "NO_CHARGE", "AUTHORIZED", "CAPTURE_PENDING", "CAPTURE_RETRYING", "CAPTURED", "WRITTEN_OFF", "RELEASED", "REFUNDED",
];
const paidAccepted: CaseStatus["state"][] = ["IN_TRANSIT", "DELIVERED"];
const freeBetaRecord: CaseStatus["state"][] = ["IN_TRANSIT", "DELIVERED", "RETURNED", "CLOSED_RETURNED"];

test("confirmation requires paid acceptance and capture or a settled free-beta record across every state/payment combination", () => {
  for (const state of states) {
    for (const payment_state of payments) {
      for (const trackingState of states) {
        const expected = payment_state === "NO_CHARGE"
          ? freeBetaRecord.includes(state) && freeBetaRecord.includes(trackingState)
          : paidAccepted.includes(state)
            && paidAccepted.includes(trackingState)
            && payment_state === "CAPTURED";
        expect(mailingConfirmed(
          { case_id: "case-a", state, payment_state },
          { case_id: "case-a", state: trackingState },
        ), `${state}/${payment_state}/${trackingState}`).toBe(expected);
      }
    }
  }
});

test("another customer's tracking cannot confirm this case under either funding mode", () => {
  for (const payment_state of ["CAPTURED", "NO_CHARGE"] as const) {
    for (const state of states) {
      for (const trackingState of states) {
        expect(mailingConfirmed(
          { case_id: "case-a", state, payment_state },
          { case_id: "case-b", state: trackingState },
        ), `${payment_state}/${state}/${trackingState}`).toBe(false);
      }
    }
  }
});

test("synthetic cards require explicit fake-mode development", () => {
  for (const mode of ["preview", "fake", "test", "live", "unknown"]) {
    for (const optedIn of [false, true]) {
      for (const development of [false, true]) {
        expect(offlinePaymentAllowed(mode, optedIn, development))
          .toBe(mode === "fake" && optedIn && development);
      }
    }
  }
});
