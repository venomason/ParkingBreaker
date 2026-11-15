import React from "react";
import { Icon } from "./Icons";

export interface Receipt {
  stepNum: number;
  stepName: string;
  summary: string;
  reopenable?: boolean;
}

interface ReceiptsProps {
  receipts: Receipt[];
  onReopen?: (stepNum: number) => void;
}

// The customer-facing six milestones, mapped from the existing internal step ids.
// stepNum stays the navigation identity; only this displayed number is remapped.
const MILESTONE_BY_STEP: Record<number, number> = {
  1: 1,
  2: 2,
  3: 4,
  4: 3,
  5: 3,
  6: 5,
  7: 6,
  9: 4,
};

export function Receipts({ receipts, onReopen }: ReceiptsProps) {
  if (receipts.length === 0) return null;

  return (
    <ol className="receipts" id="receipts" aria-label="Completed steps">
      {receipts.map((r) => (
        <li key={r.stepNum} className="receipt-item">
          <div className="receipt-btn">
            {MILESTONE_BY_STEP[r.stepNum] !== undefined && <span className="receipt-num" aria-hidden="true">{String(MILESTONE_BY_STEP[r.stepNum]).padStart(2, "0")}</span>}
            <span className="receipt-ck" aria-hidden="true">
              <Icon id="check" size={12} />
            </span>
            <span className="receipt-body">
              <span className="receipt-step">{r.stepName}</span>
              <span className="receipt-txt">{r.summary}</span>
            </span>
            {onReopen && r.reopenable !== false && <button className="chip receipt-edit" type="button" onClick={() => onReopen(r.stepNum)} aria-label={`Reopen ${r.stepName}: ${r.summary}`}>Reopen</button>}
          </div>
        </li>
      ))}
    </ol>
  );
}
