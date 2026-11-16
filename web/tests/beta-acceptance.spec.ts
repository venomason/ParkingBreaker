import { expect, test, type BrowserContext, type Page, type Route } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { resolve } from "node:path";
import { deflateSync } from "node:zlib";

const base = process.env.SECURE_BASE;
test.use({ ignoreHTTPSErrors: true });
test.skip(!base, "requires the isolated HTTPS cookie-auth API rehearsal");

const python = process.env.BETA_FIXTURE_PYTHON ?? resolve(__dirname, "../../backend/.venv/bin/python");
const helper = resolve(__dirname, "beta-acceptance-fixtures.py");

// ---------------------------------------------------------------------------
// Environment allowlist. Only the configured reserved HTTPS secure origin and
// the explicitly configured loopback transport origin may be reached. Anything
// else is aborted and the test fails. Page data never extends this set.
// ---------------------------------------------------------------------------

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
    __bbStripe: { constructorCalls: number; paymentMethodCalls: number };
    __bbMutations: { cardInsertions: number; priceInsertions: number };
  }
}

async function instrument(context: BrowserContext) {
  await context.addInitScript(() => {
    const calls = { constructorCalls: 0, paymentMethodCalls: 0 };
    Object.defineProperty(window, "__bbStripe", { value: calls, configurable: true });
    // A throwing constructor is the strongest sentinel: selected-beta tests fail
    // loudly if any code path tries to initialize Stripe.
    (window as unknown as { Stripe: unknown }).Stripe = function () {
      calls.constructorCalls += 1;
      throw new Error("selected beta must never initialize Stripe");
    };

    const mutations = { cardInsertions: 0, priceInsertions: 0 };
    Object.defineProperty(window, "__bbMutations", { value: mutations, configurable: true });
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
    stripeCalls: window.__bbStripe.constructorCalls,
    paymentMethodCalls: window.__bbStripe.paymentMethodCalls,
    cardInsertions: window.__bbMutations.cardInsertions,
    priceInsertions: window.__bbMutations.priceInsertions,
  }));
}

// ---------------------------------------------------------------------------
// Existing real intake / selection helper and backend-forward helper.
// ---------------------------------------------------------------------------

function fixtures(action: "select" | "proof", caseId: string): Record<string, unknown> {
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
  if (!transport) return route.fetch();
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

async function intakeCase(page: Page): Promise<string> {
  await page.goto(base!);
  await call(page, "POST", "/auth/appeals", {});
  const workspace = await call(page, "POST", "/workspaces", { notes: "synthetic beta" }) as { workspace_id: string; draft_revision?: number; ticket_reading_required?: boolean };
  const photo = await call(page, "POST", `/workspaces/${workspace.workspace_id}/photos`, {
    content_type: "image/png", data_base64: syntheticPng(200).toString("base64"),
  }) as { photo_ref: string };
  const issueDate = new Date(Date.now() - 3 * 86400000).toISOString().slice(0, 10);
  const ticket = {
    citation_number: "SYN-BETA-1", license_plate: "SYN123", license_plate_state: "CA",
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
      name: "Synthetic Customer", phone: "415-555-0100", email: "beta@example.test",
      mailing_address: { line1: "123 Example Street", city: "San Francisco", state: "CA", zip_code: "94103" },
    },
    customer_position: { statement: "The sign was covered. The kiosk was blank." },
    citation_photo_ref: photo.photo_ref, rental_company_paid: "NOT_RENTAL",
    ...(ticketConfirmationCommandId ? { ticket_confirmation_command_id: ticketConfirmationCommandId } : {}),
  }) as { case_id: string };
  return receipt.case_id;
}

async function selectedCase(page: Page): Promise<{ caseId: string; disclosure: string; sha: string }> {
  const caseId = await intakeCase(page);
  fixtures("select", caseId);
  const offer = await call(page, "GET", `/cases/${caseId}/beta-offer`) as { disclosure_text: string; disclosure_sha256: string };
  return { caseId, disclosure: offer.disclosure_text, sha: offer.disclosure_sha256 };
}

const ZERO: Activity = { stripeCalls: 0, paymentMethodCalls: 0, cardInsertions: 0, priceInsertions: 0 };

