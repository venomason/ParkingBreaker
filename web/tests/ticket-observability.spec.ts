import { expect, test } from "@playwright/test";
import { ReviewRecorder, ReviewSpans } from "../lib/ticket-observability";
import type { TicketReviewSession, TicketReviewObservationRequest } from "../lib/api";
const session: TicketReviewSession = {
  session_id: "rvs_" + "1".repeat(32), client_session_key: "2".repeat(32), variant: "manual_guidance_v1",
  policy_mode: "canary", canary_version: "v1", effective_treatment_version: "manual_guidance_v1:v1",
  assigned_at: "2026-11-17T00:00:00Z", observation_limit: 64,
};
const sample = {
  client_observation_id: "3".repeat(32), event_kind: "exposure" as const, outcome: "observed" as const,
  rendered_variant: "baseline" as const, timing_state: "open_or_missing_unknown" as const,
  reading_status: "missing" as const, review_status: "missing" as const,
  visibility_changes: 0, invalid_field_count: 0, customer_supplied: false,
};
test("visible review, busy and hidden intervals neither overlap nor double count", () => {
  let now = 0;
  const spans = new ReviewSpans(() => now);
  now = 10; spans.state(true, false);
  now = 30; spans.state(true, true);
  now = 60; spans.state(false, false);
  now = 70;
  expect(spans.sample()).toEqual({ visible_elapsed_ms: 20, busy_elapsed_ms: 20,
    hidden_elapsed_ms: 30, visibility_changes: 2, timing_state: "closed" });
  now = 80;
  expect(spans.sample().visible_elapsed_ms).toBe(10);
});
test("lost acknowledgement retries identical evidence and preserves actual exposure", async () => {
  const bodies: TicketReviewObservationRequest[] = [];
  const recorder = new ReviewRecorder(async (_id, body) => {
    bodies.push(structuredClone(body));
    if (bodies.length === 1) throw new Error("synthetic lost acknowledgement");
  });
  recorder.push(sample);
  recorder.assign(session);
  await expect.poll(() => bodies.length).toBe(2);
  expect(bodies[0]).toEqual(bodies[1]);
  expect(bodies[0].rendered_variant).toBe("baseline");
  expect(bodies[0].treatment_variant).toBe("manual_guidance_v1");
});
test("outages have bounded retries and bounded queue even before assignment", async () => {
  let calls = 0;
  const recorder = new ReviewRecorder(async () => { calls += 1; throw new Error("offline"); });
  for (let n = 0; n < 200; n += 1) recorder.push({ ...sample, client_observation_id: n.toString(16).padStart(32, "0") });
  expect(calls).toBe(0);
  recorder.assign(session);
  await expect.poll(() => calls).toBe(128);
  recorder.push(sample);
  await recorder.flush();
  expect(calls).toBe(128);
});
