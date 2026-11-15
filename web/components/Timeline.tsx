import React from "react";
import { SAMPLE_TIMELINE } from "../lib/api";

export function Timeline() {
  return (
    <div
      className="track"
      role="img"
      aria-label={`Timeline: ${SAMPLE_TIMELINE.map((point) => `${point.title} ${point.caption}`).join(", ")}.`}
    >
      <div className="bar" />
      <div className="barf" />
      {SAMPLE_TIMELINE.map((point) => (
        <div key={point.className} className={`mk ${point.className}`} style={{ left: point.left }}>
          <div className="pin" />
          <div className="ml"><b>{point.title}</b>{point.caption}</div>
        </div>
      ))}
    </div>
  );
}
