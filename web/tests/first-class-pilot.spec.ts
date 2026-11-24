import { createHash } from "node:crypto";
import { expect, test, type Page } from "@playwright/test";
import type { CaseStatus, MailTier, PilotFeedbackRequest, PilotFeedbackSnapshot } from "../lib/api";

const caseId = "case_first_class_synthetic";
const workspaceId = "ws_first_class_synthetic";
const acceptedAt = "2026-11-23T20:00:00Z";
const sha256 = (value: string) => createHash("sha256").update(value).digest("hex");

const workspace = {
  workspace_id: workspaceId,
  case_id: caseId,
  intake_form: { values: {}, step: 2 },
  intake: {
    ticket: {
      citation_number: "SYN-PILOT-1", license_plate: "SYN123", license_plate_state: "CA",
      issue_date: "2026-11-20", issuer_hint: "Synthetic Parking Office", violation_description: "Meter expired",
    },
    customer_position: { statement: "The parking meter screen was blank." },
  },
};

const emptyFeedback: PilotFeedbackSnapshot = {
  eligible: false, revision: 0, first_postal_scan_at: null, updated_at: null,
  routing: null, acknowledgment: null, disposition: null, source: "customer_reported",
};

test.beforeEach(async ({ context, baseURL }) => {
  const origin = new URL(baseURL ?? "http://127.0.0.1:3001");
  if (!["localhost", "127.0.0.1", "[::1]"].includes(origin.hostname) && !origin.hostname.endsWith(".example.test")) {
    throw new Error("The First-Class pilot tests require a reserved synthetic or loopback origin");
  }
  await context.route("**/*", (route) => {
    const url = new URL(route.request().url());
    return url.origin === origin.origin ? route.fallback() : route.abort("blockedbyclient");
  });
});

async function mockSelectedCase(page: Page, tier: MailTier, receiptTier = tier) {
  let accepted = false;
  let state: CaseStatus["state"] = "INTAKE";
  const paidRequests: string[] = [];
  const betaRequests: object[] = [];
  const disclosure = tier === "STANDARD"
    ? "Synthetic disclosure. Ordinary First-Class Mail costs $0. There is no tracking number, proof of delivery or delivery guarantee."
    : "Synthetic Certified Mail disclosure for $0.";
  const disclosureDigest = sha256(disclosure);
  const status = (): CaseStatus => ({
    case_id: caseId, workspace_id: workspaceId, state, fake: true,
    payment_state: accepted ? "NO_CHARGE" : null, selected_tier: accepted ? tier : null,
    amount_cents: accepted ? 0 : null,
    window_started_at: accepted ? acceptedAt : null, window_expires_at: null,
    seconds_remaining: accepted ? 1800 : null, window_status: null, question: null, stop: null,
  });

  page.on("request", (request) => {
    if (/\/(tier|authorization|authorization-recovery)$/.test(new URL(request.url()).pathname) || request.url().includes("stripe.com")) {
      paidRequests.push(request.url());
    }
  });
  await page.route("**/api/auth/session", (route) => route.fulfill({ json: { authenticated: true } }));
  await page.route("**/api/auth/pricing", (route) => route.fulfill({
    json: { experiment_id: "certified-price-v1", service_phase: "paid", amount_cents: 2499, arm: "P2499" },
  }));
  await page.route("**/api/auth/appeal", (route) => route.fulfill({ json: workspace }));
  await page.route(`**/api/cases/${caseId}/status`, (route) => route.fulfill({ json: status() }));
  await page.route(`**/api/cases/${caseId}/beta-offer`, (route) => route.fulfill({
    json: { case_id: caseId, funding: "NO_CHARGE", currency: "usd", amount_cents: 0, tier,
      disclosure_text: disclosure, disclosure_sha256: disclosureDigest, disclosure_version: 2,
      accepted_at: accepted ? acceptedAt : null },
  }));
  await page.route(`**/api/cases/${caseId}/beta-acceptance`, (route) => {
    betaRequests.push(route.request().postDataJSON());
    accepted = true;
    state = "RESEARCHING";
    return route.fulfill({
      json: { case_id: caseId, funding: "NO_CHARGE", amount_cents: 0, tier: receiptTier, accepted_at: acceptedAt },
    });
  });
  await page.route(`**/api/cases/${caseId}/tracking`, (route) => route.fulfill({
    json: { case_id: caseId, tier, state, tracking_number: "SYN-TRACKING",
      events: [{ event: "delivered", occurred_at: acceptedAt, description: "Synthetic delivery data" }],
    },
  }));
  return { betaRequests, paidRequests, disclosure, disclosureDigest, status, move: (next: CaseStatus["state"]) => { state = next; } };
}

function packetPdf() {
  const objects = [
    "<< /Type /Catalog /Pages 2 0 R >>",
    "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << >> /Contents 4 0 R >>",
    "<< /Length 0 >>\nstream\nendstream",
  ];
  let pdf = "%PDF-1.4\n";
  const offsets = objects.map((object, index) => {
    const offset = pdf.length;
    pdf += `${index + 1} 0 obj\n${object}\nendobj\n`;
    return `${String(offset).padStart(10, "0")} 00000 n \n`;
  });
  return `${pdf}xref\n0 5\n0000000000 65535 f \n${offsets.join("")}trailer\n<< /Size 5 /Root 1 0 R >>\nstartxref\n${pdf.length}\n%%EOF`;
}

