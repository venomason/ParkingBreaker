// Legal page copy. OWNER REVIEW REQUIRED before these pages are deployed.
// Each constant is intentionally in one file so the owner/lawyer can edit copy
// without touching layout code. Entity name, contact and effective date are the
// three values still awaiting the owner's decision (LEGAL-PAGES-DRAFT.md, L2-L5).
//
// The hold/refund rules duplicate the in-app terms sheet in app/page.tsx
// (`#termsDlg`); keep both in sync. The customer-choice rule itself is rendered
// from components/CustomerChoiceRules.tsx so its policy version stays single-source.

export const LEGAL_ENTITY = "ParkingBreaker";
export const LEGAL_CONTACT_EMAIL = "support@parkingbreaker.com";
export const LEGAL_EFFECTIVE_DATE = "2026-11-23";

export const HOLD_REFUND_RULES: readonly string[] = [
  "If we stop before mailing, the hold is released and you are not charged.",
  "A duplicate charge is refunded.",
  "If a destination we verified proves wrong, you receive a full refund. For other mailing errors of ours: a free re-mail where feasible, otherwise a full refund. Agency-side returns do not receive a refund. When the cause is unknown, a re-mail or refund minus mailing cost may apply.",
  "After mailing, a change of mind or the agency's decision is not a refund reason.",
];

export type LegalSection = { title: string; paragraphs: readonly string[] };

export const TERMS_SECTIONS: readonly LegalSection[] = [
  {
    title: "What we do",
    paragraphs: [
      "ParkingBreaker prepares an appeal packet from the ticket information and photos you provide, and mails it to the official office and address we can verify for your ticket.",
      "We are not a law firm, do not represent you, and do not provide legal advice. We do not evaluate the merits of your case and we do not promise any outcome. You are responsible for the facts, reasons and declarations in your submission.",
    ],
  },
  {
    title: "What you pay for",
    paragraphs: [
      "A one-time fee for preparing and mailing one certified appeal packet. The price is shown before you authorize payment. There is no subscription and no recurring charge.",
    ],
  },
  {
    title: "Card holds and charges",
    paragraphs: [
      "We place an authorization hold when you approve. If we stop before mailing, the hold is released and you are not charged. We charge only when we mail your packet.",
      "A duplicate charge is refunded.",
    ],
  },
  {
    title: "Mailing and refunds",
    paragraphs: [
      "We mail to the destination we verified for your ticket. If a destination we verified proves wrong, you receive a full refund. For other mailing errors of ours: a free re-mail where feasible, otherwise a full refund. Agency-side returns do not receive a refund. When the cause is unknown, a re-mail or refund minus mailing cost may apply.",
      "After mailing, a change of mind or the agency's decision is not a refund reason.",
    ],
  },
  {
    title: "Customer-chosen details",
    paragraphs: [
      "Where we offer a customer-directed option, you may supply or choose an entry point or detail yourself and confirm it explicitly. Your choice does not become verified because you confirm it.",
      "The correct-office guarantee does not cover a failure caused by your choice. It does cover a destination we verified that proves wrong and other failures of our service. Nothing here limits rights or refunds required by applicable law.",
    ],
  },
  {
    title: "Acceptable use",
    paragraphs: [
      "Provide accurate information and only material you may share. Do not use the service for unlawful purposes.",
    ],
  },
];

export const PRIVACY_SECTIONS: readonly LegalSection[] = [
  {
    title: "What we collect",
    paragraphs: [
      "Ticket photos you upload; the citation number, plate, dates, location, issuer and similar facts read from them or entered by you; the contact details you give us to prepare and mail the appeal; correspondence about your appeal; and payment status from our payment processor.",
      "We do not receive or store your full card number. Card data goes directly to our payment processor.",
    ],
  },
  {
    title: "How we use it",
    paragraphs: [
      "To prepare and mail your appeal, to show you the exact packet you approve, to take payment, to support you, and to meet legal and accounting obligations.",
      "We do not sell your data and we do not use it for advertising.",
    ],
  },
  {
    title: "Who processes it",
    paragraphs: [
      "Payment: Stripe. Certified mail: Lob. Email delivery: Resend. Document reading: AI model providers engaged for extraction only. Each processes data on our instructions and only as needed for the service.",
    ],
  },
  {
    title: "Retention",
    paragraphs: [
      "Business and financial records: up to 7 years. Ticket photos and extracted facts: 2 years. Diagnostic logs: 90 days. Session cookies: 30 days. Access links: 24 hours.",
      "Exact periods are fixed in our retention policy and may be adjusted where a legal obligation or preservation hold applies.",
    ],
  },
  {
    title: "Your choices",
    paragraphs: [
      "You may ask for access, correction or deletion, subject to records we must keep. Deleting a case may stop an appeal in progress.",
    ],
  },
  {
    title: "Cookies",
    paragraphs: [
      "We use strictly necessary cookies only: a session cookie and a CSRF cookie. We do not use advertising or analytics cookies.",
    ],
  },
  {
    title: "Children",
    paragraphs: ["We do not knowingly collect data from children under 18."],
  },
  {
    title: "Contact",
    paragraphs: [
      "Questions about this policy or your data: see the contact address below.",
    ],
  },
];
