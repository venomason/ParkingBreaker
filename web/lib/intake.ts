/** Contact validation and the closed ticket-fact shapes the customer confirms. No I/O. */
import type {
  IntakeForm,
  TicketAdoptedFieldName,
  TicketDiscrepancyResolution,
  TicketFactBindingPayload,
  TicketFacts,
  TicketLiteralFieldName,
  TicketReadingField,
  TicketReadingSnapshot,
  TicketReviewComparison,
  TicketReadingStatus,
  WorkspaceView,
} from "./api";

export function contactErrors(values: Record<string, string>, hasTicket: boolean): Record<string, string> {
  const errors: Record<string, string> = {};
  for (const [key, id, label] of [
    ["issuedCity", "liveIssuedCity", "city where the ticket was issued"],
    ["name", "liveName", "full name"],
    ["line1", "liveLine1", "mailing address"],
    ["city", "liveCity", "mailing city"],
  ]) if (!values[key]?.trim()) errors[id] = `Enter your ${label}.`;
  for (const [key, id] of [["issuedState", "liveIssuedState"], ["state", "liveState"]]) {
    if (!/^[A-Z]{2}$/.test(values[key] ?? "")) errors[id] = "Use a two-letter state code, like CA.";
  }
  if (!/^\d{5}(-\d{4})?$/.test(values.zip?.trim() ?? "")) errors.liveZip = "Use a five-digit ZIP code, with an optional four-digit extension.";
  if (!/^\+?[0-9][0-9 ().-]{6,19}$/.test(values.phone?.trim() ?? "")) errors.livePhone = "Add a valid contact phone number.";
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(values.email?.trim() ?? "")) errors.liveEmail = "Add an email address for private appeal access.";
  if (!hasTicket) errors.liveTicketPhoto = "Add a readable photo of the front of your ticket.";
  return errors;
}

// The exact closed literal reading order (11 fields) and adopted TicketFacts order (13).
export const TICKET_LITERAL_FIELD_ORDER: readonly TicketLiteralFieldName[] = [
  "citation_number",
  "license_plate",
  "license_plate_state",
  "issuer_name",
  "issue_date",
  "notice_date",
  "printed_deadline",
  "violation_text",
  "issued_city",
  "issued_state",
  "stage_text",
] as const;

export const TICKET_ADOPTED_FIELD_ORDER: readonly TicketAdoptedFieldName[] = [
  "citation_number",
  "license_plate",
  "license_plate_state",
  "issuer_hint",
  "issued_city",
  "issued_state",
  "issuer_kind",
  "violation_code",
  "violation_description",
  "issue_date",
  "stage",
  "notice_date",
  "printed_deadline",
] as const;

export const TICKET_LITERAL_LABELS: Record<TicketLiteralFieldName, string> = {
  citation_number: "Citation number",
  license_plate: "License plate",
  license_plate_state: "Plate state",
  issuer_name: "Issuer name",
  issue_date: "Issue date as printed",
  notice_date: "Notice date as printed",
  printed_deadline: "Printed deadline as printed",
  violation_text: "Violation text",
  issued_city: "Issued city",
  issued_state: "Issued state",
  stage_text: "Stage as printed",
};

/** The literal field that a customer's adopted fact corresponds to, if any. */
export const ADOPTED_TO_LITERAL: Partial<Record<TicketAdoptedFieldName, TicketLiteralFieldName>> = {
  citation_number: "citation_number",
  license_plate: "license_plate",
  license_plate_state: "license_plate_state",
  issuer_hint: "issuer_name",
  issued_city: "issued_city",
  issued_state: "issued_state",
  issue_date: "issue_date",
  stage: "stage_text",
  notice_date: "notice_date",
  printed_deadline: "printed_deadline",
  violation_description: "violation_text",
};

export type TicketLiteral = Record<TicketLiteralFieldName, string | null>;

/** The exact 13 adopted facts sent to SubmitIntake and saved in the binding. */
export interface AdoptedFacts {
  citation_number: string;
  license_plate: string;
  license_plate_state: string;
  issuer_hint: string;
  issued_city: string;
  issued_state: string;
  issuer_kind: "GOVERNMENT" | "PRIVATE";
  violation_code: string;
  violation_description: string;
  issue_date: string;
  stage: string;
  notice_date: string | null;
  printed_deadline: string | null;
}

