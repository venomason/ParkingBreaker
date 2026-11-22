/**
 * ParkingBreaker API client. Types are generated from the API's OpenAPI document
 * (npm run api-types); a failed call throws ApiError and never returns invented data.
 * The SAMPLE_* fixtures below still drive the prototype steps not yet wired (job W2).
 */

import { CERTIFIED_CENTS } from "./prices";
import type { components } from "./api-types";

type Schemas = components["schemas"];
export type SubmitIntake = Schemas["SubmitIntake"];
export type IntakeForm = Schemas["IntakeForm"];
export type TicketFacts = Schemas["TicketFacts"];
export type TicketFactBindingPayload = Schemas["TicketFactBindingPayload"];
export type WorkspaceView = Schemas["WorkspaceView"];
export type TicketReviewSession = Schemas["TicketReviewSession"];
export type TicketReviewObservationRequest = Schemas["TicketReviewObservationRequest"];
export type IntakeReceipt = Schemas["IntakeReceipt"];
export type PhotoRef = Schemas["PhotoRef"];
export type Offer = Schemas["Offer"];
export type Stop = Schemas["Stop"];
export type PreHoldResult = Offer | Stop;
export type MailTier = Schemas["MailTier"];
export type Authorization = Schemas["Authorization"];
export type CaseStatus = Schemas["CaseStatus"];
export type SaveRequiredCopies = Schemas["SaveRequiredCopies"];
export type QuestionAnswer = Schemas["AnswerQuestion"] | Schemas["PaymentAnswerQuestion"];
export type Draft = Schemas["Draft"];
export type AdoptionReceipt = Schemas["AdoptionReceipt"];
export type PacketPreview = Schemas["PacketPreview"];
export type ApprovalRequest = Schemas["Approve"];
export type Tracking = Schemas["Tracking"];
export type MailingRecord = Schemas["ReturnProofPacket"];
export type DestinationChoice = Schemas["DestinationChoice"];
export type DestinationDisclosure = Schemas["DestinationDisclosure"];
export type DestinationAddress = Schemas["DestinationAddress"];
export type DestinationAddressReceipt = Schemas["DestinationAddressReceipt"];
export type DestinationConsentReceipt = Schemas["DestinationConsentReceipt"];
export type BetaOffer = Schemas["BetaOffer"];
export type AcceptBeta = Schemas["AcceptBeta"];
export type BetaAcceptanceReceipt = Schemas["BetaAcceptanceReceipt"];
export type BetaFeedbackNoticeRequest = Schemas["BetaFeedbackNoticeRequest"];
export type BetaFeedbackNoticeReceipt = Schemas["BetaFeedbackNoticeReceipt"];
export type BetaFeedbackNoticeList = Schemas["BetaFeedbackNoticeList"];
// Immutable server funding posture. It is returned on every pricing response;
// a missing or unknown value must never be read as "paid".
export type ServicePhase = "paid" | "controlled_free_beta";
export type PricingAssignment = {
  experiment_id: string;
  arm: string | null;
  amount_cents: number | null;
  service_phase: ServicePhase;
};

export function isServicePhase(value: unknown): value is ServicePhase {
  return value === "paid" || value === "controlled_free_beta";
}

// Ticket reading/review/confirmation wire values. These routes are intentionally absent
// from the generated OpenAPI document (customer-facing vocabulary only), so their closed
// shapes are typed here instead of being guessed at the call site.
export type TicketLiteralFieldName =
  | "citation_number"
  | "license_plate"
  | "license_plate_state"
  | "issuer_name"
  | "issue_date"
  | "notice_date"
  | "printed_deadline"
  | "violation_text"
  | "issued_city"
  | "issued_state"
  | "stage_text";
export type TicketAdoptedFieldName =
  | "citation_number"
  | "license_plate"
  | "license_plate_state"
  | "issuer_hint"
  | "issued_city"
  | "issued_state"
  | "issuer_kind"
  | "violation_code"
  | "violation_description"
  | "issue_date"
  | "stage"
  | "notice_date"
  | "printed_deadline";
export type TicketReadingState = "shown" | "not_shown" | "unreadable";
export type TicketDiscrepancyResolution =
  | "CONFIRMED_VALUE_STANDS"
  | "ABSENCE_ACKNOWLEDGED"
  | "UNREADABLE_ACKNOWLEDGED";
