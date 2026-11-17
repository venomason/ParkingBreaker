"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { CityLightShell } from "./CityLightShell";
import { HurtleShell } from "./HurtleShell";
import { HurtleExpression, type HurtleMood } from "./HurtleExpression";
import brand from "./brand.module.css";
import styles from "./city-light-lab.module.css";

const moods:{id:HurtleMood;name:string;copy:string}[]=[
 {id:"iconic",name:"Iconic",copy:"One point becomes a connected city. Then the light rests."},
 {id:"attention",name:"Attention",copy:"A small, deliberate nudge toward the next useful action."},
 {id:"speaking",name:"Speaking",copy:"A quiet rhythm while Hurtle guides the conversation. No audio in this example."},
 {id:"setback",name:"A setback",copy:"Calm and steady. A detail needs attention; there is no alarm or flashing red."},
];
type Progress={step:number;citation:string;confirmed:boolean;story:string};
const initial:Progress={step:0,citation:"",confirmed:false,story:""};
const key="pb-city-light-example-v1";

export function CityLightLab() {
 const [original,setOriginal]=useState(false);
 const [replay,setReplay]=useState(0);
 const [mood,setMood]=useState<HurtleMood>("iconic");
 const [expressionReplay,setExpressionReplay]=useState(0);
 const [progress,setProgress]=useState(initial);
 const [ready,setReady]=useState(false);
 const [error,setError]=useState("");
 useEffect(()=>{
   let active=true;
   queueMicrotask(()=>{
     if(!active)return;
     try{const saved=JSON.parse(sessionStorage.getItem(key)??"null");if(saved&&Number.isInteger(saved.step)&&saved.step>=0&&saved.step<=3&&typeof saved.citation==="string"&&saved.citation.length<=40&&typeof saved.confirmed==="boolean"&&typeof saved.story==="string"&&saved.story.length<=500&&(saved.step<2||(saved.citation.trim()&&saved.confirmed))&&(saved.step<3||saved.story.trim()))setProgress(saved);}catch{}
     setReady(true);
   });
   return ()=>{active=false;};
 },[]);
 useEffect(()=>{if(ready)try{sessionStorage.setItem(key,JSON.stringify(progress));}catch{}},[ready,progress]);
 const advance=()=>{setError("");setProgress(p=>({...p,step:p.step+1}));};
 return <main className={styles.lab} id="brand-main">
   <div className={styles.labTop}><Link className={brand.backLink} href="/design-preview">← Existing homepage</Link><span>Artwork & motion study · version 2</span></div>
   <section className={styles.identity} aria-labelledby="logo-title">
     <div className={styles.switcher} aria-label="Artwork comparison"><button type="button" aria-pressed={!original} onClick={()=>setOriginal(false)}>Refined network</button><button type="button" aria-pressed={original} onClick={()=>setOriginal(true)}>Current preview</button></div>
     <div className={styles.lockup}><div className={styles.heroShell}>{original?<HurtleShell/>:<CityLightShell replay={replay}/>}</div><div className={styles.identityText}><p className={brand.wordmark}>Parking Breaker</p><h1 id="logo-title">A city of light.<br/>One unmistakable shell.</h1><p>Same identity. A more deliberate glow.</p></div></div>
     <div className={styles.heroControls}><button className={styles.outlineButton} type="button" onClick={()=>{setOriginal(false);setReplay(n=>n+1);}}>Replay the light <span aria-hidden="true">↻</span></button><span>One connected sequence. A quiet finish.</span></div>
   </section>
   <section className={styles.studySection} aria-labelledby="expression-title">
     <div className={styles.sectionIntro}><p className={brand.eyebrow}>One identity. A little personality.</p><h2 id="expression-title">Hurtle has a way<br/>of saying it.</h2><p>Motion supports the moment. It never changes what has actually happened.</p></div>
     <div className={styles.expressionPanel}>
       <div className={styles.moods} aria-label="Preview an expression">{moods.map(item=><button type="button" key={item.id} aria-pressed={mood===item.id} onClick={()=>{setMood(item.id);setExpressionReplay(n=>n+1);}}>{item.name}</button>)}</div>
       <HurtleExpression mood={mood} replay={expressionReplay}/>
       <p className={styles.expressionCopy} role="status">{moods.find(item=>item.id===mood)?.copy}</p>
       <p className={styles.demoNote}>Expression examples only. No real appeal outcome is represented.</p>
     </div>
   </section>
   <section className={styles.studySection} aria-labelledby="progress-title">
     <div className={styles.sectionIntro}><p className={brand.eyebrow}>Light earned, not assumed</p><h2 id="progress-title">The shell remembers<br/>what you’ve done.</h2><p>Complete these fictional tasks to light the progress mark. Future work stays quiet. Editing a confirmed detail removes its completion.</p></div>
     <div className={styles.progressPanel}>
       <div className={styles.sizeSamples}>{[32,48,96].map(size=><div key={size}><CityLightShell size={size} compact={size<60} mode="progress" completed={progress.step}/><span>{size}px</span></div>)}</div>
       <p className={styles.progressStatus} role="status">{progress.step} of 3 example tasks complete</p>
       <div className={styles.progressTask}>
         {progress.step===0&&<><h3>Choose the sample.</h3><p>No upload or camera is used in this example.</p><button className={styles.outlineButton} disabled={!ready} type="button" onClick={advance}>Select fictional ticket →</button></>}
         {progress.step===1&&<form onSubmit={e=>{e.preventDefault();if(!progress.citation.trim()||!progress.confirmed){setError("Enter the citation and check it before continuing.");return;}advance();}}><h3>Check the citation.</h3><p>Fictional print: <strong>TEST-8O4271</strong> — the character after 8 is the letter O.</p><label htmlFor="city-citation">Citation · supplied by you</label><input id="city-citation" value={progress.citation} maxLength={40} spellCheck={false} autoComplete="off" onChange={e=>setProgress(p=>({...p,citation:e.target.value,confirmed:false}))}/><label className={styles.check}><input type="checkbox" checked={progress.confirmed} onChange={e=>setProgress(p=>({...p,confirmed:e.target.checked}))}/>I checked the exact characters.</label><button className={styles.outlineButton} type="submit">Confirm citation →</button></form>}
         {progress.step===2&&<form onSubmit={e=>{e.preventDefault();if(!progress.story.trim()){setError("Add a fictional example before continuing.");return;}advance();}}><h3>Add your own words.</h3><label htmlFor="city-story">Fictional example account</label><textarea id="city-story" rows={3} maxLength={500} value={progress.story} onChange={e=>setProgress(p=>({...p,story:e.target.value}))}/><button className={styles.outlineButton} type="submit">Save example →</button></form>}
         {progress.step===3&&<><h3>Three small tasks. Done.</h3><p>Example citation: <strong>{progress.citation}</strong></p><p>No appeal was created. Nothing was charged or sent.</p></>}
         {error&&<p className={styles.error} role="alert">{error}</p>}
         <div className={styles.editControls}>{progress.step>=2&&<button type="button" onClick={()=>{setError("");setProgress(p=>({...p,step:1,confirmed:false}));}}>Edit the citation</button>}{progress.step>0&&<button type="button" onClick={()=>{setError("");setProgress(initial);}}>Reset example</button>}</div>
       </div>
     </div>
   </section>
   <section className={styles.notes} aria-labelledby="choice-title"><h2 id="choice-title">What changed—and why.</h2><div><p><strong>Connected, not random.</strong> Each light follows the geometry from the crown. A node wakes when a connection reaches it.</p><p><strong>Depth without haze.</strong> Emerald plates keep their outline. Fine gold strokes, a dark underlay and small radial halos replace the broad blur.</p><p><strong>Calm after the arrival.</strong> The light sequence runs once and settles. Pause and reduced motion show a clear static logo; progress retains only completed work.</p><p><strong>A meaningful celebration.</strong> A real dismissal animation must be driven by a confirmed dismissal record. Mailing, delivery and a successful payment do not qualify.</p></div></section>
   <div className={styles.downloads}><a href="/images/brand/hurtle-city-light-v2.svg" download>Animated shell SVG ↓</a><a href="/images/brand/hurtle-city-light-v2-static.svg" download>Static shell SVG ↓</a><span>Current artwork remains in place.</span></div>
 </main>;
}
