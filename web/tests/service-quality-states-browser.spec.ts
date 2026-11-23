import { expect, test, type Page } from "./synthetic-fixtures";
import { createHmac } from "node:crypto";
import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { DatabaseSync } from "node:sqlite";
import { join, resolve } from "node:path";
import { apiBase, appeal, base, call, completeDetails, file, inspect, layout, openDetails, output, seedDraft, shot, workerSecret } from "./t37-support";

// Additional customer states against the assembled durable fake-provider backend.
// Real durable paths: stop, returned mail (provider-signed synthetic event), keyboard-only and
// touch-only letter authorization, resume. Labelled injection: states the fake backend cannot
// produce (delivery proof, a customer question, an expired review window, a full outage).
const webhookSecret = process.env.T37_LOB_WEBHOOK_SECRET;
const dataDir = process.env.T37_DATA_DIR;
test.use({ ignoreHTTPSErrors: true, contextOptions: { reducedMotion: "reduce" } });
test.skip(!base || !apiBase || !workerSecret, "NEEDS_EXTERNAL: isolated HTTPS app, durable fake backend and private fake-worker configuration");

const note = (name: string, facts: Record<string, unknown>) => {
  mkdirSync(output, { recursive: true });
  writeFileSync(resolve(output, `${name}.json`), JSON.stringify({ synthetic: true, backendMode: "fake", ...facts }, null, 2));
};

/** A signed Lob webhook for the exact letter this case's durable cursor accepted. */
async function signedLobEvent(page: Page, caseId: string, eventType: string, trackingEvent: Record<string, unknown>) {
  const database = new DatabaseSync(join(dataDir!, "parkingbreaker.sqlite"), { readOnly: true } as never);
  try {
    const row = database.prepare(
      "select s.outcome_json as outcome from pb_submission_cursors s join pb_intake_records i on i.case_token = s.case_token where i.intake_id = ? and s.attempt = 1",
    ).get(caseId) as { outcome: string } | undefined;
    const letterId = JSON.parse(row!.outcome).letter_id as string;
    let clockOffset = 0;
    if (process.env.SYNTHETIC_REHEARSAL === "1") {
      try {
        const state = JSON.parse(readFileSync(join(dataDir!, "..", "clock-state.json"), "utf8")) as { total?: unknown };
        if (!Number.isSafeInteger(state.total) || (state.total as number) < 0) {
          throw new Error("Invalid synthetic clock offset");
        }
        clockOffset = state.total as number;
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
      }
    }
    const now = new Date(Date.now() + clockOffset * 1000);
    const payload = JSON.stringify({
      id: `evt_t37_${eventType}_${Date.now()}`,
      event_type: { id: eventType },
      date_created: now.toISOString(),
      body: { id: letterId, tracking_events: [trackingEvent] },
    });
    const stamp = String(Math.floor(now.getTime() / 1000));
    const signature = createHmac("sha256", webhookSecret!).update(`${stamp}.${payload}`).digest("hex");
    const response = await page.request.post(`${apiBase}/webhooks/lob`, {
      data: payload,
      headers: { "content-type": "application/json", "lob-signature": signature, "lob-signature-timestamp": stamp },
    });
    expect(response.status(), await response.text()).toBe(200);
  } finally { database.close(); }
}

async function mailedCase(page: Page, name: string, mode: "keyboard" | "pointer" = "pointer") {
  const id = await seedDraft(page);
  if (mode === "pointer") await inspect(page, id, name);
  else {
    // Keyboard only: every control is reached with focus and operated with Enter or Space.
    await page.locator("#liveAdopt").focus(); await page.keyboard.press("Enter");
    await expect(page.locator("#livePreview")).toBeEnabled();
    await page.locator("#liveSignatureFile").setInputFiles(file("own-signature.png"));
    await page.locator("#livePreview").focus(); await page.keyboard.press("Enter");
    await expect(page.locator("#liveOpenPacket")).toHaveAttribute("href", /^blob:/);
    await expect(page.locator("#livePacketPage")).toBeVisible({ timeout: 15000 });
    await page.locator("#livePacketReviewed").focus(); await page.keyboard.press("Space");
    await expect(page.locator("#liveApprove")).toBeEnabled();
    await page.locator("#liveApprove").focus(); await page.keyboard.press("Enter");
    await expect(page.locator("#liveTracking")).toBeVisible({ timeout: 30000 });
    return id;
  }
  await page.locator("#liveApprove").click();
  await expect(page.locator("#liveTracking")).toBeVisible({ timeout: 30000 });
  return id;
}

