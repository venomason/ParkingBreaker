"use client";

import { useId, useRef, type PointerEvent } from "react";
import styles from "./brand.module.css";

// Vector interpretation of Amir's approved green shell / gold network reference.
// Each plate and connection is independent, so motion does not require a video.
const plates = [
  "M151 20 Q190 17 220 43 L206 66 158 58 135 38Z",
  "M130 28 153 62 119 91 79 76 Q96 45 130 28Z",
  "M226 48 Q257 72 270 108 L238 121 214 75Z",
  "M73 83 111 98 93 143 47 140 Q54 109 73 83Z",
  "M160 67 201 75 228 123 202 157 142 151 120 104Z",
  "M238 134 276 118 Q288 154 286 188 L239 199 214 166Z",
  "M43 152 89 155 119 198 103 234 37 235 Q31 192 43 152Z",
  "M111 160 140 163 199 169 225 204 204 251 135 251 111 211Z",
  "M239 211 284 201 Q281 249 262 280 L220 265Z",
  "M39 248 103 246 124 277 105 321 65 307 Q47 281 39 248Z",
  "M139 264 203 264 220 291 197 338 138 337 116 310Z",
  "M226 279 257 292 Q242 325 213 346 L206 336Z",
  "M72 318 106 333 134 347 125 369 Q94 354 72 318Z",
  "M140 349 189 350 203 361 Q178 384 139 375Z",
];
const nodes = [[152,51],[78,87],[118,109],[224,92],[180,132],[41,168],[111,192],[268,171],[196,209],[219,233],[263,252],[179,267],[48,253],[91,281],[89,326],[158,347],[221,330]];
const links = [[0,1],[0,2],[0,3],[0,4],[1,2],[1,5],[2,4],[3,4],[3,7],[4,6],[4,7],[4,8],[5,6],[5,12],[6,7],[6,8],[6,11],[6,12],[6,13],[7,9],[7,10],[8,9],[8,11],[9,10],[10,11],[10,16],[11,13],[11,15],[11,16],[12,13],[13,14],[14,15],[15,16]];

