"use client";

import Image from "next/image";
import { useRef, useState } from "react";

// The last three frames reuse one background so the viewer's position stays fixed.
const photos: { scene: string; pose?: string; alt: string }[] = [
  { scene: "front", alt: "Hurtle holding a phone near the front of a parked car." },
  { scene: "rear-right", alt: "Hurtle photographing the car from the rear passenger side." },
  { scene: "wide-street", pose: "ahead", alt: "In front of the car, Hurtle points his phone out toward the street." },
  { scene: "wide-street", pose: "behind-right", alt: "Behind the right rear corner, Hurtle faces outward with his phone." },
  { scene: "wide-street", pose: "behind-left", alt: "Behind the left rear corner, Hurtle turns toward the surroundings with his phone." },
];

export function IntakeIntroduction() {
  const strip = useRef<HTMLDivElement>(null);
  const [current, setCurrent] = useState(0);

  function showPhoto(index: number) {
    const element = strip.current;
    if (element) element.scrollTo({ left: index * element.clientWidth });
  }

  return <section className="intake-introduction guide-cover" aria-labelledby="intake-title">
    <div className="curbside-copy">
      <span className="curbside-kicker">Curbside photo guide</span>
      <h1 id="intake-title">As soon as you spot a ticket, start taking photos.</h1>
      <p className="curbside-intro">Get both sides of the ticket and photos of the area around your car. You can review them later and choose which ones accurately support what you’re saying.</p>
    </div>
    <div className="curbside-gallery">
      <div className="curbside-strip" id="curbside-illustrations" ref={strip} role="group" aria-label="Hurtle’s photo illustrations" tabIndex={0} onScroll={event => {
        const { scrollLeft, clientWidth } = event.currentTarget;
        if (clientWidth) setCurrent(Math.max(0, Math.min(photos.length - 1, Math.round(scrollLeft / clientWidth))));
      }}>
        {photos.map(({ scene, pose, alt }, index) => (
          <div className="curbside-frame" key={pose ?? scene}>
            <Image src={`/images/curbside/${scene}.webp`} alt={alt} width={1200} height={800}
              sizes="(min-width: 1024px) 520px, (min-width: 600px) 48vw, 100vw" loading={index === 0 ? "eager" : "lazy"} />
            {pose && <Image className="curbside-position" src={`/images/curbside/${pose}.webp`} alt="" fill
              sizes="(min-width: 1024px) 520px, (min-width: 600px) 48vw, 100vw" />}
          </div>
        ))}
      </div>
      <div className="curbside-arrows">
        <span className="curbside-count" role="status" aria-atomic="true">Illustration {current + 1} of {photos.length}</span>
        <button type="button" aria-label="Previous illustration" aria-controls="curbside-illustrations" disabled={current === 0} onClick={() => showPhoto(current - 1)}>‹</button>
        <button type="button" aria-label="Next illustration" aria-controls="curbside-illustrations" disabled={current === photos.length - 1} onClick={() => showPhoto(current + 1)}>›</button>
      </div>
    </div>
  </section>;
}
