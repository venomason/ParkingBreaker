"use client";

import { HurtleGuide } from "./HurtleGuide";
import { PhotoPicker } from "./CurbsidePhotos";
import React, { useState, useEffect, useRef, useCallback } from "react";
import { Icon } from "./Icons";
import { TicketFactReview } from "./TicketFactReview";
import { PHOTO_UPLOAD_GUIDANCE } from "../lib/api";
import type {
  TicketDiscrepancyResolution,
  TicketReadingSnapshot,
  TicketReadingStatus,
  TicketReviewSnapshot,
} from "../lib/api";
import { validateFactDraft, type AdoptedFacts, type FactDraft, type TicketLiteral } from "../lib/intake";

export type { TicketField } from "../lib/api";
import type { TicketField } from "../lib/api";

export type Step1VisionMode = "unknown" | "legacy" | "vision";

export interface Step1VisionProps {
  workspaceId?: string | null;
  mode: Step1VisionMode;
  enabled: boolean;
  required: boolean;
  checking: boolean;
  error: string | null;
  status: TicketReadingStatus | null;
  reading: TicketReadingSnapshot | null;
  review: TicketReviewSnapshot | null;
  literal: TicketLiteral | null;
  draft: FactDraft;
  setDraft: (next: FactDraft) => void;
  resolutions: Record<string, TicketDiscrepancyResolution>;
  setResolutions: (next: Record<string, TicketDiscrepancyResolution>) => void;
  busy: "reading" | "review" | "confirm" | null;
  confirmedCommandId: string | null;
  dirty: boolean;
  frontRef: string | null;
  backRef: string | null;
  onStart: () => Promise<Step1VisionMode>;
  onPickPhoto: (kind: "ticket" | "back", file: File | null) => void;
  onRead: () => void;
  onCheckReading: () => void;
  onSaveFacts: (facts: AdoptedFacts, customerSupplied?: boolean) => void;
  onConfirm: () => void;
  onContinue: (facts: AdoptedFacts) => void;
  onOpenRules?: () => void;
}

interface Step1TicketProps {
  fields: TicketField[];
  setFields: (fields: TicketField[]) => void;
  plate: string;
  setPlate: (plate: string) => void;
  plateState: string;
  setPlateState: (state: string) => void;
  backPhoto: boolean;
  setBackPhoto: (value: boolean) => void;
  onComplete: (data: { fields: TicketField[]; plate: string; plateState: string; backPhoto: boolean }) => void;
  setActionBar: (sub: React.ReactNode, buttons: React.ReactNode) => void;
  live?: boolean;
  canCharge?: boolean;
  vision?: Step1VisionProps;
  scenePhotos?: React.ReactNode;
}

function readingStateText(reading: TicketReadingSnapshot | null) {
  if (!reading || reading.status === "missing") return "Your ticket has not been read yet.";
  if (reading.status === "pending") return "The reading is still being processed. It will not be repeated automatically.";
  if (reading.status === "unknown") return "The reading result is unknown. Check its saved status or enter the facts yourself below; it will not be repeated automatically.";
  if (!reading.current) return "Your photos changed since this reading. Read your current photos again.";
  if (reading.status === "refused") return "We couldn’t read these photos. Enter the facts yourself below or upload a clearer photo. Re-reading the same photo returns the saved result.";
  if (!reading.fields.some((field) => field.state === "shown" && field.value)) return "We couldn’t read any ticket details. Enter the facts yourself below. You must supply every required fact before confirming.";
  return "Reading complete. These are unverified suggestions; check them below.";
}

