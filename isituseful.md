
Conversation with Gemini
The repository archive in `` was ingested as a raw binary zip stream, leaving the Python and TypeScript source files (ticket_vision.py, `ticket_fact_binding.py`, `ticket_intake_service.py`, `Step1Ticket.tsx`, and `docs/verification/deepseek-corrected-live-2026-11-16.json`) compressed and unreadable in this context. However, the architecture invariants, test contracts, and live trial receipts documented in `GEMINI-CARBON-REVIEW.md` and `HANDOFF.md` provide enough structural evidence to evaluate the 8 review questions, separate true safety risks from harness artifacts, and define the minimal path forward.   
venomason/parkingbreaker
+ 3

To complete the line-by-line code verification of the customer correction path (Question 6) and exact parser regexes (Question 3), please paste the plain-text contents of ticket_vision.py, ticket_fact_binding.py, ticket_intake_service.py, and Step1Ticket.tsx. Below is the architectural review based on the handoff evidence.   
GEMINI-CARBON-REVIEW.md

1. Reconstructed Production Pipeline & Safety vs. Availability Matrix
The production intake flow enforces strict provenance separation between model-extracted facts and customer-supplied corrections before any case can advance to research, payment hold, or mail dispatch:   
GEMINI-CARBON-REVIEW.md
+ 1

Image & Initial Reading (TicketVisionReader): Accepts validated JPEG/PNG bytes (detail: original, explicit token/size caps, single transport attempt, no auto-retry) and requests 11 structured fields. Time, street location, violation code, fine, and currency are excluded from the vision reader contract.   
GEMINI-CARBON-REVIEW.md
+ 1

Fact Binding (TicketFactBinding): Tracks provenance per field (literal from initial OCR vs. adopted from customer input/correction). Crucially, binding.review_fields() returns only non-null literal fields—it never exposes null fields or adopted-only values to the secondary comparison pass.   
GEMINI-CARBON-REVIEW.md
+ 1

Coordinator Review (TicketIntakeCoordinator.request_review()): Invokes binding.review_fields() to build the comparison prompt. Unreadable (null) initial fields are omitted from the comparison call entirely.   
GEMINI-CARBON-REVIEW.md
+ 1

Customer Confirmation & Intake Stop Gate: Essential facts that remain missing or unconfirmed block case progression, payment authorization, and mail dispatch. No duplicate uncertainty layer is needed because TicketFactBinding already isolates null literals and tracks customer-adopted provenance.   
GEMINI-CARBON-REVIEW.md
+ 1

Trial Case	Observed Outcome	Classification	Impact on Customer Safety
A, B, E	Passed extraction and comparison (after B prompt fix)	Qualified	None (Safe & Available)
C	Whole-response parser refusal on damaged image; direct comparison probe marked plate unreadable	Formatting / Availability (Extraction) + Harness Artifact (Probe)	Safe (Fails closed; no bad fact adopted; raw reply logging gap on refusal)
D	Refused due to literal newline in multiline issuer name; probe showed DEM0926 vs DEMO926	Parser Contract Friction + Oracle Ambiguity	Safe (Stops on missing citation/date regardless; newline refusal is overly brittle)
F	Citation extracted as null; direct stress probe returned partial TH-48271 as conflict	Harness Artifact	
Safe in Production (review_fields() omits null citation; probe never runs in prod) 
GEMINI-CARBON-REVIEW.md
+ 1

2. Independent Judgment on the 8 Review Questions
Q1: Provenance & Uncertainty Layer

Verdict: Do not add a new uncertainty or confidence-scoring layer. TicketFactBinding.review_fields() already guarantees that null literal extractions are excluded from secondary review and that customer edits are recorded with distinct provenance.   
GEMINI-CARBON-REVIEW.md
+ 1

Q2: Whole-Response Refusal on Deliberately Unreadable Inputs (Cases C & D)

Verdict: Yes—when an input image is severely degraded or missing essential facts, a safe whole-response refusal or null field extraction that halts automated progression is a valid negative-case operating outcome.   
GEMINI-CARBON-REVIEW.md

Reporting Standard: Keep the original strict component field-recovery score unchanged in historical logs, but define Operational Safety Acceptance separately: (a) no fabricated or partial identifier is silently adopted as a literal fact, and (b) the coordinator halts or routes to customer correction. Also ensure TicketVisionReader logs sanitized raw model text when the parser rejects a reply so Case C's exact refusal reason is never lost again.   
GEMINI-CARBON-REVIEW.md
+ 1

Q3: Printed Line Wraps vs. Strict Single-Line Parser Contract (Case D)

Verdict: Requiring the LLM to convert physical multi-line ticket headers (e.g., "CITY OF ...\nPARKING DIVISION") into spaces without parser assistance causes avoidable availability failures on valid tickets.

Trusted Whitespace Canonicalization: Apply deterministic pre-validation normalization strictly to prose/header text fields: issuer_name, issuance_city, issuance_state, procedural_stage, and notes/descriptions.

Permitted transform: Replacesequences of [\r\n\t ]+ with a single ASCII space " " and .strip() leading/trailing whitespace before checking single-line constraints.

Strictly Forbidden (Counterexamples): Never collapse internal whitespace or strip characters inside citation_number, license_plate, plate_state, issue_date, violation_date, or vin (e.g., "AB\n123" or "TH- 482" in an identifier must not be silently repaired to "AB123" or "TH-482"; internal newlines in identifiers must still trigger refusal/null).   
GEMINI-CARBON-REVIEW.md

Q4: Case F Partial Citation in Extra Probe

Verdict: No new deterministic runtime guard is needed in TicketFactBinding. Production review_fields() already omits null literals, which replay proved prevents the F comparison probe from ever firing in a real customer flow. Keeping the new prompt rule ("forbid joining fragments or dropping damaged characters") plus a coordinator-level integration test asserting that null initial extractions are excluded from request_review() is sufficient.   
GEMINI-CARBON-REVIEW.md
+ 1

Q5: Case D License Plate (DEM0926 vs. DEMO926)

Verdict: Do not hardcode an O/0 equivalence rule or alter the model prompt to guess between O and 0. In standard North American license plate fonts, 0 and O are经常 visually indistinguishable without jurisdiction-specific syntax rules.   
GEMINI-CARBON-REVIEW.md

Action: Record an explicit owner adjudication / dual-oracle allowance in the benchmark grading key for Case D's plate (["DEMO926", "DEM0926"] or an ambiguous_glyph annotation in the test harness only). Because Case D lacks essential citation and date facts, it must stop before charge/dispatch regardless.   
GEMINI-CARBON-REVIEW.md

Q6: Customer Correction Path After Whole-Response Refusal

Verdict (Requires Plain-Text Verification): Under the product rule "Customers supply a readable ticket or correct missing facts themselves", we must verify in ticket_intake_service.py and Step1Ticket.tsx whether a total parser refusal (TicketVisionError / HTTP 422) blocks the UI on the upload screen or allows initializing an empty/manual TicketFactBinding where the customer types all required fields.   
GEMINI-CARBON-REVIEW.md
+ 1

Boundary: If Step1Ticket.tsx currently requires a valid JSON structure from TicketVisionReader (even one with all null fields) to transition to the fact-review/correction screen, then fixing Q3 (so D returns a valid structure with null citation/date instead of a parser crash) and ensuring unreadable tickets return null fields rather than malformed JSON is the least invasive fix—avoiding any frontend redesign.   
GEMINI-CARBON-REVIEW.md

Q7 & Q8: Minimal Implementation Packet & Production-Aligned Qualification Plan

Instead of running the on-hold 4-call C/F component harness (which tests an artificial comparison call on F that production never makes), execute the following minimal packet:   
GEMINI-CARBON-REVIEW.md
+ 1

Bounded File List:

backend/src/parkingbreaker/ticket_vision.py: Add pre-parse single-space canonicalization for [\r\n\t]+ strictly on non-identifier text fields (issuer_name, issuance_city, procedural_stage), and retain sanitized raw reply text on parse failure.   
GEMINI-CARBON-REVIEW.md

backend/tests/test_ticket_vision.py & backend/tests/test_ticket_intake_service.py: Add unit tests verifying prose-only newline canonicalization, identifier newline rejection, and TicketIntakeCoordinator.request_review() omission of null fields on captured D/F replies.   
GEMINI-CARBON-REVIEW.md

Offline Replay Verification ($0.00):

Replay the saved raw reply from Case D (captured-reply-boundary-replay.json) through the updated parser to prove D now parses cleanly into a structured reading with null citation/date, halts at the missing-essential-fact gate, and never triggers an unhandled parser refusal.   
GEMINI-CARBON-REVIEW.md
+ 1

Run the focused suite:
PB_PROVIDER_MODE=preview uv run --no-sync pytest tests/test_ticket_vision.py tests/test_ticket_fact_binding.py tests/test_ticket_intake_service.py tests/test_ticket_vision_composition.py.   
GEMINI-CARBON-REVIEW.md

Optional Finite Paid Qualification Approval (Only if live validation of the 20eed44a... prompt is required):

Scope: Run TicketIntakeCoordinator (not raw TicketVisionReader probes) across the 6 fixtures (A–F) using actual binding.review_fields() gating, with fake payment/mail providers.   
GEMINI-CARBON-REVIEW.md
+ 1

Call Budget: Maximum 9–10 calls (6 initial extractions + comparison calls only on cases where review_fields() is non-empty, skipping artificial unknown-field comparisons), capped at $0.25 reservation (~$0.09 estimated usage), 0 retries.   
GEMINI-CARBON-REVIEW.md
+ 1

Next Step for Full Code Sign-Off
Please paste the plain text of backend/src/parkingbreaker/ticket_vision.py, backend/src/parkingbreaker/ticket_fact_binding.py, backend/src/parkingbreaker/ticket_intake_service.py, and web/components/Step1Ticket.tsx so I can verify the exact exception/state handling when TicketVisionReader refuses a response and provide exact line-level diff specifications.   
GEMINI-CARBON-REVIEW.md

I had added my entire repository in the prompt. You should have access to all files.

The repository attachment (venomason/parkingbreaker) was ingested by the platform as a raw, compressed binary ZIP stream rather than extracted plain text. In my context window, the file paths are visible, but the contents of every file inside that archive are deflated binary bytes—for example, backend/src/parkingbreaker/ticket_vision.py appears literally as:   
venomason/parkingbreaker

