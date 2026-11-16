# ParkingBreaker handoff for the Figma connected session

Updated November 16, 2026, America/Los_Angeles. Amir requested an immediate, fully updated handoff and stopped implementation. **The latest customer-choice rules edits are saved but NOT verified.** The earlier OCR routing milestone passed its local checks. Nothing in this session was deployed or sent to Figma.

## Start here

- Active checkout: `/home/ghostmonday/Desktop/ParkingBreaker-Codebase`.
- HEAD, verified at handoff: `332b8384859e145379c3b3f29a2d6aad8c7a6f8a`.
- The working tree is extensively dirty. Preserve all existing changes and untracked assets. Do not reset, clean, or replace it from HEAD.
- `/home/ghostmonday/Desktop/ParkingBreaker` is a protected backup. Do not edit it. Older references to `parkingbreaker-fresh` are historical, not this session's checkout.
- No commits or pushes were made. Any later commits must remain GPG-signed and contain no AI attribution.
- `~/Vault/Ops/STOP` was absent at handoff; recheck before resuming.
- **Do implementation directly. Amir explicitly revoked the DeepSeek coding harness: “CEASE USING THE DEEPSEEK HARNESS DO IT ALL URSELF.”** Do not restart it or silently delegate. This is separate from DeepSeek as the application's OCR provider.

## Latest owner requirements

ParkingBreaker is a clerical service. It captures the customer's facts and own reasoning, improves grammar/presentation, and handles submission requirements. It does not assess argument strength, invent defenses, recommend legal strategy, or predict the agency's decision.

OCR/provider request:

1. Make switching the application OCR provider to MiniMax easy, without silently changing the current provider.
2. Interpret the owner's revised thresholds as **below 0.80: manual entry; 0.80 inclusive to below 0.90: one Gemini escalation when configured and within budget; 0.90 or higher: editable suggestions, still requiring customer confirmation**.
3. Treat confidence per field. Model confidence is an uncalibrated estimate, not measured character accuracy.
4. Customers must be able to correct facts and stop mailing even if the models believe the information is correct.

Latest guarantee/rules request:

> If a customer's custom address or other chosen data causes an appeal to be rejected before it is read, ParkingBreaker does not accept responsibility under the correct-office refund guarantee for that customer-caused failure. If required details cannot be established, decline the sale or offer an explicitly customer-directed alternative where supported.

Amir then clarified: **“explicit in the rules, but a small mention onlyy on the pipeline.”** Full policy detail belongs in Rules and refund terms, not a large warning in the customer journey. Keep the brief notice linked to the complete rules. Do not use a custom choice as an excuse to deny remedies for unrelated ParkingBreaker failures or unknown causes.

## Completed and verified OCR milestone

Primary receipt:
`/home/ghostmonday/Vault/Ops/runs/parkingbreaker-ocr-routing-20261116/implementation-receipt.json`

Verified before the later rules edits:

- 416 focused backend tests passed, no failures or skips.
- 49 actual mobile-viewport browser tests passed, no failures, skips or flaky results.
- Python lint and targeted mypy passed; frontend TypeScript and targeted ESLint passed.
- Production frontend built successfully for the isolated synthetic rehearsal.
- `git diff --check` passed.

Evidence:

- Backend XML/log: `.../parkingbreaker-ocr-routing-20261116/focused-tests.xml` and `focused-tests.log`.
- Successful browser run: `build-kit/evidence/ocr-routing-20261116-mobile-3/rehearsal.json`.
- The preceding `mobile` and `mobile-2` attempts were incomplete/failed; do not cite them as successful. They exposed stale manual-entry fixtures and two test synchronization/assertion issues that were corrected.
- `before/`, `baseline.json`, and `changed-file-manifest.json` in the Ops run preserve the starting state and distinguish earlier work.

These results prove local behavior with simulated providers, not live OCR quality, actual device qualification, payment/mail readiness, or launch readiness. **They do not verify the newer customer-choice copy and callback change below.**

### OCR implementation

- New `backend/src/parkingbreaker/ticket_ocr.py`: bounded primary/secondary routing; at most one independent Gemini extraction. The second reader sees the images, not the first reader's answer or the customer's argument.
- `ticket_vision.py`: optional finite per-field confidence, confidence schema/prompt, model/escalation metadata; legacy saved results remain readable.
- Borderline fields are suggested only if Gemini returns the exact same characters with its own estimate at least 0.90. Scores are not averaged or multiplied. Disagreements, low or missing scores, failed escalation and insufficient budget require customer input. A conflict discovered in a previously high-scoring field also requires manual input.
- `llm_anthropic.py`: MiniMax image blocks through its Anthropic-compatible API. Both counting and inference receive images; token-count preflight must succeed before image inference. SDK automatic retries remain disabled.
- `config.py`: independent OCR selection and optional Gemini secondary; both use the existing durable intake spending ledger. Fake mode constructs no real OCR providers.
- `ticket_intake_service.py` and API schemas: persist confidence/routing metadata; require `accuracy_acknowledged=true` for confirmation; keep exact photo/fact/provenance bindings and replay protections.
- `TicketFactReview.tsx`: unchecked accuracy acknowledgment; edits clear it, including editing then restoring the original value; confirm remains blocked until the current review is ready and the customer checks it.
- `LiveLetter.tsx`: final exact-PDF review checks facts, destination and return address. “Stop — information or address needs correcting” discards the local preview/signature and pauses the UI. Pause survives a same-browser session reload; it is **not** a server-wide cancellation of previously authorized mail.
- Generated API schema/types and existing tests were updated. New focused tests are in `backend/tests/test_ticket_ocr.py`.

