import { expect, test, type Page } from "./synthetic-fixtures";

const base = process.env.SECURE_BASE;
const apiBase = process.env.INGRESS_API_BASE;
const workerSecret = process.env.INGRESS_WORKER_SECRET;
test.use({ ignoreHTTPSErrors: true });
test.skip(!base || !apiBase || !workerSecret, "requires the isolated HTTPS SQLite-backed fake-provider rehearsal");

const png = "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAFklEQVR4nGP8//8/AwMDEwMDAwMDAwAkBgMB/DXemwAAAABJRU5ErkJggg==";
const address = {
  recipient: "Synthetic Appeals Office",
  attention: null,
  line1: "1 Test Plaza",
  line2: null,
  city: "Testville",
  state: "CA",
  zip_code: "94100",
};

async function api(page: Page, method: string, path: string, body?: unknown) {
  return page.evaluate(async ({ method, path, body }) => {
    const csrf = document.cookie.split("; ").find(item => item.startsWith("__Host-pb-csrf="))?.split("=")[1] ?? "";
    const response = await fetch(`/api${path}`, {
      method,
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    return { status: response.status, body: await response.json() as Record<string, unknown> };
  }, { method, path, body });
}

async function createAuthorizedUnverifiedCase(page: Page, citation: string) {
  await page.goto(base!);
  const started = await api(page, "POST", "/auth/appeals", {});
  expect(started.status).toBe(201);
  const workspace = await api(page, "POST", "/workspaces", { notes: "" });
  expect(workspace.status).toBe(201);
  const workspaceId = String(workspace.body.workspace_id);
  const photo = await api(page, "POST", `/workspaces/${workspaceId}/photos`, {
    content_type: "image/png",
    data_base64: png,
  });
  expect(photo.status).toBe(201);
  const intake = await api(page, "POST", `/workspaces/${workspaceId}/intake`, {
    ticket: {
      citation_number: citation,
      license_plate: "SYN123",
      license_plate_state: "CA",
      issuer_hint: "San Francisco Municipal Transportation Agency",
      issued_city: "San Francisco",
      issued_state: "CA",
      violation_code: "Meter expired",
      violation_description: "Meter expired",
      issue_date: new Date(Date.now()-3*86400000).toISOString().slice(0,10),
      stage: "first-level review",
    },
    customer_position: { statement: "The meter was broken; I have a photo." },
    contact: {
      name: "Synthetic Customer",
      phone: "415-555-0100",
      email: `${citation.toLowerCase()}@example.test`,
      mailing_address: { line1: "123 Example Street", city: "San Francisco", state: "CA", zip_code: "94103" },
    },
    photo_refs: [photo.body.photo_ref],
    citation_photo_ref: photo.body.photo_ref,
    rental_company_paid: "NOT_RENTAL",
  });
  expect(intake.status, JSON.stringify(intake.body)).toBe(200);
  const caseId = String(intake.body.case_id);
  expect((await api(page, "PUT", `/cases/${caseId}/tier`, { tier: "CERTIFIED" })).status).toBe(200);
  expect((await api(page, "POST", `/cases/${caseId}/authorization`, {
    terms_accepted: true,
    payment_method_id: "pm_card_visa",
  })).status).toBe(200);
  const research = await page.request.post(`${apiBase}/internal/cases/${caseId}/step`, {
    headers: { Authorization: `Bearer ${workerSecret}` },
    data: { step_id: "research" },
  });
  expect(research.status(), await research.text()).toBe(200);
  await page.reload();
  await expect(page.locator("#liveStopMessage")).toContainText("correct address for an appeal at this stage");
  return caseId;
}

async function proof(page: Page, caseId: string) {
  const response = await page.request.get(`${apiBase}/__harness__/proof/${caseId}`);
  expect(response.status()).toBe(200);
  return response.json() as Promise<{
    address_revisions: number;
    consent_events: number;
    decision_history: number;
    latest_decision: null | {
      provenance: string;
      refund_policy_version: string;
      address_revision: number;
      address_fingerprint: string;
      disclosure_sha256: string;
      consent_case_matches: boolean;
      consent_owner_matches: boolean;
      consent_is_explicit: boolean;
    };
    consent_events_match_decision: boolean;
  }>;
}

test("unverified destination stops durably without accepting a new address waiver", async ({ page, browser }) => {
  const caseId = await createAuthorizedUnverifiedCase(page, "SYN-T30-A");
  const status = (await api(page, "GET", `/cases/${caseId}/status`)).body;
  expect(status.state).toBe("STOPPED_NO_CHARGE");
  expect(status.destination_choice ?? null).toBeNull();
  await expect(page.locator("#destSubmit")).toHaveCount(0);
  const before = await proof(page, caseId);
  expect(before).toMatchObject({ address_revisions: 0, consent_events: 0, decision_history: 0, latest_decision: null });
  const addressResult = await api(page, "POST", `/cases/${caseId}/destination/address`, address);
  expect(addressResult.status).toBe(409);
  expect(addressResult.body.detail).toBe("destination_verification_required");
  const consentResult = await api(page, "POST", `/cases/${caseId}/destination/consent`, {
    address_revision: 1, address_fingerprint: "a".repeat(64), disclosure_sha256: "b".repeat(64), accepted: true,
  });
  expect(consentResult.status).toBe(409);
  expect(consentResult.body.detail).toBe("destination_verification_required");
  await page.reload();
  await expect(page.locator("#liveStopMessage")).toContainText("correct address for an appeal at this stage");
  expect(await proof(page, caseId)).toEqual(before);
  const stranger = await browser.newContext({ ignoreHTTPSErrors: true });
  try {
    const other = await stranger.newPage();
    await other.goto(base!);
    expect((await api(other, "POST", "/auth/appeals", {})).status).toBe(201);
    expect((await api(other, "GET", `/cases/${caseId}/status`)).status).toBe(404);
  } finally { await stranger.close(); }
});
