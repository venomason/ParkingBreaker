import { expect, test, type Page, type Route } from "./synthetic-fixtures";
import { createHash } from "node:crypto";
import { deflateSync } from "node:zlib";
const base = process.env.SECURE_BASE;
const privateApi = process.env.INGRESS_API_BASE;
const worker = process.env.INGRESS_WORKER_SECRET;
test.use({ ignoreHTTPSErrors: true });
test.skip(!base || !privateApi || !worker, "requires isolated HTTPS and private fake worker fixtures");
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

async function call(page: Page, method: string, path: string, body?: unknown) {
  return page.evaluate(async ({method,path,body}) => {
    const csrf = document.cookie.split("; ").find(item=>item.startsWith("__Host-pb-csrf="))?.split("=")[1] ?? "";
    const response = await fetch(`/api${path}`,{method,headers:{"Content-Type":"application/json","X-CSRF-Token":csrf},body:body == null ? undefined : JSON.stringify(body)});
    if (!response.ok) throw new Error(`synthetic ${method} ${path}: ${response.status}`);
    return response.json();
  }, {method,path,body});
}

async function seed(page: Page) {
  await page.goto(base!);
  await call(page,"POST","/auth/appeals",{});
  const view = await call(page,"POST","/workspaces",{});
  const photo = await call(page,"POST",`/workspaces/${view.workspace_id}/photos`,{
    content_type:"image/png",data_base64:"iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAFklEQVR4nGP8//8/AwMDEwMDAwMDAwAkBgMB/DXemwAAAABJRU5ErkJggg==",
  });
  const receipt = await call(page,"POST",`/workspaces/${view.workspace_id}/intake`,{
    ticket:{citation_number:"SYN-COPIES-1",license_plate:"SYN123",license_plate_state:"CA",issuer_hint:"San Francisco Municipal Transportation Agency",issued_city:"San Francisco",issued_state:"CA",violation_code:"Meter expired",violation_description:"Meter expired",issue_date:new Date(Date.now()-3*86400000).toISOString().slice(0,10)},
    contact:{name:"Synthetic Customer",phone:"415-555-0100",email:"letter@example.test",mailing_address:{line1:"123 Example Street",city:"San Francisco",state:"CA",zip_code:"94103"}},
    customer_position:{statement:"The sign was covered. The kiosk was blank."},citation_photo_ref:photo.photo_ref,
    rental_company_paid:"NOT_RENTAL",
  });
  const id = receipt.case_id;
  await call(page,"PUT",`/cases/${id}/tier`,{tier:"CERTIFIED"});
  await call(page,"POST",`/cases/${id}/authorization`,{terms_accepted:true,payment_method_id:"pm_card_visa"});
  for (const step of ["research"]) {
    const response = await page.request.post(`${privateApi}/internal/cases/${id}/step`,{headers:{Authorization:`Bearer ${worker}`},data:{step_id:step}});
    expect(response.status(),await response.text()).toBe(200);
  }
  await page.reload();
  await expect(page.locator("#requiredCopies")).toBeVisible();
  return id;
}


const file = (value: number) => ({name:`synthetic-copy-${value}.png`,mimeType:"image/png",buffer:syntheticPng(value)});
const photoRef = (value: number) => `photo_${createHash("sha256").update(syntheticPng(value)).digest("hex")}`;
const confirmation = (page: Page) => page.getByRole("checkbox",{name:"I checked every page. This is the complete, readable copy of registration."});
async function savedCopies(page: Page, id: string) {
  return (await call(page,"GET",`/cases/${id}/status`)).required_copies;
}
async function saveCopies(page: Page, id: string, refs: string[]) {
  await confirmation(page).check();
  await page.getByRole("button",{name:"Save required copies",exact:true}).click();
  await expect.poll(async ()=>(await savedCopies(page,id)).items[0].page_refs).toEqual(refs);
  await expect(page.locator("#liveDraftBtn")).toBeEnabled();
}
async function visibleCopies(page: Page, count: number) {
  const images = page.locator("#requiredCopies img");
  await expect(images).toHaveCount(count);
  for (const image of await images.all()) {
    await expect(image).toBeVisible();
    await expect.poll(()=>image.evaluate(node=>(node as HTMLImageElement).naturalWidth)).toBeGreaterThan(0);
  }
}

