import { expect, test, type Page } from "@playwright/test";

// Bounded browser probes for the private runtime phase. The backend owns enforcement;
// these checks cover an already-open screen asking for fresh posture on focus/visibility
// and never restoring a paid checkout from a failed or still-pending refresh.
//
// The fixture API is stubbed at the HTTP boundary. No real provider, database or
// credentials are involved.

type Phase = "paid" | "controlled_free_beta";

function assignment(phase: Phase) {
  return {
    experiment_id: "certified-price-v1",
    service_phase: phase,
    amount_cents: phase === "paid" ? 2499 : null,
    arm: phase === "paid" ? "P2499" : null,
  };
}

async function stubVisitor(page: Page) {
  await page.route("**/api/auth/session", (route) =>
    route.fulfill({ status: 401, json: { detail: "no_session" } }),
  );
  await page.route("**/api/auth/appeal", (route) =>
    route.fulfill({ status: 401, json: { detail: "no_session" } }),
  );
}

async function returnToVisible(page: Page) {
  // The app coalesces a focus and visibility event into one refresh; wait past that
  // window before triggering the next deliberate one.
  await page.waitForTimeout(1100);
  await page.evaluate(() => {
    window.dispatchEvent(new Event("focus"));
  });
}

test("focus and visibility refresh the server posture without remounting the journey", async ({
  page,
}) => {
  let phase: Phase = "paid";
  let requests = 0;
  await stubVisitor(page);
  let workspace = { workspace_id: "ws_runtime", case_id: null, draft_revision: 1, ticket_reading_required: false,
    intake_form: { values: { story: "My saved statement." }, step: 2 } };
  await page.route("**/api/auth/appeal", route => route.fulfill({ json: workspace }));
  await page.route("**/api/workspaces/ws_runtime/ticket-reading", route => route.fulfill({ json: {
    required: false, enabled: false, extraction: { status: "missing" }, review: {}, confirmation: { state: "missing" },
  } }));
  await page.route("**/api/workspaces/ws_runtime/intake-form", route => {
    workspace = { ...workspace, draft_revision: workspace.draft_revision + 1, intake_form: route.request().postDataJSON().form };
    return route.fulfill({ json: workspace });
  });
  await page.route("**/api/auth/pricing", (route) => {
    requests += 1;
    return route.fulfill({ json: assignment(phase) });
  });

  await page.goto("/");
  const price = page.getByLabel("Assigned certified mailing price");
  await expect(price).toContainText("$24.99");

  // A mounted journey with customer input must survive the posture refresh untouched.
  await expect(page.locator("#story")).toHaveValue("My saved statement.");
  await page.locator("#story").fill("My revised statement remains my own words.");
  const beforeFocus = requests;
  phase = "controlled_free_beta";
  await returnToVisible(page);
  await expect.poll(() => requests).toBeGreaterThan(beforeFocus);
  await expect(price).toHaveCount(0);
  await expect(page.locator("#story")).toHaveValue("My revised statement remains my own words.");
  await expect(page.locator("#s2")).toBeVisible();

  // A visibility return asks again on its own and still reports the switched posture.
  await page.waitForTimeout(1100);
  const beforeVisibility = requests;
  await page.evaluate(() => {
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect.poll(() => requests).toBeGreaterThan(beforeVisibility);
  await expect(price).toHaveCount(0);

  // A fresh page load reflects the current server mode with no local carry-over.
  const fresh = await page.context().newPage();
  await stubVisitor(fresh);
  await fresh.route("**/api/auth/pricing", (route) =>
    route.fulfill({ json: assignment("controlled_free_beta") }),
  );
  await fresh.goto("/");
  await expect(fresh.getByLabel("Assigned certified mailing price")).toHaveCount(0);
  await expect(fresh.locator("#actSnap")).toBeVisible();
  await fresh.close();
});

test("a failed refresh blocks paid UI again and a pending retry cannot flash it", async ({
  page,
}) => {
  let requests = 0;
  let failNext = false;
  let delayNext = false;
  let blocked = Promise.resolve();
  let release: () => void = () => undefined;

  await stubVisitor(page);
  await page.route("**/api/auth/pricing", async (route) => {
    requests += 1;
    if (failNext) {
      failNext = false;
      return route.fulfill({ status: 503, json: { detail: "unavailable" } });
    }
    if (delayNext) {
      delayNext = false;
      await blocked;
    }
    return route.fulfill({ json: assignment("paid") });
  });

  await page.goto("/");
  const price = page.getByLabel("Assigned certified mailing price");
  await expect(price).toContainText("$24.99");

  // One failed refresh withholds the paid posture immediately.
  failNext = true;
  const beforeFail = requests;
  await returnToVisible(page);
  await expect.poll(() => requests).toBeGreaterThan(beforeFail);
  await expect(price).toHaveCount(0);
  await expect(page.locator("#cardField")).toHaveCount(0);

  // A delayed retry is still pending. The earlier failure must keep blocking the paid
  // price until this fresh response is fully validated.
  await page.waitForTimeout(1100);
  blocked = new Promise<void>((resolve) => {
    release = resolve;
  });
  delayNext = true;
  const beforeRetry = requests;
  await page.evaluate(() => {
    window.dispatchEvent(new Event("focus"));
  });
  await expect.poll(() => requests).toBeGreaterThan(beforeRetry);
  await expect(price).toHaveCount(0);
  await expect(page.locator("#cardField")).toHaveCount(0);

  release();
  await expect(price).toContainText("$24.99");
  await expect(page.locator("#actSnap")).toBeVisible();
});
