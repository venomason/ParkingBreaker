import { Icon } from "./Icons";

const workflow = [
  ["Add your ticket", "Photograph the front, and the back if it has one. We read it, and you check every detail."],
  ["Tell your side", "Tell us what happened in your own words. We ask neutral questions about the facts."],
  ["Choose your mailing", "Review the offer for your appeal. Nothing is pre-selected."],
  ["Check the requirements", "We check the agency’s official pages and show you the sources and exact wording."],
  ["Approve your packet", "Review and edit the letter, inspect the exact PDF, and approve it before anything is sent."],
  ["Track your mailing", "Keep the available postal updates and a copy of your exact mailed packet in your appeal."],
];

export function IntakeTrust() {
  return (
    <div className="intake-support">
      <section className="intake-workflow" aria-labelledby="workflow-title">
        <p className="eyebrow">From your ticket to the mail</p>
        <h2 id="workflow-title">Less paperwork on your to-do list.</h2>
        <p className="intake-workflow-lead">
          Six clear steps. You stay in control of your words and approve the exact packet before anything is mailed.
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
          <div><Icon id="doc" size={22} /><h3>A mailing record you can keep.</h3><p>Keep the available USPS delivery or attempted-delivery updates and a copy of your mailed packet. Tracking does not tell us when the agency opens or reviews it, and it isn’t a USPS Return Receipt. Delivery is not guaranteed.</p></div>
          <div><Icon id="globe" size={22} /><h3>An address you can check.</h3><p>We look for the appeal address on the agency’s official pages and show you the sources. If we can’t verify it, you can supply an address after reading the separate terms, or stop. You approve the exact destination before mailing.</p></div>
        </div>
      </section>
    </div>
  );
}
