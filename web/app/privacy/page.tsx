import type { Metadata } from "next";
import {
  LEGAL_CONTACT_EMAIL,
  PRIVACY_REVISION_DATE,
  LEGAL_ENTITY,
  PRIVACY_SECTIONS,
} from "../../lib/legal";

export const metadata: Metadata = {
  title: `Privacy Policy | ${LEGAL_ENTITY}`,
  description: "What ParkingBreaker collects, why, who processes it, and how long it is kept.",
};

export default function PrivacyPage() {
  return (
    <main className="access-shell">
      <section className="access-card">
        <h1>Privacy Policy</h1>
        <p className="small muted">Last revised {PRIVACY_REVISION_DATE}</p>
        {PRIVACY_SECTIONS.map((section) => (
          <section key={section.title}>
            <h2>{section.title}</h2>
            {section.paragraphs.map((paragraph) => (
              <p key={paragraph}>{paragraph}</p>
            ))}
          </section>
        ))}
        <h2>Contact</h2>
        <p>
          Privacy questions: <a href={`mailto:${LEGAL_CONTACT_EMAIL}`}>{LEGAL_CONTACT_EMAIL}</a>.
        </p>
      </section>
    </main>
  );
}