Configuration in `.env.example`:

```dotenv
PB_TICKET_OCR_PRESET=deepseek-flash
PB_TICKET_OCR_ESCALATION_PRESET=gemini-3.8-flash
PB_TICKET_OCR_MANUAL_BELOW=0.80
PB_TICKET_OCR_ESCALATE_BELOW=0.90
```

For MiniMax, select `PB_TICKET_OCR_PRESET=minimax-m3.1` with a usable server-side `MINIMAX_API_KEY`. This is an available switch, not evidence that a production runtime has been switched. Gemini uses `GEMINI_API_KEY` or `GOOGLE_API_KEY`; absent credentials leave borderline fields manual. `none` disables escalation. Gemini OCR here means Gemini image understanding, not the separate Google Cloud Vision API. See `docs/OCR-ROUTING.md`.

## Most recent edits awaiting verification

Ops run:
`/home/ghostmonday/Vault/Ops/runs/parkingbreaker-customer-choice-rules-20261116/`

Its `before/` directory backs up the five previously existing files before these edits. `plan.json` records scope and authority.

Changes actually saved:

- **New `web/components/CustomerChoiceRules.tsx`** exports the detailed rules and a brief notice. The rules cover custom addresses, entry points, citation numbers, vehicle details, dates, stage and other deliberately supplied/replaced facts; require evidence of causation; preserve responsibility for service failures and applicable-law rights; distinguish customer choices from merely reviewing system-supplied values.
- Brief notice currently reads: **“Your chosen details aren’t covered by our correct-office refund guarantee if they cause the rejection.”** Adjacent Rules button opens refund terms in the same page.
- `web/app/page.tsx` renders the full rules in both the Rules sheet and Hold & refund terms. It corrects the terms to state that a system-verified destination that proves wrong receives a full refund, consistent with the existing backend policy.
- `TicketFactReview.tsx` and `LiveLetter.tsx` use the short notice instead of the earlier longer refund paragraphs. `Step1Ticket.tsx` forwards the Rules callback.
- A concrete wiring bug was fixed in `page.tsx`: `onSaveFacts` now forwards `customerSupplied` to `saveAndReviewVision`. Previously that boolean was discarded, so the explicit “Use my supplied facts without an AI comparison” action could incorrectly ask for comparison. **Verify this behavior after resuming.**
- New `docs/CUSTOMER-CHOICE-RULES.md`; updated `docs/OCR-ROUTING.md` links to it and records the narrow causal exclusion.

Validation status at handoff:

- Files and callback/component references were read back and verified present.
- `git diff --check` passed at handoff.
- A TypeScript command was launched from the repository root instead of `web/` just before interruption. Its result could not be recovered (`Unknown process id 31099`); **do not count it as a pass**. No `tsc` process appeared in the handoff process snapshot.
- No fresh type check, lint, build or browser verification has completed for these latest edits.
- No backend refund-classification logic was broadened. Existing automated classification is address-specific. Non-address incidents require evidence review under the new rules; there is no new automatic blanket refund-denial mechanism.

## Existing destination and refund behavior

The current live service implementation **refuses unverified destinations**. New customer-supplied address/consent endpoints are closed, and `DestinationChoice.tsx` presents a verification pause, not an active override form. The owner allowed refusal OR a properly disclosed custom route; this change preserves refusal and does not reopen the retired route.

Relevant files:

- `backend/src/parkingbreaker/address_policy.py`
- `backend/src/parkingbreaker/address_disclosure.py`
- `backend/src/parkingbreaker/case_service.py`
- `backend/tests/test_address_policy.py`
- `backend/tests/test_t30_customer_directed.py`
- `backend/tests/test_t30_refund_path.py`
- `web/components/DestinationChoice.tsx`

Existing historic customer-directed address exclusions require matching saved consent, proof the exact approved address was mailed, and evidence that address was the sole cause. Unknown cause does not become customer fault. Service failures, omitted required contents, duplicate charges, or mailing different bytes retain service responsibility. A system-verified destination proven wrong retains an unconditional full-refund remedy. Do not rewrite old disclosure text/consent records to pretend customers accepted the new wording.

