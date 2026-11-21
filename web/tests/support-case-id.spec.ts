import { spawnSync } from "node:child_process";
import { expect, test } from "@playwright/test";
import { formatSupportCaseId } from "../lib/support-case-id";

function render(component: string, props: object): string {
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
    const Component = require('./components/' + name + '.tsx')[name];
    const props = {...JSON.parse(process.argv[2]),onComplete:()=>{},setActionBar:()=>{},onDemoWarn:()=>{},onDemoLapse:()=>{}};
    process.stdout.write(renderToStaticMarkup(React.createElement(Component,props)));
  `;
  const result = spawnSync(process.execPath, ["-e", source, component, JSON.stringify(props)], {
    cwd: process.cwd(),
    encoding: "utf8",
  });
  expect(result.status, result.stderr).toBe(0);
  return result.stdout;
}

test("checkout confirmation (Step5Window) shows authoritative backend PB support code, copy button, hard support rule, and zero lookup/resend links", async ({ page }) => {
  await page.setContent(render("Step5Window", {
    live: true,
    supportCaseId: "PB-48213",
    amountCents: 2900,
    tier: "certified",
    timerEnd: 120000,
    now: 0,
    lapsed: false,
  }));

  await expect(page.locator("#supportCaseIdCard")).toBeVisible();
  await expect(page.locator("#supportCaseIdValue")).toHaveText("PB-48213");
  await expect(page.locator("#copyCaseIdBtn")).toHaveText("Copy case ID");
  await expect(page.locator("#supportCaseIdCard")).toContainText(
    "Save this ID — you will need it to contact support by phone or email."
  );
  await expect(page.locator("#supportCaseIdCard")).toContainText(
    "We can only respond to calls and emails that include your case ID, so save it."
  );

  const cardText = (await page.locator("#supportCaseIdCard").textContent()) ?? "";
  expect(cardText.toLowerCase()).not.toContain("resend");
  expect(cardText.toLowerCase()).not.toContain("look up");
  expect(cardText.toLowerCase()).not.toContain("forgot");
});

test("formatSupportCaseId accepts only backend-issued PB-XXXXX codes and never fabricates from opaque case tokens", () => {
  expect(formatSupportCaseId("PB-48213")).toBe("PB-48213");
  expect(formatSupportCaseId(" pb-90012 ")).toBe("PB-90012");
  expect(formatSupportCaseId("case_0193a8f2-7d3c-4e0b-9f1a")).toBeNull();
  expect(formatSupportCaseId("PB-123")).toBeNull();
  expect(formatSupportCaseId(null)).toBeNull();
  expect(formatSupportCaseId("")).toBeNull();
});