test("required copies persist, reorder and lock into the adopted packet with private access",async ({page,browser},testInfo)=>{
  const id = await seed(page);
  await expect(page.locator("#liveDraftBtn")).toBeDisabled();
  const before = await savedCopies(page,id);
  expect(before.ready).toBe(false);
  expect(before.items[0].title).toBe("copy of registration");
  expect(before.items[0].official_quote).toBeTruthy();
  expect(before.items[0].source_urls.length).toBeGreaterThan(0);
  await expect(page.locator("#requiredCopies blockquote")).toHaveText(before.items[0].official_quote);
  // The private worker is real; missing copies must leave the draft inaccessible.
  const premature = await page.request.post(`${privateApi}/internal/cases/${id}/step`,{headers:{Authorization:`Bearer ${worker}`},data:{step_id:"draft"}});
  expect(premature.status(),await premature.text()).toBe(200);
  expect((await premature.json()).state).toBe("DRAFTING");
  expect(await page.evaluate(async id => (await fetch(`/api/cases/${id}/draft`)).status,id)).toBe(409);
  await page.getByLabel("Add pages to copy of registration",{exact:true}).setInputFiles([file(0),file(255)]);
  await visibleCopies(page,2);
  await expect(confirmation(page)).not.toBeChecked();
  await expect(page.getByRole("button",{name:"Save required copies",exact:true})).toBeDisabled();
  await expect(page.locator("#liveDraftBtn")).toBeDisabled();
  await saveCopies(page,id,[photoRef(0),photoRef(255)]);
  await page.reload();
  await visibleCopies(page,2);
  const urls = await page.locator("#requiredCopies img").evaluateAll(nodes=>nodes.map(node=>(node as HTMLImageElement).src));
  expect(urls.every(url=>url.includes(`/cases/${id}/required-copies/`) && !url.includes("photo_"))).toBe(true);
  const ownImage = await page.evaluate(async url => {
    const response = await fetch(url);
    const bytes = new Uint8Array(await response.arrayBuffer());
    return {status: response.status, cacheControl: response.headers.get("cache-control"), bytes: Array.from(bytes)};
  },urls[0]);
  expect(ownImage.status).toBe(200);
  expect(ownImage.cacheControl).toBe("no-store");
  expect(createHash("sha256").update(Buffer.from(ownImage.bytes)).digest("hex")).toBe(photoRef(0).slice(6));
  const originalViewport = page.viewportSize();
  for (const [name,width,height] of [["mobile",375,812],["desktop",1440,1000]] as const) {
    await page.setViewportSize({width,height});
    const screenshotPath = process.env.SHOTS ? `${process.env.SHOTS}/required-copies-${name}.png` : testInfo.outputPath(`required-copies-${name}.png`);
    await page.screenshot({path:screenshotPath,fullPage:true});
    await testInfo.attach(`required-copies-${name}`,{path:screenshotPath,contentType:"image/png"});
  }
  if(originalViewport) await page.setViewportSize(originalViewport);
  const stranger = await browser.newContext({ignoreHTTPSErrors:true});
  try {
    // Authentication itself is required before the non-enumerating ownership check.
    const foreign = await stranger.newPage();
    await foreign.goto(base!);
    expect(await foreign.evaluate(async url => (await fetch(url,{credentials:"omit"})).status,urls[0])).toBe(401);
    await call(foreign,"POST","/auth/appeals",{});
    expect(await foreign.evaluate(async url => (await fetch(url)).status,urls[0])).toBe(404);
  } finally {await stranger.close();}
  await page.getByRole("button",{name:"Move copy of registration page 2 up",exact:true}).click();
  await expect(confirmation(page)).not.toBeChecked();
  await expect(page.locator("#liveDraftBtn")).toBeDisabled();
  await saveCopies(page,id,[photoRef(255),photoRef(0)]);
  await page.getByRole("button",{name:"Remove page 1",exact:true}).click();
  await saveCopies(page,id,[photoRef(0)]);
  await page.getByLabel("Replace all pages of copy of registration",{exact:true}).setInputFiles([file(255),file(0)]);
  await visibleCopies(page,2);
  await saveCopies(page,id,[photoRef(255),photoRef(0)]);
  const frozen = await savedCopies(page,id);
  await page.locator("#liveDraftBtn").click();
  await expect(page.locator("#liveLetterText")).toBeVisible();
  await expect(page.locator("#liveLetterText")).toHaveValue(/copy of registration/);
  const rejected = await page.evaluate(async ({id,view})=>{
    const csrf=document.cookie.split("; ").find(item=>item.startsWith("__Host-pb-csrf="))?.split("=")[1]??"";
    const response=await fetch(`/api/cases/${id}/required-copies`,{method:"PUT",headers:{"Content-Type":"application/json","X-CSRF-Token":csrf},body:JSON.stringify({research_sha256:view.research_sha256,expected_revision:view.revision,selections:[]})});
    return {status:response.status,body:await response.json()};
  },{id,view:frozen});
  expect(rejected.status).toBe(409);
  expect(rejected.body.detail).toBe("required_copies_locked");
  await page.locator("#liveAdopt").click();
  const preview = page.waitForResponse(response=>response.url().endsWith("/packet-preview") && response.status()===200);
  await page.locator("#livePreview").click();
  expect((await (await preview).json()).page_count).toBeGreaterThanOrEqual(4);
  await expect(page.locator("#liveOpenPacket")).toHaveAttribute("href", /^blob:/);
  await expect(page.locator("#livePacketPage")).toBeVisible({ timeout: 15000 });
  expect((await savedCopies(page,id)).editable).toBe(false);
});