for (const width of [375, 768, 1440]) {
  test(`T37 ${width}px stop path: a private lot leaves work saved and uncharged, including after reload`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.goto(base!);
    await page.locator("#actSnap").click();
    for (const value of ["SYN-STOP", new Date(Date.now() - 3 * 86400000).toLocaleDateString("en-US"), "Meter expired", "Example Private Parking"]) {
      await page.locator("#fixIn").fill(value); await page.locator("#actSaveFix").click();
    }
    await page.locator("#plateIn").fill("SYN123"); await page.locator("#plateState").fill("CA");
    await page.locator("#actContinueTicket").click();
    await page.locator("#story").fill("The sign was covered. The kiosk was blank.");
    await page.locator("#actSendStory").click();
    await page.getByRole("button", { name: /^Yes/ }).click();
    for (const [id, value] of Object.entries({ liveIssuedCity: "San Francisco", liveIssuedState: "CA", liveName: "Synthetic Customer", livePhone: "415-555-0100", liveEmail: "t37-stop@example.test", liveLine1: "123 Example Street", liveCity: "San Francisco", liveState: "CA", liveZip: "94103" })) await page.locator(`#${id}`).fill(value);
    await page.getByLabel("A private parking lot or company").check();
    await page.locator("#liveTicketPhoto").setInputFiles(file("ticket.png"));
    await expect.poll(async () => (await page.evaluate(async () => (await fetch("/api/auth/appeal")).ok))).toBe(true);
    await page.locator("#liveRentalPaid").selectOption("not-rental");
    await page.locator("#liveSubmit").click();
    await expect(page.locator("#liveStopMessage")).toContainText("Nothing was charged", { timeout: 20000 });
    await expect(page.locator("#s8")).toContainText("Your saved words");
    await expect(page.locator("#s8 textarea")).toHaveValue("The sign was covered. The kiosk was blank.");
    await layout(page); await shot(page, `${width}-stop`);
    await page.reload();
    await expect(page.locator("#liveStopMessage")).toContainText("Nothing was charged");
    await expect(page.locator("#s8")).toContainText("Your saved words");
    await shot(page, `${width}-stop-reload`);
    const appealState = await page.evaluate(async () => (await (await fetch("/api/auth/appeal")).json()));
    expect(appealState.case_id).toBeTruthy();
    const status = await call(page, "GET", `/cases/${appealState.case_id}/status`);
    expect(status.state).toBe("STOPPED_NO_CHARGE");
    note(`${width}-stop`, { state: status.state, payment: status.payment_state, reloadedToStop: true, viewport: width });
  });

  test(`T37 ${width}px returned mail: provider-signed return, pending remedy, reload`, async ({ page }) => {
    test.skip(!webhookSecret || !dataDir, "NEEDS_EXTERNAL: synthetic Lob webhook secret and durable data directory");
    test.setTimeout(120000);
    await page.setViewportSize({ width, height: 900 });
    const id = await mailedCase(page, `${width}-returned-setup`);
    await signedLobEvent(page, id, "letter.returned_to_sender", { type: "certified", name: "Returned to Sender", details: { event: "no_such_number", description: "Synthetic return reason for evidence.", action_required: false } });
    const sweep = await page.request.post(`${apiBase}/internal/dispatch`, { headers: { Authorization: `Bearer ${workerSecret}` } });
    expect(sweep.status()).toBe(200);
    await page.reload();
    await expect(page.locator("#h-s7")).toHaveText("Mail returned.");
    await expect(page.locator("#liveReturnRemedy")).toBeVisible();
    await expect(page.locator("#s7")).not.toContainText("USPS reports delivery");
    await expect(page.locator("#s7")).not.toContainText("Refund completed");
    await layout(page); await shot(page, `${width}-returned`);
    const status = await call(page, "GET", `/cases/${id}/status`);
    expect(["RETURNED", "CLOSED_RETURNED"]).toContain(status.state);
    note(`${width}-returned`, { state: status.state, payment: status.payment_state, remedy: status.return_remedy?.kind ?? null, refundStatus: status.return_remedy?.refund_status ?? null, providerSignedSyntheticEvent: true, viewport: width });
  });
}


