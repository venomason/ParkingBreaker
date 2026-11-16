"use client";

import Image from "next/image";
import Link from "next/link";
import { createContext, useContext, useEffect, useRef, useState } from "react";
import styles from "./motion-journey.module.css";

const MotionContext = createContext(false);
const STORAGE_KEY = "pb-motion-study-v1";
const SAMPLE = "TEST-O04271";
type Stage = "intro" | "ticket" | "facts" | "story" | "finish";
type Draft = { stage: Stage; selected: boolean; citation: string; confirmed: boolean; story: string; completed: number };
const EMPTY: Draft = { stage: "intro", selected: false, citation: SAMPLE, confirmed: false, story: "", completed: 0 };
const RECEIPTS = ["Ticket added", "Facts confirmed", "Your account saved"];

function restoreDraft(): Draft {
  try {
    const value = JSON.parse(sessionStorage.getItem(STORAGE_KEY) || "null");
    if (!value || !["intro", "ticket", "facts", "story", "finish"].includes(value.stage)
      || typeof value.selected !== "boolean" || typeof value.confirmed !== "boolean"
      || typeof value.citation !== "string" || value.citation.length > 60
      || typeof value.story !== "string" || value.story.length > 600
      || !Number.isInteger(value.completed) || value.completed < 0 || value.completed > 3) return EMPTY;
    const completed = !value.selected ? 0 : !value.citation.trim() || !value.confirmed ? Math.min(value.completed, 1)
      : !value.story.trim() ? Math.min(value.completed, 2) : value.completed;
    const stages: Stage[] = ["ticket", "facts", "story", "finish"];
    const requested = stages.indexOf(value.stage);
    return { ...value, completed, stage: requested > completed ? stages[completed] : value.stage };
  } catch { return EMPTY; }
}

export function MotionFrame({ children }: { children: React.ReactNode }) {
  const [enabled, setEnabled] = useState(false);
  const [reduced, setReduced] = useState(true);
  const [hidden, setHidden] = useState(false);
  useEffect(() => {
    const media = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setReduced(media.matches);
    const visibility = () => setHidden(document.hidden);
    // Hydrate browser preferences after the server-rendered static first frame.
    const initialization = requestAnimationFrame(() => {
      update(); visibility();
      try { setEnabled(sessionStorage.getItem("pb-motion-enabled") !== "off"); } catch { setEnabled(true); }
    });
    media.addEventListener("change", update);
    document.addEventListener("visibilitychange", visibility);
    return () => { cancelAnimationFrame(initialization); media.removeEventListener("change", update); document.removeEventListener("visibilitychange", visibility); };
  }, []);
  const moving = enabled && !reduced && !hidden;
  return <MotionContext.Provider value={moving}>
    <div className={styles.frame} data-motion-study data-motion={moving ? "on" : "off"}>
      <a className={styles.skip} href="#motion-main">Skip to content</a>
      <header className={styles.header}>
        <Link href="/motion-preview" className={styles.wordmark} aria-label="Parking Breaker home">Parking<span>Breaker</span><span className={styles.brandDot} aria-hidden="true" /></Link>
        <nav aria-label="Main navigation">
          <button className={styles.motionToggle} type="button" aria-pressed={enabled && !reduced} disabled={reduced}
            onClick={() => { setEnabled(!enabled); try { sessionStorage.setItem("pb-motion-enabled", enabled ? "off" : "on"); } catch { /* Preference is optional. */ } }}>
            <span className={styles.motionGlyph} aria-hidden="true">{enabled && !reduced ? "Ⅱ" : "▷"}</span>
            {reduced ? "Motion reduced" : enabled ? "Motion on" : "Motion off"}
          </button>
          <Link href="/motion-preview/rules" className={styles.rulesLink}>The rules <span aria-hidden="true">↗</span></Link>
        </nav>
      </header>
      {children}
      <footer className={styles.footer}><span>Clarity. One thing at a time.</span><span>Motion study · Fictional ticket only · Nothing submitted</span></footer>
    </div>
  </MotionContext.Provider>;
}

function Arrow() { return <span className={styles.arrow} aria-hidden="true">↗</span>; }

