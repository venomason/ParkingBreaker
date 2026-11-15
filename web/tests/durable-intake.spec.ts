import { expect, test, type Page, type Route } from "./synthetic-fixtures";
import { deflateSync } from "node:zlib";

const base = process.env.SECURE_BASE;
test.use({ ignoreHTTPSErrors: true });
test.skip(!base, "requires the isolated HTTPS cookie-auth API rehearsal");

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

function forward(route: Route) {
  // Chromium's resolver rules do not apply to Playwright's Node HTTP client.
  const transport = process.env.SECURE_TRANSPORT_BASE;
  return route.fetch(transport ? {url: new URL(new URL(route.request().url()).pathname, transport).href} : {});
}

async function saved(page: Page) {
  return page.evaluate(async () => {
    const response = await fetch("/api/auth/appeal");
    return response.ok ? response.json() : null;
  });
}

async function enterTicket(page: Page) {
  await page.goto(base!);
  await page.locator("#actSnap").click();
  for (const value of ["SYN-DURABLE-1", new Date(Date.now()-3*86400000).toLocaleDateString("en-US"), "Meter expired", "Private parking company"]) {
    await page.locator("#fixIn").fill(value);
    await page.locator("#actSaveFix").click();
  }
  await page.locator("#plateIn").fill("SYN123");
  await page.locator("#plateState").fill("CA");
  await expect.poll(async () => (await saved(page))?.intake_form?.values?.plate).toBe("SYN123");
}

async function openDetails(page: Page) {
  await page.locator("#story").fill("The sign was covered. The kiosk screen was blank.");
  await page.locator("#actSendStory").click();
  await page.getByRole("button", {name:/^Yes/}).first().click();
  await expect(page.locator("#liveDetails")).toBeVisible();
}

test("ticket/contact/front and back photos survive refresh and lost intake response without duplicate upload or case", async ({ page }) => {
  let uploads = 0;
  let creations = 0;
  page.on("request", request => {
    if (request.method() === "POST" && /\/workspaces\/[^/]+\/photos$/.test(request.url())) uploads++;
    if (request.method() === "POST" && request.url().endsWith("/api/workspaces")) creations++;
  });
  await enterTicket(page);
  const workspace = (await saved(page)).workspace_id;
  await page.reload();
  await expect(page.locator("#plateIn")).toHaveValue("SYN123");
  await page.locator("#actContinueTicket").click();
  await openDetails(page);
  for (const [selector,value] of Object.entries({
    liveIssuedCity:"San Francisco",liveIssuedState:"CA",liveName:"Synthetic Customer",livePhone:"415-555-0100",
    liveEmail:"durable@example.test",liveLine1:"123 Example Street",liveCity:"San Francisco",liveState:"CA",liveZip:"94103",
  })) await page.locator(`#${selector}`).fill(value);
  await page.getByRole("radio",{name:"A private parking lot or company"}).check();
  await page.locator("#liveTicketPhoto").setInputFiles({name:"synthetic.png",mimeType:"image/png",buffer:syntheticPng(255)});
  await page.locator("#liveTicketBackPhoto").setInputFiles({name:"synthetic-back.png",mimeType:"image/png",buffer:syntheticPng(0)});
  await expect.poll(async () => Boolean((await saved(page))?.intake_form?.ticket_photo_ref)).toBe(true);
  await expect.poll(async () => Boolean((await saved(page))?.intake_form?.ticket_back_photo_ref)).toBe(true);
  await expect.poll(async () => (await saved(page))?.intake_form?.values?.["contact.name"]).toBe("Synthetic Customer");
  const reference = (await saved(page)).intake_form.ticket_photo_ref;
  const backReference = (await saved(page)).intake_form.ticket_back_photo_ref;
  await page.reload();
  await expect(page.locator("#liveName")).toHaveValue("Synthetic Customer");
  await expect(page.getByText("Saved for this appeal; choose a file only to replace it.")).toBeVisible();
  await expect(page.getByText("Back of ticket saved for this appeal.")).toBeVisible();
  let firstCase: string | null = null;
  await page.route("**/api/workspaces/*/intake", async route => {
    if (firstCase) { await route.continue(); return; }
    const response = await forward(route);
    expect(response.status()).toBe(200);
    firstCase = (await response.json()).case_id;
    await route.abort("connectionreset");
  });
  await page.locator("#liveRentalPaid").selectOption("not-rental");
  await page.locator("#liveSubmit").click();
  await expect.poll(() => firstCase).not.toBeNull();
  await expect(page.locator("#liveSubmit")).toBeEnabled();
  await page.locator("#liveRentalPaid").selectOption("not-rental");
  await page.locator("#liveSubmit").click();
  await expect(page.locator("#liveStopMessage")).toBeVisible();
  const final = await saved(page);
  expect(final.workspace_id).toBe(workspace);
  expect(final.case_id).toBe(firstCase);
  expect(final.intake.citation_photo_ref).toBe(reference);
  expect(final.intake.citation_back_photo_ref).toBe(backReference);
  expect(uploads).toBe(2);
  expect(creations).toBe(1);
  await expect(page.locator("#jumperBtn, #demoWarn, #demoLapse")).toHaveCount(0);
});

