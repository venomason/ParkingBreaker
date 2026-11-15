"use client";

import React, { useCallback, useEffect, useState } from "react";
import type { CaseStatus, QuestionAnswer } from "../lib/api";
import { RequiredCopies } from "./RequiredCopies";
import { DestinationChoice } from "./DestinationChoice";
import { RequirementChecks } from "./CaseVisuals";

const LABELS: Record<string, string> = {
  CONFIRMED: "Confirmed",
  NOT_APPLICABLE: "Not applicable",
  UNKNOWN: "Not found",
  CONFLICTED: "Conflicting",
};

const TONES: Record<string, string> = {
  CONFIRMED: "ok",
  NOT_APPLICABLE: "na",
  UNKNOWN: "unk",
  CONFLICTED: "con",
};

function title(fact: string): string {
  return fact.charAt(0) + fact.slice(1).toLowerCase().replaceAll("_", " ");
}

/** The live research view: official pages opened and each fact with its exact quote. */
interface LiveResearchProps {
  status: CaseStatus | null;
  busy: boolean;
  error?: string | null;
  onAnswer?: (answer: QuestionAnswer) => void;
  onDraft: () => void;
  onRetry?: () => void;
  caseId?: string;
  onStatus?: (status: CaseStatus) => void;
  setActionBar: (sub: React.ReactNode, buttons: React.ReactNode) => void;
}

export function LiveResearch({ status, busy, onDraft, onRetry, caseId, onStatus, error, onAnswer, setActionBar }: LiveResearchProps) {
  const copiesScope = `${caseId}:${status?.required_copies?.research_sha256}:${status?.required_copies?.revision}`;
  const [copiesPending, setCopiesPending] = useState<{ scope: string; pending: boolean } | null>(null);
  const onCopiesPending = useCallback((pending: boolean) => setCopiesPending({ scope: copiesScope, pending }), [copiesScope]);
  const copiesBlocked = (copiesPending?.scope === copiesScope && copiesPending.pending) || Boolean(status?.required_copies && !status.required_copies.ready);
  const question = status?.question;
  const ready = status?.state === "DRAFTING" || status?.state === "AWAITING_ADOPTION";
  const choicePending = Boolean(status?.destination_choice);
  const answerQuestion = (answer: NonNullable<CaseStatus["question"]>["choices"][number]) => {
    if (!question) return;
    if (typeof answer === "string") {
      if (question.question_id === "VEHICLE_RELATIONSHIP" || !question.question_id) {
        onAnswer?.({ question_id: "VEHICLE_RELATIONSHIP", answer });
      }
    } else if (question.question_id && question.question_id !== "VEHICLE_RELATIONSHIP") {
      onAnswer?.({ question_id: question.question_id, answer });
    }
  };
  useEffect(() => {
    setActionBar(
      busy ? "Reading official pages…" : choicePending ? "Choose how to handle the mailing address first" : "Every fact comes with the agency's own words",
      <button className="btn btn-primary" type="button" id="liveDraftBtn" disabled={busy || !ready || choicePending || copiesBlocked || Boolean(question)} onClick={onDraft}>
        {busy ? <><span className="spinner" /> Working…</> : "Draft my letter →"}
      </button>,
    );
  }, [busy, status, choicePending, copiesBlocked, ready, question, onDraft, setActionBar]);
  return (
    <section className="screen on" id="s3" aria-labelledby="h-s3">
      <div className="voice">
        <div>
          <p className="eyebrow">Official research</p>
          <h2 id="h-s3" tabIndex={-1}>
            {busy ? "Reading the agency's official pages…" : "What the official pages say"}
          </h2>
          <p className="lead">Every fact below comes with the agency&#39;s own words and the page it came from.</p>
        </div>
      </div>
      {caseId && onStatus ? <DestinationChoice caseId={caseId} status={status} onStatus={onStatus} /> : null}
      {caseId && status && onStatus ? <RequiredCopies key={caseId} caseId={caseId} status={status} onStatus={onStatus} onPendingChange={onCopiesPending} /> : null}
      {question && <div className="card state-card" id="liveCustomerQuestion"><h3>{question.prompt}</h3><p className="small">Choose the answer that describes you. We use it only to check the agency’s procedural requirements.</p><div className="choices">{question.choices.map(answer => <button key={String(answer)} className="btn btn-ghost" type="button" disabled={busy || !onAnswer} onClick={() => answerQuestion(answer)}>{answer === true ? "Yes" : answer === false ? "No" : answer === null ? "I’m not sure" : ({REGISTERED_OWNER:"Registered owner",DRIVER_NOT_OWNER:"Driver, not the owner",BUSINESS_OR_FLEET_VEHICLE:"Business or fleet vehicle",RENTAL_OR_LEASE_RENTER:"Rental or lease renter",AUTHORISED_REPRESENTATIVE:"Authorized representative",OTHER:"Another relationship"})[answer]}</button>)}</div></div>}
      {!busy && !ready && !question && !choicePending && onRetry && <div className="card state-card"><p>Your agency check is not complete yet. Your entries remain saved.</p><button className="btn btn-primary" type="button" onClick={onRetry}>Retry agency check</button></div>}
      {error && <p className="err" role="alert">{error}</p>}
      <ResearchEvidence research={status?.research} />
    </section>
  );
}

export function ResearchEvidence({research, idPrefix = "live"}: {research: CaseStatus["research"] | undefined; idPrefix?: string}) {
  if (!research) return <p className="muted small">No saved official research is available for this appeal yet.</p>;
  return (
    <div className="research-detail">
      <RequirementChecks findings={research.findings} idPrefix={idPrefix} />
      <div className="card sources-card" id={`${idPrefix}Sources`}>
        <h3>Pages read</h3>
        <p className="small muted">Every quote below comes from one of these saved official pages.</p>
        {research.sources.length ? (
          <ul className="source-list small">
            {research.sources.map((source) => (
              <li key={source.url + source.retrieved_at}>
                <span className="source-url">{/^https?:\/\//.test(source.url) ? <a href={source.url} target="_blank" rel="noopener noreferrer">{source.url}</a> : source.url}</span>
                <span className="source-date">saved {new Date(source.retrieved_at).toLocaleString()}</span>
              </li>
            ))}
          </ul>
        ) : <p className="small muted">No official pages are saved for this appeal yet.</p>}
      </div>
      <ul className="findings" id={`${idPrefix}Findings`} aria-label="Requirement findings and exact official words">
        {research.findings.map((finding) => (
          <li key={finding.fact} className={`finding tone-${TONES[finding.status] ?? "unk"}`}>
            <div className="finding-head">
              <span className="finding-dot" aria-hidden="true" />
              <h4 className="finding-title">{title(finding.fact)}</h4>
              <span className={`bdg s-${TONES[finding.status] ?? "unk"}`}>{LABELS[finding.status] ?? finding.status}</span>
            </div>
            {finding.official_quote ? (
              <blockquote className="qline">
                <q>{finding.official_quote}</q>
              </blockquote>
            ) : (
              <p className="small muted">No quote was saved for this check. Not found, so not guessed.</p>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
