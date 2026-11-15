"use client";

import React, { useEffect, useRef, useState } from "react";
import Image from "next/image";
import { contactErrors } from "../lib/intake";
import { PHOTO_UPLOAD_GUIDANCE } from "../lib/api";

export interface LiveDetailsData {
  issuedCity: string;
  issuedState: string;
  issuerKind: "GOVERNMENT" | "PRIVATE";
  rentalPaid: "" | "yes" | "no" | "not-rental" | "unknown";
  name: string;
  phone: string;
  email: string;
  line1: string;
  line2: string;
  city: string;
  state: string;
  zip: string;
  ticketPhoto: File | null;
  ticketBackPhoto: File | null;
  ticketPhotoRef?: string | null;
  ticketBackPhotoRef?: string | null;
  evidencePhotos: EvidencePhotoEntry[];
}

export interface EvidencePhotoEntry {
  id: string;
  file: File | null;
  ref: string | null;
}

export type LiveContactValues = Omit<LiveDetailsData, "ticketPhoto" | "ticketBackPhoto" | "evidencePhotos" | "ticketPhotoRef" | "ticketBackPhotoRef">;
export const EMPTY_CONTACT: LiveContactValues = {
  rentalPaid: "",
  issuedCity: "", issuedState: "", issuerKind: "GOVERNMENT", name: "", phone: "", email: "", line1: "", line2: "", city: "", state: "", zip: "",
};

interface LiveDetailsProps {
  busy: boolean;
  submitting: boolean;
  error: string | null;
  onSubmit: (data: LiveDetailsData) => void;
  values: LiveContactValues;
  onChange: (values: LiveContactValues) => void;
  ticketPhoto: File | null;
  ticketBackPhoto: File | null;
  evidencePhotos: EvidencePhotoEntry[];
  ticketPhotoRef: string | null;
  ticketBackPhotoRef: string | null;
  onSelectPhoto: (kind: "ticket" | "back", file: File | null) => void;
  onAddEvidencePhotos: (files: File[]) => void;
  onRemoveEvidencePhoto: (id: string) => void;
  onMoveEvidencePhoto: (id: string, direction: -1 | 1) => void;
  // Required ticket-reading path: the facts and original photos were confirmed in Step 1.
  visionRequired?: boolean;
  visionFacts?: { issuedCity: string; issuedState: string; issuerKind: "GOVERNMENT" | "PRIVATE" } | null;
}

function EvidenceThumbnail({ file, number }: { file: File | null; number: number }) {
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    if (!(file instanceof Blob)) return;
    const next = URL.createObjectURL(file);
    let active = true;
    queueMicrotask(() => { if (active) setUrl(next); });
    return () => { active = false; URL.revokeObjectURL(next); };
  }, [file]);
  // Originals restored by reference are inspected in the exact packet, never guessed.
  return url ? <Image unoptimized width={72} height={72} className="evidence-thumb" src={url} alt={`Selected supporting photo ${number}`} />
    : <span className="evidence-placeholder" aria-hidden="true">{number}</span>;
}

