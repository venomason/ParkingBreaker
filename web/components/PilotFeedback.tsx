"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { api, ApiError, type PilotFeedbackAnswers, type PilotFeedbackRequest, type PilotFeedbackSnapshot } from "../lib/api";

const QUESTIONS = {
  routing: {
    label: "Did the mailing have a routing problem?",
    options: {
      returned: "It was returned",
      wrong_office: "It went to the wrong office",
      redirected: "It was sent on to another office",
      no_known_problem: "No problem that I know of",
      unknown: "I don’t know",
    },
  },
  acknowledgment: {
    label: "Did the agency acknowledge the appeal?",
    options: {
      acknowledged: "Yes, the agency confirmed receipt",
      more_info_requested: "The agency asked for more information",
      pending: "No response yet",
      unknown: "I don’t know",
    },
  },
  disposition: {
    label: "What did the agency decide?",
    options: {
      dismissed: "Dismissed the ticket",
      reduced: "Reduced the penalty",
      upheld: "Upheld the ticket",
      filing_rejected: "Rejected the appeal for a filing issue",
      pending: "Waiting for a decision or unsure",
    },
  },
} satisfies {
  [Field in keyof PilotFeedbackAnswers]: { label: string; options: Record<NonNullable<PilotFeedbackAnswers[Field]>, string> };
};

function answersFrom(snapshot: PilotFeedbackSnapshot): PilotFeedbackAnswers {
  return {
    routing: snapshot.routing ?? null,
    acknowledgment: snapshot.acknowledgment ?? null,
    disposition: snapshot.disposition ?? null,
  };
}

function FeedbackForm({ initial, save, changed, onStale }: {
  initial: PilotFeedbackSnapshot;
  save: (request: PilotFeedbackRequest) => Promise<PilotFeedbackSnapshot>;
  changed: boolean;
  onStale: () => void;
}) {
  const [saved, setSaved] = useState(initial);
  const [answers, setAnswers] = useState(() => answersFrom(initial));
  const [pending, setPending] = useState<PilotFeedbackRequest | null>(null);
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState(false);
  const [showLatest, setShowLatest] = useState(changed);
  const [error, setError] = useState<string | null>(null);
  const inFlight = useRef(false);
  const changes = {
    ...(answers.routing && answers.routing !== saved.routing ? { routing: answers.routing } : {}),
    ...(answers.acknowledgment && answers.acknowledgment !== saved.acknowledgment ? { acknowledgment: answers.acknowledgment } : {}),
    ...(answers.disposition && answers.disposition !== saved.disposition ? { disposition: answers.disposition } : {}),
  };
  const dirty = Object.keys(changes).length > 0;

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (inFlight.current || (!pending && !dirty)) return;
    const request = pending ?? { request_id: crypto.randomUUID(), expected_revision: saved.revision, ...changes };
    inFlight.current = true;
    setPending(request);
    setBusy(true);
    setSent(false);
    setError(null);
    try {
      const confirmed = await save(request);
      setSaved(confirmed);
      setAnswers(answersFrom(confirmed));
      setPending(null);
      setSent(true);
    } catch (failure) {
      if (failure instanceof ApiError && failure.status === 409 && failure.detail === "pilot_feedback_revision_changed") {
        onStale();
      } else if (failure instanceof ApiError && failure.status >= 400 && failure.status < 500) {
        setPending(null);
        setError("Your answers could not be saved. Check your access or try again later.");
      } else {
        setError("We couldn’t confirm that your answers were saved. Retry to check this same submission.");
      }
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  function question<Field extends keyof PilotFeedbackAnswers>(field: Field) {
    const { label, options } = QUESTIONS[field];
    return <label key={field}>
      {label}
      <select
        value={answers[field] ?? ""}
        disabled={busy || pending !== null}
        onChange={(event) => {
          const value = event.target.value as NonNullable<PilotFeedbackAnswers[Field]>;
          setAnswers((previous) => ({ ...previous, [field]: value }));
          setSent(false);
          setShowLatest(false);
        }}
      >
        <option value="" disabled>Choose an answer</option>
        {Object.entries(options).map(([value, description]) => <option key={value} value={value}>{description}</option>)}
      </select>
    </label>;
  }

  return <form className="pilot-feedback-form stack" onSubmit={submit} aria-busy={busy}>
    {showLatest && <p role="status">Your saved answers changed. Review the latest answers before submitting another update.</p>}
    <p className="small muted">All three questions are optional. We save the answers you choose to submit as your report about this appeal.</p>
    {question("routing")}
    {question("acknowledgment")}
    {question("disposition")}
    <button className="btn btn-primary" type="submit" disabled={busy || (!pending && !dirty)}>
      {busy ? "Saving…" : pending ? "Retry saving answers" : "Save answers"}
    </button>
    {sent && <p role="status">Thanks. Your answers were saved.</p>}
    {error && <p role="alert">{error}</p>}
  </form>;
}

export function PilotFeedback({ caseId, token }: { caseId: string; token?: string }) {
  const [snapshot, setSnapshot] = useState<PilotFeedbackSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  const [changed, setChanged] = useState(false);
  useEffect(() => {
    let current = true;
    const read = token ? api.pilotFeedbackFromLink({ case_id: caseId, token }) : api.pilotFeedback(caseId);
    void read.then((status) => {
      if (current) setSnapshot(status);
    }, (failure: unknown) => {
      if (current) setError(token && failure instanceof ApiError && [400, 403, 404].includes(failure.status)
        ? "This feedback link is unavailable. Check the latest message for your appeal."
        : "We couldn’t check whether feedback is available. Try again.");
    });
    return () => { current = false; };
  }, [caseId, token, revision]);

  function refresh() {
    setError(null);
    setSnapshot(null);
    setRevision((previous) => previous + 1);
  }

  if (!snapshot || !snapshot.eligible) {
    if (!snapshot && !error) return <p className="small muted" role="status">Checking feedback availability…</p>;
    return <div className="stack">
      {error
        ? <p role="alert">{error}</p>
        : <p className="small muted">You can share feedback once the original mailing is confirmed.</p>}
      <button className="btn btn-ghost" type="button" onClick={refresh}>Check feedback availability</button>
    </div>;
  }
  return <FeedbackForm
    initial={snapshot}
    changed={changed}
    onStale={() => { setChanged(true); refresh(); }}
    save={(request) => token ? api.recordPilotFeedbackFromLink({ case_id: caseId, token }, request) : api.recordPilotFeedback(caseId, request)}
  />;
}
