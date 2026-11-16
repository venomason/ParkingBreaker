import { expect, test, type BrowserContext, type Page, type Route } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { resolve } from "node:path";
import { deflateSync } from "node:zlib";

// Real cookie-auth, fake-provider rehearsal only. The frozen synthetic selection
// helper and actual API/owner store are used; no successful notice response is mocked.
const base = process.env.SECURE_BASE;
test.use({ ignoreHTTPSErrors: true });
test.skip(!base, "requires the isolated HTTPS cookie-auth synthetic rehearsal");

const python = process.env.BETA_FIXTURE_PYTHON ?? resolve(__dirname, "../../backend/.venv/bin/python");
const helper = resolve(__dirname, "beta-acceptance-fixtures.py");

// ---------------------------------------------------------------------------
// Exact reserved HTTPS + explicit loopback network guard.
// ---------------------------------------------------------------------------

function loopback(hostname: string) {
  return hostname === "127.0.0.1" || hostname === "localhost" || hostname === "[::1]" || hostname === "::1";
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

let unexpectedEgress: string[] = [];
let expectedBlocked: string[] = [];
const guardedContexts = new Map<BrowserContext, (route: Route) => Promise<void>>();

async function installNetworkGuard(context: BrowserContext) {
  const allowed = new Set([secureUrl().origin]);
  const transport = transportUrl();
  if (transport) allowed.add(transport.origin);
  const handler = async (route: Route) => {
    const url = new URL(route.request().url());
    if (["http:", "https:"].includes(url.protocol) && !allowed.has(url.origin)) {
      unexpectedEgress.push(`${url.origin}${url.pathname}`);
      await route.abort("blockedbyclient");
      return;
    }
    await route.fallback();
  };
  await context.route("**/*", handler);
  guardedContexts.set(context, handler);
  context.on("close", () => guardedContexts.delete(context));
}

test.beforeEach(async ({ context }) => {
  unexpectedEgress = [];
  expectedBlocked = [];
  await installNetworkGuard(context);
});

test.afterEach(async () => {
  for (const [context, handler] of guardedContexts) await context.unroute("**/*", handler);
  guardedContexts.clear();
  expect(unexpectedEgress, "blocked browser HTTP(S) requests, including the explicit negative control").toEqual(expectedBlocked);
});

// ---------------------------------------------------------------------------
// Effective Stripe sentinel plus card/price mutation observer, installed before
// the first tested-document navigation. Counters are never reset by the app.
// ---------------------------------------------------------------------------

type Activity = { stripeCalls: number; paymentMethodCalls: number; cardInsertions: number; priceInsertions: number };

declare global {
  interface Window {
    __f03Stripe: { constructorCalls: number; paymentMethodCalls: number };
    __f03Mutations: { cardInsertions: number; priceInsertions: number };
  }
}

async function instrument(context: BrowserContext) {
  await context.addInitScript(() => {
    const calls = { constructorCalls: 0, paymentMethodCalls: 0 };
    Object.defineProperty(window, "__f03Stripe", { value: calls, configurable: true });
    // A throwing constructor is an effective sentinel: any paid path fails loudly
    // and increments the count before this free-beta page can proceed.
    (window as unknown as { Stripe: unknown }).Stripe = function () {
      calls.constructorCalls += 1;
      throw new Error("accepted free beta must never initialize Stripe");
    };

    const mutations = { cardInsertions: 0, priceInsertions: 0 };
    Object.defineProperty(window, "__f03Mutations", { value: mutations, configurable: true });
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

function activity(page: Page): Promise<Activity> {
  return page.evaluate(() => ({
    stripeCalls: window.__f03Stripe.constructorCalls,
    paymentMethodCalls: window.__f03Stripe.paymentMethodCalls,
    cardInsertions: window.__f03Mutations.cardInsertions,
    priceInsertions: window.__f03Mutations.priceInsertions,
  }));
}

const ZERO: Activity = { stripeCalls: 0, paymentMethodCalls: 0, cardInsertions: 0, priceInsertions: 0 };

// ---------------------------------------------------------------------------
// Real intake and read-only synthetic selection helper (same as the frozen suite).
// ---------------------------------------------------------------------------

function fixtures(action: "select", caseId: string): Record<string, unknown> {
  const raw = execFileSync(python, [helper, "--action", action, "--case-id", caseId], {
    env: process.env,
    encoding: "utf8",
  });
  return JSON.parse(raw);
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
  if (!transport) throw new Error("SECURE_TRANSPORT_BASE is required");
  url.protocol = transport.protocol;
  url.host = transport.host;
  return route.fetch({ url: url.href });
}

function syntheticPng(value: number) {
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
  return Buffer.concat([
    Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]),
    chunk("IHDR", ihdr), chunk("IDAT", deflateSync(raw)), chunk("IEND", Buffer.alloc(0)),
  ]);
}

function pngFile(name: string, value: number) {
  return { name, mimeType: "image/png", buffer: syntheticPng(value) };
}

async function intakeCase(page: Page): Promise<{ caseId: string; workspaceId: string }> {
  await page.goto(base!);
  await call(page, "POST", "/auth/appeals", {});
  const workspace = await call(page, "POST", "/workspaces", { notes: "synthetic F03 notice" }) as { workspace_id: string; draft_revision?: number; ticket_reading_required?: boolean };
  const photo = await call(page, "POST", `/workspaces/${workspace.workspace_id}/photos`, {
    content_type: "image/png", data_base64: syntheticPng(200).toString("base64"),
  }) as { photo_ref: string };
  const issueDate = new Date(Date.now() - 3 * 86400000).toISOString().slice(0, 10);
  const ticket = {
    citation_number: "SYN-F03-1", license_plate: "SYN303", license_plate_state: "CA",
    issuer_hint: "San Francisco Municipal Transportation Agency", issued_city: "San Francisco",
    issued_state: "CA", issuer_kind: "GOVERNMENT", violation_code: "Meter expired",
    violation_description: "Meter expired", issue_date: issueDate, stage: "first-level review",
    notice_date: null, printed_deadline: null,
  };
  let ticketConfirmationCommandId: string | undefined;
  const readingStatus = await call(page, "GET", `/workspaces/${workspace.workspace_id}/ticket-reading`) as { required?: boolean };
  if (workspace.ticket_reading_required || readingStatus.required) {
    const saved = await call(page, "PUT", `/workspaces/${workspace.workspace_id}/intake-form`, {
      expected_revision: workspace.draft_revision ?? 1,
      form: {
        values: {},
        ticket_photo_ref: photo.photo_ref,
        ticket_fact_binding: {
          // Customer entry does not invent image-extraction provenance.
          literal: Object.fromEntries([
            "citation_number", "license_plate", "license_plate_state", "issuer_name",
            "issue_date", "notice_date", "printed_deadline", "violation_text",
            "issued_city", "issued_state", "stage_text",
          ].map(field => [field, null])),
          adopted: ticket,
        },
      },
    }) as { draft_revision?: number };
    const reviewRequestId = `req-${workspace.workspace_id}`;
    const review = await call(page, "POST", `/workspaces/${workspace.workspace_id}/ticket-review`, {
      request_id: reviewRequestId, expected_revision: saved.draft_revision ?? 2, customer_supplied: true,
    }) as { basis: string; fields: Array<{ field: string; comparison: string }> };
    expect(review.basis).toBe("customer-supplied facts; no model comparison");
    expect(review.fields).toEqual([]);
    const resolved_fields: Record<string, string> = {};
    for (const item of review.fields) {
      if (item.comparison === "conflict") resolved_fields[item.field] = "CONFIRMED_VALUE_STANDS";
      else if (item.comparison === "not_shown") resolved_fields[item.field] = "ABSENCE_ACKNOWLEDGED";
      else if (item.comparison === "unreadable") resolved_fields[item.field] = "UNREADABLE_ACKNOWLEDGED";
    }
    ticketConfirmationCommandId = `cmd-${workspace.workspace_id}`;
    await call(page, "POST", `/workspaces/${workspace.workspace_id}/ticket-confirmation`, {
      command_id: ticketConfirmationCommandId, expected_revision: saved.draft_revision ?? 2,
      review_request_id: reviewRequestId, expected_confirmation_generation: 0, accuracy_acknowledged: true,
      resolved_fields,
    });
  }
  const receipt = await call(page, "POST", `/workspaces/${workspace.workspace_id}/intake`, {
    ticket,
    contact: {
      name: "Synthetic Customer", phone: "415-555-0100", email: "f03@example.test",
      mailing_address: { line1: "123 Example Street", city: "San Francisco", state: "CA", zip_code: "94103" },
    },
    customer_position: { statement: "I received a notice after the initial review." },
    citation_photo_ref: photo.photo_ref, rental_company_paid: "NOT_RENTAL",
    ...(ticketConfirmationCommandId ? { ticket_confirmation_command_id: ticketConfirmationCommandId } : {}),
  }) as { case_id: string };
  return { caseId: receipt.case_id, workspaceId: workspace.workspace_id };
}

/** Real selected+accepted free case; the fake server then reports NO_CHARGE. */
async function acceptedCase(page: Page): Promise<{ caseId: string; workspaceId: string }> {
  const ids = await intakeCase(page);
  fixtures("select", ids.caseId);
  const offer = await call(page, "GET", `/cases/${ids.caseId}/beta-offer`) as { disclosure_sha256: string };
  expect(offer.disclosure_sha256).toMatch(/^[a-f0-9]{64}$/);
  await call(page, "POST", `/cases/${ids.caseId}/beta-acceptance`, {
    terms_accepted: true, disclosure_sha256: offer.disclosure_sha256,
  });
  const proof = await call(page, "GET", `/cases/${ids.caseId}/status`) as { payment_state: string };
  expect(proof.payment_state).toBe("NO_CHARGE");
  await page.reload();
  await expect(page.locator("#betaNoticesCard")).toBeVisible({ timeout: 20000 });
  return ids;
}

async function openNotices(page: Page) {
  const toggle = page.locator("#betaNoticesToggle");
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  await expect(page.locator("#betaNoticesBody")).toBeVisible();
}

function watchPaid(page: Page) {
  const paid: string[] = [];
  const stripe: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.hostname.endsWith("stripe.com") || url.hostname.endsWith("stripe.network")) stripe.push(url.href);
    if (/(\/authorization|\/tier)$/.test(url.pathname) || /payment_method/.test(url.pathname)) paid.push(url.pathname);
  });
  return { paid, stripe };
}