/** Where the ticket was issued, who issued it, the customer's contact details and photos. */
export function LiveDetails({ busy, submitting, error, onSubmit, values: v, onChange: setV, ticketPhoto, ticketBackPhoto, evidencePhotos, ticketPhotoRef, ticketBackPhotoRef, onSelectPhoto, onAddEvidencePhotos, onRemoveEvidencePhoto, onMoveEvidencePhoto, visionRequired = false, visionFacts = null }: LiveDetailsProps) {
  const [problems, setProblems] = useState<Record<string, string>>({});
  const [announcement, setAnnouncement] = useState("");
  const field = (id: string) => ({ disabled: submitting, "aria-invalid": Boolean(problems[id]), "aria-describedby": problems[id] ? `${id}-error` : undefined });
  const issue = (id: string) => problems[id] ? <span className="field-error" id={`${id}-error`}>{problems[id]}</span> : null;
  const ticketBackInput = useRef<HTMLInputElement>(null);
  const set = (key: keyof typeof v) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setV({ ...v, [key]: key.endsWith("tate") ? e.target.value.toUpperCase() : e.target.value });

  const moveEvidence = (entry: EvidencePhotoEntry, index: number, direction: -1 | 1) => {
    onMoveEvidencePhoto(entry.id, direction);
    const position = index + direction;
    setAnnouncement(`Photo ${index + 1} moved to position ${position + 1}.`);
    requestAnimationFrame(() => {
      const control = position === 0 ? "down" : position === evidencePhotos.length - 1 ? "up" : direction < 0 ? "up" : "down";
      document.querySelector<HTMLElement>(`[data-testid="move-evidence-${control}-${CSS.escape(entry.id)}"]`)?.focus();
    });
  };

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const errors = contactErrors(v, Boolean(ticketPhoto || ticketPhotoRef));
    if (!v.rentalPaid) errors.liveRentalPaid = "Choose an answer about the rental company's payment.";
    setProblems(errors);
    if (Object.keys(errors).length) {
      // DOM order gives a predictable route through the form, including after retry.
      requestAnimationFrame(() => document.querySelector<HTMLElement>('#liveDetails [aria-invalid="true"]')?.focus());
      return;
    }
    onSubmit({ ...v, zip: v.zip.trim(), phone: v.phone.trim(), email: v.email.trim(), ticketPhoto, ticketBackPhoto, evidencePhotos, ticketPhotoRef, ticketBackPhotoRef });
  };

  return (
    <form className="card" id="liveDetails" onSubmit={submit} noValidate aria-busy={submitting}>
      <h3 style={{ marginBottom: "6px" }}>A few details for the letter</h3>
      <p className="muted small">
        These contact details identify your appeal and provide its return address. Your ticket photo and any supporting photo are
        printed and mailed with your letter, each labelled as uploaded by you. The back of your ticket is optional and is mailed with the front.
      </p>
      {visionRequired ? (
        <p className="card small" id="liveTicketFactsSaved" style={{ padding: "10px 12px", background: "var(--amber-soft, #fffdfa)" }}>
          Your ticket facts were confirmed in Step 1: issued in {visionFacts?.issuedCity ?? v.issuedCity},{" "}
          {visionFacts?.issuedState ?? v.issuedState} by {visionFacts?.issuerKind === "PRIVATE" ? "a private parking lot or company" : "a city or public agency"}.
          The original front and back photos are already saved for this appeal and are mailed as uploaded.
        </p>
      ) : (
        <>
          <div className="local-ids">
            <label>
              City where the ticket was issued
              <input id="liveIssuedCity" {...field("liveIssuedCity")} aria-required="true" value={v.issuedCity} onChange={set("issuedCity")} autoComplete="off" />
              {issue("liveIssuedCity")}
            </label>
            <label>
              State
              <input id="liveIssuedState" {...field("liveIssuedState")} aria-required="true" value={v.issuedState} onChange={set("issuedState")} maxLength={2} autoComplete="off" />
              {issue("liveIssuedState")}
            </label>
          </div>
          <fieldset className="local-ids single" style={{ border: 0, padding: 0 }}>
            <legend className="small" style={{ fontWeight: 750 }}>Who issued it?</legend>
            <label style={{ display: "flex", gap: "8px", alignItems: "center" }}>
              <input
                type="radio"
                disabled={submitting}
                name="issuerKind"
                checked={v.issuerKind === "GOVERNMENT"}
                onChange={() => setV({ ...v, issuerKind: "GOVERNMENT" })}
                style={{ width: "auto", minHeight: 0 }}
              />
              A city or public agency
            </label>
            <label style={{ display: "flex", gap: "8px", alignItems: "center" }}>
              <input
                type="radio"
                disabled={submitting}
                name="issuerKind"
                checked={v.issuerKind === "PRIVATE"}
                onChange={() => setV({ ...v, issuerKind: "PRIVATE" })}
                style={{ width: "auto", minHeight: 0 }}
              />
              A private parking lot or company
            </label>
          </fieldset>
        </>
      )}
      <div className="local-ids single">
        <label>
          Has the rental company already paid this ticket?
          <select id="liveRentalPaid" {...field("liveRentalPaid")} aria-required="true" value={v.rentalPaid} onChange={(e) => setV({ ...v, rentalPaid: e.target.value as LiveContactValues["rentalPaid"] })}>
            <option value="">Choose an answer</option>
            <option value="yes">Yes</option>
            <option value="no">No</option>
            <option value="not-rental">Not a rental car</option>
            <option value="unknown">I don&#39;t know</option>
          </select>
          {issue("liveRentalPaid")}
        </label>
      </div>
      <div className="local-ids single">
        <label>
          Your full name
          <input id="liveName" {...field("liveName")} aria-required="true" value={v.name} onChange={set("name")} autoComplete="name" />
          {issue("liveName")}
        </label>
        <label>
          Phone
          <input id="livePhone" type="tel" {...field("livePhone")} aria-required="true" value={v.phone} onChange={set("phone")} autoComplete="tel" inputMode="tel" />
          {issue("livePhone")}
        </label>
        <label>
          Email
          <input id="liveEmail" type="email" {...field("liveEmail")} aria-required="true" value={v.email} onChange={set("email")} autoComplete="email" inputMode="email" />
          {issue("liveEmail")}
        </label>
        <label>
          Mailing address
          <input id="liveLine1" {...field("liveLine1")} aria-required="true" value={v.line1} onChange={set("line1")} autoComplete="address-line1" />
          {issue("liveLine1")}
        </label>
        <label>
          Apartment or unit (optional)
          <input id="liveLine2" disabled={submitting} value={v.line2} onChange={set("line2")} autoComplete="address-line2" />
        </label>
        <label>
          City
          <input id="liveCity" {...field("liveCity")} aria-required="true" value={v.city} onChange={set("city")} autoComplete="address-level2" />
          {issue("liveCity")}
        </label>
      </div>
      <div className="local-ids">
        <label>
          ZIP code
          <input id="liveZip" {...field("liveZip")} aria-required="true" value={v.zip} onChange={set("zip")} autoComplete="postal-code" inputMode="numeric" />
          {issue("liveZip")}
        </label>
        <label>
          State
          <input id="liveState" {...field("liveState")} aria-required="true" value={v.state} onChange={set("state")} maxLength={2} autoComplete="address-level1" />
          {issue("liveState")}
        </label>
      </div>
      <div className="local-ids single">
        {!visionRequired && (
          <>
            <p className="small">{PHOTO_UPLOAD_GUIDANCE}</p>
            <label>
              Photo of the front of your ticket
              {ticketPhotoRef && <span className="small">Saved for this appeal; choose a file only to replace it.</span>}
              <input id="liveTicketPhoto" {...field("liveTicketPhoto")} aria-required="true" type="file" accept="image/jpeg,image/png" onChange={(e) => onSelectPhoto("ticket", e.target.files?.[0] ?? null)} />
              {issue("liveTicketPhoto")}
            </label>
            <label>
              Photo of the back of your ticket (optional)
              {ticketBackPhotoRef && <span className="small">Back of ticket saved for this appeal.</span>}
              <input ref={ticketBackInput} id="liveTicketBackPhoto" disabled={submitting} type="file" accept="image/jpeg,image/png" onChange={(e) => onSelectPhoto("back", e.target.files?.[0] ?? null)} />
            </label>
            {(ticketBackPhoto || ticketBackPhotoRef) && <button className="btn" type="button" disabled={submitting} onClick={() => { if (ticketBackInput.current) ticketBackInput.current.value = ""; onSelectPhoto("back", null); }}>Remove back of ticket</button>}
          </>
        )}
        <aside id="photoGuide" className="photo-guide" aria-labelledby="photoGuideTitle">
          <p className="eyebrow">Your choice</p>
          <h3 id="photoGuideTitle">Optional photo guide</h3>
          <p className="small">The photos you choose will be mailed with your letter. You can continue without these optional photos; there is no set number to add.</p>
          <p className="small">{PHOTO_UPLOAD_GUIDANCE}</p>
          <ol>
            <li><b>Ticket in place (optional)</b><p>If you have one, add a photo of the ticket on your vehicle. A readable ticket photo is collected separately above.</p></li>
            <li><b>Surroundings (optional)</b><p>From a safe place, photograph your vehicle and nearby signs, meters or curb markings you want included.</p></li>
            <li><b>A detail from your statement (optional)</b><p>Add a clear close-up of something you describe in your own words, such as a sign or meter display.</p></li>
          </ol>
          <p className="small">Only take photos when it is safe. Avoid other people’s faces and unrelated personal details. Any specific agency-required item is collected separately.</p>
        </aside>
        <p className="sr" role="status">{announcement}</p>
        <div id="liveEvidenceList">
          <p className="small">Supporting photos are optional and mailed with your letter. {evidencePhotos.length} selected.</p>
          {evidencePhotos.map((entry, index) => (
            <div key={entry.id} data-evidence-id={entry.id} className="evidence-item">
              <EvidenceThumbnail file={entry.file} number={index + 1} />
              <span className="evidence-name">{entry.file?.name ?? `Saved supporting photo ${index + 1}`}{entry.ref && <span className="small"> Saved for this appeal; inspect it in your packet.</span>}</span>
              <button className="btn" type="button" data-testid={`move-evidence-up-${entry.id}`} aria-label={`Move supporting photo ${index + 1} up`} disabled={submitting || index === 0} onClick={() => moveEvidence(entry,index,-1)}>Up</button>
              <button className="btn" type="button" data-testid={`move-evidence-down-${entry.id}`} aria-label={`Move supporting photo ${index + 1} down`} disabled={submitting || index === evidencePhotos.length - 1} onClick={() => moveEvidence(entry,index,1)}>Down</button>
              <button className="btn" type="button" data-testid={`remove-evidence-${entry.id}`} aria-label={`Remove supporting photo ${index + 1}`} disabled={submitting} onClick={() => {
                onRemoveEvidencePhoto(entry.id);
                setAnnouncement(`Photo ${index + 1} removed. ${evidencePhotos.length - 1} selected.`);
                requestAnimationFrame(() => document.getElementById("liveEvidencePhoto")?.focus());
              }}>Remove</button>
            </div>
          ))}
        </div>
        <label>
          Add supporting photos (optional)
          <input id="liveEvidencePhoto" aria-describedby="photoGuide" type="file" accept="image/jpeg,image/png" multiple disabled={submitting} onChange={(e) => { onAddEvidencePhotos(Array.from(e.target.files ?? [])); e.target.value = ""; }} />
        </label>
      </div>
      {(Object.keys(problems).length > 0 || error) && (
        <p className="err" role="alert">
          {Object.keys(problems).length > 0 ? "Check the marked fields above. Your entries are still here." : error}
        </p>
      )}
      <button className="btn btn-primary" type="submit" id="liveSubmit" disabled={busy} style={{ width: "100%", marginTop: "10px" }}>
        {busy ? <><span className="spinner" /> {submitting ? "Checking the agency…" : "Saving photos…"}</> : "Check the agency →"}
      </button>
    </form>
  );
}
