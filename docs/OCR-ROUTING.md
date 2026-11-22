# Ticket transcription providers and customer control

Owner request: November 16, 2026. This concerns copying ticket text and clerical
submission accuracy, never evaluating the customer's argument or inventing a defense.

## Configuration

`PB_TICKET_OCR_PRESET=deepseek-flash` selects DeepSeek. Change only this setting to
`minimax-m3.1` to select MiniMax-M3.1-Flash using server-side `MINIMAX_API_KEY`.
This is independent of issuer/procedure lookup settings. Both image adapters validate
JPEG/PNG input and bound token spending. MiniMax requires its token-count preflight
to succeed before image inference. Existing saved readings stay replayable; a
configuration change does not silently repeat an already completed paid request.

`PB_TICKET_OCR_ESCALATION_PRESET=gemini-3.8-flash` selects the independent secondary
reader (`gemini-3.5-flash-lite` or `none` are also supported). It needs a usable
server-side `GEMINI_API_KEY`/`GOOGLE_API_KEY`. Google Cloud Vision OCR is a different
API and is not used by this setting. A Gemini primary is not escalated to itself.

For a US rehearsal with wholly synthetic tickets, set
`PB_TICKET_OCR_PRESET=gemini-3.5-flash-lite` to select Gemini as the primary reader.
The adapter validates JPEG/PNG files and sends the original image bytes. It reserves
8,192 input tokens per image, checks the 20 MB inline request limit and applies the
existing per-call ceiling. Google documents approximately 1,120 tokens per default
Gemini 3 image. [Media resolution](https://ai.google.dev/gemini-api/docs/media-resolution)
and [inline image limits](https://ai.google.dev/gemini-api/docs/image-understanding).

Use synthetic material on the unpaid US project. Google's unpaid terms permit human
review and direct developers to exclude personal information. Real customer tickets
require a provider account qualified for that data. Daily capacity depends on the
project and model. [Google's terms](https://ai.google.dev/gemini-api/terms#unpaid-services)
and [rate limits](https://ai.google.dev/gemini-api/docs/rate-limits).

## Decision rule

The latest owner thresholds are interpreted per field:

| Primary estimate | Action |
| --- | --- |
| Missing or below 0.80 | No suggestion; customer supplies the value |
| 0.80 inclusive to below 0.90 | One independent Gemini extraction if configured and budget allows |
| 0.90 or above | Offer an editable suggestion; explicit customer confirmation still required |

These numbers are **uncalibrated model estimates, not measured accuracy or a
guarantee**. The secondary sees images and the same transcription contract, never
the first answer or the customer's argument. A borderline value is offered only
when Gemini independently produces the exact same characters with its own estimate
at least 0.90. We retain that secondary estimate; we do not average scores, assume
independent errors, or manufacture a combined probability. Two 0.85 estimates do
not become 0.97. Disagreement (including O/0), uncertain characters, secondary
failure, or unavailable budget/credentials leaves the field for customer entry.
If escalation reveals a conflict in a previously high-score field, that field also
requires customer entry. A below-0.80 field is not recovered opportunistically.

`PB_TICKET_OCR_MANUAL_BELOW` and `PB_TICKET_OCR_ESCALATE_BELOW` configure the bounds;
both must be finite fractions with 0 <= manual < escalation <= 1.

There are at most two extraction inference calls, no automatic retries. The
primary/secondary limits together fit the extraction allowance. Both use the same
durable per-intake spending ledger. The existing operation claim encloses the whole
sequence; replay/reload does not repeat either call. Fake mode stays entirely offline.

## Customer confirmation and correction

Low-confidence values are not inserted into the customer's adopted facts. Customer
corrections remain distinct from model-derived literal suggestions, and the server
continues to reject forged extraction provenance. Every required fact must be supplied.

The ticket review has a mandatory unchecked accuracy acknowledgment. Editing facts
or resolutions invalidates it. The server rejects confirmation without
`accuracy_acknowledged=true`, independently of model scores. Existing binding checks
invalidate saved confirmation whenever relevant facts or images change.

The exact-packet review requires checking facts, destination and return address.
The customer can stop before authorizing mailing even when automated checks pass.
That stop withdraws the preview/signature and pauses the UI; it cannot undo mailing
already authorized. This change does not provide in-place replacement of a locked
destination. That requires a separate workflow that rebuilds and re-verifies the
packet; the customer must keep mailing paused until the destination is corrected.

The full customer-choice rule is in Rules and refund terms, with a short linked notice
in the review flow. It excludes the correct-office remedy only for a failure caused by
the customer's explicit choice, with evidence of that cause. Mere acknowledgment of a
system-supplied value does not transfer responsibility. See `CUSTOMER-CHOICE-RULES.md`.
ParkingBreaker service errors and statutory rights remain covered.

## Qualification boundary

Offline tests prove routing, payloads, persistence, spending bounds and confirmation
behavior. Actual MiniMax image inference, Gemini escalation, score calibration and
production deployment require separate evidence. This change makes no paid calls,
sends no real mail, and does not enable live payment mode.