test("removing the optional back photo persists across refresh without another upload", async ({page}) => {
  let uploads = 0;
  page.on("request", request => {
    if (request.method() === "POST" && /\/workspaces\/[^/]+\/photos$/.test(request.url())) uploads++;
  });
  await enterTicket(page);
  await page.locator("#actContinueTicket").click();
  await openDetails(page);
  await page.locator("#liveName").fill("Synthetic Customer");
  await page.locator("#liveTicketPhoto").setInputFiles({name:"synthetic.png",mimeType:"image/png",buffer:syntheticPng(255)});
  await page.locator("#liveTicketBackPhoto").setInputFiles({name:"synthetic-back.png",mimeType:"image/png",buffer:syntheticPng(0)});
  await expect.poll(async () => Boolean((await saved(page))?.intake_form?.ticket_photo_ref)).toBe(true);
  await expect.poll(async () => Boolean((await saved(page))?.intake_form?.ticket_back_photo_ref)).toBe(true);
  const frontReference = (await saved(page)).intake_form.ticket_photo_ref;
  await page.getByRole("button", {name:"Remove back of ticket"}).click();
  await expect.poll(async () => Object.hasOwn((await saved(page))?.intake_form ?? {}, "ticket_back_photo_ref")).toBe(false);
  await page.reload();
  await expect(page.locator("#liveName")).toHaveValue("Synthetic Customer");
  await expect(page.getByText("Saved for this appeal; choose a file only to replace it.")).toBeVisible();
  await expect(page.getByText("Back of ticket saved for this appeal.")).toHaveCount(0);
  expect((await saved(page)).intake_form.ticket_photo_ref).toBe(frontReference);
  expect((await saved(page)).intake_form).not.toHaveProperty("ticket_back_photo_ref");
  expect(uploads).toBe(2);
});

test("a stale autosave preserves visible local entries and the other tab's saved form", async ({ page }) => {
  await enterTicket(page);
  const view = await saved(page);
  const otherText = "Saved in another tab";
  const response = await page.evaluate(async ({workspace,revision,form,text}) => {
    const csrf = document.cookie.split("; ").find(item=>item.startsWith("__Host-pb-csrf="))?.split("=")[1] ?? "";
    return (await fetch(`/api/workspaces/${workspace}/intake-form`,{method:"PUT",headers:{"Content-Type":"application/json","X-CSRF-Token":csrf},body:JSON.stringify({expected_revision:revision,form:{...form,values:{...form.values,story:text}}})})).status;
  }, {workspace:view.workspace_id,revision:view.draft_revision ?? 1,form:view.intake_form,text:otherText});
  expect(response).toBe(200);
  await page.locator("#plateIn").fill("LOCAL456");
  await expect(page.getByText(/Save paused/)).toBeVisible();
  await expect(page.locator("#plateIn")).toHaveValue("LOCAL456");
  expect((await saved(page)).intake_form.values.story).toBe(otherText);
});

test("an uncertain autosave is replayed before newer typing is saved", async ({page}) => {
  await enterTicket(page);
  const workspace = (await saved(page)).workspace_id;
  let lost = false;
  await page.route("**/api/workspaces/*/intake-form", async route => {
    if (lost || route.request().postDataJSON().form.values.plate !== "UNCERTAIN1") {await route.continue(); return;}
    const response = await forward(route);
    expect(response.status()).toBe(200);
    lost = true;
    await route.abort("connectionreset");
  });
  await page.locator("#plateIn").fill("UNCERTAIN1");
  await expect(page.getByText(/Save paused/)).toBeVisible();
  expect((await saved(page)).intake_form.values.plate).toBe("UNCERTAIN1");
  await page.locator("#plateIn").fill("NEWER2");
  await expect.poll(async () => (await saved(page))?.intake_form?.values?.plate).toBe("NEWER2");
  await page.reload();
  await expect(page.locator("#plateIn")).toHaveValue("NEWER2");
  expect((await saved(page)).workspace_id).toBe(workspace);
});