async function expectNoPaidPath(page: Page, traffic: { paid: string[]; stripe: string[] }) {
  expect(traffic.paid, "free beta used a paid route").toEqual([]);
  expect(traffic.stripe, "free beta attempted Stripe").toEqual([]);
  expect(await activity(page)).toEqual(ZERO);
  expect(page.frames().filter((frame) => /stripe/i.test(frame.url())).length).toBe(0);
}

async function naturalWidth(page: Page, imageIndex: number) {
  return page.locator("#betaNoticeReceipts img").nth(imageIndex).evaluate((element) => (element as HTMLImageElement).naturalWidth);
}

function imagePath(caseId: string, noticeId: string, pageNumber: number) {
  return `/api/cases/${caseId}/notices/${noticeId}/pages/${pageNumber}`;
}

async function readImage(page: Page, path: string) {
  return page.evaluate(async (path) => {
    const response = await fetch(path, { cache: "no-store", credentials: "same-origin" });
    return {
      status: response.status,
      contentType: response.headers.get("content-type"),
      cacheControl: response.headers.get("cache-control"),
    };
  }, path);
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

test("free accepted case saves two ordered notice images and reloads owned private previews", async ({ page, context }, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await instrument(context);
  const { caseId } = await acceptedCase(page);
  const traffic = watchPaid(page);
  await expectNoPaidPath(page, traffic);

  await openNotices(page);
  await expect(page.locator("#betaNoticesEmpty")).toBeVisible();

  await page.locator("#betaNoticeFiles").setInputFiles([
    pngFile("envelope.png", 30),
    pngFile("notice-page.png", 180),
  ]);
  await expect(page.locator("#betaNoticeFilesList [data-selected-id]")).toHaveCount(2);
  await page.getByLabel("Role for image 1").selectOption("envelope");
  await page.getByLabel("Role for image 2").selectOption("page");
  await page.locator("#betaNoticeSeenOn").fill("2026-10-02");
  await page.locator("#betaNoticePrintedOn").fill("2026-10-01");
  const deadline = 'Hearing requested by 11/15/2026 — <b>not verified</b> & "literal"';
  await page.locator("#betaNoticeDeadline").fill(deadline);
  await page.locator("#betaNoticeSave").click();

  await expect(page.locator("#betaNoticeSaved")).toContainText("unreviewed");
  const receipt = page.locator("#betaNoticeReceipts [data-notice-id]").first();
  await expect(receipt).toBeVisible();
  await expect(receipt).toContainText("Envelope · image 1 of 2");
  await expect(receipt).toContainText("Page · image 2 of 2");
  await expect(receipt.getByText("Saved", { exact: true })).toBeVisible();
  await expect(receipt.getByText("Unreviewed", { exact: true })).toBeVisible();
  await expect(receipt.getByText("Needs confirmation", { exact: true })).toBeVisible();
  await expect(receipt.locator("dd").nth(0)).toHaveText("2026-10-02");
  await expect(receipt.locator("dd").nth(1)).toHaveText("2026-10-01");
  await expect(receipt.locator("dd").nth(2)).toHaveText(deadline);

  const images = receipt.locator("img");
  await expect(images).toHaveCount(2);
  await expect(images.nth(0)).toHaveAttribute("alt", "Envelope image, page 1 of 2");
  await expect(images.nth(1)).toHaveAttribute("alt", "Page image, page 2 of 2");
  const firstSource = await images.nth(0).getAttribute("src");
  expect(firstSource).toContain(`/api/cases/${caseId}/notices/`);
  expect(firstSource).toContain("/pages/1");
  expect(firstSource).not.toContain("/_next/image");
  await expect.poll(() => naturalWidth(page, 0)).toBeGreaterThan(0);
  await expect.poll(() => naturalWidth(page, 1)).toBeGreaterThan(0);
  await page.screenshot({ path: testInfo.outputPath("beta-notices-desktop.png"), fullPage: true });

  const listing = await call(page, "GET", `/cases/${caseId}/notices`) as {
    notices: Array<{ notice_id: string; pages: Array<{ page_number: number; role: string }> }>;
    next_cursor: string | null;
  };
  expect(listing.notices).toHaveLength(1);
  expect(listing.notices[0].pages).toEqual([
    { page_number: 1, role: "envelope" },
    { page_number: 2, role: "page" },
  ]);
  expect(listing.next_cursor).toBeNull();
  await expectNoPaidPath(page, traffic);

  // Reload lists the owned server receipts and serves both images again.
  await page.reload();
  await expect(page.locator("#betaNoticesCard")).toBeVisible({ timeout: 20000 });
  await openNotices(page);
  await expect(page.locator("#betaNoticeReceipts [data-notice-id]")).toHaveCount(1);
  await expect.poll(() => naturalWidth(page, 0)).toBeGreaterThan(0);
  await expect.poll(() => naturalWidth(page, 1)).toBeGreaterThan(0);
  await expectNoPaidPath(page, traffic);
});

test("a delayed empty list cannot erase a notice saved after that list was read", async ({ page, context }) => {
  await instrument(context);
  const { caseId } = await acceptedCase(page);
  const traffic = watchPaid(page);
  let captured!: () => void;
  const capturedResponse = new Promise<void>((resolve) => { captured = resolve; });
  let release!: () => void;
  const releaseResponse = new Promise<void>((resolve) => { release = resolve; });
  let delivered!: () => void;
  const deliveredResponse = new Promise<void>((resolve) => { delivered = resolve; });
  let first = true;
  await page.route(`**/api/cases/${caseId}/notices?*`, async (route) => {
    if (route.request().method() !== "GET" || !first) return route.fallback();
    first = false;
    const response = await forward(route);
    expect(response.status()).toBe(200);
    expect((await response.json()).notices).toEqual([]);
    captured();
    await releaseResponse;
    await route.fulfill({ response });
    delivered();
  });
  try {
    await openNotices(page);
    await capturedResponse;
    await page.locator("#betaNoticeFiles").setInputFiles([pngFile("after-list.png", 74)]);
    await page.getByLabel("Role for image 1").selectOption("page");
    await page.locator("#betaNoticeSave").click();
    await expect(page.locator("#betaNoticeSaved")).toBeVisible();
    await expect(page.locator("#betaNoticeReceipts [data-notice-id]")).toHaveCount(1);
    release();
    await deliveredResponse;
    await page.evaluate(() => new Promise<void>((resolve) => {
      requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
    }));
    await expect(page.locator("#betaNoticesLoading")).toHaveCount(0);
    await expect(page.locator("#betaNoticesEmpty")).toHaveCount(0);
    await expect(page.locator("#betaNoticeReceipts [data-notice-id]")).toHaveCount(1);
    await expectNoPaidPath(page, traffic);
  } finally {
    release();
  }
});

test("duplicate image rejection preserves details and removing the duplicate permits save", async ({ page, context }) => {
  await instrument(context);
  const { caseId } = await acceptedCase(page);
  await openNotices(page);
  let uploads = 0;
  page.on("request", (request) => {
    if (request.method() === "POST" && /\/workspaces\/[^/]+\/photos$/.test(new URL(request.url()).pathname)) uploads += 1;
  });
  const posted: Array<{ pages: Array<{ photo_ref: string }> }> = [];
  await page.route(`**/api/cases/${caseId}/notices`, async (route) => {
    if (route.request().method() === "POST") posted.push(route.request().postDataJSON());
    await route.fallback();
  });
  await page.locator("#betaNoticeFiles").setInputFiles([pngFile("original.png", 75), pngFile("duplicate.png", 75)]);
  await page.getByLabel("Role for image 1").selectOption("envelope");
  await page.getByLabel("Role for image 2").selectOption("page");
  await page.locator("#betaNoticeSeenOn").fill("2026-10-05");
  await page.locator("#betaNoticeDeadline").fill("Literal deadline pending review");
  const refused = page.waitForResponse((response) => response.url().endsWith(`/cases/${caseId}/notices`) && response.request().method() === "POST");
  await page.locator("#betaNoticeSave").click();
  expect((await refused).status()).toBe(422);
  await expect(page.locator("#betaNoticeServerError")).toBeVisible();
  await expect(page.locator("#betaNoticeFilesList [data-selected-id]")).toHaveCount(2);
  await expect(page.locator("#betaNoticeSeenOn")).toHaveValue("2026-10-05");
  await expect(page.locator("#betaNoticeDeadline")).toHaveValue("Literal deadline pending review");
  expect(posted).toHaveLength(1);
  expect(posted[0].pages[0].photo_ref).toBe(posted[0].pages[1].photo_ref);
  expect((await call(page, "GET", `/cases/${caseId}/notices`)).notices).toHaveLength(0);
  await page.getByRole("button", { name: "Remove image 2", exact: true }).click();
  await page.locator("#betaNoticeSave").click();
  await expect(page.locator("#betaNoticeSaved")).toBeVisible();
  expect(uploads).toBe(2);
  expect(posted).toHaveLength(2);
  expect(posted[1].pages).toHaveLength(1);
  expect((await call(page, "GET", `/cases/${caseId}/notices`)).notices).toHaveLength(1);
});

test("partial upload failure keeps the successful ref and resumes after replacing the bad image", async ({ page, context }) => {
  await instrument(context);
  const { caseId } = await acceptedCase(page);
  await openNotices(page);
  let uploads = 0;
  let metadataPosts = 0;
  page.on("request", (request) => {
    if (request.method() !== "POST") return;
    const path = new URL(request.url()).pathname;
    if (/\/workspaces\/[^/]+\/photos$/.test(path)) uploads += 1;
    if (path === `/api/cases/${caseId}/notices`) metadataPosts += 1;
  });
  await page.locator("#betaNoticeFiles").setInputFiles([
    pngFile("valid.png", 76),
    { name: "damaged.png", mimeType: "image/png", buffer: Buffer.from("not a PNG") },
  ]);
  await page.getByLabel("Role for image 1").selectOption("envelope");
  await page.getByLabel("Role for image 2").selectOption("page");
  await page.locator("#betaNoticePrintedOn").fill("2026-10-04");
  await page.locator("#betaNoticeDeadline").fill("Reply within the period printed on page 2");
  await page.locator("#betaNoticeSave").click();
  await expect(page.locator("#betaNoticeServerError")).toBeVisible();
  await expect(page.locator("#betaNoticeFilesList [data-selected-id]")).toHaveCount(2);
  await expect(page.locator("#betaNoticeFilesList [data-selected-id]").first()).toContainText("· uploaded");
  await expect(page.locator("#betaNoticePrintedOn")).toHaveValue("2026-10-04");
  await expect(page.locator("#betaNoticeDeadline")).toHaveValue("Reply within the period printed on page 2");
  expect(uploads).toBe(2);
  expect(metadataPosts).toBe(0);
  await page.getByRole("button", { name: "Remove image 2", exact: true }).click();
  await page.locator("#betaNoticeFiles").setInputFiles([pngFile("replacement.png", 77)]);
  await page.getByLabel("Role for image 2").selectOption("page");
  await page.locator("#betaNoticeSave").click();
  await expect(page.locator("#betaNoticeSaved")).toBeVisible();
  expect(uploads).toBe(3);
  expect(metadataPosts).toBe(1);
  const listing = await call(page, "GET", `/cases/${caseId}/notices`);
  expect(listing.notices).toHaveLength(1);
  expect(listing.notices[0].pages).toHaveLength(2);
  expect(listing.notices[0].reported_fields).toEqual({
    customer_seen_on: null, printed_on: "2026-10-04", stated_deadline_text: "Reply within the period printed on page 2",
  });
});

test("lost metadata answer after commit retries the same request and leaves one receipt", async ({ page, context }) => {
  await instrument(context);
  const { caseId } = await acceptedCase(page);
  const traffic = watchPaid(page);
  await openNotices(page);

  await page.locator("#betaNoticeFiles").setInputFiles([pngFile("lost-answer.png", 90)]);
  await page.getByLabel("Role for image 1").selectOption("envelope");
  await page.locator("#betaNoticeSeenOn").fill("2026-10-03");

  const posted: Record<string, unknown>[] = [];
  let loseAnswer = true;
  await page.route(`**/api/cases/${caseId}/notices`, async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    posted.push(route.request().postDataJSON() as Record<string, unknown>);
    if (!loseAnswer) return route.fallback();
    loseAnswer = false;
    const response = await forward(route);
    expect(response.status()).toBe(201);
    // The fake API committed the row before this deliberate lost answer.
    await route.abort("connectionreset");
  });

  await page.locator("#betaNoticeSave").click();
  await expect(page.locator("#betaNoticeUncertain")).toBeVisible({ timeout: 20000 });
  await expect(page.locator("#betaNoticeRetry")).toBeVisible();
  await expect(page.locator("#betaNoticeUncertain")).toContainText("same request");

  const committed = await call(page, "GET", `/cases/${caseId}/notices`) as {
    notices: Array<{ notice_id: string }>; next_cursor: string | null;
  };
  expect(committed.notices).toHaveLength(1);
  expect(posted).toHaveLength(1);

  await page.locator("#betaNoticeRetry").click();
  await expect(page.locator("#betaNoticeReceipts [data-notice-id]")).toHaveCount(1);
  await expect(page.locator("#betaNoticeUncertain")).toHaveCount(0);
  expect(posted).toHaveLength(2);
  expect(posted[1]).toEqual(posted[0]);
  expect(String(posted[1].request_id)).toBe(String(posted[0].request_id));
  await expect(page.locator(`[data-notice-id="${committed.notices[0].notice_id}"]`)).toBeVisible();

  await page.reload();
  await expect(page.locator("#betaNoticesCard")).toBeVisible({ timeout: 20000 });
  await openNotices(page);
  await expect(page.locator("#betaNoticeReceipts [data-notice-id]")).toHaveCount(1);
  const afterReload = await call(page, "GET", `/cases/${caseId}/notices`) as { notices: Array<{ notice_id: string }> };
  expect(afterReload.notices).toHaveLength(1);
  expect(afterReload.notices[0].notice_id).toBe(committed.notices[0].notice_id);
  expect(posted.every((body) => body.request_id === posted[0].request_id)).toBe(true);
  await expectNoPaidPath(page, traffic);
});