ParkingBreaker-.../backend/src/parkingbreaker/ticket_vision.pyUT VA 9 +1/Sh .Ru# eQWRE=U N {< E]&3 wipyyN ;cu] * E, E/+@ GbYJu...`   
venomason/parkingbreaker

Furthermore, the archive is truncated mid-stream inside build-kit/evidence/benchmark-source-gaps-2026-11-14/wa-response-rule.pdf, so the web/ and docs/ directories at the end of the alphabetical ZIP listing (Step1Ticket.tsx, intake.ts, BETA-READINESS.md, etc.) are not present in the stream at all. Only the two standalone Markdown files you attached (GEMINI-CARBON-REVIEW.md and HANDOFF.md) came through as readable plain text.   
venomason/parkingbreaker
+ 3

To let me inspect the exact code for ticket_vision.py, ticket_fact_binding.py, ticket_intake_service.py, and Step1Ticket.tsx, either upload those specific files directly as individual attachments or run this one-liner in your terminal and paste/attach the output:   
GEMINI-CARBON-REVIEW.md

Bash
cd /home/ghostmonday/Desktop/ParkingBreaker-Codebase && head -n 9999 \
  backend/src/parkingbreaker/ticket_vision.py \
  backend/src/parkingbreaker/ticket_fact_binding.py \
  backend/src/parkingbreaker/ticket_intake_service.py \
  web/components/Step1Ticket.tsx \
  web/lib/intake.ts \
  docs/verification/deepseek-corrected-live-2026-11-16.json
Once those plain-text contents are visible, I will give you the exact line-by-line verification and diff specifications.

Well then what's stopping you from going to github.com/venommason/parking-breaker? It's a public repository.

You stopped this response
https://github.com/venomason/ParkingBreaker

