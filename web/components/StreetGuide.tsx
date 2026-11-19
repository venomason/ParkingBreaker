"use client";

import Image from "next/image";
import { useCallback, useRef, useState, type CSSProperties } from "react";
import { BottomSheet } from "./BottomSheet";

const artwork = "/images/street/hurtles-block.webp";
const views = [
  { label: "Whole block", title: "From the curb to the mailbox", detail: "Explore the car, the writing desk, and the mailbox to see how your appeal takes shape.", x: 50, y: 50, zoom: 1 },
  { label: "Ticket", title: "It starts at the windshield.", detail: "Have your ticket handy. Check the issuing agency, citation number, and dates against the original before you continue.", x: 17, y: 55, zoom: 1.85 },
  { label: "Desk", title: "Your letter, in your words.", detail: "Tell us what happened. Review the exact letter and supporting pages, then decide whether to approve them for mailing.", x: 51, y: 56, zoom: 1.8 },
  { label: "Mailbox", title: "Follow the mailing record.", detail: "Once an approved packet is mailed, your appeal keeps the mailing record and available tracking updates together.", x: 76, y: 69, zoom: 1.85 },
] as const;

export function StreetGuide() {
  const [open, setOpen] = useState(false);
  const [index, setIndex] = useState(0);
  const close = useCallback(() => setOpen(false), []);
  const gesture = useRef<{ x: number; y: number } | null>(null);
  const navigation = useRef<HTMLDivElement>(null);
  const view = views[index];
  const offset = (coordinate: number) => Math.max(100 * (1 - view.zoom), Math.min(0, 50 - view.zoom * coordinate));
  const camera = {
    "--zoom": view.zoom, "--x": offset(view.x) + "%", "--y": offset(view.y) + "%",
    "--pin-scale": 1 / view.zoom,
  } as CSSProperties;

  function chooseFromKeys(event: React.KeyboardEvent<HTMLButtonElement>, current: number) {
    const target = { ArrowLeft: (current + views.length - 1) % views.length, ArrowRight: (current + 1) % views.length, Home: 0, End: views.length - 1 }[event.key];
    if (target === undefined) return;
    event.preventDefault();
    setIndex(target);
    navigation.current?.querySelectorAll("button")[target]?.focus({ preventScroll: true });
  }

  return <>
    <button type="button" className="street-launch" aria-haspopup="dialog" aria-controls="street-guide"
      onClick={() => { setIndex(0); setOpen(true); }}>
      <span className="street-postcard"><Image src={artwork} alt="" sizes="(min-width: 1024px) 520px, (min-width: 640px) 190px, 94px" fill /></span>
      <span className="street-invite">Explore how it works</span>
      <span className="street-arrow" aria-hidden="true">↗</span>
    </button>
    <BottomSheet id="street-guide" isOpen={open} onClose={close} title="Hurtle’s block" closeAriaLabel="Close Hurtle’s block">
      {open && <>
      <div className="street-viewport" style={camera}
        onPointerDown={event => {
          if (!event.isPrimary || event.button !== 0 || (event.target as Element).closest("button")) return;
          gesture.current = { x: event.clientX, y: event.clientY };
          event.currentTarget.setPointerCapture(event.pointerId);
        }}
        onPointerCancel={() => { gesture.current = null; }}
        onPointerUp={event => {
          if (!event.isPrimary || !gesture.current) return;
          const dx = event.clientX - gesture.current.x;
          const dy = event.clientY - gesture.current.y;
          gesture.current = null;
          if (Math.abs(dx) > 38 && Math.abs(dx) > Math.abs(dy) * 1.4) {
            setIndex(n => Math.max(0, Math.min(views.length - 1, n + (dx < 0 ? 1 : -1))));
          }
        }}>
        <div className="street-camera">
          <Image src={artwork} alt="A miniature street with a ticket on a pine-green car, a lamp-lit writing desk, and a blue mailbox." fill sizes="(min-width: 710px) 1312px, 185vw" draggable={false} />
          {index > 0 && <span key={view.label} className={`street-detail detail-${view.label.toLowerCase()}`} aria-hidden="true" />}
          {views.slice(1).map((stop, i) => <button key={stop.label} className="street-pin" type="button"
            style={{ left: stop.x + "%", top: stop.y + "%" }} hidden={index !== 0 && index !== i + 1}
            aria-pressed={index === i + 1} aria-label={index === i + 1 ? "Return to the whole block" : "Explore the " + stop.label.toLowerCase()}
            onClick={() => setIndex(index === i + 1 ? 0 : i + 1)}>
            <span aria-hidden="true">{index === i + 1 ? "−" : "+"}</span>
          </button>)}
        </div>
      </div>
      <div className="street-controls" ref={navigation} role="group" aria-label="Choose a place on the block">
        {views.map((stop, i) => <button type="button" key={stop.label} aria-pressed={i === index}
          onClick={() => setIndex(i)} onKeyDown={event => chooseFromKeys(event, i)}>{stop.label}</button>)}
      </div>
      <div className="street-caption" aria-live="polite" aria-atomic="true">
        <h3 key={view.title}>{view.title}</h3><p>{view.detail}</p>
      </div>
      </>}
    </BottomSheet>
  </>;
}