export type TicketReviewComparison = "match" | "conflict" | "not_shown" | "unreadable";
export type TicketReadingField = {
  field: TicketLiteralFieldName;
  state: TicketReadingState;
  value: string | null;
  confidence?: number | null;
};
export type TicketReadingSnapshot = {
  request_id?: string | null;
  status: "missing" | "pending" | "success" | "refused" | "unknown";
  current: boolean;
  synthetic: boolean;
  fields: TicketReadingField[];
  primary_model?: string | null;
  escalation?: "not_needed" | "manual" | "unavailable" | "budget_exhausted" | "failed" | "refused" | "completed" | null;
  confidence_basis?: "uncalibrated model estimate";
};
export type TicketReviewField = {
  field: TicketLiteralFieldName;
  comparison: TicketReviewComparison;
  visible_text: string | null;
};
export type TicketReviewSnapshot = {
  request_id?: string | null;
  status: "missing" | "pending" | "success" | "refused" | "unknown";
  current: boolean;
  synthetic: boolean;
  basis: "model comparison only; not external verification" | "customer-supplied facts; no model comparison";
  fields: TicketReviewField[];
};
export type TicketConfirmationSnapshot = {
  state: "missing" | "current" | "stale";
  command_id?: string | null;
  review_request_id?: string | null;
  generation: number;
  discrepancy_status?: "none" | "resolved" | "unresolved" | null;
  reason?: string | null;
};
export type TicketReadingStatus = {
  enabled: boolean;
  required: boolean;
  synthetic: boolean;
  extraction: TicketReadingSnapshot;
  review: TicketReviewSnapshot;
  confirmation: TicketConfirmationSnapshot;
  next_confirmation_generation: number;
};
export type TicketConfirmationReceipt = {
  command_id: string;
  state: "current";
  generation: number;
  review_request_id: string;
  discrepancy_status: "none" | "resolved" | "unresolved";
  synthetic: boolean;
};

export const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "/api";
// Fixed identities are a deliberate local-development option, never a production fallback.
export const PREVIEW_AUTH = process.env.NODE_ENV !== "production" && process.env.NEXT_PUBLIC_PREVIEW_AUTH === "1";
let pricingRequest: Promise<PricingAssignment> | null = null;
// Bound access checks, including response-body reads. Slow research/drafting
// requests keep their existing lifecycle; a timeout never retries a mutation.
const ACCESS_TIMEOUT_MS = 12_000;

export class ApiError extends Error {
  constructor(public status: number, public detail: string) {
    super(detail);
  }
}

function csrfToken(): string {
  if (typeof document === "undefined") return "";
  return document.cookie.split("; ").find((part) => part.startsWith("__Host-pb-csrf="))?.slice("__Host-pb-csrf=".length) ?? "";
}

async function call<T>(method: string, path: string, body?: unknown, publicAuth = false, timeoutMs?: number, quiet = false): Promise<T> {
  const controller = timeoutMs === undefined ? undefined : new AbortController();
  const timer = controller ? setTimeout(() => controller.abort(), timeoutMs) : undefined;
  try {
    const response = await fetch(`${API_BASE}${path}`, {
      method,
      signal: controller?.signal,
      credentials: "same-origin",
      cache: "no-store",
      headers: {
        ...(PREVIEW_AUTH && !publicAuth ? { Authorization: "Bearer fake-alice" } : {}),
        ...(!PREVIEW_AUTH && !publicAuth && method !== "GET" ? { "X-CSRF-Token": csrfToken() } : {}),
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!response.ok) {
      if (response.status === 401 && !publicAuth && !quiet && typeof window !== "undefined") {
        window.dispatchEvent(new Event("pb:session-required"));
      }
      let detail = "request_failed";
      try {
        const parsed = (await response.json()) as { detail?: unknown };
        if (typeof parsed.detail === "string") detail = parsed.detail;
      } catch {
        // keep the generic key
      }
      throw new ApiError(response.status, detail);
    }
    return (await response.json()) as T;
  } catch (error) {
    if (controller?.signal.aborted) throw new ApiError(0, "request_timeout");
    if (error instanceof ApiError) throw error;
    throw new ApiError(0, "network_unavailable");
  } finally {
    if (timer !== undefined) clearTimeout(timer);
  }
}

export function fileToBase64(file: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",", 2)[1] ?? "");
    reader.onerror = () => reject(new ApiError(0, "photo_not_readable"));
    reader.readAsDataURL(file);
  });
}

export const PHOTO_UPLOAD_GUIDANCE = "This beta accepts JPEG or PNG photos up to 10 MB each. PDFs, HEIC and other formats are not supported. Export a clear JPEG/PNG copy or photograph the paper original, keeping every edge and printed detail visible.";

