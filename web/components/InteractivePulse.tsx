"use client";

import { useEffect } from "react";

const INTERACTIVE_SELECTOR =
  "button, .btn, .chip, .card, .drop, .access-card, a[href], input, textarea, select, label.check";
const TILT_SELECTOR = ".intake-introduction, .how-pillar";

export function InteractivePulse() {
  useEffect(() => {
    if (typeof window === "undefined") return;
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    const progress = document.createElement("div");
    progress.id = "pb-scroll-progress";
    progress.setAttribute("aria-hidden", "true");
    document.body.appendChild(progress);

    const updateScroll = () => {
      const scrollable = document.documentElement.scrollHeight - window.innerHeight;
      const pct = scrollable > 0 ? Math.min(1, Math.max(0, window.scrollY / scrollable)) : 0;
      progress.style.transform = `scaleX(${pct})`;
    };
    updateScroll();
    window.addEventListener("scroll", updateScroll, { passive: true });

    if (reduced) {
      return () => {
        window.removeEventListener("scroll", updateScroll);
        progress.remove();
      };
    }

    const onPointerDown = (event: PointerEvent) => {
      const target =
        event.target instanceof Element ? event.target.closest(INTERACTIVE_SELECTOR) : null;
      if (!target) return;
      const pulse = document.createElement("span");
      pulse.className = "pb-pointer-pulse";
      pulse.style.left = `${event.clientX}px`;
      pulse.style.top = `${event.clientY}px`;
      document.body.appendChild(pulse);
      window.setTimeout(() => pulse.remove(), 520);
    };

    const hoverPointer = window.matchMedia("(hover: hover) and (pointer: fine)");
    const onPointerMove = (event: PointerEvent) => {
      if (event.pointerType !== "mouse" || !hoverPointer.matches) return;
      const target = event.target instanceof Element ? event.target : null;
      if (!target) return;

      const card = target.closest<HTMLElement>(TILT_SELECTOR);
      if (card) {
        const rect = card.getBoundingClientRect();
        const x = event.clientX - rect.left;
        const y = event.clientY - rect.top;
        card.style.setProperty("--px", `${x}px`);
        card.style.setProperty("--py", `${y}px`);
        const nx = rect.width > 0 ? (x / rect.width - 0.5) * 2 : 0;
        const ny = rect.height > 0 ? (y / rect.height - 0.5) * 2 : 0;
        card.style.setProperty("--rx", `${(-ny * 1.4).toFixed(2)}deg`);
        card.style.setProperty("--ry", `${(nx * 1.4).toFixed(2)}deg`);
      }

      const btn = target.closest<HTMLElement>(".btn-primary");
      if (btn) {
        const rect = btn.getBoundingClientRect();
        const dx = ((event.clientX - (rect.left + rect.width / 2)) / rect.width) * 6;
        const dy = ((event.clientY - (rect.top + rect.height / 2)) / rect.height) * 4;
        btn.style.setProperty("--mx", `${dx.toFixed(1)}px`);
        btn.style.setProperty("--my", `${dy.toFixed(1)}px`);
      }
    };

    const onPointerOut = (event: PointerEvent) => {
      const target = event.target instanceof Element ? event.target : null;
      if (!target) return;
      const card = target.closest<HTMLElement>(TILT_SELECTOR);
      if (card && !card.contains(event.relatedTarget as Node | null)) {
        card.style.setProperty("--rx", "0deg");
        card.style.setProperty("--ry", "0deg");
      }
      const btn = target.closest<HTMLElement>(".btn-primary");
      if (btn && !btn.contains(event.relatedTarget as Node | null)) {
        btn.style.setProperty("--mx", "0px");
        btn.style.setProperty("--my", "0px");
      }
    };

    const onDragOver = (event: DragEvent) => {
      const drop = event.target instanceof Element ? event.target.closest(".drop") : null;
      if (drop) drop.setAttribute("data-drag-active", "true");
    };

    const onDragLeave = (event: DragEvent) => {
      const drop = event.target instanceof Element ? event.target.closest(".drop") : null;
      if (drop) drop.removeAttribute("data-drag-active");
    };

    document.addEventListener("pointerdown", onPointerDown, { passive: true });
    document.addEventListener("pointermove", onPointerMove, { passive: true });
    document.addEventListener("pointerout", onPointerOut, { passive: true });
    document.addEventListener("dragover", onDragOver, { passive: true });
    document.addEventListener("dragleave", onDragLeave, { passive: true });
    document.addEventListener("drop", onDragLeave, { passive: true });
    return () => {
      window.removeEventListener("scroll", updateScroll);
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("pointermove", onPointerMove);
      document.removeEventListener("pointerout", onPointerOut);
      document.removeEventListener("dragover", onDragOver);
      document.removeEventListener("dragleave", onDragLeave);
      document.removeEventListener("drop", onDragLeave);
      progress.remove();
    };
  }, []);

  return null;
}
