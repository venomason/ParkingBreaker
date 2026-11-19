"use client";

import React, { useState, useEffect } from "react";
import { ConciergeMark } from "./ConciergeMark";
import { FactRow, FactItem } from "./FactRow";
import { SAMPLE_RESEARCH as R_SEQ, SAMPLE_FACTS } from "../lib/api";

interface Step3ResearchProps {
  onProgress: (confirmed: number) => void;
  onComplete: () => void;
  setActionBar: (sub: React.ReactNode, buttons: React.ReactNode) => void;
  onOpenFact: (fact: FactItem) => void;
  onOpenChecklist: () => void;
  onGoConflict: () => void;
}

export function Step3Research({
  onProgress,
  onComplete,
  setActionBar,
  onOpenFact,
  onOpenChecklist,
  onGoConflict,
}: Step3ResearchProps) {
  const [shownCount, setShownCount] = useState<number>(0);
  const isDone = shownCount >= R_SEQ.length;
  const [fast, setFast] = useState<boolean>(false);



  useEffect(() => {
    if (shownCount < R_SEQ.length) {
      const step = R_SEQ[shownCount];
      const timer = setTimeout(
        () => {
          setShownCount((c) => c + 1);
          onProgress(SAMPLE_FACTS.slice(0, shownCount + 1).filter((fact) => fact.st === "ok").length);
        },
        fast ? 40 : step.delay
      );
      return () => clearTimeout(timer);
    }
  }, [shownCount, fast, onProgress]);

  const shownFacts = SAMPLE_FACTS.slice(0, shownCount);
  const cOk = shownFacts.filter((f) => f.st === "ok").length;
  const cUnk = shownFacts.filter((f) => f.st === "unk").length;
  const cCon = shownFacts.filter((f) => f.st === "con").length;

  // Real tier-dot matrix computation (Fix 6)
  // Dots light up per tier as facts arrive
  const mustFacts = shownFacts.filter((f) => f.tier === "must");
  const impFacts = shownFacts.filter((f) => f.tier === "imp");
  const niceFacts = shownFacts.filter((f) => f.tier === "nice");

  useEffect(() => {
    if (!isDone) {
      const currentMsg = R_SEQ[Math.min(shownCount, R_SEQ.length - 1)]?.msg || "Reading rules…";
      setActionBar(
        `Verifying official rules (${shownCount + 1} of ${R_SEQ.length})…`,
        <button className="btn btn-primary" type="button" disabled>
          <span className="spinner" /> {currentMsg}
        </button>
      );
    } else {
      setActionBar(
        "Review the agency’s mailing instructions before continuing",
        <button
          className="btn btn-primary"
          type="button"
          data-go={4}
          onClick={onComplete}
        >
          Preview deadline and options →
        </button>
      );
    }
  }, [isDone, shownCount, onComplete, setActionBar]);

  return (
    <section className="screen on" id="s3" aria-labelledby="h-s3">
      <div className="voice">
        <ConciergeMark size={44} state={isDone ? "done" : "thinking"} />
        <div>
          <p className="eyebrow">Step 4 · Appeal rules</p>
          <h2 id="h-s3" tabIndex={-1}>
            San Diego’s appeal rules
          </h2>
          <span className="prototype-label">Simulated for this prototype</span>
          <p className="lead" id="s3say">
            Read the agency’s filing instructions for this sample appeal. You decide what to say in your letter.
          </p>
        </div>
      </div>

      <div className="card">
        <div className={`r-live-bar ${isDone ? "done" : ""}`} id="rLiveBar">
          <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
            <span className="r-matrix" aria-label="Tier dot matrix">
              <span className={`rm-dot ${mustFacts.length > 0 ? "on-must" : ""}`} />
              <span className={`rm-dot ${impFacts.length > 0 ? "on-imp" : ""}`} />
              <span className={`rm-dot ${niceFacts.length > 0 ? "on-nice" : ""}`} />
            </span>
            <span id="rLiveText">
              {isDone
                ? "✓ Done · Tap any fact row to see its verbatim quote"
                : R_SEQ[Math.min(shownCount, R_SEQ.length - 1)]?.msg}
            </span>
          </div>
          {!isDone && (
            <button
              className="chip"
              type="button"
              id="skipR"
              style={{ minHeight: "44px", padding: "6px 12px", fontSize: "12px" }}
              onClick={() => setFast(true)}
            >
              Skip ahead
            </button>
          )}
        </div>

        <div className="counts" id="counts">
          <span>{shownFacts.length} of {SAMPLE_FACTS.length} checks</span>
          <span style={{ color: "var(--sage)" }}>{cOk} confirmed</span>
          <span>{cUnk} unknown</span>
          <span>{cCon} conflicted</span>
        </div>

        <p className="muted small" style={{ marginBottom: "10px" }}>
          Tap any row below to inspect the verbatim quote, official source link and date.
        </p>

        <div className="fact-stream" id="factStream" role="list">
          {shownFacts.map((f) => (
            <FactRow key={f.id} fact={f} onClick={() => onOpenFact(f)} />
          ))}
        </div>
      </div>

      {isDone && (
        <div className="card summary" id="rsum">
          <h3>Mailing this sample appeal</h3>
          <ul id="rsumList">
            <li><b>6 Must-have items shown:</b> confirmed in this saved sample passage.</li>
            <li>
              <b>Important:</b> the agency mentions supporting documentation.
            </li>
            <li><b>Nice-to-have:</b> the city asks for brevity.</li>
            <li><b>Mail to:</b> P.O. Box 129038, San Diego, CA 92112-9038.</li>
          </ul>
          <p className="muted small" style={{ marginBottom: "10px" }}>
            <b>Plain-English veto:</b> Not stated on the city&apos;s page — we read the linked form
            too and found no extra requirement.
          </p>
          <div className="row">
            <button className="chip" type="button" data-go={9} onClick={onGoConflict}>
              See ticket-back conflict example
            </button>
            <button className="chip" type="button" onClick={onOpenChecklist}>
              Open full checklist
            </button>
          </div>
        </div>
      )}
    </section>
  );
}
