import { expect, test } from "./synthetic-fixtures";
import { writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { apiBase, appeal, base, call, completeDetails, file, inspect, layout, openDetails, output, seedDraft, shot, workerSecret } from "./t37-support";

// Requires the assembled app and SQLite fake-provider API, with per-appeal HTTPS
// cookies. Never run against test/live providers. No response fixtures stand in for
// positive durable flow. Injected transport failures are explicitly labelled below.
test.use({ignoreHTTPSErrors:true,contextOptions:{reducedMotion:"reduce"}});
test.skip(!base || !apiBase || !workerSecret,"NEEDS_EXTERNAL: isolated HTTPS app, durable fake backend and private fake-worker configuration");

for (const width of [320,375,768,1440]) {
  test(`T37 ${width}px evidence, announcements, keyboard, save/reload and optional selection`,async({page})=>{
    await page.setViewportSize({width,height:900});
    await openDetails(page);
    await page.locator("#liveSubmit").click();
    await expect(page.locator("#liveIssuedCity")).toBeFocused();
    await expect(page.locator("#liveIssuedCity")).toHaveAttribute("aria-describedby","liveIssuedCity-error");
    await completeDetails(page);
    await page.locator("#liveEvidencePhoto").setInputFiles([file("one.png"),file("two.png"),file("three.png")]);
    await expect.poll(async()=>(await appeal(page))?.intake_form?.evidence_photo_refs?.length).toBe(3);
    const refs = (await appeal(page)).intake_form.evidence_photo_refs;
    const move = page.getByRole("button",{name:"Move supporting photo 3 up",exact:true});
    await move.focus(); await page.keyboard.press("Enter");
    await page.getByRole("button",{name:"Remove supporting photo 1",exact:true}).click();
    await expect(page.locator("#liveEvidencePhoto")).toBeFocused();
    await expect.poll(async()=>(await appeal(page))?.intake_form?.evidence_photo_refs).toEqual([refs[2],refs[1]]);
    await page.reload();
    await expect(page.locator("#liveEvidenceList [data-evidence-id]")).toHaveCount(2);
    await expect(page.locator("#liveName")).toHaveValue("Synthetic Customer");
    await expect(page.locator("#photoGuide")).toContainText("continue without");
    // Keyboard modality: move away and back with Tab so :focus-visible is genuinely exercised.
    await page.locator("#liveEvidencePhoto").focus();
    await page.keyboard.press("Shift+Tab"); await page.keyboard.press("Tab");
    await expect(page.locator("#liveEvidencePhoto")).toBeFocused();
    expect(await page.locator("#liveEvidencePhoto").evaluate(el=>getComputedStyle(el).outlineStyle)).not.toBe("none");
    await layout(page); await shot(page,`${width}-evidence-reload`);
    await page.getByRole("button",{name:"Rules and what we never do"}).click();
    await expect(page.locator("#how")).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.locator("#how")).not.toBeVisible();
    await expect(page.getByRole("button",{name:"Rules and what we never do"})).toBeFocused();
    // Continue with no optional supporting photos; this must remain a valid intake.
    while (await page.locator("#liveEvidenceList [data-evidence-id]").count()) await page.getByRole("button",{name:"Remove supporting photo 1",exact:true}).click();
    await page.locator("#liveSubmit").click();
    await expect(page.locator("#issuerCard")).toBeVisible({timeout:20000});
    await shot(page,`${width}-offer`);
    await page.locator('input[value="certified"]').check();
    await page.route("**/api/cases/*/authorization",route=>route.fulfill({status:402,json:{detail:"card_not_authorized"}}));
    await page.locator("#actAuthBtn").click();
    await expect(page.locator("#liveActionError")).toContainText("card wasn");
    await shot(page,`${width}-payment-error`);
    await page.unroute("**/api/cases/*/authorization");
    await page.locator("#actAuthBtn").click();
    await expect(page.locator("#s5")).toBeVisible({timeout:20000});
    await layout(page); await shot(page,`${width}-review-window`);
    await page.getByRole("button",{name:/letter studio/}).click();
    await expect(page.locator("#liveFindings")).toBeVisible({timeout:20000});
    await layout(page); await shot(page,`${width}-research`);
    await page.locator("#liveDraftBtn").click();
    await expect(page.locator("#liveLetterText")).toBeVisible({timeout:20000});
  });
  test(`T37 ${width}px exact refusal, edits, packet, delayed capture, reload and recovery`,async({page})=>{
    test.setTimeout(90000);
    await page.setViewportSize({width,height:900});
    const id = await seedDraft(page);
    let approvals = 0; let authorizations = 0;
    page.on("request",r=>{if(r.url().endsWith("/approval") && r.method()==="POST") approvals++; if(r.url().endsWith("/authorization") && r.method()==="POST") authorizations++;});
    const original = await page.locator("#liveLetterText").inputValue();
    await page.locator("#liveRefuse").click();
    // Record every countdown text from first paint so a transient bad value cannot hide behind retries.
    await page.addInitScript(()=>{
      const seen: string[] = []; (window as unknown as {__pill:string[]}).__pill = seen;
      new MutationObserver(()=>{const text = document.getElementById("pillT")?.textContent; if (text && seen[seen.length-1] !== text) seen.push(text);}).observe(document,{subtree:true,childList:true,characterData:true});
    });
    await page.reload();
    await expect(page.locator("#h-s6")).toHaveText("Your letter is paused.");
    // A resumed review window must never flash a nonsensical countdown before the first clock tick.
    const status = await call(page,"GET",`/cases/${id}/status`);
    const windowSeconds = (Date.parse(status.window_expires_at) - Date.parse(status.window_started_at)) / 1000;
    const checkCountdown = (text: string) => {
      expect(text).toMatch(/^\d{1,3}:[0-5]\d$/);
      const [minutes, seconds] = text.split(":").map(Number);
      expect(minutes * 60 + seconds).toBeGreaterThan(0);
      expect(minutes * 60 + seconds).toBeLessThanOrEqual(windowSeconds);
    };
    await expect(page.locator("#pillT")).toHaveText(/^\d{1,3}:[0-5]\d$/);
    const seen = await page.evaluate(()=>(window as unknown as {__pill:string[]}).__pill);
    expect(seen.length).toBeGreaterThan(0);
    for (const text of seen) checkCountdown(text);
    expect(approvals).toBe(0);
    await shot(page,`${width}-refused`);
    await page.locator("#liveResumeLetter").click();
    const edited = original + "\nThese additional words are mine: the kiosk was blank.";
    await page.locator("#liveLetterText").fill(edited);
    await expect.poll(async()=>(await call(page,"GET",`/cases/${id}/draft`)).text).toBe(edited);
    await page.reload(); await expect(page.locator("#liveLetterText")).toHaveValue(edited);
    await inspect(page,id,`${width}`); await layout(page); await shot(page,`${width}-packet`);
    // Delayed durable dispatch: don't call the worker; the real approved case stays frozen.
    await page.route("**/dev-worker/*/dispatch",async route=>{
      const saved = await call(page,"GET",`/cases/${id}/status`); expect(saved.state).toBe("FROZEN");
      await route.fulfill({json:saved});
    });
    await page.locator("#liveApprove").click();
    await expect(page.locator("#s7")).toBeVisible();
    await expect(page.locator("#s7")).toContainText("Mailing and payment are not both confirmed yet");
    expect(approvals).toBe(1);
    await page.reload(); await expect(page.locator("#h-s7")).toContainText("Mailing authorized");
    await expect(page.locator("#s7")).not.toContainText("USPS reports delivery");
    await shot(page,`${width}-delayed-reload`);
    await page.unroute("**/dev-worker/*/dispatch");
    // Recovery failure is a transport failure, never a fabricated saved state.
    await page.route("**/api/cases/*/status",route=>route.abort("connectionreset"));
    await page.locator("#liveApprove").click();
    await expect(page.locator("#s7 [role=alert]")).toBeVisible(); await shot(page,`${width}-status-error`);
    await page.unroute("**/api/cases/*/status");
    await page.locator("#liveApprove").click();
    await expect(page.locator("#liveTracking")).toBeVisible({timeout:30000});
    await layout(page); await shot(page,`${width}-mailed`);
    await page.reload(); await expect(page.locator("#liveTracking")).toBeVisible();
    // The promised PDF copy of the mailed packet: the exact accepted bytes, verified by hash.
    await page.locator("#livePrepareMailingRecord").click();
    await expect(page.locator("#liveMailingRecord")).toHaveAttribute("download","parkingbreaker-mailing-record.pdf");
    await expect(page.locator("#s7")).toContainText("not proof of delivery or a USPS Return Receipt");
    expect(approvals).toBe(1); expect(authorizations).toBe(0);
    const final = await call(page,"GET",`/cases/${id}/status`);
    expect(final.payment_state).toBe("CAPTURED");
    writeFileSync(resolve(output,`${width}-interaction.json`),JSON.stringify({synthetic:true,backendMode:"fake",state:final.state,payment:final.payment_state,approvalsAfterDraft:approvals,authorizationsAfterDraft:authorizations,packetAndDestinationInspected:true,viewport:width},null,2));
  });
}
