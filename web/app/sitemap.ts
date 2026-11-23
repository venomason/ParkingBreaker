import type { MetadataRoute } from "next";

// Relative entries; the deployment origin is supplied by the host serving the app.
export default function sitemap(): MetadataRoute.Sitemap {
  const now = new Date();
  return [
    { url: "/", lastModified: now, changeFrequency: "weekly", priority: 1 },
    { url: "/terms", lastModified: now, changeFrequency: "yearly", priority: 0.3 },
    { url: "/privacy", lastModified: now, changeFrequency: "yearly", priority: 0.3 },
    { url: "/refund-policy", lastModified: now, changeFrequency: "yearly", priority: 0.3 },
  ];
}