export function MotionJourney() {
  const moving = useContext(MotionContext);
  const [draft, setDraft] = useState<Draft>(EMPTY);
  const [ready, setReady] = useState(false);
  const [leaving, setLeaving] = useState<"forward" | "back" | null>(null);
  const [error, setError] = useState("");
  const [announcement, setAnnouncement] = useState("");
  const heading = useRef<HTMLHeadingElement>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const shouldFocus = useRef(false);
  useEffect(() => {
    const initialization = requestAnimationFrame(() => {
      const saved = { ...restoreDraft() };
      if (window.location.hash === "#ticket" && saved.stage === "intro") saved.stage = "ticket";
      setDraft(saved); setReady(true);
    });
    return () => { cancelAnimationFrame(initialization); if (timer.current) clearTimeout(timer.current); };
  }, []);
  useEffect(() => {
    if (!ready) return;
    try { sessionStorage.setItem(STORAGE_KEY, JSON.stringify(draft)); } catch { /* The study still works without storage. */ }
  }, [draft, ready]);
  useEffect(() => {
    if (!shouldFocus.current) return;
    shouldFocus.current = false;
    heading.current?.focus({ preventScroll: true });
    window.scrollTo({ top: 0, behavior: "instant" });
  }, [draft.stage]);

  function transition(next: Draft, direction: "forward" | "back" = "forward") {
    if (timer.current) return;
    setError("");
    const apply = () => {
      shouldFocus.current = true;
      setDraft(next); setLeaving(null); timer.current = null;
      setAnnouncement(next.completed > draft.completed ? RECEIPTS[next.completed - 1] : "");
    };
    if (moving) { setLeaving(direction); timer.current = setTimeout(apply, 300); }
    else apply();
  }
  function advance(event: React.FormEvent) {
    event.preventDefault();
    if (draft.stage === "ticket") {
      if (!draft.selected) { setError("Choose the fictional ticket to continue."); return; }
      transition({ ...draft, stage: "facts", completed: Math.max(1, draft.completed) });
    } else if (draft.stage === "facts") {
      if (!draft.citation.trim() || !draft.confirmed) { setError("Enter the citation number and confirm the exact characters."); return; }
      transition({ ...draft, stage: "story", completed: Math.max(2, draft.completed) });
    } else if (draft.stage === "story") {
      if (!draft.story.trim()) { setError("Add a short fictional account before continuing."); return; }
      transition({ ...draft, stage: "finish", completed: 3 });
    }
  }
  const stageNumber = { intro: 0, ticket: 1, facts: 2, story: 3, finish: 4 }[draft.stage];
  const editable: Stage[] = ["ticket", "facts", "story"];
  const isIntro = draft.stage === "intro";

  return <main id="motion-main" className={styles.main} data-ready={ready}>
    <p className={styles.srOnly} aria-live="polite" role="status">{announcement}</p>
    <div className={styles.scene} data-scene={draft.stage} data-leaving={leaving || undefined} aria-busy={!!leaving}>
      {isIntro ? <section className={styles.hero} aria-labelledby="motion-title">
        <div className={styles.heroCopy}>
          <p className={styles.eyebrow}>A little less weight on your shoulders.</p>
          <h1 id="motion-title" ref={heading} tabIndex={-1}>Your side.<br/><span>Worth telling.</span></h1>
          <p className={styles.heroDescription}>Make your parking appeal.<br/>We’ll take it one clear step at a time.</p>
          <button className={styles.primary} disabled={!ready || !!leaving} onClick={() => transition({ ...draft, stage: "ticket" })}>Start my appeal <Arrow /></button>
          <p className={styles.quietNote}>Clerical help. Your facts. Your words.</p>
        </div>
        <div className={styles.heroArt}>
          <div className={styles.artOrbit} aria-hidden="true" />
          <Image src="/images/brand/hurtle-natural-shell-concept.png" alt="Hurtle’s sculpted olive shell, with warm gold at the seams" width={1219} height={1290} priority sizes="(max-width: 700px) 63vw, 46vw" className={styles.shellImage} />
          <span className={styles.artCaption}><i aria-hidden="true" /> A little backbone.</span>
        </div>
        <span className={styles.heroFolio} aria-hidden="true">01 — A fresh start</span>
      </section> : <div className={styles.workspace}>
        <aside className={styles.trail} aria-label="Your completed tasks">
          <div className={styles.trailHeading}><span className={styles.eyebrow}>Your progress</span><span className={styles.count}>{String(draft.completed).padStart(2, "0")}</span></div>
          {draft.completed === 0 ? <p className={styles.emptyTrail}>Small steps.<br/>A clear path forward.</p> : <ol className={styles.receipts}>
            {RECEIPTS.slice(0, draft.completed).map((label, index) => <li key={label} className={styles.receipt}>
              <button type="button" disabled={!!leaving || draft.stage === editable[index]} aria-label={`Review ${label.toLowerCase()}`} onClick={() => transition({ ...draft, stage: editable[index] }, "back")}>
                <span className={styles.receiptCheck} aria-hidden="true">✓</span><span>{label}</span><span className={styles.receiptArrow} aria-hidden="true">↗</span>
              </button>
            </li>)}
          </ol>}
          <p className={styles.trailNote}>{draft.completed ? "Your completed steps stay here." : "Completed steps will collect here."}</p>
        </aside>
        <section key={draft.stage} className={styles.taskCard} aria-labelledby="task-title">
          <div className={styles.cardTop}><span className={styles.eyebrow}>{draft.stage === "finish" ? "A moment of clarity" : "Just this, for now"}</span><span className={styles.cardNumber} aria-hidden="true">{String(stageNumber).padStart(2, "0")}</span></div>
          <h1 id="task-title" tabIndex={-1} ref={heading} className={styles.taskTitle}>{draft.stage === "ticket" ? <>Let’s start<br/>with your ticket.</> : draft.stage === "facts" ? <>The little details<br/>make a difference.</> : draft.stage === "story" ? <>Now, your side<br/>of the story.</> : <>There. A little<br/>more together.</>}</h1>
          <div className={styles.cardContent}>
            {draft.stage === "finish" ? <>
              <p className={styles.description}>Three small steps, saved in one place.</p>
              <dl className={styles.summary}>
                <div><dt>Ticket</dt><dd>Fictional sample</dd></div>
                <div><dt>Your confirmed citation</dt><dd className={styles.exact}>{draft.citation}</dd></div>
                <div><dt>Your account</dt><dd>{draft.story}</dd></div>
              </dl>
              <p className={styles.finishNote}>This is the end of the motion study. No appeal has been submitted.</p>
              <button className={styles.primary} disabled={!!leaving} onClick={() => transition({ ...EMPTY }, "back")}>Try it again <Arrow /></button>
            </> : <form onSubmit={advance} noValidate>
              {draft.stage === "ticket" && <>
                <p className={styles.description}>For this preview, use our fictional ticket.</p>
                <button type="button" className={styles.sampleTicket} aria-pressed={draft.selected} disabled={!!leaving} onClick={() => setDraft({ ...draft, selected: true })}>
                  <span className={styles.ticketStub} aria-hidden="true"><span>PARKING NOTICE</span><b>TEST</b><span>{SAMPLE}</span><i /></span>
                  <span><strong>{draft.selected ? "Fictional ticket selected" : "Use the fictional ticket"}</strong><small>Sample only · No personal information</small></span>
                  <span className={styles.sampleCheck} aria-hidden="true">{draft.selected ? "✓" : "+"}</span>
                </button>
              </>}
              {draft.stage === "facts" && <>
                <p className={styles.description}>Check every character. You’re in control of what gets saved.</p>
                <div className={styles.printed}><span>Printed on the fictional sample</span><strong>{SAMPLE}</strong></div>
                <label className={styles.fieldLabel} htmlFor="motion-citation">Citation number</label>
                <input id="motion-citation" className={styles.input} value={draft.citation} maxLength={60} autoComplete="off" spellCheck={false} aria-describedby="motion-fact-help" aria-invalid={!!error && !draft.citation.trim()} disabled={!!leaving} onChange={event => { setDraft({ ...draft, citation: event.target.value, confirmed: false, completed: Math.min(1, draft.completed) }); setError(""); }} />
                <p id="motion-fact-help" className={styles.fieldHelp}>Your entry is saved exactly as typed. The letter O and the digit 0 stay different.</p>
                <label className={styles.checkLabel}><input type="checkbox" checked={draft.confirmed} disabled={!!leaving} onChange={event => setDraft({ ...draft, confirmed: event.target.checked, completed: event.target.checked ? draft.completed : Math.min(1, draft.completed) })} /><span>I checked these characters and confirm this value.</span></label>
              </>}
              {draft.stage === "story" && <>
                <p className={styles.description}>What happened? A few plain sentences are enough.</p>
                <label className={styles.fieldLabel} htmlFor="motion-story">Your fictional account</label>
                <textarea id="motion-story" className={styles.textarea} value={draft.story} maxLength={600} rows={4} placeholder="I parked beside…" aria-describedby="motion-story-help" aria-invalid={!!error && !draft.story.trim()} disabled={!!leaving} onChange={event => { setDraft({ ...draft, story: event.target.value, completed: Math.min(2, draft.completed) }); setError(""); }} />
                <p id="motion-story-help" className={styles.fieldHelp}>Use made-up details for this preview. Nothing is sent.</p>
              </>}
              {error && <p className={styles.error} role="alert">{error}</p>}
              <div className={styles.actions}>
                <button className={styles.primary} disabled={!!leaving} type="submit">{draft.stage === "ticket" ? "Ticket ready" : draft.stage === "facts" ? "Facts confirmed" : "Save my account"}<Arrow /></button>
                <button type="button" className={styles.back} disabled={!!leaving} onClick={() => transition({ ...draft, stage: draft.stage === "ticket" ? "intro" : draft.stage === "facts" ? "ticket" : "facts" }, "back")}>← Back</button>
              </div>
            </form>}
          </div>
        </section>
      </div>}
    </div>
  </main>;
}