function checkPhotoUpload(file: Blob): void {
  if (file.type !== "image/jpeg" && file.type !== "image/png") {
    throw new ApiError(0, "photo_type_not_supported");
  }
  if (!file.size || file.size > 10_000_000) {
    throw new ApiError(0, "photo_size_invalid");
  }
}

function validatePricingAssignment(assignment: PricingAssignment): PricingAssignment {
  const validExperiment = typeof assignment.experiment_id === "string" && assignment.experiment_id.length > 0;
  const legacy = assignment.amount_cents === null && assignment.arm === null;
  const validArm = Number.isSafeInteger(assignment.amount_cents) &&
    (assignment.amount_cents as number) > 0 &&
    assignment.arm === `P${assignment.amount_cents}`;
  // service_phase is required and strict: an absent or unknown phase fails
  // closed instead of defaulting anyone into the paid checkout.
  const validPhase = isServicePhase(assignment.service_phase);
  if (!validExperiment || !validPhase || (!legacy && !validArm)) throw new ApiError(502, "invalid_pricing_assignment");
  return assignment;
}

/** One uncached POST to the existing pricing endpoint. */
function requestPricingAssignment(): Promise<PricingAssignment> {
  const request = call<PricingAssignment>("POST", "/auth/pricing", {}, false, ACCESS_TIMEOUT_MS).then(validatePricingAssignment);
  request.catch(() => {
    if (pricingRequest === request) pricingRequest = null;
  });
  return request;
}

/** Page-load pricing is deduped; an explicit refresh replaces the cached request. */
function cachedPricingAssignment(): Promise<PricingAssignment> {
  const request = pricingRequest ?? requestPricingAssignment();
  pricingRequest = request;
  return request;
}

function refreshedPricingAssignment(): Promise<PricingAssignment> {
  pricingRequest = requestPricingAssignment();
  return pricingRequest;
}

