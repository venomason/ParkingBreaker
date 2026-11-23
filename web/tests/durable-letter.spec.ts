import { expect, test, type Page } from "./synthetic-fixtures";
import { writeFileSync, unlinkSync } from "node:fs";
import { createHash } from "node:crypto";

const base = process.env.SECURE_BASE;
const privateApi = process.env.INGRESS_API_BASE;
const worker = process.env.INGRESS_WORKER_SECRET;
test.use({ignoreHTTPSErrors:true});
test.skip(!base || !privateApi || !worker, "requires isolated HTTPS and private fake worker fixtures");

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
    ticket:{citation_number:"SYN-LETTER-1",license_plate:"SYN123",license_plate_state:"CA",issuer_hint:"San Francisco Municipal Transportation Agency",issued_city:"San Francisco",issued_state:"CA",violation_code:"Meter expired",violation_description:"Meter expired",issue_date:new Date(Date.now()-3*86400000).toISOString().slice(0,10)},
    contact:{name:"Synthetic Customer",phone:"415-555-0100",email:"letter@example.test",mailing_address:{line1:"123 Example Street",city:"San Francisco",state:"CA",zip_code:"94103"}},
    customer_position:{statement:"The sign was covered. The kiosk was blank."},citation_photo_ref:photo.photo_ref,
    rental_company_paid:"NOT_RENTAL",
  });
  const id = receipt.case_id;
  await call(page,"PUT",`/cases/${id}/tier`,{tier:"CERTIFIED"});
  await call(page,"POST",`/cases/${id}/authorization`,{terms_accepted:true,payment_method_id:"pm_card_visa"});
  for (const step of ["research","draft"]) {
    const response = await page.request.post(`${privateApi}/internal/cases/${id}/step`,{headers:{Authorization:`Bearer ${worker}`},data:{step_id:step}});
    expect(response.status(),await response.text()).toBe(200);
  }
  await page.reload();
  await expect(page.locator("#liveLetterText")).toBeVisible();
  return id;
}

async function previewAndInspect(page: Page) {
  await expect(page.locator("#livePreview")).toBeEnabled();
  await page.locator("#livePreview").click();
  await expect(page.locator("#liveOpenPacket")).toHaveAttribute("href", /^blob:/);
  await expect(page.locator("#livePacketPage")).toBeVisible({ timeout: 15000 });
  await expect(page.locator("#liveApprove")).toBeDisabled();
  await page.locator("#livePacketReviewed").check();
  await expect(page.locator("#liveApprove")).toBeEnabled();
}

async function sign(page: Page) {
  const pad = page.locator("#liveSignature");
  await pad.scrollIntoViewIfNeeded();
  const box = await pad.boundingBox();
  if (!box) throw new Error("signature pad is missing");
  await page.mouse.move(box.x + 30, box.y + 60);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width - 30, box.y + 20, { steps: 8 });
  await page.mouse.up();
}

