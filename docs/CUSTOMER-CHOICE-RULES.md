# Customer choices and the correct-office guarantee

Owner direction: November 16, 2026. Full detail belongs in Rules and refund terms;
the customer journey gets one short notice linking to those rules. This concerns
clerical accuracy and entry into the agency's process, never argument quality.

## Admission and choice

- If the necessary submission details cannot be established, stop without charging
  for mailing. A model's confidence score or a customer acknowledgment is not
  independent evidence of an official destination or procedure.
- A customer-directed alternative may be used only where the application explicitly
  offers it, with complete required information and explicit confirmation of the
  exact choice. It must not bypass mandatory fulfillment checks.
- The current application stops unverified destinations and has retired new
  unverified-address consent. This wording does not re-enable that route. Existing
  address disclosure/consent records retain their original text and meaning.

## Narrow guarantee exclusion

An address, submission entry point or other customer-entered or deliberately replaced
fact is a customer choice. Examples include citation number, plate, dates and stage.
If evidence establishes that this exact choice caused misrouting, non-delivery,
return or clerical rejection before the argument was read, the correct-office
guarantee and its refund do not cover that customer-caused failure.

The choice alone is insufficient to deny a remedy. Record the exact approved value,
what was actually mailed, and the agency/carrier evidence linking the failure to
that value. An unknown cause stays unknown. A model comparison or merely checking
the accuracy acknowledgment does not establish customer fault. Do not apply the
exclusion to a merits decision or infer a pre-reading rejection from missing tracking.

Responsibility for ParkingBreaker service errors remains: mailing different content
or an address from what was approved, omitted required contents, duplicate charges,
and other service failures. A system-verified destination that proves wrong retains
the unconditional full-refund rule. A customer's review of a system-supplied value
does not alone transfer responsibility. Applicable-law rights remain preserved.

Existing automated returned-mail classification is address-specific and requires
saved customer-directed consent, exact-address mailing and evidence of sole address
cause. This change does not create a broader automatic refund-denial mechanism for
other data. Those incidents require evidence review under the rule above.

## Presentation and confirmation

`web/components/CustomerChoiceRules.tsx` supplies the detailed customer rules in both
Rules and refund terms. Ticket-fact and final-packet review show only:

> Your chosen details aren't covered by our correct-office refund guarantee if they cause the rejection.

The adjacent Rules button opens refund terms in the current page. It does not submit
or clear entries. Existing explicit fact confirmation and exact-packet approval remain
required. Customers can correct facts or stop before approval.

The ticket screen's explicit customer-supplied option forwards that choice to the
server; it must not silently request an AI comparison instead.

No live sale, provider call, refund, mailing or deployment is authorized by this file.
