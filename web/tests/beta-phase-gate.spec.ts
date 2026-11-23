import { expect, test, type Page } from "@playwright/test";

// UI-only mocks. These tests route /auth/pricing service_phase and the saved
// case reads; they never route an authorization success to simulate the server
// gate. The real controlled-phase 409 and zero-provider-row proof are owned by
// the parent's Git-backed production rehearsal.

// Reserved secure origins only. The API contract is exercised over the explicit
// loopback transport; any other network egress is aborted and asserted.
const LOOPBACK_HOSTS = new Set(["127.0.0.1", "localhost", "[::1]", "::1"]);

function configuredOrigins(): Set<string> {
  const secure = new URL(process.env.SECURE_BASE ?? "");
  if (secure.protocol !== "https:" || !secure.hostname.endsWith(".example.test") || secure.username || secure.password || secure.pathname !== "/" || secure.search || secure.hash) {
    throw new Error("Secure base must be a bare reserved HTTPS example.test origin");
  }
  const allowed = new Set([secure.origin]);
  const rawTransport = process.env.SECURE_TRANSPORT_BASE;
  if (rawTransport) {
    const transport = new URL(rawTransport);
    if (!["http:", "https:"].includes(transport.protocol) || !LOOPBACK_HOSTS.has(transport.hostname) || transport.username || transport.password || transport.pathname !== "/" || transport.search || transport.hash) {
      throw new Error("Transport must be one explicitly configured bare loopback origin");
    }
    allowed.add(transport.origin);
  }
  return allowed;
}

let unexpectedEgress: string[] = [];

test.beforeEach(async ({ context }) => {
  unexpectedEgress = [];
  const allowed = configuredOrigins();
  await context.route("**/*", (route) => {
    const url = new URL(route.request().url());
    if (!["http:", "https:"].includes(url.protocol) || allowed.has(url.origin)) return route.fallback();
    unexpectedEgress.push(route.request().url());
    return route.abort("blockedbyclient");
  });
});

test.afterEach(async ({ context }) => {
  await context.unroute("**/*");
  expect(unexpectedEgress).toEqual([]);
});

const assignment = (service_phase: string, amount_cents: number | null = null, arm: string | null = null) => ({
  experiment_id: "certified-price-v1",
  arm,
  amount_cents,
  service_phase,
});

const workspace = {  workspace_id: "ws_gate",
  case_id: "case_gate",
  intake_form: { values: {}, step: 2 },
  intake: {
    ticket: {
      citation_number: "SYN-GATE-1",
      license_plate: "SYN123",
      license_plate_state: "CA",
      issue_date: "2026-11-01",
      issuer_hint: "San Francisco Municipal Transportation Agency",
      violation_description: "Meter expired",
    },
    customer_position: { statement: "The sign was covered." },
  },
};

const status = { case_id: "case_gate", payment_state: "PENDING", state: "INTAKE" };
const offer = {
  kind: "offer",
  issuer: { issuer_name: "San Francisco Municipal Transportation Agency", deciding_body: "SFMTA" },
  tiers: [{ tier: "CERTIFIED", amount_cents: 2499 }],
};

async function instrument(page: Page, allowPaid = false) {
  await page.addInitScript((allowPaid) => {
    const calls = { stripe: 0, paymentMethods: 0 };
    Object.defineProperty(window, "__gateStripe", { value: calls, configurable: true });
    (window as unknown as { Stripe: unknown }).Stripe = function () {
      calls.stripe += 1;
      if (!allowPaid) throw new Error("gate must never initialize Stripe");
      return {
        _registerWrapper() {},
        elements: () => ({ create: () => ({ mount() {}, on() {}, destroy() {} }) }),
        createPaymentMethod: async () => {
          calls.paymentMethods += 1;
          return { paymentMethod: { id: "pm_card_visa" } };
        },
      };
    };
    const mutations = { card: 0, price: 0 };
    Object.defineProperty(window, "__gateMutations", { value: mutations, configurable: true });
    let card = false;
    let price = false;
    const sample = () => {
      const hasCard = document.querySelector("#cardField") !== null;
      const hasPrice = document.querySelector('[aria-label="Assigned certified mailing price"]') !== null;
      if (hasCard && !card) mutations.card += 1;
      if (hasPrice && !price) mutations.price += 1;
      card = hasCard;
      price = hasPrice;
    };
    document.addEventListener("DOMContentLoaded", () => {
      sample();
      new MutationObserver(sample).observe(document.documentElement, { childList: true, subtree: true });
    });
  }, allowPaid);
}