test("real server phase governs unselected paid admission without client entitlement", async ({ page, context }) => {
  const phase = process.env.PB_SERVICE_PHASE;
  expect(["paid", "controlled_free_beta"]).toContain(phase);
  if (phase === "controlled_free_beta") await instrument(context);
  const caseId = await intakeCase(page);
  const pricing = await call(page, "POST", "/auth/pricing", {}) as { service_phase: string };
  expect(pricing.service_phase).toBe(phase);
  // Direct callers can choose a tier even when no checkout is shown. The
  // admission test must reach the phase guard rather than fail for no tier.
  await call(page, "PUT", `/cases/${caseId}/tier`, { tier: "CERTIFIED" });
  const before = fixtures("proof", caseId);
  expect(before.entitlement_present).toBe(false);
  expect(before.payment_authorization_rows).toBe(0);
  expect(before.mail_attempt_rows).toBe(0);
  if (phase === "paid") {
    const admitted = await call(page, "POST", `/cases/${caseId}/authorization`, {
      terms_accepted: true, payment_method_id: "pm_card_visa",
    }) as { case_id: string; amount_cents: number };
    expect(admitted.case_id).toBe(caseId);
    expect(admitted.amount_cents).toBeGreaterThan(0);
    expect(fixtures("proof", caseId).payment_authorization_rows).toBe(1);
    return;
  }
  await page.reload();
  await expect(page.getByRole("heading", { name: "This controlled beta has no new paid mailing option.", exact: true })).toBeVisible();
  expect(await activity(page)).toEqual(ZERO);
  const refused = await page.evaluate(async (id) => {
    const csrf = document.cookie.split("; ").find((part) => part.startsWith("__Host-pb-csrf="))?.split("=")[1] ?? "";
    const response = await fetch(`/api/cases/${id}/authorization`, {
      method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
      body: JSON.stringify({ terms_accepted: true, payment_method_id: "pm_card_visa" }),
    });
    return { status: response.status, body: await response.json() };
  }, caseId);
  expect(refused).toEqual({ status: 409, body: { detail: "paid_orders_paused" } });
  expect(fixtures("proof", caseId)).toEqual(before);
  expect(await activity(page)).toEqual(ZERO);
});

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

test("selected beta keeps Stripe, card, tier and authorization activity at zero", async ({ page, context }, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await instrument(context);
  const { caseId, disclosure, sha } = await selectedCase(page);
  const paid: string[] = [];
  const stripe: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.hostname.endsWith("stripe.com") || url.hostname.endsWith("stripe.network")) stripe.push(url.href);
    if (/(\/authorization|\/tier)$/.test(url.pathname) || /payment_method/.test(url.pathname)) paid.push(url.pathname);
  });
  await page.reload();

  await expect(page.locator("#h-s4")).toHaveText("Your selected beta appeal is free.");
  await expect(page.locator("#betaOfferCard")).toContainText("$0");
  await expect(page.locator("#betaOfferCard")).toContainText("No card hold, no payment");
  await expect(page.locator("#betaDisclosureText")).toHaveText(disclosure);
  await expect(page.locator("#betaConsent")).not.toBeChecked();
  await expect(page.locator("#actBetaBtn")).toBeDisabled();
  await expect(page.locator("#cardField")).toHaveCount(0);
  await expect(page.getByLabel("Assigned certified mailing price")).toHaveCount(0);
  expect(await page.frames().filter((frame) => /stripe/i.test(frame.url())).length).toBe(0);
  expect(stripe, "selected beta attempted Stripe").toEqual([]);
  expect(await activity(page)).toEqual(ZERO);

  await expect(page.locator("#asideCharge")).toContainText("No card and no payment");
  await page.screenshot({ path: testInfo.outputPath("desktop-beta-offer.png"), fullPage: true });
  let posted: Record<string, unknown> | null = null;
  page.on("request", (request) => {
    if (request.url().endsWith(`/api/cases/${caseId}/beta-acceptance`)) posted = request.postDataJSON();
  });
  await page.locator("#betaConsent").check();
  await expect(page.locator("#actBetaBtn")).toBeEnabled();
  await page.locator("#actBetaBtn").click();
  await expect(page.locator("#h-s5")).toContainText("Free beta accepted");
  await expect(page.locator("#s5lead")).toContainText("No card hold and no payment");
  await expect(page.locator("#s5lead")).not.toContainText("charged only after the first USPS scan");

  expect(posted).toEqual({ terms_accepted: true, disclosure_sha256: sha });
  expect(paid, "selected beta used paid payment routes").toEqual([]);
  expect(stripe, "selected beta attempted Stripe").toEqual([]);
  expect(await activity(page)).toEqual(ZERO);

  const proof = fixtures("proof", caseId);
  expect(proof.accepted_at).toBeTruthy();
  expect(proof.payment_authorization_rows).toBe(0);
  expect(proof.mail_attempt_rows).toBe(0);
  expect(proof.payment_state).toBe("NO_CHARGE");

  await page.reload();
  await expect(page.locator("#cardField")).toHaveCount(0);
  await expect(page.locator("body")).not.toContainText("Card authorized");
  await expect(page.locator("body")).not.toContainText("card hold authorized");
  await expect(page.locator("#s3")).toBeVisible();
  expect(await activity(page)).toEqual(ZERO);
});

