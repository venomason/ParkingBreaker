import type { MetadataRoute } from "next";

// Reserved HTTP transport target; content pages are served on the public origin
// once the standing target is provisioned. Keeping this dynamic avoids baking a
// domain that changes between preview and production.
export default function robots(): MetadataRoute.Robots {
  return {
    rules: [{ userAgent: "*", allow: "/", disallow: ["/api/", "/dev-worker/"] }],
  };
}