/** Mean RGB of one rendered packet page (binary PPM from pdftoppm). */
function pageColors(pdf: Buffer): Array<[number, number, number]> | null {
  const directory = mkdtempSync(join(tmpdir(), "pb-t37-pdf-"));
  writeFileSync(join(directory, "packet.pdf"), pdf);
  const run = spawnSync("pdftoppm", ["-r", "20", join(directory, "packet.pdf"), join(directory, "page")], { encoding: "utf8" });
  if (run.error || run.status !== 0) return null;
  const colors: Array<[number, number, number]> = [];
  for (let number = 1; ; number++) {
    let data: Buffer;
    try { data = readFileSync(join(directory, `page-${number}.ppm`)); } catch {
      try { data = readFileSync(join(directory, `page-0${number}.ppm`)); } catch { break; }
    }
    const header = data.subarray(0, 20).toString("latin1").match(/^P6\s+(\d+)\s+(\d+)\s+255\s/)!;
    const pixels = data.subarray(header[0].length);
    let red = 0, green = 0, blue = 0, count = 0;
    // Only strongly tinted pixels count: photo pages are tinted; letter/ticket pages are white or dark ink.
    for (let index = 0; index + 2 < pixels.length; index += 3) {
      const [r, g, b] = [pixels[index], pixels[index + 1], pixels[index + 2]];
      if (Math.max(r, g, b) - Math.min(r, g, b) > 8 && Math.min(r, g, b) > 150) { red += r; green += g; blue += b; count++; }
    }
    colors.push(count > 200 ? [red / count, green / count, blue / count] : [0, 0, 0]);
  }
  return colors;
}

for (const width of [375, 1440]) {
  test(`T37 ${width}px optional photos: reorder and removal reach the exact packet in order`, async ({ page }) => {
    test.setTimeout(120000);
    await page.setViewportSize({ width, height: 900 });
    await openDetails(page);
    await completeDetails(page);
    await page.locator("#liveEvidencePhoto").setInputFiles([file("one.png"), file("two.png"), file("three.png")]);
    await expect.poll(async () => (await appeal(page))?.intake_form?.evidence_photo_refs?.length).toBe(3);
    // Order one, two, three -> move three up -> one, three, two -> remove one -> three, two.
    await page.getByRole("button", { name: "Move supporting photo 3 up", exact: true }).focus();
    await page.keyboard.press("Enter");
    await page.getByRole("button", { name: "Remove supporting photo 1", exact: true }).click();
    await expect(page.locator("#liveEvidenceList [data-evidence-id]")).toHaveCount(2);
    await shot(page, `${width}-photos-chosen`);
    await page.locator("#liveRentalPaid").selectOption("not-rental");
    await page.locator("#liveSubmit").click();
    await expect(page.locator("#issuerCard")).toBeVisible({ timeout: 20000 });
    await page.locator('input[value="certified"]').check();
    await page.locator("#termsAccepted").check();
  await page.locator("#actAuthBtn").click();
    await expect(page.locator("#s5")).toBeVisible({ timeout: 20000 });
    await page.getByRole("button", { name: /letter studio/ }).click();
    await expect(page.locator("#liveFindings")).toBeVisible({ timeout: 20000 });
    await page.locator("#liveDraftBtn").click();
    await expect(page.locator("#liveLetterText")).toBeVisible({ timeout: 20000 });
    await page.locator("#liveAdopt").click();
    await expect(page.locator("#livePreview")).toBeEnabled();
    await page.locator("#livePreview").click();
    await expect(page.locator("#liveOpenPacket")).toHaveAttribute("href", /^blob:/);
    await expect(page.locator("#livePacketPage")).toBeVisible({ timeout: 15000 });
    const packet = Buffer.from(await page.locator("#liveOpenPacket").evaluate(async (element) => Array.from(new Uint8Array(await (await fetch((element as HTMLAnchorElement).href)).arrayBuffer()))));
    mkdirSync(output, { recursive: true }); writeFileSync(resolve(output, `${width}-photos-packet.pdf`), packet);
    const colors = pageColors(packet);
    test.skip(colors === null, "NEEDS_EXTERNAL: pdftoppm (poppler-utils) is required to inspect packet page order");
    // Letter page, ticket copy, then the customer's chosen photos in their final order.
    expect(colors!.length).toBe(4);
    const [three, two] = [colors![2], colors![3]];
    expect(three[2]).toBeGreaterThan(three[0]);   // blue-tinted: "three"
    expect(two[1]).toBeGreaterThan(two[0]);       // green-tinted: "two"
    expect(two[1]).toBeGreaterThan(two[2]);
    note(`${width}-photos-packet`, { pages: colors!.length, finalOrder: ["three", "two"], removed: ["one"], reorderedByKeyboard: true, viewport: width });
  });
}

