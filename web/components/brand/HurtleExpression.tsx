"use client";

import { CityLightShell } from "./CityLightShell";
import styles from "./city-light-lab.module.css";

export type HurtleMood="iconic"|"attention"|"speaking"|"setback";

export function HurtleExpression({mood,replay=0}:{mood:HurtleMood;replay?:number}) {
  return <div className={styles.expression} data-expression={mood} key={`${mood}-${replay}`}>
    <div className={styles.expressionShell}><CityLightShell size={220} replay={replay}/></div>
  </div>;
}
