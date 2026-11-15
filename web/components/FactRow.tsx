import React from "react";
import { Icon } from "./Icons";

export interface FactItem {
  id: string;
  fact: string;
  tier: "must" | "imp" | "nice";
  st: "ok" | "unk" | "con" | "na";
  quote?: string | null;
  src: string;
  url: string;
  means: string;
  note?: string;
}

const TIER_LABEL = {
  must: "Must-have",
  imp: "Important",
  nice: "Nice-to-have",
};

const STAT_CONFIG = {
  ok: { cls: "s-ok", label: "Confirmed" },
  unk: { cls: "s-unk", label: "Unknown" },
  con: { cls: "s-con", label: "Conflicted" },
  na: { cls: "s-na", label: "Not applicable" },
};

interface FactRowProps {
  fact: FactItem;
  onClick: () => void;
}

export function FactRow({ fact, onClick }: FactRowProps) {
  const stat = STAT_CONFIG[fact.st];
  return (
    <button
      type="button"
      className="fact-row"
      data-fact={fact.id}
      onClick={onClick}
    >
      <span className={`tier-dot ${fact.tier}`} aria-hidden="true" />
      <span className="fact-main">
        <span className="fact-title">{fact.fact}</span>
        <span className="fact-sub">
          {TIER_LABEL[fact.tier]} · Tap for verbatim quote &amp; source
        </span>
      </span>
      <span className={`bdg ${stat.cls}`}>
        {fact.st === "ok" && <Icon id="check" size={11} />}
        {stat.label}
      </span>
    </button>
  );
}
