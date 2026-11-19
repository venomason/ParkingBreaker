import type { Metadata } from "next";
import { notFound } from "next/navigation";
import Home from "../page";
import { LocalJourneyPreview } from "../../components/LocalJourneyPreview";

export const dynamic = "force-dynamic";
export const metadata: Metadata = { title: "Try Hurtle Turtle · ParkingBreaker", robots: { index: false, follow: false } };

export default function TryHurtle() {
  if (process.env.PB_DESIGN_PREVIEW !== "local" || process.env.NEXT_PUBLIC_LIVE_API === "1") notFound();
  return <LocalJourneyPreview>
    {process.env.PB_FAMILY_PREVIEW === "1" && <p className="family-preview-note">Family preview · Use sample details. Payments and mailing are simulated.</p>}
    <Home />
  </LocalJourneyPreview>;
}
