"use client";

import { useEffect, useRef, useState } from "react";
import { api, ApiError, type CaseStatus, type PacketPreview, type Tracking } from "../lib/api";
import { FIRST_CLASS_LIMITS, fulfillmentCopy, mailClassName, mailingConfirmed, matchingTracking, paymentCopy } from "../lib/fulfillment";
import { formatSupportCaseId } from "../lib/support-case-id";
import { PostalEventTrail } from "./CaseVisuals";
import { SupportCaseIdCard } from "./SupportCaseIdCard";
import { ConciergeMark } from "./ConciergeMark";
import { Icon } from "./Icons";

interface Props {
  status: CaseStatus;
  tracking: Tracking | null;
  busy: boolean;
  error: string | null;
  onRefresh: () => void;
}

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
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
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
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  return (
    <div className="stack" aria-busy={busy}>
      {!review && (
        <button className="btn btn-primary" type="button" disabled={busy} onClick={() => { void open(); }}>
          Check corrected packet
        </button>
      )}
      {review && (
        <>
          <a className="chip" href={review.url} target="_blank" rel="noopener">Open corrected packet (PDF)</a>
          <p><b>Mail to</b><br />{review.packet.mail_to.join(", ")}</p>
          <p><b>Return address</b><br />{review.packet.return_address.join(", ")}</p>
          {declarations.map(item => (
            <label key={item.declaration_id} className="packet-inspect">
              <input type="checkbox" checked={Boolean(affirmed[item.declaration_id])} onChange={e => setAffirmed({ ...affirmed, [item.declaration_id]: e.target.checked })} />
              <span>{item.verbatim_text}</span>
            </label>
          ))}
          <label className="packet-inspect">
            <input type="checkbox" checked={inspected} onChange={e => setInspected(e.target.checked)} />
            <span>I reviewed this exact packet and its mailing addresses.</span>
          </label>
          <div className="crow">
            <button className="btn btn-primary" type="button" disabled={busy || !inspected || missing} onClick={() => { void approve(); }}>
              Approve this free re-mail
            </button>
            <button className="btn btn-ghost" type="button" disabled={busy} onClick={() => { setReview(null); setInspected(false); setError(null); }}>
              Close preview
            </button>
          </div>
        </>
      )}
      {error && <p className="err" role="alert">{error}</p>}
    </div>
  );
}