function activity(page: Page) {
  return page.evaluate(() => ({
    stripe: (window as unknown as { __gateStripe: { stripe: number } }).__gateStripe.stripe,
    ...(window as unknown as { __gateMutations: { card: number; price: number } }).__gateMutations,
  }));
}

async function mockUnselectedIntake(page: Page, service_phase: string) {
  await page.route("**/api/auth/session", (route) => route.fulfill({ status: 401, json: { detail: "no_session" } }));
  await page.route("**/api/auth/pricing", (route) => route.fulfill({ json: assignment(service_phase, 2499, "P2499") }));
  await page.route("**/api/auth/appeal", (route) => route.fulfill({ json: workspace }));
  await page.route("**/api/cases/case_gate/status", (route) => route.fulfill({ json: status }));
  await page.route("**/api/cases/case_gate/beta-offer", (route) => route.fulfill({ status: 409, json: { detail: "beta_entitlement_required" } }));
  await page.route("**/api/cases/case_gate/pre-hold", (route) => route.fulfill({ json: offer }));
}

test("controlled free beta unselected intake has no paid price, card, tier or authorization request", async ({ page }) => {
  await instrument(page);
  const paid: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (/(\/authorization|\/tier)$/.test(url.pathname) || /payment_method/.test(url.pathname)) paid.push(url.pathname);
  });
  await mockUnselectedIntake(page, "controlled_free_beta");
  await page.goto("/");

  await expect(page.locator("#h-s4")).toHaveText("This controlled beta has no new paid mailing option.");
  await expect(page.locator("#controlledBetaLead")).toContainText("No new card or payment is requested here");
  await expect(page.locator("#controlledBetaLead")).toContainText("does not mean free mailing is approved");
  await expect(page.locator("#controlledBetaLead")).not.toContainText("nothing was charged");
  await expect(page.locator("#controlledBetaLead")).not.toContainText("free appeal");
  await expect(page.getByRole("button", { name: "Check my appeal" }).first()).toBeVisible();
  await expect(page.getByLabel("Assigned certified mailing price")).toHaveCount(0);
  await expect(page.locator("#tierset")).toHaveCount(0);
  await expect(page.locator("#pay")).toHaveCount(0);
  await expect(page.locator("#cardField")).toHaveCount(0);
  await expect(page.locator("#actAuthBtn")).toHaveCount(0);
  await expect(page.locator("#actSnap")).toHaveCount(0);
  expect(paid).toEqual([]);
  expect(await activity(page)).toEqual({ stripe: 0, card: 0, price: 0 });

  await page.getByRole("button", { name: "Check my appeal" }).first().click();
  await expect(page.locator("#h-s4")).toHaveText("This controlled beta has no new paid mailing option.");
  await expect(page.locator("#controlledBetaLead")).not.toContainText("nothing was charged");
  expect(paid).toEqual([]);
});

test("controlled beta sidebar and how-it-works copy do not promise card charging", async ({ page }) => {
  await instrument(page);
  await mockUnselectedIntake(page, "controlled_free_beta");
  await page.goto("/");
  await expect(page.locator("#asideCharge")).toContainText("No new card or payment is requested now");
  await expect(page.locator("#asideCharge")).not.toContainText("No card and no payment");
  await expect(page.locator("#asideCharge")).not.toContainText("card is charged");
  await page.getByRole("button", { name: "Rules and what we never do", exact: true }).click();
  await expect(page.getByRole("heading", { name: "How it works & rules" })).toBeVisible();
  await expect(page.locator("body")).not.toContainText("Your card is charged only after the first USPS scan");
  await expect(page.locator("body")).toContainText("This controlled beta offers no new paid mailing option");
});

