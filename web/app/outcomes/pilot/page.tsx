import type { Metadata } from "next";
import { PilotFeedbackLink } from "../../../components/PilotFeedbackLink";

export const metadata: Metadata = { robots: { index: false, follow: false } };

export default function PilotFeedbackPage() {
  return <PilotFeedbackLink />;
}
