import { notFound } from "next/navigation";
import type { Metadata, Viewport } from "next";
import { MotionFrame } from "../../components/brand/MotionJourney";

export const metadata: Metadata = {
  title: "Parking Breaker · Motion study",
  robots: { index: false, follow: false },
};
export const viewport: Viewport = { themeColor: "#f6f4ed" };
export const dynamic = "force-dynamic";

export default function MotionPreviewLayout({ children }: { children: React.ReactNode }) {
  if (process.env.NODE_ENV === "production" && process.env.PB_DESIGN_PREVIEW !== "local") notFound();
  return <MotionFrame>{children}</MotionFrame>;
}
