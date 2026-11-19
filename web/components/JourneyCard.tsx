"use client";

import { useLayoutEffect, useRef, type ReactNode } from "react";

// Navigation follows the customer journey. The internal step numbers predate that order.
const journeyOrder = [1, 2, 4, 5, 3, 9, 6, 7, 8];

export function JourneyCard({ step, children }: { step: number; children: ReactNode }) {
  const panel = useRef<HTMLDivElement>(null);
  const previous = useRef(step);

  useLayoutEffect(() => {
    const from = previous.current;
    previous.current = step;
    const element = panel.current;
    const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
    if (!element || from === step || document.hidden || motion.matches) return;
    const direction = journeyOrder.indexOf(step) < journeyOrder.indexOf(from) ? -1 : 1;
    const travel = Math.min(84, element.clientWidth * 0.15) * direction;
    const animation = element.animate([
      { transform: `perspective(1200px) translate3d(${travel}px, 6px, -28px) rotateY(${direction * 4}deg)`, opacity: 0.64 },
      { transform: `perspective(1200px) translate3d(${direction * 3}px, 0, -2px) rotateY(${direction * 0.35}deg)`, opacity: 1, offset: 0.76 },
      { transform: "none", opacity: 1 },
    ], { duration: 360, easing: "cubic-bezier(.22,.76,.25,1)" });
    const stop = () => animation.cancel();
    motion.addEventListener("change", stop);
    return () => { stop(); motion.removeEventListener("change", stop); };
  }, [step]);

  return <div className="journey-deck"><div ref={panel} data-stage={step} className="journey-card">{children}</div></div>;
}