export function HurtleShell({ compact = false }: { compact?: boolean }) {
  const id = useId().replace(/:/g, "");
  const body = useRef<HTMLDivElement>(null);
  const frame = useRef<number | null>(null);
  function move(event: PointerEvent<HTMLDivElement>) {
    if (compact || window.matchMedia("(prefers-reduced-motion: reduce)").matches || event.currentTarget.closest("[data-motion='paused']")) return;
    const bounds = event.currentTarget.getBoundingClientRect();
    const x = (event.clientX - bounds.left) / bounds.width - .5;
    const y = (event.clientY - bounds.top) / bounds.height - .5;
    if (frame.current !== null) cancelAnimationFrame(frame.current);
    frame.current = requestAnimationFrame(() => {
      body.current?.style.setProperty("--shell-y", `${x * 13}deg`);
      body.current?.style.setProperty("--shell-x", `${-y * 10}deg`);
      frame.current = null;
    });
  }
  function reset() {
    if (frame.current !== null) cancelAnimationFrame(frame.current);
    body.current?.style.setProperty("--shell-y", "0deg");
    body.current?.style.setProperty("--shell-x", "0deg");
  }
  return <div className={compact ? styles.shellCompact : styles.shellStage} onPointerMove={move} onPointerLeave={reset} onPointerUp={reset}>
    <div className={styles.shellTilt} ref={body}>
      <svg className={styles.shell} viewBox="0 0 320 350" fill="none" aria-hidden="true">
        <defs>
          <linearGradient id={`${id}-base`} x1="95" y1="10" x2="220" y2="370" gradientUnits="userSpaceOnUse"><stop stopColor="#56b282"/><stop offset=".34" stopColor="#087454"/><stop offset=".72" stopColor="#034c3b"/><stop offset="1" stopColor="#002b22"/></linearGradient>
          <linearGradient id={`${id}-plate`} x1="50" y1="10" x2="250" y2="390" gradientUnits="userSpaceOnUse"><stop stopColor="#67bc87"/><stop offset=".35" stopColor="#168361"/><stop offset=".7" stopColor="#045640"/><stop offset="1" stopColor="#003c2e"/></linearGradient>
          <radialGradient id={`${id}-light`} cx=".26" cy=".16" r=".85"><stop stopColor="#c2f4c4" stopOpacity=".32"/><stop offset=".55" stopColor="#2fa66e" stopOpacity="0"/><stop offset="1" stopColor="#001e17" stopOpacity=".3"/></radialGradient>
          <filter id={`${id}-glow`} x="-80%" y="-80%" width="260%" height="260%"><feGaussianBlur stdDeviation="3.5"/></filter>
          <clipPath id={`${id}-clip`}><path d="M160 15C239 10 292 95 291 191C290 295 225 384 162 386C89 387 31 310 29 215C26 116 83 24 160 15Z"/></clipPath>
        </defs>
        <g transform="scale(1 .86)">
        <path d="M160 15C239 10 292 95 291 191C290 295 225 384 162 386C89 387 31 310 29 215C26 116 83 24 160 15Z" fill={`url(#${id}-base)`} stroke="#063f31" strokeWidth="3"/>
        <g clipPath={`url(#${id}-clip)`}>
          {plates.map(d=><path key={d} d={d} fill={`url(#${id}-plate)`} stroke="#003f30" strokeWidth="3.5" strokeLinejoin="round"/>)}
          {plates.map(d=><path key={d} d={d} stroke="#b5ddb0" strokeOpacity=".18" strokeWidth="1" transform="translate(0 1.6)"/>)}
          <path d="M21 0H301V400H21Z" fill={`url(#${id}-light)`}/>
          <g className={styles.networkGlow} stroke="#ffe28a" strokeWidth="3" filter={`url(#${id}-glow)`}>
            {links.map(([a,b])=><path key={`${a}-${b}`} d={`M${nodes[a]}L${nodes[b]}`}/>)}
          </g>
          <g stroke="#ffe29a" strokeWidth="1.45" strokeLinecap="round" className={styles.networkLines}>
            {links.map(([a,b],i)=><path key={`${a}-${b}`} d={`M${nodes[a]}L${nodes[b]}`} pathLength="1" style={{animationDelay:`${.25+i*.025}s`}}/>)}
          </g>
          <g className={styles.networkGlow} fill="#ffd65b" filter={`url(#${id}-glow)`}>{nodes.map(([x,y])=><circle key={`${x}-${y}`} cx={x} cy={y} r="7.5"/>)}</g>
          <g className={styles.networkNodes}>{nodes.map(([x,y],i)=><circle key={`${x}-${y}`} cx={x} cy={y} r={i%3===0?3.8:3.1} fill="#fff8d4" style={{animationDelay:`${i*.14}s`}}/>)}</g>
        </g>
        </g>
      </svg>
    </div>
  </div>;
}

export function ProgressLine({ completed, total = 3 }: { completed: number; total?: number }) {
  return <svg className={styles.progressVector} viewBox={`0 0 ${total * 52} 20`} role="img" aria-label={`${completed} of ${total} preview tasks complete`}>
    <path d={`M10 10H${(total-1)*52+10}`} stroke="currentColor" strokeOpacity=".18" strokeWidth="1.5"/>
    <path className={styles.progressFill} d={`M10 10H${(total-1)*52+10}`} stroke="#a47b23" strokeWidth="2" pathLength="1" strokeDasharray="1" style={{strokeDashoffset:total>1?1-Math.max(0,completed-1)/(total-1):1}}/>
    {Array.from({length:total},(_,i)=><g key={i}><circle cx={i*52+10} cy="10" r="5" fill={i<completed?"#a47b23":"#f6f4ed"} stroke={i<completed?"#a47b23":"#9faea2"}/>{i<completed&&<path d={`M${i*52+7.5} 10l1.7 1.8 3.4-3.6`} stroke="#fffbee" strokeWidth="1.2" fill="none"/>}</g>)}
  </svg>;
}
