import { expect, test, type Page } from "@playwright/test";
import { buildLetterData, SAMPLE_FIELDS, SAMPLE_STORY } from "../lib/api";

async function guardrails(page: Page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  const small = await page.locator("button, input, textarea, a").evaluateAll((elements) => elements.flatMap((element) => {
    const rect = element.getBoundingClientRect();
    if (!rect.width || !rect.height || !element.checkVisibility()) return [];
    return rect.width < 43.9 || rect.height < 43.9 ? [`${element.outerHTML.slice(0, 130)} (${rect.width}×${rect.height})`] : [];
  }));
  expect(small).toEqual([]);
  const text = await page.locator("body").evaluate((body) => (body as HTMLElement).innerText + " " + Array.from(body.querySelectorAll("[aria-label]")).map((e) => e.getAttribute("aria-label")).join(" "));
  expect(text.replaceAll("not a lawyer", "").replaceAll("no arrival guarantee", "")).not.toMatch(/\b(?:guarantees?|guaranteed|lawyer|legal advice|win|beat|dismiss(?:ed)?)\b/i);
}

async function jump(page: Page, step: number) {
  await page.getByRole("button", { name: "Steps ▾" }).click();
  await guardrails(page);
  await page.locator(`#jumper [data-go="${step}"]`).click();
  await expect(page.locator(`#s${step}`)).toBeVisible();
  await expect(page.locator(".screen")).toHaveCount(1);
  await guardrails(page);
}

async function toOffer(page: Page) {
  await page.goto("/");
  await guardrails(page);
  await page.locator("#actSnap").click();
  await expect(page.locator("#actOkField")).toBeVisible();
  for (let field = 0; field < 5; field++) {
    await guardrails(page);
    await page.locator("#actOkField").click();
  }
  await page.locator("#actContinueTicket").click();
  await expect(page.locator("#receipts li")).toHaveCount(1);
  await page.getByRole("button", { name: "Use sample answer", exact: true }).click();
  await page.locator("#actSendStory").click();
  await page.getByRole("button", { name: /^Yes →$/ }).click();
  await page.getByRole("button", { name: /^Yes, save it →$/ }).click();
  await page.getByRole("button", { name: "Start the research" }).click();
  await page.locator("#skipR").click();
  await page.getByRole("button", { name: "Preview deadline and options" }).click();
  await expect(page.locator('input[name="tier"]:checked')).toHaveCount(0);
  await expect(page.locator("#pay")).toHaveCount(0);
  await guardrails(page);
  await page.getByRole("button", { name: "Letter layout sample", exact: true }).click();
  await expect(page.locator("#sampleDlg")).toBeVisible();
  await expect(page.locator("#pdfDlg")).toHaveCount(0);
  await page.locator("#sampleDlg").getByRole("button", { name: "Close", exact: true }).click();
}

