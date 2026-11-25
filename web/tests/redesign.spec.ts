import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { expect, test } from "@playwright/test";
import type { AxeResults } from "axe-core";

function visual(name: string, props: object, file = "CaseVisuals"): string {
  const source = `
    const fs=require('node:fs'), ts=require('typescript');
    for(const extension of ['.ts','.tsx']) require.extensions[extension]=(module,filename)=>{
      const out=ts.transpileModule(fs.readFileSync(filename,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,esModuleInterop:true}});
      module._compile(out.outputText,filename);
    };
    const React=require('react'), {renderToStaticMarkup}=require('react-dom/server');
    const Component=require('./components/'+process.argv[3]+'.tsx')[process.argv[1]];
    process.stdout.write(renderToStaticMarkup(React.createElement(Component,JSON.parse(process.argv[2]))));
  `;
  const result=spawnSync(process.execPath,["-e",source,name,JSON.stringify(props),file],{cwd:process.cwd(),encoding:"utf8"});
  expect(result.status,result.stderr).toBe(0);
  return result.stdout;
}

test("customer milestones follow the live journey without renumbering internal steps",async({page})=>{
  for(const [step,label] of [[1,"Step 1 of 6 · Your ticket"],[2,"Step 2 of 6 · Your side"],[4,"Step 3 of 6 · Mailing choice"],[5,"Step 3 of 6 · Mailing choice"],[3,"Step 4 of 6 · Appeal rules"],[6,"Step 5 of 6 · Your letter"]] as const){
    await page.setContent(visual("Header",{step,timerEnd:null,now:0,lapsed:false},"Header"));
    await expect(page.locator("#pathNow")).toHaveText(label);
    await expect(page.locator("#pathSegs, .milestone-number, .milestone-label")).toHaveCount(0);
  }
  await page.setContent(visual("Receipts",{receipts:[{stepNum:4,stepName:"Mailing choice",summary:"Chosen"},{stepNum:3,stepName:"Research",summary:"Checked"},{stepNum:6,stepName:"Your letter",summary:"Approved"}]},"Receipts"));
  await expect(page.locator(".receipt-num")).toHaveText(["03","04","05"]);
});

test("saved but unadopted text points to adoption; a PDF preview alone never implies inspection",async({page})=>{
  const initial={adopted:false,changed:false,editingAdopted:false,previewReady:false,inspected:false,locked:false,submissionPending:false,approvalUncertain:false};
  await page.setContent(visual("PacketReviewProgress",initial));
  await expect(page.locator('[aria-current="step"]')).toHaveCount(1);
  await expect(page.locator('[aria-current="step"]')).toContainText("Adopt");
  await page.setContent(visual("PacketReviewProgress",{...initial,adopted:true,previewReady:true}));
  await expect(page.locator('[aria-current="step"]')).toContainText("Inspect");
  await expect(page.locator(".ps-status")).not.toContainText("Inspection recorded");
  await page.setContent(visual("PacketReviewProgress",{...initial,adopted:true,previewReady:true,inspected:true}));
  await expect(page.locator('[aria-current="step"]')).toContainText("Approve");
});

test("an uncertain approval has exactly one active stage and makes no completion claim",async({page})=>{
  await page.setContent(visual("PacketReviewProgress",{adopted:true,changed:false,editingAdopted:false,previewReady:true,inspected:true,locked:true,submissionPending:false,approvalUncertain:true}));
  await expect(page.locator('[aria-current="step"]')).toHaveCount(1);
  await expect(page.locator('[aria-current="step"]')).toContainText("Approve");
  await expect(page.locator(".ps-status")).toContainText("uncertain");
  await expect(page.locator(".ps-status")).not.toContainText("Inspection recorded");
});

test("requirement counts distinguish missing and conflicting facts without a success score",async({page})=>{
  await page.setContent(visual("RequirementChecks",{idPrefix:"test",findings:[
    {fact:"ADDRESS",status:"CONFIRMED",official_quote:"Mail here"},
    {fact:"SIGNATURE",status:"UNKNOWN",official_quote:null},
    {fact:"DEADLINE",status:"CONFLICTED",official_quote:"Different instructions"},
    {fact:"FORM",status:"NOT_APPLICABLE",official_quote:null},
  ]}));
  await expect(page.locator(".reqbar-legend li")).toHaveCount(4);
  await expect(page.locator(".reqbar-legend")).toContainText("not found");
  await expect(page.locator(".reqbar-legend")).toContainText("conflicting");
  await expect(page.locator("body")).not.toContainText("100%");
});

