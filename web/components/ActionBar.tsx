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
    if (!element) {
      document.documentElement.style.setProperty("--action-h", "0px");
      return;
    }
    const sync = () => {
      document.documentElement.style.setProperty("--action-h", `${Math.ceil(element.getBoundingClientRect().height + 16)}px`);
    };
    sync();
    const observer = new ResizeObserver(sync);
    observer.observe(element);
    return () => {
      observer.disconnect();
      document.documentElement.style.setProperty("--action-h", "0px");
    };
  }, [children, subText]);

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
