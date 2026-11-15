"use client";

import React, { useCallback, useRef, useState } from "react";
import type {
  TicketDiscrepancyResolution,
  TicketReadingSnapshot,
  TicketReviewSnapshot,
} from "../lib/api";
import {
  ADOPTED_TO_LITERAL,
  TICKET_LITERAL_FIELD_ORDER,
  TICKET_LITERAL_LABELS,
  adoptedValueForLiteral,
  resolutionForComparison,
  validateFactDraft,
  type AdoptedFacts,
  type FactDraft,
} from "../lib/intake";

export interface TicketFactReviewProps {
  draft: FactDraft;
  onDraftChange: (next: FactDraft) => void;
  reading: TicketReadingSnapshot | null;
  review: TicketReviewSnapshot | null;
  resolutions: Record<string, TicketDiscrepancyResolution>;
  onResolutionsChange: (next: Record<string, TicketDiscrepancyResolution>) => void;
  busy: boolean;
  locked: boolean;
  dirty: boolean;
  confirmed: boolean;
  onSaveFacts: (facts: AdoptedFacts) => void;
  onConfirm: () => void;
}

const COMPARISON_LABELS: Record<string, string> = {
  match: "The visible text matches what the reading suggested",
  conflict: "The visible text differs from the reading suggestion",
  not_shown: "The photo does not show this field",
  unreadable: "This part of the photo cannot be read",
};

const RESOLUTION_LABELS: Record<TicketDiscrepancyResolution, string> = {
  CONFIRMED_VALUE_STANDS: "Keep the value I confirmed",
  ABSENCE_ACKNOWLEDGED: "I understand the ticket does not show this",
  UNREADABLE_ACKNOWLEDGED: "I understand this cannot be read",
};

function literalText(value: string | null | undefined) {
  if (value === null) return "Not shown on the ticket";
  if (value === undefined) return "No reading yet";
  return value;
}