export const api = {
  startTicketReviewSession: (w: string, nonce: string) => call<TicketReviewSession>("POST", `/workspaces/${w}/ticket-review-sessions`, { client_session_id: nonce }, false, 1500, true),
  observeTicketReview: (w: string, session: string, body: TicketReviewObservationRequest) => call<unknown>("POST", `/workspaces/${w}/ticket-review-sessions/${session}/observations`, body, false, 1500, true),
  pricing: cachedPricingAssignment,
  // Existing endpoint, no cache: used only for an explicit server-phase refresh.
  refreshPricing: refreshedPricingAssignment,
  session: () => call<{ authenticated: true }>("GET", "/auth/session", undefined, true, ACCESS_TIMEOUT_MS),
  currentAppeal: () => call<WorkspaceView | null>("GET", "/auth/appeal", undefined, false, ACCESS_TIMEOUT_MS),
  startAppeal: () => call<{ authenticated: true }>("POST", "/auth/appeals", {}, false, ACCESS_TIMEOUT_MS),
  requestAccess: (email: string, requestId: string, workspaceId?: string) => call<{ accepted: true }>("POST", "/auth/link", { email, request_id: requestId, ...(workspaceId ? { workspace_id: workspaceId } : {}) }),
  confirmAccess: (token: string) => call<{ authenticated: true }>("POST", "/auth/confirm", { token }, true),
  logout: () => call<{ authenticated: false }>("POST", "/auth/logout", {}, false, ACCESS_TIMEOUT_MS),
  health: () => call<{ status: string; mode: string }>("GET", "/health"),
  createWorkspace: async (notes = "") => {
    if (!PREVIEW_AUTH) {
      try { await api.session(); }
      catch (error) {
        if (!(error instanceof ApiError) || error.status !== 401) throw error;
        await api.startAppeal();
      }
    }
    return call<WorkspaceView>("POST", "/workspaces", { notes });
  },
  saveIntakeForm: (w: string, revision: number, form: IntakeForm) => call<WorkspaceView>("PUT", `/workspaces/${w}/intake-form`, { expected_revision: revision, form }),
  workspace: (w: string) => call<WorkspaceView>("GET", `/workspaces/${w}`),
  uploadPhoto: async (w: string, file: Blob) => {
    checkPhotoUpload(file);
    return call<PhotoRef>("POST", `/workspaces/${w}/photos`, {
      content_type: file.type,
      data_base64: await fileToBase64(file),
    });
  },
  // Owner-scoped ticket path. The caller supplies a stable request/command id so a lost
  // response can be replayed without a second paid call.
  ticketReadingStatus: (w: string) => call<TicketReadingStatus>("GET", `/workspaces/${w}/ticket-reading`),
  requestTicketReading: (w: string, requestId: string, expectedRevision: number) =>
    call<TicketReadingSnapshot>("POST", `/workspaces/${w}/ticket-reading`, {
      request_id: requestId,
      expected_revision: expectedRevision,
    }),
  requestTicketReview: (w: string, requestId: string, expectedRevision: number, customerSupplied = false) =>
    call<TicketReviewSnapshot>("POST", `/workspaces/${w}/ticket-review`, {
      customer_supplied: customerSupplied,
      request_id: requestId,
      expected_revision: expectedRevision,
    }),
  confirmTicketReading: (
    w: string,
    command: {
      commandId: string;
      expectedRevision: number;
      reviewRequestId: string;
      expectedConfirmationGeneration: number;
      resolvedFields: Record<string, TicketDiscrepancyResolution>;
      accuracyAcknowledged: boolean;
    },
  ) =>
    call<TicketConfirmationReceipt>("POST", `/workspaces/${w}/ticket-confirmation`, {
      command_id: command.commandId,
      expected_revision: command.expectedRevision,
      review_request_id: command.reviewRequestId,
      expected_confirmation_generation: command.expectedConfirmationGeneration,
      resolved_fields: command.resolvedFields,
      accuracy_acknowledged: command.accuracyAcknowledged,
    }),
  submitIntake: (w: string, intake: SubmitIntake) =>
    call<IntakeReceipt>("POST", `/workspaces/${w}/intake`, intake),
  preHold: (c: string) => call<PreHoldResult>("GET", `/cases/${c}/pre-hold`),
  betaOffer: (c: string) => call<BetaOffer>("GET", `/cases/${c}/beta-offer`, undefined, false, ACCESS_TIMEOUT_MS),
  // Consent is only the exact displayed server disclosure; no tier, card or amount.
  acceptBeta: (c: string, disclosureSha256: string) =>
    call<BetaAcceptanceReceipt>("POST", `/cases/${c}/beta-acceptance`, {
      terms_accepted: true,
      disclosure_sha256: disclosureSha256,
    }),
  // Customer-reported notices for an accepted beta. The cursor is the opaque
  // public notice id and is passed back exactly as returned, never decoded.
  notices: (c: string, limit = 25, cursor?: string | null) =>
    call<BetaFeedbackNoticeList>(
      "GET",
      `/cases/${encodeURIComponent(c)}/notices?limit=${encodeURIComponent(String(limit))}${
        cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""
      }`
    ),
  recordNotice: (c: string, request: BetaFeedbackNoticeRequest) =>
    call<BetaFeedbackNoticeReceipt>("POST", `/cases/${encodeURIComponent(c)}/notices`, request),
  // Authenticated same-origin page endpoint; callers must render it unoptimized.
  noticePageUrl: (c: string, noticeId: string, pageNumber: number) =>
    `${API_BASE}/cases/${encodeURIComponent(c)}/notices/${encodeURIComponent(noticeId)}/pages/${encodeURIComponent(String(pageNumber))}`,
  chooseTier: (c: string, tier: MailTier) => call<Offer>("PUT", `/cases/${c}/tier`, { tier }),
  authorize: (c: string, paymentMethodId: string) =>
    call<Authorization>("POST", `/cases/${c}/authorization`, {
      terms_accepted: true,
      payment_method_id: paymentMethodId,
    }, false, ACCESS_TIMEOUT_MS),
  recoverAuthorization: (c: string) =>
    call<CaseStatus>("POST", `/cases/${c}/authorization/recovery`, undefined, false, ACCESS_TIMEOUT_MS),
  status: (c: string) => call<CaseStatus>("GET", `/cases/${c}/status`, undefined, false, ACCESS_TIMEOUT_MS),
  saveRequiredCopies: (c: string, request: SaveRequiredCopies) => call<CaseStatus>("PUT", `/cases/${c}/required-copies`, request),
  requiredCopyPageUrl: (c: string, id: string, page: number) => `${API_BASE}/cases/${encodeURIComponent(c)}/required-copies/${encodeURIComponent(id)}/pages/${page}`,
  answer: (c: string, answer: QuestionAnswer) =>
    call<CaseStatus>("POST", `/cases/${c}/answers`, answer),
  destinationDisclosure: (c: string) => call<DestinationDisclosure>("GET", `/cases/${c}/destination/disclosure`),
  saveDestinationAddress: (c: string, address: DestinationAddress) =>
    call<DestinationAddressReceipt>("POST", `/cases/${c}/destination/address`, address),
  consentDestination: (c: string, consent: { address_revision: number; address_fingerprint: string; disclosure_sha256: string; accepted: true }) =>
    call<DestinationConsentReceipt>("POST", `/cases/${c}/destination/consent`, consent),
  declineDestination: (c: string) => call<CaseStatus>("POST", `/cases/${c}/destination/decline`, {}),
  draft: (c: string) => call<Draft>("GET", `/cases/${c}/draft`),
  editDraft: (c: string, text: string, expectedSha256: string, replaceCustomerContent = false) =>
    call<Draft>("PUT", `/cases/${c}/draft`, { text, expected_sha256: expectedSha256, replace_customer_content: replaceCustomerContent }),
  adopt: (c: string, text: string, textSha256: string) =>
    call<AdoptionReceipt>("POST", `/cases/${c}/adoption`, { text, text_sha256: textSha256 }),
  previewPacket: (c: string, adoptedSha256: string, signatureRef?: string) =>
    call<PacketPreview>("POST", `/cases/${c}/packet-preview`, {
      adopted_sha256: adoptedSha256,
      ...(signatureRef ? { signature_ref: signatureRef } : {}),
    }),
  previewRemail: (c: string, adoptedSha256: string) =>
    call<PacketPreview>("POST", `/cases/${c}/packet-preview`, {
      adopted_sha256: adoptedSha256,
      remail: true,
    }),
  approve: (c: string, request: ApprovalRequest) =>
    call<CaseStatus>("POST", `/cases/${c}/approval`, request),
  tracking: (c: string) => call<Tracking>("GET", `/cases/${c}/tracking`),
  mailingRecord: (c: string) => call<MailingRecord>("GET", `/cases/${c}/mailing-record`),
  /**
   * Wait for the worker to finish a step. In development the dev-worker route runs it at
   * once; deployed, the scheduler runs it and the page polls the case status.
   */
  advance: async (c: string, step: "research" | "draft" | "dispatch"): Promise<CaseStatus> => {
    const response = await fetch(`/dev-worker/${c}/${step}`, { method: "POST" });
    if (response.ok) return (await response.json()) as CaseStatus;
    if (response.status !== 404) throw new ApiError(response.status, "worker_step_failed");
    const done = (s: CaseStatus) =>
      s.state === "STOPPED_NO_CHARGE" ||
      (step === "research" && s.state !== "RESEARCHING") ||
      (step === "draft" && s.state !== "DRAFTING") ||
      (step === "dispatch" && s.payment_state === "CAPTURED");
    for (let attempt = 0; attempt < 120; attempt++) {
      const status = await call<CaseStatus>("GET", `/cases/${c}/status`);
      if (done(status)) return status;
      await new Promise((resolve) => setTimeout(resolve, 2000));
    }
    throw new ApiError(0, "worker_step_timed_out");
  },
};

