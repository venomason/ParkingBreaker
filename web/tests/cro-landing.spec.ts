import { expect, test, type Page } from "@playwright/test";
import type { AxeResults } from "axe-core";

// UI presentation probes with explicit response fixtures. Durable API behavior is
// covered separately by durable-intake and the paid/beta synthetic campaigns.
async function visitor(page: Page, phase: "paid" | "controlled_free_beta") {
  await page.route("**/api/auth/session", route => route.fulfill({ status: 401, json: { detail: "no_session" } }));
  await page.route("**/api/auth/appeal", route => route.fulfill({ status: 401, json: { detail: "no_session" } }));
  await page.route("**/api/auth/pricing", route => route.fulfill({ json: {
    experiment_id: "certified-price-v1", service_phase: phase,
    amount_cents: phase === "paid" ? 2499 : null, arm: phase === "paid" ? "P2499" : null,
  } }));
}

async function accessible(page: Page) {
  await page.addScriptTag({ path: require.resolve("axe-core/axe.min.js") });
  const violations = await page.evaluate(async () => {
    const axe = (window as unknown as { axe: { run: (context: Document, options: object) => Promise<AxeResults> } }).axe;
    const result = await axe.run(document, { runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"] } });
    return result.violations.map(({ id, nodes }) => ({ id, targets: nodes.map(node => node.target) }));
  });
  expect(violations).toEqual([]);
}

for (const phase of ["paid", "controlled_free_beta"] as const) {
  for (const width of [320, 375, 768, 1440]) {
    test(`${phase} landing at ${width}px keeps intake, keyboard action and factual trust usable`, async ({ page }, testInfo) => {
      await page.setViewportSize({ width, height: 900 });
      await page.emulateMedia({ reducedMotion: "reduce" });
      await visitor(page, phase);
      await page.goto("/");
      await expect(page.locator("#actSnap")).toBeVisible();
      await expect(page.getByRole("heading", { level: 1 })).toHaveText("Appeal your parking ticket in your own words. We handle the mail.");
      await expect(page.locator(".workflow-list > li")).toHaveCount(6);
      await expect(page.locator(".intake-trust")).toContainText("Delivery is not guaranteed");
      await expect(page.locator(".intake-trust")).toContainText("without evaluating the merits");
      await expect(page.locator(".desk-aside")).toHaveCount(0);
      if (phase === "paid") await expect(page.getByLabel("Assigned certified mailing price")).toContainText("$24.99");
      else {
        await expect(page.getByLabel("Assigned certified mailing price")).toHaveCount(0);
        await expect(page.locator("#actAuthBtn, #card-element")).toHaveCount(0);
        await expect(page.locator("main")).not.toContainText(/\$\d/);
      }
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      const intro = (await page.locator(".intake-introduction").boundingBox())!;
      const intake = (await page.locator("#s1").boundingBox())!;
      const workflow = (await page.locator(".intake-support").boundingBox())!;
      expect(workflow.y).toBeGreaterThanOrEqual(intake.y + intake.height);
      if (width >= 1024) expect(intro.x + intro.width).toBeLessThan(intake.x);
      else expect(intro.y + intro.height).toBeLessThanOrEqual(intake.y);
      await page.locator("#actSnap").focus();
      await page.keyboard.press("Shift+Tab");
      await page.keyboard.press("Tab");
      await expect(page.locator("#actSnap")).toBeFocused();
      expect(await page.locator("#actSnap").evaluate(el => getComputedStyle(el).outlineStyle)).not.toBe("none");
      const action = (await page.locator("#actSnap").boundingBox())!;
      expect(action.width).toBeGreaterThanOrEqual(44);
      expect(action.height).toBeGreaterThanOrEqual(44);
      expect(action.x).toBeGreaterThanOrEqual(0);
      expect(action.x + action.width).toBeLessThanOrEqual(width);
      expect(action.y + action.height).toBeLessThanOrEqual(900);
      await accessible(page);
      await page.screenshot({ path: testInfo.outputPath(`landing-${width}.png`), fullPage: true });
    });
  }
}

for (const pending of ["appeal", "pricing"] as const) {
  test(`${pending} loading is announced, reduced-motion safe and keeps start-another access`, async ({ page }, testInfo) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await visitor(page, "paid");
    let release!: () => void;
    const blocked = new Promise<void>(resolve => { release = resolve; });
    await page.route(`**/api/auth/${pending}`, async route => {
      await blocked;
      await route.fallback();
    });
    try {
      await page.goto("/");
      await expect(page.locator(".access-loading [role=status]")).toContainText("Loading");
      await expect(page.getByRole("button", { name: "Start another appeal", exact: true })).toBeEnabled();
      await expect(page.locator("#actSnap")).toHaveCount(0);
      await expect(page.locator(".access-skeleton")).toHaveAttribute("aria-hidden", "true");
      const animations = await page.locator(".access-skeleton span").evaluateAll(elements => elements.map(el => getComputedStyle(el).animationName));
      expect(animations.every(name => name === "none")).toBe(true);
      await accessible(page);
      await page.screenshot({ path: testInfo.outputPath(`loading-${pending}.png`) });
    } finally { release(); }
    await expect(page.locator("#actSnap")).toBeVisible();
  });
}
