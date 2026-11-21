# ParkingBreaker Support Comms — Thin Google Intake + Backend Authority

Authoritative customer support system built directly on ParkingBreaker's SQLite transaction store, redacted support projection, existing model gateway/spend cap, deterministic pre-send verification guard, and single `EmailClient` — with Google Apps Script reduced to a **thin, zero-state intake layer**.

- **No Google Sheets copy of case data:** All case state, payment state, mail tracking, remedies, and closed-enum audit records live strictly in the backend SQLite store (`pb_support_codes`, `pb_support_messages`, `pb_support_confirmation_emails`).
- **Hard support-code rule (`NO_ID_NO_RESPONSE`):** No clear valid `PB-XXXXX` code matching `pb_support_codes` → logged as `NO_ID_NO_RESPONSE` (closed enums + SHA-256 hash only) and stopped immediately. Zero reply, zero alert, zero digest entry, zero phone/email fallback lookup.
- **No SMS, no live phone agent, no press-0, no business-hours logic.**
- **Zero ticket details in email:** Replies carry status-level wording in the app's exact vocabulary plus fresh 15-minute one-use sign-in links; citation numbers, plates, locations, agency names, and mailing addresses are never sent to the model and are blocked by the deterministic post-check.

---

## Architecture Split

1. **Thin Google Intake (`support-system/apps-script/`):**
   - `appsscript.json` — minimal OAuth scopes (`gmail.modify`, `script.external_request`, `script.scriptapp`; **no** `spreadsheets` or `gmail.send` scopes).
   - `Config.gs`, `Safeguards.gs`, `Intake.gs`, `Main.gs` — polls Gmail (including Google Voice voicemail transcript emails from `voice-noreply@google.com`), ignores SMS/bounces/OOF/self-sent/mailing lists, scrubs Luhn-valid card numbers/CVVs/tokens, extracts `PB-XXXXX`, and POSTs `{ message_id, channel, text, support_code, received_at }` to the configured HTTPS intake endpoint with `PB_SUPPORT_INTAKE_SECRET`. HTTP redirects are disabled.
2. **Backend Support Modules (`backend/src/parkingbreaker/`):**
   - `support_store.py` — `pb_support_codes` (atomic unique `PB-XXXXX` allocated in `_start_case_in_transaction`), `pb_support_messages` (append-only closed-enum + SHA-256 hash log, zero free text), and `pb_support_confirmation_emails` (idempotent post-purchase confirmation record). All 3 tables are registered in `retention_inventory.py` under `TableClass.RESTRICTED_IDENTITY_OPERATIONAL`.
   - `support_projection.py` — builds the redacted status-level `SupportProjection` directly from `pb_cases`, `pb_case_history`, `pb_submission_cursors`, `pb_return_remedies`, and `pb_pending_effects`, while collecting private ticket terms separately for post-send leak verification.
   - `support_comms.py` — `SupportCommsService` (`handle_intake`, `send_purchase_confirmation`), fixed no-advice & neutral held-ack replies, fresh 15-minute sign-in link minter (`store.issue_identity_link`), existing `CallLoggingLlmClient` + `AiSpendStoreMixin` (`AiWorker.SUPPORT_DRAFT`, 25¢ per-case cap), deterministic pre-send verification (`verify_support_draft`), and outbound dispatch via `EmailClient` with idempotency keys derived from `inbound_message_id`.
   - `api/app.py`, `api/schemas.py`, `case_service.py` — can expose `support_code` (`PB-XXXXX`) on `Authorization`, `BetaAcceptanceReceipt`, and `CaseStatus`. Customer responses omit it until the release assembly enables the qualified channel. A worker or all-role app can mount `POST /internal/support/intake` when given a support service and a dedicated intake secret. The deployment factory still needs explicit service wiring and qualification.
3. **Checkout Confirmation UI (`web/`):**
   - `SupportCaseIdCard.tsx`, `Step5Window.tsx`, `LiveFulfillment.tsx`, `support-case-id.ts` — renders the backend-issued `PB-XXXXX` code in large monospace type with a Copy button and up-front hard-rule notice (zero lookup or resend feature).

---

## Voicemail Greeting Script (< 20 Seconds)

Use this greeting after the intake and email delivery pass rehearsal. Set response
expectations from the coverage the team can provide.

> "ParkingBreaker Support. Say your case ID slowly, starting with P B, then tell us what happened. We need your ID to reply. If your deadline is close, follow the instructions on your ticket or latest notice."

---

## Open Items Resolved

1. **Model gateway & spend cap reuse:** Confirmed reusable as-is. `AiWorker.SUPPORT_DRAFT` already existed in `oplog/ai_journal.py`; adding `AiWorker.SUPPORT_DRAFT: 25` to `WORKER_CAPS_CENTS` in `ai_spend_store.py` reuses `CallLoggingLlmClient`, `pb_ai_spend`, and `pb_ai_calls` directly with no gateway rewrite.
2. **Voicemail transcript retention:** Because `pb_support_messages` persists ONLY closed enums and `message_sha256` (zero free text), raw voicemail transcripts never enter SQLite. All 3 support tables are classified in `retention_inventory.py` as `RESTRICTED_IDENTITY_OPERATIONAL`. Raw transcripts remain transiently in Gmail / Google Voice only — configure a Workspace retention/auto-purge rule (recommended: **30 days** for `PB-Support/No-ID-No-Response` threads and **1 calendar year after closure** matching `retention_policy.PROPOSED_RETENTION_YEARS = 1` for matched correspondence, subject to explicit owner policy adoption).

---

## Setup (Thin Google Intake Script Properties)

Paste the 5 files in `support-system/apps-script/` into Apps Script signed in as `support@parkingbreaker.com`, set Script Properties:

- `PB_BACKEND_INTAKE_URL` points to the approved support intake endpoint over HTTPS.
- `PB_SUPPORT_INTAKE_SECRET` matches the app's dedicated `support_intake_secret`.
- `SUPPORT_EMAIL`: `support@parkingbreaker.com`

After the dedicated endpoint and mail delivery pass rehearsal, run
`installIntakeTrigger()` once to poll every 5 minutes.

If delivery fails or the backend returns an unrecognized disposition, the script
labels the message for retry and leaves it unread. The poll continues with the
next message. The next poll can retry delivery using the same message ID.

## Automated Tests

```bash
# Backend support comms (all 14 situation rows + negative guards + HTTP endpoint)
cd backend && PB_PROVIDER_MODE=preview uv run pytest tests/test_support_comms.py tests/test_transaction_store.py tests/test_t46_safety_gaps.py tests/test_retention_inventory.py

# Thin Google Apps Script intake (zero Sheets / zero outbound email verified)
node --test support-system/tests/thin_intake.test.js

# Web checkout confirmation UI
cd web && npm run lint && npm run type-check && npx playwright test tests/support-case-id.spec.ts tests/live-presentation.spec.ts tests/launch1.spec.ts
```