test("private notice images refuse anonymous and other-case access", async ({ page, browser }) => {
  const owner = await acceptedCase(page);
  const uploaded = await call(page, "POST", `/workspaces/${owner.workspaceId}/photos`, {
    content_type: "image/png", data_base64: syntheticPng(61).toString("base64"),
  }) as { photo_ref: string };
  const notice = await call(page, "POST", `/cases/${owner.caseId}/notices`, {
    request_id: `owner-notice-${Date.now()}`,
    pages: [{ role: "envelope", photo_ref: uploaded.photo_ref }],
    customer_seen_on: "2026-10-02",
  }) as { notice_id: string };
  const ownerPath = imagePath(owner.caseId, notice.notice_id, 1);

  const own = await readImage(page, ownerPath);
  expect(own.status).toBe(200);
  expect(own.contentType).toBe("image/png");
  expect(own.cacheControl).toBe("no-store");

  const anonymous = await browser.newContext({ ignoreHTTPSErrors: true });
  try {
    await installNetworkGuard(anonymous);
    const anonymousPage = await anonymous.newPage();
    await anonymousPage.goto(base!);
    const refused = await readImage(anonymousPage, ownerPath);
    expect(refused.status).toBe(401);
  } finally {
    await anonymous.close();
  }

  const otherContext = await browser.newContext({ ignoreHTTPSErrors: true });
  try {
    await installNetworkGuard(otherContext);
    const otherPage = await otherContext.newPage();
    const other = await acceptedCase(otherPage);
    const otherUpload = await call(otherPage, "POST", `/workspaces/${other.workspaceId}/photos`, {
      content_type: "image/png", data_base64: syntheticPng(62).toString("base64"),
    }) as { photo_ref: string };
    const otherNotice = await call(otherPage, "POST", `/cases/${other.caseId}/notices`, {
      request_id: `other-notice-${Date.now()}`,
      pages: [{ role: "page", photo_ref: otherUpload.photo_ref }],
    }) as { notice_id: string };
    const otherPath = imagePath(other.caseId, otherNotice.notice_id, 1);

    const otherReadsOwner = await readImage(otherPage, ownerPath);
    expect(otherReadsOwner.status).toBe(404);
    const ownerReadsOther = await readImage(page, otherPath);
    expect(ownerReadsOther.status).toBe(404);
    const ownAgain = await readImage(otherPage, otherPath);
    expect(ownAgain.status).toBe(200);
  } finally {
    await otherContext.close();
  }
});

