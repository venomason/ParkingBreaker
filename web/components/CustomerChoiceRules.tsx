import React from "react";

/** One detailed rule shared by Rules and refund terms. No blanket waiver. */
export function CustomerChoiceRules() {
  return (
    <section aria-label="Customer choices and the correct-office guarantee" data-policy-version="2026-11-16">
      <h3 className="how-section-title">Customer choices and our correct-office guarantee</h3>
      <p>If we cannot establish the required submission details, we stop without charging for
        mailing. Where we offer a customer-directed option, you may supply or choose the entry
        point or details yourself, review the exact choice and explicitly confirm it. Required
        information must still be complete, and an option may remain unavailable.</p>
      <p>Customer-chosen details include an address or submission entry point, citation number,
        vehicle details, dates, review stage or another fact you supply or deliberately replace.
        Your choice does not become verified just because you confirm it. An OCR confidence
        score is not proof that a detail is correct.</p>
      <p>If evidence establishes that your explicitly chosen detail caused an undeliverable,
        misrouted or returned appeal, or a clerical rejection before your argument was read,
        our correct-office guarantee and its refund do not cover that customer-caused failure.
        We are not responsible under that guarantee for the consequences of that choice.</p>
      <p>The exclusion applies only to the failure caused by your choice. It does not remove
        coverage for unrelated problems, and an unknown cause is not treated as your fault.
        Simply reviewing a value we supplied does not transfer responsibility for our error
        to you. We remain responsible for sending something other than what you approved,
        omitting required contents, duplicate charges and other failures of our service.
        A destination we verified that proves wrong remains covered by a full refund.</p>
      <p>You can correct your details or stop before approving mailing. We do not guarantee
        that the agency will open, read or agree with an appeal, and we do not assess the
        strength of your argument. Nothing in this rule limits rights or refunds required
        by applicable law.</p>
    </section>
  );
}

/** Keep the customer journey brief; the complete rule stays in Rules and terms. */
export function CustomerChoiceNotice({ onOpenRules }: { onOpenRules?: () => void }) {
  return (
    <p className="small muted" data-customer-choice-notice>
      Your chosen details aren’t covered by our correct-office refund guarantee if they cause the rejection.
      {onOpenRules && <> <button className="btn btn-ghost btn-sm" type="button" onClick={onOpenRules}>Rules</button></>}
    </p>
  );
}
