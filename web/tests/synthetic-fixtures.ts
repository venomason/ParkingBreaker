import { expect, test as base } from "@playwright/test";

export { expect, type Page, type Route } from "@playwright/test";

// Test-runner-only controller injection. The production app keeps its worker routes closed.
export const test = base.extend<{ syntheticWorker: void }>({
  syntheticWorker: [async ({ page }, runTest) => {
    if (process.env.SYNTHETIC_REHEARSAL !== "1") {
      await runTest();
      return;
    }
    const apiValue = process.env.INGRESS_API_BASE;
    const secret = process.env.INGRESS_WORKER_SECRET;
    const secureValue = process.env.SECURE_BASE;
    if (!apiValue || !secret?.trim() || !secureValue) {
      throw new Error("Synthetic rehearsal requires private API, worker secret and secure base");
    }
    const api = new URL(apiValue);
    if (api.protocol !== "http:" || !["127.0.0.1", "localhost", "[::1]"].includes(api.hostname)
      || api.username || api.password || api.pathname !== "/" || api.search || api.hash) {
      throw new Error("Synthetic private API must be an HTTP loopback origin");
    }
    const secure = new URL(secureValue);
    if (secure.protocol !== "https:" || !secure.hostname.endsWith(".example.test") || secure.username || secure.password
      || secure.pathname !== "/" || secure.search || secure.hash) {
      throw new Error("Synthetic secure base must be a reserved HTTPS example.test origin");
    }
    const health = await page.request.get(`${api.origin}/health`, { maxRedirects: 0 });
    if (health.status() !== 200 || (await health.json()).mode !== "fake") {
      throw new Error("Synthetic private API must report fake mode");
    }
    await page.addInitScript(() => {
      // Offline adapter only: exercise CardField without loading Stripe or using card data.
      const syntheticStripe = function (key: string) {
        if (key !== "pk_test_synthetic_rehearsal") {
          throw new Error("Offline synthetic Stripe adapter refuses this key");
        }
        return {
          _registerWrapper() {},
          elements() {
            return {
              create(kind: string) {
                if (kind !== "card") throw new Error("Synthetic adapter supports only card elements");
                let mounted: HTMLElement | null = null;
                return {
                  mount(target: HTMLElement | string) {
                    mounted = typeof target === "string" ? document.querySelector<HTMLElement>(target) : target;
                    if (!mounted) throw new Error("Synthetic card mount is missing");
                    mounted.textContent = "Offline synthetic payment adapter — no card data or network";
                  },
                  on(event: string, callback: (value: { complete: boolean }) => void) {
                    if (event === "change") callback({ complete: true });
                  },
                  destroy() {
                    if (mounted) mounted.textContent = "";
                    mounted = null;
                  },
                };
              },
            };
          },
          async createPaymentMethod() {
            return { paymentMethod: { id: "pm_card_visa" } };
          },
        };
      };
      Object.defineProperty(window, "Stripe", { value: syntheticStripe, configurable: false, writable: false });
    });
    const unexpectedExternal: string[] = [];
    const allowed = (url: URL) => url.origin === secure.origin
      || (["http:", "https:"].includes(url.protocol)
        && ["127.0.0.1", "localhost", "[::1]"].includes(url.hostname));
    const networkPattern = "**/*";
    const networkHandler: Parameters<typeof page.route>[1] = async route => {
      const url = new URL(route.request().url());
      if (["http:", "https:"].includes(url.protocol) && !allowed(url)) {
        unexpectedExternal.push(`${url.origin}${url.pathname}`);
        await route.abort("blockedbyclient");
        return;
      }
      await route.fallback();
    };
    await page.route(networkPattern, networkHandler);
    const pattern = "**/dev-worker/**";
    const handler: Parameters<typeof page.route>[1] = async route => {
      const request = route.request();
      const url = new URL(request.url());
      const match = /^\/dev-worker\/(case_[a-f0-9]+)\/(research|draft|dispatch)$/.exec(url.pathname);
      if (request.method() !== "POST" || url.origin !== secure.origin || !match || url.search || url.hash) {
        await route.fallback();
        return;
      }
      const response = await page.request.post(`${api.origin}/internal/cases/${match[1]}/step`, {
        headers: { Authorization: `Bearer ${secret}` },
        data: { step_id: match[2] },
        maxRedirects: 0,
      });
      await route.fulfill({
        status: response.status(),
        contentType: response.headers()["content-type"] ?? "application/json",
        body: await response.body(),
      });
    };
    await page.route(pattern, handler);
    try {
      await runTest();
    } finally {
      await page.unroute(pattern, handler);
      await page.unroute(networkPattern, networkHandler);
      expect(unexpectedExternal, "Synthetic rehearsal attempted external browser HTTP(S)").toEqual([]);
    }
  }, { auto: true }],
});