test("T37 keyboard-only letter authorization (768px): adopt, signature image, exact packet, authorize", async ({ page }) => {
  test.setTimeout(120000);
  await page.setViewportSize({ width: 768, height: 900 });
  let approvals = 0;
  page.on("request", (request) => { if (request.url().endsWith("/approval") && request.method() === "POST") approvals++; });
  const id = await mailedCase(page, "768-keyboard", "keyboard");
  expect(approvals).toBe(1);
  const status = await call(page, "GET", `/cases/${id}/status`);
  expect(["IN_TRANSIT", "SUBMITTING", "FROZEN"]).toContain(status.state);
  await shot(page, "768-keyboard-mailed");
  note("768-keyboard", { state: status.state, payment: status.payment_state, approvals, noPointerClicksOnLetterControls: true });
});

test.describe("touch input (375px)", () => {
  test.use({ hasTouch: true, isMobile: true });
  test("T37 touch-only letter authorization: tap through adoption, packet, authorization", async ({ page }) => {
    test.setTimeout(120000);
    const id = await seedDraft(page);
    await page.locator("#liveAdopt").tap();
    await expect(page.locator("#livePreview")).toBeEnabled();
    await page.locator("#liveSignatureFile").setInputFiles(file("own-signature.png"));
    await page.locator("#livePreview").tap();
    await expect(page.locator("#liveOpenPacket")).toHaveAttribute("href", /^blob:/);
    await expect(page.locator("#livePacketPage")).toBeVisible({ timeout: 15000 });
    await page.locator("#livePacketReviewed").tap();
    await page.locator("#liveApprove").tap();
    await expect(page.locator("#liveTracking")).toBeVisible({ timeout: 30000 });
    await layout(page); await shot(page, "375-touch-mailed");
    const status = await call(page, "GET", `/cases/${id}/status`);
    note("375-touch", { state: status.state, payment: status.payment_state, hasTouch: true });
  });
});