test("missing service_phase on unselected intake never mounts the paid path", async ({ page }) => {
  await instrument(page);
  const paid: string[] = [];
  page.on("request", (request) => {
    if (/(\/authorization|\/tier)$/.test(new URL(request.url()).pathname)) paid.push(request.url());
  });
  await page.route("**/api/auth/session", (route) => route.fulfill({ status: 401, json: { detail: "no_session" } }));
  await page.route("**/api/auth/pricing", (route) => route.fulfill({ json: { experiment_id: "certified-price-v1", arm: "P2499", amount_cents: 2499 } }));
  await page.route("**/api/auth/appeal", (route) => route.fulfill({ json: workspace }));
  await page.route("**/api/cases/case_gate/status", (route) => route.fulfill({ json: status }));
  await page.route("**/api/cases/case_gate/beta-offer", (route) => route.fulfill({ status: 409, json: { detail: "beta_entitlement_required" } }));
  await page.route("**/api/cases/case_gate/pre-hold", (route) => route.fulfill({ json: offer }));
  await page.goto("/");

  await expect(page.getByRole("heading", { level: 1, name: "Your appeal service is unavailable", exact: true })).toBeVisible();
  await expect(page.getByLabel("Assigned certified mailing price")).toHaveCount(0);
  await expect(page.locator("#s4")).toHaveCount(0);
  await expect(page.locator("#s1")).toHaveCount(0);
  expect(paid).toEqual([]);
  expect(await activity(page)).toEqual({ stripe: 0, card: 0, price: 0 });
});

test("delayed phase never transiently mounts a paid price or card", async ({ page }) => {
  await instrument(page);
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  await page.route("**/api/auth/session", (route) => route.fulfill({ status: 401, json: { detail: "no_session" } }));
  await page.route("**/api/auth/appeal", (route) => route.fulfill({ status: 401, json: { detail: "no_session" } }));
  await page.route("**/api/auth/pricing", async (route) => {
    await gate;
    await route.fulfill({ json: assignment("controlled_free_beta", 2499, "P2499") });
  });
  await page.goto("/");
  await expect(page.getByRole("status")).toContainText("Loading");
  await expect(page.getByLabel("Assigned certified mailing price")).toHaveCount(0);
  await expect(page.locator("#actSnap")).toHaveCount(0);
  await expect(page.locator("#cardField")).toHaveCount(0);
  expect(await activity(page)).toEqual({ stripe: 0, card: 0, price: 0 });

  release();
  await expect(page.locator("#actSnap")).toBeVisible();
  await expect(page.getByLabel("Assigned certified mailing price")).toHaveCount(0);
  expect(await activity(page)).toEqual({ stripe: 0, card: 0, price: 0 });
});

test("explicit paid phase keeps the paid offer and assigned price", async ({ page }) => {
  await instrument(page, true);
  await mockUnselectedIntake(page, "paid");
  await page.goto("/");
  await expect(page.getByLabel("Assigned certified mailing price")).toHaveText("Early beta price: $24.99 · Certified Mail, everything included");
  await expect(page.locator("#h-s4")).not.toHaveText("This controlled beta has no paid mailing option.");
  await expect(page.locator("#tierset")).toBeVisible();
  await expect(page.locator("#s4")).toBeVisible();
});

