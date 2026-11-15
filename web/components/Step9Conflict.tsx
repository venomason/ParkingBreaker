"use client";

import React, { useEffect } from "react";
import { ConciergeMark } from "./ConciergeMark";
import { Icon } from "./Icons";
import { SAMPLE_ALTERNATIVES, SAMPLE_FACTS } from "../lib/api";

interface Step9ConflictProps {
  optedIn: boolean;
  setOptedIn: (value: boolean) => void;
  onBackToResearch: () => void;
  setActionBar: (sub: React.ReactNode, buttons: React.ReactNode) => void;
}

export function Step9Conflict({
  optedIn,
  setOptedIn,
  onBackToResearch,
  setActionBar,
}: Step9ConflictProps) {

  useEffect(() => {
    setActionBar(
      "Conflicting instructions example",
      <button
        className="btn btn-primary"
        type="button"
        data-go={3}
        onClick={onBackToResearch}
      >
        Back to research →
      </button>
    );
  }, [onBackToResearch, setActionBar]);

  return (
    <section className="screen on" id="s9" aria-labelledby="h-s9">
      <span className="alt-tag">Alternative research example · separate from San Diego sample</span>
      <span className="prototype-label">Simulated for this prototype</span>
      <div className="voice" style={{ marginTop: "8px" }}>
        <ConciergeMark size={44} state="calm" />
        <div>
          <p className="eyebrow">When two instructions differ</p>
          <h2 id="h-s9" tabIndex={-1}>
            Your ticket back and the website disagree.
          </h2>
          <p className="lead">
            We show both exact passages and pause the mailing decision while the deadline is
            unresolved.
          </p>
        </div>
      </div>
      <div className="quote-pair">
        <article className="card">
          <h3>Ticket back · customer notice</h3>
          <blockquote>“{SAMPLE_ALTERNATIVES.ticketBackQuote}”</blockquote>
          <small>Synthetic citation back · sample issued Oct 2, 2026 · entered Oct 15, 2026. Simulated for this prototype.</small>
        </article>
        <article className="card">
          <h3>City website · official page</h3>
          <blockquote>“{SAMPLE_FACTS.find((fact) => fact.id === "f4")?.quote}”</blockquote>
          <small>City of San Diego · Administrative Review by Mail · sandiego.gov/parking/citations/appeal · saved Oct 12, 2026. Simulated for this prototype.</small>
        </article>
      </div>
      <div className="guard">
        <Icon id="shield" size={20} />
        <span>
          The ticket back is this customer’s printed notice. We keep its wording, show the
          conflict, and do not turn the difference into a legal claim. No card hold or mailing would
          proceed while a required deadline is unresolved.
        </span>
      </div>
      <label className="optin">
        <input
          type="checkbox"
          id="conflictOpt"
          checked={optedIn}
          onChange={(e) => setOptedIn(e.target.checked)}
        />
        <span>
          <b>Include this factual sentence if this example is resolved and a letter is prepared</b>
          <em>
            The instructions printed on my citation state: &apos;{SAMPLE_ALTERNATIVES.ticketBackQuote}&apos;
          </em>
        </span>
      </label>
      <p className="conflict-preview" id="conflictPreview" role="status">
        {optedIn
          ? "Opted in for this separate example. The factual sentence would appear only if the deadline conflict were resolved and you approved a letter."
          : "Sentence not included. This opt-in does not resolve the conflicting deadline."}
      </p>
    </section>
  );
}
