"use client";

import { useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import { AccessPanel } from "./CustomerAccess";
import { PilotFeedback } from "./PilotFeedback";

type LinkDetails = { caseId: string; token: string; optOutToken: string | null };

export function PilotFeedbackLink() {
  const initialized = useRef(false);
  const [link, setLink] = useState<LinkDetails | null>();
  const [optOut, setOptOut] = useState<"idle" | "busy" | "done" | "error">("idle");

  useEffect(() => {
    if (initialized.current) return;
    initialized.current = true;
    const params = new URLSearchParams(window.location.hash.slice(1));
    const caseId = params.get("case");
    const token = params.get("token");
    const optOutToken = params.get("opt_out_token");
    window.history.replaceState(window.history.state, "", window.location.pathname);
    queueMicrotask(() => setLink(caseId && token ? { caseId, token, optOutToken } : null));
  }, []);

  async function stopReminders() {
    if (!link?.optOutToken || optOut === "busy" || optOut === "done") return;
    setOptOut("busy");
    try {
      await api.optOutOfFollowUp({ case_id: link.caseId, token: link.optOutToken });
      setOptOut("done");
    } catch {
      setOptOut("error");
    }
  }

  return <AccessPanel>
    <h1>Share feedback on your appeal</h1>
    {link === undefined && <p role="status">Opening your feedback link…</p>}
    {link === null && <p role="alert">This feedback link is invalid. Open the latest follow-up message for your appeal.</p>}
    {link && <>
      <PilotFeedback caseId={link.caseId} token={link.token} />
      <p className="small muted">If the agency sent you a notice, you can optionally share photos through your private appeal link.</p>
      {link.optOutToken && optOut !== "done" && <button className="link-button" type="button" disabled={optOut === "busy"} onClick={() => { void stopReminders(); }}>
        {optOut === "busy" ? "Saving your preference…" : "Stop follow-up reminders"}
      </button>}
      {optOut === "done" && <p role="status">Your follow-up reminders have been stopped.</p>}
      {optOut === "error" && <p role="alert">We couldn’t save your reminder preference. Try again.</p>}
    </>}
  </AccessPanel>;
}
