import { expect, test } from "@playwright/test";

const base = process.env.PRODUCTION_BASE;
const api = process.env.INGRESS_API_BASE;
const worker = process.env.INGRESS_WORKER_SECRET;

for (const separator of ["#", "?"]) {
  test(`outcome link with ${separator} permits a retry and clears the address bar`, async ({ page }) => {
    const token = "a".repeat(64);
    const reports: object[] = [];
    await page.route("**/api/outcomes/report", async (route) => {
      reports.push(route.request().postDataJSON());
      await route.fulfill({ status: reports.length === 1 ? 503 : 200, body: "{}" });
    });
    const main = await page.goto(`/outcomes/opt-out${separator}case=synthetic-case&token=${token}`);
    expect(main?.headers()["referrer-policy"]).toBe("no-referrer");
    const pending = page.getByRole("button", { name: "I have not heard back yet" });
    await expect(pending).toBeVisible();
    expect(new URL(page.url()).href).toMatch(/\/outcomes\/opt-out$/);
    expect(reports).toEqual([]);
    await pending.click();
    await expect(page.getByRole("alert").filter({ hasText: "We could not record your choice" })).toBeVisible();
    await pending.click();
    await expect(page.getByText("Thank you. We will leave the outcome unrecorded until you hear back.")).toBeVisible();
    const expected = { case_id: "synthetic-case", token, decision: "pending" };
    expect(reports).toEqual([expected, expected]);
  });
}

test("production ingress excludes private worker routes while direct worker remains authenticated", async ({ request }) => {
  test.skip(!base || !api || !worker, "needs an explicit local production web/private API rehearsal");
  expect((await request.get(`${base}/api/health`)).status()).toBe(200);
  for (const path of ["/api/internal/dispatch", "/api/internal/cases/case_123/step"]) {
    const response = await request.post(`${base}${path}`, {
      headers: { Authorization: `Bearer ${worker}` },
      data: { step_id: "research" },
    });
    expect(response.status()).toBe(404);
  }
  expect((await request.post(`${base}/dev-worker/case_123/research`)).status()).toBe(404);
  expect((await request.post(`${api}/internal/dispatch`)).status()).toBe(401);
  expect((await request.post(`${api}/internal/dispatch`, {
    headers: { Authorization: `Bearer ${worker}` },
  })).status()).toBe(200);
});
