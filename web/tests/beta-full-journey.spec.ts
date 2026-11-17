import { expect, test, type BrowserContext, type Page, type Route } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { deflateSync } from "node:zlib";
import type { IntakeForm } from "../lib/api";
import { factDraftFromValues } from "../lib/intake";

// Production Next build + real cookie auth + durable SQLite + offline provider seams.
// Customer operations use the owner UI; only selection and worker scheduling are test controlled.
const base = process.env.SECURE_BASE;
const privateApi = process.env.INGRESS_API_BASE;
const worker = process.env.INGRESS_WORKER_SECRET;
const python = process.env.BETA_FIXTURE_PYTHON ?? resolve(__dirname, "../../backend/.venv/bin/python");
test.use({ ignoreHTTPSErrors: true });
test.skip(!base || !privateApi || !worker, "requires isolated HTTPS fake-provider rehearsal");
test.setTimeout(120_000);

function loopback(hostname: string) {
  return hostname === "127.0.0.1" || hostname === "localhost" || hostname === "[::1]";
}

function parseOrigin(raw: string, label: string): URL {
  let url: URL;
  try { url = new URL(raw); } catch { throw new Error(`${label} must be an absolute URL`); }
  if (url.username || url.password || url.pathname !== "/" || url.search || url.hash) {
    throw new Error(`${label} must be a bare origin`);
  }
  return url;
}

function secureUrl(): URL {
  if (!base) throw new Error("SECURE_BASE is required");
  const url = parseOrigin(base, "Secure base");
  if (url.protocol !== "https:" || !url.hostname.endsWith(".example.test")) {
    throw new Error("Secure base must be a reserved HTTPS example.test origin");
  }
  return url;
}

function transportUrl(): URL | null {
  const raw = process.env.SECURE_TRANSPORT_BASE;
  if (!raw) return null;
  const url = parseOrigin(raw, "Secure transport base");
  if (!["http:", "https:"].includes(url.protocol) || !loopback(url.hostname)) {
    throw new Error("Secure transport base must be an HTTP(S) loopback origin");
  }
  return url;
}

let guardBlocked: string[] = [];
let guardActive = false;

test.beforeEach(async ({ page }) => {
  guardBlocked = [];
  guardActive = false;
  const secure = secureUrl();
  const transport = transportUrl();
  const allowed = (url: URL) => url.origin === secure.origin
    || (transport !== null && url.origin === transport.origin);
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (["http:", "https:"].includes(url.protocol) && !allowed(url)) {
      guardBlocked.push(`${url.origin}${url.pathname}`);
      await route.abort("blockedbyclient");
      return;
    }
    await route.fallback();
  });
  guardActive = true;
});

test.afterEach(async () => {
  if (!guardActive) return;
  guardActive = false;
  expect(guardBlocked, "unexpected browser HTTP(S) requests").toEqual([]);
});

// ---------------------------------------------------------------------------
// Instrumentation: one counting Stripe sentinel plus a MutationObserver that
// records insertions (#cardField, assigned-price label) as they happen. Both are
// installed before any navigation and never reset observed application calls.
// ---------------------------------------------------------------------------

type Activity = { stripeCalls: number; paymentMethodCalls: number; cardInsertions: number; priceInsertions: number };

declare global {
  interface Window {
    __journeyStripe: { constructorCalls: number; paymentMethodCalls: number };
    __journeyMutations: { cardInsertions: number; priceInsertions: number };
  }
}