test("First-Class beta uses one $0 exact-packet approval and lets the customer report each milestone", async ({ page }) => {
  const mock = await mockSelectedCase(page, "STANDARD");
  const text = "The parking meter screen was blank.";
  const adoptedDigest = sha256(text);
  const pdf = packetPdf();
  const packetDigest = sha256(pdf);
  const envelopeDigest = sha256("synthetic envelope");
  const approvals: object[] = [];
  let dispatches = 0;
  let snapshot = { ...emptyFeedback, eligible: true };
  const reports: PilotFeedbackRequest[] = [];
  await page.route(`**/api/cases/${caseId}/draft`, (route) => route.fulfill({
    json: { case_id: caseId, text, text_sha256: adoptedDigest, adopted_sha256: adoptedDigest, revision: 1 },
  }));
  await page.route(`**/api/cases/${caseId}/packet-preview`, (route) => route.fulfill({
    json: { pdf_base64: Buffer.from(pdf).toString("base64"), packet_sha256: packetDigest, envelope_sha256: envelopeDigest,
      letter_text_sha256: adoptedDigest, page_count: 1, mail_to: ["Synthetic Parking Office"], return_address: ["Synthetic Sender"], declarations: [] },
  }));
  await page.route(`**/api/cases/${caseId}/approval`, (route) => {
    approvals.push(route.request().postDataJSON());
    mock.move("FROZEN");
    return route.fulfill({ json: mock.status() });
  });
  await page.route(`**/dev-worker/${caseId}/dispatch`, (route) => {
    dispatches += 1;
    mock.move("IN_TRANSIT");
    return route.fulfill({ status: 404 });
  });
  await page.route(`**/api/cases/${caseId}/pilot-feedback`, (route) => {
    if (route.request().method() === "GET") return route.fulfill({ json: snapshot });
    const request: PilotFeedbackRequest = route.request().postDataJSON();
    reports.push(request);
    // Simulate a saved response whose first delivery failed.
    snapshot = { ...snapshot, routing: request.routing ?? null, disposition: request.disposition ?? null, revision: 1, updated_at: acceptedAt };
    return route.fulfill({ status: reports.length === 1 ? 503 : 200, json: snapshot });
  });

  await page.goto("/");
  await expect(page.locator("#betaOfferCard")).toContainText("Ordinary First-Class Mail");
  await expect(page.locator("#betaOfferCard")).toContainText("$0");
  await expect(page.locator("#betaDisclosureText")).toHaveText(mock.disclosure);
  await expect(page.locator("#actBetaBtn")).toBeDisabled();
  await page.locator("#betaConsent").check();
  await page.locator("#actBetaBtn").click();
  await expect(page.locator("#s5lead")).toContainText("Ordinary First-Class Mail, $0");
  await expect(page.locator("#s5lead")).toContainText("No card hold and no payment");
  expect(mock.betaRequests).toEqual([{ terms_accepted: true, disclosure_sha256: mock.disclosureDigest }]);

  mock.move("AWAITING_ADOPTION");
  await page.reload();
  await page.locator("#livePreview").click();
  await expect(page.locator("#livePacketPage")).toBeVisible();
  await expect(page.locator("#s6 .adoption-disclosure")).toContainText("send this exact packet once by Ordinary First-Class Mail");
  await expect(page.locator("#s6 .adoption-disclosure")).toContainText("for $0");
  await expect(page.locator("#liveApprove")).toBeDisabled();
  await page.locator("#livePacketReviewed").check();
  await page.locator("#liveApprove").click();
  await expect(page.locator("#h-s7")).toHaveText("Mailing recorded.");
  await expect(page.locator("#liveTracking")).toContainText("Ordinary First-Class Mail");
  await expect(page.locator("#liveTracking")).toContainText("without a tracking number, proof of delivery or a delivery guarantee");
  await expect(page.locator("#liveTracking")).not.toContainText("SYN-TRACKING");
  await expect(page.locator("#s7")).not.toContainText("USPS reports delivery");
  await expect(page.locator("#livePrepareMailingRecord")).toBeVisible();
  await page.locator("#liveApprove").click();
  await expect(page.locator("#h-s7")).toHaveText("Mailing recorded.");
  expect(dispatches).toBe(1);
  expect(approvals).toEqual([{ adopted_sha256: adoptedDigest, packet_sha256: packetDigest, envelope_sha256: envelopeDigest, affirmations: [] }]);
  await expect(page.locator("#cardField, #actAuthBtn")).toHaveCount(0);
  await expect(page.getByLabel("Assigned certified mailing price")).toHaveCount(0);
  expect(mock.paidRequests).toEqual([]);

  await expect(page.getByRole("combobox")).toHaveCount(3);
  await expect(page.getByRole("button", { name: "Save answers" })).toBeDisabled();
  await page.getByLabel("Did the mailing have a routing problem?").selectOption("wrong_office");
  await page.getByLabel("What did the agency decide?").selectOption("reduced");
  expect(reports).toEqual([]);
  await page.getByRole("button", { name: "Save answers" }).click();
  await expect(page.getByText("We couldn’t confirm that your answers were saved", { exact: false })).toBeVisible();
  await page.getByRole("button", { name: "Retry saving answers" }).click();
  await expect(page.getByText("Thanks. Your answers were saved.")).toBeVisible();
  const report = { request_id: expect.any(String), expected_revision: 0, routing: "wrong_office", disposition: "reduced" };
  expect(reports).toEqual([report, report]);
  expect(reports[1]).toEqual(reports[0]);
  await expect(page.getByLabel("Did the agency acknowledge the appeal?")).toHaveValue("");
  await expect(page.getByRole("button", { name: "Share an agency notice" })).toBeVisible();
});

