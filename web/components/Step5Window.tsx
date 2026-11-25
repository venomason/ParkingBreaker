"use client";

import { CERTIFIED_CENTS } from "../lib/prices";
import { NONMAILING_REFUND, PAID_CAPTURE_TERMS } from "../lib/legal";
import { FIRST_CLASS_LIMITS, mailClassName } from "../lib/fulfillment";
import { formatSupportCaseId } from "../lib/support-case-id";
import React, { useEffect } from "react";
import { ConciergeMark } from "./ConciergeMark";
import { SupportCaseIdCard } from "./SupportCaseIdCard";
import { Icon } from "./Icons";

interface Step5WindowProps {
  live?: boolean;
  beta?: boolean;
  supportCaseId?: string | null;
  amountCents?: number | null;
  tier: "certified" | "standard" | null;
  timerEnd: number | null;
  now: number;
  lapsed: boolean;
  onComplete: () => void;
  setActionBar: (sub: React.ReactNode, buttons: React.ReactNode) => void;
  onDemoWarn: () => void;
  onDemoLapse: () => void;
}

export function Step5Window({
  live = false,
  beta = false,
  supportCaseId = null,
  amountCents,
  tier,
  timerEnd,
  now,
  lapsed,
  onComplete,
  setActionBar,
  onDemoWarn,
  onDemoLapse,
}: Step5WindowProps) {
  const displayCaseId = formatSupportCaseId(supportCaseId) ?? (!live ? "PB-48213" : null);
  const price = live ? (amountCents == null ? null : amountCents / 100) : CERTIFIED_CENTS / 100;
  const free = beta || (live && amountCents === 0);
  const tierName = tier ? mailClassName(tier === "standard" ? "STANDARD" : "CERTIFIED") : "Mailing";

  const remaining = timerEnd ? Math.max(0, timerEnd - now) : 0;
  const isWarn = !lapsed && remaining <= 5 * 60 * 1000;
  const s = Math.ceil(remaining / 1000);
  const ringText = lapsed
    ? "0:00"
    : `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;

  useEffect(() => {
    setActionBar(
      live && (!timerEnd || lapsed) ? "Checking the saved review-window status" : "Saved · Your review window is running",
      <button
        className="btn btn-primary"
        type="button"
        data-go={6}
        disabled={live && (!timerEnd || lapsed)}
        onClick={onComplete}
      >
        Open the letter studio →
      </button>
    );
  }, [onComplete, setActionBar, live, timerEnd, lapsed]);

  return (
    <section className="screen on" id="s5" aria-labelledby="h-s5">
      <div className="voice">
        <ConciergeMark size={44} state="done" />
        <div>
          <p className="eyebrow">Step 3 · Review window</p>
          <h2 id="h-s5" tabIndex={-1}>
            {free ? "Free beta accepted. Take your time with the letter." : "Card authorized. Take your time with the letter."}
          </h2>
          <p className="lead" id="s5lead">
            {free ? (
              <>{tierName}, $0. No card hold and no payment.{tier === "standard" && ` ${FIRST_CLASS_LIMITS}`} Beta participation does not extend any ticket deadline.{live ? " The countdown uses your saved review deadline." : " You have 2 hours to review and approve."}</>
            ) : (
              <>{tierName}{price == null ? "" : `, $${price}`}, authorized. {PAID_CAPTURE_TERMS} {NONMAILING_REFUND}{live ? " The countdown uses your saved review deadline." : " You have 2 hours to review and approve."}</>
            )}
          </p>
        </div>
      </div>

      {displayCaseId && <SupportCaseIdCard caseId={displayCaseId} />}

      <div className="card is-next-task">
        <div className="section-head-row" style={{ marginBottom: "8px" }}>
          <span className={`status-pill ${lapsed || isWarn ? "is-attention" : "is-saved"}`}>
            <Icon id="check" size={12} /> {lapsed ? "Window ended" : isWarn ? "5 minutes or less" : "Active review window"}
          </span>
          <span className="muted small mono">{tierName}</span>
        </div>
        <p className={`window-time ${isWarn ? "warn" : ""}`} id="ring">
          <b id="ringT">{ringText}</b>
          <span>left to review and approve</span>
        </p>
        <ul className="wlist">
          <li>
            <span className="n">1</span>
            <span>
              Read your draft, check where each sentence came from, edit anything, then approve
              the exact PDF.{!live && " This San Diego example does not ask for a signature."}
            </span>
          </li>
          <li>
            <span className="n">2</span>
            <span>At five minutes left, we’ll give you a gentle heads-up. No rush before that.</span>
          </li>
          <li>
            <span className="n">3</span>
            <span>
              {free
                ? <>If the window closes, <b>nothing is charged</b>, your work stays saved, and we check the saved status.</>
                : live
                  ? "If the window closes, we check the saved payment and mailing status. Your work stays saved."
                  : <>If the window closes, the hold is released, <b>nothing is charged</b>, and all your work stays saved.</>}
            </span>
          </li>
        </ul>
        {!live && (
          <div className="demo">
            <p className="dl2">Prototype controls</p>
            <div className="row">
              <button className="chip" type="button" id="demoWarn" onClick={onDemoWarn}>
                Jump to 5-minute warning
              </button>
              <button className="chip" type="button" id="demoLapse" onClick={onDemoLapse}>
                Let window lapse
              </button>
            </div>
          </div>
        )}
      </div>
    </section>
  );
}