test.describe("labelled injected states (the fake backend cannot yet produce these)", () => {
  for (const width of [375, 1440]) {
    test(`T37 ${width}px delivery report: injected status/tracking responses for a real mailed case`, async ({ page }) => {
      test.setTimeout(120000);
      await page.setViewportSize({ width, height: 900 });
      const id = await mailedCase(page, `${width}-delivered-setup`);
      const status = await call(page, "GET", `/cases/${id}/status`);
      const tracking = await call(page, "GET", `/cases/${id}/tracking`);
      await page.route("**/api/cases/*/status", (route) => route.fulfill({ json: { ...status, state: "DELIVERED" } }));
      await page.route("**/api/cases/*/tracking", (route) => route.fulfill({ json: { ...tracking, state: "DELIVERED", events: [...tracking.events, { event: "delivered", occurred_at: new Date().toISOString(), description: "Injected synthetic delivery event." }] } }));
      await page.reload();
      await expect(page.locator("#h-s7")).toHaveText("USPS reports delivery.");
      await expect(page.locator("#s7")).toContainText("does not establish when the agency opened, reviewed or decided");
      await layout(page); await shot(page, `${width}-delivered-injected`);
      note(`${width}-delivered-injected`, { injected: true, reason: "no durable path emits DeliveryProofStored yet", viewport: width });
    });
  }

  test("T37 375px expired review window: guard, saved-status check and recovery", async ({ page }) => {
    test.setTimeout(120000);
    await page.setViewportSize({ width: 375, height: 900 });
    const id = await seedDraft(page);
    const status = await call(page, "GET", `/cases/${id}/status`);
    await page.route("**/api/cases/*/status", (route) => route.fulfill({ json: { ...status, window_status: "EXPIRED", seconds_remaining: 0 } }));
    await page.reload();
    await expect(page.locator(".guard")).toContainText("saved review window has ended");
    await expect(page.locator("#pillT")).toHaveText("Closed");
    await shot(page, "375-window-expired-injected");
    await page.unroute("**/api/cases/*/status");
    await page.getByRole("button", { name: "Check my appeal" }).click();
    await expect(page.locator(".guard")).toHaveCount(0, { timeout: 15000 });
    await shot(page, "375-window-recovered");
    note("375-window-expired", { injected: true, recovered: true });
  });

  test("T37 375px customer question from the saved case: announced choices, answer retained", async ({ page }) => {
    test.setTimeout(120000);
    await page.setViewportSize({ width: 375, height: 900 });
    const id = await seedDraft(page);
    const status = await call(page, "GET", `/cases/${id}/status`);
    const question = { prompt: "What is your relationship to the vehicle?", choices: ["REGISTERED_OWNER", "DRIVER_NOT_OWNER", "BUSINESS_OR_FLEET_VEHICLE", "RENTAL_OR_LEASE_RENTER", "AUTHORISED_REPRESENTATIVE", "OTHER"] };
    let answers = 0;
    await page.route("**/api/cases/*/status", (route) => route.fulfill({ json: { ...status, state: "NEEDS_CUSTOMER", question } }));
    await page.route("**/api/cases/*/answers", async (route) => {
      answers++;
      expect(JSON.parse(route.request().postData() ?? "{}")).toEqual({ question_id: "VEHICLE_RELATIONSHIP", answer: "DRIVER_NOT_OWNER" });
      await page.unroute("**/api/cases/*/status");
      await route.fulfill({ json: status });
    });
    await page.reload();
    await expect(page.locator("#liveCustomerQuestion")).toBeVisible();
    await expect(page.locator("#liveDraftBtn")).toBeDisabled();
    await layout(page); await shot(page, "375-customer-question-injected");
    await page.getByRole("button", { name: "Driver, not the owner" }).click();
    await expect(page.locator("#liveCustomerQuestion")).toHaveCount(0);
    expect(answers).toBe(1);
    note("375-customer-question", { injected: true, answerSent: "DRIVER_NOT_OWNER", answers });
  });

  test("T37 375px saved-state outage on open and on resume: persistent message, no lost work, recovery", async ({ page }) => {
    test.setTimeout(120000);
    await page.setViewportSize({ width: 375, height: 900 });
    const id = await seedDraft(page);
    // Pricing outage has dedicated pricing tests; isolate saved-state recovery here.
    await page.route("**/api/**", (route) => new URL(route.request().url()).pathname === "/api/auth/pricing"
      ? route.fallback() : route.abort("connectionrefused"));
    await page.reload();
    await expect(page.locator("main h1")).toHaveText("We couldn’t confirm this appeal’s funding");
    await expect(page.getByRole("button", { name: "Retry saved status" })).toBeVisible();
    await expect(page.locator("p[role=alert]")).toContainText("No paid checkout is shown");
    await expect(page.getByLabel("Assigned certified mailing price")).toHaveCount(0);
    await shot(page, "375-outage-injected");
    await page.unroute("**/api/**");
    await page.reload();
    await expect(page.locator("#liveLetterText")).toBeVisible();
    expect((await call(page, "GET", `/cases/${id}/draft`)).case_id).toBe(id);
    await shot(page, "375-outage-recovered");
    note("375-outage", { injected: true, recoveredToSavedLetter: true });
  });
});
