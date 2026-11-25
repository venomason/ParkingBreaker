import { NONMAILING_REFUND, PAID_CAPTURE_TERMS } from "../../../lib/legal";
import Link from "next/link";
import { ATTORNEY_DISCLOSURE } from "../../../components/LegalDisclosure";
import styles from "../../../components/brand/brand.module.css";

const sections = [
  {title:"Your ticket, in your words.", paragraphs:["Add a readable photo of the front of your ticket, and the back if it has one. Check the suggested facts and correct anything missing or wrong. Customer-supplied facts stay distinct from the reading service’s suggestions. We do not guess unclear letters or numbers.","If essential facts are still unavailable, the appeal stops before a charge or dispatch. Tell us what happened in your own words. We ask neutral questions about facts; we do not invent reasons, assess the strength of your case, or predict an outcome."]},
  {title:"The details are there when you need them.", paragraphs:["We check the agency’s official pages and show the source and exact wording for each rule used. If we cannot establish the required submission details, we pause or stop without charging for mailing. You approve the exact destination before mailing.","Review the mailing options and prices actually available for your appeal. Nothing is pre-selected. The current service phase and your appeal’s saved funding determine what is offered."]},
  {title:"Nothing goes out without your approval.", paragraphs:["Review and edit the draft, adopt the words as your own statement, inspect the exact packet PDF, and approve it before anything is sent. You are responsible for the facts, reasons, declarations and signature in your submission.","We handle formatting, printing and mailing. We do not represent you. A filed or mailed appeal does not promise a favorable decision."]},
  {title:"Clear payment boundaries.", paragraphs:["A selected free beta appeal requests no card and no payment. Existing paid appeals keep their saved payment status and mailing choice; an uncertain payment is checked before any new authorization is offered.",`For an admitted paid appeal, we place a hold first. ${PAID_CAPTURE_TERMS} ${NONMAILING_REFUND}`]},
  {title:"A record you can keep.", paragraphs:["Each appeal has its own private access link. Keep a copy of the exact approved packet and the postal updates available for your mailing.","Tracking can report delivery or attempted delivery. It does not tell us whether the agency has opened, read or decided your appeal. Tracking is not a USPS Return Receipt, and delivery is not guaranteed."]},
];

export default function RulesPreview() {
 return <main id="brand-main" className={styles.rulesPage}>
   <Link className={styles.backLink} href="/design-preview">← Back to the beginning</Link>
   <div className={styles.rulesHeading}><p className={styles.eyebrow}>The full picture</p><h1>Clear rules.<br/>No surprises.</h1><p>All the detail, here when you want it.</p></div>
   <div className={styles.rulesBody}>{sections.map((section,i)=><section key={section.title}><span className={styles.sectionNumber}>0{i+1}</span><div><h2>{section.title}</h2>{section.paragraphs.map(text=><p key={text}>{text}</p>)}</div></section>)}</div>
   <aside className={styles.legal}><h2>Our role</h2><p>{ATTORNEY_DISCLOSURE}</p></aside>
   <Link className={styles.primary} href="/design-preview/intake">Start my appeal <span className={styles.arrowCircle} aria-hidden="true">↗</span></Link>
 </main>;
}
