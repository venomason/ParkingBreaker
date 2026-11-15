"use client";

import { CERTIFIED_CENTS } from "../lib/prices";
import React, { useState, useEffect } from "react";
import { ConciergeMark } from "./ConciergeMark";
import { Icon } from "./Icons";
import { LegalDisclosure } from "./LegalDisclosure";
import { Timeline } from "./Timeline";
import { SAMPLE_FACTS } from "../lib/api";

interface Step4OfferProps {
  selectedTier: "certified" | "standard" | null;
  setSelectedTier: (tier: "certified" | "standard") => void;
  onComplete: (tier: "certified" | "standard") => void | Promise<void>;
  setActionBar: (sub: React.ReactNode, buttons: React.ReactNode) => void;
  onOpenTerms: () => void;
  onOpenSample: () => void;
  // Real flow: the agency confirmed before the hold, and only the offered tiers.
  liveOffer?: { issuerName: string; decidingBody: string; standardOffered: boolean; testMode: boolean; certifiedCents: number | null; standardCents: number | null };
  // Real flow: Stripe's own card field, rendered in place of the placeholder.
  cardSlot?: React.ReactNode;
}

export function Step4Offer({
  selectedTier,
  setSelectedTier,
  onComplete,
  setActionBar,
  onOpenTerms,
  onOpenSample,
  liveOffer,
  cardSlot,
}: Step4OfferProps) {
  const live = liveOffer !== undefined;
  // Guardrail: nothing pre-selected (selectedTier initially null)
  const [isAuthorizing, setIsAuthorizing] = useState<boolean>(false);

  const certifiedPrice = live ? (liveOffer?.certifiedCents == null ? null : liveOffer.certifiedCents / 100) : CERTIFIED_CENTS / 100;
  // One flat certified price; the standard tier is never offered.
  const price = certifiedPrice;
  const tierName = "Certified Mail";

  useEffect(() => {
    if (!selectedTier) {
      setActionBar(
        "Nothing is pre-selected · Tap a mailing option above",
        <button className="btn btn-primary" type="button" disabled>
          {`Choose Certified Mail ($${certifiedPrice})`}
        </button>
      );
      return;
    }

    if (isAuthorizing) {
      setActionBar(
        <><Icon id="shield" size={14} /> {live ? "Placing the card hold…" : "Placing the card hold (simulated)…"}</>,
        <button className="btn btn-primary" type="button" disabled>
          <span className="spinner" /> Authorizing…
        </button>
      );
      return;
    }

    setActionBar(
      <><Icon id="shield" size={14} /> No charge unless your letter is mailed</>,
      <button
        className="btn btn-primary"
        type="button"
        id="actAuthBtn"
        disabled={price == null}
        onClick={() => {
          if (isAuthorizing) return;
          setIsAuthorizing(true);
          if (live) void Promise.resolve(onComplete(selectedTier)).finally(() => setIsAuthorizing(false));
        }}
      >
        {live ? `Authorize $${price}${liveOffer?.testMode ? " (test card)" : ""} →` : `Authorize $${price} (simulated) →`}
      </button>
    );
  }, [selectedTier, isAuthorizing, price, onComplete, setActionBar, live, liveOffer?.testMode, certifiedPrice]);

  useEffect(() => {
    if (live || !isAuthorizing || !selectedTier) return;
    const timer = setTimeout(() => {
      void Promise.resolve(onComplete(selectedTier)).finally(() => setIsAuthorizing(false));
    }, 900);
    return () => clearTimeout(timer);
  }, [live, isAuthorizing, selectedTier, onComplete]);

  return (
    <section className="screen on" id="s4" aria-labelledby="h-s4">
      <div className="voice">
        <ConciergeMark size={44} state={isAuthorizing ? "thinking" : "idle"} />
        <div>
          <p className="eyebrow">Step 3 · Mailing choice</p>
          <h2 id="h-s4" tabIndex={-1}>
            Your timeline and mailing choice.
          </h2>
          <p className="thought" aria-live="polite">
            {isAuthorizing ? (live ? "Placing the hold…" : "Placing the hold (simulated)…") : ""}
          </p>
        </div>
      </div>

      {live ? (
        <div className="card" id="issuerCard">
          <p className="small" style={{ fontWeight: 750 }}>✓ Agency confirmed from its official website</p>
          <p className="dl-big">
            <strong>{liveOffer?.issuerName}</strong>
          </p>
          <p className="muted small">
            Your letter goes to {liveOffer?.decidingBody}. After the hold, we read the agency&#39;s
            official appeal pages and show you every fact with its exact quote.
          </p>
        </div>
      ) : (
        <div className="card">
          <span className="prototype-label" style={{ marginBottom: "6px" }}>
            Simulated for this prototype
          </span>
          <p className="dl-big">
            Your deadline: <strong>Oct 23</strong>.<br />
            Mail certified by <strong>Oct 20</strong>.
          </p>
          <Timeline />
          <p className="small" style={{ marginTop: "14px" }}>
            <b>The math:</b> issued Oct 2 + 21 calendar days = <b>Oct 23</b>. The postmark date is
            what counts.
          </p>
          <div className="qline">
            <q>
              {SAMPLE_FACTS.find((fact) => fact.id === "f4")?.quote}
            </q>
            <small>
              City of San Diego · sandiego.gov/parking/citations/appeal · saved Oct 12, 2026 ·
              Simulated for this prototype
            </small>
          </div>
        </div>
      )}

      <fieldset style={{ marginTop: "16px" }} id="tierset">
        <legend>
          <h3>How should your letter be mailed?</h3>
          <span className="muted small">
            Nothing is selected for you. Your price includes printing and mailing your exact approved letter.
          </span>
        </legend>
        <div className="tiers">
          <label
            className={`tier ${selectedTier === "certified" ? "sel" : ""}`}
            data-tier="certified"
            onClick={() => setSelectedTier("certified")}
          >
            <input
              type="radio"
              name="tier"
              value="certified"
              checked={selectedTier === "certified"}
              onChange={() => setSelectedTier("certified")}
            />
            <span className="tag">Recommended: tracked</span>
            <span className="top2">
              <span className="name">Certified Mail, everything included</span>
              <span className="rad" aria-hidden="true" />
            </span>
            <span className="price">
              ${certifiedPrice ?? "unavailable"} <small style={{ display: "block", marginTop: "4px" }}>one time · early beta</small>
            </span>
            <ul>
              <li>
                <Icon id="check" size={14} style={{ color: "var(--sage)" }} />
                Preparation, printing and postage
              </li>
              <li>
                <Icon id="check" size={14} style={{ color: "var(--sage)" }} />
                Available USPS tracking updates
              </li>
              <li>
                <Icon id="check" size={14} style={{ color: "var(--sage)" }} />
                PDF copy of your exact mailed packet
              </li>
            </ul>
          </label>

        </div>
      </fieldset>

      <p className="muted small">
        Early beta pricing includes preparing, printing and sending your approved packet by Certified Mail. We’re refining the service and resolving issues as they arise. We’ll review standard pricing once operations are consistently reliable.
      </p>

      <p className="nocharge">
        <Icon id="shield" size={18} />
        No charge unless your letter is mailed.
      </p>

      <LegalDisclosure id="offerAttorneyDisclosure" />

      <div className="sheet-links">
        <button className="chip" type="button" onClick={onOpenTerms}>
          Hold &amp; refund terms
        </button>
        <button className="chip" type="button" onClick={onOpenSample}>
          Letter layout sample
        </button>
      </div>

      {selectedTier && (
        <div className="card" id="pay" style={{ marginTop: "14px" }}>
          <h3 id="payH">Authorize ${price} for {tierName}</h3>
          <p className="muted small" id="payP" style={{ marginTop: "4px" }}>
            We’ll place a ${price} hold on your card. Your card is charged only after the first USPS scan, or confirmed mailing production if scans are delayed. If mailing stops first, we check the saved payment status and release any remaining hold. Our mailing mistakes are ours to fix under the return policy.
          </p>
          {cardSlot ? cardSlot : <div className="stripe">
            <Icon id="lock" size={24} style={{ color: "var(--ink-3)", flex: "none" }} />
            <div>
              <b>Secure card form by Stripe</b>
              <br />
              <span className="muted small">
                {live
                  ? "Test mode: a Stripe test card is used; no real card is charged."
                  : "Prototype placeholder. There is no card field on this page."}
              </span>
            </div>
          </div>}
        </div>
      )}
    </section>
  );
}