test("Stripe sentinel self-test counts and throws without touching application calls", async ({ browser }) => {
  const context = await browser.newContext();
  try {
    await instrument(context);
    const page = await context.newPage();
    expect(await page.evaluate(() => window.__bbStripe.constructorCalls)).toBe(0);
    expect(await page.evaluate(() => { try { (window as unknown as { Stripe: () => void }).Stripe(); } catch { return true; } return false; })).toBe(true);
    expect(await page.evaluate(() => window.__bbStripe.constructorCalls)).toBe(1);
    expect(await page.evaluate(() => { try { (window as unknown as { Stripe: () => void }).Stripe(); } catch { return true; } return false; })).toBe(true);
    expect(await page.evaluate(() => window.__bbStripe.constructorCalls)).toBe(2);
  } finally {
    await context.close();
  }
});

test("delayed funding shows no assigned price, card or payment activity while checking", async ({ page, context }) => {
  await instrument(context);
  const { caseId, disclosure } = await selectedCase(page);
  const paid: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (/(\/authorization|\/tier)$/.test(url.pathname) || /payment_method/.test(url.pathname)) paid.push(url.pathname);
  });

  let release!: () => void;
  const budgetGate = new Promise<void>((resolve) => { release = resolve; });
  let blockedOffers = 0;
  await page.route(`**/api/cases/${caseId}/beta-offer`, async (route) => {
    blockedOffers += 1;
    await budgetGate;
    await route.fallback();
  });

  await page.reload();
  // /auth/pricing settles immediately; funding stays unresolved at the barrier.
  await expect.poll(() => blockedOffers).toBeGreaterThan(0);
  await expect(page.getByRole("status")).toContainText("Loading");
  await expect(page.getByLabel("Assigned certified mailing price")).toHaveCount(0);
  await expect(page.locator("#cardField")).toHaveCount(0);
  expect(await page.evaluate(() => window.__bbMutations.priceInsertions)).toBe(0);
  expect(await activity(page)).toEqual(ZERO);
  expect(paid).toEqual([]);

  release();
  await expect(page.locator("#h-s4")).toHaveText("Your selected beta appeal is free.");
  await expect(page.locator("#betaDisclosureText")).toHaveText(disclosure);
  await expect(page.getByLabel("Assigned certified mailing price")).toHaveCount(0);
  await expect(page.locator("#cardField")).toHaveCount(0);
  expect(await activity(page)).toEqual(ZERO);
  expect(paid).toEqual([]);
});