test("appeal rules link the official wording to its source and hide retired code findings", async ({page}) => {
  const url = "https://agency.example/first-review";
  await page.setContent(visual("ResearchEvidence", {issuerName: "City Parking Office", research: {
    jurisdiction: "test-issuer", stage: "first-level review", sources: [],
    findings: [
      {fact: "FILING_INSTRUCTION", status: "CONFIRMED", passages: [
        {quote: "Enter the provision printed on your ticket in box four.", source_url: url}
      ]},
      {fact: "GOVERNING_CODE_SECTION", status: "CONFIRMED", official_quote: "Historic citation text"},
      {fact: "MANDATORY_FORMS", status: "UNKNOWN"}
    ]
  }}, "LiveResearch"));
  await expect(page.locator(".research-detail")).toContainText("City Parking Office · first-level review");
  await expect(page.getByRole("link", {name: "Read the official source"})).toHaveAttribute("href", url);
  await expect(page.locator(".finding")).toHaveCount(2);
  await expect(page.locator(".findings")).toContainText("Enter the provision printed on your ticket in box four.");
  await expect(page.locator(".findings")).toContainText("No verified wording");
  await expect(page.locator("body")).not.toContainText("Historic citation text");
});

for(const phase of ["paid","controlled_free_beta"] as const){
  for(const width of [320,390,1440]){
    test(`${phase} redesigned landing is readable and accessible at ${width}px`,async({page},testInfo)=>{
      await page.setViewportSize({width,height:900});
      await page.emulateMedia({reducedMotion:"reduce"});
      await page.route("**/api/auth/session",r=>r.fulfill({status:401,json:{detail:"no_session"}}));
      await page.route("**/api/auth/appeal",r=>r.fulfill({status:401,json:{detail:"no_session"}}));
      await page.route("**/api/auth/pricing",r=>r.fulfill({json:{experiment_id:"certified-price-v1",service_phase:phase,amount_cents:phase==="paid"?2499:null,arm:phase==="paid"?"P2499":null}}));
      await page.goto("/");
      await expect(page.locator("#actSnap")).toBeVisible();
      await expect(page.getByRole("heading",{level:1})).toHaveText("Appeal your parking ticket in your own words.");
      await expect(page.locator("#intake-title")).toHaveText("As soon as you spot a ticket, start taking photos.");
      expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
      await page.locator("#actSnap").focus();
      expect(await page.locator("#actSnap").evaluate(e=>getComputedStyle(e).outlineStyle)).not.toBe("none");
      const action=(await page.locator("#actSnap").boundingBox())!;
      expect(action.height).toBeGreaterThanOrEqual(44);
      expect(action.x).toBeGreaterThanOrEqual(0);
      expect(action.x+action.width).toBeLessThanOrEqual(width+0.5);
      expect(action.y+action.height).toBeLessThanOrEqual(900.5);
      if(phase==="controlled_free_beta") await expect(page.getByLabel("Assigned certified mailing price")).toHaveCount(0);
      else await expect(page.getByLabel("Assigned certified mailing price")).toContainText("$24.99");
      await page.addScriptTag({content:readFileSync(require.resolve("axe-core/axe.min.js"),"utf8")});
      const violations=await page.evaluate(async()=>{
        const axe=(window as unknown as {axe:{run:(context:Document,options:object)=>Promise<AxeResults>}}).axe;
        const r=await axe.run(document,{runOnly:{type:"tag",values:["wcag2a","wcag2aa","wcag21a","wcag21aa"]}});
        return r.violations.map(v=>({id:v.id,targets:v.nodes.map(n=>n.target)}));
      });
      expect(violations).toEqual([]);
      await page.screenshot({path:testInfo.outputPath(`redesign-${phase}-${width}.png`),fullPage:true});
    });
  }
}