test("saved free Certified Mail stays certified and keeps its notice upload", async ({ page }) => {
  const mock = await mockSelectedCase(page, "CERTIFIED");
  await page.goto("/");
  await expect(page.locator("#betaOfferCard")).toContainText("Certified Mail");
  await page.locator("#betaConsent").check();
  await page.locator("#actBetaBtn").click();
  await expect(page.locator("#s5lead")).toContainText("Certified Mail, $0");
  mock.move("DELIVERED");
  await page.reload();
  await expect(page.locator("#h-s7")).toHaveText("USPS reports delivery.");
  await expect(page.locator("#liveTracking")).toContainText("Certified tracking number:");
  await expect(page.getByRole("button", { name: "Share an agency notice" })).toBeVisible();
  await expect(page.locator("#pilotFeedbackCard")).toHaveCount(0);
  expect(mock.paidRequests).toEqual([]);
});

test("a receipt with a different mail class cannot open the review window", async ({ page }) => {
  await mockSelectedCase(page, "STANDARD", "CERTIFIED");
  await page.goto("/");
  await page.locator("#betaConsent").check();
  await page.locator("#actBetaBtn").click();
  await expect(page.locator("#betaError")).toContainText("The free beta acceptance could not be verified");
  await expect(page.locator("#s5lead, #cardField")).toHaveCount(0);
});

test("private pilot email opens voluntary feedback and opts out with its own token", async ({ page, context, baseURL }) => {
  const token = "a".repeat(64);
  const optOutToken = "b".repeat(64);
  const link = { case_id: caseId, token };
  let snapshot: PilotFeedbackSnapshot = { ...emptyFeedback, eligible: true, first_postal_scan_at: acceptedAt };
  const reports: object[] = [];
  await context.addCookies([{ name: "test-cookie", value: "present", url: baseURL! }]);
  await page.route("**/api/outcomes/pilot-feedback/status", async (route) => {
    expect(route.request().postDataJSON()).toEqual(link);
    expect((await route.request().allHeaders()).cookie).toBeUndefined();
    return route.fulfill({ json: snapshot });
  });
  await page.route("**/api/outcomes/pilot-feedback", (route) => {
    reports.push(route.request().postDataJSON());
    if (reports.length === 1) {
      snapshot = { ...snapshot, revision: 1, routing: "returned", updated_at: acceptedAt };
      return route.fulfill({ status: 409, json: { detail: "pilot_feedback_revision_changed" } });
    }
    snapshot = { ...snapshot, revision: 2, acknowledgment: "unknown", updated_at: acceptedAt };
    return route.fulfill({ json: snapshot });
  });
  await page.route("**/api/outcomes/opt-out", (route) => {
    expect(route.request().postDataJSON()).toEqual({ case_id: caseId, token: optOutToken });
    return route.fulfill({ json: { status: "opted_out" } });
  });
  const pageResponse = await page.goto(`/outcomes/pilot#case=${caseId}&token=${token}&opt_out_token=${optOutToken}`);
  expect(pageResponse?.headers()["referrer-policy"]).toBe("no-referrer");
  await expect(page).toHaveURL(/\/outcomes\/pilot$/);
  await page.getByLabel("Did the agency acknowledge the appeal?").selectOption("unknown");
  expect(reports).toEqual([]);
  await page.getByRole("button", { name: "Save answers" }).click();
  await expect(page.getByText("Your saved answers changed.", { exact: false })).toBeVisible();
  await expect(page.getByLabel("Did the mailing have a routing problem?")).toHaveValue("returned");
  await page.getByLabel("Did the agency acknowledge the appeal?").selectOption("unknown");
  await page.getByRole("button", { name: "Save answers" }).click();
  await expect(page.getByText("Thanks. Your answers were saved.")).toBeVisible();
  expect(reports).toEqual([
    { ...link, request_id: expect.any(String), expected_revision: 0, acknowledgment: "unknown" },
    { ...link, request_id: expect.any(String), expected_revision: 1, acknowledgment: "unknown" },
  ]);
  await page.getByRole("button", { name: "Stop follow-up reminders" }).click();
  await expect(page.getByText("Your follow-up reminders have been stopped.")).toBeVisible();
});
