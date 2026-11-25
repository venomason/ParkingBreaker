"use client";

import React, { useEffect, useRef, useState } from "react";
import type { BetaOffer } from "../lib/api";
import { mailClassName } from "../lib/fulfillment";
import { ConciergeMark } from "./ConciergeMark";
import { Icon } from "./Icons";
import { LegalDisclosure } from "./LegalDisclosure";

interface Step4BetaOfferProps {
  offer: BetaOffer;
  caseId: string;
  busy: boolean;
  blocked?: boolean;
  error: string | null;
  onAccept: () => void | Promise<void>;
  onRefresh: () => void;
  setActionBar: (sub: React.ReactNode, buttons: React.ReactNode) => void;
}

/**
 * Selected free-beta offer. This screen has no card slot, no tier choice and no
 * price fallback: the server disclosure is the whole offer. Consent starts
 * unchecked and resets whenever the case or the displayed disclosure changes.
 */
export function Step4BetaOffer({
  offer,
  caseId,
  busy,
  blocked = false,
  error,
  onAccept,
  onRefresh,
  setActionBar,
}: Step4BetaOfferProps) {
  // Consent is bound to the exact case and displayed digest, so a change in
  // either one automatically returns the checkbox to unchecked.
  const scope = `${caseId}\u0000${offer.disclosure_sha256}\u0000${offer.tier}`;
  const mailClass = mailClassName(offer.tier);
  const [consentedScope, setConsentedScope] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const inFlight = useRef<string | null>(null);
  const consented = consentedScope === scope;

  useEffect(() => {
    if (submitting || busy) {
      setActionBar(
        "Accepting your free beta selection…",
        <button className="btn btn-primary" type="button" disabled>
          <span className="spinner" /> Accepting…
        </button>
      );
      return;
    }
    setActionBar(
      <><Icon id="shield" size={14} /> $0 · no card hold · no payment</>,
      <button
        className="btn btn-primary"
        type="button"
        id="actBetaBtn"
        disabled={!consented || blocked}
        onClick={() => {
          if (!consented || inFlight.current === scope || busy) return;
          inFlight.current = scope;
          setSubmitting(true);
          void Promise.resolve(onAccept()).finally(() => {
            if (inFlight.current === scope) inFlight.current = null;
            setSubmitting(false);
          });
        }}
      >
        Accept free beta →
      </button>
    );
  }, [consented, submitting, busy, blocked, onAccept, setActionBar, scope]);

  return (
    <section className="screen on" id="s4" aria-labelledby="h-s4">
      <div className="voice">
        <ConciergeMark size={44} state={submitting ? "thinking" : "idle"} />
        <div>
          <p className="eyebrow">Step 3 · Selected free beta</p>
          <h2 id="h-s4" tabIndex={-1}>
            Your selected beta appeal is free.
          </h2>
          <p className="thought" aria-live="polite">
            {submitting ? "Recording your consent…" : ""}
          </p>
        </div>
      </div>

      <div className="card is-saved-card postal-frank-card" id="betaOfferCard">
        <div className="section-head-row">
          <span className="status-pill is-saved">
            <Icon id="check" size={12} /> Selected for this appeal from the owner-approved beta
          </span>
          <span className="postmark-stamp" aria-hidden="true">BETA · $0</span>
        </div>
        <p className="dl-big">
          <strong>{mailClass}</strong> · <strong>$0</strong>
        </p>
        <p className="muted small">
          No card hold, no payment and no card form are part of this selected beta.
          Your letter still follows the normal research, your answers, draft, adoption,
          destination and exact-packet approval steps before anything is mailed.
        </p>
      </div>

      <div className="card" style={{ marginTop: "14px" }} id="betaDisclosureCard">
        <h3>What you are accepting</h3>
        {/* Verbatim server text. Never substitute local copy or add promises. */}
        <p className="small" id="betaDisclosureText" style={{ whiteSpace: "pre-wrap", marginTop: "8px" }}>
          {offer.disclosure_text}
        </p>
      </div>

      <LegalDisclosure id="betaLegalDisclosure" />

      <label className={`packet-inspect ${consented ? "is-checked" : ""}`} htmlFor="betaConsent" style={{ marginTop: "14px" }}>
        <input
          id="betaConsent"
          type="checkbox"
          checked={consented}
          disabled={busy || submitting || blocked}
          onChange={(event) => setConsentedScope(event.target.checked ? scope : null)}
        />
        <span>
          I have read this exact disclosure. I accept {mailClass} for $0 with no card hold and no payment.
        </span>
      </label>

      {blocked && <p className="muted small" role="status" style={{ marginTop: "10px" }}>This offer cannot be accepted. Nothing was charged. Use the saved status above to continue.</p>}
      {error && (
        <div className="guard action-error" role="alert" id="betaError" style={{ marginTop: "12px" }}>
          <span>{error}</span>
          <button className="chip" type="button" onClick={onRefresh} disabled={busy || submitting}>
            Check the saved beta offer
          </button>
        </div>
      )}

      <p className="muted small" style={{ marginTop: "14px" }}>
        This free appeal includes one mailing attempt. Beta participation does not
        extend any ticket deadline.
      </p>
    </section>
  );
}