test("unsupported, oversized and over-20 selections stay on the page with no upload or notice POST", async ({ page, context }) => {
  await instrument(context);
  await acceptedCase(page);
  await openNotices(page);
  const posts: string[] = [];
  page.on("request", (request) => {
    if (request.method() !== "POST") return;
    const pathname = new URL(request.url()).pathname;
    if (/\/workspaces\/[^/]+\/photos$/.test(pathname) || /\/cases\/[^/]+\/notices$/.test(pathname)) {
      posts.push(pathname);
    }
  });

  const selected = page.locator("#betaNoticeFilesList [data-selected-id]");
  const problems = page.locator("#betaNoticeProblems");

  await page.locator("#betaNoticeFiles").setInputFiles([{ name: "notice.txt", mimeType: "text/plain", buffer: Buffer.from("not an image") }]);
  await expect(selected).toHaveCount(1);
  await expect(problems).toContainText("not JPEG or PNG");
  await page.locator("#betaNoticeSave").click();
  await expect(page.locator("#betaNoticeFormError")).toBeVisible();
  await page.waitForTimeout(300);
  expect(posts).toEqual([]);
  await expect(selected).toHaveCount(1);
  await page.locator("#betaNoticeRemoveAll").click();
  await expect(selected).toHaveCount(0);

  await page.locator("#betaNoticeFiles").setInputFiles([{
    name: "too-large.png", mimeType: "image/png", buffer: Buffer.alloc(10 * 1024 * 1024 + 1, 1),
  }]);
  await expect(selected).toHaveCount(1);
  await expect(problems).toContainText("larger than 10 MB");
  await page.locator("#betaNoticeSave").click();
  await page.waitForTimeout(300);
  expect(posts).toEqual([]);
  await page.locator("#betaNoticeRemoveAll").click();

  const many = Array.from({ length: 21 }, (_, index) => pngFile(`notice-${index + 1}.png`, (index * 5) + 1));
  await page.locator("#betaNoticeFiles").setInputFiles(many);
  await expect(selected).toHaveCount(21);
  await expect(problems).toContainText("at most 20");
  await page.locator("#betaNoticeSave").click();
  await page.waitForTimeout(300);
  expect(posts).toEqual([]);
  await expect(selected).toHaveCount(21);
  await page.locator("#betaNoticeRemoveAll").click();

  await page.locator("#betaNoticeFiles").setInputFiles([pngFile("kept.png", 120)]);
  await page.getByLabel("Role for image 1").selectOption("page");
  const longDeadline = "x".repeat(1001);
  await page.locator("#betaNoticeDeadline").fill(longDeadline);
  await expect(page.locator("#betaNoticeDeadline")).toHaveValue(longDeadline);
  await expect(problems).toContainText("1001 characters");
  await page.locator("#betaNoticeSave").click();
  await page.waitForTimeout(300);
  expect(posts).toEqual([]);
  await expect(page.locator("#betaNoticeDeadline")).toHaveValue(longDeadline);
  await page.locator("#betaNoticeDeadline").fill("Hearing requested by 11/15/2026");
  await expect(problems).toHaveCount(0);
  await expect(page.locator("#betaNoticeFormError")).toHaveCount(0);
  expect(posts).toEqual([]);
});

