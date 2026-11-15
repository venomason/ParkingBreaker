"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import type { IntakeForm, WorkspaceView } from "./api";

/** One serialized writer to the existing private workspace; a conflict never overwrites it. */
export function useWorkspaceAutosave(form: IntakeForm, enabled: boolean, ready: boolean) {
  const initial = JSON.stringify(form);
  const latest = useRef(form);
  const saved = useRef(initial);
  const workspace = useRef<WorkspaceView | null>(null);
  const opening = useRef<Promise<WorkspaceView> | null>(null);
  const writing = useRef<Promise<void> | null>(null);
  const attempted = useRef<{ view: WorkspaceView; form: IntakeForm; signature: string } | null>(null);
  const [state, setState] = useState<"saved" | "saving" | "error">("saved");

  const restore = useCallback((view: WorkspaceView) => {
    workspace.current = view;
    if (view.intake_form) saved.current = JSON.stringify(view.intake_form);
  }, []);

  const reset = useCallback(() => {
    workspace.current = null;
    opening.current = null;
    attempted.current = null;
    saved.current = "";
    setState("saved");
  }, []);

  const ensureWorkspace = useCallback(async () => {
    if (workspace.current) return workspace.current;
    if (!opening.current) opening.current = api.createWorkspace("").then((view) => {
      workspace.current = view;
      return view;
    }).finally(() => { opening.current = null; });
    return opening.current;
  }, []);

  // Resolves with the server-acknowledged workspace (its revision and saved form), so a
  // caller can use the real current revision instead of a debounced React value.
  const flush = useCallback(async (snapshot?: IntakeForm): Promise<WorkspaceView | null> => {
    if (!enabled) return null;
    if (snapshot) latest.current = snapshot;
    while (writing.current) await writing.current;
    if (!attempted.current && JSON.stringify(latest.current) === saved.current) {
      return workspace.current ?? await ensureWorkspace();
    }
    const write = async (): Promise<WorkspaceView | null> => {
      setState("saving");
      try {
        while (attempted.current || JSON.stringify(latest.current) !== saved.current) {
          // Replay the exact uncertain write before saving newer typing. A committed
          // response may have been lost; the server's equality replay recovers its revision.
          if (!attempted.current) {
            const next = latest.current;
            attempted.current = {view: await ensureWorkspace(), form: next, signature: JSON.stringify(next)};
          }
          const {view, form: next, signature} = attempted.current;
          const updated = await api.saveIntakeForm(view.workspace_id, view.draft_revision ?? 1, next);
          workspace.current = updated;
          saved.current = signature;
          attempted.current = null;
        }
        setState("saved");
        return workspace.current;
      } catch (error) {
        setState("error");
        throw error;
      }
    };
    const pending = write();
    const gate = pending.then(() => undefined, () => undefined);
    writing.current = gate;
    try { return await pending; } finally { if (writing.current === gate) writing.current = null; }
  }, [enabled, ensureWorkspace]);

  useEffect(() => {
    latest.current = form;
    if (!enabled || !ready || JSON.stringify(form) === saved.current) return;
    const timer = setTimeout(() => { void flush().catch(() => {}); }, 250);
    return () => clearTimeout(timer);
  }, [form, enabled, ready, flush]);

  useEffect(() => {
    if (!enabled) return;
    const warn = (event: BeforeUnloadEvent) => {
      if (writing.current || JSON.stringify(latest.current) !== saved.current) {
        event.preventDefault(); event.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [enabled]);

  return { state, restore, reset, ensureWorkspace, flush };
}