Two constraints prevent pulling those files from [https://github.com/venomason/ParkingBreaker](https://github.com/venomason/ParkingBreaker) directly:

Tool Boundary: My environment only has a web search snippet tool (google:search), not a live URL fetcher or git clone client—and newly pushed or niche repositories aren't indexed line-by-line in search engine snippets.

Local Branch Divergence: Even if public GitHub main (e4e4a28a912c84c34e66044f9e47ae7aba3d6b4b) were fully indexed, your review brief specifies that the candidate under review (20eed44a059e83b616bb741741c669f5e4cb08c462992fe3318b89c0e9cd8df4 on local branch beta/controlled-readiness-2026-11-15) has uncommitted and unstaged local changes that have not been pushed to GitHub. Reviewing the public main branch would miss the exact DeepSeek prompt and adapter changes you are asking CARBON to evaluate.   
GEMINI-CARBON-REVIEW.md
+ 1

Fastest way to get those exact local files into this chat in 5 seconds:

Run this command in your terminal to bundle only the plain-text review files into a single Markdown file, then drag-and-drop /tmp/carbon-review-files.md here:

Bash
cd /home/ghostmonday/Desktop/ParkingBreaker-Codebase && {
  for f in \
    backend/src/parkingbreaker/ticket_vision.py \
    backend/src/parkingbreaker/ticket_fact_binding.py \
    backend/src/parkingbreaker/ticket_intake_service.py \
    backend/src/parkingbreaker/llm_deepseek.py \
    web/components/Step1Ticket.tsx \
    web/lib/intake.ts \
    docs/verification/deepseek-corrected-live-2026-11-16.json; do
    echo "=== FILE: $f ==="
    cat "$f"
    echo ""
  done
  echo "=== GIT DIFF ==="
  git diff
} > /tmp/carbon-review-files.md
Upload /tmp/carbon-review-files.md (which will come through as readable plain text just like GEMINI-CARBON-REVIEW.md and HANDOFF.md did), and I will immediately complete the line-by-line code verification.   
GEMINI-CARBON-REVIEW.md
+ 1

tmonday@agent-218922446017 ~/Desktop/ParkingBreaker-Codebase  main

❯ 

ghostmonday@agent-218922446017 ~/Desktop/ParkingBreaker-Codebase  main

❯ 

ghostmonday@agent-218922446017 ~/Desktop/ParkingBreaker-Codebase  main

❯ 

ghostmonday@agent-218922446017 ~/Desktop/ParkingBreaker-Codebase  main

❯ 

ghostmonday@agent-218922446017 ~/Desktop/ParkingBreaker-Codebase  main

❯ cd /home/ghostmonday/Desktop/ParkingBreaker-Codebase && {

  for f in \

    backend/src/parkingbreaker/ticket_vision.py \

    backend/src/parkingbreaker/ticket_fact_binding.py \

    backend/src/parkingbreaker/ticket_intake_service.py \

    backend/src/parkingbreaker/llm_deepseek.py \

    web/components/Step1Ticket.tsx \

    web/lib/intake.ts \

    docs/verification/deepseek-corrected-live-2026-11-16.json; do

    echo "=== FILE: $f ==="

    cat "$f"

    echo ""

  done

  echo "=== GIT DIFF ==="

  git diff

} > /tmp/carbon-review-files.md

ghostmonday@agent-218922446017 ~/Desktop/ParkingBreaker-Codebase  main

❯ 

/tmp/carbon-review-files.md is now generated on your machine—just attach that file to your next message (via the + / upload button) so I can read the contents.

One quick heads-up before you upload: your terminal prompt shows you are currently on the main branch (~/Desktop/ParkingBreaker-Codebase  main). If the uncommitted DeepSeek candidate changes (20eed44a...) live on the beta/controlled-readiness-2026-11-15 branch or in another working directory, make sure git status / git branch reflects those local edits before exporting /tmp/carbon-review-files.md so the git diff and docs/verification/deepseek-corrected-live-2026-11-16.json are included.   
GEMINI-CARBON-REVIEW.md
+ 2

just list the files you want again

These are the exact files requested in the review brief:

backend/src/parkingbreaker/ticket_vision.py

backend/src/parkingbreaker/ticket_fact_binding.py

backend/src/parkingbreaker/ticket_intake_service.py

backend/src/parkingbreaker/llm_deepseek.py

backend/src/parkingbreaker/llm_config.py

backend/src/parkingbreaker/llm_input_budget.py

web/components/Step1Ticket.tsx

web/lib/intake.ts

The fact-review component imported/reached from Step1Ticket.tsx (in web/components/)

backend/tests/test_ticket_vision.py

backend/tests/test_ticket_fact_binding.py

backend/tests/test_ticket_intake_service.py

backend/tests/test_ticket_vision_composition.py

docs/verification/deepseek-corrected-live-2026-11-16.json

docs/BETA-READINESS.md   
GEMINI-CARBON-REVIEW.md

Committed and pushed to GitHub main: 332b838.

13,643 tests passed, plus lint, formatting, types and qualification self-tests.
GitHub and local main match; working tree is clean.
Commit signature verifies locally; GitHub reports unknown_key.
Secrets, databases, dependencies and private photos were excluded.
No deployment occurred. Customer beta remains NO-GO pending the documented blockers.



please await 5 more files after this message to reply

Received the 10 files (BETA-READINESS.md, deepseek-corrected-live-2026-11-16.json, intake.ts, llm_config.py, llm_deepseek.py, llm_input_budget.py, Step1Ticket.tsx, test_ticket_fact_binding.py, test_ticket_intake_service.py, and test_ticket_vision.py) and noted the 332b838 commit state. Standing by for the remaining 5 files (ticket_vision.py, ticket_fact_binding.py, ticket_intake_service.py, TicketFactReview.tsx, and test_ticket_vision_composition.py) before delivering the full review.   
BETA-READINESS.md
+ 4

With all 15 source files and test suites inspected in full, here is the Gemini CARBON architectural and evidence review for ParkingBreaker's controlled beta intake path.

1. Reconstructed Production Pipeline & Verified Enforcement (Question 1)
Tracing the exact execution path from image upload through mail dispatch confirms that provenance separation, spend caps, and confirmation gates are already enforced across five layers:

Image Preflight & Budget Allocation (llm_input_budget.py, llm_deepseek.py): Only single-frame, dimension-bounded (<= 8000x8000, <= 16MP) JPEG and PNG attachments pass image_attachment_tokens() and _media_type(). DeepSeekLlmClient.complete_json() enforces model_id == "deepseek-flash" for vision calls, sends detail: "original", caps inline payload size, makes a single transport call (max_retries=0), and reconciles token usage against the shared 50-cent AiWorker.INTAKE_READING subject cap.

Initial Extraction (TicketVisionReader.extract -> TicketIntakeCoordinator.request_reading): Reserves a single durable intake_id per workspace and claims IntakeOperation.EXTRACT bound to input_binding_hash("extract", images) before touching the provider. Replay, alias, PENDING, or UNKNOWN claims return stored state without re-calling DeepSeek. Valid responses must satisfy the closed 11-field READING_SCHEMA (citation_number, license_plate, license_plate_state, issuer_name, issue_date, notice_date, printed_deadline, violation_text, issued_city, issued_state, stage_text), where every value is either shown (printable single-line <= 200 chars) or not_shown/unreadable (None).

Literal vs. Adopted Binding (intake.ts, ticket_fact_binding.py): In the browser, literalFromReading() projects shown fields to strings and maps both not_shown and unreadable to null. factDraftFromReading() prefills only 7 plain text suggestions (citation_number, license_plate, license_plate_state, issuer_hint, issued_city, issued_state, violation_description); the customer must explicitly supply ISO calendar dates (issue_date, and notice_date/printed_deadline or their _absent checkboxes), issuer_kind (GOVERNMENT/PRIVATE), violation_code, and stage. TicketFactBinding.bind() freezes both the 11-field literal tuple and the 13-field adopted tuple into closed_field_digest.

Review Scope Filtering (TicketFactBinding.review_fields -> TicketIntakeCoordinator.request_review): binding.review_fields() returns {key: value for key, value in self.literal if value is not None}. This is a critical architectural finding: the secondary review call compares the image against the non-null literal OCR strings, not the customer's adopted edits, and omits every field where initial extraction returned not_shown or unreadable (None). Furthermore, if all 11 literal fields are None (e.g., an entirely unreadable photo), review_fields() is empty ({}) and request_review() immediately raises TicketIntakeServiceError("ticket_facts_incomplete") without calling the provider.

Confirmation, Submit & Mail Revalidation (TicketIntakeCoordinator.confirm, require_submission_confirmation, revalidate_for_mailing): Every non-match review field requires an explicit customer TicketDiscrepancyResolution (CONFIRMED_VALUE_STANDS, ABSENCE_ACKNOWLEDGED, or UNREADABLE_ACKNOWLEDGED). Any edit to a ticket.* field or photo invalidates ticket_fact_binding and marks confirmation stale, whereas edits to story or contact fields preserve the confirmation. Both submit() (before prehold) and revalidate_for_mailing() (before Lob dispatch) re-verify exact 13-field binding.matches(body.ticket) equality and ordered_images_digest(images) equality.

Verdict on Q1: Do not add a second uncertainty layer. Provenance separation and discrepancy resolution are already cryptographically bound across extraction, review, submission, and mailing.   
intake.ts
+ 4

2. Safety vs. Availability Analysis & Answers to Questions 2–6
Question 2: Whole-Response Refusal vs. Field-Recovery Score (Cases C & D)
Judgment: A whole-response refusal (reading.refused = True) or unreadable (None) field on deliberately degraded inputs (Cases C and D) is a safe fail-closed operating outcome—it prevents unverified or hallucinated text from entering literal suggestions and blocks unconfirmed progression. However, it must remain recorded as a failed strict structured-response / availability outcome in component benchmarks, because a well-behaved clerk model should return structured JSON with "state": "unreadable", "value": null for unreadable fields rather than crashing the JSON/single-line parser.

Observability Gap Found in Code: In llm_deepseek.py (lines 201–215) and ticket_vision.py (parse_reading), when a response fails _parse_reading(data) (like Case C), TicketIntakeCoordinator.request_reading() persists _refusal_result_json(...) with "fields": {}, discarding the raw model JSON/text. Recording a sanitized failure reason code (e.g., missing_key, nonprintable_char, finish_reason_length, invalid_json) in the local diagnostic/oplog or benchmark harness—without storing unredacted PII in public artifacts—will prevent blind spots like Case C's unknown refusal cause.   
GEMINI-CARBON-REVIEW.md
+ 4

Question 3: Printed Line Wraps & Single-Line Contract (Case D)
Code Inspection: Both ticket_vision.py (_visible_text, line 380) and ticket_fact_binding.py (_bounded_line, line 74) enforce value.isprintable(). In Python, "\n".isprintable(), "\r".isprintable(), and "\t".isprintable() are all False. When DeepSeek copied a multi-line issuer header on Case D with a literal \n, _visible_text raised ValueError("visible text must be a single visible line"), causing _parse_reading to refuse the entire 11-field response.

Judgment: Relying solely on prompt instructions ("Represent printed line wraps with a single space") is necessary but brittle for multi-line headers (issuer_name, violation_text, stage_text), where OCR models naturally emit \n between visual lines. However, silently repairing whitespace inside identifiers or dates is unsafe.

Exact Canonicalization Rule (If Adopted Beyond Prompt-Only):

Option A (Strict Parser + Prompt Alignment — Current 332b838 State): Keep _visible_text unchanged (value.isprintable()) and test whether the updated EXTRACT_SYSTEM / REVIEW_SYSTEM prompt in 332b838 eliminates \n emissions live.

Option B (Targeted Prose-Only Line-Wrap Canonicalization — Recommended Fallback if Live D Still Emits \n): In ticket_vision.py, canonicalize ASCII line breaks (\r\n, \n, \r) to a single space " " only for the 4 multi-word prose/header fields (TicketField.ISSUER_NAME, TicketField.VIOLATION_TEXT, TicketField.ISSUED_CITY, TicketField.STAGE_TEXT), while keeping strict rejection (isprintable() with zero repair) on all 7 identifier, code, and date fields (citation_number, license_plate, license_plate_state, issue_date, notice_date, printed_deadline, issued_state).

Counterexamples preserved: "AB\n123" in citation_number or "10/\n01/2026" in issue_date or "C\nA" in license_plate_state must still be refused immediately without repair.   
GEMINI-CARBON-REVIEW.md
+ 4

Question 4: Case F Unreadable Citation & Comparison Boundary
Code Inspection: In Case F, initial extraction returned citation_number: {"state": "unreadable", "value": null}. When the customer enters their citation number in TicketFactReview.tsx, literalFromReading() maps citation_number to null in binding.literal, while binding.adopted holds the customer's typed citation number. When TicketIntakeCoordinator.request_review() runs, it calls binding.review_fields(), which strips all keys where literal is None. Thus, citation_number is never sent to TicketVisionReader.review() in production.

Subtle Edge Case Discovered in TicketIntakeCoordinator.request_review(): Look at lines 489–491 of ticket_intake_service.py:

Python
review_fields = binding.review_fields()
if not review_fields:
    raise _error("ticket_facts_incomplete")
And look at test_fake_preview_enables_full_synthetic_acceptance in test_ticket_vision_composition.py (lines 118–136): in that test, SyntheticVisionLlm returns "not_shown" (null) for all 11 fields during POST /ticket-reading, but the test manually PUTs binding_for() (which has 3 non-null literal fields: citation_number, issue_date, license_plate) before calling POST /ticket-review!
In the real browser UI (Step1Ticket.tsx + intake.ts), literal is constructed from reading.fields via literalFromReading(reading.fields). If reading.fields has all 11 fields as not_shown or unreadable (which happens on a totally unreadable ticket, or in PB_TICKET_READING_PREVIEW=1 with SyntheticVisionLlm!), literalFromReading produces a TicketLiteral where all 11 values are null. When the customer fills out the 13 adopted facts and clicks "Save facts and compare", binding.review_fields() will be {} (empty) and request_review() will fail with ticket_facts_incomplete!

Verdict on Q4: For partially readable tickets (like Case F, where issuer/date/etc. are shown and only citation_number is unreadable), binding.review_fields() is non-empty and safely omits citation_number—no extra guard is needed for F. However, for a ticket where zero fields were shown in initial extraction (all 11 literal fields are null), request_review() currently blocks the customer with ticket_facts_incomplete even after they type all 13 facts themselves (see Question 6 below).   
GEMINI-CARBON-REVIEW.md
+ 4

Question 5: Case D License Plate (DEM0926 vs. DEMO926)
Judgment: Do not force DEMO926 in prompts or add 0/O string-equivalence rules in production code. In North American license plates, 0 and O share identical or near-identical glyphs across many jurisdictions.

Action: Keep the discrepancy documented in the benchmark assessment (deepseek-corrected-live-2026-11-16.json) or allow an explicit dual-oracle/ambiguous-glyph note in the offline benchmark grader only. In production, even if initial extraction reads DEM0926 and the customer edits license_plate to DEMO926, remember that binding.review_fields() sends the literal string (DEM0926) to review(), while binding.adopted stores the customer's confirmed DEMO926 for the appeal letter. Moreover, Case D lacks essential citation/date facts and stops before charge/dispatch.   
GEMINI-CARBON-REVIEW.md
+ 3

Question 6: Can a Customer Correct Essential Facts When Extraction Refuses or Returns All-Null?
Code Inspection (Step1Ticket.tsx, TicketFactReview.tsx, ticket_intake_service.py):

When Whole-Response Extraction is refused (Cases C & D in Trial 2):
In Step1Ticket.tsx (lines 336–339 and 353):

TypeScript
{vision.reading && vision.reading.status !== "success" && vision.reading.status !== "missing" && (
  <p className="muted small">No comparison or confirmation is possible until a readable result is saved.</p>
)}
...
{vision.literal && vision.reading?.status === "success" ? (
  <TicketFactReview ... />
And in TicketFactReview.tsx (line 206):

TypeScript
<button className="btn btn-primary" type="submit" id="visionReviewBtn" disabled={locked || busy || !reading || reading.status !== "success"}>
Verified Behavior: When reading.status === "refused", the UI hides <TicketFactReview/> entirely and displays: "The reading service could not return usable suggestions. Start a new reading. No comparison or confirmation is possible until a readable result is saved." Furthermore, because input_binding_hash("extract", current.images) is deterministic for the uploaded photo bytes, clicking "Read my ticket photos" again with the same photo hits the idempotent claim replay in TicketIntakeCoordinator.request_reading() (line 419 / line 433: if not claim.should_call: return self._reading_snapshot(claim, current)) and immediately returns the cached refused status without calling the provider! Thus, if extraction is refused, the customer must upload a different/clearer photo; they cannot type or correct facts on that photo.

When Extraction Succeeds (status === "success"), and at Least 1 Field is shown (e.g., Case F):<TicketFactReview/> renders, prefills any shown fields, leaves unreadable/not_shown fields blank for the customer to type, and request_review() succeeds because binding.review_fields() has at least 1 non-null literal field.

When Extraction Succeeds (status === "success"), but ALL 11 Fields are unreadable or not_shown (e.g., SyntheticVisionLlm in preview mode or a blurry photo where the model validly returns unreadable for all 11 fields):<TicketFactReview/> renders and allows the customer to type all 13 facts, but clicking "Save facts and compare" fails in TicketIntakeCoordinator.request_review() (line 490) with ticket_facts_incomplete because binding.review_fields() is empty ({}), and TicketFactReview.tsx (line 261) also disables "Confirm these ticket facts" when !hasReview (reviewFields.length === 0)!

Architectural Verdict on Q6:

Requiring a clearer photo when the image is completely unreadable (zero shown fields or whole-response refusal) is consistent with Amir's rule ("Customers supply a readable ticket or correct missing facts themselves. Essential facts that remain unavailable must stop charge/dispatch"), provided that partially readable tickets (where 1–10 fields are unreadable/not_shown, like Case F) allow customer entry of the missing fields.

However, Case D had readable fields and only failed because the model emitted a \n in issuer_name (turning a partially readable ticket into a whole-response refused dead-end), and Case C was refused by the parser rather than returning structured unreadable states. That is why preventing avoidable whole-response refused outcomes on C and D (via the 332b838 prompt rules and, if needed, prose line-wrap canonicalization) directly determines whether the customer reaches the <TicketFactReview/> correction form.   
GEMINI-CARBON-REVIEW.md
+ 4

3. Production-Aligned Qualification Design & Minimal Implementation Packet (Questions 7 & 8)
Why the On-Hold 4-Call C/F Plan Should Not Run Unchanged
The on-hold 4-call C/F plan tests TicketVisionReader.review() directly on Case F with an asserted citation_number. As proven in TicketFactBinding.review_fields() (ticket_fact_binding.py:182) and TicketIntakeCoordinator.request_review() (ticket_intake_service.py:489), production never sends citation_number to review() when extract() returned unreadable (None) for citation_number.   
GEMINI-CARBON-REVIEW.md
+ 3

Minimal Code Packet (Zero-Rewrite, Read-Only Until Approved)
Because commit 332b838 (20eed44a...) is already committed and passes all 13,643 backend tests with the tightened single-line and whole-field-unreadable prompt rules, no production code change is required before running a finite qualification, with one optional test/harness addition:

Keep Production Code at 332b838 (20eed44a...) Unchanged First: Test the committed prompt fixes (EXTRACT_SYSTEM and REVIEW_SYSTEM in backend/src/parkingbreaker/ticket_vision.py) before weakening _visible_text() parser strictness.

If and Only If Case D Still Emits a Literal \n in issuer_name During Live Qualification: Apply the 5-line prose-only line-wrap canonicalization (" ".join(value.splitlines()) only when \n or \r is present in ISSUER_NAME, VIOLATION_TEXT, ISSUED_CITY, or STAGE_TEXT) in backend/src/parkingbreaker/ticket_vision.py and backend/tests/test_ticket_vision.py.

Retain Raw Reply Diagnostics in the Benchmark Harness Only: Ensure the qualification script saves the raw JSON payload (or refusal reason) in the private Vault receipt whenever reading.refused or review.refused is True, so no refusal is ever opaque like Case C in Trial 2.   
GEMINI-CARBON-REVIEW.md
+ 4

Finite Production-Aligned Acceptance Plan (No Customer Charge, No Mail)
Instead of raw TicketVisionReader probes with synthetic assertions for null fields, run a two-stage qualification:

Stage 1: Zero-Cost Offline Coordinator Boundary Replay ($0.00, 0 Provider Calls)

Use test_ticket_intake_service.py pattern (ScriptedVisionClient + TicketIntakeCoordinator + CaseService with fake Stripe/Lob) to replay the captured Trial 2 extraction outputs for A, B, E, and F:

Verify that for F, literalFromReading sets literal["citation_number"] = None, the customer supplies adopted["citation_number"], coordinator.request_review() sends only the non-null literal fields from F (excluding citation_number), and confirmation + submit() succeed with review_fields() untouched.

Verify that for C and D (when refused or missing essential facts without customer completion), coordinator.confirm() and service.submit() are blocked prior to prehold (fake_resolution == []).

Stage 2: Finite Live DeepSeek Qualification Approval Request (Exact Bound)

Target: deepseek-flash (DeepSeekLlmClient) via the production TicketVisionReader.extract() -> TicketFactBinding.bind() -> binding.review_fields() -> TicketVisionReader.review() pipeline on candidate 332b838 (20eed44a...).

Scope & Call Count:

Option 1 (Focused Delta Qualification — Smallest Spend): Run only the failed/changed extraction cases C, D, F (3 extract calls) + production-scoped review(binding.review_fields()) on D and F only if their extraction succeeds and yields non-empty review_fields() (<= 2 review calls, where F's review payload naturally omits the unreadable citation_number). Total: 3 to 5 unique calls, $0.15 max reservation (~$0.05 estimated usage), 0 retries.

Option 2 (Full 6-Case Coordinator Qualification): Run extract() on A–F (6 calls) + review(binding.review_fields()) only on cases where extract() is not refused and binding.review_fields() is non-empty (<= 5 calls; C has no readable fields or refuses, F omits citation_number). Total: 9 to 11 unique calls, $0.33 max reservation (~$0.11 estimated usage), 0 retries.

Separate Acceptance Criteria:

Safety (Must be 100%): Zero fabricated or damaged partial identifiers returned as shown or match; review_fields() omits null literals; missing essential facts halt before prehold/charge/mail.

Availability / Formatting (Tracked Separately): A, B, E, F return valid structured JSON (refused=False); D returns valid single-line issuer_name (refused=False, plate DEM0926/DEMO926 recorded as glyph ambiguity); C either returns structured unreadable/not_shown JSON (refused=False) or fails closed (refused=True with raw reply captured in the private receipt).   
GEMINI-CARBON-REVIEW.md
+ 4

Summary of Unresolved Assumptions & Decision Prompt
All-Null Extraction UX (ticket_facts_incomplete): If a ticket image is so blurry that extract() succeeds (refused=False) but returns unreadable/not_shown for all 11 fields, binding.review_fields() is {} and TicketIntakeCoordinator.request_review() raises ticket_facts_incomplete—meaning the customer cannot manually enter all 13 facts on a 100%-unreadable photo; at least 1 of the 11 literal fields must be shown on the photo, or the customer must upload a clearer photo. Confirm that requiring at least one readable literal field on the photo (rather than allowing blind manual entry on a blank/unreadable image) is the intended product boundary.

Next Step Selection: Would you like to authorize Option 1 (3–5 calls, <= $0.15 reservation on C/D/F) or Option 2 (9–11 calls, <= $0.33 reservation on A–F) against 332b838 (20eed44a...), or add the offline coordinator replay test for Case F to test_ticket_intake_service.py first?

After seeing these late game things that we had to troubleshoot right as we're getting to the finish line, it looks like we're about to be able to launch. What other things might we discover are actual blockers to launch that we might not realize until after we have launched, which could mess things up significantly? Can you come up with a prompt for us to do some deep research in trying to identify and target these issues before we find them too late?

The hardest post-launch failures in an AI-assisted legal/clerical dispatch system almost never come from happy-path unit logic—they come from physical-world seams (camera formats, postal printing geometry, payment hold expirations, and municipal portal quirks) where your code's strict invariants collide with messy real-world inputs.

1. High-Risk "Day-2" Launch Blockers Specific to ParkingBreaker
Based on the architecture and test suite in 332b838, these eight failure modes pose the highest risk of surfacing only after real customers hit production:

iOS Safari HEIC/HEIF & 48MP Camera Rejections

The Seam: Step1Ticket.tsx sets accept="image/jpeg,image/png", and llm_input_budget.py strictly rejects anything exceeding 8,000 pixels on a side or 16,000,000 total pixels (MAX_IMAGE_PIXELS = 16_000_000), as well as non-JPEG/PNG media.

The Trap: Modern iPhones (iPhone 14 Pro through 17/18) shoot 24MP (5712x4284 = 24.4M pixels) or 48MP (8064x6048 = 48.7M pixels) by default, and iOS file pickers ("Choose File" from Files or certain gallery flows) frequently hand over raw .HEIC or 24MP+ JPEGs without downscaling. Those users will hit an immediate ticket_photo_not_supported (UnboundableRequestError) wall before they even start.

review_fields() Reviewing literal Instead of Customer-Edited adopted Values

The Seam: In ticket_fact_binding.py, review_fields() returns {key: value for key, value in self.literal if value is not None}, and TicketIntakeCoordinator.request_review() sends review_fields as the confirmed mapping to TicketVisionReader.review().

The Trap: If initial extraction misreads a character (e.g., DEM0926), literal["license_plate"] is "DEM0926". When the customer corrects the input box to "DEMO926", adopted["license_plate"] becomes "DEMO926", while literal["license_plate"] remains "DEM0926". The independent review call then asks DeepSeek whether the image matches "DEM0926" (the old literal!), not "DEMO926" (the customer's correction). If DeepSeek agrees with its first reading ("DEM0926"), it returns comparison: "match", so the UI tells the customer "The visible text matches what the reading suggested" even though adopted (DEMO926) differs from literal (DEM0926)! Conversely, if all 11 fields on a blurry ticket are initially unreadable (None), review_fields() is empty ({}) and request_review() throws ticket_facts_incomplete, trapping the user even after they manually type all 13 fields.

50-Cent Shared INTAKE_READING Budget Starvation Before Prehold

The Seam: Extraction, review, and prehold issuer/link assessment all share a single 50-cent cap (WORKER_CAPS_CENTS[AiWorker.INTAKE_READING] = 50) bound to the workspace's reserved intake_id.

The Trap: Because llm_input_budget.py reserves max(encoded_bytes, pixels, 8192) tokens per image for generic calls (or 8192 per image in llm_deepseek.py), a customer who uploads front + back photos, realizes the back photo is blurry, uploads a replacement photo, and runs extraction + review twice can exhaust the 50-cent cap before reaching submit(), causing prehold issuer resolution to fail with LlmBudgetExceeded.

Stripe Authorization Hold Expiry vs. Review Queue / Deadline Windows

The Seam: Standard Stripe card capture_method="manual" holds expire after 7 calendar days (unless extended or using adaptive capture).

The Trap: If a case enters case_hold_store or review_queue, or waits on a municipal "notice of violation" window before Lob mailing, the Stripe authorization hold can expire in the background before submission_coordinator triggers capture at mail time—resulting in either a failed capture after a letter is queued or an unrecoverable case stall.

Lob Physical Mail Geometry, Address Strictness & Certified Mail Return Addresses

The Seam: Municipal parking agencies frequently use PO Boxes, Attention lines (ATTN: CITATION PROCESSING CENTER / DEPT 412), or non-standard courthouse addresses that fail USPS CASS strict deliverability checks if street/secondary line splitting is rigid.

The Trap: In Lob's live environment, Certified Mail (CERTIFIED_CENTS) has stricter top-margin/first-page address window clear-zone rules and return-address requirements than standard mail. A PDF packet that passes local page-count and text checks (mail_pages.py, packet.py) can be rejected by Lob's live renderer or have the recipient address clipped by the green Certified Mail banner on Page 1.

SQLite Concurrency & Ephemeral Disk / Litestream Replication on Cloud Run

The Seam: SqliteTransactionStore uses local SQLite with WAL.

The Trap: If Cloud Run scales above max-instances=1 (even briefly during a deployment rollout or traffic spike), two instances writing to separate container disks or contending over a network-mounted volume will split-brain reservations, webhook backlogs, and idempotency claims. Additionally, if a container crashes mid-flight while claim_operation is PENDING, TicketIntakeCoordinator intentionally never retries PENDING or UNKNOWN claims for the same image hash—leaving that customer permanently stuck on "The reading is still being processed" unless the recovery CLI (recovery_cli.py) sweeps stale pending claims promptly.

Municipal "Wet Signature" & "Original Citation Stub Enclosed" Traps

The Seam: Your trap_catalog.py and required_copies.py track procedural traps, but real cities frequently update contest instructions on the back of thermal tickets.

The Trap: Some agencies reject mailed appeals unless the physical original ticket stub is enclosed or a wet-ink signature (or notarized affidavit of non-liability) is present. If the back-of-ticket OCR or research pass misses a newly added "must enclose original citation" clause, Lob mails a printed letter without the stub and the city summarily rejects it.

Email Deliverability & Magic-Link Scanner Prefetching

The Seam: access_email.py and access_link_material.py send customer access links.

The Trap: Corporate and university email security gateways (Mimecast, Proofpoint, Defender) Headless-GET every link in incoming emails within milliseconds of delivery. If the access link is single-use or binds session state on GET rather than requiring an explicit POST/button click, the spam filter consumes the customer's access token before they open the email.   
venomason/parkingbreaker
+ 4

2. Deep-Research Prompt to Uncover Hidden Pre-Launch Blockers
Copy and run the prompt below in a deep-research session (or with a full-repo audit agent) to systematically hunt down silent failure modes before opening the beta:

Markdown
# Deep Research & Pre-Launch Failure-Mode Audit: ParkingBreaker Controlled Beta

## Context & System Architecture
We are preparing a controlled beta launch for **ParkingBreaker** (`https://github.com/venomason/ParkingBreaker`, commit `332b838`), a clerical web application that helps customers contest parking tickets by:
1. Accepting 1–2 ticket photos (JPEG/PNG) in a Next.js/React frontend (`web/`).
2. Extracting 11 literal ticket fields (`citation_number`, `license_plate`, `license_plate_state`, `issuer_name`, `issue_date`, `notice_date`, `printed_deadline`, `violation_text`, `issued_city`, `issued_state`, `stage_text`) via DeepSeek V4.1 Flash (`deepseek-flash`) in a Python backend (`backend/src/parkingbreaker/`).
3. Requiring the customer to enter/confirm 13 adopted `TicketFacts` and running an independent model comparison (`TicketVisionReader.review`) bound by SHA-256 digests (`TicketFactBinding`) under a 50-cent per-case `INTAKE_READING` spend cap.
4. Resolving the issuing agency and contest procedure via web search (`Brave` / `.gov` snapshot) and DeepSeek V4 Pro (`deepseek-v4-pro`) research.
5. Placing a Stripe manual-capture payment hold (`stripe_payment.py`, `payment_coordinator.py`).
6. Generating a procedural appeal letter PDF (`appeal/drafting.py`, `appeal/packet.py`) and dispatching physical mail (Standard or Certified) via Lob (`lob_client.py`, `lob_submission.py`), with SQLite (`SqliteTransactionStore`) as the single-authority state store deployed on Cloud Run.

## Objective
Conduct an adversarial "pre-mortem" and deep technical/operational research audit to identify **post-launch blockers**—bugs, third-party API edge cases, device/browser incompatibilities, legal/procedural traps, or state-machine deadlocks that pass unit/synthetic tests today but will break real customer cases after launch.

 Do NOT give generic startup advice. Investigate and report concrete, technical, and operational failure modes across the following 7 domains, citing exact API behaviors, external provider constraints, and codebase seams:

### Domain 1: Mobile Browser & Camera Image Pipeline (iOS Safari & Android Chrome)
- How do iOS 17/18/19 Safari and Android Chrome handle `<input type="file" accept="image/jpeg,image/png">` when a user selects a photo from their library vs. takes a live camera photo (specifically 24MP/48MP iPhone Pro photos, HEIC-to-JPEG auto-conversion, EXIF orientation tags stripped during upload, and Display P3 / wide-gamut color profiles)?
- Under what conditions will a standard smartphone photo fail Pillow's `MAX_IMAGE_PIXELS = 16_000_000` (16MP), `MAX_IMAGE_DIMENSION = 8_000`, `n_frames == 1` (e.g., iOS Live Photos / MPF multi-picture format JPEGs with embedded preview frames!), or `MAX_INLINE_IMAGE_BASE64_CHARS`?
- **Crucial check:** Do modern smartphones save JPEGs with MPO (Multi-Picture Object / depth map / gain map) headers that cause Pillow (`Image.open`) to report `n_frames > 1` or `format == "MPO"` instead of `"JPEG"`, triggering a false `UnboundableRequestError` rejection?

### Domain 2: Intake State Machine, Claim Idempotency & UX Deadlocks
- Trace every state in `ticket_intake_service.py`, `ticket_fact_binding.py`, `Step1Ticket.tsx`, and `TicketFactReview.tsx` where a customer can become permanently deadlocked in their workspace without a recovery button:
  - What happens when `extract()` returns `"unreadable"`/`"not_shown"` for all 11 fields (`binding.review_fields() == {}`) and the customer types all 13 facts and clicks "Save facts and compare"?
  - What happens when `extract()` misreads a field (e.g., `DEM0926`), the customer corrects `adopted` to `DEMO926`, and `request_review()` sends `binding.review_fields()` (which uses `self.literal`, not `self.adopted`) to `review()`?
  - What happens if a customer's browser tab background-suspends on mobile during `POST /ticket-reading` or `POST /ticket-review`, or if a provider call times out at the 20.0s SDK timeout leaving the claim in `UNKNOWN` or `PENDING`? How does the customer recover without support intervention?
  - How many photo re-uploads or fact re-comparisons can a customer perform before exhausting the shared 50-cent `WORKER_CAPS_CENTS[AiWorker.INTAKE_READING]` budget prior to `submit()` / `prehold`?

### Domain 3: DeepSeek V4.1 Flash / V4 Pro & Brave Search Live Provider Quirks
- What are the real-world failure modes of DeepSeek's `response_format: {"type": "json_object"}` and vision endpoint under load (e.g., empty `{}` returns on `finish_reason == "length"`, markdown fences inside JSON strings, Unicode control/zero-width characters failing Python `.isprintable()`, rate-limit headers, or 502/504 gateway timeouts at 20s)?
- What happens in the research pipeline (`research/run.py`, `brave_search.py`) when municipal parking pages are behind Cloudflare Bot Management, Akamai, JavaScript-rendered SPAs (e.g., Tyler Technologies, AIMS, Passport, PayLock, Duncan/CivicSmart portals), or PDFs with scanned image-only text?

### Domain 4: Lob Physical Mail & PDF Rendering Traps
- What are Lob's exact production validation rules for `letters` and `certified` mail that local unit tests do not enforce?
  - Address length limits (name <= 40 chars, address_line1 <= 64 chars), secondary address parsing, PO Box / municipal department routing (`ATTN:` lines), and `us_verifications` strictness settings.
  - Page-1 address window & Certified Mail banner geometry: where does Lob place the address block and Certified Mail barcode on page 1, and can `appeal/packet.py` text collide with Lob's reserved regions?
  - Webhook ordering and idempotency (`lob_webhook.py`, `webhook_backlog_store.py`): what happens if Lob emits out-of-order tracking events, duplicate webhooks, or `letter.failed` / `letter.returned_to_sender` after Stripe capture?

### Domain 5: Stripe Manual-Capture Hold Lifecycle & Financial Reconciliation
- Trace the timing between `stripe_payment.py` authorization (`capture_method="manual"`), `case_hold_store.py`, `review_queue.py`, and `submission_coordinator.py`:
  - What is the exact Stripe authorization hold expiration window across Visa/Mastercard/Amex (typically 7 days, or 5 days on certain debit networks), and how does it interact with ParkingBreaker's review queue expiry (`test_t39_review_expiry.py`) and mail timing (`mail_timing.py`)?
  - What happens if a customer disputes a charge or a card hold expires in the millisecond window between `lob_submission.py` sending the letter to Lob and `payment_coordinator.py` capturing the PaymentIntent?

### Domain 6: Municipal Parking Contest Reality vs. Clerical Assumptions
- Across the top 40 US cities in `backend/data/cities_2x/`, what real-world municipal contest rules could invalidate a Lob-mailed letter even when the address and deadline are researched correctly?
  - Examples to investigate: mandatory online-only contest portals, mandatory original ticket/envelope enclosure, registered-owner affidavit / wet-signature requirements, third-party mailing prohibitions, or "administrative review vs. civil hearing election" form checkboxes.

### Domain 7: Single-Instance Cloud Run, SQLite WAL & Operational Recovery
- What happens to `SqliteTransactionStore`, `overseer_feed.py`, and `retention_purge.py` on Cloud Run during container instance recycling, cold starts, sigterm shutdowns mid-transaction, or accidental multi-instance scaling?
- Which `recovery_cli.py` or `health_cli.py` commands must be wired to automated cron/alerts before customer #1 arrives?

## Required Deliverable Format
For each finding:
1. **Severity:** `P0 (Silent Safety / Financial / Legal Failure)`, `P1 (Customer Deadlock / Hard Availability Block)`, or `P2 (Operational Friction)`.
2. **Exact Trigger Scenario:** Step-by-step reproduction of how a real customer or external API triggers it.
3. **Code/Provider Seam:** The exact file, function, or external API contract involved.
4. **Smallest Pre-Launch Fix or Guard:** The minimum code, config, or runbook change to eliminate the risk without a broad redesign.
3. Two Immediate Code Findings Worth Highlighting Right Now
While assembling the prompt above from the files you just shared, two concrete P1 issues stood out in the existing code that you will likely hit on Day 1 of iPhone testing:

iPhone JPEG "MPO" Multi-Picture Format Rejection (llm_input_budget.py & ticket_intake_service.py):
Modern iPhones (and many Samsung Galaxy devices in portrait/auto mode) save .jpg files with an embedded secondary gain-map or depth image (MPF / Multi-Picture Format). In Pillow (PIL.Image.open), those standard .jpg files open with decoded.format == "MPO" and decoded.n_frames == 2!
Look at llm_input_budget.py lines 169–184:

if decoded.format != expected_format: -> fails because decoded.format is "MPO", not "JPEG"!

if type(frames) is not int or frames != 1: -> fails because decoded.n_frames is 2!
And look at ticket_intake_service.py line 284 (_media_type):

if detected == "JPEG": -> fails on "MPO" JPEGs and raises ticket_photo_not_supported!
When you run your open iPhone/Safari gate from BETA-READINESS.md, a native iPhone camera photo is very likely to fail upload or preflight unless client-side canvas normalization or MPO primary-frame handling is considered.   
BETA-READINESS.md
+ 2

binding.review_fields() Comparing literal Instead of Customer-Confirmed adopted (ticket_fact_binding.py & ticket_intake_service.py):
Look at ticket_vision.py line 203 (REVIEW_SYSTEM):

"The customer has already confirmed values for a closed set of critical ticket fields. Compare each confirmed value with what is literally visible in the attached ticket image."
Yet TicketIntakeCoordinator.request_review() (line 489) passes binding.review_fields(), which returns self.literal (the unedited AI extraction output from TicketVisionReader.extract()), not the customer's self.adopted entries! If the customer fixed a typo in citation_number or license_plate on the form, review() never sees the customer's correction—it re-compares the AI's original literal string against the image.   
ticket_fact_binding.py
+ 2

Would you like me to trace the exact fix for either the literal vs. adopted review payload or the iPhone MPO / high-megapixel JPEG boundary before you run your iPhone/Safari test gate?

{

"at": "2026-11-16T14:42:57.296928+00:00",

"commit": "332b8384859e145379c3b3f29a2d6aad8c7a6f8a",

"application_fingerprint": "20eed44a059e83b616bb741741c669f5e4cb08c462992fe3318b89c0e9cd8df4",

"decision": "FOCUSED_FORMAT_AND_NULL_SCOPE_PASS_WITH_D_PLATE_AMBIGUITY; LIVE_BETA_NO_GO",

"calls": 5,

"reserved_cents": 15,

"local_usage_estimate_cents": 5,

"invoice_cost": "not measured; usage estimate uses existing configured pricing and per-call upward rounding",

"retries": 0,

"raw_replies_retained": 5,

"grades": [

{

"case": "C",

"operation": "extract",

"structured_response_pass": true,

"strict_field_pass": true,

"differences": []

},

{

"case": "D",

"operation": "extract",

"structured_response_pass": true,

"strict_field_pass": false,

"differences": [

{

"field": "license_plate",

"expected": {

"state": "shown",

"value": "DEMO926"

},

"actual": {

"state": "shown",

"value": "DEM0926"

}

}

]

},

{

"case": "F",

"operation": "extract",

"structured_response_pass": true,

"strict_field_pass": true,

"differences": []

},

{

"case": "D",

"operation": "review",

"structured_response_pass": true,

"scope_pass": true,

"matches_supplied_literal_values": true,

"independent_ground_truth_caveat": "D plate remains an unresolved disagreement with original key"

},

{

"case": "F",

"operation": "review",

"structured_response_pass": true,

"scope_pass": true,

"matches_supplied_literal_values": true,

"independent_ground_truth_caveat": null

}

],

"strict_all_fields_pass": false,

"unknown_fields_preserved": {

"C.license_plate": null,

"D.citation_number": null,

"D.issue_date": null,

"F.citation_number": null

},

"conclusion": "Committed prompt alignment produced usable structured responses for C/D/F and correct production-scoped D/F comparisons. No line-wrap canonicalization is indicated by this run. D plate DEM0926 differs from original key DEMO926; preserve uncertainty and customer correction, never force an oracle or normalize O/0.",

"limits": [

"One focused run, not reliability measurement or full six-case release qualification",

"C review and A/B/E not run under this approval",

"All-null/refusal UX and caller-supplied literal provenance questions remain as previously recorded",

"Synthetic adopted envelope exists only to exercise coordinator review; hidden facts were not recovered and no real intake was submitted",

"Printing, target, support, remaining device and other release gates remain open"

],

"next_action": "Keep production parser unchanged. Prepare narrow refusal/all-null UX corrections and resolve literal-origin expectations before beta; retain D glyph discrepancy. No further paid calls authorized by this completed envelope.",

"no_deploy_customer_charge_or_mail": true,

"repository_clean": true

}

The 5-call focused delta run on commit 332b838 (20eed44a...) cleanly validates the prompt-only approach: C, D, and F all returned valid structured JSON (structured_response_pass: true), unknown/damaged fields were preserved as null (C.license_plate, D.citation_number, D.issue_date, F.citation_number), and production-scoped review calls on D and F passed without needing to weaken the strict .isprintable() parser in ticket_vision.py.   
ticket_vision.py

With the parser and prompt boundaries proven, the next_action in your receipt—preparing narrow refusal/all-null UX corrections and resolving literal-origin vs. customer-confirmed expectations—is the exact remaining code blocker in the intake flow. Below is the surgical root-cause trace and the minimal, non-invasive design to resolve both before beta.

1. Resolving the literal vs. adopted Review Seam
The receipt notes that "matches_supplied_literal_values: true" and flags "caller-supplied literal provenance questions" and "all-null/refusal UX" as the next items to resolve. Tracing intake.ts, Step1Ticket.tsx, TicketFactReview.tsx, ticket_fact_binding.py, and ticket_intake_service.py shows why these two issues are actually two sides of the same seam:

What TicketVisionReader.review() is instructed to do (ticket_vision.py):
REVIEW_SYSTEM tells the model: "The customer has already confirmed values for a closed set of critical ticket fields. Compare each confirmed value with what is literally visible in the attached ticket image." And TicketFactReview.tsx renders each row of review.fields alongside Your fact: {adopted || "—"}.

What TicketFactBinding.bind() and Step1Ticket.tsx actually pass (ticket_fact_binding.py, ticket_intake_service.py):

When the customer clicks "Save facts and compare" in TicketFactReview.tsx, the frontend validates the 13-field FactDraft into AdoptedFacts and calls onSaveFacts(facts).   
Step1Ticket.tsx
+ 1

However, the literal half of ticket_fact_binding in the frontend comes from literalFromReading(reading.fields)—which contains only the initial extraction's shown strings and null for not_shown/unreadable fields.   
intake.ts
+ 1

In TicketIntakeCoordinator.request_review(), the backend calls review_fields = binding.review_fields(), which returns {key: value for key, value in self.literal if value is not None}.   
ticket_fact_binding.py
+ 1

The Three Consequences of This Seam:

Customer Corrections Are Ignored by Review (Case D Plate & Case F Citation):

On Case D, extract returns license_plate: "DEM0926". If the customer corrects the input box to "DEMO926", adopted["license_plate"] is "DEMO926", but literal["license_plate"] stays "DEM0926". request_review() sends "DEM0926" to DeepSeek, which returns comparison: "match", visible_text: "DEM0926"—so the UI tells the customer their entry matched even though adopted (DEMO926) differs from visible_text (DEM0926)!   
intake.ts
+ 4

On Case F, extract returns citation_number: null (unreadable). The customer reads their physical ticket and types citation_number into the form (adopted["citation_number"]), but because literal["citation_number"] is null, review_fields() omits citation_number. The customer never gets a comparison row or an UNREADABLE_ACKNOWLEDGED resolution prompt for the unreadable citation they typed.   
ticket_fact_binding.py
+ 2

All-Null Extraction Deadlocks the Customer (ticket_facts_incomplete):

On Case C (or any blurry ticket where extract succeeds with all 11 fields unreadable/not_shown, or in PB_TICKET_READING_PREVIEW=1 where SyntheticVisionLlm returns all not_shown), literalFromReading() produces 11 null values. When the customer types all 13 facts and clicks "Save facts and compare", binding.review_fields() is {} and request_review() raises ticket_facts_incomplete (ticket_intake_service.py), permanently blocking confirmation.   
intake.ts
+ 3

Whole-Response Refusal Deadlocks the Customer (reading.status === "refused"):

In Step1Ticket.tsx, <TicketFactReview/> only renders when vision.literal && vision.reading?.status === "success". If reading.status === "refused", the form is hidden, and clicking "Read my ticket photos" on the same photo replays the cached refused claim without calling the provider.   
Step1Ticket.tsx
+ 1

2. Two Architectural Options to Resolve literal-Origin & All-Null/Refusal UX
Depending on which invariant you want TicketFactBinding.literal to represent, there are two clean, minimal ways to close this before beta:

Option A: Keep literal as "Initial OCR Output" (Current Backend Contract) + Add All-Null/Refusal Escape Hatch
If your design intent (as reflected in production-review-scope-replay.json and the receipt's "scope_pass": true) is that only non-null initial OCR strings (literal) should ever be sent to review() so that customer-typed strings for unreadable fields (like F.citation_number) are never sent to the review prompt:

What stays untouched: TicketFactBinding.review_fields() continues to return {key: value for key, value in self.literal if value is not None}, so F.citation_number (null in literal) is omitted from review().

What must be fixed for All-Null (C) and refused extractions:

When reading.status === "refused" or binding.review_fields() == {} (all 11 literal fields are null):

If your product rule is "Unreadable photo (zero shown literal fields or refused reading) must stop and require a clearer photo":

In Step1Ticket.tsx, when reading.status === "refused" or literal has zero non-null values, display a clear instruction: "We couldn't read enough text from this photo to verify it. Please upload a clearer photo of the front of your ticket" and hide "Read my ticket photos" until frontRef changes (since re-clicking "Read my ticket photos" on the same frontRef will always replay the same refused/all-null claim).   
intake.ts
+ 2

Conversely, if your product rule is "Customers can manually enter all 13 facts even when the photo had 0 readable fields":

In ticket_intake_service.py, when binding.review_fields() is empty ({}), either populate literal from the customer's adopted critical fields or allow request_review() to record an empty-field / UNREADABLE_ACKNOWLEDGED review receipt without throwing ticket_facts_incomplete.   
ticket_fact_binding.py
+ 1

Option B: Bind literal to the Customer's Asserted Printable Strings for Critical Fields (While Preserving Raw Printed Dates)
If review() is meant to compare the customer's confirmed form values against the photo (except for dates, which stay as literal printed strings when shown):

However, note that Option B would send F.citation_number (the customer's typed citation) to review()—which would re-trigger the exact Case F comparison probe (TH-48271 conflict vs. unreadable) that your production-scope replay specifically avoided!   
GEMINI-CARBON-REVIEW.md
+ 2

Therefore, Option A (preserving literal as the extract output so null fields like F.citation_number are omitted from review()) is strongly aligned with your existing tests (test_ticket_fact_binding.py, test_ticket_intake_service.py) and today's 5-call receipt.   
deepseek-corrected-live-2026-11-16.json
+ 2

3. Minimal Pre-Beta UX & Provenance Fix Under Option A (Smallest Diff)
To keep TicketFactBinding and TicketVisionReader 100% compatible with the 13,643 passing tests and today's 5-call receipt while closing the remaining UX/provenance gaps:

Prevent Useless Same-Photo Replay Button on refused (web/components/Step1Ticket.tsx):

Currently, line 326 shows "Read my ticket photos" when vision.reading.status === "refused". Because TicketIntakeCoordinator.request_reading keys claims by input_binding_hash("extract", current.images), clicking that button without changing the photo is a no-op that immediately returns refused again.   
Step1Ticket.tsx
+ 2

Fix: When vision.reading?.status === "refused" && vision.reading.current, replace the "Read my ticket photos" button message with: "This photo could not be read. Choose a clearer front photo above to try again." The button re-enables automatically as soon as the user selects a new photo (!vision.reading.current).   
Step1Ticket.tsx

Handle All-Null Extraction (C) Explicitly (web/components/Step1Ticket.tsx / web/lib/intake.ts):

In intake.ts, add a 3-line helper hasAnyShownLiteral(literal: TicketLiteral | null): boolean (Object.values(literal ?? {}).some((v) => v !== null)).   
intake.ts

In Step1Ticket.tsx, if vision.reading?.status === "success" but !hasAnyShownLiteral(vision.literal) (and !vision.status?.synthetic), inform the customer on Step1Ticket.tsx before they spend 3 minutes typing 13 fields into <TicketFactReview/> that zero fields were readable on the uploaded photo and prompt them to upload a clearer photo—rather than letting them fill out all 13 fields only to hit ticket_facts_incomplete on submit. (Note: in synthetic preview mode where SyntheticVisionLlm returns all not_shown, either seed 1 synthetic shown field in SyntheticVisionLlm or populate literal from adopted in synthetic preview so browser UI preview tests match test_fake_preview_enables_full_synthetic_acceptance).   
intake.ts
+ 4

Detect Customer Edit vs. literal Match Discrepancy in TicketFactReview.tsx:

If extract read DEM0926 (literal.license_plate = "DEM0926"), the customer edited the form to DEMO926 (draft.license_plate = "DEMO926"), and review compared literal (DEM0926) and returned comparison: "match", visible_text: "DEM0926", the UI should check whether the customer's adopted value for that field actually matches item.visible_text.   
intake.ts
+ 1

If item.comparison === "match" (against literal), but the customer's edited adopted value (DEMO926) differs from item.visible_text (DEM0926), surface that difference clearly in TicketFactReview.tsx (or pass the customer-edited string in literal for non-date text fields that were originally shown, so review() compares the customer's DEMO926 directly and returns conflict with CONFIRMED_VALUE_STANDS resolution).   
intake.ts
+ 4

Would you like me to draft the exact patch for Step1Ticket.tsx / intake.ts (and ticket_intake_service.py if needed) to close the refused / all-null / edited-literal UX seam?

Please verify and finish the complete ticket-reading failure recovery path in the current ParkingBreaker checkout. Coordinate with any active writer and preserve existing work. Inspect the latest source and Carbon’s review first; report what is already implemented versus still missing.

When the reader refuses, fails, or returns no readable fields, the customer must be able to provide or correct the essential ticket facts themselves and proceed once all required facts are supplied and confirmed. Clearly distinguish customer-supplied facts from AI-extracted facts; enforce their provenance on the server. Do not invent or silently normalize uncertain characters. If essential facts remain missing, stop before charge or dispatch.

Verify this through meaningful end-to-end tests with simulated providers and external effects: complete reader refusal, all fields unreadable, partial reading with corrections, conflicting characters such as O and zero, incomplete corrections, successful confirmation and continuation, and retries/reload. Verify the actual customer interface and server behavior together. Confirm no real payment or mail is sent. Preserve existing partial-reading behavior and add focused regression coverage.

Give me the exact changed files, test outcomes, current commit/worktree state, and any unresolved issue. Do not claim a live OCR or production fulfillment pass from simulated tests. Don’t run new paid calls, deploy, or commit/push unless separately authorized.

You stopped this response
Please verify and finish the complete ticket-reading failure recovery path in the current ParkingBreaker checkout. Coordinate with any active writer and preserve existing work. Inspect the latest source and Carbon’s review first; report what is already implemented versus still missing.

When the reader refuses, fails, or returns no readable fields, the customer must be able to provide or correct the essential ticket facts themselves and proceed once all required facts are supplied and confirmed. Clearly distinguish customer-supplied facts from AI-extracted facts; enforce their provenance on the server. Do not invent or silently normalize uncertain characters. If essential facts remain missing, stop before charge or dispatch.

Verify this through meaningful end-to-end tests with simulated providers and external effects: complete reader refusal, all fields unreadable, partial reading with corrections, conflicting characters such as O and zero, incomplete corrections, successful confirmation and continuation, and retries/reload. Verify the actual customer interface and server behavior together. Confirm no real payment or mail is sent. Preserve existing partial-reading behavior and add focused regression coverage.

Give me the exact changed files, test outcomes, current commit/worktree state, and any unresolved issue. Do not claim a live OCR or production fulfillment pass from simulated tests. Don’t run new paid calls, deploy, or commit/push unless separately authorized.

Because this chat session runs in a stateless web sandbox with no shell or filesystem attachment to agent-218922446017 (/home/ghostmonday/Desktop/ParkingBreaker-Codebase), I cannot write to your local disk or run pytest directly—you pasted that prompt from your local coding-agent instructions into this browser tab. However, I have inspected all the source files you uploaded (ticket_intake_service.py, ticket_fact_binding.py, ticket_vision.py, intake.ts, Step1Ticket.tsx, TicketFactReview.tsx, and the three test suites) and prepared both:

The exact audit of what is already implemented vs. what is still missing across the backend and frontend.

The complete, drop-in code patches (and a ready-to-paste prompt if you want your local terminal agent on agent-218922446017 to apply and run them automatically).

1. Audit: Already Implemented vs. Missing in 332b838
Already Implemented:

Server Provenance Separation (ticket_fact_binding.py): TicketFactBinding binds two distinct, immutable tuples: literal (the 11-field AI reading, where None marks not_shown or unreadable) and adopted (the 13-field customer-entered TicketFacts). binding.review_fields() returns only {key: value for key, value in self.literal if value is not None}.   
ticket_fact_binding.py

Partial Reading with Unreadable Fields (ticket_intake_service.py): When at least 1 of the 11 fields is shown (e.g., Case F where citation_number is unreadable/None but other fields are shown), binding.review_fields() is non-empty, request_review() compares only the shown literal fields, and the customer can supply the missing citation_number in adopted and confirm.   
deepseek-corrected-live-2026-11-16.json
+ 2

Submit & Mail Stops (ticket_intake_service.py, intake.ts): validateFactDraft() in the UI and TicketFactBinding.bind() / require_submission_confirmation() / revalidate_for_mailing() on the server block progression if any of the 11 required adopted fields (or explicit absence flags on the 2 optional dates) are missing, unconfirmed, or mutated.   
intake.ts
+ 2

Still Missing (The 4 Failure-Recovery Blockers):

Backend All-Null / Refused Reading Block (ticket_intake_service.py lines 489–491): In TicketIntakeCoordinator.request_review(), review_fields = binding.review_fields() is followed by if not review_fields: raise _error("ticket_facts_incomplete"). Whenever extraction returns all 11 fields as unreadable/not_shown (literal is all None), or when extraction refused / failed (unknown), binding.review_fields() is {}. Even after the customer types all 13 facts into adopted, request_review() throws ticket_facts_incomplete and blocks them from confirming!

Backend Empty-Review Confirmation Block (ticket_vision.py line 153 & ticket_intake_service.py): TicketReview.__post_init__ raises ValueError("a review must compare at least one critical field") if self.fields is empty (()) when refused=False. When all 11 literal fields are None (customer-supplied recovery after all-unreadable or refused/unknown extraction), request_review() has zero AI-extracted literal fields to send to reader.review(). It should record a zero-call, ClaimStatus.SUCCESS review claim with fields=() bound to current.images_digest and binding.closed_field_digest (0 provider calls, 0 cents spent) so the customer can confirm their 13 customer-supplied adopted facts without faking any literal AI extraction.

Frontend Hides <TicketFactReview/> on refused / unknown Reading (Step1Ticket.tsx lines 336–370, TicketFactReview.tsx line 206, intake.ts line 205):

literalFromReading() in intake.ts returns null when reading.fields is empty ([], which happens on status === "refused" or "unknown").   
intake.ts
+ 1

Step1Ticket.tsx only renders <TicketFactReview/> when vision.literal && vision.reading?.status === "success".   
Step1Ticket.tsx

TicketFactReview.tsx disables "Save facts and compare" unless reading.status === "success" (line 206) and disables "Confirm these ticket facts" when !hasReview (reviewFields.length === 0, line 261)!   
TicketFactReview.tsx

Customer Edit vs. AI literal Conflict (D Plate DEM0926 vs. DEMO926):

When extract() reads DEM0926 (literal.license_plate = "DEM0926"), and the customer corrects the form input to DEMO926 (adopted.license_plate = "DEMO926"), binding.review_fields() sends literal ("DEM0926") to reader.review(). If the review model agrees the image says "DEM0926", it returns comparison: "match", visible_text: "DEM0926".   
deepseek-corrected-live-2026-11-16.json
+ 3

Because _parse_review() in ticket_vision.py only checks _collapse(visible) != _collapse(confirmed[field.value]) against literal (DEM0926), the review item stays match even though the customer's adopted fact (DEMO926) conflicts with visible_text (DEM0926)!   
ticket_fact_binding.py
+ 2

Enforcing provenance on the server means: binding.literal stays strictly the AI's extract() output ("DEM0926" or None), binding.adopted stays strictly the customer's 13 facts ("DEMO926"), and in TicketIntakeCoordinator.request_review(), for any non-null literal field that has a corresponding text field in adopted (citation_number, license_plate, license_plate_state, issuer_name -> issuer_hint, issued_city, issued_state, violation_text -> violation_description, stage_text -> stage), if ReviewComparison.MATCH is returned by the reader against literal but _collapse(visible_text) != _collapse(adopted_value), the coordinator downgrades that field's comparison to ReviewComparison.CONFLICT so the customer must explicitly resolve CONFIRMED_VALUE_STANDS ("Keep the value I confirmed") before confirming!   
intake.ts
+ 3

2. Exact Code Changes Required (4 Files + 2 Test Files)
File 1: backend/src/parkingbreaker/ticket_intake_service.py
Add _ADOPTED_FOR_LITERAL mapping (for non-date text fields where adopted and literal are both literal strings, whereas dates in adopted are ISO YYYY-MM-DD and dates in literal are raw printed strings like 10/12/2026):

Python
_ADOPTED_TEXT_FOR_LITERAL: Final[Mapping[TicketField, str]] = {
    TicketField.CITATION_NUMBER: "citation_number",
    TicketField.LICENSE_PLATE: "license_plate",
    TicketField.LICENSE_PLATE_STATE: "license_plate_state",
    TicketField.ISSUER_NAME: "issuer_hint",
    TicketField.VIOLATION_TEXT: "violation_description",
    TicketField.ISSUED_CITY: "issued_city",
    TicketField.ISSUED_STATE: "issued_state",
}
(Note: stage in adopted can be a normalized label like "first-level review" while stage_text in literal is "NOTICE OF VIOLATION", and dates in adopted are ISO YYYY-MM-DD, so only the 7 direct text fields above compare visible_text directly against adopted when upgrading a literal match to CONFLICT.)

Allow Customer-Supplied Recovery in request_review() When extract Returned Refused / Unknown / All-Null (binding.review_fields() == {}):

Verify that an extraction attempt (IntakeOperation.EXTRACT) has already been claimed/recorded for this workspace (status in {ClaimStatus.SUCCESS, ClaimStatus.REFUSED, ClaimStatus.UNKNOWN}) so callers cannot skip request_reading entirely.

When review_fields = binding.review_fields() is {} (all 11 literal fields are None because extraction returned all not_shown/unreadable, or extraction was refused/unknown and the customer supplied all 13 adopted facts with an all-None literal projection):

Instead of raising ticket_facts_incomplete, claim IntakeOperation.REVIEW bound to review_confirmation_binding(current.images_digest, binding.closed_field_digest) and immediately complete it with ClaimStatus.SUCCESS, review.fields = (), Usage(0, 0, 0), and _review_result_json((), {}, binding, current.images_digest, refused=False, input_hash=..., synthetic=self._deps.synthetic) without calling the LLM provider.   
ticket_intake_service.py

When review_fields is non-empty and reader.review() succeeds:

Check each item in review.fields: if item.comparison is ReviewComparison.MATCH and item.field in _ADOPTED_TEXT_FOR_LITERAL:

Python
adopted_map = dict(binding.adopted)
adopted_key = _ADOPTED_TEXT_FOR_LITERAL.get(item.field)
if (
    item.comparison is ReviewComparison.MATCH
    and adopted_key is not None
    and item.visible_text is not None
    and " ".join(item.visible_text.split())
    != " ".join((adopted_map.get(adopted_key) or "").split())
):
    item = FieldReview(item.field, ReviewComparison.CONFLICT, item.visible_text)
This guarantees that on Case D (literal = DEM0926, customer adopted = DEMO926), even if the model says visible_text: "DEM0926" matches literal, the coordinator flags license_plate as conflict and requires CONFIRMED_VALUE_STANDS resolution!   
deepseek-corrected-live-2026-11-16.json
+ 3

Allow _parse_stored_review and _parse_review_record in ticket_intake_service.py to accept confirmed == {} and comparisons == {} when all 11 literal fields in binding.literal are None (customer-supplied recovery mode):

In _parse_stored_review(claim) and _parse_review_record(claim): if confirmed == {} and comparisons == {}, return (), False (or fields = () in _ReviewRecord) instead of calling parse_review({}, {}) (which returns None when confirmed is empty).   
ticket_intake_service.py
+ 1

File 2: web/lib/intake.ts
Add emptyTicketLiteral() and update literalFromReading() so that when reading is refused or unknown (where reading.fields is []), the UI can still construct an explicit all-null TicketLiteral (representing 0 AI-extracted fields) so the customer can enter all 13 adopted facts themselves:

TypeScript
export function emptyTicketLiteral(): TicketLiteral {
  return Object.fromEntries(TICKET_LITERAL_FIELD_ORDER.map((field) => [field, null])) as TicketLiteral;
}
In literalFromReading(fields: TicketReadingField[], allowEmpty = false): TicketLiteral | null: if allowEmpty && Array.isArray(fields) && fields.length === 0, return emptyTicketLiteral().   
intake.ts

File 3: web/components/Step1Ticket.tsx
Update readingStateText(reading) so that when reading.status === "refused" or "unknown", it tells the customer:

For "refused": "The reading service could not return usable suggestions from this photo. Enter the ticket facts yourself below, or upload a clearer photo."

For "unknown": "The reading service did not complete. Enter the ticket facts yourself below, or check the saved status."   
Step1Ticket.tsx

Remove the misleading paragraph "No comparison or confirmation is possible until a readable result is saved." when reading.status === "refused" || reading.status === "unknown", and instead render <TicketFactReview/> whenever:

TypeScript
const canEnterFacts =
  vision.reading?.status === "success" ||
  vision.reading?.status === "refused" ||
  vision.reading?.status === "unknown";
passing literal = vision.literal ?? emptyTicketLiteral().   
intake.ts
+ 1

When vision.reading?.status === "refused" && vision.reading.current, hide the redundant "Read my ticket photos" button (which would just replay the cached refusal for the same image hash) until the customer picks a new photo (!vision.reading.current).   
Step1Ticket.tsx
+ 1

File 4: web/components/TicketFactReview.tsx
Enable "Save facts and compare" (#visionReviewBtn) when reading?.status === "success" || reading?.status === "refused" || reading?.status === "unknown".

Support 0-AI-field customer-supplied confirmation:

Currently, line 101 sets const hasReview = reviewFields.length > 0; and line 261 disables #visionConfirmBtn when !hasReview.   
TicketFactReview.tsx

Change const hasReview = Boolean(review && review.status === "success" && review.current); so that when all 11 AI literal fields were null (reviewFields.length === 0, meaning 100% of facts are customer-supplied and 0 AI literal fields existed to compare), the review card displays:
"No AI-extracted text was available to compare. All ticket facts below are supplied by you. Confirm them to continue."
and enables #visionConfirmBtn ("Confirm these ticket facts")!   
TicketFactReview.tsx

3. One-Step Execution Command for Your Local Agent on agent-218922446017
If you want your local coding agent (which has write access to /home/ghostmonday/Desktop/ParkingBreaker-Codebase) to apply these exact changes and run the full backend + frontend test verification immediately, paste this exact instruction into your local terminal agent:

Plaintext
Apply the CARBON ticket-reading failure recovery specification across the 4 intake files and tests without running any paid calls, commits, pushes, or deployments:

1. In `backend/src/parkingbreaker/ticket_intake_service.py`:
   - Add `_ADOPTED_TEXT_FOR_LITERAL` mapping the 7 direct non-date literal fields (`citation_number`, `license_plate`, `license_plate_state`, `issuer_name` -> `issuer_hint`, `violation_text` -> `violation_description`, `issued_city`, `issued_state`) to their adopted keys.
   - In `TicketIntakeCoordinator.request_review()`:
     * Require that an `IntakeOperation.EXTRACT` claim exists for the workspace's reservation with status in `{ClaimStatus.SUCCESS, ClaimStatus.REFUSED, ClaimStatus.UNKNOWN}` and matching image digest (`input_binding_hash(EXTRACT_OPERATION, current.images)`) when `binding.review_fields()` is empty (`{}`).
     * When `binding.review_fields()` is `{}` (all 11 literal fields are `None`, which occurs on all-unreadable extraction or after a `refused`/`unknown` extraction), do NOT raise `ticket_facts_incomplete`. Instead, claim `IntakeOperation.REVIEW` for `binding_sha256` and complete it immediately as `ClaimStatus.SUCCESS` with `review_fields=()`, `confirmed={}`, `Usage(0, 0, 0)`, and zero provider calls.
     * In `_parse_stored_review` and `_parse_review_record`, when `confirmed == {}` and `comparisons == {}` and all values in `binding.literal` are `None`, return empty review fields `()` cleanly without raising `StorageCorruption`.
     * When `reader.review()` returns `ReviewComparison.MATCH` on a field in `_ADOPTED_TEXT_FOR_LITERAL`, compare `_collapse(item.visible_text)` against `_collapse(adopted_map[adopted_key])`; if they differ (e.g. AI literal `DEM0926` vs customer-corrected adopted `DEMO926`), downgrade `item` to `FieldReview(item.field, ReviewComparison.CONFLICT, item.visible_text)` so `confirm()` enforces `CONFIRMED_VALUE_STANDS`.

2. In `web/lib/intake.ts`:
   - Export `emptyTicketLiteral(): TicketLiteral` returning all 11 `TICKET_LITERAL_FIELD_ORDER` keys mapped to `null`.
   - Update `literalFromReading(fields: TicketReadingField[])` (or its caller in `web/`) so that a `refused` or `unknown` reading with `fields: []` produces `emptyTicketLiteral()` instead of `null`, while still returning `null` if `fields` is malformed.

3. In `web/components/Step1Ticket.tsx` and `web/components/TicketFactReview.tsx`:
   - Allow `<TicketFactReview/>` to render and submit when `vision.reading?.status` is `"success"`, `"refused"`, or `"unknown"`.
   - Hide `#visionReadBtn` when `vision.reading?.status === "refused" && vision.reading.current` so the user does not replay a cached refusal on the unchanged photo.
   - In `TicketFactReview.tsx`, when `review?.status === "success" && review.current` and `reviewFields.length === 0` (all-null literal recovery), render a clear provenance notice ("No AI-extracted fields were available to compare; all ticket facts are customer-supplied.") and enable `#visionConfirmBtn` (`!dirty && review.status === "success" && review.current`).

4. Add end-to-end simulated-provider tests in `backend/tests/test_ticket_intake_service.py`, `backend/tests/test_ticket_vision_composition.py`, and frontend tests covering:
   - Complete reader refusal (`status == "refused"`) -> customer supplies all 13 adopted facts with all-null literal -> `request_review` succeeds with 0 extra LLM calls -> `confirm` -> `submit` succeeds -> server provenance preserves all-null `literal` vs all-13 `adopted`.
   - Provider failure (`LlmProviderError` -> `status == "unknown"`) -> customer supplies all 13 facts -> `request_review` -> `confirm` -> `submit`.
   - All 11 fields `unreadable` (`status == "success"`, all values `None`) -> customer supplies all 13 facts -> `request_review` -> `confirm` -> `submit`.
   - Partial reading with customer correction and conflicting `0`/`O` (`DEM0926` in `literal` vs `DEMO926` in `adopted`) -> `request_review` downgrades false `literal` match to `conflict` -> requires `CONFIRMED_VALUE_STANDS` -> `confirm` -> `submit` preserves `literal="DEM0926"` and `adopted="DEMO926"`.
   - Incomplete corrections blocked before prehold/charge/mail.
   - Reload/restart idempotency and zero real provider/payment/mail calls.
If you would like me to print the full file-by-file unified git diff directly in this chat so you can git apply it yourself, say the word and I will output the complete patch.

