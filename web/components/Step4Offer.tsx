"use client";

import { NONMAILING_REFUND, PAID_CAPTURE_TERMS } from "../lib/legal";
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
  onComplete: (tier: "certified" | "standard", termsAccepted: boolean) => void | Promise<void>;
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
  const [isAuthorizing, setIsAuthorizing] = useState<boolean>(false);
  const [consentFor, setConsentFor] = useState<string | null>(null);

  const certifiedPrice = live ? (liveOffer?.certifiedCents == null ? null : liveOffer.certifiedCents / 100) : CERTIFIED_CENTS / 100;
  const price = certifiedPrice;
  const tierName = "Certified Mail";

  const termsAccepted = selectedTier !== null && consentFor === selectedTier;

  useEffect(() => {
    if (!selectedTier) {
      setActionBar(
        "Card hold first. See when we charge below.",
        <button
          className="btn btn-primary"
          id="actChooseCertified"
          type="button"
          disabled={certifiedPrice == null}
          onClick={() => setSelectedTier("certified")}
        >
          {certifiedPrice == null ? "Certified Mail unavailable" : `Choose Certified Mail · $${certifiedPrice.toFixed(2)}`}
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
      <><Icon id="shield" size={14} /> {termsAccepted ? "Card hold first. See when we charge below." : "Agree to the mailing terms above to continue"}</>,
      <button
        className="btn btn-primary"
        type="button"
        id="actAuthBtn"
        disabled={price == null || !termsAccepted}
        aria-disabled={price == null || !termsAccepted}
        onClick={() => {
          if (isAuthorizing || !termsAccepted) return;
          setIsAuthorizing(true);
          if (live) void Promise.resolve(onComplete(selectedTier, termsAccepted)).finally(() => setIsAuthorizing(false));
        }}
      >
        {live ? `Authorize $${price}${liveOffer?.testMode ? " (test card)" : ""} →` : `Authorize $${price} (simulated) →`}
      </button>
    );
  }, [selectedTier, isAuthorizing, price, onComplete, setActionBar, live, liveOffer?.testMode, certifiedPrice, setSelectedTier, termsAccepted]);

  useEffect(() => {
    if (live || !isAuthorizing || !selectedTier || !termsAccepted) return;
    const timer = setTimeout(() => {
      void Promise.resolve(onComplete(selectedTier, termsAccepted)).finally(() => setIsAuthorizing(false));
    }, 900);
    return () => clearTimeout(timer);
  }, [live, isAuthorizing, selectedTier, onComplete, termsAccepted]);

  return (
    <section className="screen on" id="s4" aria-labelledby="h-s4">
      <div className="voice">
        <ConciergeMark size={44} state={isAuthorizing ? "thinking" : "idle"} />
        <div>
          <p className="eyebrow">Step 3 · Mailing choice</p>
          <h2 id="h-s4" tabIndex={-1}>
            Choose how to mail your appeal.
          </h2>
          <p className="lead">
            Nothing is pre-selected. You inspect and approve the exact PDF packet before anything is mailed.
          </p>
          <p className="thought" aria-live="polite">
            {isAuthorizing ? (live ? "Placing the hold…" : "Placing the hold (simulated)…") : ""}
          </p>
        </div>
      </div>

      {live ? (
        <div className="card is-saved-card postal-frank-card" id="issuerCard">
          <div className="section-head-row">
            <span className="status-pill is-saved"><Icon id="check" size={12} /> Agency confirmed from official site</span>
            <span className="postmark-stamp" aria-hidden="true">USPS MAIL</span>
          </div>
          <p className="dl-big">
            <strong>{liveOffer?.issuerName}</strong>
          </p>
          <p className="muted small">
            After you authorize a temporary card hold, we verify the rules for filing your appeal. You review the exact packet before mailing.
          </p>
        </div>
      ) : (
        <div className="card postal-frank-card">
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
            The price includes clerical preparation, printing, and USPS postage.
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
            <span className="tag">Recommended · USPS tracked</span>
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
                PDF copy of your exact approved packet
              </li>
            </ul>
          </label>
        </div>
      </fieldset>

      <p className="nocharge">
        <Icon id="shield" size={18} />
        You authorize a hold before reviewing your packet.
      </p>

      <LegalDisclosure id="offerAttorneyDisclosure" />
      <p id="paymentTerms">{PAID_CAPTURE_TERMS} {NONMAILING_REFUND}</p>

      <label className={`packet-inspect ${termsAccepted ? "is-checked" : ""}`} htmlFor="termsAccepted">
        <input
          id="termsAccepted"
          type="checkbox"
          checked={termsAccepted}
          onChange={(event) => setConsentFor(event.target.checked ? selectedTier : null)}
        />
        <span>
          I agree to the{" "}
          <a href="/terms" target="_blank" rel="noreferrer">Terms of Service</a>,{" "}
          <a href="/refund-policy" target="_blank" rel="noreferrer">Refund Policy</a> and{" "}
          <a href="/privacy" target="_blank" rel="noreferrer">Privacy Policy</a>, and to the hold
          and refund terms for this mailing.
        </span>
      </label>

      <div className="sheet-links" style={{ marginTop: "12px" }}>
        <button className="chip" type="button" onClick={onOpenTerms}>
          Hold &amp; refund terms
        </button>
        <button className="chip" type="button" onClick={onOpenSample}>
          Letter layout sample
        </button>
      </div>

      {selectedTier && (
        <div className="card is-next-task" id="pay" style={{ marginTop: "14px" }}>
          <div className="section-head-row">
            <h3 id="payH">Authorize ${price} for {tierName}</h3>
            <span className="status-pill is-next"><Icon id="lock" size={12} /> Hold only</span>
          </div>
          <p className="muted small" id="payP" style={{ marginTop: "4px", marginBottom: "10px" }}>
            We’ll place a ${price} hold on your card.
          </p>
          {cardSlot ? cardSlot : (
            <div className="stripe">
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
            </div>
          )}
        </div>
      )}
    </section>
  );
}