export const SAMPLE_FACTS: Array<{ id: string; fact: string; tier: "must" | "imp" | "nice"; st: "ok" | "unk" | "con" | "na"; quote: string | null; src: string; url: string; means: string; note?: string }> = [
  {
    id: "f1",
    fact: "Written request by mail",
    tier: "must" as const,
    st: "ok" as const,
    quote: "To submit your Administrative Review by Mail, send your written request and supporting documentation to:\n\nPO Box 129038, San Diego, CA 92112-9038",
    src: "City of San Diego · Administrative Review by Mail",
    url: "https://www.sandiego.gov/parking/citations/appeal",
    means: "Confirms the City of San Diego accepts written administrative review requests by mail.",
    note: undefined
  },
  {
    id: "f2",
    fact: "Citation number, plate number and state in the letter",
    tier: "must" as const,
    st: "ok" as const,
    quote: "Although no particular forms are necessary, please be as brief and concise as possible. The citation number, vehicle license plate number and state must be included in the request.",
    src: "City of San Diego · Administrative Review by Mail",
    url: "https://www.sandiego.gov/parking/citations/appeal",
    means: "Places your citation number, license plate and state in the opening sentence.",
    note: undefined
  },
  {
    id: "f6",
    fact: "No particular form is necessary",
    tier: "must" as const,
    st: "ok" as const,
    quote: "Although no particular forms are necessary, please be as brief and concise as possible. The citation number, vehicle license plate number and state must be included in the request.",
    src: "City of San Diego · Administrative Review by Mail",
    url: "https://www.sandiego.gov/parking/citations/appeal",
    means: "A standard formatted letter is accepted; no separate city PDF form is required.",
    note: undefined
  },
  {
    id: "f3",
    fact: "Mailing destination",
    tier: "must" as const,
    st: "ok" as const,
    quote: "To submit your Administrative Review by Mail, send your written request and supporting documentation to:\n\nPO Box 129038, San Diego, CA 92112-9038",
    src: "City of San Diego · Administrative Review by Mail",
    url: "https://www.sandiego.gov/parking/citations/appeal",
    means: "Addresses your envelope to PO Box 129038, San Diego, CA 92112-9038.",
    note: undefined
  },
  {
    id: "f4",
    fact: "21 calendar days from issuance for this ticket",
    tier: "must" as const,
    st: "ok" as const,
    quote: "Pursuant to State law, requests for Administrative Review must be received (postmarked) within 21 calendar days from the date the citation was issued or within 14 calendar days of the mailing of the Notice of Delinquent Parking Violation.",
    src: "City of San Diego · Administrative Review by Mail",
    url: "https://www.sandiego.gov/parking/citations/appeal",
    means: "Oct 2 issue date + 21 calendar days sets your Oct 23 postmark deadline.",
    note: "The same passage has a different 14-day clock for a mailed delinquency notice. This sample is an original citation."
  },
  {
    id: "f5",
    fact: "Postmark is the counting rule",
    tier: "must" as const,
    st: "ok" as const,
    quote: "Pursuant to State law, requests for Administrative Review must be received (postmarked) within 21 calendar days from the date the citation was issued or within 14 calendar days of the mailing of the Notice of Delinquent Parking Violation.",
    src: "City of San Diego · Administrative Review by Mail",
    url: "https://www.sandiego.gov/parking/citations/appeal",
    means: "USPS postmark date on or before Oct 23 satisfies the city deadline.",
    note: undefined
  },
  {
    id: "f7",
    fact: "Supporting documentation is mentioned",
    tier: "imp" as const,
    st: "ok" as const,
    quote: "To submit your Administrative Review by Mail, send your written request and supporting documentation to:\n\nPO Box 129038, San Diego, CA 92112-9038",
    src: "City of San Diego · Administrative Review by Mail",
    url: "https://www.sandiego.gov/parking/citations/appeal",
    means: "Preserves your sign photo inside your downloadable evidence record.",
    note: "The sign photo stays in the evidence record. This prototype does not add exhibit pages to the mailed letter."
  },
  {
    id: "f8",
    fact: "Keep the request concise",
    tier: "nice" as const,
    st: "ok" as const,
    quote: "Although no particular forms are necessary, please be as brief and concise as possible. The citation number, vehicle license plate number and state must be included in the request.",
    src: "City of San Diego · Administrative Review by Mail",
    url: "https://www.sandiego.gov/parking/citations/appeal",
    means: "Keeps the letter focused strictly on your confirmed facts.",
    note: undefined
  },

];

