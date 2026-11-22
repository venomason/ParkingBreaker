"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import Link from "next/link";
import { BrandLogo } from "./BrandLogo";
import { AccessLoading } from "./AccessLoading";
import { api, ApiError } from "../lib/api";
import type { BetaOffer, CaseStatus, ServicePhase } from "../lib/api";

export function AccessPanel({ children }: { children: ReactNode }) {
  return <main className="access-shell"><Link className="access-brand" href="/" aria-label="ParkingBreaker home"><BrandLogo /></Link><section className="access-card">{children}</section></main>;
}

export function RequestAccess({ workspaceId }: { workspaceId: string }) {
  const [sent, setSent] = useState(false);
  const [error, setError] = useState(false);
  const [busy, setBusy] = useState(false);
  const pending = useRef<{email: string; id: string} | null>(null);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    const email = String(new FormData(event.currentTarget).get("email") ?? "").trim();
    if (pending.current?.email !== email) pending.current = { email, id: crypto.randomUUID() };
    setBusy(true); setError(false);
    try { await api.requestAccess(email, pending.current.id, workspaceId); setSent(true); }
    catch { setError(true); } finally { setBusy(false); }
  }
  return <AccessPanel><h1>Return to this appeal</h1>{sent ? <p>If the address matches this appeal, a private link will arrive shortly.</p> : <><p>Enter the email you supplied for this ticket. Each appeal has separate access.</p><form onSubmit={submit}><label htmlFor="recovery-email">Email address</label><input id="recovery-email" name="email" type="email" required maxLength={254} /><button className="btn btn-primary" type="submit" disabled={busy}>{busy ? "Requesting…" : "Email this appeal’s access link"}</button></form></>}{error && <p role="alert">We couldn’t request a link. Try again.</p>}</AccessPanel>;
}

export type FundingState = "checking" | "beta" | "paid" | "unknown" | "refreshing" | "unresolved";
/** Resolved immutable server posture; "checking"/"unknown" must never imply paid. */
export type PhaseState = "checking" | ServicePhase | "unknown";

/**
 * Status-level payment evidence, distinct from the context (phase + funding).
 * Only a saved case with an authoritative payment state and a launched window is
 * admitted paid. A pending/uncertain original authorization is kept separate.
 */
export type SavedFundingKind = "none" | "beta" | "saved_pending" | "admitted_paid";
export function savedFundingKind(status: Pick<CaseStatus, "payment_state" | "window_started_at"> | null): SavedFundingKind {
  if (!status) return "none";
  if (status.payment_state === "NO_CHARGE") return "beta";
  if (status.window_started_at == null) return "none";
  if (status.payment_state === "AUTHORIZED" || status.payment_state === "CAPTURED") return "admitted_paid";
  if (status.payment_state === "CAPTURE_PENDING" || status.payment_state === "CAPTURE_RETRYING") return "saved_pending";
  return "none";
}

export type ServiceAccess = {
  phase: PhaseState;
  funding: FundingState;
  savedFunding: SavedFundingKind;
  /** A paid-order refusal latch; removes every new-paid affordance immediately. */
  newPaidBlocked: boolean;
  pricingRefreshFailed: boolean;
  /** Uncached refresh of the immutable server phase using POST /auth/pricing. */
  refreshPricing: () => Promise<boolean>;
  /**
   * Apply authenticated status/offer evidence to the shared funding posture.
   * Never accepts beta, never authorizes, never infers eligibility locally.
   */
  beginFundingRefresh: () => void;
  applyFunding: (status: CaseStatus | null, offer: BetaOffer | null | undefined) => void;
  blockNewPaid: () => void;
};

const ServicePhaseContext = createContext<ServiceAccess>({
  phase: "unknown",
  funding: "unknown",
  savedFunding: "none",
  newPaidBlocked: false,
  pricingRefreshFailed: false,
  refreshPricing: async () => false,
  beginFundingRefresh: () => {},
  applyFunding: () => {},
  blockNewPaid: () => {},
});

/** Journey reads the resolved phase/funding/saved status to decide what may be mounted. */
export function useServicePhase() {
  return useContext(ServicePhaseContext);
}

