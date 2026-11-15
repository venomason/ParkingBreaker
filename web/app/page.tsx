
"use client";

import { CERTIFIED_CENTS } from "../lib/prices";
import React, { useState, useEffect, useCallback, useRef, useMemo } from "react";

import { IntakeIntroduction } from "../components/IntakeIntroduction";
import { LegalDisclosure } from "../components/LegalDisclosure";
import { IntakeTrust } from "../components/IntakeTrust";
import { AccessLoading } from "../components/AccessLoading";
import { Header } from "../components/Header";
import { ActionBar } from "../components/ActionBar";
import { Receipts, Receipt } from "../components/Receipts";
import { StepJumper } from "../components/StepJumper";
import { DesktopAside } from "../components/DesktopAside";
import { BottomSheet } from "../components/BottomSheet";
import { SvgDefs, Icon } from "../components/Icons";

import { Step1Ticket, TicketField, type Step1VisionMode, type Step1VisionProps } from "../components/Step1Ticket";
import { Step2YourSide } from "../components/Step2YourSide";
import { Step3Research } from "../components/Step3Research";
import { Step4Offer } from "../components/Step4Offer";
import { Step4BetaOffer } from "../components/Step4BetaOffer";
import { Step5Window } from "../components/Step5Window";
import { Step6LetterStudio, SentenceItem, NoteItem } from "../components/Step6LetterStudio";
import { Step7Mailed } from "../components/Step7Mailed";
import { Step8Stop } from "../components/Step8Stop";
import { Step9Conflict } from "../components/Step9Conflict";
import { FactRow, FactItem } from "../components/FactRow";
import {
  ApiError,
  LIVE_FIELDS,
  SAMPLE_FACTS,
  SAMPLE_FIELDS,
  SAMPLE_PREVIEW,
  api,
  buildLetterData,
  isoDate,
} from "../lib/api";
import type { ApprovalRequest, PacketPreview, CaseStatus, Draft, Offer, Stop, Tracking, IntakeForm, BetaOffer, TicketDiscrepancyResolution, TicketReadingStatus, TicketReviewSnapshot, TicketFactBindingPayload, WorkspaceView, TicketFacts } from "../lib/api";
import { LiveDetails, LiveDetailsData, EMPTY_CONTACT, EvidencePhotoEntry, LiveContactValues } from "../components/LiveDetails";
import { LiveResearch, ResearchEvidence } from "../components/LiveResearch";
import { LiveLetter } from "../components/LiveLetter";
import { LiveFulfillment } from "../components/LiveFulfillment";
import { BetaNotices } from "../components/BetaNotices";
import { CARD_FORM_AVAILABLE, CardField, CardFieldHandle } from "../components/CardField";
import { CustomerGate, savedFundingKind, useServicePhase } from "../components/CustomerAccess";
import { PREVIEW_AUTH } from "../lib/api";
import { useWorkspaceAutosave } from "../lib/useWorkspaceAutosave";
import { emptyFactDraft, factDraftFromAdopted, factDraftFromReading, factDraftFromValues, literalFromReading, ticketFactsFromAdopted, ticketFormWithDraft, restoredTicketStep, validateFactDraft, type AdoptedFacts, type FactDraft, type TicketLiteral } from "../lib/intake";
import { customerStage, fulfillmentCopy, mailingConfirmed, offlinePaymentAllowed } from "../lib/fulfillment";

// The real flow against the API (fake, test or live providers behind it). Off: the prototype.
const LIVE = process.env.NEXT_PUBLIC_LIVE_API === "1";

function plainError(error: unknown): string {
  const detail = error instanceof ApiError ? error.detail : "request_failed";
  const known: Record<string, string> = {
    network_unavailable: "We couldn’t reach the service. Check your connection and try again.",
    unknown_photo_ref: "One of your photos didn't upload. Please add it again.",
    photo_not_readable: "That file could not be read and was not saved. Export a fresh JPEG or PNG, or take another clear photo of the paper original.",
    photo_type_not_supported: "That file was not uploaded. This beta accepts JPEG or PNG only. For a PDF or HEIC file, export a clear JPEG/PNG copy or photograph the paper original, keeping all printed details visible.",
    photo_size_invalid: "That file was not uploaded. Choose a non-empty JPEG or PNG up to 10 MB. Export a smaller image without making the printed details unreadable.",
    card_not_authorized: "The card wasn't authorized. Nothing was charged. Try another card.",
    choose_tier_first: "Choose how your letter should be mailed first.",
    card_form_unavailable: "The secure card form is unavailable. No authorization was requested. Please try again later.",
    packet_preview_mismatch: "The packet or envelope changed. Preview and inspect the current packet before approving it.",
    packet_not_ready: "The packet is missing a required item or signature. Check your saved details and signature before previewing again.",
    research_not_ready: "The saved agency check is incomplete. Review the agency requirements and deadline before continuing.",
    required_form_not_ready: "A required agency form is not ready for this packet. Mailing is blocked until the required form can be completed.",
    required_copies_not_ready: "A required supporting copy is missing. Complete the required copies section before previewing again.",
    declaration_unaffirmed: "Confirm every required declaration in the current preview before approving the packet.",
    deadline_not_feasible: "The verified deadline cannot be met by this mailing. Check the saved appeal status before continuing.",
    destination_verification_required: "Mailing is paused until the agency's appeal address is verified. Your saved address, consent and work are preserved; they do not authorize a new mailing.",
    approval_refused: "The packet does not yet meet the verified mailing requirements. Check your saved details before previewing again.",
    adopt_current_text_first: "The saved letter changed. Review and adopt the current exact text before previewing the packet.",
    case_not_approvable: "This appeal is not ready for packet approval. Check its saved status before continuing.",
    citation_photo_required: "Your ticket photo is missing. Add it before previewing the packet.",
    citation_back_photo_required: "Your saved back-of-ticket photo is unavailable. Upload it again or remove it before continuing.",
    evidence_photo_required: "A selected evidence photo is missing. Add it before previewing the packet.",
    unknown_signature_ref: "The saved signature is unavailable. Add your signature again before previewing the packet.",
    beta_entitlement_required: "This appeal is not part of the selected beta. Check the saved status before choosing how to mail.",
    beta_disclosure_changed: "The beta disclosure changed. Read the current text and agree again before accepting.",
    beta_qualification_expired: "This selected beta offer can no longer be accepted. Nothing was charged. Check the saved status.",
    beta_qualification_changed: "This appeal changed since the beta offer was made. Review the current offer before accepting.",
    invalid_beta_receipt: "The free beta acceptance could not be verified from the server. Nothing was charged. Check the saved status.",
    paid_orders_paused: "The server refused a new paid authorization in this controlled beta. No new card was charged and your saved appeal is unchanged. Check the saved status before continuing.",
    ticket_reading_disabled: "Ticket reading is not available for this appeal. Your saved work is unchanged.",
    ticket_form_required: "Add and save your ticket photos before this step.",
    ticket_photo_required: "Add a front photo of your ticket before reading it.",
    ticket_photo_not_found: "Your saved ticket photo is unavailable. Add it again.",
    ticket_photo_not_supported: "That file is not a readable JPEG or PNG photo. Choose another photo.",
    ticket_facts_required: "Confirm your ticket facts before comparing or continuing.",
    ticket_facts_incomplete: "The reading did not provide enough visible text to compare. Read your ticket photos again.",
    ticket_confirmation_required: "Confirm your ticket facts before submitting.",
    ticket_confirmation_stale: "Your ticket facts or photos changed. Compare and confirm them again before continuing.",
    ticket_confirmation_unavailable: "We couldn’t read the saved ticket confirmation. Keep this page open and retry; your work is saved.",
    confirmation_generation_stale: "Your ticket confirmation changed in another tab. Check the saved status and confirm again.",
    confirmation_command_reused: "That confirmation was already used for different facts. Check the saved status and confirm the current facts.",
    ticket_review_not_found: "No saved comparison was found. Save your ticket facts and compare again.",
    ticket_review_not_completed: "The saved comparison did not finish. Save your facts to compare again.",
    ticket_review_binding_mismatch: "The saved comparison no longer matches your ticket facts. Compare and confirm again.",
    ticket_reading_provider_unavailable: "The reading service is unavailable right now. Check the saved status before trying again.",
    ticket_review_provider_unavailable: "The comparison service is unavailable right now. Check the saved status before trying again.",
    ticket_reading_budget_exhausted: "The reading limit for this appeal is reached. No new reading was attempted; check the saved status or continue with the saved facts.",
    ticket_review_budget_exhausted: "The comparison limit for this appeal is reached. No new comparison was attempted; check the saved status.",
    workspace_changed_reload_required: "This appeal was changed in another tab. Reload the saved appeal before continuing.",
    request_id_reused_for_another_claim: "Your ticket photos changed since that request. Start a new reading for the current photos.",
    discrepancy_resolution_required: "Choose your resolution for every difference before confirming.",
    discrepancy_resolution_invalid: "That resolution does not match the saved comparison. Review each difference again.",
    discrepancy_resolution_unexpected: "Remove the resolution for any field that matched; only differences need a resolution.",
    intake_already_submitted: "This appeal was already submitted. Keep the saved appeal URL to check its status.",
  };
  return known[detail] ?? "We couldn’t confirm that action. Keep this page open and retry; check the saved status before repeating payment or mailing.";
}

// ─── Canvas PDF ──────────────────────────────────────────────────────────────

const PAGE_W = 612, PAGE_H = 792, PAGE_SCALE = 2;

function newPdfCanvas() {
  const c = document.createElement("canvas");
  c.width = PAGE_W * PAGE_SCALE; c.height = PAGE_H * PAGE_SCALE;
  const x = c.getContext("2d")!;
  x.scale(PAGE_SCALE, PAGE_SCALE);
  x.fillStyle = "#fff"; x.fillRect(0, 0, PAGE_W, PAGE_H);
  x.fillStyle = "#181818"; x.textBaseline = "top";
  return { canvas: c, ctx: x };
}

function pdfComposer() {
  const pages: HTMLCanvasElement[] = [];
  let current: ReturnType<typeof newPdfCanvas> | null = null;
  let y = 0;
  function page() { current = newPdfCanvas(); pages.push(current.canvas); y = 55; }
  function lines(text: string, font = "12px Georgia", gap = 17) {
    if (!current) page();
    const ctx = current!.ctx;
    ctx.font = font;
    for (const segment of String(text).split("\n")) {
      let row = "";
      for (const word of segment.split(/\s+/).filter(Boolean)) {
        const next = row ? row + " " + word : word;
        if (ctx.measureText(next).width > 500 && row) { if (y > 716) page(); current!.ctx.font = font; current!.ctx.fillText(row, 56, y); y += gap; row = word; }
        else row = next;
      }
      if (y > 716) page(); current!.ctx.font = font; current!.ctx.fillText(row, 56, y); y += gap;
    }
    y += 7;
  }
  function space(n = 9) { y += n; }
  return { pages, lines, space, page };
}

