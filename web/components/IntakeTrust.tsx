import { Icon } from "./Icons";

const workflow = [
  ["Photograph your ticket", "Start at the windshield. Photograph the ticket and any surroundings you want to keep. Check the ticket details and issuing agency we read."],
  ["Tell your side", "Tell us what happened in your own words. We ask neutral questions about the facts."],
  ["Review your mailing offer", "See the mailing class and any fee for your appeal before you accept."],
  ["Check the requirements", "We check the agency’s official pages and show you the sources and exact wording."],
  ["Approve your packet", "Review and edit the letter, inspect the exact PDF, and approve it before anything is sent."],
  ["Keep your mailing record", "After you approve the packet, ParkingBreaker sends it through Lob. Your appeal keeps a copy of the mailed packet."],
];

export function IntakeTrust() {
  return (
    <details className="intake-support guide-notes">
      <summary>How the appeal and mailing work</summary>
      <section className="intake-workflow" aria-labelledby="workflow-title">
        <p className="eyebrow">From your ticket to the mail</p>
        <h2 id="workflow-title">Less paperwork on your to-do list.</h2>
        <p className="intake-workflow-lead">
          Clerical assistance · No legal advice or promised outcome. You stay in control of your words and approve the exact packet before anything is mailed.
        </p>
        <ol className="workflow-list">
          {workflow.map(([title, description], index) => (
            <li key={title}>
              <span className="workflow-number" aria-hidden="true">0{index + 1}</span>
              <div><h3>{title}</h3><p>{description}</p></div>
            </li>
          ))}
        </ol>
      </section>
      <section className="intake-trust" aria-labelledby="trust-title">
        <p className="eyebrow">What you can expect</p>
        <h2 id="trust-title">Your words. Your approval.</h2>
        <div className="trust-grid">
          <div><Icon id="doc" size={22} /><h3>Your words, your control.</h3><p>Tell us what happened. Review and edit the draft before adopting it as your own statement. We don’t judge your reasons or invent arguments. We handle the clerical work without evaluating the merits of your appeal.</p></div>
          <div><Icon id="check" size={22} /><h3>Nothing goes out without your OK.</h3><p>You inspect and approve the exact PDF first.</p></div>
          <div><Icon id="lock" size={22} /><h3>Private by default.</h3><p>Each appeal has its own private link.</p></div>
          <div><Icon id="doc" size={22} /><h3>A mailing record you can keep.</h3><p>Keep a copy of your mailed packet. Certified Mail shows available postal updates. Ordinary First-Class Mail comes without a tracking number, proof of delivery or a delivery guarantee.</p></div>
          <div><Icon id="globe" size={22} /><h3>An address you can check.</h3><p>We look for the appeal address on the agency’s official pages and show you the sources. If we cannot establish the required submission details, we pause or stop without charging for mailing. You approve the exact destination before mailing.</p></div>
        </div>
      </section>
    </details>
  );
}
