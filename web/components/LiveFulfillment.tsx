"use client";

import { useEffect, useRef, useState } from "react";
import { api, ApiError, type CaseStatus, type PacketPreview, type Tracking } from "../lib/api";
import { fulfillmentCopy, mailingConfirmed, paymentCopy } from "../lib/fulfillment";
import { PostalEventTrail } from "./CaseVisuals";

interface Props { status: CaseStatus; tracking: Tracking | null; busy: boolean; error: string | null; onRefresh: () => void }

function RemailReview({ caseId, onRefresh }: { caseId: string; onRefresh: () => void }) {
  const [review, setReview] = useState<{ packet: PacketPreview; adopted: string; url: string } | null>(null);
  const [inspected, setInspected] = useState(false);
  const [affirmed, setAffirmed] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inFlight = useRef(false);
  useEffect(() => () => { if (review) URL.revokeObjectURL(review.url); }, [review]);

  async function open() {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true); setError(null); setReview(null); setInspected(false); setAffirmed({});
    try {
      const draft = await api.draft(caseId);
      if (!draft.adopted_sha256) throw new Error("No adopted letter");
      const packet = await api.previewRemail(caseId, draft.adopted_sha256);
      const bytes = Uint8Array.from(atob(packet.pdf_base64), c => c.charCodeAt(0));
      const hash = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)), b => b.toString(16).padStart(2, "0")).join("");
      if (hash !== packet.packet_sha256) throw new Error("Packet hash mismatch");
      setReview({ packet, adopted: draft.adopted_sha256, url: URL.createObjectURL(new Blob([bytes], { type: "application/pdf" })) });
    } catch {
      setError("A corrected packet is unavailable. Check your mailing status for the latest update.");
    } finally { inFlight.current = false; setBusy(false); }
  }

  const declarations = review?.packet.declarations ?? [];
  const missing = declarations.some(item => item.required && !affirmed[item.declaration_id]);
  async function approve() {
    if (!review || !inspected || missing || inFlight.current) return;
    inFlight.current = true;
    setBusy(true); setError(null);
    try {
      await api.approve(caseId, {
        remail: true,
        adopted_sha256: review.adopted,
        packet_sha256: review.packet.packet_sha256,
        envelope_sha256: review.packet.envelope_sha256,
        affirmations: declarations.filter(item => affirmed[item.declaration_id]).map(item => ({ declaration_id: item.declaration_id, text_sha256: item.text_sha256 })),
      });
      setReview(null);
      setInspected(false);
      onRefresh();
    } catch (failure) {
      if (failure instanceof ApiError && failure.status >= 400 && failure.status < 500) {
        setReview(null);
        setError("The corrected packet could not be approved. Check again before approving another packet.");
      } else {
        setError("We could not confirm your approval. Retry this same packet or check mailing status.");
      }
    } finally { inFlight.current = false; setBusy(false); }
  }

  return <div aria-busy={busy}>
    {!review && <button className="btn" type="button" disabled={busy} onClick={() => { void open(); }}>Check corrected packet</button>}
    {review && <>
      <a href={review.url} target="_blank" rel="noopener">Open corrected packet (PDF)</a>
      <p><b>Mail to</b><br />{review.packet.mail_to.join(", ")}</p>
      <p><b>Return address</b><br />{review.packet.return_address.join(", ")}</p>
      {declarations.map(item => <label key={item.declaration_id}>
        <input type="checkbox" checked={Boolean(affirmed[item.declaration_id])} onChange={e => setAffirmed({ ...affirmed, [item.declaration_id]: e.target.checked })} /> {item.verbatim_text}
      </label>)}
      <p><label><input type="checkbox" checked={inspected} onChange={e => setInspected(e.target.checked)} /> I reviewed this exact packet and its mailing addresses.</label></p>
      <button className="btn" type="button" disabled={busy || !inspected || missing} onClick={() => { void approve(); }}>Approve this free re-mail</button>
      <button className="btn" type="button" disabled={busy} onClick={() => { setReview(null); setInspected(false); setError(null); }}>Close preview</button>
    </>}
    {error && <p role="alert">{error}</p>}
  </div>;
}