export function CustomerGate({ children }: { children: ReactNode }) {
  const [pricing, setPricing] = useState<number | null>(null);
  const [pricingReady, setPricingReady] = useState(false);
  const [pricingError, setPricingError] = useState(false);
  // Phase is the server's immutable posture, resolved separately from the
  // per-case funding state. Neither may stand in for the other.
  const [phase, setPhase] = useState<PhaseState>("checking");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const [hasAccess, setHasAccess] = useState(false);
  const [hasExistingAppeal, setHasExistingAppeal] = useState(false);
  const [recovery, setRecovery] = useState<string | null>(null);
  // Funding is resolved separately from price assignment: a server-confirmed beta
  // case must never depend on, or display, a paid price for this case.
  const [funding, setFunding] = useState<FundingState>("checking");
  const [savedFunding, setSavedFunding] = useState<SavedFundingKind>("none");
  const [newPaidBlocked, setNewPaidBlocked] = useState(false);
  const [pricingRefreshFailed, setPricingRefreshFailed] = useState(false);
  const accessPending = useRef(false);
  const fundingToken = useRef(0);
  const pricingToken = useRef(0);
  // Initial pricing is deduped by api.pricing. An explicit refresh uses the same
  // endpoint without the cache; the token makes a late older response inert.
  const loadPricing = useCallback(async (refresh: boolean): Promise<boolean> => {
    const token = ++pricingToken.current;
    if (!refresh) {
      // Defer the visible reset so an effect-triggered load does not cascade.
      queueMicrotask(() => {
        if (token !== pricingToken.current) return;
        setPricingError(false);
        setPricingReady(false);
        setPhase("checking");
      });
    }
    // A refresh keeps any earlier failed-refresh block in force while this retry is
    // pending. Only a fully validated fresh response may clear it, so an automatic
    // focus/poll retry can never flash a stale paid checkout in the meantime.
    try {
      const assignment = await (refresh ? api.refreshPricing() : api.pricing());
      if (token !== pricingToken.current) return false;
      if (!assignment.experiment_id) throw new Error("invalid pricing experiment");
      setPhase(assignment.service_phase);
      if (assignment.amount_cents === null && assignment.arm === null) {
        setPricing(null);
      } else {
        if (
          !Number.isSafeInteger(assignment.amount_cents) ||
          assignment.amount_cents! <= 0 ||
          assignment.arm !== `P${assignment.amount_cents}`
        ) throw new Error("invalid pricing assignment");
        setPricing(assignment.amount_cents);
      }
      setPricingError(false);
      setPricingRefreshFailed(false);
      return true;
    } catch {
      if (token !== pricingToken.current) return false;
      if (refresh) {
        // Keep the resolved phase and the mounted journey; a failed refresh
        // never stands in for evidence of paid ordering.
        setPricingRefreshFailed(true);
      } else {
        setPricingError(true);
        setPhase("unknown");
      }
      return false;
    } finally {
      if (token === pricingToken.current) setPricingReady(true);
    }
  }, []);
  const refreshPricing = useCallback(() => loadPricing(true), [loadPricing]);
  const blockNewPaid = useCallback(() => setNewPaidBlocked(true), []);
  // One shared funding update fed only by authenticated status/offer reads. A
  // selected beta (accepted or not) is beta, so the paid header/card charge text
  // is hidden immediately; a saved admitted paid/pending record stays paid even
  // while new admissions are controlled, and no acceptance is inferred here.
  const beginFundingRefresh = useCallback(() => {
    fundingToken.current += 1;
    setFunding("refreshing");
  }, []);
  const applyFunding = useCallback((status: CaseStatus | null, offer: BetaOffer | null | undefined) => {
    if (!status) { setFunding("unresolved"); return; }
    fundingToken.current += 1;
    const kind = savedFundingKind(status);
    setSavedFunding(kind);
    if (kind === "admitted_paid" || kind === "saved_pending") setFunding("paid");
    else if (status.payment_state === "NO_CHARGE" || offer) setFunding("beta");
    else setFunding(offer === null ? "paid" : "unresolved");
  }, []);
  const loadFunding = useCallback(() => {
    const token = ++fundingToken.current;
    const workspace = new URLSearchParams(window.location.search).get("w");
    queueMicrotask(() => { if (token === fundingToken.current) { setFunding("checking"); setSavedFunding("none"); } });
    api.currentAppeal().then(async (current) => {
      if (token !== fundingToken.current) return;
      setHasExistingAppeal(Boolean(current?.workspace_id));
      setRecovery(workspace && current?.workspace_id !== workspace ? workspace : null);
      if (!current?.case_id) { setFunding("paid"); return; }
      try {
        const status = await api.status(current.case_id);
        if (token !== fundingToken.current) return;
        setSavedFunding(savedFundingKind(status));
        if (status.payment_state === "NO_CHARGE") { setFunding("beta"); return; }
        try {
          await api.betaOffer(current.case_id);
          if (token === fundingToken.current) setFunding("beta");
        } catch (offerError) {
          if (token !== fundingToken.current) return;
          // Only the exact unselected-entitlement refusal permits the paid path.
          setFunding(offerError instanceof ApiError && offerError.status === 409 && offerError.detail === "beta_entitlement_required" ? "paid" : "unknown");
        }
      } catch {
        if (token === fundingToken.current) setFunding("unknown");
      }
    }).catch((error: unknown) => {
      if (token !== fundingToken.current) return;
      // 401 is the ordinary no-session visitor; anything else stays unresolved.
      if (error instanceof ApiError && error.status === 401) { setHasExistingAppeal(false); setRecovery(workspace); }
      setFunding(error instanceof ApiError && error.status === 401 ? "paid" : "unknown");
    });
  }, []);
  useEffect(() => {
    queueMicrotask(() => { void loadPricing(false); });
    loadFunding();
    api.session().then(() => setHasAccess(true)).catch(() => setHasAccess(false));
  }, [loadFunding, loadPricing]);
  // A private runtime mode change is enforced by the backend immediately. An open
  // screen asks for fresh posture when it becomes visible again, and at most every 15
  // seconds while visible. Only pricing posture is refreshed: funding, saved drafts and
  // the mounted journey stay exactly as they are. A failure keeps the conservative
  // block until a fully validated response succeeds.
  useEffect(() => {
    let last = Date.now();
    let inFlight = false;
    const refresh = () => {
      if (document.visibilityState !== "visible") return;
      const now = Date.now();
      if (now - last < 1000) return;
      last = now;
      if (inFlight) return;
      inFlight = true;
      void refreshPricing().finally(() => { inFlight = false; });
    };
    const onVisibility = () => {
      if (document.visibilityState === "visible") refresh();
    };
    const timer = window.setInterval(refresh, 15_000);
    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("focus", refresh);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [refreshPricing]);
  function retryAccess() {
    loadFunding();
    // A funding failure may have hidden a simultaneous pricing failure. Retry
    // both prerequisites so recovery does not strand the visitor at a second gate.
    void loadPricing(false);
  }
  async function closeAccess() {
    if (accessPending.current) return;
    accessPending.current = true;
    setBusy(true); setError(false);
    try { await api.logout(); window.location.reload(); }
    catch { accessPending.current = false; setError(true); setBusy(false); }
  }
  async function startAnother() {
    if (accessPending.current) return;
    accessPending.current = true;
    setBusy(true); setError(false);
    try {
      await api.startAppeal();
      window.location.replace("/");
    } catch { accessPending.current = false; setError(true); setBusy(false); }
  }
  const beta = funding === "beta";
  const phaseKnown = phase === "paid" || phase === "controlled_free_beta";
  // A public paid price needs resolved paid phase + paid funding, plus a known
  // status that does not already hold this case's own paid/pending record. A
  // refusal latch or failed/unknown phase never shows it.
  const effectivePaidBlocked = newPaidBlocked || pricingRefreshFailed || funding === "refreshing" || funding === "unresolved";
  const priceVisible = !effectivePaidBlocked && savedFunding === "none" && funding === "paid" && phase === "paid" && pricing !== null;
  const pricingLoading = !beta && (!pricingReady || !phaseKnown) && !pricingError;
  const pricingFailed = !beta && pricingError;
  const fundingBlocked = funding === "checking" || funding === "unknown";
  const access = useMemo<ServiceAccess>(() => ({ phase, funding, savedFunding, newPaidBlocked: effectivePaidBlocked, pricingRefreshFailed, refreshPricing, beginFundingRefresh, applyFunding, blockNewPaid }), [phase, funding, savedFunding, effectivePaidBlocked, pricingRefreshFailed, refreshPricing, beginFundingRefresh, applyFunding, blockNewPaid]);
  // Starting a ticket never requires a customer account. Each new appeal has its
  // own credential; switching scope keeps earlier appeals available by their links.
  return <>
    <div className="access-session">
      <span>Each ticket has its own private appeal</span>
      {priceVisible && <span aria-label="Assigned certified mailing price">Early beta price: ${(pricing / 100).toFixed(2)} · Certified Mail, everything included</span>}
      {/* Creating a private appeal scope does not authorize payment or mailing. */}
      <button className="chip" type="button" onClick={startAnother} disabled={busy}>{busy ? "Opening…" : hasExistingAppeal ? "Start another appeal" : "Start an appeal"}</button>
      {hasAccess && <button className="chip" type="button" onClick={closeAccess} disabled={busy}>Close private access</button>}
      {error && <span role="alert">We couldn’t change appeal access. Check your connection and try again.</span>}
    </div>
    {recovery ? <RequestAccess workspaceId={recovery} /> : fundingBlocked ? <AccessPanel>
      {funding === "unknown" && <h1>We couldn’t confirm this appeal’s funding</h1>}
      {funding === "unknown" ? <>
        <p role="alert">We couldn’t load your saved appeal. Check your connection and retry, or start another appeal. No paid checkout is shown until your saved status is confirmed.</p>
        <button className="btn btn-primary" type="button" onClick={retryAccess} disabled={busy}>Retry saved status</button>
      </> : <AccessLoading title="Checking your saved appeal" message="Loading… Checking your saved appeal before opening it." />}
    </AccessPanel> : beta ? <ServicePhaseContext.Provider value={access}>{children}</ServicePhaseContext.Provider> : pricingLoading ? <AccessPanel><AccessLoading /></AccessPanel> : (pricingFailed || !phaseKnown) ? <AccessPanel>
      <h1>Your appeal service is unavailable</h1>
      <p role="alert">We couldn’t load pricing. Check your connection and try again. <button type="button" onClick={() => void loadPricing(false)} disabled={busy}>Retry</button></p>
    </AccessPanel> : <ServicePhaseContext.Provider value={access}>{children}</ServicePhaseContext.Provider>}
  </>;
}

export function ConfirmAccess() {
  const token = useRef("");
  const initialized = useRef(false);
  const inFlight = useRef(false);
  const [state, setState] = useState<"loading" | "ready" | "confirming" | "invalid" | "unavailable" | "done">("loading");
  useEffect(() => {
    if (initialized.current) return;
    initialized.current = true;
    // Fragments never reach HTTP access logs. Remove it immediately and keep it in memory only.
    const fragment = new URLSearchParams(window.location.hash.slice(1));
    const raw = fragment.get("token") ?? "";
    window.history.replaceState(null, "", window.location.pathname);
    if (fragment.getAll("token").length === 1 && /^[A-Za-z0-9_-]{43}$/.test(raw)) {
      token.current = raw;
      queueMicrotask(() => setState("ready"));
    } else queueMicrotask(() => setState("invalid"));
  }, []);
  async function confirm() {
    if (inFlight.current || !token.current) return;
    inFlight.current = true; setState("confirming");
    try {
      await api.confirmAccess(token.current);
      token.current = ""; setState("done");
      window.location.replace("/");
    } catch (error) {
      if (error instanceof ApiError && (error.status === 401 || error.status === 400)) {
        token.current = ""; setState("invalid");
      } else setState("unavailable");
    } finally { inFlight.current = false; }
  }
  return <AccessPanel>
    <p className="access-eyebrow">One last step</p>
    <h1>{state === "invalid" ? "Let’s get you a fresh link" : "Confirm it’s you"}</h1>
    {state === "invalid" ? <><p>This link has expired, has already been used, or isn’t complete.</p><Link className="btn btn-primary" href="/">Start a separate appeal</Link></> : <>
      <p>Open only the appeal associated with this private link on this device.</p>
      <button className="btn btn-primary" type="button" onClick={confirm} disabled={state === "loading" || state === "confirming" || state === "done"}>{state === "confirming" || state === "done" ? "Opening your appeal…" : "Open this appeal"}</button>
      {state === "unavailable" && <p role="alert">We couldn’t open your appeal. Try once more. If the link was already used, return to the device where you opened it.</p>}
    </>}
  </AccessPanel>;
}
