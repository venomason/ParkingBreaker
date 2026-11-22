import { spawnSync } from "node:child_process";
import { closeSync, mkdtempSync, openSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { expect, test } from "@playwright/test";

function config(mode: string, key: string, apiOrigin?: string) {
  // File-backed output also works in sandboxes without child-process pipe support.
  const directory = mkdtempSync(join(tmpdir(), "pb-config-"));
  const stdout = openSync(join(directory, "out"), "w");
  const stderr = openSync(join(directory, "err"), "w");
  try {
    const result = spawnSync(process.execPath, ["--input-type=module", "-e", "const c=(await import('./next.config.ts')).default; console.log(JSON.stringify(await c.rewrites()))"], {
      cwd: process.cwd(),
      env: { ...process.env, PB_PROVIDER_MODE: mode, NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY: key, NODE_NO_WARNINGS: "1", ...(apiOrigin ? { PB_API_ORIGIN: apiOrigin } : {}) },
      stdio: ["ignore", stdout, stderr],
    });
    return { ...result, stdout: readFileSync(join(directory, "out"), "utf8"), stderr: readFileSync(join(directory, "err"), "utf8") };
  } finally {
    closeSync(stdout); closeSync(stderr); rmSync(directory, { recursive: true });
  }
}

test("customer web refuses missing or mixed browser payment keys", () => {
  for (const mode of ["test", "live"]) {
    for (const key of ["", "pk_test_", "pk_live_", "pk_test_value ", "pk_live_value\n", "pk_test_valueé", mode === "test" ? "pk_live_value" : "pk_test_value"]) {
      const result = config(mode, key);
      expect(result.status).not.toBe(0);
      expect(result.stderr).toContain("NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY");
      if (key) expect(result.stderr).not.toContain(key);
    }
    expect(config(mode, `pk_${mode}_synthetic`).status).toBe(0);
  }
});

test("saved-appeal, pricing and fresh-appeal routes use the configured backend", () => {
  const result = config("fake", "", "https://backend.example.test");
  expect(result.status).toBe(0);
  const routes = JSON.parse(result.stdout) as Array<{ source: string; destination: string }>;
  const auth = routes.find((route) => route.source === "/api/auth/:path*");
  expect(auth?.destination).toBe("https://backend.example.test/auth/:path*");
  for (const endpoint of ["appeal", "pricing", "appeals"]) {
    expect(auth?.destination.replace(":path*", endpoint)).toBe(`https://backend.example.test/auth/${endpoint}`);
  }
});

test("public API proxy contains only public routes and no worker catch-all", () => {
  const result = config("fake", "", "https://backend.example.test");
  expect(result.status).toBe(0);
  const routes = JSON.parse(result.stdout) as Array<{ source: string; destination: string }>;
  expect(routes.map((route) => route.source)).toEqual([
    "/api/health", "/api/auth/:path*", "/api/workspaces/:path*", "/api/cases/:path*", "/api/outcomes/:path*", "/api/webhooks/:path*",
  ]);
  const outcomes = routes.find((route) => route.source === "/api/outcomes/:path*");
  for (const action of ["opt-out", "report"]) {
    expect(outcomes?.destination.replace(":path*", action)).toBe(`https://backend.example.test/outcomes/${action}`);
  }
  for (const route of routes) {
    expect(route.source).not.toContain("internal");
    expect(route.destination).not.toContain("internal");
  }
});
