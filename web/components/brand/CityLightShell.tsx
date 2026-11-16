"use client";

import { useEffect, useId, useRef, useState, type CSSProperties } from "react";
import { CITY_LIGHT_CSS, CITY_NODES, LIGHT_EDGES, NODE_ARRIVAL, SHELL_OUTLINE, SHELL_PLATES, completedNodeIds } from "./cityLightNetwork";

type Props={mode?:"hero"|"progress";completed?:number;total?:number;size?:number;compact?:boolean;replay?:number};
export function CityLightShell({mode="hero",completed=0,total=3,size=280,compact=false,replay=0}:Props) {
  const id=useId().replace(/:/g,"");
  const svg=useRef<SVGSVGElement>(null);
  const [motion,setMotion]=useState({ready:false,enabled:false});
  useEffect(()=>{
    const root=svg.current?.closest("[data-brand-preview]");
    const preference=window.matchMedia("(prefers-reduced-motion: reduce)");
    const update=()=>setMotion({ready:true,enabled:!preference.matches && root?.getAttribute("data-motion")==="playing"});
    const observer=new MutationObserver(update);
    if(root)observer.observe(root,{attributes:true,attributeFilter:["data-motion"]});
    const frame=requestAnimationFrame(update);
    preference.addEventListener("change",update);
    return ()=>{cancelAnimationFrame(frame);observer.disconnect();preference.removeEventListener("change",update);};
  },[]);
  const lit=mode==="progress"?completedNodeIds(completed,total):new Set(CITY_NODES.map((_,i)=>i));
  const animate=mode==="hero"&&motion.enabled&&motion.ready;
  const visibleCompleted=Number.isFinite(completed)&&Number.isFinite(total)&&total>0?Math.min(Math.max(0,Math.floor(completed)),Math.floor(total)):0;
  return <svg ref={svg} key={replay} xmlns="http://www.w3.org/2000/svg" width={size} height={size*350/320} viewBox="0 0 320 350" fill="none" role="img" aria-label={mode==="progress"?`${visibleCompleted} of ${total} tasks complete` :"Hurtle Turtle’s emerald shell with a connected gold light network"} data-city-shell data-mode={mode} data-detail={compact?"compact":"full"} data-animate={animate} data-ready={mode==="progress"||motion.ready}>
    <style>{CITY_LIGHT_CSS}</style>
    <defs>
      <linearGradient id={`${id}-base`} x1="95" y1="10" x2="220" y2="370" gradientUnits="userSpaceOnUse"><stop stopColor="#56b282"/><stop offset=".34" stopColor="#087454"/><stop offset=".72" stopColor="#034c3b"/><stop offset="1" stopColor="#002b22"/></linearGradient>
      <linearGradient id={`${id}-plate`} x1="50" y1="10" x2="250" y2="390" gradientUnits="userSpaceOnUse"><stop stopColor="#67bc87"/><stop offset=".35" stopColor="#168361"/><stop offset=".7" stopColor="#045640"/><stop offset="1" stopColor="#003c2e"/></linearGradient>
      <radialGradient id={`${id}-light`} cx=".26" cy=".16" r=".85"><stop stopColor="#c2f4c4" stopOpacity=".26"/><stop offset=".55" stopColor="#2fa66e" stopOpacity="0"/><stop offset="1" stopColor="#001e17" stopOpacity=".3"/></radialGradient>
      <radialGradient id={`${id}-halo`}><stop stopColor="#ffe08a" stopOpacity=".52"/><stop offset=".42" stopColor="#f4d475" stopOpacity=".23"/><stop offset="1" stopColor="#f4d475" stopOpacity="0"/></radialGradient>
      <clipPath id={`${id}-clip`}><path d={SHELL_OUTLINE}/></clipPath>
    </defs>
    <g transform="scale(1 .86)">
      <path data-layer="silhouette" d={SHELL_OUTLINE} fill={`url(#${id}-base)`} stroke="#063f31" strokeWidth="3"/>
      <g clipPath={`url(#${id}-clip)`}>
        <g data-layer="plates">{SHELL_PLATES.map(d=><path key={d} d={d} fill={`url(#${id}-plate)`} stroke="#003f30" strokeWidth="2.8" strokeLinejoin="round"/>)}</g>
        <g data-layer="plate-highlights">{SHELL_PLATES.map(d=><path key={d} d={d} stroke="#b5ddb0" strokeOpacity=".13" strokeWidth="1" transform="translate(0 1.2)"/>)}</g>
        <path data-layer="surface-light" d="M21 0H301V400H21Z" fill={`url(#${id}-light)`}/>
        <g data-layer="unlit-connections">{LIGHT_EDGES.map(e=><path key={`${e.from}-${e.to}`} d={e.path} className="city-wire"/>)}</g>
        <g data-layer="unlit-nodes">{CITY_NODES.map(([x,y],i)=><circle key={i} className="city-node-dark" cx={x} cy={y} r={compact?4:2.5}/>)}</g>
        <g data-layer="connection-depth">{LIGHT_EDGES.map(e=><path key={`${e.from}-${e.to}`} d={e.path} className="city-line-depth" opacity={lit.has(e.from)&&lit.has(e.to)?1:0}/>)}</g>
        <g data-layer="illuminated-connections">{LIGHT_EDGES.map((e,i)=><path key={i} data-edge={`${e.from}-${e.to}`} data-lit={lit.has(e.from)&&lit.has(e.to)} d={e.path} className="city-lit-line" pathLength="1" style={{"--arrival":`${e.start}s`,"--duration":`${e.duration}s`} as CSSProperties}/>)}</g>
        <g data-layer="travelling-light" aria-hidden="true">{mode==="hero"&&LIGHT_EDGES.map((e,i)=><circle key={i} className="city-traveler" r="2" style={{"--arrival":`${e.start}s`,"--duration":`${e.duration}s`,"--from-x":`${CITY_NODES[e.from][0]}px`,"--from-y":`${CITY_NODES[e.from][1]}px`,"--to-x":`${CITY_NODES[e.to][0]}px`,"--to-y":`${CITY_NODES[e.to][1]}px`} as CSSProperties}/>)}</g>
        <g data-layer="illuminated-nodes">{CITY_NODES.map(([x,y],i)=><g key={i} data-node={i} data-lit={lit.has(i)} className="city-node-light" style={{"--arrival":`${NODE_ARRIVAL[i]}s`} as CSSProperties}><circle className="city-halo" cx={x} cy={y} r="10" fill={`url(#${id}-halo)`}/><circle className="city-node-core" cx={x} cy={y} r={compact?4:2.6}/></g>)}</g>
      </g>
    </g>
  </svg>;
}
