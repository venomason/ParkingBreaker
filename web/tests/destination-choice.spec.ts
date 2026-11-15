import { spawnSync } from "node:child_process";
import { expect, test } from "@playwright/test";
import type { CaseStatus } from "../lib/api";

// The owner-approved text is read from the backend source so the test fails if the web
// ever shows anything but the exact bytes the server serves.
const DISCLOSURE_TEXT = spawnSync("python3", ["-c", `
import re, sys, ast
src = open(sys.argv[1]).read()
tree = ast.parse(src)
for node in tree.body:
    if isinstance(node, ast.Assign) and node.targets[0].id == "DISCLOSURE_TEXT":
        sys.stdout.write(ast.literal_eval(node.value))
`, "../backend/src/parkingbreaker/address_disclosure.py"], { encoding: "utf8" }).stdout;

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
    const mod = require('./components/'+process.argv[1]+'.tsx');
    const Component = mod[process.argv[2]];
    const props = {...JSON.parse(process.argv[3]),onDraft:()=>{},onStatus:()=>{},setActionBar:()=>{}};
    process.stdout.write(renderToStaticMarkup(React.createElement(Component,props)));
  `;
  const [file, name] = component.split(":");
  const result = spawnSync(process.execPath, ["-e", source, file, name, JSON.stringify(props)], { cwd: process.cwd(), encoding: "utf8" });
  expect(result.status, result.stderr).toBe(0);
  return result.stdout;
}

const SHA = "a".repeat(64);
const choiceStatus: CaseStatus = {
  case_id: "case_synthetic", workspace_id: "ws_synthetic", state: "RESEARCHING", payment_state: "AUTHORIZED",
  selected_tier: "CERTIFIED", window_started_at: "2026-10-21T10:00:00Z", window_expires_at: "2026-10-21T10:30:00Z",
  seconds_remaining: 1500, window_status: "ACTIVE", question: null, stop: null, fake: true,
  research: { sources: [], findings: [] },
  destination_choice: { required: true, reason_code: "ADDRESS_UNVERIFIED", disclosure_version: "v1", disclosure_sha256: SHA, address_revision: null, address_fingerprint: null },
};

test("the disclosure renders verbatim, paragraph by paragraph", async ({ page }) => {
  expect(DISCLOSURE_TEXT.split("\n\n").length).toBeGreaterThan(3);
  await page.setContent(render("DestinationChoice:DisclosureText", { text: DISCLOSURE_TEXT }));
  const paragraphs = await page.locator("#destDisclosure p").allTextContents();
  expect(paragraphs.join("\n\n")).toBe(DISCLOSURE_TEXT);
});

test("a stale address-choice response offers no unverified consent or mailing action", async ({ page }) => {
  await page.setContent(render("LiveResearch:LiveResearch", { status: choiceStatus, busy: false, caseId: "case_synthetic" }));
  await expect(page.locator("#destChoice")).toContainText("Mailing is paused");
  await expect(page.locator("#destConsent")).toHaveCount(0);
  await expect(page.locator("#destSubmit")).toHaveCount(0);
  await expect(page.locator("#destChoice")).not.toContainText("You can now continue to your letter");
});

test("no destination panel without destination_choice", async ({ page }) => {
  await page.setContent(render("LiveResearch:LiveResearch", { status: { ...choiceStatus, destination_choice: undefined }, busy: false, caseId: "case_synthetic" }));
  await expect(page.locator("#destChoice")).toHaveCount(0);
});

const messages: Record<string, string> = {
  research_not_ready: "The saved agency check is incomplete.",
  required_form_not_ready: "A required agency form is not ready for this packet.",
  required_copies_not_ready: "A required supporting copy is missing.",
  declaration_unaffirmed: "Confirm every required declaration in the current preview",
  deadline_not_feasible: "The verified deadline cannot be met by this mailing.",
  destination_verification_required: "Mailing is paused until the agency's appeal address is verified.",
};
for (const [detail, expected] of Object.entries(messages)) {
  test(`customer action error ${detail} has specific accurate copy`, async () => {
    // Evaluate the actual page function, not a duplicated lookup table.
    const result = spawnSync(process.execPath, ["-e", `
      const ts=require('typescript'), fs=require('node:fs');
      const source=ts.createSourceFile('page.tsx',fs.readFileSync('app/page.tsx','utf8'),ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
      const fn=source.statements.find(n=>ts.isFunctionDeclaration(n)&&n.name?.text==='plainError');
      if(!fn) throw new Error('plainError is missing');
      const js=ts.transpileModule(fn.getText(source),{compilerOptions:{target:ts.ScriptTarget.ES2020}}).outputText;
      class ApiError extends Error { constructor(detail){super(detail);this.detail=detail;} }
      process.stdout.write(eval(js+';plainError(new ApiError('+JSON.stringify(process.argv[1])+'))'));
    `, detail], {cwd: process.cwd(), encoding:"utf8"});
    expect(result.status, result.stderr).toBe(0);
    expect(result.stdout).toContain(expected);
    expect(result.stdout).not.toContain("We couldn’t confirm that action.");
    expect(result.stdout).not.toContain("Nothing was charged");
  });
}
