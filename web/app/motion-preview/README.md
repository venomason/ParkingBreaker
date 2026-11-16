# ParkingBreaker card motion study

Local review route: `/motion-preview`. This study uses the newly preferred natural olive shell still, on warm off-white. It preserves the real intake and the earlier design studies. It does not contain a new mascot drawing or use the rejected city-light shell.

## Preview boundary

- Compiled production starts require `PB_DESIGN_PREVIEW=local` to show this route; otherwise it returns 404. Search indexing is disabled.
- Use `PB_PROVIDER_MODE=preview NEXT_PUBLIC_LIVE_API=0 PB_API_ORIGIN=http://127.0.0.1:9` when building and serving locally.
- Three fictional tasks demonstrate the interaction only. No uploads, OCR, backend APIs, payment, mail, or fulfillment are called.
- Preview data is stored only in the current tab's session storage under `pb-motion-study-v1`. It is not a production provenance or confirmation gate.
- The Rules page copies the existing design-preview rules into this study. Those descriptions do not qualify the real service for release.
- No dependency was added; React, CSS transforms and opacity provide the transitions. The shell is optimized raster artwork, not an animated vector or a 3D asset.

## Motion decisions

- A single task card enters from below over 680ms, with its heading and content following at 160ms and 230ms. Exits take 300ms.
- Desktop completion moves the card toward the left history. Mobile completion moves upward toward compact completed-task markers. Future tasks are not listed.
- Editing confirmed facts removes their completion marker and downstream completion. The exact typed string is preserved, including O/0 differences. Confirmation must be renewed.
- No perpetual background animation runs while a customer enters facts.
- The motion switch persists per tab. OS reduced-motion takes precedence; all content remains visible and usable in the static state. Hidden tabs do not animate.
- This is a visual direction to review before applying it to the actual intake. Real intake integration must preserve the existing server-side recovery and provenance work and pass its separate end-to-end checks.

## Validation and remaining limits

Evidence: `/home/ghostmonday/Vault/Ops/runs/parkingbreaker-card-motion-20261116T192055Z/`.

The initial compiled study passed 30 browser checks (including four accessibility scans), 13 physical Android checks, and the Next.js build. Focused ESLint also passed. Tests exercise fictional data, completion markers, exact characters, incomplete entry, edits, reload, responsive layouts and reduced motion. They do not establish a live OCR, payment, mailing or beta release pass.

Android frame timing is recorded separately in `android-results.json` and `android-performance-isolated.json`. The first measurement included a large first-transition hitch; isolated repeats are more representative but still show occasional dropped frames. Do not claim universal 60fps performance or generalize this phone's result to every device. The final receipt identifies the exact file hashes tested.

## Independent Gemini review

Review only; do not edit this checkout. Assess the sequence at `/motion-preview`, propose precise timing/easing refinements, and critique desktop/mobile composition. Provide a short implementation brief, not a replacement component library. Separately propose a Rive rig for the ORIGINAL Hurtle artwork, preserving his face and proportions, with states for attention, reassurance, setbacks and a verified-dismissal celebration. Do not draw a replacement mascot, make paid calls, purchase tools, or deploy anything.
