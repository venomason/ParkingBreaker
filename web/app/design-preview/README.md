# Parking Breaker brand preview

Local review of the approved off-white, deep green and gold direction. The shell is an editable SVG interpretation of the supplied brand reference; typography uses the repository's bundled Plus Jakarta Sans. No new dependencies or remote assets.

## Run locally

The existing CSP disallows the development server's eval-based JavaScript. Use a compiled preview without weakening CSP:

```sh
cd web
PB_PROVIDER_MODE=preview NEXT_PUBLIC_LIVE_API=0 PB_API_ORIGIN=http://127.0.0.1:9 npm run build
PB_DESIGN_PREVIEW=local PB_PROVIDER_MODE=preview NEXT_PUBLIC_LIVE_API=0 PB_API_ORIGIN=http://127.0.0.1:9 npm run start -- --hostname 127.0.0.1
```

Open `http://127.0.0.1:3001/design-preview`. The Rules and fictional walkthrough pages are linked from there. In production mode, all three routes return 404 unless `PB_DESIGN_PREVIEW=local` is explicitly set at runtime. Keep this flag out of deployment configuration.

## Scope

- Independent SVG shell plates, gold connections and light points; subtle pointer tilt and idle movement. This is vector depth styling, not a rotatable 3D mesh.
- Pause control, system reduced-motion support and pause when the document is hidden.
- One task at a time in a fictional walkthrough. Receipts and vector progress follow completed actions. Editing the example citation clears its confirmation. Exact characters are retained.
- The walkthrough uses only a fictional sample and browser session storage. It does not upload a photo, call OCR, create an appeal, authorize a payment or send mail.
- Existing production intake, authentication, recovery, payment and mail behavior are unchanged. Integrating this presentation into that flow requires separate end-to-end acceptance; the preview is not evidence of live OCR or fulfillment qualification.

The Rules page consolidates copy from `IntakeIntroduction`, `IntakeTrust`, `LegalDisclosure` and the existing Rules sheet. Actual service availability and the authoritative terms remain those shown by the real intake for the specific appeal.
