import { expect, type Page } from "@playwright/test";
import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

// Shared helpers for the T37 browser evidence specs. Requires the assembled app and the
// SQLite fake-provider API with per-appeal HTTPS cookies. Never run against test/live providers.
export const base = process.env.SECURE_BASE;
export const apiBase = process.env.INGRESS_API_BASE;
export const workerSecret = process.env.INGRESS_WORKER_SECRET;
export const output = resolve(process.env.T37_EVIDENCE_DIR ?? "../build-kit/evidence/task-T37-2026-10-22/browser");
export const fixtures = resolve("../build-kit/evidence/task-T37-2026-10-22/fixtures");
export const png = readFileSync(resolve(fixtures,"ticket.png"));
export const file = (name: string) => ({name,mimeType:"image/png",buffer:readFileSync(resolve(fixtures,name === "own-signature.png" ? "signature.png" : name))});

export async function call(page: Page, method: string, path: string, body?: unknown) {
  return page.evaluate(async ({method,path,body}) => {
    const csrf = document.cookie.split("; ").find(x=>x.startsWith("__Host-pb-csrf="))?.split("=")[1] ?? "";
    const response = await fetch(`/api${path}`,{method,headers:{"Content-Type":"application/json","X-CSRF-Token":csrf},body:body === undefined ? undefined : JSON.stringify(body)});
    if (!response.ok) throw new Error(`Synthetic request failed: ${method} ${path} ${response.status}`);
    return response.json();
  },{method,path,body});
}
// Polling helper: a not-yet-created appeal session is an expected transient, not a failure.
export async function appeal(page: Page) {
  return page.evaluate(async () => {
    const response = await fetch("/api/auth/appeal",{cache:"no-store"});
    return response.ok ? response.json() : null;
  });
}
// Full-height viewport screenshots keep the fixed action bar at the bottom of the capture
// (rather than overlaying mid-page content, as a stitched fullPage capture does).
export async function shot(page: Page, name: string) {
  mkdirSync(output,{recursive:true});
  await accessible(page,name);
  const viewport = page.viewportSize()!;
  await page.evaluate(()=>window.scrollTo(0,0));
  const height = await page.evaluate(()=>document.documentElement.scrollHeight);
  await page.setViewportSize({width:viewport.width,height:Math.min(Math.max(height,viewport.height),7000)});
  await page.waitForTimeout(200);
  await page.screenshot({path:resolve(output,`${name}.png`)});
  await page.setViewportSize(viewport);
}
// Automated WCAG 2.1 A/AA scan (axe-core from the already-installed lint dependency tree).
export async function accessible(page: Page, name: string) {
  await page.addScriptTag({path:require.resolve("axe-core/axe.min.js")});
  const result = await page.evaluate(async()=>{
    const axe = (window as unknown as {axe:{run:(context:Document,options:object)=>Promise<{violations:Array<{id:string;impact:string|null;help:string;nodes:Array<{target:unknown[]}>}>;passes:unknown[]}>}}).axe;
    const run = await axe.run(document,{runOnly:{type:"tag",values:["wcag2a","wcag2aa","wcag21a","wcag21aa"]}});
    return {violations:run.violations.map(v=>({id:v.id,impact:v.impact,help:v.help,targets:v.nodes.map(n=>n.target).slice(0,8)})),passes:run.passes.length};
  });
  mkdirSync(output,{recursive:true});
  writeFileSync(resolve(output,`${name}-axe.json`),JSON.stringify({synthetic:true,standard:"axe-core WCAG 2.1 A/AA",...result},null,2));
  expect(result.violations,`axe violations at ${name}`).toEqual([]);
}
export async function layout(page: Page) {
  expect(await page.evaluate(()=>document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  const controls = page.locator("button:visible, input[type=file]:visible, .tier:visible");
  for (const control of await controls.all()) {
    const box = await control.boundingBox();
    expect(box?.width).toBeGreaterThanOrEqual(44);
    expect(box?.height).toBeGreaterThanOrEqual(44);
  }
  // Nothing the customer needs may be hidden behind an ellipsis or clipped.
  const truncated = await page.evaluate(()=>[...document.querySelectorAll<HTMLElement>("body *")].filter(element=>{
    if (element.classList.contains("sr")) return false; // visually-hidden text for screen readers
    const style = getComputedStyle(element);
    return (style.textOverflow === "ellipsis" || style.overflow === "hidden" && style.whiteSpace === "nowrap") && element.scrollWidth > element.clientWidth + 1;
  }).map(element=>`${element.tagName.toLowerCase()}.${element.className}: ${element.textContent?.slice(0,60)}`));
  expect(truncated,"visible text truncated").toEqual([]);
  expect(await page.evaluate(()=>matchMedia("(prefers-reduced-motion: reduce)").matches)).toBe(true);
  const animations = await page.locator(".screen").evaluateAll(elements=>elements.flatMap(element=>[element,...element.querySelectorAll("*")]).some(element=>{
    const style = getComputedStyle(element);
    return style.animationName !== "none" && parseFloat(style.animationDuration) > .001;
  }));
  expect(animations).toBe(false);
  await expect(page.locator("#jumperBtn, #demoWarn, #demoLapse")).toHaveCount(0);
  await expect(page.locator("body")).not.toContainText(/\blawyer\b|legal advice|guaranteed|\bwin\b|dismissal rates/i);
}
export async function openDetails(page: Page) {
  await page.goto(base!);
  expect((await call(page,"GET","/health")).mode).toBe("fake");
  await page.locator("#actSnap").click();
  for (const value of ["SYN-T37",new Date(Date.now()-3*86400000).toLocaleDateString("en-US"),"Meter expired","San Francisco Municipal Transportation Agency"]) {
    await page.locator("#fixIn").fill(value); await page.locator("#actSaveFix").click();
  }
  await page.locator("#plateIn").fill("SYN123"); await page.locator("#plateState").fill("CA");
  await page.locator("#actContinueTicket").click();
  await page.locator("#actSendStory").click();
  await expect(page.locator("#story")).toBeFocused();
  await expect(page.locator("#story")).toHaveAttribute("aria-invalid","true");
  await expect(page.locator("#storyErr")).toContainText("own words");
  await page.locator("#story").fill("The sign was covered. The kiosk was blank.");
  await page.locator("#actSendStory").click();
  await page.getByRole("button",{name:/^Yes/}).click();
  await expect(page.locator("#liveDetails")).toBeVisible();
}
export async function completeDetails(page: Page) {
  for (const [id,value] of Object.entries({liveIssuedCity:"San Francisco",liveIssuedState:"CA",liveName:"Synthetic Customer",livePhone:"415-555-0100",liveEmail:"t37@example.test",liveLine1:"123 Example Street",liveCity:"San Francisco",liveState:"CA",liveZip:"94103"})) await page.locator(`#${id}`).fill(value);
  await page.locator("#liveRentalPaid").selectOption("not-rental");
  await page.locator("#liveTicketPhoto").setInputFiles(file("ticket.png"));
  await expect.poll(async()=>(await appeal(page))?.intake_form?.ticket_photo_ref).toBeTruthy();
}
export async function seedDraft(page: Page) {
  await page.goto(base!);
  expect((await call(page,"GET","/health")).mode).toBe("fake");
  await call(page,"POST","/auth/appeals",{});
  const workspace = await call(page,"POST","/workspaces",{});
  const photo = await call(page,"POST",`/workspaces/${workspace.workspace_id}/photos`,{content_type:"image/png",data_base64:png.toString("base64")});
  const receipt = await call(page,"POST",`/workspaces/${workspace.workspace_id}/intake`,{
    ticket:{citation_number:"SYN-T37",license_plate:"SYN123",license_plate_state:"CA",issuer_hint:"San Francisco Municipal Transportation Agency",issued_city:"San Francisco",issued_state:"CA",violation_code:"Meter expired",violation_description:"Meter expired",issue_date:new Date(Date.now()-3*86400000).toISOString().slice(0,10)},
    contact:{name:"Synthetic Customer",phone:"415-555-0100",email:"t37@example.test",mailing_address:{line1:"123 Example Street",city:"San Francisco",state:"CA",zip_code:"94103"}},
    customer_position:{statement:"The sign was covered. The kiosk was blank."},citation_photo_ref:photo.photo_ref,
    rental_company_paid:"NOT_RENTAL",
  });
  const id: string = receipt.case_id;
  await call(page,"PUT",`/cases/${id}/tier`,{tier:"CERTIFIED"});
  await call(page,"POST",`/cases/${id}/authorization`,{terms_accepted:true,payment_method_id:"pm_card_visa"});
  for (const step of ["research","draft"]) {
    const response = await page.request.post(`${apiBase}/internal/cases/${id}/step`,{headers:{Authorization:`Bearer ${workerSecret}`},data:{step_id:step}});
    expect(response.status(),await response.text()).toBe(200);
  }
  await page.reload();
  await expect(page.locator("#liveLetterText")).toBeVisible();
  return id;
}
export async function inspect(page: Page, id: string, name: string) {
  await page.locator("#liveAdopt").click();
  await expect(page.locator("#livePreview")).toBeEnabled();
  await page.locator("#liveSignatureFile").setInputFiles(file("own-signature.png"));
  await page.locator("#livePreview").click();
  await expect(page.locator("#livePacketPreview iframe")).toHaveAttribute("src",/^blob:/);
  await expect(page.locator("#liveApprove")).toBeDisabled();
  const bytes = await page.locator("#liveOpenPacket").evaluate(async element => Array.from(new Uint8Array(await (await fetch((element as HTMLAnchorElement).href)).arrayBuffer())));
  const packet = Buffer.from(bytes);
  expect(packet.subarray(0,5).toString()).toBe("%PDF-");
  mkdirSync(output,{recursive:true}); writeFileSync(resolve(output,`${name}-packet.pdf`),packet);
  const draft = await call(page,"GET",`/cases/${id}/draft`);
  expect(draft.adopted_sha256).toBe(createHash("sha256").update(draft.text).digest("hex"));
  await page.locator("#livePacketReviewed").check();
  await expect(page.locator("#liveApprove")).toBeEnabled();
}