/** Saved postal and payment records remain separate, including returns and delayed capture. */
export function LiveFulfillment({ status, tracking, busy, error, onRefresh }: Props) {
  const copy = fulfillmentCopy(status, tracking);
  const supportCaseId = formatSupportCaseId(status.support_code);
  const matched = matchingTracking(status, tracking);
  const tier = status.selected_tier ?? matched?.tier;
  const firstClass = tier === "STANDARD";
  const remedy = status.return_remedy;
  const proof = firstClass ? null : remedy?.proof_packet;
  const betaClosed = remedy?.kind === "BETA_RETURN_CLOSED";
  const [proofUrl, setProofUrl] = useState<{ sha256: string; url: string } | null>(null);
  const mailed = matched !== null && mailingConfirmed(status, matched);
  const recordKey = `${status.case_id}:${remedy?.reason_code ?? "original"}:${matched?.tracking_number ?? ""}`;
  const [record, setRecord] = useState<{ key: string; url: string } | null>(null);
  const [recordError, setRecordError] = useState<string | null>(null);

  useEffect(() => () => { if (record) URL.revokeObjectURL(record.url); }, [record]);

  async function prepareRecord() {
    setRecordError(null);
    try {
      const saved = await api.mailingRecord(status.case_id);
      const bytes = Uint8Array.from(atob(saved.pdf_base64), c => c.charCodeAt(0));
      const hash = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)), b => b.toString(16).padStart(2, "0")).join("");
      if (hash !== saved.pdf_sha256) {
        setRecordError("The mailing record could not be verified. Try again later.");
        return;
      }
      setRecord({ key: recordKey, url: URL.createObjectURL(new Blob([bytes], { type: "application/pdf" })) });
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
        const hash = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)), b => b.toString(16).padStart(2, "0")).join("");
        if (!active || hash !== proof.pdf_sha256) return;
        url = URL.createObjectURL(new Blob([bytes], { type: "application/pdf" }));
        setProofUrl({ sha256: proof.pdf_sha256, url });
      } catch { /* Missing or invalid proof never becomes a fabricated receipt. */ }
    })();
    return () => { active = false; if (url) URL.revokeObjectURL(url); };
  }, [proof]);

  const refundUnderReview = status.nonmailing_refund != null && status.nonmailing_refund !== "refunded";
  const paymentSettled = !refundUnderReview && ["NO_CHARGE", "CAPTURED", "AUTHORIZED", "RELEASED", "REFUNDED"].includes(status.payment_state ?? "");
  const isReturned = status.state === "RETURNED" || status.state === "CLOSED_RETURNED";

  return (
    <section className="screen on fulfillment" id="s7" aria-labelledby="h-s7" aria-busy={busy}>
      <div className="voice">
        <ConciergeMark size={44} state={busy ? "thinking" : mailed && !isReturned ? "done" : "idle"} />
        <div>
          <p className="eyebrow">Step 6 · Your mailing record</p>
          <h2 id="h-s7" tabIndex={-1}>{copy.title}</h2>
          <p className="lead">{copy.detail}</p>
        </div>
      </div>

      {supportCaseId && <SupportCaseIdCard caseId={supportCaseId} compact />}

      <div className={`card state-card payment-card ${paymentSettled ? "is-saved-card" : "is-attention-card"}`} role="status">
        <div className="section-head-row">
          <h3>Payment</h3>
          <span className={`status-pill ${paymentSettled ? "is-saved" : "is-attention"}`}>
            {paymentSettled ? "✓ Recorded" : "Checking"}
          </span>
        </div>
        <p>{paymentCopy(status)}</p>
      </div>

      <div className={`card state-card postal-card postal-frank-card ${isReturned ? "is-attention-card" : mailed ? "is-saved-card" : ""}`} id={mailed ? "liveTracking" : "mailingEvents"}>
        <div className="postal-head">
          <h3><Icon id="mail" size={18} /> {firstClass ? mailClassName(tier) : "Postal record"}</h3>
          {!firstClass && matched && (
            <span className={`bdg ${matched.events.length ? "s-ok" : "s-unk"}`}>
              {matched.events.length ? `${matched.events.length} reported` : "No events yet"}
            </span>
          )}
        </div>
        {firstClass ? (
          <p className="small muted">{FIRST_CLASS_LIMITS}</p>
        ) : (
          <>
            {tier === "CERTIFIED" && (
              <p className="tracking-number">
                <b>Certified tracking number:</b>{" "}
                <span className="mono">{matched?.tracking_number ?? "not reported yet"}</span>
              </p>
            )}
            {matched?.events.length ? (
              <>
                <p className="small muted">Only the events the saved postal record reports appear below, with their recorded times and wording.</p>
                <PostalEventTrail events={matched.events} />
              </>
            ) : (
              <p className="muted small">No postal events are available yet. Check again later.</p>
            )}
            {tier === "CERTIFIED" && (
              <p className="small muted">Certified Mail supplies postal records. It does not change the agency’s filing requirements or establish its decision.</p>
            )}
          </>
        )}
        {mailed && (
          record?.key === recordKey ? (
            <a className="btn btn-ghost" id="liveMailingRecord" href={record.url} download="parkingbreaker-mailing-record.pdf">
              <Icon id="doc" size={16} /> Download your mailing record (PDF)
            </a>
          ) : (
            <button className="btn btn-ghost" id="livePrepareMailingRecord" type="button" onClick={() => { void prepareRecord(); }}>
              <Icon id="doc" size={16} /> Get your mailing record (PDF)
            </button>
          )
        )}
        {mailed && (
          <p className="small muted">
            Your mailing record contains a copy of your exact approved packet.{!firstClass && " Postal tracking, when available, is shown above. This is not proof of delivery or a USPS Return Receipt."}
          </p>
        )}
        {recordError && <p className="err" role="alert">{recordError}</p>}
      </div>

      {remedy && (
        <div className="card state-card is-attention-card" id="liveReturnRemedy">
          <div className="section-head-row">
            <h3>Your returned mailing</h3>
            <span className="status-pill is-attention">Needs review</span>
          </div>
          <p>{remedy.message}</p>
          {remedy.refund_status && !betaClosed && (
            <p><b>{remedy.refund_status === "completed" ? "Refund completed" : "Refund pending"}</b> · ${(remedy.refund_cents / 100).toFixed(2)}</p>
          )}
          {remedy.remail && !betaClosed && <p>The saved remedy includes a free re-mail. No additional card authorization is requested here.</p>}
          {remedy.reason_code === "REMAIL_REVIEW_AVAILABLE" && <RemailReview key={status.case_id} caseId={status.case_id} onRefresh={onRefresh} />}
          {proof && proofUrl?.sha256 === proof.pdf_sha256 && <a className="chip" href={proofUrl.url} download="parkingbreaker-mailing-record.pdf">Download saved mailing evidence</a>}
          {proof && proofUrl?.sha256 !== proof.pdf_sha256 && <p className="small">The saved evidence file is not available to download yet. Try refreshing this record.</p>}
        </div>
      )}

      {error && <p className="err" role="alert">{error}</p>}
      <button className="btn btn-primary" id="liveApprove" type="button" onClick={onRefresh} disabled={busy}>
        {busy ? "Checking saved status…" : "Check mailing status →"}
      </button>
      {status.payment_state === "NO_CHARGE" ? (
        <p className="small muted">This selected beta appeal is free. If something goes wrong, we will review the saved mailing record. No second mailing is automatically authorized.</p>
      ) : (
        <p className="small muted">You can return using this appeal’s private link. Our mailing mistakes are ours to fix under the return policy.</p>
      )}
    </section>
  );
}
