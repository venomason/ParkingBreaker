"use client";

import React, { useState, useEffect } from "react";
import { ConciergeMark } from "./ConciergeMark";
import { HurtlePoseIllustration } from "./HurtleMascot";
import { Icon } from "./Icons";
import { sampleTrackingEvents } from "../lib/api";

interface Step7MailedProps {
  tier: "certified" | "standard";
  onDownloadEvidence: () => void;
  onDownloadReport: () => void;
  setActionBar: (sub: React.ReactNode, buttons: React.ReactNode) => void;
}

export function Step7Mailed({
  tier,
  onDownloadEvidence,
  onDownloadReport,
  setActionBar,
}: Step7MailedProps) {
  const isCert = tier !== "standard";
  const [shownEvents, setShownEvents] = useState<number>(0);
  const [outcome, setOutcome] = useState<string | null>(null);

  const evList = sampleTrackingEvents(isCert);

  useEffect(() => {
    if (shownEvents < evList.length) {
      const timer = setTimeout(() => {
        setShownEvents((c) => c + 1);
      }, 520);
      return () => clearTimeout(timer);
    }
  }, [shownEvents, evList.length]);

  const isDone = shownEvents >= evList.length;

  useEffect(() => {
    if (!isDone) {
      setActionBar(
        "Simulating USPS tracking updates…",
        <button className="btn btn-primary" type="button" disabled>
          <span className="spinner" /> Updating tracking…
        </button>
      );
    } else {
      setActionBar(
        "Complete evidence archive ready",
        <button
          className="btn btn-primary"
          type="button"
          id="actDlEvidence"
          onClick={onDownloadEvidence}
        >
          Download your evidence record (PDF)
        </button>
      );
    }
  }, [isDone, onDownloadEvidence, setActionBar]);

  return (
    <section className="screen on" id="s7" aria-labelledby="h-s7">
      <div className="voice">
        <ConciergeMark size={44} state={isDone ? "done" : "thinking"} />
        <div>
          <p className="eyebrow">Step 6 · Tracking</p>
          <h2 id="h-s7" tabIndex={-1}>
            Your letter is on its way.
          </h2>
          <p className="lead" id="s7lead">
            {isCert
              ? "Here’s every step, worded exactly as USPS reports it."
              : "Standard Mail doesn’t report tracking, so here’s what we can honestly tell you."}
          </p>
          <span className="prototype-label">Simulated for this prototype</span>
        </div>
      </div>

      <div className="card" style={{ marginBottom: "14px", padding: "12px", textAlign: "center" }}>
        <HurtlePoseIllustration pose="waiting" className="w-full max-w-[320px] mx-auto block" />
      </div>

      <div className="card">
        <ol className="tracking" id="tracking" aria-live="polite">
          {evList.slice(0, shownEvents).map((e, idx) => (
            <li key={idx} className={e.c}>
              <span className="td">
                {e.c === "deliv" && <Icon id="check" size={13} style={{ color: "#fff" }} />}
              </span>
              <div className="tm">
                <span className={`who ${e.c === "deliv" ? "usps" : e.c}`}>{e.w}</span>
                {e.d}
              </div>
              <div className="tt">{e.t}</div>
              <div className="ts">{e.s}</div>
            </li>
          ))}
        </ol>
      </div>

      {isDone && (
        <div id="s7rest">
          {isCert ? (
            <>
              <div className="guard" style={{ marginTop: "14px" }}>
                <Icon id="shield" size={18} />
                <span>
                  USPS reports delivered to the agency’s mailing address. USPS cannot tell us when
                  staff open or review it. The agency replies to you directly.
                </span>
              </div>
              <div className="card report" style={{ marginTop: "12px" }}>
                <span className="file">PDF</span>
                <div style={{ flex: 1 }}>
                  <h3>Delivery Confirmation report</h3>
                  <p className="muted small">
                    A ParkingBreaker report built from USPS tracking events. It is not a USPS Return
                    Receipt.
                  </p>
                </div>
                <button
                  className="chip"
                  type="button"
                  id="dlReport"
                  onClick={onDownloadReport}
                >
                  Download report
                </button>
              </div>
            </>
          ) : (
            <div className="guard" style={{ marginTop: "14px" }}>
              <Icon id="shield" size={18} />
              <span>
                First-Class Mail has no tracking, so we can’t report arrival. The agency replies to you
                directly.
              </span>
            </div>
          )}

          <p className="later">About three weeks later</p>
          <div className="card outcome" id="outcome">
            {outcome ? (
              <>
                <h3>Thanks. Noted: “{outcome}”.</h3>
                <p className="muted small" style={{ marginTop: "4px" }}>
                  Nothing else is needed from you. Your letter, report and notes stay in your
                  account.
                </p>
              </>
            ) : (
              <>
                <h3>Have you heard back from the City of San Diego?</h3>
                <p className="muted small" style={{ margin: "4px 0 12px" }}>
                  One tap. It helps us see how letters fare overall. Optional.
                </p>
                <div className="choices">
                  {[
                    "They cancelled the citation",
                    "They kept the citation",
                    "Not yet",
                    "Rather not say",
                  ].map((oc) => (
                    <button
                      key={oc}
                      className="btn btn-ghost btn-sm"
                      type="button"
                      data-oc={oc}
                      onClick={() => setOutcome(oc)}
                    >
                      {oc}
                    </button>
                  ))}
                </div>
              </>
            )}
          </div>
        </div>
      )}
    </section>
  );
}
