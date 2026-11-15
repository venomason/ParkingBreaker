"use client";

import React, { useEffect, useRef } from "react";

interface ActionBarProps {
  subText?: React.ReactNode;
  children?: React.ReactNode;
}

export function ActionBar({ subText, children }: ActionBarProps) {
  const bar = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const element = bar.current;
    if (!element) { document.documentElement.style.setProperty("--action-h", "0px"); return; }
    const observer = new ResizeObserver(() => document.documentElement.style.setProperty("--action-h", `${element.getBoundingClientRect().height}px`));
    observer.observe(element);
    return () => observer.disconnect();
  }, [children]);
  if (!children) return null;

  return (
    <div className="action-bar" id="actionBar" ref={bar}>
      {subText && (
        <p className="action-sub" id="actionSub">
          {subText}
        </p>
      )}
      <div className="action-in" id="actionIn">
        {children}
      </div>
    </div>
  );
}
