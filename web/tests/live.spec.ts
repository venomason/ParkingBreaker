import { expect, test } from "@playwright/test";
import { deflateSync } from "node:zlib";

// Real flow against a running fake-mode API and web server (see .claude/launch.json:
// api-fake on 8000, web-live on 3002). Skipped unless LIVE_BASE is set.
const LIVE_BASE = process.env.LIVE_BASE;
const SHOTS = process.env.SHOTS; // optional folder for step screenshots
const shot = async (page: import("@playwright/test").Page, name: string) => {
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: true });
};
test.skip(!LIVE_BASE, "needs LIVE_BASE (web-live) and the fake-mode API");

function png(): Buffer {
  // A valid 2x2 white PNG built in memory.
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
  const raw = Buffer.from([0, 255, 255, 255, 255, 255, 255, 0, 255, 255, 255, 255, 255, 255]);
  return Buffer.concat([
    Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]),
    chunk("IHDR", ihdr), chunk("IDAT", deflateSync(raw)), chunk("IEND", Buffer.alloc(0)),
  ]);
}

test("live ticket runs from intake to mailed and charged", async ({ page }) => {
  const bodies: string[] = [];
  let authorizationRequests = 0;
  let approvalRequests = 0;
  page.on("request", (r) => {
    if (r.method() === "POST" && r.url().endsWith("/authorization")) authorizationRequests++;
    if (r.method() === "POST" && r.url().endsWith("/approval")) approvalRequests++;
  });
  page.on("request", (r) => { if (r.url().includes("/api/")) bodies.push(r.postData() ?? ""); });
  await page.goto(LIVE_BASE!);
  await page.locator("#actSnap").click();
  const issued = new Date(Date.now() - 3 * 86400000);
  const date = `${String(issued.getMonth() + 1).padStart(2, "0")}/${String(issued.getDate()).padStart(2, "0")}/${issued.getFullYear()}`;
  for (const value of ["SYN-0001", date, "Meter expired", "San Francisco Municipal Transportation Agency"]) {
    await page.locator("#fixIn").fill(value);
    await page.locator("#actSaveFix").click();
  }
  await page.locator("#plateIn").fill("SYN123");
  await page.locator("#plateState").fill("CA");
  await page.locator("#actContinueTicket").click();
  await page.locator("#story").fill("The meter was broken when I parked; I photographed its error screen.");
  await page.locator("#actSendStory").click();
  await page.getByRole("button", { name: /^Yes/ }).first().click();
  await page.locator("#liveIssuedCity").fill("San Francisco");
  await page.locator("#liveIssuedState").fill("CA");
  await page.locator("#liveName").fill("Alex Example");
  await page.locator("#livePhone").fill("415-555-0100");
  await page.locator("#liveEmail").fill("alex@example.test");
  await page.locator("#liveLine1").fill("123 Example Street");
  await page.locator("#liveCity").fill("San Francisco");
  await page.locator("#liveZip").fill("94110");
  await page.locator("#liveState").fill("CA");
  await page.locator("#liveTicketPhoto").setInputFiles({ name: "ticket.png", mimeType: "image/png", buffer: png() });
  await page.locator("#liveRentalPaid").selectOption("not-rental");
  await page.locator("#liveSubmit").click();
  await expect(page.locator("#issuerCard")).toContainText("SFMTA", { timeout: 20000 });
  await shot(page, "1-offer");
  await expect(page.locator('input[value="standard"]')).toHaveCount(0);
  await page.locator('input[value="certified"]').check();
  // An unavailable secure card form must never turn into a synthetic card on live providers.
  await page.route("**/api/health", (route) => route.fulfill({ json: { status: "ok", mode: "live" } }));
  await page.locator("#termsAccepted").check();
  await page.locator("#actAuthBtn").click();
  await expect(page.locator("#toast")).toContainText("The secure card form is unavailable");
  expect(authorizationRequests).toBe(0);
  await page.unroute("**/api/health");
  await page.locator("#termsAccepted").check();
  await page.locator("#actAuthBtn").click();
  await expect(page.locator("#s5")).toBeVisible({ timeout: 20000 });
  await expect(page.locator("#demoWarn, #demoLapse, #jumperBtn")).toHaveCount(0);
  await page.getByRole("button", { name: /letter studio/ }).click();
  await expect(page.locator("#liveFindings")).toContainText("Mailing address", { timeout: 20000 });
  await expect(page.locator("#liveSources")).toContainText("sfmta");
  await shot(page, "2-research");
  await page.locator("#liveDraftBtn").click();
  await expect(page.locator("#liveLetterText")).toHaveValue(/Citation SYN-0001/, { timeout: 20000 });
  await page.locator("#liveAdopt").click();
  // Wait until the adoption round trip settles before signing.
  await expect(page.locator("#livePreview")).toBeEnabled({ timeout: 20000 });
  const pad = page.locator("#liveSignature");
  await pad.scrollIntoViewIfNeeded();
  const box = await pad.boundingBox();
  if (!box) throw new Error("no signature pad");
  // Stay clear of the edges: the fixed action bar and the dev-tools badge can overlap them.
  await page.mouse.move(box.x + box.width / 3, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width - 30, box.y + 12, { steps: 8 });
  await page.mouse.up();
  const inked = await pad.evaluate((c) => {
    const ctx = (c as HTMLCanvasElement).getContext("2d")!;
    const { data } = ctx.getImageData(0, 0, (c as HTMLCanvasElement).width, (c as HTMLCanvasElement).height);
    for (let i = 0; i < data.length; i += 4) if (data[i] < 128) return true;
    return false;
  });
  expect(inked).toBe(true);
  await shot(page, "3-letter-signed");
  expect(approvalRequests).toBe(0);
  await page.locator("#livePreview").click();
  await expect(page.locator("#liveOpenPacket")).toHaveAttribute("href", /^blob:/);
  await expect(page.locator("#livePacketPage")).toBeVisible({ timeout: 15000 });
  await expect(page.locator("#liveApprove")).toBeDisabled();
  expect(approvalRequests).toBe(0);
  await page.locator("#livePacketReviewed").check();
  await expect(page.locator("#liveApprove")).toBeEnabled();
  // Simulate delayed payment confirmation for longer than the old four-attempt bound.
  let pendingDispatches = 0;
  await page.route("**/dev-worker/*/dispatch", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    pendingDispatches++;
    await route.fulfill({ response, json: { ...body, payment_state: "CAPTURE_PENDING" } });
  });
  await page.locator("#liveApprove").click();
  await expect(page.getByText(/Mailing and payment are not both confirmed yet/)).toBeVisible({ timeout: 30000 });
  expect(pendingDispatches).toBe(4);
  await expect(page.locator("#liveTracking")).toHaveCount(0);
  await expect(page.locator("#liveApprove")).toHaveText("Check mailing status →");
  await page.unroute("**/dev-worker/*/dispatch");
  await page.locator("#liveApprove").click();
  await expect(page.locator("#liveTracking")).toContainText("Certified tracking number", { timeout: 30000 });
  await expect(page.locator("#liveTracking")).not.toContainText("not reported yet");
  await shot(page, "4-mailed");
  expect(bodies.join("\n")).not.toMatch(/\b4242\s?4242\s?4242\s?4242\b/);
  expect(bodies.some((b) => b.includes("payment_method_id"))).toBe(true);
  expect(authorizationRequests).toBe(1);
  expect(approvalRequests).toBe(1);
});
