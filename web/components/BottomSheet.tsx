"use client";

import React, { useEffect, useRef } from "react";
import { Icon } from "./Icons";

interface BottomSheetProps {
  id: string;
  isOpen: boolean;
  onClose: () => void;
  title: string;
  children: React.ReactNode;
  footer?: React.ReactNode;
  closeAriaLabel?: string;
}

export function BottomSheet({
  id,
  isOpen,
  onClose,
  title,
  children,
  footer,
  closeAriaLabel = "Close",
}: BottomSheetProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const handleRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const dlg = dialogRef.current;
    if (!dlg) return;

    if (isOpen) {
      if (!dlg.open) {
        dlg.showModal();
      }
    } else {
      if (dlg.open) {
        dlg.close();
      }
    }
  }, [isOpen]);

  useEffect(() => {
    const dlg = dialogRef.current;
    const handle = handleRef.current;
    if (!dlg || !handle) return;

    let startY = 0;
    let curY = 0;
    let dragging = false;

    const onPointerDown = (e: PointerEvent) => {
      dragging = true;
      startY = e.clientY;
      curY = 0;
      dlg.style.transition = "none";
      handle.setPointerCapture(e.pointerId);
    };

    const onPointerMove = (e: PointerEvent) => {
      if (!dragging) return;
      curY = Math.max(0, e.clientY - startY);
      dlg.style.transform = `translateY(${curY}px)`;
    };

    const onPointerUp = () => {
      if (!dragging) return;
      dragging = false;
      dlg.style.transition = "";
      if (curY > 95) {
        dlg.style.transform = "";
        onClose();
      } else {
        dlg.style.transform = "";
      }
    };

    handle.addEventListener("pointerdown", onPointerDown);
    handle.addEventListener("pointermove", onPointerMove);
    handle.addEventListener("pointerup", onPointerUp);
    handle.addEventListener("pointercancel", onPointerUp);

    return () => {
      handle.removeEventListener("pointerdown", onPointerDown);
      handle.removeEventListener("pointermove", onPointerMove);
      handle.removeEventListener("pointerup", onPointerUp);
      handle.removeEventListener("pointercancel", onPointerUp);
    };
  }, [onClose]);

  return (
    <dialog
      ref={dialogRef}
      id={id}
      aria-labelledby={`${id}-h`}
      onClick={(e) => {
        if (e.target === dialogRef.current) {
          onClose();
        }
      }}
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
    >
      <div className="sheet-pill-wrap" ref={handleRef}>
        <span className="sheet-pill" />
      </div>
      <div className="dlg-h">
        <h2 id={`${id}-h`}>{title}</h2>
        <button
          className="iconbtn"
          type="button"
          onClick={onClose}
          aria-label={closeAriaLabel}
        >
          <Icon id="x" size={16} />
        </button>
      </div>
      <div className="dlg-b">{children}</div>
      {footer && <div className="dlg-f">{footer}</div>}
    </dialog>
  );
}
