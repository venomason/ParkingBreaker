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
  // Real flow: the customer types each detail (no sample reading); photos come later.
  live?: boolean;
  // Real owner-scoped ticket reading/review/confirmation path, discovered after workspace restore/creation.
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

export function Step1Ticket({ fields, setFields, plate, setPlate, plateState, setPlateState, backPhoto, setBackPhoto, onComplete, setActionBar, live = false, vision, scenePhotos }: Step1TicketProps) {
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

  const visionActive = live && Boolean(vision) && (vision?.mode === "vision" || (started && vision?.mode !== "legacy"));

  useEffect(() => {
    if (!hasScanned || isScanning || visionActive) return;
    reviewCard.current?.scrollIntoView({ block: "center", behavior: "instant" });
  }, [hasScanned, isScanning, visionActive, fieldIdx, editingField]);

  useEffect(() => {
    if (!isScanning) return;
    const timer = setTimeout(() => {
      setIsScanning(false); setHasScanned(true);
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
      if (vision) {
        setStarted(true);
        // The capability answer decides whether this stays on the reading path or falls
        // back to the legacy manual entry; no state is set from an effect.
        void vision.onStart().then((mode) => {
          if (mode === "legacy") beginLegacyEntry();
        }).catch(() => {});
        return;
      }
      beginLegacyEntry();
      return;
    }
    setHasScanned(true); setIsScanning(true); setThought("Reading your ticket…");
  }, [live, vision, beginLegacyEntry]);

  const handleSaveField = useCallback(() => {
    const val = editValue.trim();
    if (!val) { setFieldErr(true); document.getElementById("fixIn")?.focus(); return; }
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
        setActionBar("Checking how your ticket is read…", <button className="btn btn-primary" type="button" disabled><span className="spinner" /> Checking…</button>);
        return;
      }
      if (vision.error) {
        setActionBar("We couldn’t check the ticket reading service.", <button className="btn btn-primary" type="button" id="visionRetryCheck" onClick={() => { void vision.onStart(); }}>Try again</button>);
        return;
      }
      if (!vision.frontRef) {
        setActionBar("Add a front photo of your ticket to read it.", null);
        return;
      }
      if (vision.confirmedCommandId && !vision.dirty) {
        setActionBar("Ticket facts confirmed for these exact photos.", <button className="btn btn-primary" type="button" id="actContinueTicket" onClick={continueFromVision}>Continue to your side →</button>);
        return;
      }
      setActionBar("Confirm your ticket facts before continuing.", null);
      return;
    }

    if (!hasScanned && !isScanning) {
      setActionBar(
        <>
          <Icon id="shield" size={14} /> No charge unless your letter is mailed
        </>,
        <button
          className="btn btn-primary"
          type="button"
          id="actSnap"
          onClick={handleStartScan}
        >
          {live ? (vision ? "Add ticket photo →" : "Enter ticket details →") : "Use sample front photo →"}
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
          `Correcting ${currentField.label}`,
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
      "Step 1 complete · Plate stays local",
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
  }, [visionActive, vision, hasScanned, isScanning, fieldIdx, editingField, editValue, currentField, plate, plateState, fields, backPhoto, onComplete, setActionBar, handleSaveField, handleStartScan, live, continueFromVision]);

  return (
    <section className="screen on" id="s1" aria-labelledby="h-s1">
      <div className="voice">
        <HurtleGuide pose="ticket" />
        <div>
          <p className="eyebrow">Step 1 · Your ticket</p>
          <h2 id="h-s1" tabIndex={-1}>
            Start with your ticket.
          </h2>
          <p className="lead">
            {visionActive
              ? "Upload your ticket photos, check the unverified reading suggestions, and confirm the facts we will use for your letter."
              : live
                ? (vision ? "Start with the front. Check the issuing agency and ticket details before you continue." : "Enter each detail as printed on your ticket. Keep the front and back handy.")
                : "Try the sample ticket. You’ll check the details one at a time."}
          </p>
          <p className="thought" aria-live="polite">
            {thought}
          </p>
        </div>
      </div>

      {visionActive && vision && (
        <div id="s1grid">
          <section className="card" id="ticketVision" aria-labelledby="visionTitle" aria-busy={Boolean(vision.busy)}>
            <p className="eyebrow">Read from your photos</p>
            <h2 id="visionTitle" style={{ margin: "4px 0 8px" }}>Your ticket photos</h2>
            <p id="ticketPhotoFormats" className="muted small">{PHOTO_UPLOAD_GUIDANCE}</p>
            <p className="muted small" style={{ marginBottom: "10px" }}>
              The front photo is required. Your uploaded photos are processed by an AI reading
              service, which may see your plate, name or address printed on the ticket. The reading is only a set of
              suggestions; you confirm every fact below.
            </p>
            {vision.error && <p className="err" role="alert">{vision.error}</p>}
            {vision.checking && <p className="small" role="status">Checking how your ticket is read…</p>}
            <div className="local-ids single">
              <div>
                <strong>Front of ticket · Required</strong>
                <div className="picker-row">
                  <PhotoPicker id="visionFrontCamera" label="Take front photo" camera disabled={Boolean(vision.busy) || vision.checking} onPick={files => vision.onPickPhoto("ticket", files[0])} />
                  <PhotoPicker id="visionFrontPhoto" label="Choose front photo" disabled={Boolean(vision.busy) || vision.checking} onPick={files => vision.onPickPhoto("ticket", files[0])} />
                </div>
                {vision.frontRef && <span className="small" id="visionFrontSaved">Front photo saved for this appeal.</span>}
              </div>
              <div>
                <strong>Back of ticket</strong>
                <div className="picker-row">
                  <PhotoPicker id="visionBackCamera" label="Take back photo" camera disabled={Boolean(vision.busy) || vision.checking} onPick={files => vision.onPickPhoto("back", files[0])} />
                  <PhotoPicker id="visionBackPhoto" label="Choose back photo" disabled={Boolean(vision.busy) || vision.checking} onPick={files => vision.onPickPhoto("back", files[0])} />
                </div>
                {vision.backRef && <span className="small" id="visionBackSaved">Back photo saved for this appeal.</span>}
              </div>
              {vision.backRef && (
                <button className="btn" type="button" disabled={Boolean(vision.busy)} onClick={() => vision.onPickPhoto("back", null)}>Remove back of ticket</button>
              )}
            </div>
            <div className="row" style={{ gap: "8px", flexWrap: "wrap", marginTop: "10px" }}>
              {(!vision.reading || vision.reading.status === "missing" || !vision.reading.current) && (
                <button className="btn btn-primary" type="button" id="visionReadBtn" disabled={!vision.frontRef || Boolean(vision.busy)} onClick={vision.onRead}>
                  {vision.busy === "reading" ? <><span className="spinner" /> Reading…</> : "Read my ticket photos"}
                </button>
              )}
              {(vision.reading?.status === "pending" || vision.reading?.status === "unknown") && (
                <button className="btn" type="button" id="visionCheckReading" disabled={Boolean(vision.busy)} onClick={vision.onCheckReading}>
                  {vision.busy === "reading" ? "Checking…" : "Check reading status"}
                </button>
              )}
            </div>
            <p className="small" id="visionReadingState" role="status" aria-live="polite">{readingStateText(vision.reading)}</p>
            {vision.status?.synthetic && <p className="muted small" id="visionSynthetic">This environment returns synthetic readings for acceptance testing; no provider is called.</p>}
            {vision.reading && vision.reading.status !== "success" && vision.reading.status !== "missing" && (
              <p className="muted small">You can supply and confirm the facts yourself. If an essential fact is unavailable, stop here until you can provide it.</p>
            )}
            {vision.confirmedCommandId && !vision.dirty && (
              <p id="visionConfirmed" role="status" className="small" style={{ marginTop: "8px", color: "var(--sage)" }}>
                Ticket facts confirmed for these exact photos and facts. Any photo or fact change requires a new comparison and confirmation.
              </p>
            )}
            {vision.dirty && (
              <p id="visionDirty" role="status" className="err">
                Your ticket facts or photos changed. The previous comparison and confirmation no longer apply; compare and confirm again.
              </p>
            )}
          </section>

          {scenePhotos}
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
        </div>
      )}

      {!hasScanned && !visionActive && (
        <div id="capture" className="card capture">
          <div className="ticket-prep">
            <p><strong>Front of your ticket</strong><span>Keep the citation number, date and issuing agency readable.</span></p>
            <details className="guide-notes">
              <summary>Photo tips and the back of the ticket</summary>
              <p>Photograph both sides in good light. Keep all four corners in frame so dates, the issuing agency and any instructions stay readable.</p>
              {!live && <button className="chip" type="button" id="backCapture" aria-pressed={backPhoto}
                onClick={() => setBackPhoto(!backPhoto)}>
                {backPhoto ? "✓ Back photo added" : "Add sample back photo"}
              </button>}
            </details>
          </div>
          <div className="privacy-line">
            <Icon id="lock" size={18} style={{ flex: "none" }} />
            <span>
              {live
                ? "Your uploaded ticket photos are processed by an AI reading service. Because the photos may show your plate, name or address, those details may reach that service. You confirm or correct every fact before it is used."
                : "Your name, plate and address never go to an AI. They’re added to your letter only at the end."}
            </span>
          </div>
          {!live && <p className="muted small" style={{ fontSize: "12.5px" }}>
            Prototype uses a synthetic citation. The plate is omitted from the sample image.
          </p>}
        </div>
      )}

      {!visionActive && scenePhotos}

      {hasScanned && !visionActive && (
        <div id="s1grid">
          {!live && <div className="vf" id="ticketWrap">
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
              className={`ticket ${compact && fieldIdx < fields.length ? "compact" : ""} ${
                isScanning ? "scanning" : ""
              }`}
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
          </div>}

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
                  <div className="card fcur" aria-live="polite">
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
                      onChange={(e) => setEditValue(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") {
                          e.preventDefault();
                          handleSaveField();
                        }
                      }}
                      autoComplete="off"
                      autoFocus
                    />
                    <p className="err" role="alert" id="fieldEntryError">{fieldErr ? "Enter this detail as printed on your ticket." : ""}</p>
                    <p className="hint">Press Enter or tap Save below.</p>
                  </div>
                ) : (
                  <div className="card fcur" aria-live="polite">
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
              <div id="s1done" className="card fcur" ref={reviewCard}>
                <p className="k" style={{ color: "var(--sage)" }}>
                  {live ? "✓ Ticket details entered" : "✓ All five read details confirmed"}
                </p>
                <h2 style={{ margin: "4px 0 8px" }}>Your ticket is ready.</h2>
                <p className="muted small">
                  {live ? "Enter the plate number and state exactly as printed on your ticket. These identifiers are added to your letter and never sent to an AI." : "San Diego asks for the plate number and state in the letter. This entry stays local to the letter and never goes to an AI."}
                </p>
                <div className="local-ids">
                  <label>
                    Plate number
                    <input
                      id="plateIn" aria-invalid={plateErr} aria-describedby="plateErr"
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
                      id="plateState" aria-invalid={plateErr} aria-describedby="plateErr"
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
    </section>
  );
}
