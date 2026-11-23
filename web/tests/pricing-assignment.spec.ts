import { expect, test } from "@playwright/test";

// Exact response shape for the new contract: the phase is never defaulted.
const assignment = (amount_cents: number | null, arm: string | null, service_phase: string) => ({
  experiment_id: "certified-price-v1",
  arm,
  amount_cents,
  service_phase,
});

async function mockBase(page: import("@playwright/test").Page) {
  await page.route("**/api/auth/session", (route) => route.fulfill({ status: 401, json: { detail: "no_session" } }));
  await page.route("**/api/auth/appeal", (route) => route.fulfill({ status: 401, json: { detail: "no_session" } }));
}

test("assigned price is displayed and gates the live journey before it renders", async ({ page }) => {
  const requests: string[] = [];
  let release!: () => void;
  const blocked = new Promise<void>((resolve) => { release = resolve; });
  await mockBase(page);
  await page.addInitScript(() => { document.cookie = "__Host-pb-csrf=visitor-csrf; Secure; Path=/; SameSite=Lax"; });
  await page.route("**/api/auth/pricing", async (route) => {
    requests.push(route.request().method());
    expect(route.request().headers()["x-csrf-token"]).toBe("visitor-csrf");
    await blocked;
    await route.fulfill({ json: assignment(2499, "P2499", "paid") });
  });
  await page.goto("/");
  await expect.poll(() => requests).toEqual(["POST"]);
  await expect(page.getByRole("status")).toContainText("Loading");
  await expect(page.getByRole("button", { name: "Start an appeal" })).toBeEnabled();
  await expect(page.locator("#actSnap")).toHaveCount(0);
  release();
  await expect(page.getByLabel("Assigned certified mailing price")).toHaveText("Early beta price: $24.99 · Certified Mail, everything included");
  await expect(page.locator("#actSnap")).toBeVisible();
});

test("another visitor assignment arm is displayed without a hardcoded fallback", async ({ page }) => {
  await mockBase(page);
  await page.route("**/api/auth/pricing", (route) => route.fulfill({ json: assignment(1999, "P1999", "paid") }));
  await page.goto("/");
  await expect(page.getByLabel("Assigned certified mailing price")).toHaveText("Early beta price: $19.99 · Certified Mail, everything included");
});