async function completePrivateDetails(page: Page) {
  for (const [selector,value] of Object.entries({
    liveIssuedCity:"San Francisco",liveIssuedState:"CA",liveName:"Synthetic Customer",livePhone:"415-555-0100",
    liveEmail:"multi@example.test",liveLine1:"123 Example Street",liveCity:"San Francisco",liveState:"CA",liveZip:"94103",
  })) await page.locator(`#${selector}`).fill(value);
  await page.getByRole("radio",{name:"A private parking lot or company"}).check();
  await page.locator("#liveTicketPhoto").setInputFiles({name:"ticket.png",mimeType:"image/png",buffer:syntheticPng(255)});
  await expect.poll(async () => Boolean((await saved(page))?.intake_form?.ticket_photo_ref)).toBe(true);
}

const supportingPhoto = (name: string, value: number) => ({name,mimeType:"image/png",buffer:syntheticPng(value)});

test("three chosen photos keep removal and ordering across reload and intake without reupload", async ({page}) => {
  let uploads = 0;
  page.on("request", request => {
    if (request.method() === "POST" && /\/workspaces\/[^/]+\/photos$/.test(request.url())) uploads++;
  });
  await enterTicket(page);
  await page.locator("#actContinueTicket").click();
  await openDetails(page);
  await completePrivateDetails(page);
  await page.locator("#liveEvidencePhoto").setInputFiles([
    supportingPhoto("one.png",40),supportingPhoto("two.png",80),supportingPhoto("three.png",120),
  ]);
  await expect.poll(async () => (await saved(page))?.intake_form?.evidence_photo_refs?.length).toBe(3);
  const original: string[] = (await saved(page)).intake_form.evidence_photo_refs;
  expect(new Set(original).size).toBe(3);
  await page.getByRole("button",{name:"Move supporting photo 3 up",exact:true}).click();
  await expect.poll(async () => (await saved(page))?.intake_form?.evidence_photo_refs).toEqual([original[0],original[2],original[1]]);
  await page.getByRole("button",{name:"Remove supporting photo 1",exact:true}).click();
  await expect.poll(async () => (await saved(page))?.intake_form?.evidence_photo_refs).toEqual([original[2],original[1]]);
  await page.locator("#liveEvidencePhoto").setInputFiles(supportingPhoto("four.png",160));
  await expect.poll(async () => (await saved(page))?.intake_form?.evidence_photo_refs?.length).toBe(3);
  const chosen: string[] = (await saved(page)).intake_form.evidence_photo_refs;
  expect(chosen.slice(0,2)).toEqual([original[2],original[1]]);
  expect(chosen).not.toContain(original[0]);
  await page.reload();
  await expect(page.locator("#liveEvidenceList [data-evidence-id]")).toHaveCount(3);
  expect((await saved(page)).intake_form.evidence_photo_refs).toEqual(chosen);
  await page.locator("#liveRentalPaid").selectOption("not-rental");
  await page.locator("#liveSubmit").click();
  await expect(page.locator("#liveStopMessage")).toBeVisible();
  expect((await saved(page)).intake.photo_refs).toEqual(chosen);
  expect(uploads).toBe(5);
});

test("removal and reordering during delayed uploads survive late completions", async ({page}) => {
  await enterTicket(page);
  await page.locator("#actContinueTicket").click();
  await openDetails(page);
  const pending: {route: Route; response: Awaited<ReturnType<Route["fetch"]>>}[] = [];
  await page.route("**/api/workspaces/*/photos", async route => {
    const response = await forward(route);
    expect(response.status()).toBe(201);
    pending.push({route,response});
  });
  await page.locator("#liveEvidencePhoto").setInputFiles([
    supportingPhoto("one.png",40),supportingPhoto("two.png",80),supportingPhoto("three.png",120),
  ]);
  await expect.poll(() => pending.length).toBe(3);
  await expect(page.locator("#liveSubmit")).toBeDisabled();
  await page.getByRole("button",{name:"Remove supporting photo 2",exact:true}).click();
  await page.getByRole("button",{name:"Move supporting photo 2 up",exact:true}).click();
  await expect(page.locator("#liveEvidenceList [data-evidence-id]")).toHaveCount(2);
  const uploaded = await Promise.all(pending.map(item => item.response.json()));
  const requestBodies = pending.map(item => item.route.request().postDataJSON());
  const refFor = (value: number) => uploaded[requestBodies.findIndex(body => body.data_base64 === syntheticPng(value).toString("base64"))].photo_ref as string;
  for (const item of [...pending].reverse()) await item.route.fulfill({response:item.response});
  await page.unroute("**/api/workspaces/*/photos");
  await expect.poll(async () => (await saved(page))?.intake_form?.evidence_photo_refs).toEqual([refFor(120),refFor(40)]);
  await page.reload();
  await expect(page.locator("#liveEvidenceList [data-evidence-id]")).toHaveCount(2);
  expect((await saved(page)).intake_form.evidence_photo_refs).toEqual([refFor(120),refFor(40)]);
  expect((await saved(page)).intake_form.evidence_photo_refs).not.toContain(refFor(80));
});