// UI-only transition test. The deterministic authorization refusal is routed;
// no authorization success is mocked and no server-gate correctness is claimed.
// The real 409/zero-provider proof belongs to the parent rehearsal.
test("stale paid UI 409 refreshes actual phase, removes every paid affordance, and keeps the Journey mounted", async ({ page }) => {
  let pricingCalls = 0;
  let statusCalls = 0;
  let offerCalls = 0;
  await instrument(page, true);
  await page.route("**/api/auth/session", (route) => route.fulfill({ status: 401, json: { detail: "no_session" } }));
  await page.route("**/api/auth/pricing", (route) => {
    pricingCalls += 1;
    return route.fulfill({ json: pricingCalls === 1 ? assignment("paid", 2499, "P2499") : assignment("controlled_free_beta", 2499, "P2499") });
  });
  await page.route("**/api/auth/appeal", (route) => route.fulfill({ json: workspace }));
  await page.route("**/api/cases/case_gate/status", (route) => {
    statusCalls += 1;
    return route.fulfill({ json: { ...status, payment_state: "PENDING" } });
  });
  await page.route("**/api/cases/case_gate/beta-offer", (route) => {
    offerCalls += 1;
    return route.fulfill({ status: 409, json: { detail: "beta_entitlement_required" } });
  });
  await page.route("**/api/cases/case_gate/pre-hold", (route) => route.fulfill({ json: offer }));
  await page.route("**/api/cases/case_gate/tier", (route) => route.fulfill({ json: offer }));
  await page.route("**/api/cases/case_gate/authorization", (route) => route.fulfill({ status: 409, json: { detail: "paid_orders_paused" } }));
  await page.route("**/api/health", (route) => route.fulfill({ json: { status: "ok", mode: "fake" } }));

  await page.goto("/");
  await expect(page.getByLabel("Assigned certified mailing price")).toHaveText("Early beta price: $24.99 · Certified Mail, everything included");
  await expect(page.locator("#tierset")).toBeVisible();
  await page.locator('input[name="tier"][value="certified"]').check();
  // Real DOM identity: a reload would detach this handle. The mounted Journey
  // shell (and its in-memory state) must survive the in-place phase refresh.
  const shell = await page.$("#main");
  expect(shell).not.toBeNull();
  await expect(page.locator("#actAuthBtn")).toBeDisabled();
  await page.locator("#termsAccepted").check();
  await expect(page.locator("#actAuthBtn")).toBeEnabled();
  await page.locator("#actAuthBtn").click();

  await expect(page.locator("#h-s4")).toHaveText("Paid ordering is paused for this appeal.");
  await expect(page.locator("#controlledBetaLead")).toContainText("No new payment was authorized");
  await expect(page.locator("#controlledBetaLead")).not.toContainText("nothing was charged");
  await expect(page.getByLabel("Assigned certified mailing price")).toHaveCount(0);
  await expect(page.locator("#tierset")).toHaveCount(0);
  await expect(page.locator("#pay")).toHaveCount(0);
  await expect(page.locator("#cardField")).toHaveCount(0);
  await expect(page.locator("#actAuthBtn")).toHaveCount(0);
  await expect(page.locator("#asideCharge")).toContainText("No new card or payment is requested now");
  await expect(page.locator("#asideCharge")).not.toContainText("card is charged");
  expect(await shell!.evaluate((el) => el.isConnected)).toBe(true);
  expect(pricingCalls).toBe(2);
  expect(statusCalls).toBeGreaterThanOrEqual(2);
  expect(offerCalls).toBeGreaterThanOrEqual(2);
  expect((await activity(page)).stripe).toBeGreaterThan(0);
  expect(await page.evaluate(() => (window as unknown as { __gateStripe: { paymentMethods: number } }).__gateStripe.paymentMethods)).toBe(1);
});

test("last paid place taken during checkout shows saved work and the issuer link", async ({ page }) => {
  await instrument(page, true);
  await mockUnselectedIntake(page, "paid");
  let full = false;
  let authorizationRequests = 0;
  const stop = {
    case_id: "case_gate", kind: "stop", family: "BUSINESS_EXCLUSION", reason_key: "paid_cohort_full",
    message: "The paid beta is full. Your work is saved.", official_url: "https://sfmta.com/",
    work_preserved: true, no_charge: true,
  };
  await page.route("**/api/cases/case_gate/status", route => route.fulfill({
    json: full ? { ...status, state: "STOPPED_NO_CHARGE", payment_state: null, stop } : status,
  }));
  await page.route("**/api/cases/case_gate/tier", route => route.fulfill({ json: offer }));
  await page.route("**/api/cases/case_gate/authorization", route => {
    full = true;
    authorizationRequests++;
    return route.fulfill({ status: 409, json: { detail: "paid_cohort_full" } });
  });
  await page.goto("/");
  await page.locator('input[name="tier"][value="certified"]').check();
  await page.locator("#termsAccepted").check();
  await page.locator("#actAuthBtn").click();
  await expect(page.locator("#liveStopMessage")).toHaveText(stop.message);
  await expect(page.getByRole("link", { name: "Open the issuer's official site" })).toHaveAttribute("href", stop.official_url);
  await expect(page.getByRole("button", { name: "Start a new appeal" })).toHaveCount(0);
  await expect(page.locator("#cardField")).toHaveCount(0);
  await page.reload();
  await expect(page.locator("#liveStopMessage")).toHaveText(stop.message);
  expect(authorizationRequests).toBe(1);
});

