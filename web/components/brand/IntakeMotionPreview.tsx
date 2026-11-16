"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { HurtleShell, ProgressLine } from "./HurtleShell";
import styles from "./brand.module.css";

type PreviewState = { step: 0|1|2|3; citation: string; checked: boolean; story: string };
const fresh:PreviewState={step:0,citation:"",checked:false,story:""};
const storageKey="pb-brand-fictional-preview-v1";

export function IntakeMotionPreview() {
 const [state,setState]=useState<PreviewState>(fresh);
 const [ready,setReady]=useState(false);
 const [error,setError]=useState("");
 const heading=useRef<HTMLHeadingElement>(null);
 const previousStep=useRef<number|null>(null);
 useEffect(()=>{
   try {
     const saved=JSON.parse(sessionStorage.getItem(storageKey)??"null");
     if(saved && [0,1,2,3].includes(saved.step) && typeof saved.citation==="string" && saved.citation.length<=40 && typeof saved.checked==="boolean" && typeof saved.story==="string" && saved.story.length<=2000) {
       const valid = saved.step<2 || (saved.citation.trim().length>0 && saved.checked && (saved.step<3 || saved.story.trim().length>0));
       if(valid)setState(saved);
     }
   } catch { /* Storage is optional for this local design preview. */ }
   setReady(true);
 },[]);
 useEffect(()=>{
   if(!ready)return;
   try{sessionStorage.setItem(storageKey,JSON.stringify(state));}catch{ /* Keep the preview usable without storage. */ }
   if(previousStep.current!==null && previousStep.current!==state.step)heading.current?.focus();
   previousStep.current=state.step;
 },[state,ready]);
 const go=(step:PreviewState["step"])=>{setError("");setState(s=>({...s,step}));};
 return <main className={styles.intakePage} id="brand-main">
   <div className={styles.intakeTop}><Link className={styles.backLink} href="/design-preview">← Home</Link><span>Fictional walkthrough</span><ProgressLine completed={state.step}/></div>
   {state.step>0&&<div className={styles.receiptTrail} aria-label="Completed preview tasks"><span><i aria-hidden="true">✓</i> Sample selected</span>{state.step>=2&&<span><i aria-hidden="true">✓</i> Citation checked</span>}{state.step>=3&&<span><i aria-hidden="true">✓</i> Your words saved</span>}</div>}
   <section className={styles.task} key={state.step}>
     <div className={styles.taskMark}><HurtleShell compact/></div>
     {state.step===0&&<>
       <p className={styles.eyebrow}>One thing at a time</p><h1 ref={heading} tabIndex={-1}>Start with your ticket.</h1><p className={styles.taskLead}>A clear view of the details. That’s all we need to begin.</p>
       <button className={styles.sampleButton} type="button" onClick={()=>go(1)} disabled={!ready}>
         <svg width="48" height="48" viewBox="0 0 48 48" fill="none" aria-hidden="true"><rect x="11" y="5" width="26" height="38" rx="3" stroke="currentColor" strokeWidth="1.5"/><path d="M17 15h14M17 21h14M17 27h9" stroke="currentColor" strokeWidth="1.5"/><path d="M30 34h11m-5.5-5.5v11" stroke="#a47b23" strokeWidth="1.5"/></svg>
         <strong>Try a fictional ticket</strong><span>Explore the flow without uploading anything.</span><span className={styles.sampleAction}>Select sample <b aria-hidden="true">↗</b></span>
       </button>
       <p className={styles.taskNote}>This walkthrough demonstrates the design. No reading service, payment or mail is connected.</p>
     </>}
     {state.step===1&&<>
       <p className={styles.eyebrow}>Check one detail</p><h1 ref={heading} tabIndex={-1}>Make it letter-perfect.</h1><p className={styles.taskLead}>O and zero are different. Enter exactly what you see.</p>
       <div className={styles.sampleTicket}><span>FICTIONAL TICKET · NO PAYMENT DUE</span><p>Citation number</p><strong>TEST-8O4271</strong><small>The character after 8 is the letter O.</small></div>
       <form className={styles.taskForm} onSubmit={e=>{e.preventDefault();if(!state.citation.trim()||!state.checked){setError("Enter the citation and confirm that you checked it.");return;}go(2);}}>
         <label htmlFor="preview-citation">Citation number <span>You enter this</span></label><input id="preview-citation" value={state.citation} maxLength={40} autoComplete="off" spellCheck={false} aria-describedby="character-note" onChange={e=>setState(s=>({...s,citation:e.target.value,checked:false}))}/>
         <p className={styles.fieldNote} id="character-note">Your characters stay exactly as typed. Nothing is silently corrected.</p>
         <label className={styles.checkLabel}><input type="checkbox" checked={state.checked} onChange={e=>setState(s=>({...s,checked:e.target.checked}))}/><span>I checked this against the fictional ticket.</span></label>
         {error&&<p className={styles.error} role="alert">{error}</p>}
         <button className={styles.primary} type="submit">Confirm this detail <span className={styles.arrowCircle} aria-hidden="true">→</span></button>
       </form>
     </>}
     {state.step===2&&<>
       <p className={styles.eyebrow}>In your own words</p><h1 ref={heading} tabIndex={-1}>What happened?</h1><p className={styles.taskLead}>Take your time. A simple account is enough.</p>
       <form className={styles.taskForm} onSubmit={e=>{e.preventDefault();if(!state.story.trim()){setError("Add a few words to try this step.");return;}go(3);}}>
         <label htmlFor="preview-story">Your example account</label><textarea id="preview-story" value={state.story} maxLength={2000} rows={6} placeholder="This is a fictional example…" onChange={e=>setState(s=>({...s,story:e.target.value}))}/><p className={styles.fieldNote}>Use fictional details. This example stays in this browser tab’s session.</p>
         {error&&<p className={styles.error} role="alert">{error}</p>}
         <button className={styles.primary} type="submit">Save my words <span className={styles.arrowCircle} aria-hidden="true">→</span></button>
         <button className={styles.textButton} type="button" onClick={()=>setState(s=>({...s,step:1,checked:false}))}>Edit the citation</button>
       </form>
     </>}
     {state.step===3&&<>
       <p className={styles.eyebrow}>A small task. Done.</p><h1 ref={heading} tabIndex={-1}>Room to breathe.</h1><p className={styles.taskLead}>You’ve reached the end of the design preview.</p>
       <div className={styles.previewReceipt}><span className={styles.eyebrow}>Your example, kept together</span><dl><dt>Citation · supplied by you</dt><dd>{state.citation}</dd><dt>Your own words</dt><dd>{state.story}</dd></dl></div>
       <p className={styles.taskNote}>No appeal was created, and nothing was charged or sent. The live intake and its confirmation checks remain separate.</p>
       <Link className={styles.primary} href="/design-preview">Back to the beginning <span className={styles.arrowCircle} aria-hidden="true">↗</span></Link><button className={styles.textButton} type="button" onClick={()=>{setState(fresh);setError("");}}>Restart the walkthrough</button>
     </>}
   </section>
 </main>;
}
