import React from "react";
import { HurtleAvatarIcon } from "./HurtleMascot";

export type MarkState = "idle" | "thinking" | "done" | "calm";

interface ConciergeMarkProps {
  size?: number;
  state?: MarkState;
}

export function ConciergeMark({ size = 44, state = "idle" }: ConciergeMarkProps) {
  return (
    <span
      className="mark"
      data-state={state}
      style={{ "--s": `${size}px` } as React.CSSProperties}
      aria-hidden="true"
    >
      <HurtleAvatarIcon size={size} />
    </span>
  );
}