export function Step1Ticket({
  fields,
  setFields,
  plate,
  setPlate,
  plateState,
  setPlateState,
  backPhoto,
  setBackPhoto,
  onComplete,
  setActionBar,
  live = false,
  canCharge = !live,
  vision,
  scenePhotos,
}: Step1TicketProps) {
  const savedCount = live ? fields.findIndex(field => !field.v.trim()) : 0;
  const initialIndex = savedCount === -1 ? fields.length : savedCount;
  const hasSaved = live && fields.some(field => field.v.trim());
  const [fieldIdx, setFieldIdx] = useState<number>(initialIndex);
  const [hasScanned, setHasScanned] = useState<boolean>(hasSaved);
  const [started, setStarted] = useState<boolean>(false);
  const [isScanning, setIsScanning] = useState<boolean>(false);
  const [editingField, setEditingField] = useState<boolean>(hasSaved && initialIndex < fields.length);
  const [editValue, setEditValue] = useState<string>("");
  const [compact, setCompact] = useState<boolean>(true);
  const [fieldErr, setFieldErr] = useState(false);
  const [plateErr, setPlateErr] = useState<boolean>(false);
  const [thought, setThought] = useState<string>("");

  const reviewCard = useRef<HTMLDivElement>(null);

  const photoModeAvailable = Boolean(vision && vision.mode !== "legacy");
  const visionActive = live && Boolean(vision) && (vision?.mode === "vision" || (started && vision?.mode !== "legacy"));

  useEffect(() => {
    if (!hasScanned || isScanning || visionActive) return;
    reviewCard.current?.scrollIntoView({ block: "center", behavior: "instant" });
  }, [hasScanned, isScanning, visionActive, fieldIdx, editingField]);

  useEffect(() => {
    if (!isScanning) return;
    const timer = setTimeout(() => {
      setIsScanning(false);
      setHasScanned(true);
      setThought("We found five details. Let’s check them together, one at a time.");
    }, 1200);
    return () => clearTimeout(timer);
  }, [isScanning]);

  const beginLegacyEntry = useCallback(() => {
    setHasScanned(true);
    setThought("Type each detail exactly as it is printed on your ticket.");
    setEditValue("");
    setEditingField(true);
  }, []);

  const handleStartScan = useCallback(() => {
    if (live) {
      if (vision && vision.mode !== "legacy") {
        setStarted(true);
        void vision.onStart().then((mode) => {
          if (mode === "legacy") beginLegacyEntry();
        }).catch(() => {});
        return;
      }
      beginLegacyEntry();
      return;
    }
    setHasScanned(true);
    setIsScanning(true);
    setThought("Reading your ticket…");
  }, [live, vision, beginLegacyEntry]);

  const handleSaveField = useCallback(() => {
    const val = editValue.trim();
    if (!val) {
      setFieldErr(true);
      document.getElementById("fixIn")?.focus();
      return;
    }
    setFieldErr(false);
    const next = [...fields];
    next[fieldIdx] = {
      ...next[fieldIdx],
      v: val,
      t: val.toUpperCase(),
    };
    setFields(next);
    setFieldIdx(fieldIdx + 1);
    const following = next[fieldIdx + 1];
    if (live && following && !following.v.trim()) {
      setEditValue("");
      return;
    }
    setEditingField(false);
  }, [editValue, fields, fieldIdx, setFields, live]);

  const currentField = fields[fieldIdx];

  const continueFromVision = useCallback(() => {
    if (!vision) return;
    const result = validateFactDraft(vision.draft);
    if (!result.facts) {
      requestAnimationFrame(() => document.querySelector<HTMLElement>('#visionFactsForm [aria-invalid="true"]')?.focus());
      return;
    }
    vision.onContinue(result.facts);
  }, [vision]);

  // Action Bar Synchronization
  useEffect(() => {
    if (visionActive && vision) {
      if (vision.checking) {
        setActionBar(
          "Checking how your ticket is read…",
          <button className="btn btn-primary" type="button" disabled>
            <span className="spinner" /> Checking…
          </button>
        );
        return;
      }
      if (vision.error) {
        setActionBar(
          "Needs attention · Check the message above",
          <button className="btn btn-primary" type="button" id="visionRetryCheck" onClick={() => { void vision.onStart(); }}>
            Try again
          </button>
        );
        return;
      }
      if (!vision.frontRef) {
        setActionBar("Next: add a clear photo of the front of your ticket above", null);
        return;
      }
      if (vision.confirmedCommandId && !vision.dirty) {
        setActionBar(
          "Saved · Ticket facts confirmed for these exact photos",
          <button className="btn btn-primary" type="button" id="actContinueTicket" onClick={continueFromVision}>
            Continue to your side →
          </button>
        );
        return;
      }
      setActionBar("Next: check and confirm your ticket facts above", null);
      return;
    }

    if (!hasScanned && !isScanning) {
      setActionBar(
        <>
          <Icon id="shield" size={14} />{" "}
          {canCharge ? "No charge unless your letter is mailed" : "Nothing is mailed without your exact approval"}
        </>,
        <button
          className="btn btn-primary"
          type="button"
          id="actSnap"
          onClick={handleStartScan}
        >
          {live ? (photoModeAvailable ? <><Icon id="camera" size={18} /> Add ticket photo →</> : "Enter ticket details →") : "Use sample front photo →"}
        </button>
      );
      return;
    }

    if (isScanning) {
      setActionBar(
        "Reading citation fields locally…",
        <button className="btn btn-primary" type="button" disabled>
          <span className="spinner" /> Scanning ticket…
        </button>
      );
      return;
    }

    if (fieldIdx < fields.length) {
      if (editingField) {
        setActionBar(
          `Entering detail ${fieldIdx + 1} of ${fields.length} · ${currentField.label}`,
          <>
            <button
              className="btn btn-ghost"
              type="button"
              id="actCancelFix"
              onClick={() => setEditingField(false)}
            >
              Cancel
            </button>
            <button
              className="btn btn-primary"
              type="button"
              id="actSaveFix"
              onClick={handleSaveField}
            >
              Save &amp; continue
            </button>
          </>
        );
      } else {
        setActionBar(
          `Confirming detail ${fieldIdx + 1} of ${fields.length}`,
          <>
            <button
              className="btn btn-ghost"
              type="button"
              id="actFixField"
              onClick={() => {
                setEditValue(currentField.v);
                setEditingField(true);
              }}
            >
              Fix it
            </button>
            <button
              className="btn btn-primary"
              type="button"
              id="actOkField"
              onClick={() => {
                if (live && !currentField.v.trim()) {
                  setEditValue("");
                  setEditingField(true);
                  return;
                }
                setFieldIdx(fieldIdx + 1);
              }}
            >
              <Icon id="check" size={18} /> Looks right
            </button>
          </>
        );
      }
      return;
    }

    // All fields confirmed
    setActionBar(
      "Saved · Ticket details ready",
      <button
        className="btn btn-primary"
        type="button"
        id="actContinueTicket"
        onClick={() => {
          const p = plate.trim().toUpperCase();
          const s = plateState.trim().toUpperCase();
          if (!p || !/^[A-Z]{2}$/.test(s)) {
            setPlateErr(true);
            document.getElementById(!p ? "plateIn" : "plateState")?.focus();
            return;
          }
          onComplete({ fields, plate: p, plateState: s, backPhoto });
        }}
      >
        Continue to your side →
      </button>
    );
  }, [visionActive, vision, photoModeAvailable, canCharge, hasScanned, isScanning, fieldIdx, editingField, editValue, currentField, plate, plateState, fields, backPhoto, onComplete, setActionBar, handleSaveField, handleStartScan, live, continueFromVision]);

  const isFirstLanding = !hasScanned && !visionActive;

  return (
    <section className="screen on" id="s1" aria-labelledby="h-s1">
      <div className={`voice${isFirstLanding ? " curbside-hero-voice" : ""}`}>
        <HurtleGuide pose="ticket" />
        <div>
          <p className="eyebrow">{isFirstLanding ? "Curbside parking appeal · Clerical mail service" : "Step 1 · Your ticket"}</p>
          <h1 id="h-s1" tabIndex={-1}>
            {isFirstLanding
              ? "Appeal your parking ticket in your own words."
              : visionActive
                ? "Add and confirm your ticket."
                : "Check your ticket details."}
          </h1>
          <p className="lead">
            {isFirstLanding
              ? live
                ? photoModeAvailable
                  ? "We read your ticket photo, check the agency’s official mailing instructions, and format your statement into a ready-to-mail packet. You approve the exact PDF before anything is sent."
                  : "Enter the details printed on your ticket. We check the agency’s official mailing instructions and prepare a packet you inspect and approve before mailing."
                : "Try a sample parking ticket appeal from curbside check to exact-PDF approval. Nothing is sent or charged in this prototype."
              : visionActive
                ? "Upload your ticket photos, check the unverified reading suggestions, and confirm every fact we will print in your letter."
                : live
                  ? "Enter each detail exactly as printed on your ticket. Keep the front and back handy."
                  : "Check the five sample details one at a time."}
          </p>
          {thought && (
            <p className="thought" aria-live="polite">
              {thought}
            </p>
          )}
        </div>
      </div>

      {isFirstLanding && (
        <div id="capture" className="card capture curbside-start-card">
          <div className="curbside-route-strip" aria-label="How ParkingBreaker works">
            <div className="route-chip is-active">
              <span className="route-num">01</span>
              <span>{live && !photoModeAvailable ? "Enter ticket" : "Ticket photo"}</span>
            </div>
            <div className="route-chip">
              <span className="route-num">02</span>
              <span>Your account</span>
            </div>
            <div className="route-chip">
              <span className="route-num">03</span>
              <span>Exact PDF mail</span>
            </div>
          </div>
          <div className="ticket-prep">
            <p>
              <strong>{live && !photoModeAvailable ? "Have the paper ticket in hand" : "Keep all four corners of the ticket in frame"}</strong>
              <span>Make sure the citation number, issue date, and issuing agency are clear and readable.</span>
            </p>
            <details className="guide-notes">
              <summary>Photo tips, back of ticket &amp; privacy</summary>
              <p>Photograph the front first, plus the back if it prints appeal instructions. Keep glare off the printed lines.</p>
              <p className="small muted" style={{ marginTop: "6px" }}>
                {live
                  ? "Uploaded ticket photos are processed by an AI reading service, which may see your plate, name, or address on the ticket. You confirm or correct every fact before it is used."
                  : "Your name, plate, and address never go to an AI in this sample. Prototype uses a synthetic citation."}
              </p>
              {!live && (
                <button
                  className="chip"
                  type="button"
                  id="backCapture"
                  aria-pressed={backPhoto}
                  onClick={() => setBackPhoto(!backPhoto)}
                >
                  {backPhoto ? "✓ Back photo added" : "Add sample back photo"}
                </button>
              )}
            </details>
          </div>
        </div>
      )}

      {visionActive && vision && (
        <div id="s1grid">
          <section className="card status-card" id="ticketVision" aria-labelledby="visionTitle" aria-busy={Boolean(vision.busy)}>
            <div className="section-head-row">
              <div>
                <p className="eyebrow">Step 1A · Ticket photos</p>
                <h2 id="visionTitle">Your ticket photos</h2>
              </div>
              <span className={`status-pill ${vision.frontRef ? "is-saved" : "is-attention"}`}>
                {vision.frontRef ? "✓ Front saved" : "Required next"}
              </span>
            </div>
            {vision.error && <p className="err" role="alert">{vision.error}</p>}
            {vision.checking && <p className="status-banner is-attention" role="status">Checking how your ticket is read…</p>}

            <div className="ticket-upload-slots">
              <div className={`ticket-slot ${vision.frontRef ? "is-saved" : "is-attention"}`}>
                <div className="ticket-slot-head">
                  <strong>Front of ticket</strong>
                  <span className={`bdg ${vision.frontRef ? "s-ok" : "t-imp"}`}>{vision.frontRef ? "Saved" : "Required"}</span>
                </div>
                <div className="picker-row">
                  <PhotoPicker id="visionFrontCamera" label="Take front photo" camera primary={!vision.frontRef} disabled={Boolean(vision.busy) || vision.checking} onPick={files => vision.onPickPhoto("ticket", files[0])} />
                  <PhotoPicker id="visionFrontPhoto" label="Choose front photo" disabled={Boolean(vision.busy) || vision.checking} onPick={files => vision.onPickPhoto("ticket", files[0])} />
                </div>
                {vision.frontRef && <span className="slot-saved-note" id="visionFrontSaved"><Icon id="check" size={14} /> Front photo saved for this appeal.</span>}
              </div>

              <div className={`ticket-slot ${vision.backRef ? "is-saved" : ""}`}>
                <div className="ticket-slot-head">
                  <strong>Back of ticket</strong>
                  <span className={`bdg ${vision.backRef ? "s-ok" : "s-na"}`}>{vision.backRef ? "Saved" : "Optional"}</span>
                </div>
                <div className="picker-row">
                  <PhotoPicker id="visionBackCamera" label="Take back photo" camera disabled={Boolean(vision.busy) || vision.checking} onPick={files => vision.onPickPhoto("back", files[0])} />
                  <PhotoPicker id="visionBackPhoto" label="Choose back photo" disabled={Boolean(vision.busy) || vision.checking} onPick={files => vision.onPickPhoto("back", files[0])} />
                </div>
                {vision.backRef && (
                  <div className="row" style={{ justifyContent: "space-between", marginTop: "6px" }}>
                    <span className="slot-saved-note" id="visionBackSaved"><Icon id="check" size={14} /> Back photo saved for this appeal.</span>
                    <button className="chip" type="button" disabled={Boolean(vision.busy)} onClick={() => vision.onPickPhoto("back", null)}>Remove back of ticket</button>
                  </div>
                )}
              </div>
            </div>

            <p id="ticketPhotoFormats" className="muted small" style={{ marginTop: "12px" }}>{PHOTO_UPLOAD_GUIDANCE}</p>
            <p className="muted small" style={{ marginTop: "6px" }}>
              Uploaded photos are processed by an AI reading service, which may see your plate, name, or address printed on the ticket. Reading results are unverified suggestions—you confirm every fact below.
            </p>

            <div className="row" style={{ gap: "8px", flexWrap: "wrap", marginTop: "14px" }}>
              {(!vision.reading || vision.reading.status === "missing" || !vision.reading.current) && (
                <button className="btn btn-primary" type="button" id="visionReadBtn" disabled={!vision.frontRef || Boolean(vision.busy)} onClick={vision.onRead}>
                  {vision.busy === "reading" ? <><span className="spinner" /> Reading ticket…</> : "Read my ticket photos →"}
                </button>
              )}
              {(vision.reading?.status === "pending" || vision.reading?.status === "unknown") && (
                <button className="btn btn-ghost" type="button" id="visionCheckReading" disabled={Boolean(vision.busy)} onClick={vision.onCheckReading}>
                  {vision.busy === "reading" ? "Checking…" : "Check reading status"}
                </button>
              )}
            </div>

            <p className="small status-line" id="visionReadingState" role="status" aria-live="polite">{readingStateText(vision.reading)}</p>
            {vision.status?.synthetic && <p className="muted small" id="visionSynthetic">This environment returns synthetic readings for acceptance testing; no provider is called.</p>}
            {vision.reading && vision.reading.status !== "success" && vision.reading.status !== "missing" && (
              <p className="muted small">You can supply and confirm the facts yourself. If an essential fact is unavailable, stop here until you can provide it.</p>
            )}
            {vision.confirmedCommandId && !vision.dirty && (
              <p id="visionConfirmed" role="status" className="status-banner is-saved">
                <Icon id="check" size={16} />
                <span>Ticket facts confirmed for these exact photos and facts. Any photo or fact change requires a new comparison and confirmation.</span>
              </p>
            )}
            {vision.dirty && (
              <p id="visionDirty" role="status" className="err">
                Your ticket facts or photos changed. The previous comparison and confirmation no longer apply; compare and confirm again.
              </p>
            )}
          </section>

          {vision.frontRef ? (
            <TicketFactReview
              key={vision.workspaceId ?? "no-workspace"}
              workspaceId={vision.workspaceId}
              draft={vision.draft}
              onDraftChange={(next) => vision.setDraft(next)}
              reading={vision.reading}
              review={vision.review}
              resolutions={vision.resolutions}
              onResolutionsChange={vision.setResolutions}
              busy={Boolean(vision.busy)}
              locked={false}
              dirty={vision.dirty}
              confirmed={Boolean(vision.confirmedCommandId) && !vision.dirty}
              onSaveFacts={vision.onSaveFacts}
              onConfirm={vision.onConfirm}
              onOpenRules={vision.onOpenRules}
            />
          ) : null}

          {scenePhotos}
        </div>
      )}

      {hasScanned && !visionActive && (
        <div id="s1grid">
          {!live && (
            <div className="vf" id="ticketWrap">
              <div className="vf-bar">
                <span>Sample Citation Optical Loupe</span>
                <span className="vf-badge" id="loupeBadge">
                  {isScanning
                    ? "Scanning…"
                    : fieldIdx < fields.length
                      ? `${fieldIdx + 1} of ${fields.length} · ${fields[fieldIdx].label}`
                      : "5/5 Confirmed"}
                </span>
              </div>
              <div
                className={`ticket ${compact && fieldIdx < fields.length ? "compact" : ""} ${isScanning ? "scanning" : ""}`}
                id="ticket"
                aria-label="Photo of your ticket (sample)"
              >
                <div className="th">
                  CITY OF SAN DIEGO<small>NOTICE OF PARKING VIOLATION</small>
                </div>
                {fields.map((f, i) => {
                  const isHl = i === fieldIdx;
                  const isOk = i < fieldIdx;
                  return (
                    <div
                      key={f.k}
                      className={`tr ${isHl ? "hl" : ""} ${isOk ? "ok" : ""}`}
                      data-f={f.k}
                    >
                      <span>{f.label.toUpperCase()}</span>
                      <span data-v={f.k}>{f.t}</span>
                    </div>
                  );
                })}
                <div className="tr na">
                  <span>PLATE</span>
                  <span>(omitted)</span>
                </div>
              </div>
              <div className="ticket-foot">
                {fieldIdx < fields.length ? (
                  <button
                    type="button"
                    className="ticket-toggle"
                    id="toggleTicketView"
                    onClick={() => setCompact(!compact)}
                  >
                    {compact ? "Show all ticket lines" : "Focus active line"}
                  </button>
                ) : (
                  <span />
                )}
                <button
                  type="button"
                  className="ticket-toggle"
                  id="backLater"
                  aria-pressed={backPhoto}
                  onClick={() => setBackPhoto(!backPhoto)}
                >
                  {backPhoto ? "✓ Back photo added" : "+ Back photo"}
                </button>
              </div>
            </div>
          )}

          <div id="fieldsWrap">
            <div className="fprog" id="fprog" aria-hidden="true">
              {fields.map((_, i) => (
                <i
                  key={i}
                  className={i < fieldIdx ? "on" : i === fieldIdx ? "cur" : ""}
                />
              ))}
            </div>

            <ol className="flist" id="flist" aria-label="Confirmed details — tap any pill to re-open">
              {fields.slice(0, fieldIdx).map((f, idx) => (
                <li key={f.k}>
                  <button
                    type="button"
                    onClick={() => {
                      setFieldIdx(idx);
                      setEditingField(false);
                      setCompact(true);
                    }}
                    title={`Tap to re-check ${f.label}`}
                  >
                    <span className="ck">
                      <Icon id="check" size={10} style={{ color: "#fff" }} />
                    </span>
                    <span className="k">{f.label}:</span>
                    <span className="v">{f.v}</span>
                  </button>
                </li>
              ))}
            </ol>

            {fieldIdx < fields.length && (
              <div key={fieldIdx} id="fcur" ref={reviewCard}>
                {editingField ? (
                  <div className="card fcur is-next-task" aria-live="polite">
                    <p className="k">
                      Editing detail {fieldIdx + 1} of {fields.length} · {currentField.label}
                    </p>
                    <label className="sr" htmlFor="fixIn">
                      Correct {currentField.label}
                    </label>
                    <input
                      id="fixIn"
                      aria-invalid={fieldErr}
                      aria-describedby="fieldEntryError"
                      value={editValue}
                      onChange={(e) => { setEditValue(e.target.value); setFieldErr(false); }}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") {
                          e.preventDefault();
                          handleSaveField();
                        }
                      }}
                      autoComplete="off"
                      autoFocus
                    />
                    <p className="err" role="alert" id="fieldEntryError">
                      {fieldErr ? "Enter this detail as printed on your ticket." : ""}
                    </p>
                    <p className="hint">{currentField.hint} Press Enter or tap Save &amp; continue below.</p>
                  </div>
                ) : (
                  <div className="card fcur is-next-task" aria-live="polite">
                    <p className="k">
                      Detail {fieldIdx + 1} of {fields.length} · {currentField.label}
                    </p>
                    <p className="v">{currentField.v}</p>
                    <p className="hint">{currentField.hint} Is this right?</p>
                  </div>
                )}
              </div>
            )}

            {fieldIdx >= fields.length && (
              <div id="s1done" className="card fcur is-saved-head" ref={reviewCard}>
                <p className="k" style={{ color: "var(--sage)" }}>
                  {live ? "✓ Ticket details entered" : "✓ All five read details confirmed"}
                </p>
                <h2 style={{ margin: "4px 0 8px" }}>Your ticket is ready.</h2>
                <p className="muted small">
                  {live
                    ? "Enter the plate number and state exactly as printed on your ticket. These identifiers are added to your letter and never sent to an AI."
                    : "San Diego asks for the plate number and state in the letter. This entry stays local to the letter and never goes to an AI."}
                </p>
                <div className="local-ids">
                  <label>
                    Plate number
                    <input
                      id="plateIn"
                      aria-invalid={plateErr}
                      aria-describedby="plateErr"
                      autoComplete="off"
                      value={plate}
                      onChange={(e) => {
                        setPlate(e.target.value);
                        setPlateErr(false);
                      }}
                      required
                    />
                    {!live && <span className="sr" id="plateHelp">Synthetic prototype value</span>}
                  </label>
                  <label>
                    State
                    <input
                      id="plateState"
                      aria-invalid={plateErr}
                      aria-describedby="plateErr"
                      autoComplete="off"
                      value={plateState}
                      maxLength={2}
                      onChange={(e) => {
                        setPlateState(e.target.value);
                        setPlateErr(false);
                      }}
                      required
                      aria-label="Two-letter plate state"
                    />
                  </label>
                </div>
                {plateErr && (
                  <p className="err" id="plateErr" role="alert">
                    Enter a plate number and 2-letter state to continue.
                  </p>
                )}
              </div>
            )}
          </div>
        </div>
      )}

      {!visionActive && scenePhotos}
    </section>
  );
}
