"use client";

import React, { useEffect, useImperativeHandle, useRef, useState, forwardRef } from "react";
import { loadStripe } from "@stripe/stripe-js/pure";
import type { Stripe, StripeCardElement } from "@stripe/stripe-js";

// Only a test publishable key is accepted outside a production build.
const KEY = process.env.NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY ?? "";
export const CARD_FORM_AVAILABLE = KEY.startsWith("pk_test_") || (KEY.startsWith("pk_live_") && process.env.NODE_ENV === "production");

export interface CardFieldHandle {
  /** Create a Stripe payment method in the browser; returns its pm_ id or throws. */
  paymentMethod: () => Promise<string>;
}

/** Stripe's own card field. Card data goes from the browser to Stripe, never to our API. */
export const CardField = forwardRef<CardFieldHandle>(function CardField(_props, ref) {
  const mount = useRef<HTMLDivElement>(null);
  const stripe = useRef<Stripe | null>(null);
  const card = useRef<StripeCardElement | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    loadStripe(KEY).then((loaded) => {
      if (cancelled || !loaded || !mount.current) return;
      stripe.current = loaded;
      card.current = loaded.elements().create("card", { hidePostalCode: false });
      card.current.mount(mount.current);
      card.current.on("change", (event) => setProblem(event.error?.message ?? null));
    });
    return () => {
      cancelled = true;
      card.current?.destroy();
    };
  }, []);

  useImperativeHandle(ref, () => ({
    paymentMethod: async () => {
      if (!stripe.current || !card.current) throw new Error("card_form_not_ready");
      const result = await stripe.current.createPaymentMethod({ type: "card", card: card.current });
      if (result.error || !result.paymentMethod) {
        setProblem(result.error?.message ?? "The card could not be used.");
        throw new Error("card_not_accepted");
      }
      return result.paymentMethod.id;
    },
  }));

  return (
    <div>
      <div ref={mount} id="cardField" style={{ padding: "12px", border: "1.5px solid var(--line-strong)", borderRadius: "11px", background: "#fff" }} />
      {problem && <p className="err" role="alert">{problem}</p>}
    </div>
  );
});
