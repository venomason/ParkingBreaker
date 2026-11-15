"use client";

import React, { useState } from "react";

interface StepJumperProps {
  currentStep: number;
  onGoTo: (step: number) => void;
}

export function StepJumper({ currentStep, onGoTo }: StepJumperProps) {
  const [open, setOpen] = useState(false);

  const steps = [
    { num: 1, label: "Your ticket" },
    { num: 2, label: "Your side" },
    { num: 3, label: "Check the requirements" },
    { num: 4, label: "Mailing choice" },
    { num: 5, label: "Your review window" },
    { num: 6, label: "Approve your packet" },
    { num: 7, label: "Track your mailing" },
    { num: 8, label: "Plain stop" },
    { num: 9, label: "Conflicting instructions", text: "↔" },
  ];

  return (
    <div className="jumper-wrap">
      {open && (
        <div className="jumper" id="jumper" aria-label="Prototype step jumper">
          {steps.map((s) => (
            <button
              key={s.num}
              type="button"
              data-go={s.num}
              onClick={() => {
                setOpen(false);
                onGoTo(s.num);
              }}
              aria-label={s.label}
              aria-current={currentStep === s.num ? "step" : undefined}
            >
              {s.text || s.num}
            </button>
          ))}
        </div>
      )}
      <button
        type="button"
        className="jumper-toggle"
        id="jumperBtn"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        Steps ▾
      </button>
    </div>
  );
}
