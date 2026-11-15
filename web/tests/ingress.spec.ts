import { expect, test } from "@playwright/test";

const base = process.env.PRODUCTION_BASE;
const api = process.env.INGRESS_API_BASE;
const worker = process.env.INGRESS_WORKER_SECRET;

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
