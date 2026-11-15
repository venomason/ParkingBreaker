import { spawnSync } from "node:child_process";
import { expect, test } from "@playwright/test";
import type { CaseStatus } from "../lib/api";

function render(component: string, props: object, environment: Record<string,string> = {}): string {
  // Run the real React renderer outside Playwright's JSX locator transform.
  const source = `
    const fs = require('node:fs');
    const ts = require('typescript');
    for (const extension of ['.ts','.tsx']) require.extensions[extension] = (module, filename) => {
      const result = ts.transpileModule(fs.readFileSync(filename,'utf8'), {
        compilerOptions: {module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, esModuleInterop:true}
      });
      module._compile(result.outputText,filename);
    };
    const React = require('react');
    const {renderToStaticMarkup} = require('react-dom/server');
    const name = process.argv[1];
    const Component = name === 'Home' ? require('./app/page.tsx').default : require('./components/'+name+'.tsx')[name];
    const props = {...JSON.parse(process.argv[2]),onComplete:()=>{},onDraft:()=>{},setActionBar:()=>{},onDemoWarn:()=>{},onDemoLapse:()=>{}};
    process.stdout.write(renderToStaticMarkup(React.createElement(Component,props)));
  `;
  const result = spawnSync(process.execPath,["-e",source,component,JSON.stringify(props)],{cwd:process.cwd(),env:{...process.env,...environment},encoding:"utf8"});
  expect(result.status,result.stderr).toBe(0);
  return result.stdout;
}

test("long saved evidence is quoted exactly without claiming complete procedure", async ({ page }) => {
  const quote = "Mail the written request to this office; include the citation number. ".repeat(8);
  const status: CaseStatus = {
    case_id: "case_synthetic", workspace_id: "ws_synthetic", state: "DRAFTING", payment_state: "AUTHORIZED",
    selected_tier: "CERTIFIED", window_started_at: null, window_expires_at: null, seconds_remaining: null,
    window_status: null, question: null, stop: null, fake:true,
    research: { sources: [], findings: [{fact:"MAILING_ADDRESS",status:"CONFIRMED",official_quote:quote}] },
  };
  await page.setContent(render("LiveResearch", {status,busy:false}));
  expect(await page.locator("q").textContent()).toBe(quote);
  await expect(page.getByText(/complete official procedure/)).toHaveCount(0);
});

test("production without the live path is unavailable and never exposes prototype progress", async ({page}) => {
  for (const flag of ["", "0"]) {
    await page.setContent(render("Home", {}, {NODE_ENV:"production",NEXT_PUBLIC_LIVE_API:flag}));
    await expect(page.getByRole("heading",{name:"Your appeal service is unavailable"})).toBeVisible();
    await expect(page.locator("#actSnap, #jumperBtn, #demoWarn, #demoLapse")).toHaveCount(0);
  }
});

test("live review window displays server amount and excludes all prototype controls", async ({ page }) => {
  await page.setContent(render("Step5Window", {
    live:true,amountCents:4321,tier:"certified",timerEnd:120000,now:0,lapsed:false,
  }));
  await expect(page.locator("#s5lead")).toContainText("$43.21");
  await expect(page.locator("#demoWarn, #demoLapse")).toHaveCount(0);
  await expect(page.locator("#s5")).not.toContainText("This San Diego example");
  await expect(page.locator("#s5")).not.toContainText("nothing is charged");
});
