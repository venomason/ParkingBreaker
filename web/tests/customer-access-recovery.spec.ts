import { expect, test, type Page } from "@playwright/test";

const price = { experiment_id: "certified-price-v1", arm: "P2499", amount_cents: 2499, service_phase: "paid" };
const startButton = (page: Page) => page.getByRole("button", { name: "Start another appeal", exact: true });

async function session(page: Page) {
  await page.route("**/api/auth/session", (route) => route.fulfill({ status: 401, json: { detail: "no_session" } }));
}

for (const failure of ["network", "500"] as const) {
  test(`${failure} on both access reads recovers with one saved-status retry`, async ({ page }) => {
    await session(page);
    let failing = true;
    let pricingCalls = 0;
    await page.route("**/api/auth/appeal", (route) => failing
      ? failure === "network" ? route.abort() : route.fulfill({ status: 500, body: "upstream failed" })
      : route.fulfill({ status: 401, json: { detail: "no_session" } }));
    await page.route("**/api/auth/pricing", (route) => {
      pricingCalls += 1;
      return failing
        ? failure === "network" ? route.abort() : route.fulfill({ status: 500, body: "upstream failed" })
        : route.fulfill({ json: price });
    });
    await page.goto("/");
    await expect(page.getByRole("button", { name: "Retry saved status" })).toBeVisible();
    await expect(startButton(page)).toBeEnabled();
    await expect(page.locator("#actSnap")).toHaveCount(0);
    await expect(page.getByLabel("Assigned certified mailing price")).toHaveCount(0);
    const before = pricingCalls;
    failing = false;
    await page.getByRole("button", { name: "Retry saved status" }).click();
    await expect(page.locator("#actSnap")).toBeVisible();
    expect(pricingCalls).toBeGreaterThan(before);
  });
}

test("fresh appeal can be requested during outage and retried after a failed start", async ({ page }) => {
  await session(page);
  let started = false;
  let starts = 0;
  await page.route("**/api/auth/pricing", (route) => started
    ? route.fulfill({ json: price }) : route.fulfill({ status: 500, body: "unavailable" }));
  await page.route("**/api/auth/appeal", (route) => started
    ? route.fulfill({ json: null }) : route.abort());
  await page.route("**/api/auth/appeals", (route) => {
    starts += 1;
    if (starts === 1) return route.fulfill({ status: 500, body: "unavailable" });
    started = true;
    return route.fulfill({ status: 201, json: { authenticated: true } });
  });
  await page.goto("/");
  await expect(page.getByRole("button", { name: "Retry saved status" })).toBeVisible();
  await startButton(page).click();
  await expect(page.getByText("We couldn’t change appeal access.", { exact: false })).toBeVisible();
  await expect(startButton(page)).toBeEnabled();
  await startButton(page).click();
  await expect(page.locator("#actSnap")).toBeVisible();
  expect(starts).toBe(2);
});

for (const endpoint of ["appeal", "pricing", "status", "beta-offer", "appeals"] as const) {
  test(`hung ${endpoint} request times out and offers recovery`, async ({ page }) => {
    await session(page);
    let failing = true;
    let hungRequests = 0;
    await page.clock.install();
    await page.route("**/api/auth/appeal", (route) => route.fulfill({ json:
      endpoint === "status" || endpoint === "beta-offer" ? { workspace_id: "ws_timeout", case_id: "case_timeout" } : null,
    }));
    await page.route("**/api/auth/pricing", (route) => route.fulfill({ json: price }));
    await page.route("**/api/cases/case_timeout/status", (route) => route.fulfill({ json: { payment_state: "PENDING" } }));
    await page.route("**/api/cases/case_timeout/beta-offer", (route) => route.fulfill({ status: 409, json: { detail: "beta_entitlement_required" } }));
    const url = endpoint === "status" || endpoint === "beta-offer"
      ? `**/api/cases/case_timeout/${endpoint}` : `**/api/auth/${endpoint}`;
    await page.route(url, async (route) => {
      if (failing) { hungRequests += 1; return; }
      if (endpoint === "appeals") return route.fulfill({ status: 500, body: "retry reached server" });
      await route.fallback();
    });
    await page.goto("/");
    if (endpoint === "appeals") await startButton(page).click();
    await expect.poll(() => hungRequests).toBeGreaterThan(0);
    await page.clock.fastForward(12_001);
    if (endpoint === "appeals") {
      await expect(page.getByText("We couldn’t change appeal access.", { exact: false })).toBeVisible();
      await expect(startButton(page)).toBeEnabled();
    } else {
      const retry = page.getByRole("button", { name: endpoint === "pricing" ? "Retry" : "Retry saved status", exact: true });
      await expect(retry).toBeVisible();
      await expect(startButton(page)).toBeEnabled();
      await expect(page.locator("#actSnap")).toHaveCount(0);
      failing = false;
      await retry.click();
      await expect(page.locator("#s1")).toBeAttached();
    }
  });
}

test("retry rechecks a private workspace link before opening another scope", async ({ page }) => {
  await session(page);
  let failing = true;
  await page.route("**/api/auth/pricing", (route) => route.fulfill({ json: price }));
  await page.route("**/api/auth/appeal", (route) => failing
    ? route.abort() : route.fulfill({ json: { workspace_id: "different_workspace", case_id: null } }));
  await page.goto("/?w=private_workspace");
  await expect(page.getByRole("button", { name: "Retry saved status" })).toBeVisible();
  failing = false;
  await page.getByRole("button", { name: "Retry saved status" }).click();
  await expect(page.getByRole("heading", { name: "Return to this appeal" })).toBeVisible();
  await expect(page.locator("#actSnap")).toHaveCount(0);
});