/** Suggestions, the closed 13-field customer facts, and the independent per-field comparison. */
export function TicketFactReview({
  draft,
  onDraftChange,
  reading,
  review,
  resolutions,
  onResolutionsChange,
  busy,
  locked,
  dirty,
  confirmed,
  onSaveFacts,
  onConfirm,
}: TicketFactReviewProps) {
  const [errors, setErrors] = useState<Record<string, string>>({});
  const formRef = useRef<HTMLFormElement>(null);
  const set = (key: keyof FactDraft) => (value: FactDraft[typeof key]) => onDraftChange({ ...draft, [key]: value });
  const textField = (key: keyof FactDraft, label: string, extra?: React.ReactNode) => (
    <label>
      {label}
      <input
        id={`fact-${key}`}
        value={String(draft[key] ?? "")}
        maxLength={200}
        disabled={locked || busy}
        aria-invalid={Boolean(errors[`fact-${key}`])}
        aria-describedby={errors[`fact-${key}`] ? `fact-${key}-error` : undefined}
        onChange={(event) => set(key)(event.target.value as never)}
        autoComplete="off"
      />
      {errors[`fact-${key}`] && <span className="field-error" id={`fact-${key}-error`}>{errors[`fact-${key}`]}</span>}
      {extra}
    </label>
  );

  const submit = useCallback((event: React.FormEvent) => {
    event.preventDefault();
    if (locked || busy) return;
    const result = validateFactDraft(draft);
    setErrors(result.errors);
    if (!result.facts) {
      requestAnimationFrame(() => formRef.current?.querySelector<HTMLElement>('[aria-invalid="true"]')?.focus());
      return;
    }
    onSaveFacts(result.facts);
  }, [draft, locked, busy, onSaveFacts]);

  const literalEntries = TICKET_LITERAL_FIELD_ORDER.map((field) => {
    const found = reading?.fields.find((entry) => entry.field === field);
    return { field, entry: found, value: found?.state === "shown" ? found.value : null, state: found?.state ?? null };
  });
  const printed = (field: (typeof TICKET_LITERAL_FIELD_ORDER)[number]) => literalEntries.find((entry) => entry.field === field)?.value;
  const reviewFields = review?.status === "success" && review.current ? review.fields : [];
  const expectedResolutions = reviewFields.filter((field) => field.comparison !== "match").map((field) => field.field);
  const unresolved = dirty || expectedResolutions.some((field) => !resolutions[field]);
  const hasReview = reviewFields.length > 0;

  return (
    <div id="visionFactReview" className="stack">
      <section aria-labelledby="visionReadingsTitle" className="card" id="visionReadings">
        <p className="eyebrow">Unverified reading</p>
        <h3 id="visionReadingsTitle" style={{ marginBottom: "6px" }}>What the reading suggests</h3>
        <p className="muted small" style={{ marginBottom: "10px" }}>
          These are suggestions from an AI reading of your uploaded photos, not confirmed facts. The reading can see
          everything printed on the photo, which may include your name, plate or address. Check every value yourself.
        </p>
        <ul id="visionSuggestions" className="stack" style={{ listStyle: "none", padding: 0, margin: 0 }}>
          {literalEntries.map(({ field, value, state }) => (
            <li key={field} data-vision-field={field} style={{ padding: "6px 0", borderBottom: "1px solid var(--line)" }}>
              <b className="small">{TICKET_LITERAL_LABELS[field]}</b>{" "}
              {state === null ? (
                <span className="muted small">No reading yet.</span>
              ) : state === "shown" ? (
                <span className="small">“{value}”</span>
              ) : (
                <span className="small">{state === "not_shown" ? "Not shown" : "Unreadable"}</span>
              )}
            </li>
          ))}
        </ul>
      </section>

      <form ref={formRef} id="visionFactsForm" className="card" onSubmit={submit} noValidate aria-busy={busy}>
        <p className="eyebrow">Your confirmed facts</p>
        <h3 style={{ marginBottom: "6px" }}>Enter the ticket facts yourself</h3>
        <p className="muted small" style={{ marginBottom: "10px" }}>
          Printed dates stay as they appear on the ticket above. Below, choose the calendar dates, stage and issuer
          yourself. Nothing is filled in for you, and nothing is verified.
        </p>
        <div className="local-ids single">
          {textField("citation_number", "Citation number")}
          {textField("license_plate", "License plate")}
          {textField("license_plate_state", "Plate state (two letters)")}
          {textField("issuer_hint", "Who issued the ticket, as printed")}
          {textField("issued_city", "City where the ticket was issued")}
          {textField("issued_state", "State where issued (two letters)")}
        </div>
        <fieldset id="fact-issuer_kind" className="local-ids single" style={{ border: 0, padding: 0 }} aria-invalid={Boolean(errors["fact-issuer_kind"])} aria-describedby={errors["fact-issuer_kind"] ? "fact-issuer_kind-error" : undefined}>
          <legend className="small" style={{ fontWeight: 750 }}>Who issued it?</legend>
          {([["GOVERNMENT", "A city or public agency"], ["PRIVATE", "A private parking lot or company"]] as const).map(([value, label]) => (
            <label key={value} style={{ display: "flex", gap: "8px", alignItems: "center" }}>
              <input
                type="radio"
                name="fact-issuer_kind"
                id={`fact-issuer_kind-${value.toLowerCase()}`}
                value={value}
                checked={draft.issuer_kind === value}
                disabled={locked || busy}
                onChange={() => set("issuer_kind")(value)}
                style={{ width: "auto", minHeight: 0 }}
              />
              {label}
            </label>
          ))}
          {errors["fact-issuer_kind"] && <span className="field-error" id="fact-issuer_kind-error">{errors["fact-issuer_kind"]}</span>}
        </fieldset>
        <div className="local-ids single">
          {textField("violation_code", "Violation code")}
          {textField("violation_description", "Violation description")}
          <label>
            Issue date (calendar date)
            <input
              id="fact-issue_date"
              type="date"
              value={draft.issue_date}
              disabled={locked || busy}
              aria-invalid={Boolean(errors["fact-issue_date"])}
              aria-describedby={errors["fact-issue_date"] ? "fact-issue_date-error" : undefined}
              onChange={(event) => set("issue_date")(event.target.value)}
            />
            <span className="muted small">Printed on the ticket: {literalText(printed("issue_date"))}</span>
            {errors["fact-issue_date"] && <span className="field-error" id="fact-issue_date-error">{errors["fact-issue_date"]}</span>}
          </label>
          {textField("stage", "Stage printed on the ticket")}
        </div>
        <div className="local-ids single">
          <label>
            Notice date (calendar date)
            <input
              id="fact-notice_date"
              type="date"
              value={draft.notice_date}
              disabled={locked || busy || draft.notice_date_absent}
              aria-invalid={Boolean(errors["fact-notice_date"])}
              aria-describedby={errors["fact-notice_date"] ? "fact-notice_date-error" : undefined}
              onChange={(event) => set("notice_date")(event.target.value)}
            />
            <span className="muted small">Printed on the ticket: {literalText(printed("notice_date"))}</span>
            <span style={{ display: "flex", gap: "8px", alignItems: "center", marginTop: "4px" }}>
              <input
                id="fact-notice_date-absent"
                type="checkbox"
                checked={draft.notice_date_absent}
                disabled={locked || busy}
                onChange={(event) => onDraftChange({ ...draft, notice_date_absent: event.target.checked, notice_date: event.target.checked ? "" : draft.notice_date })}
                style={{ width: "auto", minHeight: 0 }}
              />
              This ticket does not print a notice date
            </span>
            {errors["fact-notice_date"] && <span className="field-error" id="fact-notice_date-error">{errors["fact-notice_date"]}</span>}
          </label>
          <label>
            Printed deadline (calendar date)
            <input
              id="fact-printed_deadline"
              type="date"
              value={draft.printed_deadline}
              disabled={locked || busy || draft.printed_deadline_absent}
              aria-invalid={Boolean(errors["fact-printed_deadline"])}
              aria-describedby={errors["fact-printed_deadline"] ? "fact-printed_deadline-error" : undefined}
              onChange={(event) => set("printed_deadline")(event.target.value)}
            />
            <span className="muted small">Printed on the ticket: {literalText(printed("printed_deadline"))}</span>
            <span style={{ display: "flex", gap: "8px", alignItems: "center", marginTop: "4px" }}>
              <input
                id="fact-printed_deadline-absent"
                type="checkbox"
                checked={draft.printed_deadline_absent}
                disabled={locked || busy}
                onChange={(event) => onDraftChange({ ...draft, printed_deadline_absent: event.target.checked, printed_deadline: event.target.checked ? "" : draft.printed_deadline })}
                style={{ width: "auto", minHeight: 0 }}
              />
              This ticket does not print a deadline
            </span>
            {errors["fact-printed_deadline"] && <span className="field-error" id="fact-printed_deadline-error">{errors["fact-printed_deadline"]}</span>}
          </label>
        </div>
        {Object.keys(errors).length > 0 && (
          <p className="err" role="alert">Check the marked facts above. Your entries are still here.</p>
        )}
        <button className="btn btn-primary" type="submit" id="visionReviewBtn" disabled={locked || busy || !reading || reading.status !== "success"}>
          {busy ? <><span className="spinner" /> Comparing…</> : "Save facts and compare"}
        </button>
        {!reading && <p className="muted small">Read your ticket photos above before comparing.</p>}
      </form>

      {review && (
        <section className="card" id="visionReview" aria-labelledby="visionReviewTitle">
          <p className="eyebrow">Independent comparison</p>
          <h3 id="visionReviewTitle" style={{ marginBottom: "6px" }}>What the comparison found</h3>
          <p className="muted small" style={{ marginBottom: "10px" }}>
            This is a model comparison only, not external verification. A match does not prove a fact. For every
            difference, you must choose your own resolution before you can confirm.
          </p>
          {review.status !== "success" || !review.current ? (
            <p className="small" role="status" id="visionReviewState">
              {review.status === "refused"
                ? "The comparison service could not return usable results. Save your facts again to start a fresh comparison."
                : review.status === "pending" || review.status === "unknown"
                  ? "The comparison is not confirmed yet. Check the saved status; it will not be repeated automatically."
                  : review.status === "missing"
                    ? "No comparison has been requested yet."
                    : "These facts or photos changed, so the previous comparison no longer applies. Save your facts to compare again."}
            </p>
          ) : (
            <>
              <div className="stack" id="visionReviewList">
                {reviewFields.map((item) => {
                  const expected = item.comparison === "match" ? null : resolutionForComparison(item.comparison);
                  const adoptedField = Object.entries(ADOPTED_TO_LITERAL).find(([, literal]) => literal === item.field)?.[0];
                  const adopted = adoptedField && draft ? adoptedValueForLiteral(draft as unknown as AdoptedFacts, item.field) : null;
                  return (
                    <div key={item.field} className="card" data-review-field={item.field} style={{ padding: "10px 12px", background: "var(--amber-soft, #fffdfa)" }}>
                      <b className="small">{TICKET_LITERAL_LABELS[item.field]}</b>
                      <p className="small" style={{ margin: "2px 0" }}>{COMPARISON_LABELS[item.comparison] ?? item.comparison}.</p>
                      {item.visible_text && <p className="small">Visible on the ticket: “{item.visible_text}”</p>}
                      <p className="small muted">Your fact: {adopted || "—"}</p>
                      {expected && !confirmed && (
                        <label className="small" style={{ display: "block", marginTop: "6px" }}>
                          Your resolution for this field
                          <select
                            id={`resolve-${item.field}`}
                            value={resolutions[item.field] ?? ""}
                            disabled={locked || busy}
                            onChange={(event) => onResolutionsChange({ ...resolutions, [item.field]: event.target.value as TicketDiscrepancyResolution })}
                          >
                            <option value="">Choose a resolution</option>
                            <option value={expected}>{RESOLUTION_LABELS[expected]}</option>
                          </select>
                        </label>
                      )}
                      {expected && confirmed && <p className="small muted">Your recorded resolution: {RESOLUTION_LABELS[expected]}.</p>}
                    </div>
                  );
                })}
              </div>
              {review.basis && <p className="muted small">{review.basis}</p>}
              {!confirmed && (
                <>
                  <button
                    className="btn btn-primary"
                    type="button"
                    id="visionConfirmBtn"
                    disabled={locked || busy || unresolved || !hasReview}
                    onClick={() => onConfirm()}
                  >
                    {busy ? <><span className="spinner" /> Confirming…</> : "Confirm these ticket facts"}
                  </button>
                  {unresolved && hasReview && <p className="muted small" role="status">Resolve every comparison above before confirming.</p>}
                </>
              )}
            </>
          )}
        </section>
      )}
    </div>
  );
}