test("letter edits save exactly, survive refresh, and replay an uncertain save before newer edits", async ({page}) => {
  const id = await seed(page);
  const original = await page.locator("#liveLetterText").inputValue();
  const changed = original+"\nMy additional exact words: the kiosk was blank.";
  await page.locator("#liveLetterText").fill(changed);
  await expect.poll(async ()=>(await call(page,"GET",`/cases/${id}/draft`)).text).toBe(changed);
  await page.reload();
  await expect(page.locator("#liveLetterText")).toHaveValue(changed);
  let lost = false;
  await page.route("**/api/cases/*/draft",async route=>{
    if (route.request().method()!=="PUT" || lost) {await route.continue();return;}
    const target = process.env.SECURE_TRANSPORT_BASE;
    const response = await route.fetch(target ? {url:new URL(new URL(route.request().url()).pathname,target).href} : {});
    expect(response.status()).toBe(200);
    lost = true;
    await route.abort("connectionreset");
  });
  const uncertain = changed+"\nMy first update.";
  await page.locator("#liveLetterText").fill(uncertain);
  await expect(page.locator("#s6 [role=alert]")).toContainText(/couldn[’']t (?:reach|confirm)/);
  const latest = uncertain+"\nMy newer update.";
  await page.locator("#liveLetterText").fill(latest);
  await page.locator("#liveSaveEdit").click();
  await expect.poll(async ()=>(await call(page,"GET",`/cases/${id}/draft`)).text).toBe(latest);
  await page.reload();
  await expect(page.locator("#liveLetterText")).toHaveValue(latest);
  await expect(page.locator("#liveApprove")).toHaveCount(0);
});

test("unsaved edits cannot approve a previous adopted letter after navigating away and back", async ({page}) => {
  await seed(page);
  await page.locator("#liveAdopt").click();
  await previewAndInspect(page);
  await page.locator("#liveEditAdopted").click();
  await expect(page.locator("#livePacketPreview")).toHaveCount(0);
  const changed = (await page.locator("#liveLetterText").inputValue())+"\nExact unsaved addition.";
  // A disconnected save leaves the older adopted text on the server.
  await page.route("**/api/cases/*/draft",async route=>{
    if (route.request().method()==="PUT") await route.abort("connectionreset"); else await route.continue();
  });
  await page.locator("#liveLetterText").fill(changed);
  await page.locator("#liveSaveEdit").click();
  await expect(page.locator("#s6 [role=alert]")).toContainText(/couldn[’']t reach/);
  await page.getByRole("button",{name:/Reopen Official research/}).click();
  await expect(page.getByRole("textbox",{name:"Unsaved letter edits"})).toHaveValue(changed);
  await page.locator("#liveDraftBtn").click();
  await expect(page.locator("#liveLetterText")).toHaveValue(changed);
  await expect(page.locator("#liveApprove")).toHaveCount(0);
  await expect(page.locator("#liveAdopt")).toHaveCount(0);
});

test("server expiry stops approval and retains the saved letter without a prototype restart", async ({page}) => {
  const clock = process.env.SYNTHETIC_CLOCK_PATH;
  test.skip(!clock,"requires the isolated fake harness clock file");
  const id = await seed(page);
  const text = await page.locator("#liveLetterText").inputValue();
  await page.locator("#liveAdopt").click();
  await previewAndInspect(page);
  try {
    // Advance past the actual server window in bounded harness-clock steps.
    const current = await call(page,"GET",`/cases/${id}/status`);
    let advance = current.seconds_remaining + 5;
    while (advance > 0) {
      const seconds = Math.min(advance, 3600);
      writeFileSync(clock!, String(seconds));
      await call(page,"GET",`/cases/${id}/status`);
      advance -= seconds;
    }
    await expect(page.locator("#liveStopMessage")).toBeVisible({timeout:12000});
    await expect(page.locator("#liveApprove, #demoWarn, #demoLapse, #jumperBtn")).toHaveCount(0);
    await expect(page.getByRole("button",{name:"Start a new window"})).toHaveCount(0);
    const status = await call(page,"GET",`/cases/${id}/status`);
    expect(status.state).toBe("STOPPED_NO_CHARGE");
    expect(status.payment_state).not.toBe("CAPTURED");
    expect((await call(page,"GET",`/cases/${id}/draft`)).text).toBe(text);
    await page.reload();
    await expect(page.locator("#liveStopMessage")).toBeVisible();
  } finally {unlinkSync(clock!);}
});

test("packet inspection shows exact bytes and envelope before any approval request", async ({page}) => {
  const id = await seed(page);
  let approvals = 0;
  page.on("request", request => {
    if (request.method() === "POST" && request.url().endsWith("/approval")) approvals++;
  });
  await page.locator("#liveAdopt").click();
  await expect(page.locator("#livePreview")).toBeEnabled();
  await expect(page.locator("#liveApprove")).toHaveCount(0);
  const response = page.waitForResponse(response => response.url().endsWith("/packet-preview") && response.status() === 200);
  await page.locator("#livePreview").click();
  const packet = await (await response).json();
  await expect(page.locator("#liveOpenPacket")).toHaveAttribute("href", /^blob:/);
  await expect(page.locator("#livePacketPage")).toBeVisible({ timeout: 15000 });
  await expect(page.locator("#liveApprove")).toBeDisabled();
  expect(approvals).toBe(0);
  const encoded = await page.locator("#liveOpenPacket").evaluate(async link => {
    const bytes = new Uint8Array(await (await fetch((link as HTMLAnchorElement).href)).arrayBuffer());
    return btoa(Array.from(bytes, byte => String.fromCharCode(byte)).join(""));
  });
  expect(encoded).toBe(packet.pdf_base64);
  expect(createHash("sha256").update(Buffer.from(encoded,"base64")).digest("hex")).toBe(packet.packet_sha256);
  for (const line of [...packet.mail_to, ...packet.return_address]) {
    await expect(page.locator("#livePacketPreview")).toContainText(line);
  }
  await expect(page.locator("#livePacketPreview")).toContainText(`${packet.page_count} page`);
  expect((await call(page,"GET",`/cases/${id}/status`)).state).toBe("AWAITING_ADOPTION");
  await page.locator("#livePacketReviewed").check();
  await expect(page.locator("#liveApprove")).toBeEnabled();
  expect(approvals).toBe(0);
});

test("a mismatched preview digest cannot expose or authorize unseen bytes", async ({page}) => {
  await seed(page);
  await page.locator("#liveAdopt").click();
  let approvals = 0;
  page.on("request", request => {
    if (request.method() === "POST" && request.url().endsWith("/approval")) approvals++;
  });
  await page.route("**/api/cases/*/packet-preview", async route => {
    const target = process.env.SECURE_TRANSPORT_BASE;
    const response = await route.fetch(target ? {url:new URL(new URL(route.request().url()).pathname,target).href} : {});
    expect(response.status()).toBe(200);
    const packet = await response.json();
    await route.fulfill({response,json:{...packet,packet_sha256:"0".repeat(64)}});
  });
  await page.locator("#livePreview").click();
  await expect(page.locator("#s6 [role=alert]")).toContainText("packet or envelope changed");
  await expect(page.locator("#livePacketPreview, #liveApprove")).toHaveCount(0);
  expect(approvals).toBe(0);
  await page.unroute("**/api/cases/*/packet-preview");
  await previewAndInspect(page);
});

test("signature changes and editing invalidate the inspected packet", async ({page}) => {
  await seed(page);
  await page.locator("#liveAdopt").click();
  await previewAndInspect(page);
  await sign(page);
  await expect(page.locator("#livePacketPreview, #liveApprove")).toHaveCount(0);
  await previewAndInspect(page);
  await page.getByRole("button",{name:"Clear signature",exact:true}).click();
  await expect(page.locator("#livePacketPreview, #liveApprove")).toHaveCount(0);
  await previewAndInspect(page);
  await page.locator("#liveEditAdopted").click();
  await expect(page.locator("#livePacketPreview, #liveApprove")).toHaveCount(0);
  await page.locator("#liveCancelEdit").click();
  await expect(page.locator("#livePreview")).toBeEnabled();
  await expect(page.locator("#livePacketReviewed, #liveApprove")).toHaveCount(0);
});

test("lost signed approval response retries the original inspected packet without reupload", async ({page}) => {
  const id = await seed(page);
  await page.locator("#liveAdopt").click();
  await expect(page.locator("#livePreview")).toBeEnabled();
  await sign(page);
  let uploads = 0;
  let previews = 0;
  const requests: string[] = [];
  page.on("request", request => {
    if (request.method() !== "POST") return;
    if (/\/workspaces\/[^/]+\/photos$/.test(request.url())) uploads++;
    if (request.url().endsWith("/packet-preview")) previews++;
  });
  const previewResponse = page.waitForResponse(response => response.url().endsWith("/packet-preview") && response.status() === 200);
  await previewAndInspect(page);
  const inspected = await (await previewResponse).json();
  expect(uploads).toBe(1);
  let lost = false;
  await page.route("**/api/cases/*/approval", async route => {
    requests.push(route.request().postData() ?? "");
    const target = process.env.SECURE_TRANSPORT_BASE;
    const response = await route.fetch(target ? {url:new URL(new URL(route.request().url()).pathname,target).href} : {});
    expect(response.status()).toBe(200);
    if (!lost) {
      lost = true;
      await route.abort("connectionreset");
    } else await route.fulfill({response});
  });
  await page.locator("#liveApprove").click();
  await expect(page.locator("#s6 [role=alert]")).toContainText("Approval may have been saved");
  await expect(page.locator("#liveApprove")).toHaveText("Retry this packet approval →");
  await expect(page.locator("#liveEditAdopted")).toBeDisabled();
  await expect(page.getByRole("button",{name:"Clear signature",exact:true})).toBeDisabled();
  expect((await call(page,"GET",`/cases/${id}/status`)).state).toBe("FROZEN");
  const retryResponse = page.waitForResponse(response => response.url().endsWith("/approval") && response.status() === 200);
  await page.locator("#liveApprove").click();
  await retryResponse;
  expect(requests).toHaveLength(2);
  expect(requests[1]).toBe(requests[0]);
  const approved = JSON.parse(requests[0]);
  expect(approved.packet_sha256).toBe(inspected.packet_sha256);
  expect(approved.envelope_sha256).toBe(inspected.envelope_sha256);
  expect(approved.signature_ref).toBeTruthy();
  expect(uploads).toBe(1);
  expect(previews).toBe(1);
});


test("letter studio warns while editing and displays packet preview conflicts", async ({page}) => {
  await seed(page);
  const letter = page.locator("#liveLetterText");
  const original = await letter.inputValue();
  await expect(page.locator("#live-letter-fit")).toBeEmpty();

  const long = original + "\n" + "The sign was covered.\n".repeat(100);
  await letter.fill(long);
  await expect(page.locator("#live-letter-fit")).toContainText("may exceed two printed pages");
  await letter.fill(original);
  await expect(page.locator("#live-letter-fit")).toBeEmpty();
  await expect(page.locator("#liveAdopt")).toBeEnabled();
  await page.locator("#liveAdopt").click();

  await page.route("**/packet-preview", route =>
    route.fulfill({status:409, json:{detail:"letter_does_not_fit"}}));
  await page.locator("#livePreview").click();
  await expect(page.getByRole("alert").filter({hasText:"Your letter does not fit"})).toBeVisible();
  await expect(page.locator("#liveApprove")).toHaveCount(0);

  await page.unroute("**/packet-preview");
  await previewAndInspect(page);
  await page.route("**/approval", route =>
    route.fulfill({status:409, json:{detail:"required_form_not_ready"}}));
  await page.locator("#liveApprove").click();
  await expect(page.getByRole("alert").filter({hasText:"A required agency form"})).toBeVisible();
  await expect(page.locator("#liveOpenPacket")).toHaveCount(0);
});