test("a lost required-copy save response reconciles stored pages before drafting",async ({page})=>{
  const id = await seed(page);
  await page.getByLabel("Add pages to copy of registration",{exact:true}).setInputFiles([file(0),file(255)]);
  await visibleCopies(page,2);
  await confirmation(page).check();
  let lost=false;
  await page.route("**/api/cases/*/required-copies",async route=>{
    if(route.request().method()!=="PUT" || lost){await route.continue();return;}
    const response=await forward(route);
    expect(response.status()).toBe(200); lost=true;
    await route.abort("connectionreset");
  });
  await page.getByRole("button",{name:"Save required copies",exact:true}).click();
  await expect(page.locator("#requiredCopies [role=alert]")).toContainText("couldn’t confirm the save");
  await expect(page.locator("#liveDraftBtn")).toBeDisabled();
  await expect(page.getByLabel("Add pages to copy of registration",{exact:true})).toBeDisabled();
  await page.getByRole("button",{name:"Check saved copies and retry",exact:true}).click();
  await expect(page.locator("#liveDraftBtn")).toBeEnabled();
  expect((await savedCopies(page,id)).items[0].page_refs).toEqual([photoRef(0),photoRef(255)]);
  await page.reload();
  await visibleCopies(page,2);
  await expect(page.locator("#liveDraftBtn")).toBeEnabled();
});

test("a delayed page upload cannot restore an obsolete copy editor after navigation",async ({page})=>{
  const id=await seed(page);
  let release!:()=>void;
  const barrier=new Promise<void>(resolve=>{release=resolve;});
  let began!:()=>void;
  const started=new Promise<void>(resolve=>{began=resolve;});
  let finished!:()=>void;
  const completed=new Promise<void>(resolve=>{finished=resolve;});
  await page.route("**/api/workspaces/*/photos",async route=>{
    const response=await forward(route); began();
    await barrier;
    try {await route.fulfill({response});} catch {/* Navigation may already have cancelled the request. */}
    finally {finished();}
  });
  try {
    await page.getByLabel("Add pages to copy of registration",{exact:true}).setInputFiles(file(0));
    await started;
    await page.goto(`${base}/?obsolete-copy-upload=1`);
    await expect(page.locator("#requiredCopies")).toBeVisible();
    release(); await completed;
    await expect(page.locator("#requiredCopies img")).toHaveCount(0);
    await expect(page.locator("#liveDraftBtn")).toBeDisabled();
    expect((await savedCopies(page,id)).items[0].page_refs).toEqual([]);
  } finally {release();}
});

test("a same-revision drafting lock reconciles a rejected copy edit without an endless retry", async ({page})=>{
  const id=await seed(page);
  await page.getByLabel("Add pages to copy of registration",{exact:true}).setInputFiles([file(0),file(255)]);
  await visibleCopies(page,2);
  await saveCopies(page,id,[photoRef(0),photoRef(255)]);
  const saved=await savedCopies(page,id);
  await page.getByRole("button",{name:"Move copy of registration page 2 up",exact:true}).click();
  await confirmation(page).check();
  const drafted=await page.request.post(`${privateApi}/internal/cases/${id}/step`,{headers:{Authorization:`Bearer ${worker}`},data:{step_id:"draft"}});
  expect(drafted.status(),await drafted.text()).toBe(200);
  expect((await drafted.json()).state).toBe("AWAITING_ADOPTION");
  const locked=await savedCopies(page,id);
  expect(locked.revision).toBe(saved.revision);
  expect(locked.editable).toBe(false);
  const rejected=page.waitForResponse(response=>response.url().endsWith("/required-copies") && response.request().method()==="PUT" && response.status()===409);
  await page.getByRole("button",{name:"Save required copies",exact:true}).click();
  await rejected;
  await expect(page.getByRole("button",{name:"Check saved copies and retry",exact:true})).toHaveCount(0);
  const letter=page.locator("#liveLetterText");
  const fixed=page.getByText("These copies are fixed for this letter.",{exact:true});
  await expect(letter.or(fixed)).toBeVisible();
  const final=await savedCopies(page,id);
  expect(final.items[0].page_refs).toEqual([photoRef(0),photoRef(255)]);
  expect(final.editable).toBe(false);
  if(await fixed.isVisible()) {
    await expect(page.getByRole("status")).toContainText("Your local changes were not saved");
    await expect(page.getByLabel("Add pages to copy of registration",{exact:true})).toHaveCount(0);
  }
});
