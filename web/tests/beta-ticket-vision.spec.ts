import { expect, test, type Page, type Route } from "@playwright/test";
import { deflateSync } from "node:zlib";
import { LIVE_FIELDS, type IntakeForm, type TicketFactBindingPayload, type TicketReadingStatus, type WorkspaceView } from "../lib/api";
import { EMPTY_CONTACT } from "../components/LiveDetails";
import { emptyFactDraft, factDraftFromAdopted, factDraftFromValues, restoredTicketStep, ticketFormWithDraft, ticketValuesFromDraft, validateFactDraft } from "../lib/intake";

// Required ticket-reading mode only: real owner-scoped upload -> unverified reading ->
// explicit 13 facts -> manual review when extraction is empty -> explicit confirmation.
// No fixture stands in for the customer path and no required runtime is disabled here.
const base = process.env.SECURE_BASE;
const privateApi = process.env.INGRESS_API_BASE;
const worker = process.env.INGRESS_WORKER_SECRET;
test.use({ ignoreHTTPSErrors: true });
test.setTimeout(120_000);

const LITERAL_FIELDS = ["citation_number","license_plate","license_plate_state","issuer_name","issue_date","notice_date","printed_deadline","violation_text","issued_city","issued_state","stage_text"] as const;

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

async function rawCall(page: Page, method: string, path: string, body?: unknown): Promise<{ status: number; json: unknown }> {
  return page.evaluate(async ({ method, path, body }) => {
    const csrf = document.cookie.split("; ").find((part) => part.startsWith("__Host-pb-csrf="))?.split("=")[1] ?? "";
    const response = await fetch(`/api${path}`, {
      method,
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    let json: unknown = null;
    try { json = await response.json(); } catch { /* keep null */ }
    return { status: response.status, json };
  }, { method, path, body });
}

async function appeal(page: Page) {
  return call(page, "GET", "/auth/appeal") as Promise<Record<string, unknown> | null>;
}

function forward(route: Route) {
  const browser = route.request().frame().page().context().browser();
  if (browser?.browserType().name() === "firefox" && process.env.PLAYWRIGHT_FIREFOX_PROXY) {
    return route.fetch();
  }
  const transport = process.env.SECURE_TRANSPORT_BASE;
  return route.fetch(transport ? { url: new URL(new URL(route.request().url()).pathname, transport).href } : {});
}

const ISSUE_DATE = new Date(Date.now() - 3 * 86400000).toISOString().slice(0, 10);
const FACTS: Record<string, string> = {
  citation_number: "SYN-VISION-001",
  license_plate: "VSN123",
  license_plate_state: "CA",
  issuer_hint: "San Francisco Municipal Transportation Agency",
  issued_city: "San Francisco",
  issued_state: "CA",
  violation_code: "VC-101",
  violation_description: "Meter expired",
  issue_date: ISSUE_DATE,
  stage: "Initial review — printed",
};

async function beginVision(page: Page) {
  await page.goto(base!);
  await page.locator("#actSnap").click();
  await expect(page.locator("#ticketVision")).toBeVisible({ timeout: 20000 });
}

async function uploadAndRead(page: Page) {
  await page.locator("#visionFrontPhoto").setInputFiles({ name: "synthetic-front.png", mimeType: "image/png", buffer: syntheticPng(200) });
  await page.locator("#visionBackPhoto").setInputFiles({ name: "synthetic-back.png", mimeType: "image/png", buffer: syntheticPng(150) });
  await expect(page.locator("#visionFrontSaved")).toBeVisible();
  await expect(page.locator("#visionBackSaved")).toBeVisible();
  await page.locator("#visionReadBtn").click();
  await expect(page.locator("#visionSuggestions")).toBeVisible({ timeout: 20000 });
  await expect(page.locator("#visionSuggestions [data-vision-field]")).toHaveCount(LITERAL_FIELDS.length);
}

async function fillFacts(page: Page) {
  for (const [key, value] of Object.entries(FACTS)) await page.locator(`#fact-${key}`).fill(value);
  await page.locator("#fact-issuer_kind-government").check();
  await page.locator("#fact-notice_date-absent").check();
  await page.locator("#fact-printed_deadline-absent").check();
}

async function reviewFacts(page: Page) {
  await page.locator("#visionReviewBtn").click();
  await expect(page.locator("#visionReview")).toContainText("No AI comparison was performed.", { timeout: 20000 });
}

async function resolveEveryDifference(page: Page) {
  let resolved = 0;
  for (const field of LITERAL_FIELDS) {
    const select = page.locator(`#resolve-${field}`);
    if (!(await select.count())) continue;
    const values = await select.locator("option").evaluateAll((options) => options.map((option) => (option as HTMLOptionElement).value).filter(Boolean));
    expect(values).toHaveLength(1);
    await select.selectOption(values[0]);
    resolved += 1;
  }
  return resolved;
}

async function confirmFacts(page: Page) {
  await expect(page.locator("#visionConfirmBtn")).toBeDisabled();
  await expect(page.locator("#visionAccuracyAcknowledged")).not.toBeChecked();
  await page.locator("#visionAccuracyAcknowledged").check();
  await expect(page.locator("#visionConfirmBtn")).toBeEnabled();
  await page.locator("#visionConfirmBtn").click();
  await expect(page.locator("#visionConfirmed")).toBeVisible({ timeout: 20000 });
}

async function workspaceStatus(page: Page, workspaceId: string) {
  return call(page, "GET", `/workspaces/${workspaceId}/ticket-reading`) as Promise<Record<string, unknown>>;
}

test.describe("actual photo upload boundary", () => {
  test.skip(!base || !privateApi || !worker, "requires isolated HTTPS required-mode rehearsal");

  test("unsupported and corrupt files cannot start work; JPEG/PNG recovery succeeds", async ({ page }) => {
    const mutations: string[] = [];
    const uploads: string[] = [];
    page.on("request", request => {
      if (request.method() !== "POST") return;
      const path = new URL(request.url()).pathname;
      mutations.push(path);
      if (path.endsWith("/photos")) uploads.push(request.postDataJSON().content_type);
    });
    await beginVision(page);
    await expect(page.locator("#ticketPhotoFormats")).toContainText("PDFs, HEIC");
    const input = page.locator("#visionFrontPhoto");
    await expect(input).toBeEnabled();
    const error = page.locator("#ticketVision [role=alert]");
    for (const file of [
      { name: "ticket.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.4 synthetic") },
      { name: "ticket.heic", mimeType: "image/heic", buffer: Buffer.from("synthetic HEIC") },
      { name: "ticket.svg", mimeType: "image/svg+xml", buffer: Buffer.from("<svg/>") },
      { name: "ticket.gif", mimeType: "image/gif", buffer: Buffer.from("GIF89a") },
      { name: "empty.png", mimeType: "image/png", buffer: Buffer.alloc(0) },
      { name: "large.jpg", mimeType: "image/jpeg", buffer: Buffer.alloc(10_000_001) },
    ]) {
      await input.setInputFiles(file);
      await expect(error).toContainText("was not uploaded");
      await expect(page.locator("#visionReadBtn")).toBeDisabled();
      await expect(page.locator("#visionFrontSaved")).toHaveCount(0);
    }
    expect(uploads).toEqual([]);
    for (const file of [
      { name: "corrupt.png", mimeType: "image/png", buffer: Buffer.from("\x89PNG\r\n\x1a\ncorrupt", "binary") },
      { name: "disguised.jpg", mimeType: "image/jpeg", buffer: Buffer.from("%PDF-1.4 synthetic") },
    ]) {
      const rejected = page.waitForResponse(response => new URL(response.url()).pathname.endsWith("/photos") && response.request().method() === "POST");
      await input.setInputFiles(file);
      expect((await rejected).status()).toBe(409);
      await expect(error).toContainText("was not saved");
      await expect(page.locator("#visionReadBtn")).toBeDisabled();
    }
    const jpeg = await page.evaluate(() => {
      const canvas = document.createElement("canvas"); canvas.width = 160; canvas.height = 80;
      const context = canvas.getContext("2d")!;
      context.fillStyle = "white"; context.fillRect(0, 0, 160, 80);
      context.fillStyle = "black"; context.fillText("SYNTHETIC TICKET", 5, 30);
      return canvas.toDataURL("image/jpeg").split(",")[1];
    });
    await input.setInputFiles({ name: "valid.jpg", mimeType: "image/jpeg", buffer: Buffer.from(jpeg, "base64") });
    await expect(page.locator("#visionFrontSaved")).toBeVisible();
    await page.locator("#visionBackPhoto").setInputFiles({ name: "valid.png", mimeType: "image/png", buffer: syntheticPng(120) });
    await expect(page.locator("#visionBackSaved")).toBeVisible();
    await expect(error).toHaveCount(0);
    await expect(page.locator("#visionReadBtn")).toBeEnabled();
    expect(uploads).toEqual(["image/png", "image/jpeg", "image/jpeg", "image/png"]);
    expect(mutations.filter(path => /\/(ticket-reading|ticket-review|intake|authorization|beta-acceptance|submit)$/.test(path))).toEqual([]);
  });
});

test.describe("ticket draft persistence units", () => {
  const legacyValues = {
    ...Object.fromEntries(LIVE_FIELDS.map((field) => [`field.${field.k}`, `saved ${field.k}`])),
    ...Object.fromEntries(Object.keys(EMPTY_CONTACT).map((key) => [`contact.${key}`, `saved ${key}`])),
    plate: "VSN123", plateState: "CA", story: "Exact customer story.", owner: "yes", photo: "false", backPhoto: "true",
  };
  const draft = { ...emptyFactDraft(), ...FACTS, issuer_kind: "GOVERNMENT" as const, notice_date: ISSUE_DATE, printed_deadline_absent: true };
  const binding: TicketFactBindingPayload = {
    literal: Object.fromEntries(LITERAL_FIELDS.map((field) => [field, null])),
    adopted: { ...validateFactDraft(draft).facts! },
  };

  test("autosave and flush stay within 30 values while preserving every draft and binding value", () => {
    const form = ticketFormWithDraft({ values: legacyValues, step: 1 }, draft, null);
    expect(Object.keys(form.values!)).toHaveLength(Object.keys(legacyValues).length + 1);
    expect(Object.keys(form.values!).length).toBeLessThanOrEqual(30);
    expect(form.values).toMatchObject(legacyValues);
    expect(factDraftFromValues(JSON.parse(JSON.stringify(form)).values)).toEqual(draft);

    const confirmed = ticketFormWithDraft(form, draft, binding);
    expect(confirmed.values).toEqual(legacyValues);
    expect(confirmed.ticket_fact_binding).toEqual(binding);
    const storyEdit = ticketFormWithDraft({ ...confirmed, step: 2, values: { ...confirmed.values, story: "Updated story." } }, draft, binding);
    const reloaded: IntakeForm = JSON.parse(JSON.stringify(storyEdit));
    expect(Object.keys(reloaded.values!).length).toBeLessThanOrEqual(30);
    expect(reloaded.values).toEqual({ ...legacyValues, story: "Updated story." });
    expect(reloaded.ticket_fact_binding).toEqual(binding);
    expect(factDraftFromAdopted(reloaded.ticket_fact_binding!.adopted)).toEqual(draft);

    // Editing one fact replaces the envelope with the exact draft; unrelated data survives.
    const edited = { ...draft, stage: "Second review — printed" };
    const invalidated = ticketFormWithDraft(reloaded, edited, null);
    expect(invalidated.ticket_fact_binding).toBeUndefined();
    expect(factDraftFromValues(invalidated.values!)).toEqual(edited);
    expect(invalidated.values).toMatchObject(reloaded.values!);
    expect(Object.keys(invalidated.values!).length).toBeLessThanOrEqual(30);
  });

  for (const field of ["notice_date", "printed_deadline"] as const) {
    test(`${field} stays unconfirmed after reload until explicitly chosen`, () => {
      for (const values of [{}, { [`ticket.${field}`]: "" }, { [`ticket.${field}`]: "", [`ticket.${field}_absent`]: "false" }]) {
        const restored = factDraftFromValues(values);
        expect(restored[field]).toBe("");
        expect(restored[`${field}_absent`]).toBe(false);
        expect(validateFactDraft(restored).errors).toHaveProperty(`fact-${field}`);
      }
      const unconfirmed = { ...draft, [field]: "", [`${field}_absent`]: false };
      expect(factDraftFromValues(ticketValuesFromDraft(unconfirmed))).toEqual(unconfirmed);
      const absent = { ...unconfirmed, [`${field}_absent`]: true };
      expect(factDraftFromValues(ticketValuesFromDraft(absent))).toEqual(absent);
      expect(factDraftFromValues({ [`ticket.${field}_absent`]: "true" })[`${field}_absent`]).toBe(true);
      const dated = { ...unconfirmed, [field]: ISSUE_DATE };
      expect(factDraftFromValues(ticketValuesFromDraft(dated))).toEqual(dated);
      expect(factDraftFromAdopted({})[`${field}_absent`]).toBe(false);
      expect(factDraftFromAdopted({ [field]: "" })[`${field}_absent`]).toBe(false);
      expect(factDraftFromAdopted({ [field]: null })[`${field}_absent`]).toBe(true);
    });
  }

  test("required saved Step 2 needs fresh current confirmation proof", () => {
    const workspace: WorkspaceView = { workspace_id: "ws_synthetic", notes: "", draft_revision: 1, ticket_reading_required: true, work_preserved: true, intake_form: { values: legacyValues, step: 2 } };
    const status: TicketReadingStatus = {
      enabled: true, required: true, synthetic: true, next_confirmation_generation: 1,
      extraction: { status: "success", current: true, synthetic: true, fields: [] },
      review: { status: "success", current: true, synthetic: true, fields: [], basis: "model comparison only; not external verification" },
      confirmation: { state: "current", command_id: "synthetic-confirmation", generation: 1 },
    };
    expect(restoredTicketStep(workspace, null)).toBe(1);
    expect(restoredTicketStep(workspace)).toBe(1);
    for (const state of ["missing", "stale"] as const) {
      expect(restoredTicketStep(workspace, { ...status, confirmation: { ...status.confirmation, state } })).toBe(1);
    }
    expect(restoredTicketStep(workspace, { ...status, confirmation: { state: "current", generation: 1 } })).toBe(1);
    expect(restoredTicketStep(workspace, status)).toBe(2);
    expect(restoredTicketStep({ ...workspace, ticket_reading_required: false }, null)).toBe(2);
    expect(restoredTicketStep({ ...workspace, ticket_reading_required: false }, { ...status, confirmation: { state: "missing", generation: 0 } })).toBe(1);
  });
});

test.describe("required ticket vision browser", () => {
test.skip(!base || !privateApi || !worker, "requires isolated HTTPS required-mode rehearsal");
let savedForms: IntakeForm[];
let saveStatuses: number[];
test.beforeEach(async ({ page }) => {
  savedForms = [];
  saveStatuses = [];
  page.on("request", (request) => {
    if (request.method() === "PUT" && new URL(request.url()).pathname.endsWith("/intake-form")) savedForms.push(request.postDataJSON().form);
  });
  page.on("response", (response) => {
    if (response.request().method() === "PUT" && new URL(response.url()).pathname.endsWith("/intake-form")) saveStatuses.push(response.status());
  });
});
test.afterEach(() => {
  expect(savedForms.length, "actual autosave/flush requests observed").toBeGreaterThan(0);
  for (const form of savedForms) expect(Object.keys(form.values ?? {}).length).toBeLessThanOrEqual(30);
  expect(saveStatuses.length, "actual save acknowledgments observed").toBeGreaterThan(0);
  expect(saveStatuses.every((status) => status === 200), "autosave/flush must be accepted").toBe(true);
});

test("empty reading, explicit manual facts and confirmation save, restore and authorize intake", async ({ page }) => {
  await beginVision(page);
  await uploadAndRead(page);
  await expect(page.locator("#visionReadings")).toContainText(/unverified|suggestion/i);

  // The printed date text stays a separate literal from the customer's ISO date.
  const printedIssue = (await page.locator('[data-vision-field="issue_date"]').textContent()) ?? "";
  await fillFacts(page);
  await expect(page.locator("#fact-issue_date")).toHaveAttribute("type", "date");
  await expect(page.locator("#fact-issue_date")).toHaveValue(ISSUE_DATE);
  expect(printedIssue).not.toContain(ISSUE_DATE);

  await reviewFacts(page);
  const resolved = await resolveEveryDifference(page);
  expect(resolved).toBe(0);
  await expect(page.locator("#actContinueTicket")).toHaveCount(0);
  await page.locator("#visionAccuracyAcknowledged").check();
  await expect(page.locator("#visionConfirmBtn")).toBeEnabled();
  await page.locator("#fact-license_plate").fill("CORRECTED123");
  await expect(page.locator("#visionConfirmBtn")).toHaveCount(0);
  await page.locator("#fact-license_plate").fill(FACTS.license_plate);
  // Even restoring the old value must not restore consent.
  await reviewFacts(page);
  await resolveEveryDifference(page);
  await expect(page.locator("#visionAccuracyAcknowledged")).not.toBeChecked();
  await confirmFacts(page);
  await expect(page.locator("#actContinueTicket")).toBeVisible();

  const saved = await appeal(page);
  const workspaceId = String(saved?.workspace_id ?? "");
  const form = saved?.intake_form as IntakeForm | undefined;
  const binding = form?.ticket_fact_binding;
  expect(binding, "the confirmed envelope is saved in the existing private form").toBeTruthy();
  expect(Object.keys(form!.values ?? {}).filter((key) => key === "ticket" || key.startsWith("ticket."))).toEqual([]);
  expect(binding!.adopted).toEqual({
    citation_number: FACTS.citation_number,
    license_plate: FACTS.license_plate,
    license_plate_state: FACTS.license_plate_state,
    issuer_hint: FACTS.issuer_hint,
    issued_city: FACTS.issued_city,
    issued_state: FACTS.issued_state,
    issuer_kind: "GOVERNMENT",
    violation_code: FACTS.violation_code,
    violation_description: FACTS.violation_description,
    issue_date: ISSUE_DATE,
    stage: FACTS.stage,
    notice_date: null,
    printed_deadline: null,
  });
  for (const field of LITERAL_FIELDS) expect(Object.keys(binding!.literal)).toContain(field);
  expect(binding!.literal.issue_date === null || binding!.literal.issue_date !== ISSUE_DATE).toBe(true);
  expect(binding!.literal.stage_text === null || binding!.literal.stage_text !== FACTS.stage).toBe(true);

  const status = await workspaceStatus(page, workspaceId);
  expect(status.required).toBe(true);
  const confirmation = status.confirmation as { state: string; command_id: string; generation: number };
  expect(confirmation.state).toBe("current");
  expect(confirmation.generation).toBe(1);
  expect(String(confirmation.command_id)).toMatch(/^[A-Za-z0-9]/);

  // Refresh restores the saved confirmation and command id from GET, not local state.
  await page.reload();
  await expect(page.locator("#ticketVision")).toBeVisible({ timeout: 20000 });
  await expect(page.locator("#visionConfirmed")).toBeVisible();
  await expect(page.locator("#fact-stage")).toHaveValue(FACTS.stage);
  await page.locator("#actContinueTicket").click();
  await expect(page.locator("#story")).toBeVisible();

  await page.locator("#story").fill("The sign was covered. The kiosk was blank.");
  await page.locator("#actSendStory").click();
  await page.getByRole("button", { name: /^Yes/ }).first().click();
  await expect(page.locator("#liveDetails")).toBeVisible();
  for (const [id, value] of Object.entries({ liveName: "Synthetic Customer", livePhone: "415-555-0100", liveEmail: "ticket-vision@example.test", liveLine1: "123 Example Street", liveCity: "San Francisco", liveState: "CA", liveZip: "94103" })) await page.locator(`#${id}`).fill(value);
  await page.locator("#liveRentalPaid").selectOption("not-rental");
  await expect(page.locator("#liveTicketPhoto")).toHaveCount(0);
  await page.locator("#liveSubmit").click();
  let caseId = "";
  await expect.poll(async () => { caseId = String((await appeal(page))?.case_id ?? ""); return caseId; }, { timeout: 30000 }).toMatch(/^case_/);
  const submitted = await appeal(page);
  const intake = submitted?.intake as { ticket_confirmation_command_id?: string; ticket?: Record<string, unknown> } | undefined;
  expect(intake?.ticket_confirmation_command_id).toBe(confirmation.command_id);
  expect(intake?.ticket).toMatchObject({
    citation_number: FACTS.citation_number,
    violation_code: FACTS.violation_code,
    violation_description: FACTS.violation_description,
    issue_date: ISSUE_DATE,
    stage: FACTS.stage,
    notice_date: null,
    printed_deadline: null,
  });
});

test("missing confirmation cannot advance and the required intake is refused", async ({ page }) => {
  await beginVision(page);
  await uploadAndRead(page);
  await fillFacts(page);
  await reviewFacts(page);
  await resolveEveryDifference(page);
  await page.locator("#visionAccuracyAcknowledged").check();
  await expect(page.locator("#visionConfirmBtn")).toBeEnabled();
  await expect(page.locator("#visionConfirmed")).toHaveCount(0);
  await expect(page.locator("#actContinueTicket")).toHaveCount(0);

  const saved = await appeal(page);
  const workspaceId = String(saved?.workspace_id ?? "");
  const form = saved?.intake_form as { ticket_photo_ref?: string; ticket_back_photo_ref?: string; ticket_fact_binding?: { adopted: Record<string, string | null> } } | undefined;
  const binding = form?.ticket_fact_binding;
  expect(binding).toBeTruthy();
  const refused = await rawCall(page, "POST", `/workspaces/${workspaceId}/intake`, {
    ticket: binding!.adopted,
    rental_company_paid: "NOT_RENTAL",
    customer_position: { statement: "The sign was covered." },
    contact: { name: "Synthetic Customer", phone: "415-555-0100", email: "ticket-vision@example.test", mailing_address: { line1: "123 Example Street", line2: "", city: "San Francisco", state: "CA", zip_code: "94103" } },
    citation_photo_ref: form?.ticket_photo_ref,
    citation_back_photo_ref: form?.ticket_back_photo_ref ?? null,
    photo_refs: [],
  });
  expect(refused.status).toBe(409);
  expect(JSON.stringify(refused.json)).toContain("ticket_confirmation_required");
});

test("fact edits invalidate comparison/confirmation and a lost confirmation response replays the same command", async ({ page }) => {
  await beginVision(page);
  await uploadAndRead(page);
  await fillFacts(page);
  await reviewFacts(page);
  await resolveEveryDifference(page);
  await confirmFacts(page);
  const saved = await appeal(page);
  const workspaceId = String(saved?.workspace_id ?? "");
  const status = await workspaceStatus(page, workspaceId);
  const commandId = String((status.confirmation as { command_id: string }).command_id);
  const reviewRequestId = String((status.confirmation as { review_request_id: string }).review_request_id);

  const reviewBodies: string[] = [];
  const confirmationBodies: string[] = [];
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (path.endsWith("/ticket-review")) reviewBodies.push(request.postData() ?? "");
    if (path.endsWith("/ticket-confirmation")) confirmationBodies.push(request.postData() ?? "");
  });

  // A fact edit immediately withdraws the current confirmation; the next compare must
  // be a new explicit request bound to the changed envelope.
  await page.locator("#fact-stage").fill("Second review — printed");
  await expect(page.locator("#visionConfirmed")).toHaveCount(0);
  await expect(page.locator("#actContinueTicket")).toHaveCount(0);
  await expect(page.locator("#visionDirty")).toBeVisible();
  await page.locator("#visionReviewBtn").click();
  await expect(page.locator("#visionReview")).toContainText("No AI comparison was performed.", { timeout: 20000 });
  const firstReview = JSON.parse(reviewBodies[reviewBodies.length - 1]) as { request_id: string };
  expect(firstReview.request_id).not.toBe(reviewRequestId);
  await resolveEveryDifference(page);

  await page.locator("#visionAccuracyAcknowledged").check();
  let lost = false;
  await page.route("**/api/workspaces/*/ticket-confirmation", async (route) => {
    const response = await forward(route);
    expect(response.status()).toBe(200);
    if (!lost) { lost = true; await route.abort("connectionreset"); }
    else await route.fulfill({ response });
  });
  await page.locator("#visionConfirmBtn").click();
  await expect(page.locator("#visionConfirmed")).toHaveCount(0);
  await expect(page.locator("#ticketVision")).toContainText(/couldn.t reach|connection|unavailable/i);
  await page.locator("#visionConfirmBtn").click();
  await expect(page.locator("#visionConfirmed")).toBeVisible({ timeout: 20000 });
  expect(confirmationBodies.length).toBe(2);
  const first = JSON.parse(confirmationBodies[0]) as { command_id: string; review_request_id: string; expected_confirmation_generation: number };
  const second = JSON.parse(confirmationBodies[1]) as { command_id: string; review_request_id: string; expected_confirmation_generation: number };
  expect(first.command_id).toBe(second.command_id);
  expect(second.command_id).not.toBe(commandId);
  expect(second.review_request_id).toBe(first.review_request_id);
  await page.unroute("**/api/workspaces/*/ticket-confirmation");
});

test("a lost reading response replays the exact request id and never starts a second reading", async ({ page }) => {
  await beginVision(page);
  await page.locator("#visionFrontPhoto").setInputFiles({ name: "synthetic-front.png", mimeType: "image/png", buffer: syntheticPng(200) });
  await expect(page.locator("#visionFrontSaved")).toBeVisible();
  const bodies: string[] = [];
  page.on("request", (request) => { if (new URL(request.url()).pathname.endsWith("/ticket-reading") && request.method() === "POST") bodies.push(request.postData() ?? ""); });
  let lost = false;
  await page.route("**/api/workspaces/*/ticket-reading", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    const response = await forward(route);
    expect(response.status()).toBe(200);
    if (!lost) { lost = true; await route.abort("connectionreset"); }
    else await route.fulfill({ response });
  });
  await page.locator("#visionReadBtn").click();
  await expect(page.locator("#ticketVision")).toContainText(/couldn.t reach|could not|unavailable/i, { timeout: 20000 });
  const replay = page.waitForResponse(response => new URL(response.url()).pathname.endsWith("/ticket-reading") && response.request().method() === "POST");
  await page.locator("#visionReadBtn").click();
  expect((await replay).status()).toBe(200);
  await expect(page.locator("#visionSuggestions")).toBeVisible({ timeout: 20000 });
  expect(bodies.length).toBe(2);
  const first = JSON.parse(bodies[0]) as { request_id: string };
  const second = JSON.parse(bodies[1]) as { request_id: string };
  expect(second.request_id).toBe(first.request_id);
  await page.unroute("**/api/workspaces/*/ticket-reading");
});