test("an uncertain checkout uses the saved hold across reload without creating another card", async ({ page }) => {
  await instrument(page, true);
  await mockUnselectedIntake(page, "paid");
  let pending = false;
  let authorizationRequests = 0;
  let recoveryRequests = 0;
  await page.route("**/api/cases/case_gate/tier", route => route.fulfill({ json: offer }));
  await page.route("**/api/cases/case_gate/status", route => route.fulfill({
    json: { ...status, payment_state: null, authorization_pending: pending },
  }));
  await page.route("**/api/cases/case_gate/authorization", route => {
    authorizationRequests++;
    pending = true;
    return route.fulfill({ status: 402, json: { detail: "card_not_authorized" } });
  });
  await page.route("**/api/cases/case_gate/authorization/recovery", route => {
    recoveryRequests++;
    expect(route.request().method()).toBe("POST");
    expect(route.request().postData()).toBeNull();
    pending = false;
    return route.fulfill({
      json: {
        ...status,
        state: "RESEARCHING",
        payment_state: "AUTHORIZED",
        amount_cents: 2499,
        window_started_at: new Date().toISOString(),
        window_status: "ACTIVE",
        seconds_remaining: 7180,
      },
    });
  });

  await page.goto("/");
  await page.locator('input[name="tier"][value="certified"]').check();
  await page.locator("#termsAccepted").check();
  await page.locator("#actAuthBtn").click();
  await expect(page.locator("#h-s4")).toHaveText("Check your original card hold.");
  await expect(page.locator("#cardField")).toHaveCount(0);
  expect(await page.evaluate(() => (window as unknown as { __gateStripe: { paymentMethods: number } }).__gateStripe.paymentMethods)).toBe(1);
  await page.reload();
  await expect(page.locator("#h-s4")).toHaveText("Check your original card hold.");
  await expect(page.locator("#cardField")).toHaveCount(0);
  await page.getByRole("button", { name: "Check card hold" }).click();
  await expect(page.locator("#s5")).toBeVisible();
  expect(authorizationRequests).toBe(1);
  expect(recoveryRequests).toBe(1);
  expect(await page.evaluate(() => (window as unknown as { __gateStripe: { paymentMethods: number } }).__gateStripe.paymentMethods)).toBe(0);
});

for (const reload of [false, true]) {
  test(`a verified decline refreshes eligibility before restoring checkout (reload: ${reload})`, async ({ page }) => {
    await instrument(page, true);
    await mockUnselectedIntake(page, "paid");
    let pending = false;
    let declined = false;
    let eligibilityReadsAfterDecline = 0;
    const stop = {
      case_id: "case_gate", kind: "stop", family: "BUSINESS_EXCLUSION", reason_key: "paid_cohort_full",
      message: "The paid beta is full. Your work is saved.", official_url: "https://sfmta.com/",
      work_preserved: true, no_charge: true,
    };
    await page.route("**/api/cases/case_gate/tier", route => route.fulfill({ json: offer }));
    await page.route("**/api/cases/case_gate/status", route => route.fulfill({
      json: { ...status, payment_state: null, authorization_pending: pending },
    }));
    await page.route("**/api/cases/case_gate/authorization", route => {
      pending = true;
      return route.fulfill({ status: 409, json: { detail: "authorization_outcome_unresolved" } });
    });
    await page.route("**/api/cases/case_gate/authorization/recovery", route => {
      pending = false;
      declined = true;
      return route.fulfill({ json: { ...status, payment_state: null } });
    });
    await page.route("**/api/cases/case_gate/pre-hold", route => {
      if (declined) eligibilityReadsAfterDecline++;
      return route.fulfill({ json: declined ? stop : offer });
    });

    await page.goto("/");
    await page.locator('input[name="tier"][value="certified"]').check();
    await page.locator("#termsAccepted").check();
  await page.locator("#actAuthBtn").click();
    await expect(page.locator("#h-s4")).toHaveText("Check your original card hold.");
    if (reload) await page.reload();
    await page.getByRole("button", { name: "Check card hold" }).click();
    await expect(page.locator("#liveStopMessage")).toHaveText(stop.message);
    await expect(page.getByRole("link", { name: "Open the issuer's official site" })).toHaveAttribute("href", stop.official_url);
    await expect(page.locator("#cardField")).toHaveCount(0);
    expect(eligibilityReadsAfterDecline).toBe(1);
  });
}

