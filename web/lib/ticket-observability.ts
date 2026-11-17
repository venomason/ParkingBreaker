"use client";

import { useEffect, useRef, useState } from "react";
import { api, type TicketReviewObservationRequest, type TicketReviewSession } from "./api";

type Variant = TicketReviewSession["variant"];
type Status = TicketReviewObservationRequest["reading_status"];
type EventKind = TicketReviewObservationRequest["event_kind"];
type Outcome = TicketReviewObservationRequest["outcome"];
type Sample = Omit<TicketReviewObservationRequest, "treatment_variant" | "treatment_version">;
const MAX_EVENTS = 64;
const DAY_MS = 86_400_000;
export const reviewNonce = () => crypto.randomUUID().replaceAll("-", "");

/** Disjoint elapsed intervals, never inferred attention or time spent thinking.
 * Hidden time takes precedence over busy time. Each sample drains the interval;
 * consumers may sum samples, but must not infer an unobserved final interval.
 */
export class ReviewSpans {
  private last: number;
  private visible = 0;
  private busyTime = 0;
  private hiddenTime = 0;
  private changes = 0;
  constructor(private clock: () => number, private busy = false, private hidden = false) {
    this.last = clock();
  }
  private advance() {
    const now = this.clock();
    const elapsed = Math.max(0, Math.min(DAY_MS, now - this.last));
    this.last = now;
    if (this.hidden) this.hiddenTime += elapsed;
    else if (this.busy) this.busyTime += elapsed;
    else this.visible += elapsed;
  }
  state(busy: boolean, hidden: boolean) {
    this.advance();
    if (hidden !== this.hidden) this.changes += 1;
    this.busy = busy;
    this.hidden = hidden;
  }
  sample() {
    this.advance();
    const result = {
      visible_elapsed_ms: Math.min(DAY_MS, Math.round(this.visible)),
      busy_elapsed_ms: Math.min(DAY_MS, Math.round(this.busyTime)),
      hidden_elapsed_ms: Math.min(DAY_MS, Math.round(this.hiddenTime)),
      visibility_changes: Math.min(10_000, this.changes),
      timing_state: "closed" as const,
    };
    this.visible = this.busyTime = this.hiddenTime = this.changes = 0;
    return result;
  }
}

/** Bounded best effort delivery. Retries reuse the exact body and idempotency ID.
 * No customer field values, URLs, tokens, or error text enter these samples.
 */
export class ReviewRecorder {
  private session: TicketReviewSession | null = null;
  private pending: Sample[] = [];
  private sent = 0;
  private flushing = false;
  constructor(private send: (session: string, body: TicketReviewObservationRequest) => Promise<unknown>) {}
  assign(session: TicketReviewSession) { this.session = session; void this.flush(); }
  push(sample: Sample) {
    if (this.sent + this.pending.length >= MAX_EVENTS) return;
    this.pending.push(sample);
    void this.flush();
  }
  async flush() {
    if (this.flushing || !this.session) return;
    this.flushing = true;
    try {
      while (this.pending.length && this.session) {
        const sample = this.pending.shift()!;
        this.sent += 1;
        const body = { ...sample, treatment_variant: this.session.variant,
          treatment_version: this.session.effective_treatment_version };
        for (let attempt = 0; attempt < 2; attempt += 1) {
          try { await this.send(this.session.session_id, body); break; }
          catch { /* Missing delivery remains unknown; never block customer actions. */ }
        }
      }
    } finally { this.flushing = false; }
  }
}

function sessionNonce(workspace: string): string {
  const key = "pb-review-session:" + workspace;
  try {
    const old = sessionStorage.getItem(key);
    if (old && /^[0-9a-f]{32}$/.test(old)) return old;
    const nonce = reviewNonce();
    sessionStorage.setItem(key, nonce);
    return nonce;
  } catch { return reviewNonce(); }
}

export function useTicketReviewObservation(context: {
  workspaceId?: string | null; busy: boolean; readingStatus: Status; reviewStatus: Status;
}) {
  const [assignment, setAssignment] = useState<{ workspace: string; session: TicketReviewSession; variant: Variant } | null>(null);
  const current = useRef(context);
  const interacted = useRef(false);
  const active = useRef<{ workspace: string; recorder: ReviewRecorder; spans: ReviewSpans } | null>(null);
  const rendered: Variant = assignment && assignment.workspace === context.workspaceId ? assignment.variant : "baseline";
  const renderedRef = useRef<Variant>(rendered);

  useEffect(() => { current.current = context; renderedRef.current = rendered; });
  useEffect(() => {
    const workspace = context.workspaceId;
    if (!workspace) return;
    let cancelled = false;
    interacted.current = false;
    const recorder = new ReviewRecorder((id, body) => api.observeTicketReview(workspace, id, body));
    const spans = new ReviewSpans(() => performance.now(), current.current.busy, document.hidden);
    const run = { workspace, recorder, spans };
    active.current = run;
    const nonce = sessionNonce(workspace);
    void (async () => {
      for (let attempt = 0; attempt < 2 && !cancelled; attempt += 1) {
        try {
          const session = await api.startTicketReviewSession(workspace, nonce);
          if (cancelled) return;
          recorder.assign(session);
          // Guidance must not appear halfway through an edit. Record actual exposure
          // separately from eligibility so suppressed assignments aren't counted as seen.
          setAssignment({ workspace, session, variant: interacted.current ? "baseline" : session.variant });
          return;
        } catch { /* Keep the baseline usable even when observation storage is unavailable. */ }
      }
    })();
    const visibility = () => {
      spans.state(current.current.busy, document.hidden);
      recorder.push({ ...spans.sample(), client_observation_id: reviewNonce(),
        event_kind: "timing_checkpoint", outcome: "observed", rendered_variant: renderedRef.current,
        invalid_field_count: 0, customer_supplied: false,
        reading_status: current.current.readingStatus, review_status: current.current.reviewStatus });
    };
    document.addEventListener("visibilitychange", visibility);
    return () => {
      cancelled = true;
      document.removeEventListener("visibilitychange", visibility);
      if (active.current === run) active.current = null;
      // No fabricated final/abandonment event. Browser exit delivery is not guaranteed.
    };
  }, [context.workspaceId]);
  useEffect(() => { active.current?.spans.state(context.busy, document.hidden); }, [context.busy]);

  function observe(event: EventKind, outcome: Outcome = "observed", invalid = 0, supplied = false) {
    const run = active.current;
    if (!run || run.workspace !== context.workspaceId) return;
    run.recorder.push({ ...run.spans.sample(), client_observation_id: reviewNonce(),
      event_kind: event, outcome, rendered_variant: rendered,
      invalid_field_count: Math.min(13, invalid), customer_supplied: supplied,
      reading_status: context.readingStatus, review_status: context.reviewStatus });
  }
  useEffect(() => {
    if (!assignment || assignment.workspace !== context.workspaceId) return;
    const run = active.current;
    if (!run || run.workspace !== assignment.workspace) return;
    run.recorder.push({ ...run.spans.sample(), client_observation_id: reviewNonce(),
      event_kind: "exposure", outcome: "observed", rendered_variant: assignment.variant,
      invalid_field_count: 0, customer_supplied: false,
      reading_status: current.current.readingStatus, review_status: current.current.reviewStatus });
  }, [assignment, context.workspaceId]);
  return { variant: rendered, observe, interact: () => { interacted.current = true; } };
}
