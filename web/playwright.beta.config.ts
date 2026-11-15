import { defineConfig, type LaunchOptions } from "@playwright/test";

// Controlled-free beta desktop/mobile matrix. The existing default campaign in
// playwright.config.ts is unchanged; this config is only used through an explicit
// --config selection and never substitutes, skips or falls back from a required
// project.
//
// Transport: Chromium keeps the canonical host-resolver map. Firefox has no
// host-resolver flag, so it uses the owned loopback HTTP CONNECT proxy that only
// tunnels the canonical HTTPS origin to the owned loopback TLS port. Transport is
// declared in each project's launch options so every context created from the
// launched browser (including stranger/anonymous contexts) inherits it.
//
// Mobile projects emulate a layout viewport only. They are not physical devices,
// cameras or real-device qualification.

const SYNTHETIC = process.env.SYNTHETIC_REHEARSAL === "1";
const CANONICAL_ORIGIN = "https://portal.example.test";

function explicit(name: string): string {
  const value = (process.env[name] ?? "").trim();
  if (SYNTHETIC && !value) {
    throw new Error(
      `Beta browser matrix requires ${name}; a missing/unlaunchable required project is a failure, not a skip`,
    );
  }
  return value;
}

function bareUrl(name: string, value: string): URL {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new Error(`${name} must be an absolute URL`);
  }
  if (url.username || url.password || url.pathname !== "/" || url.search || url.hash) {
    throw new Error(`${name} must be a bare origin`);
  }
  return url;
}

function browserBaseURL(): string {
  const configured = process.env.SYNTHETIC_BASE_URL?.trim();
  if (!SYNTHETIC) {
    return configured ? bareUrl("SYNTHETIC_BASE_URL", configured).origin : "http://127.0.0.1:3001";
  }
  const url = bareUrl("SYNTHETIC_BASE_URL", explicit("SYNTHETIC_BASE_URL"));
  if (url.protocol !== "https:" || url.origin !== CANONICAL_ORIGIN) {
    throw new Error(`Beta browser matrix requires the canonical origin ${CANONICAL_ORIGIN}`);
  }
  return url.origin;
}

function loopbackProxy(name: string, value: string): string {
  const url = bareUrl(name, value);
  if (url.protocol !== "http:" || !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname) || !url.port) {
    throw new Error(`${name} must be an explicit loopback HTTP CONNECT origin`);
  }
  return url.origin;
}

const baseURL = browserBaseURL();

function chromiumLaunchOptions(): LaunchOptions {
  const executablePath = explicit("PLAYWRIGHT_CHROME_PATH");
  const hostRules = explicit("PLAYWRIGHT_HOST_RULES");
  if (!executablePath) {
    return {};
  }
  if (!hostRules) {
    return { executablePath };
  }
  if (!/^MAP portal\.example\.test 127\.0\.0\.1:\d+$/.test(hostRules)) {
    throw new Error("PLAYWRIGHT_HOST_RULES must map the canonical host to one owned loopback TLS port");
  }
  return { executablePath, args: [`--host-resolver-rules=${hostRules}`, "--no-proxy-server"] };
}

function firefoxLaunchOptions(): LaunchOptions {
  const server = explicit("PLAYWRIGHT_FIREFOX_PROXY");
  if (!server) {
    return {};
  }
  return { proxy: { server: loopbackProxy("PLAYWRIGHT_FIREFOX_PROXY", server) } };
}

export default defineConfig({
  testDir: "./tests",
  fullyParallel: false,
  workers: 1,
  use: {
    baseURL,
    ignoreHTTPSErrors: SYNTHETIC,
    viewport: { width: 375, height: 812 },
    contextOptions: { reducedMotion: "reduce" },
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium-desktop1280x900",
      use: {
        browserName: "chromium",
        viewport: { width: 1280, height: 900 },
        launchOptions: chromiumLaunchOptions(),
      },
    },
    {
      name: "chromium-mobile375x812",
      use: {
        browserName: "chromium",
        viewport: { width: 375, height: 812 },
        launchOptions: chromiumLaunchOptions(),
      },
    },
    {
      name: "firefox-desktop1280x900",
      use: {
        browserName: "firefox",
        viewport: { width: 1280, height: 900 },
        launchOptions: firefoxLaunchOptions(),
      },
    },
    {
      name: "firefox-mobile375x812",
      use: {
        browserName: "firefox",
        viewport: { width: 375, height: 812 },
        launchOptions: firefoxLaunchOptions(),
      },
    },
  ],
});
