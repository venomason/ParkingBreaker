import type { Metadata } from "next";
import { CustomerChoiceRules } from "../../components/CustomerChoiceRules";
import { ATTORNEY_DISCLOSURE } from "../../components/LegalDisclosure";
import {
  LEGAL_CONTACT_EMAIL,
  LEGAL_EFFECTIVE_DATE,
  LEGAL_ENTITY,
  TERMS_SECTIONS,
} from "../../lib/legal";

export const metadata: Metadata = {
  title: `Terms of Service | ${LEGAL_ENTITY}`,
  description: "ParkingBreaker terms of service for clerical document preparation and mailing.",
};

export default function TermsPage() {
  return (
    <main className="access-shell">
      <section className="access-card">
        <h1>Terms of Service</h1>
        <p className="small muted">Effective {LEGAL_EFFECTIVE_DATE}</p>
        <p className="small muted">{ATTORNEY_DISCLOSURE}</p>
        {TERMS_SECTIONS.map((section) => (
          <section key={section.title}>
            <h2>{section.title}</h2>
            {section.paragraphs.map((paragraph) => (
              <p key={paragraph}>{paragraph}</p>
            ))}
          </section>
        ))}
        <CustomerChoiceRules />
        <h2>Contact</h2>
        <p>
          Questions about these terms: <a href={`mailto:${LEGAL_CONTACT_EMAIL}`}>{LEGAL_CONTACT_EMAIL}</a>.
        </p>
      </section>
    </main>
  );
}
