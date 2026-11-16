// Preserve the current shell silhouette, plate layout, gold network and palette.
export const SHELL_OUTLINE = "M160 15C239 10 292 95 291 191C290 295 225 384 162 386C89 387 31 310 29 215C26 116 83 24 160 15Z";
export const SHELL_PLATES = [
  "M151 20 Q190 17 220 43 L206 66 158 58 135 38Z", "M130 28 153 62 119 91 79 76 Q96 45 130 28Z",
  "M226 48 Q257 72 270 108 L238 121 214 75Z", "M73 83 111 98 93 143 47 140 Q54 109 73 83Z",
  "M160 67 201 75 228 123 202 157 142 151 120 104Z", "M238 134 276 118 Q288 154 286 188 L239 199 214 166Z",
  "M43 152 89 155 119 198 103 234 37 235 Q31 192 43 152Z", "M111 160 140 163 199 169 225 204 204 251 135 251 111 211Z",
  "M239 211 284 201 Q281 249 262 280 L220 265Z", "M39 248 103 246 124 277 105 321 65 307 Q47 281 39 248Z",
  "M139 264 203 264 220 291 197 338 138 337 116 310Z", "M226 279 257 292 Q242 325 213 346 L206 336Z",
  "M72 318 106 333 134 347 125 369 Q94 354 72 318Z", "M140 349 189 350 203 361 Q178 384 139 375Z",
] as const;
export const CITY_NODES = [[152,51],[78,87],[118,109],[224,92],[180,132],[41,168],[111,192],[268,171],[196,209],[219,233],[263,252],[179,267],[48,253],[91,281],[89,326],[158,347],[221,330]] as const;
export const CITY_LINKS = [[0,1],[0,2],[0,3],[0,4],[1,2],[1,5],[2,4],[3,4],[3,7],[4,6],[4,7],[4,8],[5,6],[5,12],[6,7],[6,8],[6,11],[6,12],[6,13],[7,9],[7,10],[8,9],[8,11],[9,10],[10,11],[10,16],[11,13],[11,15],[11,16],[12,13],[13,14],[14,15],[15,16]] as const;

// Shortest travel distance makes each node wake only after an incoming connection
// reaches it. An edge always starts at an already reached node, never at random.
const distances:number[] = CITY_NODES.map((_,i)=>i===0?0:Infinity);
const visited = new Set<number>();
const length=(a:number,b:number)=>Math.hypot(CITY_NODES[a][0]-CITY_NODES[b][0],CITY_NODES[a][1]-CITY_NODES[b][1]);
for(let i=0;i<CITY_NODES.length;i++) {
  const next=distances.reduce((best,d,index)=>!visited.has(index) && (best===-1||d<distances[best])?index:best,-1);
  visited.add(next);
  for(const [a,b] of CITY_LINKS) {
    const neighbor=a===next?b:b===next?a:-1;
    if(neighbor>=0)distances[neighbor]=Math.min(distances[neighbor],distances[next]+length(next,neighbor));
  }
}
const speed=125;
export const NODE_ARRIVAL = distances.map(d=>.18+d/speed);
export const NODE_ORDER = CITY_NODES.map((_,i)=>i).sort((a,b)=>distances[a]-distances[b]);
export const LIGHT_EDGES = CITY_LINKS.map(([a,b])=>{
  const [from,to]=distances[a]<=distances[b]?[a,b]:[b,a];
  return {from,to,start:NODE_ARRIVAL[from],duration:length(from,to)/speed,path:`M${CITY_NODES[from]}L${CITY_NODES[to]}`};
});
export const SEQUENCE_SECONDS = Math.max(...LIGHT_EDGES.map(edge=>edge.start+edge.duration))+.45;

export function completedNodeIds(completed:number,total:number):Set<number> {
  if(!Number.isFinite(completed)||!Number.isFinite(total)||total<=0)return new Set();
  const count=Math.min(Math.max(0,Math.floor(completed)),Math.floor(total));
  return new Set(NODE_ORDER.slice(0,Math.ceil(CITY_NODES.length*count/Math.floor(total))));
}

// Embedded in the SVG so exported artwork remains independently usable.
export const CITY_LIGHT_CSS = `
[data-city-shell] .city-wire{fill:none;stroke:#b0a260;stroke-width:1.05;opacity:.23;stroke-linecap:round}
[data-city-shell] .city-lit-line{fill:none;stroke:#f4d77d;stroke-width:1.45;stroke-linecap:round;stroke-dasharray:1;stroke-dashoffset:0}
[data-city-shell] .city-line-depth{fill:none;stroke:#0b3225;stroke-width:3;opacity:.5}
[data-city-shell] .city-node-dark{fill:#1b624b;stroke:#799c66;stroke-width:.65}
[data-city-shell] .city-node-core{fill:#fff2c1}
[data-city-shell] .city-halo{opacity:.65}
[data-city-shell] .city-traveler{opacity:0;fill:#fff9df}
[data-city-shell][data-detail=compact] .city-wire{opacity:.17;stroke-width:1.8}
[data-city-shell][data-detail=compact] .city-lit-line{stroke-width:2.7}
[data-city-shell][data-detail=compact] .city-halo{display:none}
[data-city-shell][data-detail=compact] [data-layer=plate-highlights]{display:none}
[data-city-shell] [data-lit=false].city-lit-line{stroke-dashoffset:1;opacity:0}
[data-city-shell] [data-lit=false].city-node-light{opacity:0}
[data-city-shell][data-mode=progress] [data-lit=true].city-lit-line{transition:stroke-dashoffset .55s ease,opacity .25s}
[data-city-shell][data-mode=progress] [data-lit=true].city-node-light{transition:opacity .4s .25s}
[data-city-shell][data-animate=true] .city-lit-line{animation:city-flow var(--duration) linear var(--arrival) both}
[data-city-shell][data-animate=true] .city-node-light{animation:city-arrive .38s ease-out var(--arrival) both}
[data-city-shell][data-animate=true] .city-traveler{animation:city-travel var(--duration) linear var(--arrival) both}
[data-city-shell][data-ready=false] .city-lit-line,[data-city-shell][data-ready=false] .city-node-light:not([data-node='0']){opacity:0}
@keyframes city-flow{from{stroke-dashoffset:1;opacity:0}1%{opacity:1}to{stroke-dashoffset:0;opacity:1}}
@keyframes city-arrive{from{opacity:0}to{opacity:1}}
@keyframes city-travel{0%{transform:translate(var(--from-x),var(--from-y));opacity:0}8%,85%{opacity:1}100%{transform:translate(var(--to-x),var(--to-y));opacity:0}}
@media(prefers-reduced-motion:reduce){[data-city-shell] *{animation:none!important;transition:none!important}[data-city-shell] .city-traveler{display:none}}
`;