test("mobile notice controls are labelled and reachable by keyboard", async ({ page, context }, testInfo) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await instrument(context);
  await acceptedCase(page);

  const toggle = page.locator("#betaNoticesToggle");
  await toggle.focus();
  await expect(toggle).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  await expect(page.locator("#betaNoticesBody")).toBeVisible();

  const fileInput = page.locator("#betaNoticeFiles");
  await expect(fileInput).toHaveAccessibleName(/Notice photos/);
  await fileInput.focus();
  await expect(fileInput).toBeFocused();
  await fileInput.setInputFiles([pngFile("first.png", 20), pngFile("second.png", 44)]);

  await page.getByLabel("Role for image 1").selectOption("envelope");
  await page.getByLabel("Role for image 2").selectOption("page");
  const roleOne = page.getByLabel("Role for image 1");
  await expect(roleOne).toHaveAccessibleName("Role for image 1");
  await roleOne.focus();
  await expect(roleOne).toBeFocused();

  const moveUp = page.getByRole("button", { name: "Move image 2 up" });
  await moveUp.scrollIntoViewIfNeeded();
  await moveUp.focus();
  await expect(moveUp).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.locator("#betaNoticeFilesList [data-selected-id]").first()).toContainText("second.png");

  const remove = page.getByRole("button", { name: "Remove image 2" });
  await remove.focus();
  await expect(remove).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.locator("#betaNoticeFilesList [data-selected-id]")).toHaveCount(1);

  const seen = page.locator("#betaNoticeSeenOn");
  await expect(seen).toHaveAccessibleName(/Date seen on the notice/);
  await seen.focus();
  await expect(seen).toBeFocused();
  const deadline = page.locator("#betaNoticeDeadline");
  await expect(deadline).toHaveAccessibleName(/Deadline text exactly as printed/);
  await deadline.focus();
  await expect(deadline).toBeFocused();
  const save = page.locator("#betaNoticeSave");
  await expect(save).toHaveAccessibleName("Save notice");
  await save.scrollIntoViewIfNeeded();
  await save.focus();
  await expect(save).toBeFocused();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(1);
  await page.screenshot({ path: testInfo.outputPath("beta-notices-mobile.png"), fullPage: true });
});