test.describe("browser flows", () => {
// Prototype controls exist only in development, so exercise the actual dev fixture.
test.skip(!process.env.PROTOTYPE_BASE, "requires a development-only prototype fixture");
test.use({baseURL: process.env.PROTOTYPE_BASE});

for (const tier of ["certified"] as const) {
  test(`${tier}: sample steps 1–7, adoption, PDF, tracking and downloads`, async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await toOffer(page);
    await page.locator(`input[value="${tier}"]`).check();
    await page.locator("#actAuthBtn").click();
    await expect(page.locator("#s5")).toBeVisible();
    await expect(page.locator("#pillT")).not.toHaveText("0:00");
    await guardrails(page);
    await page.getByRole("button", { name: "Open the letter studio" }).click();
    await expect(page.locator("#actAdoptBtn")).toBeDisabled();
    await expect(page.locator("#pdfDlg")).toHaveCount(0);
    await guardrails(page);
    await page.getByRole("button", { name: "Show me", exact: true }).click();
    await page.locator("#provDlg").getByRole("button", { name: "Remove", exact: true }).click();
    await expect(page.locator("#actAdoptBtn")).toBeEnabled();
    await page.locator("#actAdoptBtn").click();
    await expect(page.locator("#senderBox")).toBeVisible();
    await page.locator("#actPreviewPdf").click();
    await expect(page.locator("#pdfDlg")).toBeVisible();
    await expect(page.locator("#pdfCanvasMount canvas")).toHaveCount(1);
    await guardrails(page);
    const pdfDownload = page.waitForEvent("download");
    await page.getByRole("link", { name: "Download exact PDF" }).click();
    expect((await pdfDownload).suggestedFilename()).toBe("parkingbreaker-letter-sample.pdf");
    await page.locator("#approveBtn").click();
    await expect(page.locator("#s7")).toBeVisible();
    await expect(page.locator("#pill")).toHaveCount(0);
    await expect(page.locator("#actDlEvidence")).toBeEnabled();
    await expect(page.locator("#tracking li")).toHaveCount(tier === "certified" ? 5 : 2);
    await expect(page.locator("#receipts li")).toHaveCount(5);
    await guardrails(page);
    const archive = page.waitForEvent("download");
    await page.locator("#actDlEvidence").click();
    expect((await archive).suggestedFilename()).toContain("evidence-record");
    if (tier === "certified") {
      const report = page.waitForEvent("download");
      await page.locator("#dlReport").click();
      expect((await report).suggestedFilename()).toContain("delivery-confirmation");
    } else await expect(page.locator("#dlReport")).toHaveCount(0);
    await page.getByRole("button", { name: "Not yet", exact: true }).click();
    await expect(page.locator("#outcome")).toContainText("Thanks. Noted");
    expect(errors).toEqual([]);
  });
}

test("jumper, sheets, edit provenance, expiry, restart and re-entry rules", async ({ page }) => {
  await page.goto("/");
  for (const step of [8, 9]) await jump(page, step);
  await expect(page.locator("#conflictOpt")).not.toBeChecked();
  await page.locator("#conflictOpt").check();
  await expect(page.locator("#conflictPreview")).toContainText("Opted in");
  await jump(page, 6);
  await page.getByRole("button", { name: "Show me", exact: true }).click();
  await page.getByRole("button", { name: "Edit sentence", exact: true }).click();
  await page.locator("#editTa").fill("I returned to my car the next morning.");
  await page.locator("#provDlg").getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.locator("#editNoteErr")).toBeVisible();
  await page.locator("#editNote").fill("I remember returning the next morning.");
  await page.locator("#provDlg").getByRole("button", { name: "Save", exact: true }).click();
  await page.getByRole("button", { name: "Your notes", exact: true }).click();
  await expect(page.locator("#notesDlg")).toContainText("I remember returning the next morning.");
  await page.locator("#notesDlg").getByRole("button", { name: "Close", exact: true }).click();
  await jump(page, 5);
  await page.locator("#demoWarn").click();
  await expect(page.locator("#toast")).toContainText("Five minutes left");
  await page.locator("#demoLapse").click();
  await expect(page.locator("#toast")).toContainText("window closed");
  await page.getByRole("button", { name: "Open the letter studio" }).click();
  await expect(page.locator("#paper")).toContainText("I returned to my car the next morning.");
  await expect(page.locator("#actRestartBar")).toBeVisible();
  await page.locator("#actRestartBar").click();
  await expect(page.locator("#actAdoptBtn")).toBeEnabled();
  await page.locator("#actAdoptBtn").click();
  await page.locator("#senderName").fill(" ");
  await expect(page.locator("#actPreviewPdf")).toBeDisabled();
  await page.locator("#senderName").fill("Sample Sender");
  await page.locator("#actPreviewPdf").click();
  await page.locator("#pdfDlg").getByRole("button", { name: "Go back", exact: true }).click();
  await page.locator("#unlockBtn").click();
  await expect(page.locator("#pdfDlg")).toHaveCount(0);
  await jump(page, 4);
  await page.locator('input[value="certified"]').check();
  await jump(page, 3);
  await page.getByRole("button", { name: "Official evidence checklist" }).click();
  await page.locator('#chkDlg [data-fact="f1"]').click();
  await expect(page.locator("#factDlg")).toBeVisible();
  await guardrails(page);
  await page.locator("#factDlg").getByRole("button", { name: "Close", exact: true }).click();
  await jump(page, 4);
  await expect(page.locator('input[name="tier"]:checked')).toHaveCount(0);
  await jump(page, 9);
  await expect(page.locator("#conflictOpt")).not.toBeChecked();
});

});

test("pure draft adapter keeps custom statements and omits unconfirmed ownership", () => {
  const custom = buildLetterData(SAMPLE_FIELDS, "LOCAL123", "AZ", "I parked at noon. I saw a covered sign.", "no", false);
  expect(custom.sents.filter((sentence) => sentence.id.startsWith("c")).map((sentence) => sentence.text)).toEqual(["I parked at noon.", "I saw a covered sign."]);
  expect(custom.sents.some((sentence) => sentence.id === "s7")).toBe(false);
  const sample = buildLetterData(SAMPLE_FIELDS, "DEMO000", "CA", SAMPLE_STORY, "yes", true);
  expect(sample.sents.filter((sentence) => sentence.flag)).toHaveLength(1);
  expect(sample.sents.find((sentence) => sentence.id === "s4")?.src).toEqual(["n3"]);
});
