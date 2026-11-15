"""The customer-directed destination disclosure (T30). Pure: no clocks, files or network.

OWNER APPROVED 2026-10-21 as DISCLOSURE_VERSION "v1" (replaces engineering draft "draft-1",
preserved in build-kit/evidence/task-T30-2026-10-20/). The owner chose to launch without a
lawyer review; the text is written for plain clarity: it names the payment-address mistake,
limits the refund exclusion to delivery failures caused solely by the supplied address, and
never speaks about appeal outcomes. Changing the text changes the hash, so every earlier
consent stops matching and the customer must accept the new wording.

Consent binds the sha256 of the exact UTF-8 text below (DISCLOSURE_SHA256). The text is
shown before affirmative consent and is never pre-accepted.
"""

from __future__ import annotations

import hashlib

DISCLOSURE_VERSION = "v1"

DISCLOSURE_TEXT = (
    "We could not confirm the official appeal mailing address from the agency's official "
    "sources.\n"
    "\n"
    "You can enter the mailing address yourself, and we will mail your approved appeal to "
    "that exact address. Make sure you enter the agency's contest or appeal address, not the "
    "payment address.\n"
    "\n"
    "Because we could not verify this address, we cannot confirm that it is the agency's "
    "current appeal office, that USPS can deliver to it, or that the agency will accept mail "
    "there.\n"
    "\n"
    "How refunds work for an address you supply: if your letter is undeliverable, returned, "
    "or not accepted solely because the address you entered is wrong, our normal refund for "
    "an address error does not apply. Every other mistake of ours remains our responsibility, "
    "including mailing to any address other than the exact one you approved, leaving out "
    "required contents, charging you twice, or any failure of our service. Nothing here "
    "limits your rights under applicable law.\n"
    "\n"
    "Prefer not to take that risk? You can decline right now. Your card will not be charged, "
    "the hold will be released, and your work will stay saved."
)

DISCLOSURE_SHA256 = hashlib.sha256(DISCLOSURE_TEXT.encode("utf-8")).hexdigest()