test("free-beta sentinel counts and throws without pretending to be overridden", async ({ browser }) => {
  const context = await browser.newContext({ ignoreHTTPSErrors: true });
  try {
    await installNetworkGuard(context);
    await instrument(context);
    const page = await context.newPage();
    await page.goto(base!);
    // CSP permits this origin; the network guard must independently block it.
    const disallowed = "https://api.stripe.com/f03-egress-probe";
    expectedBlocked.push(disallowed);
    expect(await page.evaluate(async (url) => {
      try { await fetch(url); return "transport succeeded"; } catch { return "blocked"; }
    }, disallowed)).toBe("blocked");
    expect(unexpectedEgress).toEqual([disallowed]);
    expect(await page.evaluate(() => window.__f03Stripe.constructorCalls)).toBe(0);
    expect(await page.evaluate(() => {
      try { (window as unknown as { Stripe: () => void }).Stripe(); } catch { return true; }
      return false;
    })).toBe(true);
    expect(await page.evaluate(() => window.__f03Stripe.constructorCalls)).toBe(1);
    expect(await page.evaluate(() => {
      try { (window as unknown as { Stripe: () => void }).Stripe(); } catch { return true; }
      return false;
    })).toBe(true);
    expect(await page.evaluate(() => window.__f03Stripe.constructorCalls)).toBe(2);
  } finally {
    await context.close();
  }
});