**Remaining product limitation:** customers can stop at final review but cannot replace an already locked destination in place. Implementing that requires a controlled revision/rebuild, invalidation of old preview/signature/approval, verification of the replacement route, and renewed approval. Do not patch a frozen mailing payload or send after an uncertain earlier approval.

## Phone and running services at handoff

Fresh observations:

- `adb devices -l` returned **no attached devices**. Forward list was empty; reverse inspection reported no device/emulator. Do not claim the smartphone is currently connected.
- Former persistent phone bridge PID `1400767` was **not running** in the handoff snapshot.
- Old fake API PID `1392915` was still running on loopback port `41957`.
- Old Next process PID `1392916` was still running; its saved runtime uses port `57787`.

Runtime record:
`/home/ghostmonday/Vault/Ops/runs/parkingbreaker-functional-photo-20261116T194735Z/runtime.json`

Runtime directory `/tmp/pb-functional-photo-n4uhs_ng`, web copy `/tmp/pb-functional-photo-n4uhs_ng/web`, reserved origin `https://portal.example.test/`, mode `fake`, phase `controlled_free_beta`, ticket-reading preview enabled. This is an older build, not the just-edited source. Presence of its processes does not establish current HTTP health.

Earlier phone connection: A56 Android device; CDP forward `tcp:9333 -> localabstract:chrome_devtools_remote`; design preview used `http://127.0.0.1:3001/design-preview/intake`; functional flow used the reserved HTTPS origin through a page-scoped transport bridge. Its former bridge script is `/home/ghostmonday/Vault/Ops/runs/parkingbreaker-upload-connection-20261116/restore-existing-bridge.cjs`. Reinspect the actual device/page before reuse.

Earlier actual selected PNG upload succeeded and was saved; receipt: `.../parkingbreaker-upload-connection-20261116/upload-result.json`. The subsequent “synthetic readings; no provider is called” message was real evidence of fake mode, not evidence that DeepSeek failed to read the image. Do not tell Amir the phone is on real OCR until a rebuilt real-provider path is actually observed.

## Figma and visual continuation

Amir is moving this handoff to a better Figma-connected session. **No Figma file key, node, selected frame or destination was supplied here; no Figma read/write/import/sync was performed.** The Companion Page event contained `page_id: null` and `document_view: null`, which provides no design destination.

Existing untracked visual work must be preserved:

- `web/app/design-preview/` including intake, rules and logo-lab routes.
- `web/app/motion-preview/` and its rules route.
- `web/components/brand/`: `HurtleExpression`, `HurtleShell`, `BrandFrame`, `BrandLanding`, `CityLightShell`, `CityLightLab`, `IntakeMotionPreview`, `MotionJourney`, styles and network artwork support.
- `web/public/images/brand/`: `hurtle-city-light-v2.svg`, `hurtle-city-light-v2-static.svg`, `parking-breaker-city-light-v2-lockup.svg`, `hurtle-natural-shell-concept.png`.
- Amir also posted two turtle character images in the conversation. Do not invent local paths for those exact attachments or assume the existing assets are identical.
- `isituseful.md` is untracked; this handoff does not certify that its contents were incorporated.

Read the relevant Figma skills in the new session before using its tools. Discover/reuse the actual connected Figma file and clarify only if the destination remains missing. Keep the clerical product boundary, concise pipeline notice, editable facts, exact-packet confirmation, real payment state and provider-mode honesty through any design work. Do not migrate this app to Sites or replace the working checkout with a new mockup.

## Resume in this order

1. Preserve the dirty working tree and read the latest owner instruction. This handoff is a stopped checkpoint, not permission to ignore a later direction.
2. Finish checks for the recent rules edits from the correct directory:

   ```bash
   cd /home/ghostmonday/Desktop/ParkingBreaker-Codebase/web
   npx --no-install tsc --noEmit
   npx --no-install eslint app/page.tsx components/CustomerChoiceRules.tsx components/TicketFactReview.tsx components/Step1Ticket.tsx components/LiveLetter.tsx --max-warnings=0
   ```

   Check the short notice opens the full rules without submitting or losing entries, including mobile layout. Verify the forwarded customer-supplied choice does not call a model unnecessarily. Prefer focused checks over repeating unrelated suites.
3. Continue the requested Figma work against the real destination once accessible; avoid redesign churn or destructive imports.
4. Qualify MiniMax/Gemini image behavior with a concrete, separately authorized bounded trial before claiming they work live. Prior DeepSeek five-call evidence is under `.../parkingbreaker-carbon-followup-20261116T142808Z/approved-live/assessment.json`; its earlier approval envelope was completed, not an evergreen spending allowance. Never use the coding harness to test app OCR.
5. Reconnect/rebuild the phone flow only after verifying the actual connected device, runtime and provider mode. Real Stripe/Lob and launch readiness have not been newly established by this work.

No paid provider calls, real payments, refunds, mail, deployment, account/security changes, commits or Figma writes were made during these two implementation tasks. Read credentials only through approved runtime mechanisms; never include their values in prompts, chat, logs or this handoff.