test("pricing failure has no default, offers retry, and keeps children gated until retry resolves", async ({ page }) => {
  let calls = 0;
  await mockBase(page);
  await page.route("**/api/auth/pricing", (route) => {
    calls++;
    if (calls === 1) return route.abort();
    return route.fulfill({ json: assignment(2999, "P2999", "paid") });
  });
  await page.goto("/");
  await expect(page.locator("p[role=alert]")).toContainText("couldn’t load pricing");
  await expect(page.getByRole("main")).toHaveCount(1);
  await expect(page.getByRole("heading", { level: 1, name: "Your appeal service is unavailable", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Start an appeal" })).toBeEnabled();
  await expect(page.getByLabel("Assigned certified mailing price")).toHaveCount(0);
  await expect(page.locator("#actSnap")).toHaveCount(0);
  await page.getByRole("button", { name: "Retry" }).click();
  await expect(page.getByLabel("Assigned certified mailing price")).toHaveText("Early beta price: $29.99 · Certified Mail, everything included");
  await expect(page.locator("#actSnap")).toBeVisible();
  expect(calls).toBe(2);
});

test("retry keeps journey gated until delayed pricing assignment resolves", async ({ page }) => {
  let calls = 0;
  let release!: () => void;
  const pending = new Promise<void>((resolve) => { release = resolve; });
  await mockBase(page);
  await page.route("**/api/auth/pricing", async (route) => {
    calls++;
    if (calls === 1) return route.abort();
    await pending;
    return route.fulfill({ json: assignment(2499, "P2499", "paid") });
  });
  await page.goto("/");
  await expect(page.locator("p[role=alert]")).toContainText("couldn’t load pricing");
  await page.getByRole("button", { name: "Retry" }).click();
  await expect(page.getByRole("button", { name: "Start an appeal" })).toBeEnabled();
  await expect(page.locator("#actSnap")).toHaveCount(0);
  release();
  await expect(page.getByLabel("Assigned certified mailing price")).toHaveText("Early beta price: $24.99 · Certified Mail, everything included");
  await expect(page.locator("#actSnap")).toBeVisible();
  expect(calls).toBe(2);
});

test("null legacy assignment displays no price and preserves access controls", async ({ page }) => {
  await mockBase(page);
  await page.route("**/api/auth/pricing", (route) => route.fulfill({ json: assignment(null, null, "paid") }));
  await page.route("**/api/auth/appeals", (route) => route.fulfill({ json: { authenticated: true } }));
  await page.goto("/");
  await expect(page.getByLabel("Assigned certified mailing price")).toHaveCount(0);
  await expect(page.locator("#actSnap")).toBeVisible();
  await page.getByRole("button", { name: "Start an appeal" }).click();
  await expect(page).toHaveURL(/\/$/);
});

test("malformed assignment stays blocked with retry and no price", async ({ page }) => {
  let calls = 0;
  await mockBase(page);
  await page.route("**/api/auth/pricing", (route) => {
    calls++;
    // The phase is present and paid; only the raw arm/amount pair is invalid.
    return route.fulfill({ json: calls === 1 ? { experiment_id: "certified-price-v1", arm: "P2499", amount_cents: 1999, service_phase: "paid" } : assignment(2499, "P2499", "paid") });
  });
  await page.goto("/");
  await expect(page.locator("p[role=alert]")).toContainText("couldn’t load pricing");
  await expect(page.locator("#actSnap")).toHaveCount(0);
  await expect(page.getByLabel("Assigned certified mailing price")).toHaveCount(0);
  await page.getByRole("button", { name: "Retry" }).click();
  await expect(page.getByLabel("Assigned certified mailing price")).toHaveText("Early beta price: $24.99 · Certified Mail, everything included");
  expect(calls).toBe(2);
});

test("server-confirmed beta case survives pricing failure without a paid price", async ({ page }) => {
  await page.route("**/api/auth/session", (route) => route.fulfill({ status: 401, json: { detail: "no_session" } }));
  await page.route("**/api/auth/appeal", (route) => route.fulfill({ json: { workspace_id: "ws_beta", case_id: "case_beta" } }));
  await page.route("**/api/auth/pricing", (route) => route.abort());
  await page.route("**/api/cases/case_beta/status", (route) => route.fulfill({ json: { case_id: "case_beta", payment_state: "NO_CHARGE", state: "RESEARCHING" } }));
  await page.goto("/");
  await expect(page.getByLabel("Assigned certified mailing price")).toHaveCount(0);
  await expect(page.getByRole("heading", { level: 1, name: "Your appeal service is unavailable", exact: true })).toHaveCount(0);
  await expect(page.locator("#s1")).toBeVisible();
  await expect(page.getByRole("button", { name: "Start another appeal", exact: true })).toBeEnabled();
});

// UI-only phase mocks: the real server gate remains the authority.
test("controlled free beta never exposes the assigned paid price", async ({ page }) => {
  await mockBase(page);
  await page.route("**/api/auth/pricing", (route) => route.fulfill({ json: assignment(2499, "P2499", "controlled_free_beta") }));
  await page.goto("/");
  await expect(page.getByLabel("Assigned certified mailing price")).toHaveCount(0);
  await expect(page.locator("#actSnap")).toBeVisible();
  await expect(page.getByRole("button", { name: "Start an appeal" })).toBeEnabled();
});

test("missing service_phase fails closed, shows retry and never defaults paid", async ({ page }) => {
  let calls = 0;
  await mockBase(page);
  await page.route("**/api/auth/pricing", (route) => {
    calls++;
    return route.fulfill({ json: calls === 1 ? { experiment_id: "certified-price-v1", arm: "P2499", amount_cents: 2499 } : assignment(2499, "P2499", "paid") });
  });
  await page.goto("/");
  await expect(page.locator("p[role=alert]")).toContainText("couldn’t load pricing");
  await expect(page.getByLabel("Assigned certified mailing price")).toHaveCount(0);
  await expect(page.locator("#actSnap")).toHaveCount(0);
  await page.getByRole("button", { name: "Retry" }).click();
  await expect(page.getByLabel("Assigned certified mailing price")).toHaveText("Early beta price: $24.99 · Certified Mail, everything included");
  expect(calls).toBe(2);
});

test("unknown service_phase stays gated with no paid price or journey", async ({ page }) => {
  await mockBase(page);
  await page.route("**/api/auth/pricing", (route) => route.fulfill({ json: assignment(2499, "P2499", "unexpected_phase") }));
  await page.goto("/");
  await expect(page.locator("p[role=alert]")).toContainText("couldn’t load pricing");
  await expect(page.getByLabel("Assigned certified mailing price")).toHaveCount(0);
  await expect(page.locator("#actSnap")).toHaveCount(0);
});
