import React from "react";

export function SvgDefs() {
  return (
    <svg width="0" height="0" style={{ position: "absolute" }} aria-hidden="true">
      <defs>
        <radialGradient id="gHub" cx=".35" cy=".3" r=".8">
          <stop offset="0" stopColor="#3b5b9a" />
          <stop offset=".6" stopColor="#1f3768" />
          <stop offset="1" stopColor="#14213d" />
        </radialGradient>
        <symbol id="i-check" viewBox="0 0 24 24">
          <path
            d="M5 12.5l4.2 4.2L19 7"
            fill="none"
            stroke="currentColor"
            strokeWidth="3"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </symbol>
        <symbol id="i-shield" viewBox="0 0 24 24">
          <path
            d="M12 3l7.5 3v5.5c0 4.6-3.2 8.3-7.5 9.5-4.3-1.2-7.5-4.9-7.5-9.5V6L12 3z"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinejoin="round"
          />
          <path
            d="M8.8 12.2l2.2 2.2 4.3-4.6"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </symbol>
        <symbol id="i-x" viewBox="0 0 24 24">
          <path
            d="M6 6l12 12M18 6L6 18"
            fill="none"
            stroke="currentColor"
            strokeWidth="2.4"
            strokeLinecap="round"
          />
        </symbol>
        <symbol id="i-chev" viewBox="0 0 24 24">
          <path
            d="M6 9l6 6 6-6"
            fill="none"
            stroke="currentColor"
            strokeWidth="2.2"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </symbol>
        <symbol id="i-dot" viewBox="0 0 24 24">
          <circle cx="12" cy="12" r="4" fill="currentColor" />
        </symbol>
        <symbol id="i-lock" viewBox="0 0 24 24">
          <rect x="4.5" y="10.5" width="15" height="10" rx="2.5" fill="none" stroke="currentColor" strokeWidth="2" />
          <path d="M8 10.5V8a4 4 0 018 0v2.5" fill="none" stroke="currentColor" strokeWidth="2" />
        </symbol>
        <symbol id="i-doc" viewBox="0 0 24 24">
          <path d="M7 3h7l5 5v13H7z" fill="none" stroke="currentColor" strokeWidth="2" strokeLinejoin="round" />
          <path d="M14 3v5h5" fill="none" stroke="currentColor" strokeWidth="2" strokeLinejoin="round" />
        </symbol>
        <symbol id="i-globe" viewBox="0 0 24 24">
          <circle cx="12" cy="12" r="8.5" fill="none" stroke="currentColor" strokeWidth="1.8" />
          <path d="M3.5 12h17M12 3.5c2.6 2.6 2.6 14.4 0 17M12 3.5c-2.6 2.6-2.6 14.4 0 17" fill="none" stroke="currentColor" strokeWidth="1.8" />
        </symbol>
      </defs>
    </svg>
  );
}

export function Icon({ id, size = 16, style = {} }: { id: string; size?: number; style?: React.CSSProperties }) {
  return (
    <svg width={size} height={size} aria-hidden="true" style={style}>
      <use href={`#i-${id}`} />
    </svg>
  );
}