/** Draft values stay exactly as typed; absence needs an explicit choice, never blank. */
export interface FactDraft {
  citation_number: string;
  license_plate: string;
  license_plate_state: string;
  issuer_hint: string;
  issued_city: string;
  issued_state: string;
  issuer_kind: "" | "GOVERNMENT" | "PRIVATE";
  violation_code: string;
  violation_description: string;
  issue_date: string;
  stage: string;
  notice_date: string;
  printed_deadline: string;
  notice_date_absent: boolean;
  printed_deadline_absent: boolean;
}

export function emptyFactDraft(): FactDraft {
  return {
    citation_number: "",
    license_plate: "",
    license_plate_state: "",
    issuer_hint: "",
    issued_city: "",
    issued_state: "",
    issuer_kind: "",
    violation_code: "",
    violation_description: "",
    issue_date: "",
    stage: "",
    notice_date: "",
    printed_deadline: "",
    notice_date_absent: false,
    printed_deadline_absent: false,
  };
}

/** Strict ISO calendar date (YYYY-MM-DD), never a US-slash heuristic. */
export function isIsoCalendarDate(value: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const parsed = new Date(`${value}T00:00:00Z`);
  return !Number.isNaN(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value;
}

function boundedLine(value: string): boolean {
  return value.length > 0 && value.length <= 200 && /\S/.test(value) && !/[\u0000-\u001f\u007f]/.test(value);
}

function isState(value: string): boolean {
  return /^[A-Z]{2}$/.test(value);
}

function isOptionalDate(value: string, absent: boolean, errors: Record<string, string>, id: string, label: string) {
  if (absent) return;
  if (!isIsoCalendarDate(value)) errors[id] = `Choose ${label} as a calendar date, or confirm it is not printed.`;
}

/** Validate the draft without changing any customer-entered string. */
export function validateFactDraft(draft: FactDraft): { errors: Record<string, string>; facts: AdoptedFacts | null } {
  const errors: Record<string, string> = {};
  const required = (key: keyof FactDraft, label: string) => {
    if (!boundedLine(String(draft[key] ?? ""))) errors[`fact-${key}`] = `Enter ${label} exactly as printed on your ticket.`;
  };
  required("citation_number", "the citation number");
  required("license_plate", "the plate number");
  if (!isState(draft.license_plate_state)) errors["fact-license_plate_state"] = "Use a two-letter plate state, like CA.";
  required("issuer_hint", "who issued the ticket");
  required("issued_city", "the city where the ticket was issued");
  if (!isState(draft.issued_state)) errors["fact-issued_state"] = "Use a two-letter issued state, like CA.";
  if (draft.issuer_kind !== "GOVERNMENT" && draft.issuer_kind !== "PRIVATE") {
    errors["fact-issuer_kind"] = "Choose whether a public agency or a private company issued it.";
  }
  required("violation_code", "the violation code");
  required("violation_description", "the violation description");
  if (!isIsoCalendarDate(draft.issue_date)) errors["fact-issue_date"] = "Choose the issue date as a calendar date.";
  required("stage", "the stage printed on the ticket");
  isOptionalDate(draft.notice_date, draft.notice_date_absent, errors, "fact-notice_date", "the notice date");
  isOptionalDate(draft.printed_deadline, draft.printed_deadline_absent, errors, "fact-printed_deadline", "the printed deadline");
  if (draft.issue_date && !isIsoCalendarDate(draft.issue_date)) {
    errors["fact-issue_date"] = "Choose the issue date as a calendar date.";
  }
  if (Object.keys(errors).length) return { errors, facts: null };
  const issuerKind: AdoptedFacts["issuer_kind"] = draft.issuer_kind === "PRIVATE" ? "PRIVATE" : "GOVERNMENT";
  return {
    errors,
    facts: {
      citation_number: draft.citation_number,
      license_plate: draft.license_plate,
      license_plate_state: draft.license_plate_state,
      issuer_hint: draft.issuer_hint,
      issued_city: draft.issued_city,
      issued_state: draft.issued_state,
      issuer_kind: issuerKind,
      violation_code: draft.violation_code,
      violation_description: draft.violation_description,
      issue_date: draft.issue_date,
      stage: draft.stage,
      notice_date: draft.notice_date_absent ? null : draft.notice_date,
      printed_deadline: draft.printed_deadline_absent ? null : draft.printed_deadline,
    },
  };
}

/** Build the closed literal projection from the durable reading; null when incomplete. */
export function literalFromReading(fields: TicketReadingField[]): TicketLiteral | null {
  if (!Array.isArray(fields) || fields.length !== TICKET_LITERAL_FIELD_ORDER.length) return null;
  const literal: Partial<Record<TicketLiteralFieldName, string | null>> = {};
  for (const entry of fields) {
    if (!TICKET_LITERAL_FIELD_ORDER.includes(entry.field)) return null;
    if (entry.field in literal) return null;
    if (entry.state === "shown") {
      if (typeof entry.value !== "string" || !entry.value.trim() || entry.value.length > 200) return null;
      literal[entry.field] = entry.value;
    } else {
      if (entry.value !== null) return null;
      literal[entry.field] = null;
    }
  }
  if (TICKET_LITERAL_FIELD_ORDER.some((field) => !(field in literal))) return null;
  return literal as TicketLiteral;
}

/** Failed or stale reading supplies no AI facts. Customer input stays in adopted. */
export function literalFromSnapshot(reading: TicketReadingSnapshot | null): TicketLiteral {
  if (reading?.current && reading.status === "success") {
    const literal = literalFromReading(reading.fields);
    if (literal) return literal;
  }
  return Object.fromEntries(TICKET_LITERAL_FIELD_ORDER.map((field) => [field, null])) as TicketLiteral;
}

/** Prefill only plainly shown text suggestions; never stage, issuer kind, code or a date conversion. */
export function factDraftFromReading(fields: TicketReadingField[], base: FactDraft = emptyFactDraft()): FactDraft {
  const shown = (name: TicketLiteralFieldName) => {
    const entry = fields.find((field) => field.field === name);
    return entry?.state === "shown" && typeof entry.value === "string" ? entry.value : "";
  };
  const fill = (current: string, suggested: string) => (current.trim() ? current : suggested);
  return {
    ...base,
    citation_number: fill(base.citation_number, shown("citation_number")),
    license_plate: fill(base.license_plate, shown("license_plate")),
    license_plate_state: fill(base.license_plate_state, shown("license_plate_state")),
    issuer_hint: fill(base.issuer_hint, shown("issuer_name")),
    issued_city: fill(base.issued_city, shown("issued_city")),
    issued_state: fill(base.issued_state, shown("issued_state")),
    violation_description: fill(base.violation_description, shown("violation_text")),
  };
}

export function resolutionForComparison(comparison: TicketReviewComparison): TicketDiscrepancyResolution {
  if (comparison === "conflict") return "CONFIRMED_VALUE_STANDS";
  if (comparison === "not_shown") return "ABSENCE_ACKNOWLEDGED";
  return "UNREADABLE_ACKNOWLEDGED";
}

/** The customer-facing value for a comparison, from the adopted facts already confirmed. */
export function adoptedValueForLiteral(facts: AdoptedFacts, field: TicketLiteralFieldName): string | null {
  switch (field) {
    case "citation_number": return facts.citation_number;
    case "license_plate": return facts.license_plate;
    case "license_plate_state": return facts.license_plate_state;
    case "issuer_name": return facts.issuer_hint;
    case "issue_date": return facts.issue_date;
    case "notice_date": return facts.notice_date;
    case "printed_deadline": return facts.printed_deadline;
    case "violation_text": return facts.violation_description;
    case "issued_city": return facts.issued_city;
    case "issued_state": return facts.issued_state;
    case "stage_text": return facts.stage;
  }
}

export function factDraftFromAdopted(adopted: Record<string, string | null>): FactDraft {
  const text = (key: string) => (typeof adopted[key] === "string" ? (adopted[key] as string) : "");
  const optional = (key: string) => adopted[key] === null;
  return {
    citation_number: text("citation_number"),
    license_plate: text("license_plate"),
    license_plate_state: text("license_plate_state"),
    issuer_hint: text("issuer_hint"),
    issued_city: text("issued_city"),
    issued_state: text("issued_state"),
    issuer_kind: adopted.issuer_kind === "GOVERNMENT" || adopted.issuer_kind === "PRIVATE" ? adopted.issuer_kind : "",
    violation_code: text("violation_code"),
    violation_description: text("violation_description"),
    issue_date: text("issue_date"),
    stage: text("stage"),
    notice_date: text("notice_date"),
    printed_deadline: text("printed_deadline"),
    notice_date_absent: optional("notice_date"),
    printed_deadline_absent: optional("printed_deadline"),
  };
}

export function factDraftFromValues(values: Record<string, string>): FactDraft {
  const draft = emptyFactDraft();
  // One ticket-only draft entry fits beside all legacy/contact fields. Keep the
  // explicit absence booleans: an empty calendar input is still unconfirmed.
  if (values.ticket) {
    try {
      const saved: unknown = JSON.parse(values.ticket);
      if (saved && typeof saved === "object" && !Array.isArray(saved)) {
        const entries = saved as Record<string, unknown>;
        for (const key of TICKET_ADOPTED_FIELD_ORDER) {
          const value = entries[key];
          if (key === "issuer_kind") {
            draft[key] = value === "GOVERNMENT" || value === "PRIVATE" ? value : "";
          } else if (typeof value === "string") draft[key] = value;
        }
        draft.notice_date_absent = entries.notice_date_absent === true;
        draft.printed_deadline_absent = entries.printed_deadline_absent === true;
        return draft;
      }
    } catch { /* Older drafts use the individual ticket.* entries below. */ }
  }
  for (const key of TICKET_ADOPTED_FIELD_ORDER) {
    if (key === "issuer_kind") {
      const value = values[`ticket.${key}`];
      draft.issuer_kind = value === "GOVERNMENT" || value === "PRIVATE" ? value : "";
    } else if (key === "notice_date" || key === "printed_deadline") {
      const value = values[`ticket.${key}`];
      draft[key] = value ?? "";
      draft[`${key}_absent`] = values[`ticket.${key}_absent`] === "true";
    } else {
      draft[key] = values[`ticket.${key}`] ?? "";
    }
  }
  return draft;
}

/** Incomplete facts cannot use the validated binding; save their exact draft separately. */
export function ticketValuesFromDraft(draft: FactDraft): Record<string, string> {
  return { ticket: JSON.stringify(draft) };
}

/** Keep the 15 draft fields out of values once the closed envelope carries them. */
export function ticketFormWithDraft(form: IntakeForm, draft: FactDraft, binding: TicketFactBindingPayload | null): IntakeForm {
  const legacyValues = Object.fromEntries(Object.entries(form.values ?? {}).filter(([key]) => key !== "ticket" && !key.startsWith("ticket.")));
  return {
    ...form,
    values: { ...legacyValues, ...(binding ? {} : ticketValuesFromDraft(draft)) },
    ticket_fact_binding: binding ?? undefined,
  };
}

/** Required drafts resume only with fresh GET confirmation proof, never a saved step alone. */
export function restoredTicketStep(workspace: WorkspaceView, status?: TicketReadingStatus | null): 1 | 2 {
  const required = workspace.ticket_reading_required || status?.required;
  if (required && (status?.confirmation.state !== "current" || !status.confirmation.command_id)) return 1;
  return workspace.intake_form?.step ?? 1;
}

/** Adopted facts are sent exactly; nothing is trimmed or differently normalised here. */
export function ticketFactsFromAdopted(adopted: Record<string, string | null>): TicketFacts {
  return {
    citation_number: String(adopted.citation_number ?? ""),
    license_plate: String(adopted.license_plate ?? ""),
    license_plate_state: String(adopted.license_plate_state ?? ""),
    issuer_hint: String(adopted.issuer_hint ?? ""),
    issued_city: String(adopted.issued_city ?? ""),
    issued_state: String(adopted.issued_state ?? ""),
    issuer_kind: adopted.issuer_kind === "PRIVATE" ? "PRIVATE" : "GOVERNMENT",
    violation_code: String(adopted.violation_code ?? ""),
    violation_description: String(adopted.violation_description ?? ""),
    issue_date: String(adopted.issue_date ?? ""),
    stage: String(adopted.stage ?? ""),
    notice_date: adopted.notice_date === null || adopted.notice_date === undefined ? null : String(adopted.notice_date),
    printed_deadline: adopted.printed_deadline === null || adopted.printed_deadline === undefined ? null : String(adopted.printed_deadline),
  };
}
