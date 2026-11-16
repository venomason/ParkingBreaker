import { notFound } from "next/navigation";
import type { Metadata } from "next";
import { BrandFrame } from "../../components/brand/BrandFrame";

export const metadata: Metadata = {title:"Parking Breaker · Brand preview",robots:{index:false,follow:false}};
export const dynamic = "force-dynamic";

export default function DesignPreviewLayout({children}:{children:React.ReactNode}) {
  // Explicit local opt-in allows a compiled preview under the existing strict CSP.
  // Ordinary production starts keep this review-only surface unavailable.
  if (process.env.NODE_ENV === "production" && process.env.PB_DESIGN_PREVIEW !== "local") notFound();
  return <BrandFrame>{children}</BrandFrame>;
}
