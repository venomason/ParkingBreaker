import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  fullyParallel: false,
  workers: 1,
  use: {
    baseURL: process.env.SYNTHETIC_BASE_URL ?? "http://127.0.0.1:3001",
    ignoreHTTPSErrors: process.env.SYNTHETIC_REHEARSAL === "1",
    launchOptions: process.env.PLAYWRIGHT_CHROME_PATH
      ? { executablePath: process.env.PLAYWRIGHT_CHROME_PATH,
          args: process.env.PLAYWRIGHT_HOST_RULES ? [`--host-resolver-rules=${process.env.PLAYWRIGHT_HOST_RULES}`, "--no-proxy-server"] : undefined }
      : undefined,
    viewport: { width: 375, height: 812 },
    contextOptions: { reducedMotion: "reduce" },
    trace: "retain-on-failure",
  },
});