async function instrument(context: BrowserContext) {
  await context.addInitScript(() => {
    const calls = { constructorCalls: 0, paymentMethodCalls: 0 };
    Object.defineProperty(window, "__journeyStripe", { value: calls, configurable: true });
    // A throwing constructor is the strongest sentinel: selected-beta tests fail
    // loudly if any code path tries to initialize Stripe.
    (window as unknown as { Stripe: unknown }).Stripe = function () {
      calls.constructorCalls += 1;
      throw new Error("selected beta must never initialize Stripe");
    };

    const mutations = { cardInsertions: 0, priceInsertions: 0 };
    Object.defineProperty(window, "__journeyMutations", { value: mutations, configurable: true });
    let card = false;
    let price = false;
    const sample = () => {
      const hasCard = document.querySelector("#cardField") !== null;
      const hasPrice = document.querySelector('[aria-label="Assigned certified mailing price"]') !== null;
      if (hasCard && !card) mutations.cardInsertions += 1;
      if (hasPrice && !price) mutations.priceInsertions += 1;
      card = hasCard;
      price = hasPrice;
    };
    document.addEventListener("DOMContentLoaded", () => {
      sample();
      new MutationObserver(sample).observe(document.documentElement, { childList: true, subtree: true });
    });
  });
}

async function activity(page: Page): Promise<Activity> {
  return page.evaluate(() => ({
    stripeCalls: window.__journeyStripe.constructorCalls,
    paymentMethodCalls: window.__journeyStripe.paymentMethodCalls,
    cardInsertions: window.__journeyMutations.cardInsertions,
    priceInsertions: window.__journeyMutations.priceInsertions,
  }));
}

async function call(page: Page, method: string, path: string, body?: unknown) {
  return page.evaluate(async ({ method, path, body }) => {
    const csrf = document.cookie.split("; ").find((part) => part.startsWith("__Host-pb-csrf="))?.split("=")[1] ?? "";
    const response = await fetch(`/api${path}`, {
      method,
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!response.ok) throw new Error(`synthetic request failed: ${method} ${path} ${response.status}`);
    return response.json();
  }, { method, path, body });
}

function forward(route: Route) {
  const url = new URL(route.request().url());
  if (url.origin !== secureUrl().origin) throw new Error("Forwarding requires the canonical secure origin");
  const browser = route.request().frame().page().context().browser();
  // Firefox's APIRequestContext inherits the owned CONNECT proxy. Its allowlist
  // accepts only the canonical authority, never the loopback transport URL.
  if (browser?.browserType().name() === "firefox" && process.env.PLAYWRIGHT_FIREFOX_PROXY) {
    return route.fetch();
  }
  const transport = transportUrl();
  if (!transport) return route.fetch();
  url.protocol = transport.protocol;
  url.host = transport.host;
  return route.fetch({ url: url.href });
}

function syntheticPng(value: number, signature = false) {
  const crc = (buf: Buffer) => {
    let c = ~0;
    for (const b of buf) { c ^= b; for (let k = 0; k < 8; k++) c = (c >>> 1) ^ (0xedb88320 & -(c & 1)); }
    return ~c >>> 0;
  };
  const chunk = (type: string, data: Buffer) => {
    const t = Buffer.from(type);
    const out = Buffer.alloc(12 + data.length);
    out.writeUInt32BE(data.length, 0); t.copy(out, 4); data.copy(out, 8);
    out.writeUInt32BE(crc(Buffer.concat([t, data])), 8 + data.length);
    return out;
  };
  const ihdr = Buffer.from([0, 0, 0, 2, 0, 0, 0, 2, 8, 2, 0, 0, 0]);
  const raw = Buffer.from([0, value, value, value, value, value, value, 0, value, value, value, value, value, value]);
  // A uniform rectangle is correctly rejected as an empty signature. Keep
  // this synthetic ink distinct from its background through image sanitizing.
  if (signature) raw.fill(255, 4, 7);
  return Buffer.concat([
    Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]),
    chunk("IHDR", ihdr), chunk("IDAT", deflateSync(raw)), chunk("IEND", Buffer.alloc(0)),
  ]);
}


function fixture(name: "select" | "proof", caseId: string) {
  return JSON.parse(execFileSync(python, [resolve(__dirname, "beta-acceptance-fixtures.py"), "--action", name, "--case-id", caseId], { encoding: "utf8", env: process.env }));
}
function proof(caseId: string) {
  return JSON.parse(execFileSync(python, [resolve(__dirname, "beta-full-journey-fixtures.py"), "--case-id", caseId], { encoding: "utf8", env: process.env }));
}
const ZERO: Activity = { stripeCalls: 0, paymentMethodCalls: 0, cardInsertions: 0, priceInsertions: 0 };
const hash = (bytes: Buffer | string) => createHash("sha256").update(bytes).digest("hex");

