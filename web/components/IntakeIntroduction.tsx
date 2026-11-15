import { HurtlePoseIllustration } from "./HurtleMascot";

export function IntakeIntroduction() {
  return (
    <section className="intake-introduction" aria-labelledby="intake-title">
      <div className="intake-hero-copy">
        <p className="eyebrow">Parking ticket appeals, simplified</p>
        <h1 id="intake-title">Appeal your parking ticket in your own words. We handle the mail.</h1>
        <p className="intake-summary">
          Tell us what happened and review the exact packet before anything is sent. We print it, send it by USPS Certified Mail, and keep your mailing record and available tracking updates together.
        </p>
        <p className="intake-scope">Clerical service only. No legal advice and no promised outcome.</p>
      </div>
      <div className="intake-route" aria-hidden="true">
        <HurtlePoseIllustration pose="welcoming" />
      </div>
    </section>
  );
}
