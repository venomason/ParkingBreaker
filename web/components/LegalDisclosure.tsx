"use client";

import React from "react";

/**
 * The clerical-scope disclosure shown wherever a customer commits money or mail.
 *
 * Candidate wording reviewed for accuracy and placement (audit A087). It states what the
 * service does and does not do; it is not a claim of legal exemption in any jurisdiction.
 */
export const ATTORNEY_DISCLOSURE =
  "ParkingBreaker is an automated procedural research, document-formatting and USPS print-and-mail tool. It is not a law firm, does not represent you, does not provide legal advice, and is not a substitute for the advice of an attorney. You are responsible for the facts, reasons and declarations in your submission.";

export function LegalDisclosure({ id }: { id: string }) {
  return (
    <p className="small muted" id={id}>
      {ATTORNEY_DISCLOSURE}
    </p>
  );
}
