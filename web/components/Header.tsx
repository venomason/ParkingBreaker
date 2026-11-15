"use client";

import React, { useEffect, useRef } from "react";
import { Icon } from "./Icons";
import { PBShieldIcon } from "./HurtleMascot";

interface HeaderProps {
  step: number;
  fulfillmentLabel?: string;
  deliveryComplete?: boolean;
  timerEnd: number | null;
  now: number;
  lapsed: boolean;
  evidenceCount?: string;
  onBrandClick: () => void;
  onEvidenceClick: () => void;
  onRulesClick: () => void;
}

const MILESTONES = [
  "Your ticket",
  "Your side",
  "Mailing choice",
  "Research",
  "Your letter",
  "Tracking",
];

export function Header({
  step,
  fulfillmentLabel,
  deliveryComplete,
  timerEnd,
  now,
  lapsed,
  evidenceCount = "8/9",
  onBrandClick,
  onEvidenceClick,
  onRulesClick,
}: HeaderProps) {
  const header = useRef<HTMLElement>(null);
  useEffect(() => {
    const element = header.current;
    if (!element) return;
    const observer = new ResizeObserver(() => document.documentElement.style.setProperty("--header-h", `${element.getBoundingClientRect().height + 12}px`));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  // Compute progress milestone (0-5)
  const milestoneMap: Record<number, number> = {
    1: 0,
    2: 1,
    4: 2,
    5: 2,
    3: 3,
    6: 4,
    7: 5,
    8: -1,
    9: 3,
  };
  const m = milestoneMap[step] ?? 0;

  const showPill = !!timerEnd && (step === 5 || step === 6);
  const remaining = timerEnd ? Math.max(0, timerEnd - now) : 0;
  const isWarn = !lapsed && remaining <= 5 * 60 * 1000;
  const s = Math.ceil(remaining / 1000);
  const pillText = lapsed
    ? "Closed"
    : `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;

  return (
    <header className="top" ref={header}>
      <div className="top-in">
        <button
          className="brand"
          type="button"
          onClick={onBrandClick}
          aria-label="ParkingBreaker home"
        >
          <PBShieldIcon size={34} className="brand-mark" />
          <span className="brand-word">ParkingBreaker</span>
        </button>

        <div className="top-r">
          {showPill && (
            <span
              className={`pill ${isWarn ? "warn" : ""} ${lapsed ? "off" : ""}`}
              id="pill"
            >
              <span className="pd" aria-hidden="true" />
              <span id="pillT">{pillText}</span>
            </span>
          )}
          <button
            className="iconbtn"
            type="button"
            onClick={onEvidenceClick}
            aria-label={`Evidence ${evidenceCount}: official evidence checklist`}
          >
            <Icon id="doc" size={15} />
            <span className="iconbtn-text">Evidence</span>{" "}
            <span className="badge-ct" id="hdrEvCount">
              {evidenceCount}
            </span>
          </button>
          <button
            className="iconbtn"
            type="button"
            onClick={onRulesClick}
            aria-label="Rules and what we never do"
          >
            <Icon id="shield" size={15} />
            <span className="iconbtn-text">Rules</span>
          </button>
        </div>
      </div>

      <div className="pbar-wrap" aria-label="Your progress">
        <p className="path-now" id="pathNow">
          {step === 8
            ? "Stopped safely · nothing charged"
            : step === 9
            ? "Example · conflicting instructions"
            : step === 7
            ? fulfillmentLabel ?? "Tracking · step 6 of 6"
            : `Step ${m + 1} of 6 · ${MILESTONES[m]}`}
        </p>
        <div className="pbar-steps" id="pathSegs" aria-hidden="true">
          {[0, 1, 2, 3, 4, 5].map((i) => {
            const done =
              step === 7
                ? fulfillmentLabel ? i < 5 || Boolean(deliveryComplete) : true
                : step === 8
                ? i < 2
                : step === 9
                ? i < 3
                : i < m;
            const cur = (step === 7 && fulfillmentLabel ? !deliveryComplete && i === 5 : false) || step !== 7 && step !== 8 && step !== 9 && i === m;
            const paused = (step === 8 && i === 2) || (step === 9 && i === 3);
            const cls = done ? "done" : cur ? "cur" : paused ? "paused" : "";
            return <i key={i} className={cls}><span className="milestone-number">{done ? "✓" : i + 1}</span><span className="milestone-label">{MILESTONES[i]}</span></i>;
          })}
        </div>
      </div>
    </header>
  );
}