test("a legacy saved photo restores and migrates without losing the selection", async ({page}) => {
  await enterTicket(page);
  await page.locator("#actContinueTicket").click();
  await openDetails(page);
  await completePrivateDetails(page);
  const ref = await page.evaluate(async data => {
    const csrf = document.cookie.split("; ").find(item=>item.startsWith("__Host-pb-csrf="))?.split("=")[1] ?? "";
    const view = await (await fetch("/api/auth/appeal")).json();
    const upload = await fetch(`/api/workspaces/${view.workspace_id}/photos`,{method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":csrf},body:JSON.stringify({content_type:"image/png",data_base64:data})});
    if (!upload.ok) throw new Error("synthetic upload failed");
    const ref = (await upload.json()).photo_ref;
    const {evidence_photo_refs: _old, ...form} = view.intake_form;
    void _old;
    const response = await fetch(`/api/workspaces/${view.workspace_id}/intake-form`,{method:"PUT",headers:{"Content-Type":"application/json","X-CSRF-Token":csrf},body:JSON.stringify({expected_revision:view.draft_revision,form:{...form,evidence_photo_ref:ref}})});
    if (!response.ok) throw new Error("synthetic legacy save failed");
    return ref as string;
  }, syntheticPng(80).toString("base64"));
  await page.reload();
  await expect(page.locator("#liveEvidenceList [data-evidence-id]")).toHaveCount(1);
  await expect.poll(async () => (await saved(page))?.intake_form?.evidence_photo_refs).toEqual([ref]);
  await page.getByRole("button",{name:"Remove supporting photo 1",exact:true}).click();
  await expect.poll(async () => (await saved(page))?.intake_form?.evidence_photo_refs).toEqual([]);
  await page.reload();
  await expect(page.locator("#liveEvidenceList [data-evidence-id]")).toHaveCount(0);
});

test("retrying a failed photo upload saves its reference before the original intake is replayed", async ({page}) => {
  let uploads = 0;
  await enterTicket(page);
  await page.locator("#actContinueTicket").click();
  await openDetails(page);
  await completePrivateDetails(page);
  await page.route("**/api/workspaces/*/photos", async route => {
    uploads++;
    if (uploads === 1) await route.abort("connectionreset");
    else await route.fulfill({response:await forward(route)});
  });
  await page.locator("#liveEvidencePhoto").setInputFiles(supportingPhoto("retry.png",80));
  await expect(page.locator("#liveDetails [role=alert]")).toBeVisible();
  await expect(page.locator("#liveSubmit")).toBeEnabled();
  let firstCase: string | null = null;
  await page.route("**/api/workspaces/*/intake", async route => {
    const refs = route.request().postDataJSON().photo_refs;
    expect(refs).toHaveLength(1);
    expect((await saved(page)).intake_form.evidence_photo_refs).toEqual(refs);
    const response = await forward(route);
    expect(response.status()).toBe(200);
    const id = (await response.json()).case_id;
    if (!firstCase) {firstCase=id;await route.abort("connectionreset");}
    else {expect(id).toBe(firstCase);await route.fulfill({response});}
  });
  await page.locator("#liveRentalPaid").selectOption("not-rental");
  await page.locator("#liveSubmit").click();
  await expect.poll(() => firstCase).not.toBeNull();
  await expect(page.locator("#liveSubmit")).toBeEnabled();
  await page.locator("#liveRentalPaid").selectOption("not-rental");
  await page.locator("#liveSubmit").click();
  await expect(page.locator("#liveStopMessage")).toBeVisible();
  expect(uploads).toBe(2);
  expect((await saved(page)).case_id).toBe(firstCase);
});
