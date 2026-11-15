import { expect, test } from "@playwright/test";
import { spawnSync } from "node:child_process";
import { closeSync, mkdtempSync, openSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

function render(name: string, props: object): string {
  const script = `
    const fs = require('node:fs'); const ts = require('typescript');
    for (const extension of ['.ts', '.tsx']) require.extensions[extension] = (module, file) => {
      module._compile(ts.transpileModule(fs.readFileSync(file, 'utf8'), {
        compilerOptions: {module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true}
      }).outputText, file);
    };
    const component = require('./components/' + process.argv[1] + '.tsx')[process.argv[1]];
    process.stdout.write(require('react-dom/server').renderToStaticMarkup(
      require('react').createElement(component, JSON.parse(process.argv[2]))));
  `;
  const directory = mkdtempSync(join(tmpdir(), "pb-launch1-"));
  const stdout = openSync(join(directory, "out"), "w");
  const stderr = openSync(join(directory, "err"), "w");
  try {
    const result = spawnSync(process.execPath, ["-e", script, name, JSON.stringify(props)], {
      encoding: "utf8", stdio: ["ignore", stdout, stderr],
    });
    expect(result.error).toBeUndefined();
    expect(result.status, readFileSync(join(directory, "err"), "utf8")).toBe(0);
    return readFileSync(join(directory, "out"), "utf8");
  } finally {
    closeSync(stdout); closeSync(stderr); rmSync(directory, { recursive: true });
  }
}

test("rental payment question appears in intake with no default answer and restores unknown", () => {
  const props = {busy: false, submitting: false, error: null, values: {rentalPaid: ""}, ticketPhoto: null, ticketBackPhoto: null, evidencePhotos: [], ticketPhotoRef: null, ticketBackPhotoRef: null};
  const html = render("LiveDetails", props);
  expect(html).toContain("Has the rental company already paid this ticket?");
  expect(html).toContain('id="liveRentalPaid"');
  expect(html).toContain('<option value="" selected="">Choose an answer</option>');
  for (const value of ["yes", "no", "not-rental", "unknown"]) expect(html).toContain(`value="${value}"`);
  const restored = render("LiveDetails", {...props, values: {rentalPaid: "unknown"}});
  expect(restored).toContain('<option value="unknown" selected="">');
});

test("pre-payment stop shows recorded message and official link without a payment control", () => {
  const props = {liveStop: {message: "Your ticket is more than 10 days old, so we can't prepare an appeal in time. Nothing was charged.", officialQuote: null, officialUrl: "https://issuer.gov/appeals"}};
  const html = render("Step8Stop", props);
  expect(html).toContain("Your ticket is more than 10 days old");
  expect(html).toContain('href="https://issuer.gov/appeals"');
  expect(html).toContain('rel="noopener noreferrer"');
  expect(html).not.toMatch(/<input|<select|Stripe|card hold/i);
  expect(render("Step8Stop", {liveStop: {...props.liveStop, officialUrl: null}})).not.toMatch(/<a\b[^>]*href=/);
  expect(render("Step8Stop", {liveStop: {...props.liveStop, officialUrl: "javascript:alert(1)"}})).not.toMatch(/<a\b[^>]*href=/);
});

test("generated prototype and live price is $29", () => {
  const source = readFileSync("../backend/src/parkingbreaker/prices.py", "utf8");
  const generated = readFileSync("lib/prices.ts", "utf8");
  expect(source).toMatch(/^CERTIFIED_CENTS = PRICE_ARMS\["P2999"\]$/m);
  expect(generated).toContain("export const CERTIFIED_CENTS = 2999;");
  const offer = {selectedTier: "certified"};
  expect(render("Step4Offer", offer)).toContain("$29");
  expect(render("Step5Window", {tier: "certified", now: 0, timerEnd: 100000})).toContain("$29");
  const current = {issuerName: "Recorded agency", decidingBody: "Review office", standardOffered: false, testMode: false, certifiedCents: 2999, standardCents: null};
  expect(render("Step4Offer", {...offer, liveOffer: current})).toContain("$29");
  expect(render("Step5Window", {live: true, tier: "certified", amountCents: 2999, now: 0, timerEnd: 100000})).toContain("$29");
});
