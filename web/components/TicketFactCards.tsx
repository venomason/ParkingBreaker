"use client";

import { Children, useLayoutEffect, useRef, useState, type ReactNode } from "react";

const topics = ["Ticket & plate", "Issuer", "Dates & stage"];

export function TicketFactCards({ children }: { children: ReactNode }) {
  const track = useRef<HTMLDivElement>(null);
  const [current, setCurrent] = useState(0);

  // Fit the visible card even when a validation message appears or text wraps at zoom.
  useLayoutEffect(() => {
    const row = track.current;
    const pane = row?.children[current] as HTMLElement | undefined;
    if (!row || !pane) return;
    const fit = () => { row.style.height = `${pane.offsetHeight}px`; };
    fit();
    const observer = new ResizeObserver(fit);
    observer.observe(pane);
    return () => observer.disconnect();
  }, [current]);

  function move(index: number) {
    const row = track.current;
    if (!row) return;
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    (row.children[index] as HTMLElement).focus({ preventScroll: true });
    row.scrollTo({ left: index * row.clientWidth, behavior: reduce ? "instant" : "smooth" });
  }

  return <div className="fact-deck">
    <div className="fact-tabs" role="group" aria-label="Ticket fact groups">
      {topics.map((topic, index) => <button key={topic} type="button"
        aria-current={current === index ? "step" : undefined} aria-controls={`fact-card-${index}`}
        onClick={() => move(index)}>{topic}</button>)}
    </div>
    <div ref={track} className="fact-track" onScroll={event => {
      const row = event.currentTarget;
      if (row.clientWidth) setCurrent(Math.max(0, Math.min(topics.length - 1, Math.round(row.scrollLeft / row.clientWidth))));
    }}>
      {Children.map(children, (child, index) => <section id={`fact-card-${index}`} aria-label={topics[index]} tabIndex={-1} className="fact-pane">{child}</section>)}
    </div>
  </div>;
}
