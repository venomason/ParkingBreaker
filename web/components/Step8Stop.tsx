"use client";

import React, { useEffect } from "react";
import { ConciergeMark } from "./ConciergeMark";
import { Icon } from "./Icons";
import { SAMPLE_ALTERNATIVES } from "../lib/api";

interface Step8StopProps {
  onCopyNotes: () => void;
  onOpenAgencyForm: () => void;
  onEditDetails?: () => void;
  onStartNew?: () => void;
  setActionBar: (sub: React.ReactNode, buttons: React.ReactNode) => void;
  // Real flow: the API's plain stop (message already says nothing was charged).
  liveStop?: { message: string; officialQuote: string | null; officialUrl?: string | null; savedWords?: string };
}

export function Step8Stop({
  onCopyNotes,
  onOpenAgencyForm,
  onEditDetails,
  onStartNew,
  setActionBar,
  liveStop,
}: Step8StopProps) {
  useEffect(() => {
    if (liveStop) {
      // Live stop renders its primary recovery controls directly inside the card so
      // they never duplicate right above a floating bar on small screens.
      setActionBar(null, null);
      return;
    }
    setActionBar(
      "Stopped safely · Nothing was charged",
      <>
        <button
          className="btn btn-ghost"
          type="button"
          id="actOpenForm"
          onClick={onOpenAgencyForm}
        >
          Open their official page
        </button>
        <button
          className="btn btn-primary"
          type="button"
          id="actCopyNotes"
          onClick={onCopyNotes}
        >
          Copy my notes
        </button>
      </>
    );
  }, [onCopyNotes, onOpenAgencyForm, setActionBar, liveStop]);

  if (liveStop) {
    return (
      <section className="screen on" id="s8" aria-labelledby="h-s8">
        <div className="voice">
          <ConciergeMark size={44} state="calm" />
          <div>
            <p className="eyebrow">Safe stop · No charge</p>
            <h2 id="h-s8" tabIndex={-1}>
              We need to stop here. Nothing was charged.
            </h2>
            <p className="lead" id="liveStopMessage">{liveStop.message}</p>
          </div>
        </div>

        {(liveStop.officialUrl?.startsWith("https://") || onEditDetails || onStartNew) && (
          <div className="card is-next-task" style={{ marginBottom: "14px" }}>
            <p className="eyebrow">What you can do next</p>
            {liveStop.officialUrl?.startsWith("https://") && (
              <p style={{ marginBottom: "12px" }}>
                <a className="chip" href={liveStop.officialUrl} target="_blank" rel="noopener noreferrer">
                  <Icon id="globe" size={14} /> Open the issuer&#39;s official site
                </a>
              </p>
            )}
            {(onEditDetails || onStartNew) && (
              <div className="row" style={{ gap: "10px" }}>
                {onEditDetails && (
                  <button className="btn btn-primary" type="button" id="liveStopEditDetails" onClick={onEditDetails}>
                    Update my details →
                  </button>
                )}
                {onStartNew && (
                  <button className="btn btn-ghost" type="button" id="liveStopStartNew" onClick={onStartNew}>
                    Start a new appeal
                  </button>
                )}
              </div>
            )}
          </div>
        )}

        {liveStop.savedWords && (
          <div className="card state-card is-saved-card">
            <div className="section-head-row">
              <h3>Your saved words</h3>
              <span className="status-pill is-saved"><Icon id="check" size={12} /> Saved</span>
            </div>
            <p className="small muted">Your work remains available. You can copy it for your own records or the agency’s form.</p>
            <textarea className="saved-words-textarea" aria-label="Saved account or letter" value={liveStop.savedWords} readOnly />
            <button className="btn btn-ghost" type="button" onClick={onCopyNotes}>
              Copy saved words
            </button>
          </div>
        )}

        {liveStop.officialQuote && (
          <div className="card" style={{ marginTop: "14px" }}>
            <p className="eyebrow" style={{ color: "var(--ink-3)" }}>Their exact words</p>
            <div className="qline"><q>{liveStop.officialQuote}</q></div>
          </div>
        )}
      </section>
    );
  }

  return (
    <section className="screen on" id="s8" aria-labelledby="h-s8">
      <span className="alt-tag">Alternative path · a different agency</span>
      <span className="prototype-label">Simulated for this prototype</span>
      <div className="voice" style={{ marginTop: "8px" }}>
        <ConciergeMark size={44} state="calm" />
        <div>
          <p className="eyebrow">A plain stop</p>
          <h2 id="h-s8" tabIndex={-1}>
            We need to stop here. Nothing was charged.
          </h2>
          <p className="lead">
            This agency only accepts contests on its own form, and a mailed letter can’t include it.
            Here is their exact wording.
          </p>
        </div>
      </div>
      <div className="card">
        <p className="eyebrow" style={{ color: "var(--ink-3)" }}>
          Their exact words
        </p>
        <div className="qline">
          <q>
            {SAMPLE_ALTERNATIVES.stopQuote}
          </q>
          <small>
            Sample County Parking Office · Contest a Citation · samplecounty.gov/parking/contest ·
            retrieved Oct 14, 2026 (illustrative)
          </small>
        </div>
      </div>
      <ul className="stoplist" style={{ marginTop: "12px" }}>
        <li>
          <span className="si" style={{ background: "var(--sage-soft)", color: "var(--sage)" }}>
            <Icon id="shield" size={20} />
          </span>
          <span>
            <b>Nothing was charged.</b>{" "}
            <span className="muted small">We stopped before a card hold was placed.</span>
          </span>
        </li>
        <li>
          <span className="si" style={{ background: "var(--amber-soft)", color: "var(--amber-ink)" }}>
            <Icon id="doc" size={20} />
          </span>
          <span>
            <b>Your work is saved.</b>{" "}
            <span className="muted small">
              Your ticket details, your own words, your photo and the research stay in your account.
            </span>
          </span>
        </li>
        <li>
          <span className="si" style={{ background: "var(--coral-soft)", color: "var(--coral-ink)" }}>
            <Icon id="globe" size={20} />
          </span>
          <span>
            <b>What you can do.</b>{" "}
            <span className="muted small">
              Fill in Form PR-1 on the agency’s official page. You can copy your own words from your
              saved notes into it.
            </span>
          </span>
        </li>
      </ul>
      <div className="guard" style={{ marginTop: "12px" }}>
        <Icon id="shield" size={20} />
        <span>
          We stop rather than send something the agency says it won’t process. We won’t guess around
          their rules.
        </span>
      </div>
    </section>
  );
}
