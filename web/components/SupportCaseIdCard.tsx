"use client";

import React, { useState } from "react";

interface SupportCaseIdCardProps {
  caseId: string;
  compact?: boolean;
}

export function SupportCaseIdCard({ caseId, compact = false }: SupportCaseIdCardProps) {
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");

  async function handleCopy() {
    try {
      if (typeof navigator !== "undefined" && navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
        await navigator.clipboard.writeText(caseId);
        setCopyState("copied");
        window.setTimeout(() => setCopyState("idle"), 2500);
        return;
      }
      setCopyState("failed");
    } catch {
      setCopyState("failed");
    }
  }

  return (
    <div
      className={`card support-case-id${compact ? " support-case-id-compact" : ""}`}
      id="supportCaseIdCard"
      aria-labelledby="supportCaseIdHeading"
    >
      <div className="support-case-id-head">
        <div>
          <p className="eyebrow" id="supportCaseIdHeading">Your case ID</p>
          <p className="support-case-id-value mono" id="supportCaseIdValue">{caseId}</p>
        </div>
        <button
          type="button"
          className="chip support-case-id-copy"
          id="copyCaseIdBtn"
          onClick={() => { void handleCopy(); }}
        >
          {copyState === "copied" ? "Copied" : "Copy case ID"}
        </button>
      </div>
      <p className="support-case-id-note">
        Save this ID — you will need it to contact support by phone or email.
      </p>
      <p className="support-case-id-rule">
        We can only respond to calls and emails that include your case ID, so save it.
      </p>
      <span className="sr-only" role="status" aria-live="polite">
        {copyState === "copied"
          ? `Copied ${caseId} to clipboard.`
          : copyState === "failed"
            ? `Copy unavailable. Please record ${caseId} manually.`
            : ""}
      </span>
    </div>
  );
}