test("optional dates stay unconfirmed through draft autosave and reload", async ({ page }) => {
  await beginVision(page);
  await uploadAndRead(page);
  for (const [key, value] of Object.entries(FACTS)) await page.locator(`#fact-${key}`).fill(value);
  await page.locator("#fact-issuer_kind-government").check();
  await expect.poll(async () => {
    const saved = await appeal(page);
    return factDraftFromValues((saved?.intake_form as IntakeForm)?.values ?? {});
  }).toEqual({ ...emptyFactDraft(), ...FACTS, issuer_kind: "GOVERNMENT" });

  await page.reload();
  for (const field of ["notice_date", "printed_deadline"]) {
    await expect(page.locator(`#fact-${field}`)).toHaveValue("");
    await expect(page.locator(`#fact-${field}-absent`)).not.toBeChecked();
  }
  await expect(page.locator("#fact-stage")).toHaveValue(FACTS.stage);
  await page.locator("#visionReviewBtn").click();
  await expect(page.locator("#fact-notice_date")).toHaveAttribute("aria-invalid", "true");
  await expect(page.locator("#fact-printed_deadline")).toHaveAttribute("aria-invalid", "true");
  await expect(page.locator("#visionReviewList")).toHaveCount(0);
  await expect(page.locator("#actContinueTicket")).toHaveCount(0);

  // Explicit absence and an entered calendar date each survive a later draft reload.
  await page.locator("#fact-notice_date-absent").check();
  await page.locator("#fact-printed_deadline").fill(ISSUE_DATE);
  await expect.poll(async () => factDraftFromValues(((await appeal(page))?.intake_form as IntakeForm)?.values ?? {})).toEqual({
    ...emptyFactDraft(), ...FACTS, issuer_kind: "GOVERNMENT", notice_date_absent: true, printed_deadline: ISSUE_DATE,
  });
  await page.reload();
  await expect(page.locator("#fact-notice_date-absent")).toBeChecked();
  await expect(page.locator("#fact-printed_deadline-absent")).not.toBeChecked();
  await expect(page.locator("#fact-printed_deadline")).toHaveValue(ISSUE_DATE);
  await expect(page.locator("#visionReviewBtn")).toBeEnabled();
});

