"use client";

import { useState } from "react";
import Link from "next/link";
import { BrandLogo } from "./BrandLogo";

type Status =
  | "ready"
  | "opted_out"
  | "recorded"
  | "pending"
  | "invalid"
  | "error";

async function post(path: string, body: Record<string, string>): Promise<number> {
  const response = await fetch(`/api${path}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
    credentials: "omit",
    cache: "no-store",
  });
  return response.status;
}

export function OutcomeLink({ caseId, token }: { caseId: string; token: string }) {
  const [status, setStatus] = useState<Status>(() =>
    caseId && token ? "ready" : "invalid",
  );
  const [busy, setBusy] = useState(false);

  async function act(kind: "opt-out" | "dismissed" | "upheld" | "pending") {
    if (busy || !caseId || !token) return;
    setBusy(true);
    try {
      const path = kind === "opt-out" ? "/outcomes/opt-out" : "/outcomes/report";
      let body: Record<string, string> = { case_id: caseId, token };
      if (kind !== "opt-out") body = { ...body, decision: kind };
      const code = await post(path, body);
      if (code === 200) {
        setStatus(kind === "opt-out" ? "opted_out" : kind === "pending" ? "pending" : "recorded");
      } else if (code === 400) {
        setStatus("invalid");
      } else {
        setStatus("error");
      }
    } catch {
      setStatus("error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="access-shell">
      <Link className="access-brand" href="/" aria-label="ParkingBreaker home">
        <BrandLogo />
      </Link>
      <section className="access-card">
        <h1>Your appeal follow-up</h1>
        {status === "invalid" && (
          <p role="alert">
            This link is not valid. If you followed a recent email, open the link from that message again.
          </p>
        )}
        {status === "error" && (
          <p role="alert">
            We could not record your choice just now. Please try again in a moment.
          </p>
        )}
        {status === "opted_out" && (
          <p>
            You will not receive further follow-up reminders about this appeal. Your case record is
            unchanged.
          </p>
        )}
        {status === "recorded" && <p>Thank you — we recorded what the agency told you.</p>}
        {status === "pending" && (
          <p>Thank you. We will leave the outcome unrecorded until you hear back.</p>
        )}
        {status === "ready" && (
          <>
            <p>
              If you have heard from the agency, tell us what it said. If you have not heard back,
              choose that option and we will keep the outcome unrecorded.
            </p>
            <div className="outcome-actions">
              <button className="btn btn-primary" type="button" disabled={busy} onClick={() => act("dismissed")}>
                The agency dismissed it
              </button>
              <button className="btn btn-ghost" type="button" disabled={busy} onClick={() => act("upheld")}>
                The agency upheld it
              </button>
              <button className="btn btn-ghost" type="button" disabled={busy} onClick={() => act("pending")}>
                I have not heard back yet
              </button>
            </div>
            <p className="outcome-fineprint">
              ParkingBreaker prepares appeals clerically and mails them by USPS Certified Mail. We
              do not give legal advice and do not promise any outcome.{" "}
              <button className="link-button" type="button" disabled={busy} onClick={() => act("opt-out")}>
                Stop follow-up reminders
              </button>
              .
            </p>
          </>
        )}
      </section>
    </main>
  );
}
