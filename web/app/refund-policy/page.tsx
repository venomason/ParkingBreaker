import type { Metadata } from "next";
import { CustomerChoiceRules } from "../../components/CustomerChoiceRules";
import {
  HOLD_REFUND_RULES,
  LEGAL_CONTACT_EMAIL,
  LEGAL_EFFECTIVE_DATE,
  LEGAL_ENTITY,
} from "../../lib/legal";

export const metadata: Metadata = {
  title: `Refund Policy | ${LEGAL_ENTITY}`,
  description: "Hold release, duplicate charges, verified-destination errors and mailing-error refunds.",
};

export default function RefundPolicyPage() {
  return (
    <main className="access-shell">
      <section className="access-card">
        <h1>Refund Policy</h1>
        <p className="small muted">Effective {LEGAL_EFFECTIVE_DATE}</p>
        <ul>
          {HOLD_REFUND_RULES.map((rule) => (
            <li key={rule}>{rule}</li>
          ))}
        </ul>
        <CustomerChoiceRules />
        <h2>Contact</h2>
        <p>
          Refund questions: <a href={`mailto:${LEGAL_CONTACT_EMAIL}`}>{LEGAL_CONTACT_EMAIL}</a>.
        </p>
      </section>
    </main>
  );
}