test("required saved Step 2 stays at ticket confirmation when status discovery fails", async ({ page }) => {
  await beginVision(page);
  await uploadAndRead(page);
  await fillFacts(page);
  await reviewFacts(page);
  await resolveEveryDifference(page);
  await confirmFacts(page);
  await page.locator("#actContinueTicket").click();
  const story = "Saved customer story before the status outage.";
  await page.locator("#story").fill(story);
  await expect.poll(async () => ((await appeal(page))?.intake_form as IntakeForm)?.values?.story).toBe(story);
  const saved = await appeal(page);
  const form = saved?.intake_form as IntakeForm;
  expect(saved?.ticket_reading_required).toBe(true);
  expect(form.step).toBe(2);
  expect(form.ticket_fact_binding).toBeTruthy();

  const processingCalls: string[] = [];
  page.on("request", (request) => {
    if (request.method() === "POST" && /\/ticket-(reading|review|confirmation)$/.test(new URL(request.url()).pathname)) processingCalls.push(request.url());
  });
  await page.route("**/api/workspaces/*/ticket-reading", (route) => route.request().method() === "GET" ? route.abort("connectionreset") : route.fallback());
  await page.reload();
  await expect(page.locator("#ticketVision")).toBeVisible();
  await expect(page.locator("#visionRetryCheck")).toBeVisible();
  await expect(page.locator("#story")).toHaveCount(0);
  await expect(page.locator("#liveSubmit")).toHaveCount(0);
  await expect(page.locator("#actContinueTicket")).toHaveCount(0);
  await expect.poll(async () => ((await appeal(page))?.intake_form as IntakeForm)?.step).toBe(1);
  const gated = (await appeal(page))?.intake_form as IntakeForm;
  expect(gated.values?.story).toBe(story);
  expect(gated.ticket_fact_binding).toEqual(form.ticket_fact_binding);
  expect(processingCalls).toEqual([]);

  // Recover only through fresh GET proof; no extraction/review/confirmation is repeated.
  await page.unroute("**/api/workspaces/*/ticket-reading");
  await page.locator("#visionRetryCheck").click();
  await expect(page.locator("#visionConfirmed")).toBeVisible();
  await page.locator("#actContinueTicket").click();
  await expect(page.locator("#story")).toHaveValue(story);
  expect(processingCalls).toEqual([]);
});