// UI-only discovery: authenticated saved status/offer reveal a selected but
// unaccepted beta after the phase refresh resolves to paid. The shared funding
// update hides the header/card charge text in place and posts no acceptance.
test("saved-status refresh discovers a selected preaccept beta under paid phase and hides paid charge text", async ({ page }) => {
  await instrument(page);
  let selected = false;
  let releaseOffer!: () => void;
  const offerGate = new Promise<void>((resolve) => { releaseOffer = resolve; });
  let pricingCalls = 0;
  let statusCalls = 0;
  let offerCalls = 0;
  const acceptRequests: string[] = [];
  const selectedOffer = {
    case_id: "case_gate",
    disclosure_text: "Exact server beta disclosure.",
    disclosure_sha256: "a".repeat(64),
    // Selected, not accepted: the saved case is still preaccept.
    accepted_at: null,
  };
  page.on("request", (request) => {
    if (/\/beta-acceptance$/.test(new URL(request.url()).pathname)) acceptRequests.push(request.url());
  });
  await page.route("**/api/auth/session", (route) => route.fulfill({ status: 401, json: { detail: "no_session" } }));
  await page.route("**/api/auth/pricing", (route) => {
    pricingCalls += 1;
    return route.fulfill({ json: pricingCalls === 1 ? assignment("controlled_free_beta", 2499, "P2499") : assignment("paid", 2499, "P2499") });
  });
  await page.route("**/api/auth/appeal", (route) => route.fulfill({ json: workspace }));
  await page.route("**/api/cases/case_gate/status", (route) => {
    statusCalls += 1;
    // Preaccept: INTAKE and PENDING with no window and no accepted timestamp.
    return route.fulfill({ json: { ...status, payment_state: "PENDING", state: "INTAKE" } });
  });
  await page.route("**/api/cases/case_gate/beta-offer", async (route) => {
    offerCalls += 1;
    if (!selected) return route.fulfill({ status: 409, json: { detail: "beta_entitlement_required" } });
    await offerGate;
    return route.fulfill({ json: selectedOffer });
  });
  await page.route("**/api/cases/case_gate/pre-hold", (route) => route.fulfill({ json: offer }));

  await page.goto("/");
  await expect(page.locator("#h-s4")).toHaveText("This controlled beta has no new paid mailing option.");
  await expect(page.getByLabel("Assigned certified mailing price")).toHaveCount(0);

  selected = true;
  await page.getByRole("button", { name: "Check my appeal" }).first().click();
  try {
    await expect.poll(() => pricingCalls).toBe(2);
    await expect(page.getByLabel("Assigned certified mailing price")).toHaveCount(0);
    await expect(page.locator("#cardField")).toHaveCount(0);
    expect(await activity(page)).toEqual({ stripe: 0, card: 0, price: 0 });
  } finally { releaseOffer(); }

  await expect(page.locator("#h-s4")).toHaveText("Your selected beta appeal is free.");
  await expect(page.getByLabel("Assigned certified mailing price")).toHaveCount(0);
  await expect(page.locator("#tierset")).toHaveCount(0);
  await expect(page.locator("#cardField")).toHaveCount(0);
  await expect(page.locator("#asideCharge")).toContainText("No card and no payment in this selected free beta");
  await expect(page.locator("#asideCharge")).not.toContainText("card is charged");
  await expect(page.getByRole("button", { name: /Accept free beta/ })).toBeVisible();
  expect(acceptRequests).toEqual([]);
  expect(await activity(page)).toEqual({ stripe: 0, card: 0, price: 0 });
  expect(pricingCalls).toBeGreaterThanOrEqual(2);
  expect(statusCalls).toBeGreaterThanOrEqual(2);
  expect(offerCalls).toBeGreaterThanOrEqual(2);
});