/** Saved postal and payment records remain separate, including returns and delayed capture. */
export function LiveFulfillment({status, tracking, busy, error, onRefresh}: Props) {
  const copy = fulfillmentCopy(status, tracking);
  const matched = tracking?.case_id === status.case_id ? tracking : null;
  const remedy = status.return_remedy;
  const proof = remedy?.proof_packet;
  // A closed free-beta return carries the backend's closed-case message; no money or
  // second mailing exists, so its refund and re-mail rows are never shown.
  const betaClosed = remedy?.kind === "BETA_RETURN_CLOSED";
  const [proofUrl, setProofUrl] = useState<{sha256: string; url: string} | null>(null);
  const mailed = matched !== null && mailingConfirmed(status, matched);
  const recordKey = `${status.case_id}:${remedy?.reason_code ?? "original"}:${matched?.tracking_number ?? ""}`;
  const [record, setRecord] = useState<{key: string; url: string} | null>(null);
  const [recordError, setRecordError] = useState<string | null>(null);
  useEffect(() => () => { if (record) URL.revokeObjectURL(record.url); }, [record]);
  async function prepareRecord() {
    setRecordError(null);
    try {
      const saved = await api.mailingRecord(status.case_id);
      const bytes = Uint8Array.from(atob(saved.pdf_base64), c => c.charCodeAt(0));
      const hash = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256",bytes)), b => b.toString(16).padStart(2,"0")).join("");
      // A record whose bytes do not match its saved hash is never offered.
      if (hash !== saved.pdf_sha256) { setRecordError("The mailing record could not be verified. Try again later."); return; }
      setRecord({key: recordKey, url: URL.createObjectURL(new Blob([bytes],{type:"application/pdf"}))});
    } catch {
      setRecordError("The mailing record is not available yet. Try again later.");
    }
  }
  useEffect(() => {
    if (!proof) return;
    let active = true;
    let url: string | null = null;
    void (async () => {
      try {
        const bytes = Uint8Array.from(atob(proof.pdf_base64), c => c.charCodeAt(0));
        const hash = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256",bytes)), b => b.toString(16).padStart(2,"0")).join("");
        if (!active || hash !== proof.pdf_sha256) return;
        url = URL.createObjectURL(new Blob([bytes],{type:"application/pdf"}));
        setProofUrl({sha256: proof.pdf_sha256,url});
      } catch { /* Missing or invalid proof never becomes a fabricated receipt. */ }
    })();
    return () => { active = false; if (url) URL.revokeObjectURL(url); };
  }, [proof]);
  return <section className="screen on fulfillment" id="s7" aria-labelledby="h-s7" aria-busy={busy}>
    <div className="voice"><div>
      <p className="eyebrow">Your mailing record</p>
      <h2 id="h-s7" tabIndex={-1}>{copy.title}</h2>
      <p className="lead">{copy.detail}</p>
    </div></div>
    <div className="card state-card payment-card" role="status"><h3>Payment</h3><p>{paymentCopy(status)}</p></div>
    <div className="card state-card postal-card" id={mailed ? "liveTracking" : "mailingEvents"}>
      <div className="postal-head">
        <h3>Postal record</h3>
        {matched && <span className={`bdg ${matched.events.length ? "s-ok" : "s-unk"}`}>{matched.events.length ? `${matched.events.length} reported` : "No events yet"}</span>}
      </div>
      {matched?.tier !== "STANDARD" && (
        <p className="tracking-number"><b>Certified tracking number:</b> <span className="mono">{matched?.tracking_number ?? "not reported yet"}</span></p>
      )}
      {matched?.events.length ? (
        <>
          <p className="small muted">Only the events the saved postal record reports appear below, with their recorded times and wording.</p>
          <PostalEventTrail events={matched.events} />
        </>
      ) : <p className="muted small">No postal events are available yet. Check again later.</p>}
      <p className="small muted">Certified Mail supplies postal records. It does not change the agency’s filing requirements or establish its decision.</p>
      {mailed && (record?.key === recordKey
        ? <a id="liveMailingRecord" href={record.url} download="parkingbreaker-mailing-record.pdf">Download your mailing record (PDF)</a>
        : <button className="btn" id="livePrepareMailingRecord" type="button" onClick={() => { void prepareRecord(); }}>Get your mailing record (PDF)</button>)}
      {mailed && <p className="small muted">Your mailing record is a copy of the exact packet the mail provider accepted. Postal tracking, when available, is shown above. This is not proof of delivery or a USPS Return Receipt.</p>}
      {recordError && <p className="small" role="alert">{recordError}</p>}
    </div>
    {remedy && <div className="card state-card" id="liveReturnRemedy">
      <h3>Your returned mailing</h3><p>{remedy.message}</p>
      {remedy.refund_status && !betaClosed && <p><b>{remedy.refund_status === "completed" ? "Refund completed" : "Refund pending"}</b> · ${(remedy.refund_cents / 100).toFixed(2)}</p>}
      {remedy.remail && !betaClosed && <p>The saved remedy includes a free re-mail. No additional card authorization is requested here.</p>}
      {remedy.reason_code === "REMAIL_REVIEW_AVAILABLE" && <RemailReview key={status.case_id} caseId={status.case_id} onRefresh={onRefresh} />}
      {proof && proofUrl?.sha256 === proof.pdf_sha256 && <a href={proofUrl.url} download="parkingbreaker-mailing-record.pdf">Download saved mailing evidence</a>}
      {proof && proofUrl?.sha256 !== proof.pdf_sha256 && <p className="small">The saved evidence file is not available to download yet. Try refreshing this record.</p>}
    </div>}
    {error && <p className="err" role="alert">{error}</p>}
    <button className="btn btn-primary" id="liveApprove" type="button" onClick={onRefresh} disabled={busy}>{busy ? "Checking saved status…" : "Check mailing status →"}</button>
    {status.payment_state === "NO_CHARGE"
      ? <p className="small muted">This selected beta appeal is free. If something goes wrong, we will review the saved mailing record. No second mailing is automatically authorized.</p>
      : <p className="small muted">You can return using this appeal’s private link. Our mailing mistakes are ours to fix under the return policy.</p>}
  </section>;
}
