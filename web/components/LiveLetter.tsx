"use client";

import { HurtleGuide } from "./HurtleGuide";
import { LetterFitWarning } from "./Step6LetterStudio";
import React, { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { Draft, MailTier, PacketPreview } from "../lib/api";
import { FIRST_CLASS_LIMITS, mailClassName } from "../lib/fulfillment";
import { PacketReviewProgress } from "./CaseVisuals";
import { ExactPacketPages } from "./ExactPacketPages";
import { LegalDisclosure } from "./LegalDisclosure";
import { CustomerChoiceNotice } from "./CustomerChoiceRules";
import { Icon } from "./Icons";

type Affirmation = { declaration_id: string; text_sha256: string };

interface LiveLetterProps {
  draft: Draft;
  value?: string;
  onTextChange?: (text: string) => void;
  autoSave?: boolean;
  busy: boolean;
  error: string | null;
  betaTier?: MailTier | null;
  submissionPending?: boolean;
  approvalUncertain?: boolean;
  preview: PacketPreview | null;
  previewUrl: string | null;
  onInvalidatePreview: () => void;
  onPreview: (signature: Blob | null) => void;
  onSave: (text: string, replaceCustomerContent?: boolean) => void;
  onAdopt: () => void;
  onApprove: (affirmations: Affirmation[]) => void;
  onOpenRules?: () => void;
}

/** Adopt exact words, inspect the assembled packet and envelope, then authorize mailing. */
export function LiveLetter({
  draft,
  value,
  onTextChange,
  autoSave = false,
  busy,
  error,
  betaTier,
  submissionPending = false,
  approvalUncertain = false,
  preview,
  previewUrl,
  onInvalidatePreview,
  onPreview,
  onSave,
  onAdopt,
  onApprove,
  onOpenRules,
}: LiveLetterProps) {
  const [localText, setLocalText] = useState(draft.text);
  const text = value ?? localText;
  const setText = onTextChange ?? setLocalText;
  const [signed, setSigned] = useState(false);
  const [signatureFile, setSignatureFile] = useState<File | null>(null);
  const [signatureError, setSignatureError] = useState<string | null>(null);
  const [refused, setRefused] = useState(false);
  const pauseKey = `pb-letter-paused:${draft.case_id}`;

  useEffect(() => {
    try { if (sessionStorage.getItem(pauseKey) === "1") queueMicrotask(() => setRefused(true)); }
    catch { /* Private browsing can disable storage; the consent gate still applies. */ }
  }, [pauseKey]);

  const [reviewedUrl, setReviewedUrl] = useState<string | null>(null);
  const inspected = Boolean(preview && previewUrl && reviewedUrl === previewUrl);
  const [affirmedFor, setAffirmedFor] = useState<{ url: string | null; ids: Record<string, boolean> }>({ url: null, ids: {} });
  const affirmed = affirmedFor.url === previewUrl ? affirmedFor.ids : {};
  const setAffirmed = (declarationId: string, checked: boolean) => setAffirmedFor(previous => ({
    url: previewUrl,
    ids: { ...(previous.url === previewUrl ? previous.ids : {}), [declarationId]: checked },
  }));

  const declarations = preview?.declarations ?? [];
  const missingDeclarations = declarations.some(declaration => declaration.required && !affirmed[declaration.declaration_id]);
  const locked = submissionPending || approvalUncertain;
  const [editingAdopted, setEditingAdopted] = useState(Boolean(draft.adopted_sha256 && text !== draft.text));
  const canvas = useRef<HTMLCanvasElement>(null);
  const drawing = useRef(false);
  const signatureRevision = useRef(0);
  const approvalAttempt = useRef(0);
  const approvalInFlight = useRef(false);
  const mounted = useRef(true);
  const flags = draft.flags ?? [];
  const hasOmissions = flags.some((flag) => flag.details.some((detail) => detail.startsWith("omitted customer text:")));
  const adopted = Boolean(!flags.length && draft.adopted_sha256 && draft.text_sha256 && draft.adopted_sha256 === draft.text_sha256 && text === draft.text);
  const changed = text !== draft.text;
  const empty = text.trim().length === 0;
  const aiSpans = (draft.spans ?? []).filter((span) => span.author === "ai");
  const latest = useRef({ busy, submissionPending, approvalUncertain, adopted, editingAdopted, onPreview });

  useEffect(() => {
    if (refused || !autoSave || !changed || empty || busy || error || locked || (adopted && !editingAdopted)) return;
    const timer = setTimeout(() => onSave(text), 500);
    return () => clearTimeout(timer);
  }, [refused, autoSave, changed, empty, busy, error, locked, adopted, editingAdopted, onSave, text]);

  useLayoutEffect(() => {
    latest.current = { busy, submissionPending, approvalUncertain, adopted, editingAdopted, onPreview };
  }, [busy, submissionPending, approvalUncertain, adopted, editingAdopted, onPreview]);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      signatureRevision.current += 1;
      approvalAttempt.current += 1;
      approvalInFlight.current = false;
    };
  }, []);

  useEffect(() => {
    if (!busy) approvalInFlight.current = false;
  }, [busy]);

  const point = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const box = e.currentTarget.getBoundingClientRect();
    return [(e.clientX - box.left) * (e.currentTarget.width / box.width), (e.clientY - box.top) * (e.currentTarget.height / box.height)];
  };
  const start = (e: React.PointerEvent<HTMLCanvasElement>) => {
    if (busy || locked || editingAdopted || !adopted || approvalInFlight.current) return;
    const ctx = canvas.current?.getContext("2d");
    if (!ctx) return;
    onInvalidatePreview();
    setSignatureFile(null); setSignatureError(null);
    signatureRevision.current += 1;
    drawing.current = true;
    e.currentTarget.setPointerCapture(e.pointerId);
    const [x, y] = point(e);
    ctx.lineWidth = 3; ctx.lineCap = "round"; ctx.strokeStyle = "#111";
    ctx.beginPath(); ctx.moveTo(x, y);
  };
  const move = (e: React.PointerEvent<HTMLCanvasElement>) => {
    if (busy || locked || editingAdopted || !adopted || approvalInFlight.current) return;
    const ctx = canvas.current?.getContext("2d");
    if (!drawing.current || !ctx) return;
    const [x, y] = point(e);
    ctx.lineTo(x, y); ctx.stroke(); setSigned(true);
  };
  const paintBlank = useCallback(() => {
    const c = canvas.current;
    const ctx = c?.getContext("2d");
    if (c && ctx) { ctx.fillStyle = "#fff"; ctx.fillRect(0, 0, c.width, c.height); }
  }, []);
  const discardSignature = () => {
    onInvalidatePreview();
    signatureRevision.current += 1;
    approvalAttempt.current += 1;
    approvalInFlight.current = false;
    drawing.current = false;
    paintBlank();
    setSigned(false);
    setSignatureFile(null); setSignatureError(null);
  };
  const clear = () => {
    if (busy || locked) return;
    discardSignature();
  };
  const unlock = () => {
    if (busy || locked || !adopted) return;
    discardSignature();
    setEditingAdopted(true);
  };
  const cancelEdit = () => {
    if (busy || locked || !editingAdopted) return;
    discardSignature();
    setText(draft.text);
    setEditingAdopted(false);
  };
  const save = () => {
    if (busy || locked || empty || !changed) return;
    discardSignature();
    onSave(text);
  };
  const replaceOmittedContent = () => {
    if (busy || locked || empty || !hasOmissions) return;
    discardSignature();
    onSave(text, true);
  };
  const adopt = () => {
    if (busy || locked || adopted || changed || empty || flags.length) return;
    onAdopt();
  };
  const previewPacket = () => {
    const state = latest.current;
    if (state.busy || state.submissionPending || state.approvalUncertain || !state.adopted || state.editingAdopted || approvalInFlight.current) return;
    approvalInFlight.current = true;
    const attempt = ++approvalAttempt.current;
    const dispatch = (signature: Blob | null) => {
      const current = latest.current;
      if (
        !approvalInFlight.current ||
        attempt !== approvalAttempt.current ||
        current.busy ||
        !current.adopted ||
        current.editingAdopted ||
        current.submissionPending ||
        current.approvalUncertain ||
        !mounted.current
      ) {
        if (attempt === approvalAttempt.current) approvalInFlight.current = false;
        return;
      }
      approvalAttempt.current += 1;
      try {
        latest.current.onPreview(signature);
      } catch {
        approvalInFlight.current = false;
      }
    };
    if (signatureFile) { dispatch(signatureFile); return; }
    if (!signed || !canvas.current) {
      dispatch(null);
      return;
    }
    const revision = signatureRevision.current;
    const canvasElement = canvas.current;
    try {
      canvasElement.toBlob((blob) => {
        if (revision !== signatureRevision.current) {
          if (attempt === approvalAttempt.current) approvalInFlight.current = false;
          return;
        }
        dispatch(blob);
      }, "image/png");
    } catch {
      if (attempt === approvalAttempt.current) approvalInFlight.current = false;
    }
  };

  const approve = () => {
    if (busy || editingAdopted || !adopted || approvalInFlight.current || (!locked && !inspected) || missingDeclarations) return;
    approvalInFlight.current = true;
    onApprove(
      declarations
        .filter(declaration => affirmed[declaration.declaration_id])
        .map(declaration => ({ declaration_id: declaration.declaration_id, text_sha256: declaration.text_sha256 })),
    );
  };

  const pause = () => {
    if (busy || locked) return;
    discardSignature();
    setRefused(true);
    try { sessionStorage.setItem(pauseKey, "1"); } catch { /* Consent remains required. */ }
    requestAnimationFrame(() => document.getElementById("h-s6")?.focus());
  };

  if (refused && !locked) {
    return (
      <section className="screen on" id="s6" aria-labelledby="h-s6">
        <p className="eyebrow">Your choice · Mailing paused</p>
        <h2 id="h-s6" tabIndex={-1}>Your letter is paused.</h2>
        <div className="card state-card is-saved-card">
          <p role="status"><strong>You chose not to mail this letter.</strong> This action did not request mailing authorization.</p>
          <p className="small muted">Your saved letter and account remain available below. To correct a ticket fact or an address, start a new appeal from the top of the page. Any card hold follows its saved expiry.</p>
        </div>
        <label className="composer-label" htmlFor="pausedLetter">Your words</label>
        <textarea id="pausedLetter" className="letter-sheet-textarea" value={text} readOnly />
        <button
          type="button"
          className="btn btn-primary"
          id="liveResumeLetter"
          style={{ width: "100%", marginTop: "12px" }}
          onClick={() => {
            setRefused(false);
            try { sessionStorage.removeItem(pauseKey); } catch { /* Consent remains required. */ }
          }}
        >
          Resume reviewing my letter →
        </button>
      </section>
    );
  }

  return (
    <section className="screen on letter-workspace" id="s6" aria-labelledby="h-s6">
      <div className="voice">
        <HurtleGuide pose="letter" />
        <div>
          <p className="eyebrow">Step 5 · Your letter &amp; exact packet</p>
          <h2 id="h-s6" tabIndex={-1}>Read it, change anything, then adopt your exact words.</h2>
          <p className="lead">
            Nothing is mailed until you adopt this exact text, inspect the assembled packet and envelope, and approve it. Your ticket and supporting photos are printed after the letter, labelled as uploaded by you.
          </p>
        </div>
      </div>

      <PacketReviewProgress
        adopted={adopted}
        changed={changed}
        editingAdopted={editingAdopted}
        previewReady={Boolean(preview && previewUrl)}
        inspected={inspected}
        locked={locked}
        submissionPending={submissionPending}
        approvalUncertain={approvalUncertain}
      />

      {aiSpans.length > 0 && (
        <div className="card small reading-slip" id="liveAiSpans" style={{ marginBottom: "12px" }}>
          <b>Clerical formatting added during drafting:</b>
          <ul>{aiSpans.map((span) => <li key={span.id}>{span.text}</li>)}</ul>
        </div>
      )}

      {flags.length > 0 && (
        <div className="card small is-attention-card" id="liveDraftFlags" style={{ marginBottom: "12px" }}>
          <div className="section-head-row">
            <b>This letter still needs your edits before adoption.</b>
            <span className="status-pill is-attention">Needs edit</span>
          </div>
          <ul>
            {flags.flatMap((flag, index) => flag.details.map((detail, detailIndex) => (
              <li key={`${index}-${detailIndex}`}>{detail}</li>
            )))}
          </ul>
          <p>Restore missing words or edit unsupported wording directly in the letter below.</p>
          {hasOmissions && (
            <div style={{ marginTop: "10px" }}>
              <p className="small muted" style={{ marginBottom: "8px" }}>
                If you intend the current letter to replace the original content listed as omitted, use the button below. This clears only missing-content flags; unsupported wording still needs an edit.
              </p>
              <button className="btn btn-ghost" type="button" id="liveReplaceCustomerContent" disabled={busy || locked || empty} onClick={replaceOmittedContent}>
                Replace my original omitted content with this letter
              </button>
            </div>
          )}
        </div>
      )}

      <div className="letter-sheet">
        <div className="letter-sheet-head">
          <div className="row" style={{ gap: "8px" }}>
            <span className={`status-pill ${changed ? "is-attention" : adopted && !editingAdopted ? "is-saved" : "is-next"}`}>
              {changed
                ? "Unsaved edits"
                : adopted && !editingAdopted
                  ? "✓ Adopted exact wording"
                  : "Saved draft · Editable"}
            </span>
          </div>
          <div className="row" style={{ gap: "8px" }}>
            {adopted && !editingAdopted && (
              <button className="chip" type="button" id="liveEditAdopted" disabled={busy || locked} onClick={unlock}>
                Edit my letter
              </button>
            )}
            {!locked && (
              <button className="chip" type="button" id="liveRefuse" disabled={busy} onClick={pause}>
                Do not mail this letter
              </button>
            )}
          </div>
        </div>

        <label className="sr" htmlFor="liveLetterText">Letter text</label>
        <textarea
          id="liveLetterText"
          className={`letter-sheet-textarea${preview && adopted && !editingAdopted ? " is-preview-compact" : ""}`}
          aria-describedby={locked ? undefined : "live-letter-fit"}
          value={text}
          onChange={(e) => {
            if (!busy && !locked && (!adopted || editingAdopted)) {
              onInvalidatePreview();
              setText(e.target.value);
            }
          }}
          readOnly={busy || (adopted && !editingAdopted) || locked}
        />

        <div className="letter-sheet-foot">
          {!locked && <LetterFitWarning id="live-letter-fit" text={text} />}
          <p className="small" role="status">
            {changed
              ? "Your latest edits are not saved yet. Keep this page open."
              : "This exact text is saved for your appeal."}
          </p>
        </div>
      </div>

      {editingAdopted && (
        <div className="card is-attention-card" style={{ marginTop: "12px" }}>
          <p className="small">Saving changes means you will need to adopt the revised text again.</p>
          <div className="crow" style={{ marginTop: "10px" }}>
            <button className="btn btn-ghost" type="button" id="liveCancelEdit" disabled={busy || locked} onClick={cancelEdit}>
              Cancel edit
            </button>
            <button className="btn btn-primary" type="button" id="liveSaveEdit" disabled={busy || locked || empty || !changed} onClick={save}>
              Save my changes
            </button>
          </div>
        </div>
      )}

      {changed && !editingAdopted && !adopted && (
        <button className="btn btn-ghost" type="button" id="liveSaveEdit" disabled={busy || locked || empty} onClick={save} style={{ marginTop: "10px" }}>
          Save my changes
        </button>
      )}

      {!adopted && !changed && (
        <div className="card is-next-task" style={{ marginTop: "14px" }}>
          <p className="eyebrow">Step 5A · Adopt your wording</p>
          <p className="adoption-disclosure" id="liveAdoptionDisclosure" style={{ margin: "6px 0 12px" }}>
            {draft.adoption_screen_text}
          </p>
          <button
            aria-describedby="liveAdoptionDisclosure"
            className="btn btn-primary"
            type="button"
            id="liveAdopt"
            disabled={busy || locked || empty || flags.length > 0}
            onClick={adopt}
            style={{ width: "100%" }}
          >
            These are my exact words. Adopt this text →
          </button>
        </div>
      )}

      {adopted && !editingAdopted && (
        <div className="card signature-card">
          <div className="section-head-row">
            <div>
              <p className="eyebrow">Step 5B · Signature &amp; packet assembly</p>
              <h3>Add your signature (optional unless required)</h3>
            </div>
            {(signed || signatureFile) && <span className="status-pill is-saved">✓ Signature ready</span>}
          </div>
          <p className="small muted" style={{ margin: "4px 0 10px" }}>
            Agency signature requirements are checked before mailing. Adding a signature here does not authorize mailing.
          </p>
          <canvas
            ref={(element) => {
              if (element && canvas.current !== element) {
                canvas.current = element;
                paintBlank();
              }
            }}
            id="liveSignature"
            role="img"
            aria-label="Draw your signature"
            aria-describedby="signatureHelp"
            width={480}
            height={140}
            onPointerDown={start}
            onPointerMove={move}
            onPointerUp={() => { drawing.current = false; }}
            onPointerCancel={() => { drawing.current = false; }}
            style={{
              width: "100%",
              maxWidth: "520px",
              border: "1.5px solid var(--line-strong)",
              borderRadius: "12px",
              touchAction: "none",
              background: "#fff",
              pointerEvents: busy || locked ? "none" : "auto",
            }}
          />
          <p id="signatureHelp" className="small muted" style={{ marginTop: "6px" }}>
            Draw using your finger or pointer, or choose a PNG image of your own signature below.
          </p>
          <label className="signature-upload" htmlFor="liveSignatureFile">
            Choose a signature image (PNG)
            <input
              id="liveSignatureFile"
              type="file"
              accept="image/png"
              disabled={busy || locked}
              onChange={event => {
                const file = event.target.files?.[0];
                event.target.value = "";
                if (!file) return;
                discardSignature();
                if (file.type !== "image/png" || file.size > 5 * 1024 * 1024) {
                  setSignatureError("Choose a PNG signature image smaller than 5 MB.");
                  return;
                }
                setSignatureFile(file);
              }}
            />
          </label>
          {signatureFile && <p className="status-banner is-saved" role="status"><Icon id="check" size={14} /> Signature image selected: {signatureFile.name}. Check it in your exact packet.</p>}
          {signatureError && <p className="err" role="alert">{signatureError}</p>}
          <div className="crow">
            <button className="chip" type="button" disabled={busy || locked} onClick={clear}>Clear signature</button>
          </div>
          {!locked && (
            <button
              className="btn btn-primary"
              type="button"
              id="livePreview"
              disabled={busy || editingAdopted}
              onClick={previewPacket}
              style={{ width: "100%", marginTop: "14px" }}
            >
              {busy ? <><span className="spinner" /> Preparing your packet…</> : preview ? "Refresh exact packet preview →" : "Preview my exact packet →"}
            </button>
          )}

          {preview && previewUrl && (
            <div id="livePacketPreview" className="packet-preview">
              <div className="section-head-row">
                <div>
                  <p className="eyebrow">Step 5C · Exact packet &amp; envelope</p>
                  <h3>Inspect your exact mailing</h3>
                </div>
                <span className={`status-pill ${inspected ? "is-saved" : "is-attention"}`}>
                  {inspected ? "✓ Inspected" : `${preview.page_count} page${preview.page_count === 1 ? "" : "s"} · Inspect all`}
                </span>
              </div>
              <p className="small muted" style={{ marginBottom: "14px" }}>
                {preview.page_count} page{preview.page_count === 1 ? "" : "s"}. Check every page, the destination, and your return address before authorizing mailing.
              </p>

              <div className="envelope-window-card">
                <div className="envelope-top-row">
                  <div className="envelope-return">
                    <b>Return address</b>
                    <address>{preview.return_address.join("\n")}</address>
                  </div>
                  <div className="postmark-stamp" aria-hidden="true">
                    {betaTier ? mailClassName(betaTier) : "USPS MAIL"}
                  </div>
                </div>
                <div className="envelope-dest">
                  <b>Mail to</b>
                  <address>{preview.mail_to.join("\n")}</address>
                </div>
              </div>

              <div className="packet-file-links">
                <a className="chip" id="liveOpenPacket" href={previewUrl} target="_blank" rel="noopener noreferrer">
                  <Icon id="doc" size={14} /> Open exact PDF
                </a>
                <a className="chip" href={previewUrl} download="parkingbreaker-packet.pdf">
                  Download exact PDF
                </a>
              </div>

              <ExactPacketPages key={previewUrl} url={previewUrl} />

              {!locked && (
                <button id="liveCorrectBeforeMail" type="button" className="btn btn-ghost" disabled={busy} onClick={pause}>
                  Pause to correct ticket facts or addresses
                </button>
              )}

              <label className={`packet-inspect ${inspected ? "is-checked" : ""}`}>
                <input
                  id="livePacketReviewed"
                  type="checkbox"
                  checked={inspected}
                  disabled={busy || locked}
                  onChange={event => setReviewedUrl(event.target.checked ? previewUrl : null)}
                />
                <span>
                  I have inspected this exact PDF, checked the ticket facts, mailing destination and
                  return address, corrected any mistakes, and confirm that this is what I want mailed.
                </span>
              </label>

              <CustomerChoiceNotice onOpenRules={onOpenRules} />

              {declarations.length > 0 && (
                <fieldset id="liveDeclarations" className="declarations-box">
                  <legend><b>Declarations the agency requires</b></legend>
                  <p className="small muted">Each one starts unchecked. Affirm only what you mean; changing the packet means affirming again.</p>
                  {declarations.map(declaration => (
                    <label key={declaration.declaration_id} className={`packet-inspect ${affirmed[declaration.declaration_id] ? "is-checked" : ""}`}>
                      <input
                        id={`liveDeclaration_${declaration.declaration_id}`}
                        type="checkbox"
                        checked={Boolean(affirmed[declaration.declaration_id])}
                        disabled={busy || locked}
                        onChange={event => setAffirmed(declaration.declaration_id, event.target.checked)}
                      />
                      <span>
                        {declaration.verbatim_text}
                        {declaration.signature_required && <span className="small muted"> (requires your signature)</span>}
                      </span>
                    </label>
                  ))}
                </fieldset>
              )}
            </div>
          )}

          {preview && !previewUrl && (
            <p className="err" role="alert">The PDF could not be displayed. Preview the packet again before approving.</p>
          )}
          {preview && !locked && (
            <p className="small adoption-disclosure">
              {betaTier
                ? <>By choosing “Approve and mail”, you authorize us to send this exact packet once by {mailClassName(betaTier)} to the destination shown above for $0. No card hold or payment.{betaTier === "STANDARD" && ` ${FIRST_CLASS_LIMITS}`}</>
                : "By choosing ‘Approve and mail’, you authorize automated mailing of this exact packet to the destination shown above, using your existing service authorization."}
            </p>
          )}
          {!locked && <LegalDisclosure id="liveAttorneyDisclosure" />}
          {!locked && missingDeclarations && (
            <p role="alert" className="err" id="liveDeclarationsMissing">
              Affirm every required declaration above before approving.
            </p>
          )}
          {(preview || locked) && (
            <button
              className="btn btn-primary"
              type="button"
              id="liveApprove"
              disabled={busy || editingAdopted || (!locked && !inspected) || (!locked && missingDeclarations)}
              onClick={approve}
              style={{ width: "100%", marginTop: "12px" }}
            >
              {busy
                ? <><span className="spinner" /> Checking your mailing…</>
                : submissionPending
                  ? "Check mailing status →"
                  : approvalUncertain
                    ? "Retry this packet approval →"
                    : "Approve and mail →"}
            </button>
          )}
        </div>
      )}

      {error && <p className="err" role="alert">{error}</p>}
    </section>
  );
}