export interface TicketField {
  k: string;
  label: string;
  v: string;
  t: string;
  hint: string;
}

export interface SentenceItem {
  id: string;
  p: number;
  tag: string;
  text: string;
  src: string[];
  fixed?: boolean;
  flag?: boolean;
  edited?: boolean;
  removed?: boolean;
}

export interface NoteItem {
  id: string;
  g: string;
  cls: string;
  text: string;
}

// Real flow: the customer types each detail as printed on the ticket.
export const LIVE_FIELDS: TicketField[] = [
  { k: "cit", label: "Citation number", v: "", t: "", hint: "Exactly as printed." },
  { k: "date", label: "Date issued", v: "", t: "", hint: "MM/DD/YYYY. Your deadline is counted from this date." },
  { k: "vio", label: "Violation", v: "", t: "", hint: "As printed on the ticket." },
  { k: "agency", label: "Issued by", v: "", t: "", hint: "The agency name printed on the ticket." },
];

/** "10/02/2026" or "2026-10-02" to ISO; null when it is not a real date. */
export function isoDate(text: string): string | null {
  const t = text.trim();
  const us = /^(\d{1,2})\/(\d{1,2})\/(\d{4})$/.exec(t);
  const iso = /^(\d{4})-(\d{2})-(\d{2})$/.exec(t);
  const parts = us ? [us[3], us[1], us[2]] : iso ? [iso[1], iso[2], iso[3]] : null;
  if (!parts) return null;
  const [y, m, d] = parts as [string, string, string];
  const value = `${y}-${m.padStart(2, "0")}-${d.padStart(2, "0")}`;
  const parsed = new Date(`${value}T00:00:00Z`);
  return Number.isNaN(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== value ? null : value;
}

export const SAMPLE_FIELDS: TicketField[] = [
  { k: "cit", label: "Citation number", v: "0000000", t: "0000000", hint: "A synthetic sample number for this prototype." },
  { k: "date", label: "Date issued", v: "October 2, 2026", t: "10/02/2026", hint: "Your deadline is counted from this date." },
  { k: "vio", label: "Violation", v: "Street cleaning", t: "STREET CLEANING", hint: "As printed on the ticket." },
  { k: "loc", label: "Location", v: "3800 block of 30th Street", t: "3800 BLK 30TH ST", hint: "Where the ticket says it was issued." },
  { k: "agency", label: "Issued by", v: "City of San Diego", t: "CITY OF SAN DIEGO", hint: "We’ll confirm this agency through official sources next." },
];

export const SAMPLE_STORY =
  "I parked on my block the evening before. The street-cleaning sign was covered by a tree branch, so you couldn’t read it. I took a photo of the sign the next morning.";

function splitSentences(t: string): string[] {
  return (t.match(/[^.!?]+[.!?]*/g) || [t]).map((s) => s.trim()).filter(Boolean);
}

export function buildLetterData(
  fields: TicketField[],
  plate: string,
  plateState: string,
  story: string,
  owner: string | null,
  photo: boolean | null
): { sents: SentenceItem[]; notes: NoteItem[] } {
  const fv = (k: string) => fields.find((f) => f.k === k)?.v ?? "";
  const isSample = !story || story === SAMPLE_STORY;
  const ownerVal = owner || "yes";
  const hasPhoto = photo === null ? true : photo;

  const notes: NoteItem[] = [
    { id: "n1", g: "Ticket details you confirmed", cls: "", text: `Citation ${fv("cit")} \u00b7 issued ${fv("date")} \u00b7 ${fv("vio").toLowerCase()} \u00b7 ${fv("loc")}` },
  ];
  if (isSample) {
    notes.push({ id: "n2", g: "Your words", cls: "words", text: '"I parked on my block the evening before."' });
    notes.push({ id: "n3", g: "Your words", cls: "words", text: '"The street-cleaning sign was covered by a tree branch, so you couldn’t read it."' });
    notes.push({ id: "n4", g: "Your words", cls: "words", text: '"I took a photo of the sign the next morning."' });
  } else {
    notes.push({ id: "n2", g: "Your words", cls: "words", text: `"${story}"` });
  }
  notes.push({ id: "n5", g: "Your answers", cls: "", text: `Registered owner on Oct 2? ${ownerVal === "yes" ? "Yes" : ownerVal === "no" ? "No" : "Not sure"}` });
  notes.push({ id: "n6", g: "Your answers", cls: "", text: `Save your photo in evidence record? ${hasPhoto ? "Yes" : "No"}` });
  notes.push({ id: "n7", g: "Local ticket details", cls: "", text: `Plate ${plate} \u00b7 state ${plateState}. Added after drafting; not sent to AI.` });
  notes.push({ id: "r1", g: "From official page", cls: "", text: "Mail to: P.O. Box 129038, San Diego, CA 92112-9038" });
  notes.push({ id: "r2", g: "From official page", cls: "", text: "The citation number, vehicle license plate number and state must be included." });
  notes.push({ id: "t1", g: "Standard wording", cls: "", text: "A courtesy closing line. Yours to keep or remove." });

  const sents: SentenceItem[] = [
    { id: "s1", p: 0, tag: "Official address rule", text: "Dear Initial Review Desk:", src: ["r1"], fixed: true },
    { id: "s2", p: 1, tag: "Ticket + official rule", text: `I am writing to request an initial review of parking citation ${fv("cit")}, issued on ${fv("date")}, for a ${fv("vio").toLowerCase()} violation. The vehicle license plate number is ${plate} and the plate state is ${plateState}.`, src: ["n1", "n7", "r2"], fixed: true },
  ];
  if (isSample) {
    sents.push({ id: "s3", p: 1, tag: "Your own words", text: "I parked on the block the evening before the citation was issued.", src: ["n2"] });
    sents.push({ id: "s4", p: 1, tag: "Your own words", text: "The street-cleaning sign on that block was covered by a tree branch, and I could not read it.", src: ["n3"] });
    sents.push({ id: "s5", p: 1, tag: "Your own words", text: "I took a photo of the sign the next morning.", src: ["n4"] });
  } else {
    splitSentences(story).forEach((t, i) =>
      sents.push({ id: `c${i}`, p: 1, tag: "Your own words", text: t, src: ["n2"] })
    );
  }
  sents.push({ id: "s6", p: 1, tag: "\u26a0\ufe0f Unsourced date \u2014 check", text: "I returned to my car on October 3 and found the citation.", src: [], flag: true });
  if (ownerVal === "yes") sents.push({ id: "s7", p: 1, tag: "Your ownership answer", text: `I was the registered owner of the vehicle on ${fv("date")}.`, src: ["n5"] });
  sents.push({ id: "s8", p: 2, tag: "Courtesy closing", text: "Thank you for reviewing this request.", src: ["t1"] });

  return { sents: sents.map((s) => ({ ...s, edited: false, removed: false })), notes };
}


// SAMPLE until API: local preview identity; never sent to a provider.
export const SAMPLE_PREVIEW = {
  plate: "DEMO000",
  plateState: "CA",
  senderName: "Jordan Rivera",
  senderAddress: "123 Sample Street, San Diego, CA 92101",
  letterDate: "October 15, 2026",
  recipient: "City of San Diego\nParking Administration\nP.O. Box 129038\nSan Diego, CA 92112-9038",
};

// SAMPLE until API: presentation cadence for saved research.
export const SAMPLE_RESEARCH = [
    { msg: "Reading mailed-review instructions…", id: "f1", delay: 520 },
    { msg: "Checking contents the city asks for…", id: "f2", delay: 460 },
    { msg: "Checking form requirements…", id: "f6", delay: 440 },
    { msg: "Recording mailing destination…", id: "f3", delay: 480 },
    { msg: "Calculating deadline (Oct 2 + 21 calendar days = Oct 23)…", id: "f4", delay: 500 },
    { msg: "Confirming postmark rule…", id: "f5", delay: 440 },
    { msg: "Checking supporting material wording…", id: "f7", delay: 440 },
    { msg: "Recording brevity guidance…", id: "f8", delay: 400 },
  ];

// SAMPLE until API: tracking display records for each mailing tier.
export function sampleTrackingEvents(certified: boolean) {
  const evList = [
    {
      c: "us",
      w: "ParkingBreaker",
      d: "Oct 15 · 4:12 PM",
      t: "You approved the exact PDF",
      s: "1 letter page · no signature or exhibit page in this example",
    },
    {
      c: "us",
      w: "ParkingBreaker",
      d: "Oct 16 · 9:40 AM",
      t: `Mail provider accepted the ${certified ? "Certified Mail" : "First-Class Mail"} letter`,
      s: `$${CERTIFIED_CENTS / 100} charged after mailing acceptance.`,
    },
  ];

  if (certified) {
    evList.push(
      {
        c: "usps",
        w: "USPS reports",
        d: "Oct 16 · 6:02 PM",
        t: "Accepted at San Diego, CA 92199",
        s: "Tracking 9407 0000 0000 0000 0000 00 (sample)",
      },
      {
        c: "usps",
        w: "USPS reports",
        d: "Oct 17 · 3:18 AM",
        t: "In transit to the destination facility",
        s: "San Diego, CA distribution center",
      },
      {
        c: "deliv",
        w: "USPS reports",
        d: "Oct 19 · 10:42 AM",
        t: "USPS reports delivered to the agency’s mailing address",
        s: "P.O. Box 129038, San Diego, CA 92112-9038",
      }
    );
  }

  return evList;
}

// SAMPLE until API: alternate examples are separate from the main case.
export const SAMPLE_ALTERNATIVES = {
  stopQuote: "Requests for review must be submitted using Form PR-1. Letters received without a completed Form PR-1 will not be processed.",
  ticketBackQuote: "Mail your request for initial review within 14 calendar days of the issue date.",
};

// SAMPLE until API: saved deadline calculation used by the offer timeline.
export const SAMPLE_TIMELINE = [
  { className: "dn edge-l", left: "0%", title: "Oct 2", caption: "Issued" },
  { className: "up today", left: "61.9%", title: "Today", caption: "Oct 15" },
  { className: "dn by", left: "85.7%", title: "Oct 20", caption: "Mail by" },
  { className: "up end edge-r", left: "100%", title: "Oct 23", caption: "Deadline" },
];