function pdfFromCanvases(canvases: HTMLCanvasElement[]): Blob {
  const objects: string[] = [null as unknown as string, null as unknown as string];
  const kids: string[] = [];
  canvases.forEach((canvas, i) => {
    const imageBytes = atob(canvas.toDataURL("image/jpeg", 0.92).split(",")[1]);
    const im = objects.length + 1;
    objects.push(`<< /Type /XObject /Subtype /Image /Width ${canvas.width} /Height ${canvas.height} /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length ${imageBytes.length} >>\nstream\n${imageBytes}\nendstream`);
    const content = `q ${PAGE_W} 0 0 ${PAGE_H} 0 0 cm /Im${i} Do Q`;
    const co = objects.length + 1;
    objects.push(`<< /Length ${content.length} >>\nstream\n${content}\nendstream`);
    const pa = objects.length + 1;
    objects.push(`<< /Type /Page /Parent 2 0 R /MediaBox [0 0 ${PAGE_W} ${PAGE_H}] /Resources << /XObject << /Im${i} ${im} 0 R >> >> /Contents ${co} 0 R >>`);
    kids.push(`${pa} 0 R`);
  });
  objects[0] = "<< /Type /Catalog /Pages 2 0 R >>";
  objects[1] = `<< /Type /Pages /Kids [${kids.join(" ")}] /Count ${kids.length} >>`;
  let pdf = "%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"; const offsets: number[] = [0];
  objects.forEach((body, i) => { offsets.push(pdf.length); pdf += `${i + 1} 0 obj\n${body}\nendobj\n`; });
  const xref = pdf.length;
  pdf += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`;
  offsets.slice(1).forEach((pos) => { pdf += `${String(pos).padStart(10, "0")} 00000 n \n`; });
  pdf += `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF`;
  return new Blob([Uint8Array.from(pdf, (c) => c.charCodeAt(0))], { type: "application/pdf" });
}

function composeLetterPages(sents: SentenceItem[], senderName: string, senderAddress: string, citNum: string): HTMLCanvasElement[] {
  const d = pdfComposer();
  d.lines(senderName + "\n" + senderAddress); d.space(11);
  d.lines(SAMPLE_PREVIEW.letterDate); d.space(10);
  d.lines(SAMPLE_PREVIEW.recipient); d.space(10);
  d.lines(`Re: Request for Administrative Review \u00b7 Citation ${citNum}`, "bold 12px Georgia"); d.space(8);
  const live = sents.filter((s) => !s.removed);
  [0, 1, 2].forEach((p) => { const t = live.filter((s) => s.p === p).map((s) => s.text).join(" "); if (t) d.lines(t); });
  d.space(13); d.lines("Sincerely,\n" + senderName);
  return d.pages;
}

// ─── Receipts helper ─────────────────────────────────────────────────────────

function buildReceipts(step: number, fields: TicketField[], photo: boolean | null, tier: "certified" | "standard" | null): Receipt[] {
  const fv = (k: string) => fields.find((f) => f.k === k)?.v ?? "";
  const price = CERTIFIED_CENTS / 100;
  const tierName = tier === "standard" ? "Standard Mail" : "Certified Mail";
  const list: Receipt[] = [];
  if (step > 1 && step !== 8) list.push({ stepNum: 1, stepName: "Ticket", summary: `\u2713 Ticket confirmed \u00b7 ${fv("agency").replace("City of ", "")} \u00b7 Oct 2` });
  if (step > 2 && step !== 8) list.push({ stepNum: 2, stepName: "Your side", summary: `\u2713 Your side recorded \u00b7 ${photo ? "1 photo saved" : "in your own words"}` });
  if (step > 3 && step !== 8 && step !== 9) list.push({ stepNum: 3, stepName: "Research", summary: "\u2713 8 facts confirmed, 1 unknown \u00b7 PO Box 129038" });
  if (step > 4 && step !== 8 && step !== 9) list.push({ stepNum: 4, stepName: "Mailing choice", summary: `\u2713 ${tierName} \u00b7 $${price} card hold authorized` });
  if (step > 6 && step !== 8 && step !== 9) list.push({ stepNum: 6, stepName: "Exact PDF", summary: "\u2713 Exact PDF approved for mailing" });
  return list;
}

function liveReceipts(step: number, fields: TicketField[], offer: Offer | null, beta: BetaOffer | null, status: CaseStatus | null, amountCents: number | null): Receipt[] {
  const cit = fields.find((f) => f.k === "cit")?.v ?? "";
  const list: Receipt[] = [];
  if (step === 8) return list;
  if (step > 1) list.push({ stepNum: 1, stepName: "Ticket", summary: `\u2713 Ticket confirmed \u00b7 ${cit}` });
  if (step > 2) list.push({ stepNum: 2, stepName: "Your side", summary: "\u2713 Your side and contact details saved" });
  if (beta && step >= 4) list.push({ stepNum: 4, stepName: "Selected beta", summary: `\u2713 Free beta \u00b7 $0 \u00b7 no card hold or payment${status?.payment_state === "NO_CHARGE" ? " \u00b7 accepted" : ""}` });
  if (offer && (step === 3 || step >= 5)) list.push({ stepNum: 4, stepName: "Agency and hold", summary: `${offer.issuer.issuer_name}${amountCents == null ? "" : ` · $${(amountCents / 100).toFixed(2)}`}${status?.payment_state ? ` · ${status.payment_state.toLowerCase().replaceAll("_", " ")}` : ""}` });
  const findings = status?.research?.findings ?? [];
  if (step >= 6 && findings.length) {
    const confirmed = findings.filter((f) => f.status === "CONFIRMED").length;
    const notApplicable = findings.filter((f) => f.status === "NOT_APPLICABLE").length;
    list.push({ stepNum: 3, stepName: "Official research", summary: `${confirmed} confirmed · ${notApplicable} not applicable · ${findings.length} checks` });
  }
  return list;
}

// ─── Main page component ──────────────────────────────────────────────────────

export default function Home() {
  if (process.env.NODE_ENV === "production" && !LIVE) return <main className="access-shell"><section className="access-card"><h1>Your appeal service is unavailable</h1><p>Please try again later.</p></section></main>;
  return LIVE && !PREVIEW_AUTH ? <CustomerGate><Journey /></CustomerGate> : <Journey />;
}

// Controlled/free or refused phase: no paid tier, card or authorization CTA.
function ControlledBetaWaiting({
  paused,
  admittedPaid,
  savedPending,
  phaseRefreshFailed,
  busy,
  onRetry,
  setActionBar,
}: {
  paused: boolean;
  admittedPaid: boolean;
  savedPending: boolean;
  phaseRefreshFailed: boolean;
  busy: boolean;
  onRetry: () => void;
  setActionBar: (sub: React.ReactNode, buttons: React.ReactNode) => void;
}) {
  const action = admittedPaid ? "Continue saved appeal" : paused ? "Check my appeal" : "Check my appeal";
  useEffect(() => {
    setActionBar(
      admittedPaid ? "Your existing payment stays the same" : "No new card or payment is requested here",
      <button className="btn btn-primary" type="button" onClick={onRetry} disabled={busy}>
        {busy ? "Checking…" : action}
      </button>
    );
  }, [busy, action, admittedPaid, onRetry, setActionBar]);
  const heading = admittedPaid
    ? "Your saved paid appeal is unchanged."
    : savedPending
      ? "We’re checking your existing payment."
      : paused
        ? "Paid ordering is paused for this appeal."
        : "This controlled beta has no new paid mailing option.";
  const lead = admittedPaid
    ? "This appeal already has an existing payment and mailing window on record. No new card or payment is requested; your saved work is unchanged. Use Check my appeal for the latest update."
    : savedPending
      ? "Do not authorize another payment. Use Check my appeal for the latest update."
      : paused
        ? "Paid orders are paused. No new payment was authorized. Your saved appeal and answers are unchanged. Use Check my appeal for the latest update before trying again."
        : "No new card or payment is requested here. This does not mean free mailing is approved. Your saved appeal, answers and existing work are unchanged. Use Check my appeal for the latest update on whether this appeal is already selected for the free beta or has an existing payment.";
  return (
    <section className="screen on" id="s4" aria-labelledby="h-s4">
      <div className="voice">
        <div>
          <p className="eyebrow">Step 3 · Mailing options paused</p>
          <h2 id="h-s4" tabIndex={-1}>{heading}</h2>
          <p className="lead" id="controlledBetaLead">{lead}</p>
        </div>
      </div>
      <div className="card">
        <p className="small" id="controlledBetaStatus">
          The server reports the controlled free beta for new paid orders. Your saved appeal may already
          hold a selected free beta or an earlier payment; any such saved record stays exactly as the
          server reports it. Refresh the server phase and use Check my appeal for the latest update before choosing anything.
        </p>
        {phaseRefreshFailed && <p className="small" role="status" id="phaseRefreshFailed">We couldn’t refresh the server phase. No price or card is shown; use Check my appeal for the latest update.</p>}
        <button className="btn btn-primary" type="button" onClick={onRetry} disabled={busy}>
          {busy ? "Checking…" : action}
        </button>
      </div>
    </section>
  );
}

function Journey() {
  // Local simulated flow, matching the prototype. Fixtures enter only through lib/api.
  const resolvedAccess = useServicePhase();
  // The prototype and fixed preview identity explicitly simulate the paid flow;
  // only a real server-resolved journey may leave phase/funding unresolved.
  const simulatedPaid = !LIVE || PREVIEW_AUTH;
  const phase = simulatedPaid ? "paid" as const : resolvedAccess.phase;
  const funding = simulatedPaid ? "paid" as const : resolvedAccess.funding;
  const refreshPricing = resolvedAccess.refreshPricing;
  const blockNewPaid = resolvedAccess.blockNewPaid;
  const newPaidBlocked = resolvedAccess.newPaidBlocked;
  const beginFundingRefresh = resolvedAccess.beginFundingRefresh;
  const pricingRefreshFailed = resolvedAccess.pricingRefreshFailed;
  const applyFunding = resolvedAccess.applyFunding;
  const [step, setStep] = useState(1);
  const [evidenceCount, setEvidenceCount] = useState(8);
  const [fields, setFields] = useState<TicketField[]>(LIVE ? LIVE_FIELDS : SAMPLE_FIELDS);
  const [plate, setPlate] = useState(LIVE ? "" : SAMPLE_PREVIEW.plate);
  const [plateState, setPlateState] = useState(LIVE ? "" : SAMPLE_PREVIEW.plateState);
  const [liveCase, setLiveCase] = useState<{ workspaceId: string; caseId: string } | null>(null);
  const [draftReady, setDraftReady] = useState(!LIVE || PREVIEW_AUTH);
  const [resumeUnavailable, setResumeUnavailable] = useState(false);
  const [contactValues, setContactValues] = useState<LiveContactValues>(EMPTY_CONTACT);
  const [photoFiles, setPhotoFiles] = useState<{ticket: File | null; back: File | null}>({ticket:null,back:null});
  const [photoRefs, setPhotoRefs] = useState<{ticket:string | null; back:string | null}>({ticket:null,back:null});
  const [evidencePhotos, setEvidencePhotos] = useState<EvidencePhotoEntry[]>([]);
  const [photoUploads, setPhotoUploads] = useState(0);
  const photoVersions = useRef({ticket:0,back:0});
  // Real ticket reading/review/confirmation path. The capability is discovered only
  // after this appeal's anonymous workspace exists; preview may stay legacy.
  const [workspaceId, setWorkspaceId] = useState<string | null>(null);
  const [visionMode, setVisionMode] = useState<Step1VisionMode>(LIVE && !PREVIEW_AUTH ? "unknown" : "legacy");
  const [visionStatus, setVisionStatus] = useState<TicketReadingStatus | null>(null);
  const [visionChecking, setVisionChecking] = useState(false);
  const [visionError, setVisionError] = useState<string | null>(null);
  const [ticketUploadError, setTicketUploadError] = useState<string | null>(null);
  const [visionBusy, setVisionBusy] = useState<"reading" | "review" | "confirm" | null>(null);
  const [visionDraft, setVisionDraft] = useState<FactDraft>(emptyFactDraft);
  const [visionLiteral, setVisionLiteral] = useState<TicketLiteral | null>(null);
  const [visionReview, setVisionReview] = useState<TicketReviewSnapshot | null>(null);
  const [visionResolutions, setVisionResolutions] = useState<Record<string, TicketDiscrepancyResolution>>({});
  const [visionConfirmed, setVisionConfirmed] = useState<{ commandId: string; reviewRequestId: string; generation: number } | null>(null);
  const [visionDirty, setVisionDirty] = useState(false);
  const visionReadingRequest = useRef<{ id: string; signature: string } | null>(null);
  const visionReviewRequest = useRef<{ id: string; signature: string } | null>(null);
  const visionCommandRequest = useRef<{ id: string; signature: string } | null>(null);
  const [visionBinding, setVisionBinding] = useState<TicketFactBindingPayload | null>(null);
  const [workspaceTicketReadingRequired, setWorkspaceTicketReadingRequired] = useState(false);
  const visionRequired = workspaceTicketReadingRequired || Boolean(visionStatus?.required);
  const [liveOffer, setLiveOffer] = useState<Offer | null>(null);
  const [liveBeta, setLiveBeta] = useState<BetaOffer | null>(null);
  const [betaError, setBetaError] = useState<string | null>(null);
  const [betaBusy, setBetaBusy] = useState(false);
  const [betaBlocked, setBetaBlocked] = useState(false);
  const liveCaseIdRef = useRef<string | null>(null);
  const [liveStop, setLiveStop] = useState<Stop | null>(null);
  const [liveBusy, setLiveBusy] = useState(false);
  const accessRequest = useRef<{ workspace: string; email: string; id: string } | null>(null);
  const restored = useRef(false);
  const cardRef = useRef<CardFieldHandle>(null);
  const [liveStatus, setLiveStatus] = useState<CaseStatus | null>(null);
  const [liveDraft, setLiveDraft] = useState<Draft | null>(null);
  const [liveLetterText, setLiveLetterText] = useState<string | null>(null);
  const pendingLetterEdit = useRef<{text: string; expected: string; replaceCustomerContent: boolean} | null>(null);
  const letterSaveInFlight = useRef(false);
  const [liveTracking, setLiveTracking] = useState<Tracking | null>(null);
  const [liveSubmissionPending, setLiveSubmissionPending] = useState(false);
  const [livePacketPreview, setLivePacketPreview] = useState<{ packet: PacketPreview; url: string; signatureRef?: string; caseId: string; adoptedSha256: string } | null>(null);
  const [liveApprovalUncertain, setLiveApprovalUncertain] = useState(false);
  const pendingPacketApproval = useRef<{ caseId: string; request: ApprovalRequest } | null>(null);
  const packetActionInFlight = useRef(false);
  const packetPreviewRevision = useRef(0);
  const livePacketUrl = useRef<string | null>(null);
  const invalidateLivePreview = useCallback(() => {
    packetPreviewRevision.current += 1;
    if (livePacketUrl.current) URL.revokeObjectURL(livePacketUrl.current);
    livePacketUrl.current = null;
    setLivePacketPreview(null);
  }, []);
  const [liveError, setLiveError] = useState<string | null>(null);
  // Set only after the server refuses a stale paid attempt with paid_orders_paused.
  const [paidPaused, setPaidPaused] = useState(false);
  const [backPhoto, setBackPhoto] = useState(false);
  const [story, setStory] = useState("");
  const [owner, setOwner] = useState<string | null>(null);
  const [photo, setPhoto] = useState<boolean | null>(null);
  const [tier, setTier] = useState<"certified" | "standard" | null>(null);
  const [liveAmount, setLiveAmount] = useState<number | null>(null);
  const [timerEnd, setTimerEnd] = useState<number | null>(null);
  const [lapsed, setLapsed] = useState(false);
  const [warned, setWarned] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const [run, setRun] = useState(0);
  const [traceMode, setTraceMode] = useState(false);
  const [conflictOptedIn, setConflictOptedIn] = useState(false);
  const [sents, setSents] = useState<SentenceItem[]>([]);
  const [notes, setNotes] = useState<NoteItem[]>([]);
  const [adopted, setAdopted] = useState(false);
  const [senderName, setSenderName] = useState(SAMPLE_PREVIEW.senderName);
  const [senderAddress, setSenderAddress] = useState(SAMPLE_PREVIEW.senderAddress);
  const [pdfPreview, setPdfPreview] = useState<{ pages: HTMLCanvasElement[]; url: string } | null>(null);
  const pdfPagesRef = useRef<HTMLCanvasElement[]>([]);
  const pdfBlobRef = useRef<Blob | null>(null);
  const pdfUrlRef = useRef<string | null>(null);
  const [actionSub, setActionSub] = useState<React.ReactNode>(null);
  const [actionButtons, setActionButtons] = useState<React.ReactNode>(null);
  const setActionBar = useCallback((sub: React.ReactNode, buttons: React.ReactNode) => { setActionSub(sub); setActionButtons(buttons); }, []);
  // Save an incomplete draft compactly; confirmed facts live only in the binding.
  const intakeForm = useMemo<IntakeForm>(() => ticketFormWithDraft({
    values: {
      ...Object.fromEntries(fields.map(field => [`field.${field.k}`, field.v])),
      ...Object.fromEntries(Object.entries(contactValues).map(([key,value]) => [`contact.${key}`,value])),
      plate, plateState, story, owner: owner ?? "", photo: photo === null ? "" : String(photo), backPhoto: String(backPhoto),
    },
    ticket_photo_ref: photoRefs.ticket,
    ticket_back_photo_ref: photoRefs.back,
    evidence_photo_refs: evidencePhotos.flatMap(entry => entry.ref ? [entry.ref] : []),
    step: step === 1 ? 1 : 2,
  }, visionDraft, visionBinding), [fields,contactValues,visionDraft,visionBinding,plate,plateState,story,owner,photo,backPhoto,photoRefs,evidencePhotos,step]);
  const { state: autosaveState, restore: restoreWorkspace, reset: resetWorkspace, ensureWorkspace, flush: flushWorkspace } = useWorkspaceAutosave(intakeForm, LIVE && !PREVIEW_AUTH && !liveCase, draftReady);
  const [sheetHow, setSheetHow] = useState(false);
  const [sheetFact, setSheetFact] = useState(false);
  const [sheetFactData, setSheetFactData] = useState<FactItem | null>(null);
  const [sheetChk, setSheetChk] = useState(false);
  const [sheetTerms, setSheetTerms] = useState(false);
  const [sheetSample, setSheetSample] = useState(false);
  const [sheetProv, setSheetProv] = useState(false);
  const [sheetProvSent, setSheetProvSent] = useState<SentenceItem | null>(null);
  const [sheetProvEditing, setSheetProvEditing] = useState(false);
  const [sheetNotes, setSheetNotes] = useState(false);
  const [sheetPdf, setSheetPdf] = useState(false);
  const [toast, setToastMsg] = useState<string | null>(null);
  const toastTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const showToast = useCallback((msg: string) => {
    setToastMsg(msg);
    if (toastTimerRef.current) clearTimeout(toastTimerRef.current);
    toastTimerRef.current = setTimeout(() => setToastMsg(null), 4600);
  }, []);

  useEffect(() => {
    const id = setInterval(() => {
      const currentTime = Date.now();
      setNow(currentTime);
      if (!timerEnd || lapsed) return;
      const remaining = timerEnd - currentTime;
      if (remaining <= 5 * 60 * 1000 && remaining > 0 && !warned) {
        setWarned(true); showToast(LIVE ? "Five minutes remain on the saved review window. Your work stays saved." : "Five minutes left in your window. If it closes, nothing is charged and your work stays saved.");
      }
      if (remaining <= 0) {
        invalidateLivePreview();
        setLapsed(true); showToast(LIVE ? "The saved review window has ended. Checking payment and mailing status keeps retries safe." : "Your window closed. No charge was made, and your work is saved.");
      }
    }, 1000);
    return () => clearInterval(id);
  }, [timerEnd, lapsed, warned, showToast, invalidateLivePreview]);

  const goTo = useCallback((n: number) => {
    if (n < 1 || n > 9) return;
    invalidateLivePreview();
    setActionBar(null, null);
    setRun((previous) => previous + 1);
    setNow(Date.now());
    if (n === 9) setConflictOptedIn(false);
    setSheetHow(false); setSheetFact(false); setSheetChk(false);
    setSheetTerms(false); setSheetSample(false); setSheetProv(false);
    setSheetNotes(false); setSheetPdf(false); setSheetProvEditing(false);
    if (!LIVE && n === 1) setFields((LIVE ? LIVE_FIELDS : SAMPLE_FIELDS).map((f) => ({ ...f })));
    if (!LIVE && n === 2) { setStory(""); setOwner(null); setPhoto(null); setSents([]); setAdopted(false); }
    if (n === 3) setEvidenceCount(0);
    if (n === 4) setTier(null);
    if (n === 7) setTier((previous) => previous ?? "certified");
    if (!LIVE && (n === 5 || n === 6)) {
      setTier((previous) => previous ?? "certified");
      setTimerEnd((previous) => previous ?? Date.now() + 30 * 60 * 1000);
    }
    setStep(n);
    if (!LIVE && n === 6 && sents.length === 0) { const { sents: s, notes: no } = buildLetterData(fields, plate, plateState, story, owner, photo); setSents(s); setNotes(no); setAdopted(false); }
    if (n === 7 || n === 8) { setTimerEnd(null); setLapsed(false); setWarned(false); }

  }, [fields, photo, owner, plate, plateState, sents.length, story, setActionBar, invalidateLivePreview]);

  useEffect(() => {
    const target = document.getElementById(`s${step}`);
    target?.scrollIntoView({ block: "start", behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth" });
    document.getElementById(`h-s${step}`)?.focus({ preventScroll: true });
  }, [step, run]);

  useEffect(() => {
    const viewport = window.visualViewport;
    const sync = () => {
      const offset = viewport ? Math.max(0, window.innerHeight - viewport.height) : 0;
      document.documentElement.style.setProperty("--kb-offset", offset > 80 ? `${offset}px` : "0px");
    };
    sync(); viewport?.addEventListener("resize", sync); viewport?.addEventListener("scroll", sync);
    return () => { viewport?.removeEventListener("resize", sync); viewport?.removeEventListener("scroll", sync); };
  }, []);

  useEffect(() => () => {
    packetPreviewRevision.current += 1;
    if (pdfUrlRef.current) URL.revokeObjectURL(pdfUrlRef.current);
    if (livePacketUrl.current) URL.revokeObjectURL(livePacketUrl.current);
    if (toastTimerRef.current) clearTimeout(toastTimerRef.current);
  }, []);

  useEffect(() => { liveCaseIdRef.current = liveCase?.caseId ?? null; }, [liveCase]);

  // Only the accepted contract's exact unselected refusal means "not beta".
  // Every other failure (auth, network, unknown, expired) must never imply paid.
  const fetchBetaOffer = useCallback(async (caseId: string): Promise<BetaOffer | null> => {
    try {
      return await api.betaOffer(caseId);
    } catch (error) {
      if (error instanceof ApiError && error.status === 409 && error.detail === "beta_entitlement_required") return null;
      throw error;
    }
  }, []);

  const completeResearch = useCallback(() => goTo(4), [goTo]);
  const backToResearch = useCallback(() => goTo(3), [goTo]);
  const openConflict = useCallback(() => goTo(9), [goTo]);
  const reopenStopDetails = useCallback(async () => {
    if (LIVE && !PREVIEW_AUTH) {
      try { await api.startAppeal(); } catch { /* createWorkspace retries startAppeal if needed */ }
    }
    resetWorkspace();
    setPhotoRefs({ ticket: null, back: null });
    setEvidencePhotos((prev) => prev.map((entry) => ({ ...entry, ref: null })));
    setLiveStop(null);
    setLiveCase(null);
    setLiveStatus(null);
    setLiveOffer(null);
    setLiveBeta(null);
    setLiveError(null);
    if (LIVE && typeof window !== "undefined") {
      window.history.replaceState(null, "", window.location.pathname);
    }
    goTo(2);
  }, [resetWorkspace, goTo]);
  const startNewFromStop = useCallback(async () => {
    if (LIVE && !PREVIEW_AUTH) {
      try { await api.startAppeal(); } catch { /* createWorkspace retries startAppeal if needed */ }
    }
    resetWorkspace();
    setPhotoRefs({ ticket: null, back: null });
    setEvidencePhotos((prev) => prev.map((entry) => ({ ...entry, ref: null })));
    setLiveStop(null);
    setLiveCase(null);
    setLiveStatus(null);
    setLiveOffer(null);
    setLiveBeta(null);
    setLiveError(null);
    if (LIVE && typeof window !== "undefined") {
      window.history.replaceState(null, "", window.location.pathname);
    }
    goTo(1);
  }, [resetWorkspace, goTo]);
  const openNotes = useCallback(() => setSheetNotes(true), []);
  const openTerms = useCallback(() => setSheetTerms(true), []);
  const openSample = useCallback(() => setSheetSample(true), []);
  const openRules = useCallback(() => setSheetHow(true), []);
  const openChecklist = useCallback(() => setSheetChk(true), []);
  const copyNotes = useCallback(async () => {
    if (!LIVE) { showToast("Prototype: your saved notes would be copied to your clipboard."); return; }
    try { await navigator.clipboard.writeText(liveDraft?.text ?? story); showToast("Your saved words were copied."); }
    catch { showToast("Copy is unavailable. Select and copy your own words from the saved account below."); }
  }, [story, liveDraft, showToast]);
  const openAgencyForm = useCallback(() => showToast("Prototype: this would open the agency's official form page."), [showToast]);

  const handleStep1Complete = useCallback((data: { fields: TicketField[]; plate: string; plateState: string; backPhoto: boolean }) => {
    setFields(data.fields); setPlate(data.plate); setPlateState(data.plateState); setBackPhoto(data.backPhoto); goTo(2);
  }, [goTo]);

  const handleStep2Complete = useCallback((data: { story: string; owner: string; photo: boolean }) => {
    setStory(data.story); setOwner(data.owner); setPhoto(data.photo); goTo(3);
  }, [goTo]);

  // ─── Real ticket reading, review and confirmation ─────────────────────────────
  const visionPhotoSignatureFor = useCallback((view?: WorkspaceView | null) => {
    if (view) return `${view.intake_form?.ticket_photo_ref ?? ""}|${view.intake_form?.ticket_back_photo_ref ?? ""}`;
    return `${photoRefs.ticket ?? ""}|${photoRefs.back ?? ""}`;
  }, [photoRefs]);

  const visionBindingSignatureFor = useCallback((binding: TicketFactBindingPayload | null, view?: WorkspaceView | null) =>
    `${JSON.stringify([binding?.literal ?? null, binding?.adopted ?? null])}|${visionPhotoSignatureFor(view)}`, [visionPhotoSignatureFor]);

  const restoreVisionForm = useCallback((view: WorkspaceView) => {
    const form = view.intake_form;
    const binding = form?.ticket_fact_binding ?? null;
    setVisionBinding(binding);
    if (binding) {
      setVisionLiteral(binding.literal as TicketLiteral);
      setVisionDraft(factDraftFromAdopted(binding.adopted));
      // The confirmed ticket facts also fill the Step 2 issued-place summary.
      setContactValues((previous) => ({
        ...previous,
        issuedCity: typeof binding.adopted.issued_city === "string" ? binding.adopted.issued_city : previous.issuedCity,
        issuedState: typeof binding.adopted.issued_state === "string" ? binding.adopted.issued_state : previous.issuedState,
        issuerKind: binding.adopted.issuer_kind === "PRIVATE" ? "PRIVATE" : "GOVERNMENT",
      }));
    } else if (form) {
      setVisionDraft(factDraftFromValues(form.values ?? {}));
    }
  }, []);

  const applyRestoredVision = useCallback((view: WorkspaceView, status: TicketReadingStatus) => {
    setWorkspaceId(view.workspace_id);
    setVisionStatus(status);
    setVisionMode(view.ticket_reading_required || status.enabled ? "vision" : "legacy");
    setVisionError(null);
    setVisionDirty(false);
    const binding = view.intake_form?.ticket_fact_binding ?? null;
    if (status.extraction.status === "success") {
      const literal = literalFromReading(status.extraction.fields);
      if (literal) setVisionLiteral(literal);
    }
    visionReadingRequest.current = status.extraction.request_id
      ? { id: status.extraction.request_id, signature: visionPhotoSignatureFor(view) }
      : null;
    visionReviewRequest.current = status.review.request_id
      ? { id: status.review.request_id, signature: visionBindingSignatureFor(binding, view) }
      : null;
    setVisionReview(status.review.request_id ? status.review : null);
    if (status.confirmation.state === "current" && status.confirmation.command_id) {
      setVisionConfirmed({
        commandId: status.confirmation.command_id,
        reviewRequestId: status.confirmation.review_request_id ?? status.review.request_id ?? "",
        generation: status.confirmation.generation,
      });
      visionCommandRequest.current = {
        id: status.confirmation.command_id,
        signature: `${status.confirmation.review_request_id ?? ""}|${visionBindingSignatureFor(binding, view)}`,
      };
    } else {
      setVisionConfirmed(null);
      visionCommandRequest.current = null;
    }
  }, [visionBindingSignatureFor, visionPhotoSignatureFor]);

  const discoverVision = useCallback(async (existing?: WorkspaceView): Promise<{ view: WorkspaceView; status: TicketReadingStatus } | null> => {
    if (!LIVE || PREVIEW_AUTH) { setVisionMode("legacy"); return null; }
    setVisionChecking(true); setVisionError(null);
    try {
      const view = existing ?? await ensureWorkspace();
      setWorkspaceId(view.workspace_id);
      setWorkspaceTicketReadingRequired(Boolean(view.ticket_reading_required));
      if (view.ticket_reading_required) setVisionMode("vision");
      // Restore saved input even when the separate status request is unavailable.
      restoreVisionForm(view);
      setVisionConfirmed(null);
      const status = await api.ticketReadingStatus(view.workspace_id);
      applyRestoredVision(view, status);
      return { view, status };
    } catch (error) {
      setVisionError(plainError(error));
      return null;
    } finally {
      setVisionChecking(false);
    }
  }, [ensureWorkspace, applyRestoredVision, restoreVisionForm]);

  const refreshVisionStatusOnly = useCallback(async (workspace?: string): Promise<TicketReadingStatus | null> => {
    const id = workspace ?? workspaceId;
    if (!id) return null;
    const view = await api.workspace(id);
    const status = await api.ticketReadingStatus(id);
    setVisionStatus(status);
    setVisionMode(view.ticket_reading_required || status.enabled ? "vision" : "legacy");
    setWorkspaceTicketReadingRequired(Boolean(view.ticket_reading_required));
    if (status.extraction.status === "success") {
      const literal = literalFromReading(status.extraction.fields);
      if (literal) setVisionLiteral(literal);
    }
    setVisionReview(status.review.request_id ? status.review : null);
    if (status.confirmation.state === "current" && status.confirmation.command_id) {
      setVisionConfirmed({
        commandId: status.confirmation.command_id,
        reviewRequestId: status.confirmation.review_request_id ?? "",
        generation: status.confirmation.generation,
      });
    } else {
      setVisionConfirmed(null);
    }
    setVisionBinding(view.intake_form?.ticket_fact_binding ?? null);
    return status;
  }, [workspaceId]);

  const flushVisionForm = useCallback(async (overrides: Partial<IntakeForm>): Promise<WorkspaceView> => {
    const form = ticketFormWithDraft({
      ...intakeForm,
      ...overrides,
    }, visionDraft, overrides.ticket_fact_binding ?? visionBinding);
    let view: WorkspaceView;
    try {
      const saved = await flushWorkspace(form);
      if (!saved) throw new ApiError(503, "workspace_unavailable");
      view = await api.workspace(saved.workspace_id);
      restoreWorkspace(view);
    } catch (error) {
      if (!(error instanceof ApiError) || error.status !== 409) throw error;
      const id = workspaceId ?? (await ensureWorkspace()).workspace_id;
      const latest = await api.workspace(id);
      view = await api.saveIntakeForm(id, latest.draft_revision ?? 1, form);
      restoreWorkspace(view);
    }
    setWorkspaceId(view.workspace_id);
    return view;
  }, [intakeForm, visionDraft, visionBinding, flushWorkspace, restoreWorkspace, workspaceId, ensureWorkspace]);

  const updateVisionDraft = useCallback((next: FactDraft) => {
    const hadReview = Boolean(visionReview) || Boolean(visionConfirmed);
    setVisionDraft(next);
    setVisionBinding(null);
    setVisionReview(null); setVisionConfirmed(null); setVisionResolutions({});
    visionReviewRequest.current = null; visionCommandRequest.current = null;
    if (hadReview) setVisionDirty(true);
  }, [visionReview, visionConfirmed]);

  const invalidateVisionPhotos = useCallback(() => {
    const hadReview = Boolean(visionReview) || Boolean(visionConfirmed);
    setVisionLiteral(null);
    setVisionBinding(null);
    setVisionReview(null); setVisionConfirmed(null); setVisionResolutions({});
    visionReadingRequest.current = null;
    visionReviewRequest.current = null; visionCommandRequest.current = null;
    setVisionStatus((previous) => previous ? { ...previous, extraction: { ...previous.extraction, current: false } } : previous);
    if (hadReview) setVisionDirty(true);
  }, [visionReview, visionConfirmed]);

  const readTicketVision = useCallback(async () => {
    if (!LIVE || PREVIEW_AUTH || visionBusy) return;
    setVisionError(null); setVisionBusy("reading");
    try {
      const view = await flushVisionForm({
        ticket_photo_ref: photoRefs.ticket,
        ticket_back_photo_ref: photoRefs.back,
      });
      const form = view.intake_form;
      if (!form?.ticket_photo_ref) throw new ApiError(409, "ticket_photo_required");
      const signature = visionPhotoSignatureFor(view);
      const existing = visionReadingRequest.current;
      // A lost response replays the same request id; changed photos start a new one.
      const requestId = existing && existing.signature === signature ? existing.id : crypto.randomUUID();
      visionReadingRequest.current = { id: requestId, signature };
      const snapshot = await api.requestTicketReading(view.workspace_id, requestId, view.draft_revision ?? 1);
      setVisionStatus((previous) => previous ? { ...previous, extraction: snapshot } : previous);
      if (snapshot.status === "success") {
        const literal = literalFromReading(snapshot.fields);
        if (literal) {
          setVisionLiteral(literal);
          setVisionDraft((previous) => factDraftFromReading(snapshot.fields, previous));
        } else {
          setVisionLiteral(null);
          setVisionError("The reading did not return a complete set of suggestions. Start a new reading.");
        }
      } else if (snapshot.status === "refused") {
        setVisionLiteral(null);
        setVisionError("The reading service could not return usable suggestions. Start a new reading with clearer photos.");
      }
    } catch (error) {
      setVisionError(plainError(error));
    } finally {
      setVisionBusy(null);
    }
  }, [visionBusy, flushVisionForm, photoRefs, visionPhotoSignatureFor]);

  const saveAndReviewVision = useCallback(async (facts: AdoptedFacts) => {
    if (!LIVE || PREVIEW_AUTH || visionBusy) return;
    if (!visionLiteral) { setVisionError("Read your ticket photos before comparing."); return; }
    setVisionError(null); setVisionBusy("review");
    try {
      const binding: TicketFactBindingPayload = {
        literal: {
          citation_number: visionLiteral.citation_number ?? facts.citation_number,
          license_plate: visionLiteral.license_plate ?? facts.license_plate,
          license_plate_state: visionLiteral.license_plate_state ?? facts.license_plate_state,
          issuer_name: visionLiteral.issuer_name ?? facts.issuer_hint,
          issue_date: visionLiteral.issue_date,
          notice_date: visionLiteral.notice_date,
          printed_deadline: visionLiteral.printed_deadline,
          violation_text: visionLiteral.violation_text ?? facts.violation_description,
          issued_city: visionLiteral.issued_city ?? facts.issued_city,
          issued_state: visionLiteral.issued_state ?? facts.issued_state,
          stage_text: visionLiteral.stage_text,
        },
        adopted: {
          citation_number: facts.citation_number,
          license_plate: facts.license_plate,
          license_plate_state: facts.license_plate_state,
          issuer_hint: facts.issuer_hint,
          issued_city: facts.issued_city,
          issued_state: facts.issued_state,
          issuer_kind: facts.issuer_kind,
          violation_code: facts.violation_code,
          violation_description: facts.violation_description,
          issue_date: facts.issue_date,
          stage: facts.stage,
          notice_date: facts.notice_date,
          printed_deadline: facts.printed_deadline,
        },
      };
      setVisionBinding(binding);
      const view = await flushVisionForm({
        ticket_fact_binding: binding,
      });
      const canonical = view.intake_form?.ticket_fact_binding ?? binding;
      setVisionBinding(canonical);
      const signature = visionBindingSignatureFor(canonical, view);
      const existing = visionReviewRequest.current;
      // The same saved envelope replays the same request id after a lost response.
      const requestId = existing && existing.signature === signature ? existing.id : crypto.randomUUID();
      visionReviewRequest.current = { id: requestId, signature };
      const snapshot = await api.requestTicketReview(view.workspace_id, requestId, view.draft_revision ?? 1);
      setVisionReview(snapshot);
      setVisionStatus((previous) => previous ? { ...previous, review: snapshot } : previous);
      setVisionResolutions({});
      if (snapshot.status === "success") {
        setVisionDirty(false);
      } else if (snapshot.status === "refused") {
        setVisionError("The comparison service could not return usable results. Save your facts to start a fresh comparison.");
      }
    } catch (error) {
      setVisionError(plainError(error));
    } finally {
      setVisionBusy(null);
    }
  }, [visionBusy, visionLiteral, flushVisionForm, visionBindingSignatureFor]);

  const confirmTicketVision = useCallback(async () => {
    if (!LIVE || PREVIEW_AUTH || visionBusy || !visionReview) return;
    const reviewRequestId = visionReview.request_id ?? visionReviewRequest.current?.id;
    if (!reviewRequestId || visionReview.status !== "success" || !visionReview.current) {
      setVisionError("Save your facts and get a current comparison before confirming.");
      return;
    }
    const resolved: Record<string, TicketDiscrepancyResolution> = {};
    for (const field of visionReview.fields) {
      if (field.comparison === "match") continue;
      const expected = field.comparison === "conflict" ? "CONFIRMED_VALUE_STANDS" : field.comparison === "not_shown" ? "ABSENCE_ACKNOWLEDGED" : "UNREADABLE_ACKNOWLEDGED";
      if (visionResolutions[field.field] !== expected) {
        setVisionError("Choose your resolution for every difference before confirming.");
        return;
      }
      resolved[field.field] = expected;
    }
    setVisionError(null); setVisionBusy("confirm");
    try {
      const facts = validateFactDraft(visionDraft).facts;
      if (!facts) { setVisionError("Check your ticket facts before confirming."); return; }
      const view = await flushVisionForm({
        ticket_fact_binding: visionBinding ?? undefined,
      });
      // Observe the actual previous generation; never send an assumed next value.
      const observed = await api.ticketReadingStatus(view.workspace_id);
      setVisionStatus(observed);
      const generation = observed.confirmation?.generation ?? 0;
      const signature = `${reviewRequestId}|${visionBindingSignatureFor(visionBinding, view)}|${JSON.stringify(resolved)}`;
      const existing = visionCommandRequest.current;
      // The same command id repeats this exact confirmation after a lost response.
      const commandId = existing && existing.signature === signature ? existing.id : crypto.randomUUID();
      visionCommandRequest.current = { id: commandId, signature };
      const receipt = await api.confirmTicketReading(view.workspace_id, {
        commandId,
        expectedRevision: view.draft_revision ?? 1,
        reviewRequestId,
        expectedConfirmationGeneration: generation,
        resolvedFields: resolved,
      });
      setVisionConfirmed({ commandId: receipt.command_id, reviewRequestId: receipt.review_request_id, generation: receipt.generation });
      setVisionResolutions({});
      setVisionDirty(false);
      visionCommandRequest.current = null;
      setVisionStatus((previous) => previous ? {
        ...previous,
        confirmation: {
          state: "current",
          command_id: receipt.command_id,
          review_request_id: receipt.review_request_id,
          generation: receipt.generation,
          discrepancy_status: receipt.discrepancy_status,
          reason: null,
        },
        next_confirmation_generation: receipt.generation,
      } : previous);
      showToast("Ticket facts confirmed for these exact photos.");
    } catch (error) {
      setVisionError(plainError(error));
    } finally {
      setVisionBusy(null);
    }
  }, [visionBusy, visionReview, visionResolutions, visionDraft, visionBinding, flushVisionForm, visionBindingSignatureFor, showToast]);

  const handleVisionContinue = useCallback((facts: AdoptedFacts) => {
    if (!visionConfirmed || visionDirty) return;
    const mapped: Record<string, string> = {
      cit: facts.citation_number,
      date: facts.issue_date,
      vio: facts.violation_description,
      agency: facts.issuer_hint,
    };
    const nextFields = LIVE_FIELDS.map((field) => ({ ...field, v: mapped[field.k] ?? "", t: (mapped[field.k] ?? "").toUpperCase() }));
    setFields(nextFields);
    setPlate(facts.license_plate);
    setPlateState(facts.license_plate_state);
    setBackPhoto(Boolean(photoRefs.back));
    setContactValues((previous) => ({ ...previous, issuedCity: facts.issued_city, issuedState: facts.issued_state, issuerKind: facts.issuer_kind }));
    goTo(2);
  }, [visionConfirmed, visionDirty, photoRefs.back, goTo]);

  const stopFromStatus = useCallback((status: CaseStatus) => {
    if (status.state === "STOPPED_NO_CHARGE" && status.stop) { setLiveStop(status.stop); goTo(8); return true; }
    return false;
  }, [goTo]);

  // Apply the authoritative saved case status without discarding an original
  // saved window/amount when the server response omits it.
  const applySavedStatus = useCallback((saved: CaseStatus) => {
    setLiveStatus(saved);
    if (saved.amount_cents != null) setLiveAmount(saved.amount_cents);
    if (saved.seconds_remaining != null) {
      setTimerEnd(Date.now() + saved.seconds_remaining * 1000);
    } else if (!saved.window_started_at) {
      // Only the server can confirm that this case has no saved window.
      setTimerEnd(null);
    }
    if (saved.window_status === "EXPIRED") setLapsed(true);
    stopFromStatus(saved);
  }, [stopFromStatus]);

  // Single current-case refresh shared by the stale-409 transition and the
  // waiting button. It rereads the immutable server phase (uncached, using the
  // existing endpoint), the saved status and the case's offer. It never accepts
  // beta, authorizes, creates a card/replacement key or reloads the Journey.
  const refreshCurrentCase = useCallback(async (): Promise<boolean> => {
    if (!LIVE || !liveCase) return false;
    const caseId = liveCase.caseId;
    setLiveBusy(true); setLiveError(null);
    beginFundingRefresh();
    let phaseOk = false;
    try {
      phaseOk = await refreshPricing();
      const saved = await api.status(caseId);
      if (liveCaseIdRef.current !== caseId) return false;
      applySavedStatus(saved);
      let offer: BetaOffer | null | undefined;
      try {
        offer = await api.betaOffer(caseId);
      } catch (error) {
        // The exact unselected refusal is ordinary; any other failure must not
        // be read as paid or as an entitlement to a new card.
        if (!(error instanceof ApiError && error.status === 409 && error.detail === "beta_entitlement_required")) {
          setLiveError(plainError(error));
        } else {
          offer = null;
        }
      }
      if (liveCaseIdRef.current !== caseId) return false;
      // Shared funding is fed only by these authenticated reads, so the paid
      // header/card charge text reacts in place without a child unmount.
      applyFunding(saved, offer);
      if (offer) {
        setLiveBeta(offer);
        setBetaBlocked(false);
        if (offer.accepted_at) { setLiveAmount(0); setTier("certified"); }
      } else {
        // Drop the stale paid offer so no card affordance can remount.
        setLiveBeta(null);
        setLiveOffer(null);
        setTier(null);
      }
      // Resume this case's own saved stage instead of mounting a new checkout.
      // Stage 6/7 need their owned evidence fetched first; every read is guarded
      // so a stale response for a previous case can never mount over this one.
      if (saved.window_started_at || saved.state !== "INTAKE") {
        const stage = customerStage(saved);
        if (stage === 6) {
          const draft = await api.draft(caseId);
          if (liveCaseIdRef.current !== caseId) return false;
          setLiveDraft(draft);
          // Initialize the editable copy only when the customer has not typed
          // newer text; unsaved edits always win over a refreshed draft.
          setLiveLetterText((previous) => previous ?? draft.text);
        } else if (stage === 7) {
          try {
            const tracking = await api.tracking(caseId);
            if (liveCaseIdRef.current !== caseId) return false;
            setLiveTracking(tracking);
          } catch {
            if (liveCaseIdRef.current !== caseId) return false;
            setLiveError("The saved mailing is here, but postal updates are unavailable. Check mailing status to retry.");
          }
        }
        if (liveCaseIdRef.current !== caseId) return false;
        goTo(stage);
      }
    } catch (error) {
      if (liveCaseIdRef.current === caseId) { applyFunding(null, undefined); setLiveError(plainError(error)); }
    } finally {
      setLiveBusy(false);
    }
    return phaseOk;
  }, [liveCase, refreshPricing, beginFundingRefresh, applyFunding, applySavedStatus, goTo]);

  const handleStep4Complete = useCallback(async (selectedTier: "certified" | "standard") => {
    if (LIVE) {
      if (!liveCase || newPaidBlocked || phase !== "paid") return;
      const caseId = liveCase.caseId;
      setLiveError(null);
      try {
        await api.chooseTier(caseId, selectedTier === "standard" ? "STANDARD" : "CERTIFIED");
        let paymentMethod: string;
        if (CARD_FORM_AVAILABLE && cardRef.current) {
          paymentMethod = await cardRef.current.paymentMethod();
        } else if (offlinePaymentAllowed(
          (await api.health()).mode,
          process.env.NEXT_PUBLIC_OFFLINE_FIXTURES === "1",
          process.env.NODE_ENV === "development",
        )) {
          paymentMethod = "pm_card_visa";
        } else {
          throw new ApiError(503, "card_form_unavailable");
        }
        const auth = await api.authorize(caseId, paymentMethod);
        setTier(selectedTier);
        setLiveAmount(auth.amount_cents);
        const saved = await api.status(caseId);
        setLiveStatus(saved);
        setTimerEnd(saved.seconds_remaining == null ? null : Date.now() + saved.seconds_remaining * 1000);
        setWarned(false); setLapsed(false);
        goTo(5);
      } catch (error) {
        // The server gate refused a stale paid attempt. Remove every new-paid
        // affordance immediately (header, sidebar, card), then reread the actual
        // server phase/status/offer in place. The mounted Journey and unsaved
        // edits stay; no new payment method or replacement key is created.
        if (error instanceof ApiError && error.status === 409 && error.detail === "paid_orders_paused") {
          setPaidPaused(true);
          blockNewPaid();
          setLiveOffer(null);
          setTier(null);
          await refreshCurrentCase();
          return;
        }
        setLiveError(plainError(error)); showToast(plainError(error));
      }
      return;
    }
    setTier(selectedTier);
    if (!timerEnd) { setTimerEnd(Date.now() + 30 * 60 * 1000); setWarned(false); setLapsed(false); }
    goTo(5);
  }, [goTo, timerEnd, liveCase, showToast, blockNewPaid, refreshCurrentCase, newPaidBlocked, phase]);

  const handleBetaAccept = useCallback(async () => {
    if (!LIVE || !liveCase || !liveBeta || betaBusy) return;
    const caseId = liveCase.caseId;
    const offer = liveBeta;
    setBetaBusy(true); setBetaError(null); setLiveError(null); setBetaBlocked(false);
    try {
      const receipt = await api.acceptBeta(caseId, offer.disclosure_sha256);
      if (receipt.case_id !== caseId || receipt.funding !== "NO_CHARGE" || receipt.amount_cents !== 0 || receipt.tier !== "CERTIFIED") {
        throw new ApiError(502, "invalid_beta_receipt");
      }
      const saved = await api.status(caseId);
      if (liveCaseIdRef.current !== caseId) return;
      setLiveStatus(saved);
      setLiveAmount(0);
      setTier("certified");
      setLiveBeta({ ...offer, accepted_at: receipt.accepted_at });
      setTimerEnd(saved.seconds_remaining == null ? null : Date.now() + saved.seconds_remaining * 1000);
      setWarned(false); setLapsed(saved.window_status === "EXPIRED");
      goTo(5);
    } catch (error) {
      if (liveCaseIdRef.current !== caseId) return;
      const detail = error instanceof ApiError ? error.detail : "";
      if (detail === "beta_disclosure_changed") {
        try { setLiveBeta(await api.betaOffer(caseId)); } catch { /* keep old; consent re-arms on refresh */ }
        setBetaError("The beta disclosure changed. Read the current text and agree again before accepting.");
      } else {
        if (detail === "beta_qualification_expired" || detail === "beta_qualification_changed" || detail === "beta_entitlement_required") setBetaBlocked(true);
        setBetaError(plainError(error));
      }
    } finally {
      setBetaBusy(false);
    }
  }, [goTo, liveCase, liveBeta, betaBusy]);

  const refreshBetaOffer = useCallback(async () => {
    if (!LIVE || !liveCase || betaBusy) return;
    const caseId = liveCase.caseId;
    setBetaBusy(true); setBetaError(null);
    try {
      const fresh = await api.betaOffer(caseId);
      if (liveCaseIdRef.current !== caseId) return;
      setLiveBeta(fresh); setBetaBlocked(false);
      if (fresh.accepted_at) {
        const saved = await api.status(caseId);
        if (liveCaseIdRef.current !== caseId) return;
        setLiveStatus(saved); setLiveAmount(0); setTier("certified");
        setTimerEnd(saved.seconds_remaining == null ? null : Date.now() + saved.seconds_remaining * 1000);
        setLapsed(saved.window_status === "EXPIRED");
        goTo(saved.window_started_at ? 3 : 4);
      }
    } catch (error) {
      if (liveCaseIdRef.current === caseId) setBetaError(plainError(error));
    } finally {
      setBetaBusy(false);
    }
  }, [liveCase, betaBusy, goTo]);

  useEffect(() => {
    if (!LIVE || PREVIEW_AUTH || restored.current) return;
    restored.current = true;
    (async () => {
      try {
        const workspace = await api.currentAppeal();
        if (!workspace) return;
        const requestedWorkspace = new URLSearchParams(window.location.search).get("w");
        if (requestedWorkspace && requestedWorkspace !== workspace.workspace_id) return;
        restoreWorkspace(workspace);
        setWorkspaceId(workspace.workspace_id);
        // Discover the ticket-reading capability only after the restored workspace exists.
        const discovered = workspace.case_id ? null : await discoverVision(workspace);
        {
          const form = workspace.intake_form;
          if (form) {
            const values = form.values ?? {};
            setFields((previous) => previous.map((field) => ({...field,v:values[`field.${field.k}`] ?? field.v,t:(values[`field.${field.k}`] ?? field.v).toUpperCase()})));
            setPlate(values.plate ?? ""); setPlateState(values.plateState ?? "");
            setStory(values.story ?? ""); setOwner(values.owner || null);
            setPhoto(values.photo === "" || values.photo === undefined ? null : values.photo === "true");
            setBackPhoto(values.backPhoto === "true");
            setContactValues(Object.fromEntries(Object.entries(EMPTY_CONTACT).map(([key,value]) => [key,values[`contact.${key}`] ?? value])) as LiveContactValues);
            setPhotoRefs({ticket:form.ticket_photo_ref ?? null,back:form.ticket_back_photo_ref ?? null});
            const savedEvidenceRefs = form.evidence_photo_refs ?? (form.evidence_photo_ref ? [form.evidence_photo_ref] : []);
            setEvidencePhotos(savedEvidenceRefs.map(ref => ({id:crypto.randomUUID(),file:null,ref})));
            setStep(workspace.case_id ? form.step ?? 1 : restoredTicketStep(workspace, discovered?.status));
          }
        }
        // Required reading cannot resume on a legacy manual-only path.
        if (!workspace.case_id && (workspace.ticket_reading_required || discovered?.status.required) && restoredTicketStep(workspace, discovered?.status) === 1) {
          setVisionDirty(false);
          setStep(1);
        }
        if (!workspace.case_id || !workspace.intake) return;
        const intake = workspace.intake;
        const status = await api.status(workspace.case_id);
        setLiveCase({ workspaceId: workspace.workspace_id, caseId: workspace.case_id });
        // Resolve funding before any paid checkout or assigned price is shown.
        let restoredBeta: BetaOffer | null = null;
        let betaAccepted = status.payment_state === "NO_CHARGE";
        if (betaAccepted) {
          // The saved case proves the accepted funding even if the offer read fails.
          try { restoredBeta = await api.betaOffer(workspace.case_id); } catch { restoredBeta = null; }
        } else {
          restoredBeta = await fetchBetaOffer(workspace.case_id);
          if (restoredBeta?.accepted_at) betaAccepted = true;
        }
        setLiveBeta(restoredBeta);
        setPlate(intake.ticket.license_plate); setPlateState(intake.ticket.license_plate_state);
        setStory(intake.customer_position.statement);
        setFields((previous) => previous.map((field) => ({ ...field, v: ({ cit: intake.ticket.citation_number, date: intake.ticket.issue_date, agency: intake.ticket.issuer_hint, vio: intake.ticket.violation_description } as Record<string, string | null>)[field.k] ?? field.v })));
        setLiveStatus(status);
        setLiveAmount(status.amount_cents ?? null);
        setTier(status.selected_tier === "STANDARD" ? "standard" : "certified");
        setTimerEnd(status.seconds_remaining == null ? null : Date.now() + status.seconds_remaining * 1000);
        setLapsed(status.window_status === "EXPIRED");
        window.history.replaceState(null, "", `?w=${workspace.workspace_id}&c=${workspace.case_id}`);
        if (status.state === "STOPPED_NO_CHARGE" && status.stop) {
          setLiveStop(status.stop); setStep(8);
        } else if (customerStage(status) === 7) {
          setLiveSubmissionPending(true);
          setStep(7);
          try { setLiveTracking(await api.tracking(workspace.case_id)); }
          catch { setLiveError("The saved mailing is here, but postal updates are unavailable. Check mailing status to retry."); }
        } else if (customerStage(status) === 6) {
          setLiveDraft(await api.draft(workspace.case_id));
          setStep(6);
        } else if (restoredBeta && !betaAccepted) {
          setStep(4);
        } else if (betaAccepted && !status.window_started_at) {
          setStep(3);
        } else if (status.window_started_at) {
          setStep(3);
        } else {
          const offer = await api.preHold(workspace.case_id);
          if (offer.kind === "stop") { setLiveStop(offer as Stop); setStep(8); }
          else { setLiveOffer(offer as Offer); setStep(4); }
        }
      } catch (error) {
        if (!(error instanceof ApiError) || error.status !== 401) { setLiveError(plainError(error)); setResumeUnavailable(true); }
      } finally { setDraftReady(true); }
    })();
  }, [restoreWorkspace, fetchBetaOffer, discoverVision]);

  useEffect(() => {
    if (!LIVE || !liveCase || liveBusy || liveStatus?.state === "STOPPED_NO_CHARGE") return;
    let active = true;
    const poll = async () => {
      try {
        const status = await api.status(liveCase.caseId);
        if (!active) return;
        setLiveStatus(status);
        setTimerEnd(status.seconds_remaining == null ? null : Date.now() + status.seconds_remaining * 1000);
        setLapsed(status.window_status === "EXPIRED");
        if (status.window_status === "EXPIRED") invalidateLivePreview();
        if (status.state === "STOPPED_NO_CHARGE" && status.stop) { invalidateLivePreview(); setLiveStop(status.stop); setStep(8); }
        else if (customerStage(status) === 7) {
          setLiveSubmissionPending(true);
          // Uncertain approval keeps its original packet/replay path until resolved.
          if (!pendingPacketApproval.current) setStep(7);
          const tracking = await api.tracking(liveCase.caseId);
          if (active) setLiveTracking(tracking);
        }
      } catch (error) { if (active) setLiveError(plainError(error)); }
    };
    const timer = setInterval(() => {void poll();},5000);
    return () => {active = false;clearInterval(timer);};
  }, [liveCase,liveBusy,liveStatus?.state,liveStatus?.payment_state,invalidateLivePreview]);

  const selectPhoto = useCallback(async (kind: "ticket" | "back", file: File | null) => {
    const version = ++photoVersions.current[kind];
    setTicketUploadError(null);
    setPhotoFiles(previous => ({...previous,[kind]:file}));
    setPhotoRefs(previous => ({...previous,[kind]:null}));
    // Changed ticket bytes invalidate the current reading, comparison and confirmation.
    if (LIVE && !PREVIEW_AUTH && (kind === "ticket" || kind === "back")) invalidateVisionPhotos();
    if (!file || PREVIEW_AUTH) return;
    setPhotoUploads(count => count + 1); setLiveError(null); setVisionError(null);
    try {
      const workspace = await ensureWorkspace();
      setWorkspaceId(workspace.workspace_id);
      const uploaded = await api.uploadPhoto(workspace.workspace_id,file);
      if (photoVersions.current[kind] === version) setPhotoRefs(previous => ({...previous,[kind]:uploaded.photo_ref}));
    } catch (error) {
      if (photoVersions.current[kind] === version) {
        const message = plainError(error);
        setLiveError(message);
        setTicketUploadError(message);
      }
    }
    finally { setPhotoUploads(count => count - 1); }
  }, [ensureWorkspace, invalidateVisionPhotos]);

  const uploadEvidencePhoto = useCallback(async (entry: EvidencePhotoEntry): Promise<string | null> => {
    if (!entry.file) return null;
    setPhotoUploads(count => count + 1); setLiveError(null);
    try {
      const workspace = await ensureWorkspace();
      const uploaded = await api.uploadPhoto(workspace.workspace_id, entry.file);
      setEvidencePhotos(previous => previous.map(current => current.id === entry.id ? {...current,ref:uploaded.photo_ref} : current));
      return uploaded.photo_ref;
    } catch (error) {
      setLiveError(plainError(error));
      return null;
    } finally { setPhotoUploads(count => count - 1); }
  }, [ensureWorkspace]);

  const addEvidencePhotos = useCallback((files: File[]) => {
    const entries = files.map(file => ({id:crypto.randomUUID(),file,ref:null}));
    setEvidencePhotos(previous => [...previous,...entries]);
    if (!PREVIEW_AUTH) for (const entry of entries) void uploadEvidencePhoto(entry);
  }, [uploadEvidencePhoto]);

  const removeEvidencePhoto = useCallback((id: string) => {
    setEvidencePhotos(previous => previous.filter(entry => entry.id !== id));
  }, []);

  const moveEvidencePhoto = useCallback((id: string, direction: -1 | 1) => {
    setEvidencePhotos(previous => {
      const index = previous.findIndex(entry => entry.id === id);
      const target = index + direction;
      if (index < 0 || target < 0 || target >= previous.length) return previous;
      const reordered = [...previous];
      [reordered[index], reordered[target]] = [reordered[target], reordered[index]];
      return reordered;
    });
  }, []);

  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => {
      if (photoUploads) { event.preventDefault(); event.returnValue = ""; }
    };
    window.addEventListener("beforeunload",warn);
    return () => window.removeEventListener("beforeunload",warn);
  }, [photoUploads]);

  const handleLiveDetails = useCallback(async (data: LiveDetailsData) => {
    const fv = (k: string) => fields.find((f) => f.k === k)?.v.trim() ?? "";
    const ticketVisionRequired = visionRequired;
    const issued = ticketVisionRequired ? null : isoDate(fv("date"));
    if (!ticketVisionRequired && !issued) { setLiveError("The date issued should look like 10/02/2026. Go back to Step 1 to fix it."); return; }
    setLiveBusy(true); setLiveError(null);
    try {
      let workspace: WorkspaceView;
      let ticketRef: string;
      let ticketBackRef: string | null;
      let ticket: TicketFacts;
      let confirmationCommandId: string | undefined;
      if (ticketVisionRequired) {
        // Save the current story/contact and reuse the exact confirmed facts and original photos.
        const saved = await flushVisionForm({
          ticket_photo_ref: photoRefs.ticket,
          ticket_back_photo_ref: photoRefs.back,
        });
        const form = saved.intake_form;
        if (!form?.ticket_photo_ref) throw new ApiError(409, "ticket_photo_required");
        const binding = form.ticket_fact_binding;
        if (!binding) throw new ApiError(409, "ticket_confirmation_required");
        const observed = await api.ticketReadingStatus(saved.workspace_id);
        if (observed.confirmation.state !== "current" || !observed.confirmation.command_id) {
          throw new ApiError(409, "ticket_confirmation_stale");
        }
        workspace = saved;
        ticketRef = form.ticket_photo_ref;
        ticketBackRef = form.ticket_back_photo_ref ?? null;
        ticket = ticketFactsFromAdopted(binding.adopted);
        confirmationCommandId = observed.confirmation.command_id;
        setVisionStatus(observed);
        setVisionConfirmed({
          commandId: observed.confirmation.command_id,
          reviewRequestId: observed.confirmation.review_request_id ?? "",
          generation: observed.confirmation.generation,
        });
      } else {
        // The statement is the customer's side; workspace notes stay for their own drafts.
        if (!PREVIEW_AUTH) await flushWorkspace();
        workspace = PREVIEW_AUTH ? await api.createWorkspace("") : await ensureWorkspace();
        const uploadedFront = data.ticketPhotoRef ?? (data.ticketPhoto ? (await api.uploadPhoto(workspace.workspace_id, data.ticketPhoto)).photo_ref : null);
        if (!uploadedFront) throw new ApiError(409, "unknown_photo_ref");
        const uploadedBack = data.ticketBackPhotoRef ?? (data.ticketBackPhoto ? (await api.uploadPhoto(workspace.workspace_id, data.ticketBackPhoto)).photo_ref : null);
        ticketRef = uploadedFront;
        ticketBackRef = uploadedBack ?? null;
        ticket = {
          citation_number: fv("cit"),
          license_plate: plate,
          license_plate_state: plateState,
          issuer_hint: fv("agency"),
          issued_city: data.issuedCity.trim(),
          issued_state: data.issuedState,
          issuer_kind: data.issuerKind,
          violation_code: fv("vio"),
          violation_description: fv("vio"),
          issue_date: issued!,
          stage: "first-level review",
        };
      }
      const evidenceRefs: string[] = [];
      let evidenceUploadRetried = false;
      for (const entry of data.evidencePhotos) {
        const ref = entry.ref ?? (entry.file ? await uploadEvidencePhoto(entry) : null);
        if (!ref) throw new ApiError(409,"unknown_photo_ref");
        if (!entry.ref) evidenceUploadRetried = true;
        evidenceRefs.push(ref);
      }
      if (evidenceUploadRetried && !PREVIEW_AUTH) {
        await flushWorkspace({...intakeForm, ticket_photo_ref: ticketRef, ticket_back_photo_ref: ticketBackRef, evidence_photo_refs:evidenceRefs});
      }
      const receipt = await api.submitIntake(workspace.workspace_id, {
        ticket,
        rental_company_paid: data.rentalPaid === "yes" ? true : data.rentalPaid === "no" ? false : data.rentalPaid === "not-rental" ? "NOT_RENTAL" : null,
        customer_position: { statement: story },
        contact: {
          name: data.name.trim(),
          phone: data.phone,
          email: data.email,
          mailing_address: {
            line1: data.line1.trim(),
            line2: data.line2.trim(),
            city: data.city.trim(),
            state: data.state,
            zip_code: data.zip,
          },
        },
        citation_photo_ref: ticketRef,
        citation_back_photo_ref: ticketBackRef,
        photo_refs: evidenceRefs,
        ...(confirmationCommandId ? { ticket_confirmation_command_id: confirmationCommandId } : {}),
      });
      if (!PREVIEW_AUTH) {
        if (accessRequest.current?.workspace !== workspace.workspace_id || accessRequest.current.email !== data.email) {
          accessRequest.current = { workspace: workspace.workspace_id, email: data.email, id: crypto.randomUUID() };
        }
        try { await api.requestAccess(data.email, accessRequest.current.id); }
        catch { showToast("Your appeal is saved. The access email could not be queued yet; keep this appeal’s URL to request another link."); }
      }
      const ids = { workspaceId: workspace.workspace_id, caseId: receipt.case_id };
      setLiveCase(ids);
      window.history.replaceState(null, "", `?w=${ids.workspaceId}&c=${ids.caseId}`);
      const beta = await fetchBetaOffer(receipt.case_id);
      if (beta) {
        setLiveBeta(beta);
        if (beta.accepted_at) {
          const saved = await api.status(receipt.case_id);
          setLiveStatus(saved); setLiveAmount(0); setTier("certified");
          setTimerEnd(saved.seconds_remaining == null ? null : Date.now() + saved.seconds_remaining * 1000);
          goTo(5);
        } else {
          goTo(4);
        }
        return;
      }
      const preHold = await api.preHold(receipt.case_id);
      if (preHold.kind === "stop") { setLiveStop(preHold as Stop); goTo(8); }
      else { setLiveOffer(preHold as Offer); goTo(4); }
    } catch (error) {
      setLiveError(plainError(error));
    } finally {
      setLiveBusy(false);
    }
  }, [fields, plate, plateState, story, goTo, showToast, flushWorkspace, ensureWorkspace, uploadEvidencePhoto, intakeForm, fetchBetaOffer, visionRequired, flushVisionForm, photoRefs]);

  const handleDestinationStatus = useCallback((status: CaseStatus) => {
    applySavedStatus(status);
  }, [applySavedStatus]);

  // Waiting/limited-beta and stale-409 retry share one in-place current-case
  // refresh. No reload, no new authorization, no replacement key.
  const checkSavedStatus = useCallback(async () => {
    if (!LIVE || !liveCase || liveBusy) return;
    await refreshCurrentCase();
  }, [liveCase, liveBusy, refreshCurrentCase]);

  // Explicit context/status distinction: only a resolved paid phase plus paid
  // funding permits a NEW paid affordance. A saved case's own authoritative
  // payment+window is separate and keeps its truthful existing-paid recovery.
  const savedKind = LIVE ? savedFundingKind(liveStatus) : "none";
  const admittedPaid = savedKind === "admitted_paid";
  const savedPending = savedKind === "saved_pending";
  const newPaidAccess = !newPaidBlocked && !admittedPaid && !savedPending && phase === "paid" && funding === "paid";
  const betaFunding = !admittedPaid && !savedPending && (funding === "beta" || savedKind === "beta");

  const handleStep5Complete = useCallback(() => {
    if (!LIVE) { goTo(6); return; }
    if (!liveCase) return;
    const caseId = liveCase.caseId;
    goTo(3);
    setLiveBusy(true); setLiveError(null);
    (async () => {
      try {
        let status = await api.advance(caseId, "research");
        if (status.state === "NEEDS_CUSTOMER" && owner === "yes" && status.question?.question_id === "VEHICLE_RELATIONSHIP") {
          status = await api.answer(caseId, { question_id: "VEHICLE_RELATIONSHIP", answer: "REGISTERED_OWNER" });
        }
        setLiveStatus(status);
        stopFromStatus(status);
      } catch (error) {
        setLiveError(plainError(error)); showToast(plainError(error));
      } finally {
        setLiveBusy(false);
      }
    })();
  }, [goTo, liveCase, owner, showToast, stopFromStatus]);

  const handleLiveDraft = useCallback(async () => {
    if (!liveCase) return;
    setLiveBusy(true); setLiveError(null);
    try {
      const status = await api.advance(liveCase.caseId, "draft");
      if (stopFromStatus(status)) return;
      setLiveDraft(await api.draft(liveCase.caseId));
      goTo(6);
    } catch (error) {
      setLiveError(plainError(error)); showToast(plainError(error));
    } finally {
      setLiveBusy(false);
    }
  }, [goTo, liveCase, showToast, stopFromStatus]);

  const liveDraftAction = useCallback(async (action: (caseId: string) => Promise<unknown>) => {
    if (!liveCase) return;
    invalidateLivePreview();
    setLiveBusy(true); setLiveError(null);
    try {
      await action(liveCase.caseId);
      setLiveDraft(await api.draft(liveCase.caseId));
    } catch (error) {
      setLiveError(plainError(error));
    } finally {
      setLiveBusy(false);
    }
  }, [liveCase, invalidateLivePreview]);

  const saveLiveLetter = useCallback(async (text: string, replaceCustomerContent = false) => {
    if (!liveCase || !liveDraft || letterSaveInFlight.current) return;
    letterSaveInFlight.current = true;
    invalidateLivePreview();
    setLiveLetterText(text);
    setLiveBusy(true); setLiveError(null);
    try {
      if (!pendingLetterEdit.current) pendingLetterEdit.current = {text, expected: liveDraft.text_sha256, replaceCustomerContent};
      const uncertain = pendingLetterEdit.current;
      let saved = await api.editDraft(liveCase.caseId, uncertain.text, uncertain.expected, uncertain.replaceCustomerContent);
      pendingLetterEdit.current = null;
      if (saved.text !== text || (replaceCustomerContent && !uncertain.replaceCustomerContent)) {
        pendingLetterEdit.current = {text, expected: saved.text_sha256, replaceCustomerContent};
        saved = await api.editDraft(liveCase.caseId, text, saved.text_sha256, replaceCustomerContent);
        pendingLetterEdit.current = null;
      }
      setLiveDraft(saved);
    } catch (error) {
      if (error instanceof ApiError && error.detail === "stale_draft") {
        pendingLetterEdit.current = null;
        try { setLiveDraft(await api.draft(liveCase.caseId)); } catch { /* Keep local words if the refresh fails. */ }
        setLiveError("The saved letter changed before this action. Your local words are still here. Review the letter and save or replace the omitted content again.");
      } else {
        // Replay the exact uncertain request, including its deliberate replacement intent.
        setLiveError(plainError(error));
      }
    }
    finally { letterSaveInFlight.current = false; setLiveBusy(false); }
  }, [liveCase, liveDraft, invalidateLivePreview]);

  useEffect(() => {
    if (!LIVE || liveLetterText == null || liveLetterText === liveDraft?.text) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [liveLetterText, liveDraft?.text]);

  const handleLivePreview = useCallback(async (signature: Blob | null) => {
    if (!liveCase || !liveDraft || packetActionInFlight.current || liveSubmissionPending || pendingPacketApproval.current || lapsed) return;
    if (liveDraft.adopted_sha256 !== liveDraft.text_sha256 || (liveLetterText ?? liveDraft.text) !== liveDraft.text || liveDraft.flags?.length) return;
    packetActionInFlight.current = true;
    invalidateLivePreview();
    const revision = packetPreviewRevision.current;
    setLiveBusy(true); setLiveError(null);
    try {
      const signatureRef = signature
        ? (await api.uploadPhoto(liveCase.workspaceId, signature)).photo_ref
        : undefined;
      if (revision !== packetPreviewRevision.current) return;
      const packet = await api.previewPacket(liveCase.caseId, liveDraft.text_sha256, signatureRef);
      if (revision !== packetPreviewRevision.current) return;
      if (packet.letter_text_sha256 !== liveDraft.text_sha256) throw new ApiError(409, "packet_preview_mismatch");
      const bytes = Uint8Array.from(atob(packet.pdf_base64), character => character.charCodeAt(0));
      const digest = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)), byte => byte.toString(16).padStart(2, "0")).join("");
      if (digest !== packet.packet_sha256) throw new ApiError(409, "packet_preview_mismatch");
      if (revision !== packetPreviewRevision.current) return;
      const url = URL.createObjectURL(new Blob([bytes], { type: "application/pdf" }));
      livePacketUrl.current = url;
      setLivePacketPreview({ packet, url, signatureRef, caseId: liveCase.caseId, adoptedSha256: liveDraft.text_sha256 });
    } catch (error) {
      if (revision === packetPreviewRevision.current) setLiveError(plainError(error));
    } finally {
      packetActionInFlight.current = false;
      setLiveBusy(false);
    }
  }, [liveCase, liveDraft, liveLetterText, liveSubmissionPending, lapsed, invalidateLivePreview]);

  const handleLiveApprove = useCallback(async (affirmations: { declaration_id: string; text_sha256: string }[] = []) => {
    if (!liveCase || packetActionInFlight.current || (!liveDraft && !liveSubmissionPending)) return;
    let approval = pendingPacketApproval.current;
    if (!liveSubmissionPending && !approval) {
      if (!liveDraft || !livePacketPreview || livePacketPreview.caseId !== liveCase.caseId || livePacketPreview.adoptedSha256 !== liveDraft.adopted_sha256 || liveDraft.adopted_sha256 !== liveDraft.text_sha256 || (liveLetterText ?? liveDraft.text) !== liveDraft.text || lapsed) return;
      approval = {
        caseId: liveCase.caseId,
        request: {
          adopted_sha256: livePacketPreview.adoptedSha256,
          packet_sha256: livePacketPreview.packet.packet_sha256,
          envelope_sha256: livePacketPreview.packet.envelope_sha256,
          affirmations,
          ...(livePacketPreview.signatureRef ? { signature_ref: livePacketPreview.signatureRef } : {}),
        },
      };
      // A lost response replays these exact inspected hashes and signature reference.
      pendingPacketApproval.current = approval;
    }
    if (approval && approval.caseId !== liveCase.caseId) return;
    packetActionInFlight.current = true;
    setLiveBusy(true); setLiveError(null);
    try {
      let frozen: CaseStatus;
      if (liveSubmissionPending) {
        frozen = await api.status(liveCase.caseId);
      } else if (approval) {
        frozen = await api.approve(approval.caseId, approval.request);
        pendingPacketApproval.current = null;
        setLiveApprovalUncertain(false);
        setLiveSubmissionPending(true);
      } else return;
      if (stopFromStatus(frozen)) return;
      let status = frozen;
      for (let i = 0; i < 4 && status.payment_state !== "CAPTURED"; i++) {
        status = await api.advance(liveCase.caseId, "dispatch");
        if (stopFromStatus(status)) return;
      }
      const tracking = await api.tracking(liveCase.caseId);
      setLiveStatus(status);
      setLiveTracking(tracking);
      if (!mailingConfirmed(status, tracking)) {
        setStep(7);
        setLiveError("Your packet is still being processed. Mailing and payment are not both confirmed yet. Check mailing status to continue.");
        return;
      }
      setTimerEnd(null);
      goTo(7);
    } catch (error) {
      const pending = pendingPacketApproval.current;
      if (pending && error instanceof ApiError && error.status >= 400 && error.status < 500) {
        pendingPacketApproval.current = null;
        setLiveApprovalUncertain(false);
        invalidateLivePreview();
        setLiveError(plainError(error));
      } else if (pending) {
        setLiveApprovalUncertain(true);
        setLiveError("The connection was interrupted. Approval may have been saved. Retry this same inspected packet to confirm; your original signature and packet will be reused.");
      } else {
        setLiveError("We could not confirm the mailing and payment. Check mailing status to continue.");
      }
    } finally {
      packetActionInFlight.current = false;
      setLiveBusy(false);
    }
  }, [goTo, liveCase, liveDraft, livePacketPreview, liveLetterText, liveSubmissionPending, lapsed, stopFromStatus, invalidateLivePreview]);


  const refreshMailing = useCallback(async () => {
    if (!liveCase || liveBusy) return;
    if (liveStatus && ["FROZEN", "SUBMITTING", "IN_TRANSIT"].includes(liveStatus.state) && !["CAPTURED", "REFUNDED", "RELEASED", "WRITTEN_OFF"].includes(liveStatus.payment_state ?? "")) {
      await handleLiveApprove();
      return;
    }
    setLiveBusy(true); setLiveError(null);
    try {
      setLiveStatus(await api.status(liveCase.caseId));
      setLiveTracking(await api.tracking(liveCase.caseId));
    } catch (error) { setLiveError(plainError(error)); }
    finally { setLiveBusy(false); }
  }, [liveCase, liveStatus, liveBusy, handleLiveApprove]);

  const handleRestartWindow = useCallback(() => {
    if (LIVE) return;
    setTimerEnd(Date.now() + 30 * 60 * 1000); setWarned(false); setLapsed(false);
    showToast("A new 30-minute window has started.");
  }, [showToast]);

  const handleOpenExactPdf = useCallback(() => {
    if (!adopted || sents.some((s) => !s.removed && s.flag) || !senderName.trim() || !senderAddress.trim() || lapsed) return;
    const fv = (k: string) => fields.find((f) => f.k === k)?.v ?? "";
    const pages = composeLetterPages(sents, senderName, senderAddress, fv("cit"));
    pdfPagesRef.current = pages; pdfBlobRef.current = pdfFromCanvases(pages);
    if (pdfUrlRef.current) URL.revokeObjectURL(pdfUrlRef.current);
    pdfUrlRef.current = URL.createObjectURL(pdfBlobRef.current);
    setPdfPreview({ pages, url: pdfUrlRef.current });
    setSheetPdf(true);
  }, [adopted, sents, senderName, senderAddress, lapsed, fields]);

  const handleApprovePdf = useCallback(() => {
    if (lapsed) { showToast("Your window closed. Start a new one to approve. Nothing is lost."); return; }
    if (!adopted || !sheetPdf || !pdfBlobRef.current) return;
    setSheetPdf(false); goTo(7);
  }, [goTo, lapsed, showToast, adopted, sheetPdf]);

  const handleOpenSentenceModal = useCallback((sent: SentenceItem) => {
    setSheetProvSent(sent); setSheetProvEditing(false); setSheetProv(true);
  }, []);

  const handleProvSave = useCallback((newText: string, editNote: string) => {
    if (!sheetProvSent) return;
    if (adopted) return;
    const changed = newText.trim() !== sheetProvSent.text || sheetProvSent.flag;
    if (newText.trim() && changed && !editNote.trim()) return;
    const noteId = `edit-${sheetProvSent.id}-${notes.length}`;
    if (newText.trim() && changed) setNotes((previous) => [...previous, { id: noteId, g: "Your edit note", cls: "words", text: editNote.trim() }]);
    setSents((previous) => previous.map((sent) => sent.id !== sheetProvSent.id ? sent : !newText.trim() ? { ...sent, removed: true } : { ...sent, text: newText.trim(), ...(changed ? { src: [noteId], edited: true, flag: false } : {}) }));
    setSheetProv(false);
  }, [sheetProvSent, adopted, notes.length]);

  const handleProvRemove = useCallback(() => {
    if (!sheetProvSent || adopted || sheetProvSent.fixed) return;
    setSents((prev) => prev.map((s) => s.id === sheetProvSent.id ? { ...s, removed: true } : s));
    setSheetProv(false); showToast("Sentence removed. It won\u2019t appear in your letter.");
  }, [sheetProvSent, adopted, showToast]);

  const handleDownloadEvidence = useCallback(() => {
    const fv = (k: string) => fields.find((f) => f.k === k)?.v ?? "";
    const q = pdfComposer();
    q.lines("Evidence record \u00b7 sample", "bold 22px Georgia", 29);
    q.lines("Simulated for this prototype. No live research or mailing occurred.", "12px sans-serif");
    q.space(8); q.lines(`Citation ${fv("cit")} \u00b7 ${fv("date")} \u00b7 plate ${plate}, ${plateState}`);
    q.lines(`Back photo: ${backPhoto ? "sample added" : "not provided"}. Sign photo: ${photo ? "mailed after the letter and saved in the evidence record" : "not provided"}.`);
    q.lines("Your notes and sentence provenance", "bold 14px Georgia");
    notes.forEach((note) => q.lines(`${note.g}: ${note.text}`, "11px Georgia", 16));
    q.lines("Saved research · simulated", "bold 14px Georgia");
    SAMPLE_FACTS.forEach((fact) => {
      q.lines(`${fact.fact} · ${fact.st}`, "bold 11px Georgia", 16);
      q.lines(fact.quote ?? fact.note ?? "Unknown", "11px Georgia", 16);
      q.lines(fact.url, "10px sans-serif", 15);
    });
    const allPages = [...pdfPagesRef.current, ...q.pages];
    const blob = pdfFromCanvases(allPages); const url = URL.createObjectURL(blob);
    const a = document.createElement("a"); a.href = url; a.download = "parkingbreaker-evidence-record-sample.pdf";
    document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 30000);
    showToast("Sample evidence record PDF downloaded.");
  }, [fields, plate, plateState, backPhoto, photo, notes, showToast]);

  const handleDownloadReport = useCallback(() => {
    const fv = (k: string) => fields.find((f) => f.k === k)?.v ?? "";
    const q = pdfComposer();
    q.lines("ParkingBreaker Delivery Confirmation", "bold 22px Georgia", 30);
    q.lines("Simulated for this prototype · not a USPS Return Receipt.", "12px sans-serif"); q.space(12);
    q.lines(`Citation ${fv("cit")} \u00b7 Certified Mail \u00b7 tracking 9407 0000 0000 0000 0000 00 (sample)`);
    q.lines("USPS reports: accepted in San Diego, CA 92199 on Oct 16, 2026.");
    q.lines("USPS reports delivered to the agency mailing address on Oct 19, 2026."); q.space(12);
    q.lines("This report does not establish when agency staff opened or reviewed the letter.");
    const blob = pdfFromCanvases(q.pages); const url = URL.createObjectURL(blob);
    const a = document.createElement("a"); a.href = url; a.download = "parkingbreaker-delivery-confirmation-sample.pdf";
    document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 30000);
    showToast("Sample Delivery Confirmation report downloaded.");
  }, [fields, showToast]);

  const step1Vision = useMemo<Step1VisionProps>(() => ({
    mode: visionMode,
    enabled: Boolean(visionStatus?.enabled),
    required: visionRequired,
    checking: visionChecking,
    error: ticketUploadError ?? visionError,
    status: visionStatus,
    reading: visionStatus?.extraction ?? null,
    review: visionReview,
    literal: visionLiteral,
    draft: visionDraft,
    setDraft: updateVisionDraft,
    resolutions: visionResolutions,
    setResolutions: setVisionResolutions,
    busy: visionBusy,
    confirmedCommandId: visionConfirmed?.commandId ?? null,
    dirty: visionDirty,
    frontRef: photoRefs.ticket,
    backRef: photoRefs.back,
    onStart: async () => {
      const found = await discoverVision();
      if (!found) return "unknown" as const;
      return found.status.enabled ? "vision" as const : "legacy" as const;
    },
    onPickPhoto: (kind, file) => { void selectPhoto(kind, file); },
    onRead: () => { void readTicketVision(); },
    onCheckReading: () => { void refreshVisionStatusOnly().catch((error) => setVisionError(plainError(error))); },
    onSaveFacts: (facts) => { void saveAndReviewVision(facts); },
    onConfirm: () => { void confirmTicketVision(); },
    onContinue: handleVisionContinue,
  }), [visionMode, visionStatus, visionRequired, visionChecking, visionError, ticketUploadError, visionReview, visionLiteral, visionDraft, updateVisionDraft, visionResolutions, visionBusy, visionConfirmed, visionDirty, photoRefs, discoverVision, selectPhoto, readTicketVision, refreshVisionStatusOnly, saveAndReviewVision, confirmTicketVision, handleVisionContinue]);

  const visionAdoptedFacts = useMemo(() => validateFactDraft(visionDraft).facts, [visionDraft]);

  const receipts = LIVE ? liveReceipts(step, fields, liveOffer, liveBeta, liveStatus, liveAmount) : buildReceipts(step, fields, photo, tier);
  const fv = (k: string) => fields.find((f) => f.k === k)?.v ?? "";

  if (LIVE && (!draftReady || resumeUnavailable)) return <main className="access-shell"><section className="access-card">{resumeUnavailable ? <h1>Your appeal service is unavailable</h1> : <AccessLoading title="Opening saved appeal…" message="Loading… Restoring your saved progress." />}{resumeUnavailable && <><p>We couldn’t load the saved state. Keep your appeal URL and try again.</p><button className="btn btn-primary" onClick={() => window.location.reload()}>Try again</button></>}</section></main>;

  return (
    <>
      <SvgDefs />

      <a href="#main" className="skip">Skip to content</a>

      <Header now={now} step={step} timerEnd={timerEnd} lapsed={lapsed} evidenceCount={LIVE ? liveStatus?.research ? `${liveStatus.research.findings.filter(f => f.status === "CONFIRMED").length}/${liveStatus.research.findings.length}` : "pending" : `${evidenceCount}/9`} onBrandClick={() => { if (LIVE && liveCase) { if (liveStatus) goTo(customerStage(liveStatus)); } else goTo(1); }} onEvidenceClick={() => setSheetChk(true)} onRulesClick={openRules} fulfillmentLabel={LIVE && step === 7 && liveStatus ? fulfillmentCopy(liveStatus,liveTracking).title : undefined} deliveryComplete={LIVE && liveStatus?.state === "DELIVERED" && liveTracking?.case_id === liveStatus.case_id && liveTracking?.state === "DELIVERED"} />

      {LIVE && !PREVIEW_AUTH && !liveCase && <div className="access-session" role="status"><span>{photoUploads ? "Saving photo…" : autosaveState === "error" ? "Save paused. Your entries remain on this page; another tab may have changed this appeal." : autosaveState === "saving" ? "Saving your entries…" : autosaveState === "saved" ? "Entries saved for this appeal" : "Your entries will save as you go"}</span>{autosaveState === "error" && <button className="chip" onClick={() => {void flushWorkspace().catch(() => {});}}>Retry save</button>}</div>}
      <main id="main" className={`app-shell${step === 1 ? " intake-shell" : ""}`}>
        {step === 1 && <IntakeIntroduction />}
        <div className="stream-col">
          <Receipts receipts={LIVE && liveCase ? receipts.map(receipt => ({...receipt,reopenable:receipt.stepNum === 3})) : receipts} onReopen={goTo} />
          {LIVE && liveError && step !== 2 && step !== 3 && step !== 6 && step !== 7 && <div className="guard action-error" role="alert" id="liveActionError">{liveError}</div>}
          {lapsed && (step === 5 || step === 6) && <div className="guard" role="status"><span>{LIVE ? "The saved review window has ended. Your work stays saved; checking server status is required before another action." : "Your window closed. Nothing was charged and your work is saved."}</span>{LIVE && liveCase ? <button className="chip" type="button" disabled={liveBusy} onClick={async () => { try { setLiveError(null); handleDestinationStatus(await api.status(liveCase.caseId)); } catch (error) { setLiveError(plainError(error)); } }}>Check my appeal</button> : <button className="chip" type="button" onClick={handleRestartWindow}>Start a new window</button>}</div>}
          <div key={run}>
          {step === 1 && (<Step1Ticket live={LIVE} fields={fields} setFields={setFields} plate={plate} setPlate={setPlate} plateState={plateState} setPlateState={setPlateState} backPhoto={backPhoto} setBackPhoto={setBackPhoto} onComplete={handleStep1Complete} setActionBar={setActionBar} vision={LIVE && !PREVIEW_AUTH ? step1Vision : undefined} />)}
          {step === 2 && (<Step2YourSide story={story} setStory={setStory} owner={owner} setOwner={setOwner} photo={photo} setPhoto={setPhoto} onComplete={handleStep2Complete} setActionBar={setActionBar} onRulesClick={openRules} liveSlot={LIVE ? <LiveDetails busy={liveBusy || photoUploads > 0} submitting={liveBusy} error={liveError} onSubmit={handleLiveDetails} values={contactValues} onChange={setContactValues} ticketPhoto={photoFiles.ticket} ticketBackPhoto={photoFiles.back} evidencePhotos={evidencePhotos} ticketPhotoRef={photoRefs.ticket} ticketBackPhotoRef={photoRefs.back} onSelectPhoto={(kind,file) => {void selectPhoto(kind,file);}} onAddEvidencePhotos={addEvidencePhotos} onRemoveEvidencePhoto={removeEvidencePhoto} onMoveEvidencePhoto={moveEvidencePhoto} visionRequired={visionRequired} visionFacts={visionAdoptedFacts ? { issuedCity: visionAdoptedFacts.issued_city, issuedState: visionAdoptedFacts.issued_state, issuerKind: visionAdoptedFacts.issuer_kind } : null} /> : undefined} />)}
          {step === 3 && LIVE && (<LiveResearch status={liveStatus} busy={liveBusy} onDraft={handleLiveDraft} onRetry={handleStep5Complete} caseId={liveCase?.caseId} onStatus={handleDestinationStatus} error={liveError} onAnswer={async answer => { if (!liveCase || liveBusy) return; setLiveBusy(true); setLiveError(null); try { handleDestinationStatus(await api.answer(liveCase.caseId,answer)); } catch (error) { setLiveError(plainError(error)); } finally { setLiveBusy(false); } }} setActionBar={setActionBar} />)}
          {step === 3 && !LIVE && (<Step3Research onProgress={setEvidenceCount} onComplete={completeResearch} setActionBar={setActionBar} onOpenFact={(f) => { setSheetFactData(f); setSheetFact(true); }} onOpenChecklist={() => setSheetChk(true)} onGoConflict={openConflict} />)}
          {step === 4 && LIVE && liveBeta && (<Step4BetaOffer offer={liveBeta} caseId={liveCase?.caseId ?? ""} busy={liveBusy || betaBusy} blocked={betaBlocked} error={betaError} onAccept={handleBetaAccept} onRefresh={() => { void refreshBetaOffer(); }} setActionBar={setActionBar} />)}
          {step === 4 && LIVE && !liveBeta && (!liveOffer || phase !== "paid" || paidPaused || newPaidBlocked) && (<ControlledBetaWaiting paused={paidPaused} admittedPaid={admittedPaid} savedPending={savedPending} phaseRefreshFailed={pricingRefreshFailed} busy={liveBusy} onRetry={checkSavedStatus} setActionBar={setActionBar} />)}
          {step === 4 && !(LIVE && (liveBeta || !liveOffer || phase !== "paid" || paidPaused || newPaidBlocked)) && (<Step4Offer liveOffer={LIVE && liveOffer ? { issuerName: liveOffer.issuer.issuer_name, decidingBody: liveOffer.issuer.deciding_body, standardOffered: liveOffer.tiers.some((t) => t.tier === "STANDARD"), testMode: process.env.NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY?.startsWith("pk_test_") ?? false, certifiedCents: liveOffer.tiers.find(t => t.tier === "CERTIFIED")?.amount_cents ?? null, standardCents: liveOffer.tiers.find(t => t.tier === "STANDARD")?.amount_cents ?? null } : undefined} cardSlot={LIVE ? CARD_FORM_AVAILABLE ? <CardField ref={cardRef} /> : <p className="small" role="status">The secure card form is unavailable. Please try again later.</p> : undefined} selectedTier={tier} setSelectedTier={setTier} onComplete={handleStep4Complete} setActionBar={setActionBar} onOpenTerms={openTerms} onOpenSample={openSample} />)}
          {step === 5 && (<Step5Window live={LIVE} beta={Boolean(LIVE && (liveStatus?.payment_state === "NO_CHARGE" || liveBeta?.accepted_at))} amountCents={liveAmount} now={now} tier={tier ?? "certified"} timerEnd={timerEnd} lapsed={lapsed} onComplete={handleStep5Complete} setActionBar={setActionBar} onDemoWarn={() => { setTimerEnd(Date.now() + 5 * 60 * 1000); setWarned(false); setLapsed(false); }} onDemoLapse={() => { setTimerEnd(Date.now()); setWarned(true); setLapsed(false); }} />)}
          {step === 6 && LIVE && liveDraft && (<LiveLetter key={liveDraft.text_sha256 + (liveDraft.adopted_sha256 ?? "")} draft={liveDraft} value={liveLetterText ?? liveDraft.text} onTextChange={setLiveLetterText} autoSave busy={liveBusy || (lapsed && !liveSubmissionPending && !liveApprovalUncertain)} error={liveError} submissionPending={liveSubmissionPending} approvalUncertain={liveApprovalUncertain} preview={livePacketPreview?.packet ?? null} previewUrl={livePacketPreview?.url ?? null} onInvalidatePreview={invalidateLivePreview} onSave={saveLiveLetter} onAdopt={() => liveDraftAction((c) => api.adopt(c, liveDraft.text, liveDraft.text_sha256))} onPreview={handleLivePreview} onApprove={handleLiveApprove} />)}
          {LIVE && step !== 6 && liveLetterText != null && liveLetterText !== liveDraft?.text && <div className="card"><p role="alert">These letter edits have not been saved. Keep this page open and copy your text before leaving.</p><textarea aria-label="Unsaved letter edits" value={liveLetterText} readOnly /></div>}
          {step === 6 && !LIVE && (<Step6LetterStudio traceMode={traceMode} setTraceMode={setTraceMode} citationNumber={fv("cit")} lapsed={lapsed} onOpenNotes={openNotes} onOpenSentenceModal={handleOpenSentenceModal} onOpenExactPdf={handleOpenExactPdf} onRestartWindow={handleRestartWindow} setActionBar={setActionBar} sents={sents} adopted={adopted} setAdopted={setAdopted} senderName={senderName} setSenderName={setSenderName} senderAddress={senderAddress} setSenderAddress={setSenderAddress} />)}
          {step === 7 && LIVE && liveStatus && <LiveFulfillment status={liveStatus} tracking={liveTracking} busy={liveBusy} error={liveError} onRefresh={() => { void refreshMailing(); }} />}
          {step === 7 && !LIVE && (<Step7Mailed tier={tier ?? "certified"} onDownloadEvidence={handleDownloadEvidence} onDownloadReport={handleDownloadReport} setActionBar={setActionBar} />)}
          {step === 8 && (<Step8Stop liveStop={LIVE && liveStop ? { message: liveStop.message, officialQuote: liveStop.official_quote ?? null, officialUrl: liveStop.official_url ?? null, savedWords: liveDraft?.text ?? story } : undefined} onCopyNotes={copyNotes} onOpenAgencyForm={openAgencyForm} onEditDetails={reopenStopDetails} onStartNew={startNewFromStop} setActionBar={setActionBar} />)}
          {step === 9 && (<Step9Conflict optedIn={conflictOptedIn} setOptedIn={setConflictOptedIn} onBackToResearch={backToResearch} setActionBar={setActionBar} />)}
          </div>
          {LIVE && liveCase && liveStatus?.payment_state === "NO_CHARGE" && (
            <BetaNotices key={`beta-notices-${liveCase.caseId}`} caseId={liveCase.caseId} workspaceId={liveCase.workspaceId} />
          )}
        </div>
        {step === 1 ? <IntakeTrust /> : <DesktopAside onEvidenceClick={openChecklist} onRulesClick={openRules} servicePhase={phase} funding={funding} paidCase={admittedPaid} savedPending={savedPending} betaCase={betaFunding} newPaidBlocked={newPaidBlocked} />}
      </main>

      <div className={step === 1 ? "intake-actions" : undefined}><ActionBar subText={actionSub}>{LIVE && (step === 6 || step === 7) ? null : actionButtons}</ActionBar></div>
      {!LIVE && <StepJumper currentStep={step} onGoTo={goTo} />}
      {toast && <div className="toast show" id="toast" role="status" aria-live="polite">{toast}</div>}

      {/* How it works */}
      <BottomSheet id="how" isOpen={sheetHow} onClose={() => setSheetHow(false)} title="How it works & rules">
        <div className="how-pillars">
          <div className="how-pillar">
            <span className="how-pillar-tag">Clerical only</span>
            <p><strong>We handle the clerical work.</strong> We prepare and mail your letter. We don’t judge your case or predict how the agency will decide.</p>
          </div>
          <div className="how-pillar">
            <span className="how-pillar-tag">Source-backed</span>
            <p><strong>Every fact, sourced.</strong> Each agency rule we use shows the official page and the exact words.</p>
          </div>
          <div className="how-pillar">
            <span className="how-pillar-tag">Clear billing</span>
            <p>
              {newPaidAccess ? (
                <><strong>No charge unless mailed.</strong> Your card is charged only after the first USPS scan, or confirmed mailing production if scans are delayed.</>
              ) : admittedPaid ? (
                <><strong>Your existing payment stays the same.</strong> This appeal keeps its own saved mailing choice, payment status and window; no new card or payment is requested.</>
              ) : savedPending ? (
                <><strong>We’re checking your existing payment.</strong> No new card or replacement payment is requested; use Check my appeal for the latest update.</>
              ) : betaFunding ? (
                <><strong>No card and no payment in this selected free beta.</strong> This appeal is funded by the beta and is not charged.</>
              ) : (
                <><strong>No new card or payment is requested now.</strong> This appeal’s service phase, funding or any earlier saved payment is still being confirmed; use Check my appeal for the latest update before choosing a mailing option.</>
              )}
            </p>
          </div>
        </div>
        <h3 className="how-section-title">Six calm steps</h3>
        <ol className="how-steps">
          <li><div><strong>Add your ticket.</strong><span>Photograph the front, and the back if it has one. We read it, and you check every detail.</span></div></li>
          <li><div><strong>Tell your side.</strong><span>Tell us what happened in your own words. We ask neutral questions about the facts.</span></div></li>
          <li><div><strong>Choose your mailing.</strong><span>{LIVE ? (newPaidAccess ? "You choose from the mailing options and prices confirmed for this appeal. Nothing is pre-selected." : admittedPaid ? "Your existing mailing choice and payment status stay as recorded; no new payment is requested." : savedPending ? "We’re checking your existing payment; no new payment is requested." : betaFunding ? "This selected free beta offers no paid mailing option, and no card or payment is requested." : phase === "controlled_free_beta" ? "This controlled beta offers no new paid mailing option. No new card or payment is requested now; use Check my appeal for the latest update." : "No paid mailing options are shown until this appeal’s service phase and funding are confirmed.") : `You choose Certified Mail ($${CERTIFIED_CENTS / 100}). Nothing is pre-selected.`}</span></div></li>
          <li><div><strong>Check the requirements.</strong><span>We check the agency’s official pages and show you the sources and exact wording.</span></div></li>
          <li><div><strong>Approve your packet.</strong><span>Review and edit the letter, inspect the exact PDF, and approve it before anything is sent.</span></div></li>
          <li><div><strong>Track your mailing.</strong><span>Keep the available postal updates and a copy of your exact mailed packet in your appeal.</span></div></li>
        </ol>
        <h3 className="how-section-title">What we never do</h3>
        <ul className="never">
          <li><span className="never-badge" aria-hidden="true"><Icon id="x" size={14} /></span><span>Predict how the agency will decide, or judge the strength of your case.</span></li>
          <li><span className="never-badge" aria-hidden="true"><Icon id="x" size={14} /></span><span>Suggest arguments or add facts you didn&#39;t give us.</span></li>
          <li><span className="never-badge" aria-hidden="true"><Icon id="x" size={14} /></span><span>State an agency rule without its official source and exact words.</span></li>
          <li><span className="never-badge" aria-hidden="true"><Icon id="x" size={14} /></span><span>{betaFunding ? "Charge you in a selected free beta where no payment is requested." : "Charge you before mailing is confirmed."}</span></li>
          <li><span className="never-badge" aria-hidden="true"><Icon id="x" size={14} /></span><span>Claim that the agency has opened, read or decided your appeal. We report the postal updates available for your mailing.</span></li>
          <li><span className="never-badge" aria-hidden="true"><Icon id="x" size={14} /></span><span>Pre-select options or hide prices.</span></li>
        </ul>
      </BottomSheet>

      {/* Single fact */}
      <BottomSheet id="factDlg" isOpen={sheetFact} onClose={() => setSheetFact(false)} title={sheetFactData?.fact ?? "Official Evidence"}>
        {sheetFactData && (
          <>
            <div className="row" style={{ marginBottom: "8px" }}>
              <span className={`bdg t-${sheetFactData.tier}`}>{sheetFactData.tier === "must" ? "Must-have" : sheetFactData.tier === "imp" ? "Important" : "Nice-to-have"}</span>
              <span className={`bdg s-${sheetFactData.st}`}>{sheetFactData.st === "ok" ? "Confirmed" : sheetFactData.st === "unk" ? "Unknown" : sheetFactData.st === "con" ? "Conflicted" : "N/A"}</span>
            </div>
            {sheetFactData.quote ? <blockquote className="sheet-quote">&#34;{sheetFactData.quote}&#34;</blockquote> : <div className="guard" style={{ margin: "10px 0" }}><b>Not found, so not guessed.</b> {sheetFactData.note}</div>}
            {sheetFactData.quote && sheetFactData.note && <p className="muted small" style={{ marginBottom: "10px" }}>{sheetFactData.note}</p>}
            <div className="card" style={{ padding: "12px 14px", marginBottom: "12px", background: "#fffdfa" }}>
              <b className="small">What this means for your letter</b>
              <p className="small" style={{ marginTop: "3px", color: "var(--ink-2)" }}>{sheetFactData.means}</p>
            </div>
            <p className="small"><b>{sheetFactData.src}</b><br /><a href={sheetFactData.url} target="_blank" rel="noopener noreferrer">{sheetFactData.url}</a><br /><span className="muted">Saved Oct 12, 2026 \u00b7 Simulated for this prototype</span></p>
          </>
        )}
      </BottomSheet>

      {/* Full checklist */}
      <BottomSheet id="chkDlg" isOpen={sheetChk} onClose={() => setSheetChk(false)} title="Research Checklist">
        {LIVE ? <ResearchEvidence research={liveStatus?.research} idPrefix="checklist" /> : <>
        <p className="muted small" style={{ marginBottom: "12px" }}>Tap any item to inspect its verbatim quote, official source link and date.</p>
        {(["must", "imp", "nice"] as const).map((t) => {
          const items = SAMPLE_FACTS.filter((f) => f.tier === t);
          const label = t === "must" ? "Must-have (needed to mail)" : t === "imp" ? "Important (shapes the letter)" : "Nice-to-have (extra context)";
          return (
            <div key={t} style={{ marginBottom: "16px" }}>
              <h3 style={{ fontSize: "16px", marginBottom: "8px" }}>{label}</h3>
              <div className="fact-stream">{items.map((f) => <FactRow key={f.id} fact={f} onClick={() => { setSheetFactData(f); setSheetChk(false); setSheetFact(true); }} />)}</div>
            </div>
          );
        })}
        <div className="card" style={{ padding: "14px", background: "#fffdfa", marginTop: "8px" }}>
          <b className="small">Why we checked further</b>
          <p className="small muted" style={{ marginTop: "4px" }}><b>A plain-English veto:</b> Not stated on the city&#39;s page — we read the linked form too and found no extra requirement.</p>
        </div>
        </>}
      </BottomSheet>

      {/* Terms */}
      <BottomSheet id="termsDlg" isOpen={sheetTerms} onClose={() => setSheetTerms(false)} title="Hold & refund terms">
        <ul className="never">
          <li>If we stop before mailing, the hold is released and you are not charged.</li>
          <li>A duplicate charge is refunded.</li>
          <li>Our mailing errors: a free re-mail where feasible, otherwise a full refund. Agency-side returns do not receive a refund. When the cause is unknown, a re-mail or refund minus mailing cost may apply. Customer-supplied addresses have the separate terms shown before acceptance.</li>
          <li>After mailing, a change of mind or the agency&#39;s decision is not a refund reason.</li>
        </ul>
        <LegalDisclosure id="termsAttorneyDisclosure" />
      </BottomSheet>

      {/* Layout sample */}
      <BottomSheet id="sampleDlg" isOpen={sheetSample} onClose={() => setSheetSample(false)} title="Letter layout sample">
        <p className="muted small">A glimpse of spacing and sections before authorization. You approve the exact PDF before anything is mailed.</p>
        <div className="sample-letter" aria-label="Sample letter layout, not the exact PDF">
          <span className="sample-stamp">Layout only \u00b7 not your PDF</span>
          <div className="line short" /><div className="line med" /><div className="line" />
          <div className="line short" style={{ marginTop: "16px" }} /><div className="line" /><div className="line med" /><div className="line" />
          <div className="line short" style={{ marginTop: "16px" }} />
        </div>
      </BottomSheet>

      {/* Sentence provenance & edit */}
      <BottomSheet id="provDlg" isOpen={sheetProv} onClose={() => { setSheetProv(false); setSheetProvEditing(false); }} title="Where this came from">
        {sheetProvSent && !sheetProvEditing && (
          <>
            <blockquote className="sheet-quote">&#34;{sheetProvSent.text}&#34;</blockquote>
            {sheetProvSent.flag ? (
              <div className="flagbar" style={{ marginBottom: "12px" }}><Icon id="dot" size={18} style={{ color: "#d97706", flex: "none" }} /><div><b>No source in your notes</b><p>This sentence adds a date you didn&#39;t give — edit or remove.</p></div></div>
            ) : (
              <div className="stack" style={{ marginBottom: "14px" }}>
                {sheetProvSent.src.map((nid) => { const n = notes.find((x) => x.id === nid); return n ? <div key={nid} className="card" style={{ padding: "10px 12px", background: "var(--amber-soft)" }}><b className="small" style={{ display: "block", color: "var(--amber-ink)" }}>{n.g}</b><span className="small">{n.text}</span></div> : null; })}
              </div>
            )}
            {!adopted && (
              <div className="row">
                <button className="btn btn-soft btn-sm" type="button" onClick={() => setSheetProvEditing(true)}>Edit sentence</button>
                {!sheetProvSent.fixed && <button className="btn btn-ghost btn-sm" type="button" onClick={handleProvRemove}>Remove</button>}
              </div>
            )}
            {adopted && <p className="muted small">Text adopted. Tap &#34;Unlock&#34; on the studio bar to make changes.</p>}
          </>
        )}
        {sheetProvSent && sheetProvEditing && (
          <ProvEditForm sent={sheetProvSent} onSave={(text, note) => { handleProvSave(text, note); setSheetProvEditing(false); }} onCancel={() => setSheetProvEditing(false)} />
        )}
      </BottomSheet>

      {/* Notes */}
      <BottomSheet id="notesDlg" isOpen={sheetNotes} onClose={() => setSheetNotes(false)} title="Your notes">
        <p className="muted small" style={{ marginBottom: "10px" }}>Everything the letter is allowed to say.</p>
        {(() => {
          const groups: string[] = []; const grouped: Record<string, NoteItem[]> = {};
          notes.forEach((n) => { if (!groups.includes(n.g)) groups.push(n.g); if (!grouped[n.g]) grouped[n.g] = []; grouped[n.g].push(n); });
          return groups.map((g) => (
            <div key={g} style={{ marginBottom: "12px" }}>
              <p className="eyebrow" style={{ color: "var(--ink-3)" }}>{g}</p>
              {grouped[g].map((n) => <div key={n.id} className="card" style={{ padding: "10px 12px", marginBottom: "6px", fontSize: "14px" }}>{n.text}</div>)}
            </div>
          ));
        })()}
      </BottomSheet>

      {/* Exact PDF */}
      {adopted && <BottomSheet id="pdfDlg" isOpen={sheetPdf} onClose={() => setSheetPdf(false)} title="Approve the exact PDF" closeAriaLabel="Close preview"
        footer={<><button className="btn btn-ghost btn-sm" type="button" onClick={() => setSheetPdf(false)}>Go back</button><button className="btn btn-primary btn-sm" type="button" id="approveBtn" onClick={handleApprovePdf}>Approve this PDF (simulate mailing)</button></>}>
        {sheetPdf && adopted && pdfPreview && (
          <>
            <p className="small" style={{ marginBottom: "10px" }}>This is the actual PDF generated from the text and sender details you approved. {pdfPreview.pages.length} page{pdfPreview.pages.length === 1 ? "" : "s"}; no signature or exhibit page is added in this sample. <a href={pdfPreview.url} download="parkingbreaker-letter-sample.pdf">Download exact PDF</a>.</p>
            <div className="pdf-canvas-wrap" id="pdfCanvasMount">{pdfPreview.pages.map((c, i) => <PdfCanvasDisplay key={i} canvas={c} />)}</div>
            <div className="guard" style={{ marginTop: "12px" }}><Icon id="shield" size={18} /><span>Only this PDF would be mailed by Certified Mail. Your card is charged only after the first USPS scan, or confirmed mailing production if scans are delayed.</span></div>
          </>
        )}
      </BottomSheet>}
    </>
  );
}

function PdfCanvasDisplay({ canvas }: { canvas: HTMLCanvasElement }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => { if (ref.current) { ref.current.innerHTML = ""; ref.current.appendChild(canvas); } }, [canvas]);
  return <div ref={ref} />;
}

function ProvEditForm({ sent, onSave, onCancel }: { sent: SentenceItem; onSave: (text: string, note: string) => void; onCancel: () => void; }) {
  const [text, setText] = useState(sent.text);
  const [note, setNote] = useState("");
  const [noteErr, setNoteErr] = useState(false);
  const handleSave = () => {
    const v = text.trim().replace(/\s+/g, " ");
    if (!v) { onSave("", ""); return; }
    const changed = v !== sent.text || sent.flag;
    if (changed && !note.trim()) { setNoteErr(true); return; }
    onSave(v, note.trim());
  };
  return (
    <>
      <label htmlFor="editTa" style={{ fontWeight: 750, display: "block", marginBottom: "4px" }}>Edit sentence</label>
      <textarea id="editTa" style={{ width: "100%", minHeight: "84px", font: "inherit", fontSize: "16.5px", padding: "10px", border: "2px solid var(--coral)", borderRadius: "12px", marginBottom: "10px" }} value={text} onChange={(e) => setText(e.target.value)} autoFocus />
      <label htmlFor="editNote" style={{ fontWeight: 750, display: "block", marginBottom: "4px" }}>Your note supporting this edit</label>
      <textarea id="editNote" placeholder="What did you observe or what detail are you providing?" style={{ width: "100%", minHeight: "72px", font: "inherit", fontSize: "15.5px", padding: "10px", border: "1.5px solid var(--line-strong)", borderRadius: "12px" }} value={note} onChange={(e) => { setNote(e.target.value); setNoteErr(false); }} />
      <p className="muted small" style={{ margin: "6px 0 10px" }}>Required for every changed sentence. This note stays beside the sentence in your workspace.</p>
      {noteErr && <p className="err" id="editNoteErr">Add your own note before saving the change.</p>}
      <div className="row"><button className="btn btn-primary btn-sm" type="button" onClick={handleSave}>Save</button><button className="btn btn-ghost btn-sm" type="button" onClick={onCancel}>Cancel</button></div>
    </>
  );
}