test("short pipeline notice opens full rules without submitting or losing entered facts", async ({ page }) => {
  await beginVision(page);
  await uploadAndRead(page);
  await fillFacts(page);
  await reviewFacts(page);

  const notice = page.locator("#visionReview [data-customer-choice-notice]");
  await expect(notice).toBeVisible();
  await expect(notice).toContainText("Your chosen details aren’t covered by our correct-office refund guarantee if they cause the rejection.");
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(1);

  const mutationsDuringRules: string[] = [];
  const track = (request: { method: () => string; url: () => string }) => {
    if (request.method() === "POST" || request.method() === "PUT") {
      mutationsDuringRules.push(`${request.method()} ${new URL(request.url()).pathname}`);
    }
  };
  page.on("request", track);
  await notice.locator(".notice-rules-btn").click();
  const termsDlg = page.locator("#termsDlg");
  await expect(termsDlg).toBeVisible();
  await expect(termsDlg.locator('[data-policy-version="2026-11-16"]')).toContainText("Customer choices and our correct-office guarantee");
  await expect(termsDlg).toContainText("If a destination we verified proves wrong, you receive a full refund.");
  await termsDlg.locator('button[aria-label="Close"]').click();
  await expect(termsDlg).not.toBeVisible();
  page.off("request", track);

  expect(mutationsDuringRules).toEqual([]);
  await expect(page.locator("#fact-citation_number")).toHaveValue(FACTS.citation_number);
  await expect(page.locator("#fact-stage")).toHaveValue(FACTS.stage);
  await expect(page.locator("#fact-issuer_kind-government")).toBeChecked();
  await confirmFacts(page);
});
});