async function noPayment(page: Page, caseId?: string) {
  expect(await activity(page)).toEqual(ZERO);
  await expect(page.locator("#cardField")).toHaveCount(0);
  await expect(page.getByLabel("Assigned certified mailing price")).toHaveCount(0);
  expect(page.frames().filter(frame => /stripe/i.test(frame.url()))).toHaveLength(0);
  if (caseId) {
    const saved = proof(caseId);
    expect(saved.payment_authorization_rows).toBe(0);
    expect(saved.payment_operation_rows).toBe(0);
    expect(saved.payment_cursor_rows).toBe(0);
    if (saved.case) {
      expect(saved.case.payment_state).toBe("NO_CHARGE");
      expect(saved.case.amount_cents).toBe(0);
    }
    expect(saved.history.filter((item: { machine: string; event_name: string }) => item.machine === "payment" && /captur|refund|authoriz/i.test(item.event_name))).toEqual([]);
  }
}

// Same installed axe-core and WCAG tags as t37-support.accessible; that module also
// eagerly reads historical image fixtures, absent in a clean source-only rehearsal.
async function accessible(page: Page, name: string) {
  await page.addScriptTag({ path: require.resolve("axe-core/axe.min.js") });
  const result = await page.evaluate(async () => {
    const axe = (window as unknown as { axe: { run: (context: Document, options: object) => Promise<{ violations: unknown[]; passes: unknown[] }> } }).axe;
    const scan = await axe.run(document, { runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"] } });
    return { violations: scan.violations, passes: scan.passes.length };
  });
  expect(result.passes).toBeGreaterThan(0);
  const directory = resolve(process.env.T37_EVIDENCE_DIR ?? "test-results/beta-full-journey");
  mkdirSync(directory, { recursive: true });
  writeFileSync(resolve(directory, `${name}-axe.json`), JSON.stringify({ synthetic: true, standard: "axe-core WCAG 2.1 A/AA", ...result }, null, 2));
  expect(result.violations, `axe violations at ${name}`).toEqual([]);
}

// Reach a control with actual sequential Tab navigation from the page's focused
// heading. No locator.focus() or fabricated keyboard events are used.
async function tabTo(page: Page, selector: string) {
  for (let step = 0; step < 80; step++) {
    await page.keyboard.press("Tab");
    if (await page.locator(selector).evaluate(element => document.activeElement === element)) return;
  }
  throw new Error(`Tab path did not reach ${selector}`);
}

async function workerStep(id: string, step: string) {
  if (!privateApi) throw new Error("INGRESS_API_BASE is required");
  const api = parseOrigin(privateApi, "Private API base");
  if (api.protocol !== "http:" || !loopback(api.hostname)) {
    throw new Error("Private API base must be an HTTP loopback origin");
  }
  // This is a Node test-controller action, outside the browser's CONNECT proxy.
  const response = await fetch(`${api.origin}/internal/cases/${encodeURIComponent(id)}/step`, {
    method: "POST",
    headers: { Authorization: `Bearer ${worker}`, "Content-Type": "application/json" },
    body: JSON.stringify({ step_id: step }),
    redirect: "error",
    signal: AbortSignal.timeout(30_000),
  });
  const body = await response.text();
  expect(response.status, body).toBe(200);
  return JSON.parse(body);
}

const VISION_LITERAL_FIELDS = ["citation_number","license_plate","license_plate_state","issuer_name","issue_date","notice_date","printed_deadline","violation_text","issued_city","issued_state","stage_text"] as const;

// Required ticket-reading mode: the same real customer path, through the actual
// upload -> unverified reading -> explicit facts -> comparison -> confirmation UI.
async function visionTicket(page: Page) {
  await page.locator("#visionFrontPhoto").setInputFiles({ name: "synthetic-ticket-front.png", mimeType: "image/png", buffer: syntheticPng(200) });
  await page.locator("#visionBackPhoto").setInputFiles({ name: "synthetic-ticket-back.png", mimeType: "image/png", buffer: syntheticPng(150) });
  // Uploads establish the anonymous session asynchronously; authenticated polls follow it.
  await expect.poll(() => page.evaluate(async () => (await fetch("/api/auth/session")).status)).toBe(200);
  await expect.poll(async () => (await call(page, "GET", "/auth/appeal"))?.intake_form?.ticket_back_photo_ref).toBeTruthy();
  await page.locator("#visionReadBtn").click();
  await expect(page.locator("#visionSuggestions")).toBeVisible();
  const issueDate = new Date(Date.now() - 3 * 86400000).toISOString().slice(0, 10);
  const facts: Record<string, string> = {
    citation_number: "SYN-BETA-JOURNEY",
    license_plate: "SYN123",
    license_plate_state: "CA",
    issuer_hint: "San Francisco Municipal Transportation Agency",
    issued_city: "San Francisco",
    issued_state: "CA",
    violation_code: "METER EXPIRED",
    violation_description: "Meter expired",
    issue_date: issueDate,
    stage: "first-level review",
  };
  for (const [key, value] of Object.entries(facts)) await page.locator(`#fact-${key}`).fill(value);
  await page.locator("#fact-issuer_kind-government").check();
  await expect.poll(async () => factDraftFromValues((await call(page, "GET", "/auth/appeal"))?.intake_form?.values ?? {})).toMatchObject({ ...facts, issuer_kind: "GOVERNMENT" });
  await noPayment(page);
  await page.reload();
  for (const [key, value] of Object.entries(facts)) await expect(page.locator(`#fact-${key}`)).toHaveValue(value);
  for (const field of ["notice_date", "printed_deadline"]) {
    await expect(page.locator(`#fact-${field}`)).toHaveValue("");
    await expect(page.locator(`#fact-${field}-absent`)).not.toBeChecked();
  }
  await page.locator("#fact-notice_date-absent").check();
  await page.locator("#fact-printed_deadline-absent").check();
  await page.locator("#visionReviewBtn").click();
  await expect(page.locator("#visionReview")).toContainText("No AI comparison was performed.");
  for (const field of VISION_LITERAL_FIELDS) {
    const select = page.locator(`#resolve-${field}`);
    if (await select.count()) await select.selectOption("ABSENCE_ACKNOWLEDGED");
  }
  await page.locator("#visionAccuracyAcknowledged").check();
  await expect(page.locator("#visionConfirmBtn")).toBeEnabled();
  await page.locator("#visionConfirmBtn").click();
  await expect(page.locator("#visionConfirmed")).toBeVisible();
  await page.locator("#actContinueTicket").click();
  await expect.poll(async () => (await call(page, "GET", "/auth/appeal"))?.intake_form?.step).toBe(2);
  const saved = await call(page, "GET", "/auth/appeal");
  expect(saved.ticket_reading_required).toBe(true);
  expect(saved.intake_form.ticket_fact_binding.adopted).toEqual({ ...facts, issuer_kind: "GOVERNMENT", notice_date: null, printed_deadline: null });
  await page.route("**/api/workspaces/*/ticket-reading", (route) => route.request().method() === "GET" ? route.abort("connectionreset") : route.fallback());
  await noPayment(page);
  await page.reload();
  await expect(page.locator("#visionRetryCheck")).toBeVisible();
  await expect(page.locator("#ticketVision")).toBeVisible();
  await expect(page.locator("#story")).toHaveCount(0);
  await expect(page.locator("#actContinueTicket")).toHaveCount(0);
  await expect.poll(async () => (await call(page, "GET", "/auth/appeal"))?.intake_form?.step).toBe(1);
  expect((await call(page, "GET", "/auth/appeal")).intake_form.ticket_fact_binding).toEqual(saved.intake_form.ticket_fact_binding);
  await page.unroute("**/api/workspaces/*/ticket-reading");
  await page.locator("#visionRetryCheck").click();
  await expect(page.locator("#visionConfirmed")).toBeVisible();
  await page.locator("#actContinueTicket").click();
}

async function startCustomer(page: Page) {
  const savedForms: IntakeForm[] = [];
  const saveStatuses: number[] = [];
  page.on("request", (request) => {
    if (request.method() === "PUT" && new URL(request.url()).pathname.endsWith("/intake-form")) savedForms.push(request.postDataJSON().form);
  });
  page.on("response", (response) => {
    if (response.request().method() === "PUT" && new URL(response.url()).pathname.endsWith("/intake-form")) saveStatuses.push(response.status());
  });
  await page.goto(base!);
  expect((await call(page, "GET", "/health")).mode).toBe("fake");
  await page.locator("#actSnap").click();
  // The capability is discovered after this appeal's workspace exists. Preserve the
  // legacy manual path for fake previews and use the real reading path when required.
  await expect.poll(async () => (await page.locator("#ticketVision").count()) + (await page.locator("#fixIn").count()), { timeout: 15000 }).toBeGreaterThan(0);
  const visionRequired = (await page.locator("#ticketVision").count()) > 0;
  if (visionRequired) {
    await visionTicket(page);
  } else {
    for (const value of ["SYN-BETA-JOURNEY", new Date(Date.now() - 3 * 86400000).toLocaleDateString("en-US"), "Meter expired", "San Francisco Municipal Transportation Agency"]) {
      await page.locator("#fixIn").fill(value);
      await page.locator("#actSaveFix").click();
    }
    await page.locator("#plateIn").fill("SYN123");
    await page.locator("#plateState").fill("CA");
    await page.locator("#actContinueTicket").click();
  }
  await page.locator("#story").fill("The sign was covered. The kiosk was blank.");
  await page.locator("#actSendStory").click();
  await page.getByRole("button", { name: /^Yes/ }).first().click();
  await expect(page.locator("#liveDetails")).toBeVisible();
  for (const [id, value] of Object.entries({ liveIssuedCity: "San Francisco", liveIssuedState: "CA", liveName: "Synthetic Customer", livePhone: "415-555-0100", liveEmail: "beta-journey@example.test", liveLine1: "123 Example Street", liveCity: "San Francisco", liveState: "CA", liveZip: "94103" })) {
    if (await page.locator(`#${id}`).count()) await page.locator(`#${id}`).fill(value);
  }
  await page.locator("#liveRentalPaid").selectOption("not-rental");
  if (!visionRequired) await page.locator("#liveTicketPhoto").setInputFiles({ name: "synthetic-ticket.png", mimeType: "image/png", buffer: syntheticPng(200) });
  // Upload/autosave creates the anonymous session through the real customer
  // path. Wait for that request before polling an authenticated resource.
  await expect.poll(() => page.evaluate(async () => (await fetch("/api/auth/session")).status)).toBe(200);
  await expect.poll(async () => (await call(page, "GET", "/auth/appeal"))?.intake_form?.ticket_photo_ref).toBeTruthy();
  await page.locator("#liveEvidencePhoto").setInputFiles({ name: "synthetic-supporting.png", mimeType: "image/png", buffer: syntheticPng(80) });
  await expect.poll(async () => (await call(page, "GET", "/auth/appeal"))?.intake_form?.evidence_photo_refs?.length).toBe(1);
  await page.locator("#liveSubmit").click();
  let caseId = "";
  await expect.poll(async () => { caseId = (await call(page, "GET", "/auth/appeal"))?.case_id ?? ""; return caseId; }).toMatch(/^case_/);
  expect(savedForms.length, "actual autosave/flush requests observed").toBeGreaterThan(0);
  for (const form of savedForms) expect(Object.keys(form.values ?? {}).length).toBeLessThanOrEqual(30);
  expect(saveStatuses.length).toBeGreaterThan(0);
  expect(saveStatuses.every((status) => status === 200), "autosave/flush must be accepted").toBe(true);
  expect(fixture("select", caseId).selected).toEqual([caseId]);
  await page.reload();
  await expect(page.locator("#h-s4")).toHaveText("Your selected beta appeal is free.");
  return caseId;
}

for (const recovery of [false, true]) {
  test(recovery
    ? "free beta loses signed approval response, retries exact intent, reloads and mails only once"
    : "free beta customer completes intake, disclosure, research, signed packet, tracking and original record", async ({ page, context, browser }, testInfo) => {
    expect(process.env.PB_SERVICE_PHASE).toBe("controlled_free_beta");
    await instrument(context);
    // Sentinels survive navigation in the test process; document counters are also
    // checked before every reload, so a later clean document cannot hide activity.
    const forbidden: string[] = [];
    page.on("request", request => {
      const url = new URL(request.url());
      if (url.hostname.endsWith("stripe.com") || url.hostname.endsWith("stripe.network") || /\/(authorization|tier|capture|refund)(\/|$)|payment_method/.test(url.pathname)) forbidden.push(url.pathname);
    });
    await page.route("**/dev-worker/**", async route => {
      const match = /^\/dev-worker\/(case_[a-f0-9]+)\/(research|draft|dispatch)$/.exec(new URL(route.request().url()).pathname);
      if (!match || route.request().method() !== "POST") return route.fallback();
      await route.fulfill({ status: 200, json: await workerStep(match[1], match[2]) });
    });
    const id = await startCustomer(page);
    const offer = await call(page, "GET", `/cases/${id}/beta-offer`);
    await expect(page.locator("#betaDisclosureText")).toHaveText(offer.disclosure_text);
    await expect(page.locator("#betaDisclosureText")).toContainText(/development|errors/i);
    await expect(page.locator("#betaOfferCard")).toContainText("$0");
    await expect(page.locator("#betaConsent")).not.toBeChecked();
    await expect(page.locator("#actBetaBtn")).toBeDisabled();
    await noPayment(page, id);
    if (!recovery) await accessible(page, "full-beta-offer");
    await tabTo(page, "#betaConsent");
    await page.keyboard.press("Space");
    await expect(page.locator("#betaConsent")).toBeChecked();
    await tabTo(page, "#actBetaBtn");
    const acceptance = page.waitForRequest(request => request.url().endsWith("/beta-acceptance") && request.method() === "POST");
    await page.keyboard.press("Enter");
    expect((await acceptance).postDataJSON()).toEqual({ terms_accepted: true, disclosure_sha256: offer.disclosure_sha256 });
    await expect(page.locator("#h-s5")).toContainText("Free beta accepted");
    const accepted = fixture("proof", id);
    expect(accepted.accepted_at).toBeTruthy();
    await noPayment(page, id);
    await workerStep(id, "research");
    await page.reload();
    await expect(page.locator("#liveFindings")).toBeVisible();
    await expect(page.locator("#liveSources")).toContainText("https://sfmta.gov/");
    await expect(page.locator("#liveFindings")).toContainText("21 calendar days");
    await expect(page.locator("#liveFindings")).toContainText("11 South Van Ness");
    await noPayment(page, id);
    await page.locator("#liveDraftBtn").click();
    await expect(page.locator("#liveLetterText")).toBeVisible();
    const ownWords = `${await page.locator("#liveLetterText").inputValue()}\nMy exact additional account: the sign was covered when I parked.`;
    await page.locator("#liveLetterText").fill(ownWords);
    await expect.poll(async () => (await call(page, "GET", `/cases/${id}/draft`)).text).toBe(ownWords);
    await noPayment(page, id);
    await page.reload();
    await expect(page.locator("#liveLetterText")).toHaveValue(ownWords);
    await page.locator("#liveAdopt").click();
    const adopted = await call(page, "GET", `/cases/${id}/draft`);
    expect(adopted.adopted_sha256).toBe(hash(ownWords));
    await page.locator("#liveSignatureFile").setInputFiles({ name: "own-synthetic-signature.png", mimeType: "image/png", buffer: syntheticPng(40, true) });
    const previewResponse = page.waitForResponse(response => response.url().endsWith("/packet-preview"));
    await page.locator("#livePreview").click();
    const previewResult = await previewResponse;
    expect(previewResult.status(), await previewResult.text()).toBe(200);
    let preview = await previewResult.json();
    await expect(page.locator("#livePacketPreview iframe")).toHaveAttribute("src", /^blob:/);
    const bytes = Buffer.from(await page.locator("#liveOpenPacket").evaluate(async link => Array.from(new Uint8Array(await (await fetch((link as HTMLAnchorElement).href)).arrayBuffer()))));
    expect(bytes.subarray(0, 5).toString()).toBe("%PDF-");
    expect(hash(bytes)).toBe(preview.packet_sha256);
    expect(bytes.toString("base64")).toBe(preview.pdf_base64);
    for (const line of [...preview.mail_to, ...preview.return_address]) await expect(page.locator("#livePacketPreview")).toContainText(line);
    await expect(page.locator("#liveApprove")).toBeDisabled();
    await expect(page.locator("#livePacketReviewed")).not.toBeChecked();
    if (!recovery) await accessible(page, "full-beta-exact-packet");
    await page.locator("#livePacketReviewed").check();
    if (!recovery) {
      const mailingRequests: string[] = [];
      page.on("request", request => {
        if (/\/(approval|submit)$/.test(new URL(request.url()).pathname)) mailingRequests.push(request.url());
      });
      await page.locator("#liveCorrectBeforeMail").click();
      await expect(page.locator("#h-s6")).toHaveText("Your letter is paused.");
      await expect(page.locator("#liveApprove")).toHaveCount(0);
      await page.reload();
      await expect(page.locator("#h-s6")).toHaveText("Your letter is paused.");
      await page.locator("#liveResumeLetter").click();
      await expect(page.locator("#livePacketPreview")).toHaveCount(0);
      await expect(page.locator("#liveApprove")).toHaveCount(0);
      expect(mailingRequests).toEqual([]);
      await page.locator("#liveSignatureFile").setInputFiles({ name: "own-synthetic-signature.png", mimeType: "image/png", buffer: syntheticPng(40, true) });
      const replacement = page.waitForResponse(response => response.url().endsWith("/packet-preview"));
      await page.locator("#livePreview").click();
      preview = await (await replacement).json();
      await expect(page.locator("#livePacketReviewed")).not.toBeChecked();
      await page.locator("#livePacketReviewed").check();
    }
    if (!recovery) {
      const liveNotice = page.locator("#livePacketPreview [data-customer-choice-notice]");
      await expect(liveNotice).toBeVisible();
      await expect(liveNotice).toContainText("Your chosen details aren’t covered by our correct-office refund guarantee if they cause the rejection.");
      await liveNotice.locator(".notice-rules-btn").click();
      const termsDlg = page.locator("#termsDlg");
      await expect(termsDlg).toBeVisible();
      await expect(termsDlg.locator('[data-policy-version="2026-11-16"]')).toContainText("Customer choices and our correct-office guarantee");
      await termsDlg.locator('button[aria-label="Close"]').click();
      await expect(termsDlg).not.toBeVisible();
      await expect(page.locator("#livePacketReviewed")).toBeChecked();
    }
    const approvalBodies: string[] = [];
    let lost = false;
    await page.route("**/api/cases/*/approval", async route => {
      approvalBodies.push(route.request().postData() ?? "");
      const response = await forward(route);
      expect(response.status()).toBe(200);
      if (recovery && !lost) { lost = true; await route.abort("connectionreset"); }
      else await route.fulfill({ response });
    });
    await page.locator("#liveApprove").click();
    if (recovery) {
      await expect(page.locator("#s6 [role=alert]")).toContainText("Approval may have been saved");
      await expect(page.locator("#liveEditAdopted")).toBeDisabled();
      const saved = proof(id);
      expect(saved.case.letter_state).toBe("FROZEN");
      await expect(page.locator("#liveApprove")).toHaveText("Retry this packet approval →");
      await page.locator("#liveApprove").click();
      await expect.poll(() => approvalBodies.length).toBe(2);
      expect(approvalBodies[1]).toBe(approvalBodies[0]);
    }
    const approved = JSON.parse(approvalBodies[0]);
    expect(approved.packet_sha256).toBe(preview.packet_sha256);
    expect(approved.envelope_sha256).toBe(preview.envelope_sha256);
    expect(approved.signature_ref).toMatch(/^photo_/);
    await expect(page.locator("#s7")).toBeVisible();
    await noPayment(page, id);
    await workerStep(id, "dispatch");
    await page.reload();
    await expect(page.locator("#liveTracking")).toBeVisible();
    const tracking = await call(page, "GET", `/cases/${id}/tracking`);
    expect(tracking.case_id).toBe(id);
    expect(tracking.tracking_number).toMatch(/^9407/);
    await expect(page.locator("#liveTracking")).toContainText(tracking.tracking_number);
    await expect(page.locator("#s7")).toContainText(/free|no charge/i);
    await expect(page.locator("#s7")).toContainText("No second mailing is automatically authorized");
    await page.locator("#livePrepareMailingRecord").click();
    await expect(page.locator("#liveMailingRecord")).toBeVisible();
    const downloadEvent = page.waitForEvent("download");
    await page.locator("#liveMailingRecord").click();
    const download = await downloadEvent;
    expect(download.suggestedFilename()).toBe("parkingbreaker-mailing-record.pdf");
    const downloadPath = await download.path();
    expect(downloadPath).toBeTruthy();
    expect(hash(readFileSync(downloadPath!))).toBe(preview.packet_sha256);
    const record = await call(page, "GET", `/cases/${id}/mailing-record`);
    expect(record.pdf_sha256).toBe(preview.packet_sha256);
    expect(record.tracking_number).toBe(tracking.tracking_number);
    if (!recovery) await accessible(page, "full-beta-mailing-record");
    await noPayment(page, id);
    await workerStep(id, "dispatch");
    await page.locator("#liveApprove").click(); // real owner "Check mailing status" control
    await noPayment(page, id);
    await page.reload();
    await expect(page.locator("#liveTracking")).toBeVisible();
    expect(fixture("proof", id).accepted_at).toBe(accepted.accepted_at);
    const durable = proof(id);
    expect(durable.mail_attempts).toHaveLength(1);
    expect(durable.case.attempt).toBe(1);
    expect(durable.mail_attempts[0].pdf_sha256).toBe(preview.packet_sha256);
    expect(durable.mail_attempts[0].stored_bytes_sha256).toBe(preview.packet_sha256);
    expect(durable.submission_operations.filter((op: { kind: string }) => op.kind === "create")).toHaveLength(1);
    expect(durable.submission_operations.every((op: { attempt: number }) => op.attempt === 1)).toBe(true);
    const stranger = await browser.newContext({ ignoreHTTPSErrors: true });
    try {
      const foreign = await stranger.newPage();
      // Same reserved origin, no provider navigation. Authenticate a distinct owner.
      await foreign.goto(base!);
      expect(await foreign.evaluate(async id => (await fetch(`/api/cases/${id}/mailing-record`)).status, id)).toBe(401);
      await call(foreign, "POST", "/auth/appeals", {});
      for (const resource of ["status", "draft", "tracking", "mailing-record", "beta-offer"]) {
        expect(await foreign.evaluate(async ({ id, resource }) => (await fetch(`/api/cases/${id}/${resource}`)).status, { id, resource })).toBe(404);
      }
    } finally { await stranger.close(); }
    await noPayment(page, id);
    expect(forbidden, "free customer journey touched a payment route").toEqual([]);
    await testInfo.attach("durable-free-journey-proof", { body: JSON.stringify({ synthetic: true, recovery, packet_sha256: preview.packet_sha256, ...durable }, null, 2), contentType: "application/json" });
  });
}