// Draft recovery regression: the waiting retry must return the real
// AWAITING_ADOPTION status, hydrate the owned draft before stage 6 mounts, and
// never overwrite text the customer has already typed but not saved.
test("waiting retry hydrates the owned draft before stage 6 mounts", async ({ page }) => {
  const letter = "Dear SFMTA,\n\nI am writing about citation SYN-GATE-1.";
  const laterDraftText = "Server draft changed while this page was open.";
  let draftCalls = 0;
  let restored = false;
  const draft = {
    case_id: "case_gate",
    revision: 2,
    text: letter,
    text_sha256: "b".repeat(64),
    adopted_sha256: null,
    adoption_screen_text: "Adopt this letter?",
    spans: [],
    flags: [],
  };
  await mockUnselectedIntake(page, "controlled_free_beta");
  await page.unroute("**/api/cases/case_gate/status");
  await page.unroute("**/api/cases/case_gate/beta-offer");
  await page.route("**/api/cases/case_gate/status", (route) => route.fulfill({ json: restored ? {
    ...status, payment_state: "AUTHORIZED", state: "AWAITING_ADOPTION", amount_cents: 2499, selected_tier: "CERTIFIED",
    window_started_at: new Date(Date.now() - 300000).toISOString(), window_expires_at: new Date(Date.now() + 1500000).toISOString(),
    seconds_remaining: 1500, window_status: "ACTIVE",
  } : status }));
  await page.route("**/api/cases/case_gate/beta-offer", (route) => route.fulfill({ status: 409, json: { detail: "beta_entitlement_required" } }));
  await page.route("**/api/cases/case_gate/draft", (route) => {
    draftCalls += 1;
    return route.fulfill({ json: draftCalls === 1 ? draft : { ...draft, revision: 3, text: laterDraftText, text_sha256: "c".repeat(64) } });
  });

  await page.goto("/");
  await expect(page.locator("#h-s4")).toHaveText("This controlled beta has no new paid mailing option.");
  await expect(page.locator("#s6")).toHaveCount(0);

  restored = true;
  await page.getByRole("button", { name: "Check my appeal" }).first().click();

  await expect(page.locator("#s6")).toBeVisible();
  await expect(page.locator("#liveLetterText")).toHaveValue(letter);
  await expect(page.locator("#s4")).toHaveCount(0);
  await expect(page.getByLabel("Assigned certified mailing price")).toHaveCount(0);
  await expect(page.locator("#cardField")).toHaveCount(0);
  expect(draftCalls).toBeGreaterThanOrEqual(1);
});

// Status-level evidence, not context alone: an existing paid case keeps its own
// saved payment/window and is never relabeled free under a controlled phase.
test("existing admitted paid case under controlled phase keeps saved-payment copy and its saved work", async ({ page }) => {
  await page.route("**/api/auth/session", (route) => route.fulfill({ status: 401, json: { detail: "no_session" } }));
  await page.route("**/api/auth/pricing", (route) => route.fulfill({ json: assignment("controlled_free_beta", 2499, "P2499") }));
  await page.route("**/api/auth/appeal", (route) => route.fulfill({ json: workspace }));
  await page.route("**/api/cases/case_gate/status", (route) => route.fulfill({ json: {
    ...status,
    payment_state: "AUTHORIZED",
    state: "RESEARCHING",
    amount_cents: 2499,
    selected_tier: "CERTIFIED",
    window_started_at: "2026-11-07T12:00:00Z",
    window_expires_at: "2026-11-07T12:30:00Z",
    seconds_remaining: 1200,
    window_status: "ACTIVE",
  } }));
  await page.route("**/api/cases/case_gate/beta-offer", (route) => route.fulfill({ status: 409, json: { detail: "beta_entitlement_required" } }));
  await page.goto("/");
  await expect(page.locator("#s3")).toBeVisible();
  await expect(page.locator("#asideCharge")).toContainText("Your existing payment stays the same");
  await expect(page.locator("#asideCharge")).not.toContainText("No card and no payment");
  await expect(page.locator("#asideCharge")).not.toContainText("card is charged");
  await expect(page.getByLabel("Assigned certified mailing price")).toHaveCount(0);
  await expect(page.locator("#cardField")).toHaveCount(0);
});

test("failed offer refresh under paid phase keeps funding unresolved without a paid flash", async ({ page }) => {
  await instrument(page);
  let refresh = false;
  await mockUnselectedIntake(page, "controlled_free_beta");
  await page.route("**/api/auth/pricing", (route) => route.fulfill({ json: assignment(refresh ? "paid" : "controlled_free_beta", 2499, "P2499") }));
  await page.route("**/api/cases/case_gate/beta-offer", (route) => route.fulfill(refresh
    ? { status: 503, json: { detail: "request_unavailable" } }
    : { status: 409, json: { detail: "beta_entitlement_required" } }));
  await page.goto("/");
  await expect(page.locator("#h-s4")).toHaveText("This controlled beta has no new paid mailing option.");
  refresh = true;
  await page.getByRole("button", { name: "Check my appeal" }).first().click();
  await expect(page.locator("#liveActionError")).toBeVisible();
  await expect(page.getByLabel("Assigned certified mailing price")).toHaveCount(0);
  await expect(page.locator("#cardField")).toHaveCount(0);
  await expect(page.locator("#asideCharge")).not.toContainText("card is charged");
  expect(await activity(page)).toEqual({ stripe: 0, card: 0, price: 0 });
});
