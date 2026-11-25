import type { NextConfig } from "next";

// The browser talks to the API through this same-origin proxy, so no CORS is needed and
// nothing secret is exposed: worker steps go through the server-only dev-worker route.
// Next writes these rewrites at build time. Remote deployments must supply their
// reachable PB_API_ORIGIN during `next build`; a runtime-only value cannot retarget them.
const apiOrigin = process.env.PB_API_ORIGIN || "http://127.0.0.1:8000";

const mode = process.env.PB_PROVIDER_MODE;
if (mode === "test" || mode === "live") {
  const key = process.env.NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY ?? "";
  const prefix = mode === "test" ? "pk_test_" : "pk_live_";
  if (!key.startsWith(prefix) || key.length <= prefix.length || !/^[A-Za-z0-9_]+$/.test(key)) {
    throw new Error("NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY is missing or does not match PB_PROVIDER_MODE");
  }
}

const nextConfig: NextConfig = {
  agentRules: false,
  reactStrictMode: true,
  devIndicators: false,
  async headers() {
    return [
      {
        source: "/(.*)",
        headers: [
          {
            key: "Content-Security-Policy",
            value: [
              "default-src 'self'",
              "script-src 'self' 'unsafe-inline' https://js.stripe.com",
              "style-src 'self' 'unsafe-inline'",
              "img-src 'self' data: blob:",
              "font-src 'self' data:",
              "frame-src 'self' blob: https://js.stripe.com https://hooks.stripe.com",
              "connect-src 'self' blob: https://api.stripe.com",
              "frame-ancestors 'none'",
              "base-uri 'self'",
              "form-action 'self'",
            ].join("; "),
          },
          {
            key: "X-Frame-Options",
            value: "DENY",
          },
          {
            key: "X-Content-Type-Options",
            value: "nosniff",
          },
          {
            key: "Referrer-Policy",
            value: "strict-origin-when-cross-origin",
          },
          {
            // Scope to this exact host on purpose: no includeSubDomains or
            // preload until every subdomain is verified HTTPS-only.
            key: "Strict-Transport-Security",
            value: "max-age=15552000",
          },
        ],
      },
      {
        source: "/outcomes/:page(opt-out|pilot)",
        headers: [{ key: "Referrer-Policy", value: "no-referrer" }],
      },
    ];
  },
  async rewrites() {
    // Only public application routes cross customer ingress. Workers remain on
    // the private API listener and retain their separate bearer authentication.
    return ["health", "auth/:path*", "workspaces/:path*", "cases/:path*", "outcomes/:path*", "webhooks/:path*"].map((path) => ({
      source: `/api/${path}`,
      destination: `${apiOrigin}/${path}`,
    }));
  },
};

export default nextConfig;
