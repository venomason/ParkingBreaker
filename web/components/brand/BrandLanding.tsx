import Link from "next/link";
import { HurtleShell } from "./HurtleShell";
import styles from "./brand.module.css";

export function BrandLanding() {
  return <main id="brand-main" className={styles.landing}>
    <div className={styles.heroLockup}>
      <HurtleShell/>
      <div className={styles.heroWords}>
        <p className={styles.wordmark} aria-label="Parking Breaker">Parking Breaker</p>
        <div className={styles.heroDetails}>
          <h1>Your side. Clearly put.</h1>
          <p>A little less paperwork. A little more peace of mind.</p>
          <Link className={styles.primary} href="/design-preview/intake">Start my appeal <span className={styles.arrowCircle} aria-hidden="true">↗</span></Link>
        </div>
      </div>
    </div>
    <p className={styles.scope}>Clerical assistance. No legal advice or promised outcome.</p>
  </main>;
}