test("funding failure stays unknown with no paid path until explicit retry recovers beta", async ({ page, context }) => {
  await instrument(context);
  const { caseId, disclosure } = await selectedCase(page);
  const paid: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (/(\/authorization|\/tier)$/.test(url.pathname) || /payment_method/.test(url.pathname)) paid.push(url.pathname);
  });

  let failing = true;
  let calls = 0;
  await page.route(`**/api/cases/${caseId}/beta-offer`, async (route) => {
    calls += 1;
    if (failing) return route.fulfill({ status: 500, json: { detail: "synthetic_offer_failure" } });
    await route.fallback();
  });
  await page.route(`**/api/cases/${caseId}/status`, async (route) => {
    if (failing) return route.fulfill({ status: 500, json: { detail: "synthetic_status_failure" } });
    await route.fallback();
  });

  await page.reload();
  await expect(page.getByRole("heading", { level: 1, name: "We couldn’t confirm this appeal’s funding" })).toBeVisible();
  await expect(page.locator("p[role=alert]")).toContainText("No paid checkout is shown");
  await expect(page.getByLabel("Assigned certified mailing price")).toHaveCount(0);
  await expect(page.locator("#cardField")).toHaveCount(0);
  // Nothing may drift into the paid path or resolve on its own.
  await page.waitForTimeout(3000);
  await expect(page.getByRole("heading", { level: 1, name: "We couldn’t confirm this appeal’s funding" })).toBeVisible();
  await expect(activity(page)).resolves.toMatchObject({ cardInsertions: 0, priceInsertions: 0, stripeCalls: 0 });
  expect(paid).toEqual([]);

  failing = false;
  const before = calls;
  await page.getByRole("button", { name: "Retry saved status" }).click();
  await expect(page.locator("#h-s4")).toHaveText("Your selected beta appeal is free.");
  await expect(page.locator("#betaDisclosureText")).toHaveText(disclosure);
  await expect(page.getByLabel("Assigned certified mailing price")).toHaveCount(0);
  await expect(page.locator("#cardField")).toHaveCount(0);
  expect(calls).toBeGreaterThan(before);
  expect(paid).toEqual([]);
});

test("lost acceptance response commits once and reload recovers the same free window", async ({ page, context }) => {
  await instrument(context);
  const { caseId } = await selectedCase(page);
  await page.reload();
  await expect(page.locator("#h-s4")).toContainText("free");
  await page.route("**/api/cases/*/beta-acceptance", async (route) => {
    const response = await forward(route);
    expect(response.status()).toBe(200);
    await route.abort("connectionreset");
  });
  await page.locator("#betaConsent").check();
  await page.locator("#actBetaBtn").click();
  await expect(page.locator("#betaError")).toBeVisible();
  await expect(page.locator("#h-s4")).toContainText("free");
  await expect(page.locator("#cardField")).toHaveCount(0);
  await expect(page.locator("body")).not.toContainText("Card authorized");

  const committed = fixtures("proof", caseId);
  expect(committed.accepted_at).toBeTruthy();

  // Explicit saved-status retry must not accept a second time or reset the window.
  await page.getByRole("button", { name: "Check the saved beta offer" }).click();
  await expect(page.locator("#s3")).toBeVisible();
  await page.reload();
  await expect(page.locator("#s3")).toBeVisible();
  await expect(page.locator("#cardField")).toHaveCount(0);
  const recovered = fixtures("proof", caseId);
  expect(recovered.accepted_at).toBe(committed.accepted_at);
  expect(recovered.case_token).toBe(committed.case_token);
  expect(recovered.payment_authorization_rows).toBe(0);
});

test("mobile free consent and acceptance stay reachable by pointer and keyboard", async ({ page, context }, testInfo) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await instrument(context);
  const { caseId } = await selectedCase(page);
  await page.reload();
  await expect(page.locator("#h-s4")).toHaveText("Your selected beta appeal is free.");
  const consent = page.locator("#betaConsent");
  await consent.scrollIntoViewIfNeeded();
  await consent.check();
  await consent.uncheck();
  await consent.focus();
  await page.keyboard.press("Space");
  await expect(consent).toBeChecked();
  const accept = page.locator("#actBetaBtn");
  await expect(accept).toBeEnabled();
  await accept.scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("mobile-beta-offer.png"), fullPage: true });
  await accept.focus();
  await page.keyboard.press("Enter");
  await expect(page.locator("#h-s5")).toContainText("Free beta accepted");
  await expect(page.locator("#s5lead")).toContainText("No card hold and no payment");
  expect(await activity(page)).toEqual(ZERO);
  const proof = fixtures("proof", caseId);
  expect(proof.payment_authorization_rows).toBe(0);
  expect(proof.payment_state).toBe("NO_CHARGE");
});
