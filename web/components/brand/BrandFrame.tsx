"use client";

import Link from "next/link";
import { useEffect, useState, type ReactNode } from "react";
import styles from "./brand.module.css";

export function BrandFrame({ children }: { children: ReactNode }) {
  const [moving,setMoving] = useState(false);
  const [reduced,setReduced] = useState(false);
  const [hidden,setHidden] = useState(false);
  useEffect(()=>{
    const pref=window.matchMedia("(prefers-reduced-motion: reduce)");
    const update=()=>{let paused=false;try{paused=sessionStorage.getItem("pb-brand-motion")==="paused";}catch{}setReduced(pref.matches);setMoving(!pref.matches && !paused);};
    const visibility=()=>setHidden(document.hidden);
    update(); visibility();pref.addEventListener("change",update);document.addEventListener("visibilitychange",visibility);
    return ()=>{pref.removeEventListener("change",update);document.removeEventListener("visibilitychange",visibility);};
  },[]);
  return <div className={styles.frame} data-brand-preview data-motion={moving&&!hidden&&!reduced?"playing":"paused"}>
    <a className={styles.skip} href="#brand-main">Skip to content</a>
    <header className={styles.header}>
      <Link className={styles.smallBrand} href="/design-preview" aria-label="Parking Breaker home"><span className={styles.brandDot} aria-hidden="true"/>Parking Breaker</Link>
      <nav aria-label="Main navigation">
        <button className={styles.motionButton} type="button" aria-label={reduced?"Reduced motion enabled":moving?"Pause animation":"Play animation"} disabled={reduced} onClick={()=>{try{sessionStorage.setItem("pb-brand-motion",moving?"paused":"playing");}catch{}setMoving(!moving);}}>
          <svg viewBox="0 0 20 20" width="16" height="16" fill="none" aria-hidden="true">{moving&&!reduced?<><path d="M7 5v10M13 5v10" stroke="currentColor" strokeWidth="1.5"/></>:<path d="m7 4 9 6-9 6Z" stroke="currentColor" strokeWidth="1.5"/>}</svg>
          <span>{reduced?"Reduced motion":moving?"Motion on":"Motion off"}</span>
        </button>
        <Link className={styles.rulesLink} href="/design-preview/rules">Rules <span aria-hidden="true">↗</span></Link>
      </nav>
    </header>
    {children}
    <footer className={styles.footer}><span>Your words. Your approval.</span><span className={styles.previewLabel}>Design preview · nothing submitted</span></footer>
  </div>;
}
