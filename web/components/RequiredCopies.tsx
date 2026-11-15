"use client";
import React, { useEffect, useRef, useState } from "react";
import { api, ApiError, PHOTO_UPLOAD_GUIDANCE, type CaseStatus, type SaveRequiredCopies } from "../lib/api";
type View = NonNullable<CaseStatus["required_copies"]>;
type Page = { ref: string; url: string };
type Selection = { pages: Page[]; confirmed: boolean };
interface Props { caseId: string; status: CaseStatus; onStatus: (status: CaseStatus) => void; onPendingChange: (pending: boolean) => void }
export function RequiredCopies(props: Props) {
  const view = props.status.required_copies;
  const [notice, setNotice] = useState<{ caseId: string; text: string } | null>(null);
  return <>{notice?.caseId === props.caseId && <p role="status" className="small">{notice.text}</p>}{view?.items.length ? <Editor key={`${props.caseId}:${view.research_sha256}:${view.revision}`} {...props} view={view} onReconcile={text => setNotice({ caseId: props.caseId, text })} /> : null}</>;
}
function Editor({ caseId, status, onStatus, onPendingChange, view, onReconcile }: Props & { view: View; onReconcile: (text: string) => void }) {
  const [items, setItems] = useState<Record<string, Selection>>(() => Object.fromEntries(view.items.map(item => [item.requirement_id, { pages: item.page_refs.map((ref, i) => ({ ref, url: api.requiredCopyPageUrl(caseId, item.requirement_id, i + 1) })), confirmed: !!item.page_refs.length }])));
  const [dirty, setDirty] = useState(false);
  const [failedImages, setFailedImages] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [uncertain, setUncertain] = useState<SaveRequiredCopies | null>(null);
  const alive = useRef(true), operating = useRef(false);
  const previews = useRef<string[]>([]);
  const current = useRef(view);
  useEffect(() => { current.current = view; }, [view]);
  const editable = view.editable && !busy && !uncertain;
  useEffect(() => { alive.current = true; const urls = previews.current; return () => { alive.current = false; urls.forEach(url => URL.revokeObjectURL(url)); }; }, []);
  useEffect(() => { onPendingChange(dirty || busy || !!error || !!uncertain || failedImages.length > 0); }, [dirty, busy, error, uncertain, failedImages, onPendingChange]);
  function change(id: string, pages: Page[], confirmed = false) {
    setItems(previous => ({ ...previous, [id]: { pages, confirmed } })); setFailedImages(previous => previous.filter(url => Object.entries(items).some(([key, value]) => (key === id ? pages : value.pages).some(page => page.url === url)))); setDirty(true); setError(null);
  }
  async function upload(id: string, files: File[], replace: boolean) {
    if (!editable || operating.current || !files.length) return;
    const pages: Page[] = replace ? [] : [...items[id].pages];
    const total = Object.values(items).reduce((sum, item) => sum + item.pages.length, 0) - items[id].pages.length;
    if (pages.length + files.length > 10 || total + pages.length + files.length > 20 || files.some(f => !["image/jpeg", "image/png"].includes(f.type) || f.size > 10_000_000 || !f.size)) { setError("Choose JPEG or PNG images up to 10 MB each, 10 pages per copy and 20 pages total."); return; }
    operating.current = true; setBusy(true); setError(null); setDirty(true);
    setItems(previous => ({ ...previous, [id]: { ...previous[id], confirmed: false } }));
    try {
      for (const [index, file] of files.entries()) {
        const uploaded = await api.uploadPhoto(status.workspace_id, file);
        if (!alive.current || !current.current.editable) return;
        const url = URL.createObjectURL(file); previews.current.push(url);
        setItems(previous => ({ ...previous, [id]: { pages: [...(replace && index === 0 ? [] : previous[id].pages), { ref: uploaded.photo_ref, url }], confirmed: false } }));
      }
    } catch { if (alive.current) setError("A page could not be uploaded. Pages shown are retained. Select the missing pages again."); }
    finally { operating.current = false; if (alive.current) setBusy(false); }
  }
  function needsReconciliation(latest: CaseStatus, payload: SaveRequiredCopies) {
    const inventory = latest.required_copies;
    return !inventory || !inventory.editable || inventory.research_sha256 !== payload.research_sha256 || inventory.revision !== payload.expected_revision;
  }
  function reconcile(latest: CaseStatus, payload: SaveRequiredCopies) {
    const inventory = latest.required_copies;
    const matches = inventory?.research_sha256 === payload.research_sha256 && inventory.items.every(item => {
      const selection = payload.selections.find(saved => saved.requirement_id === item.requirement_id);
      return JSON.stringify(item.page_refs) === JSON.stringify(selection?.page_refs ?? []);
    }) && payload.selections.every(selection => inventory.items.some(item => item.requirement_id === selection.requirement_id));
    setItems(Object.fromEntries((inventory?.items ?? []).map(item => [item.requirement_id, {
      pages: item.page_refs.map((ref, i) => ({ ref, url: api.requiredCopyPageUrl(caseId, item.requirement_id, i + 1) })), confirmed: !!item.page_refs.length,
    }])));
    setDirty(false); setUncertain(null); setError(null); setFailedImages([]); setBusy(false); onPendingChange(false);
    onReconcile(matches ? "The saved copies have been recovered." : "The saved copies changed or were fixed when drafting began. Your local changes were not saved. Review the current saved copies.");
    onStatus(latest);
  }
  async function save() {
    if (operating.current || !view.editable) return;
    const payload: SaveRequiredCopies = uncertain ?? { research_sha256: view.research_sha256, expected_revision: view.revision, selections: view.items.filter(i => items[i.requirement_id].pages.length).map(i => ({ requirement_id: i.requirement_id, page_refs: items[i.requirement_id].pages.map(p => p.ref), confirmed: true })) };
    operating.current = true; setBusy(true); setError(null);
    try {
      if (uncertain) {
        const latest = await api.status(caseId);
        if (!alive.current || !current.current.editable) return;
        if (needsReconciliation(latest, payload)) { reconcile(latest, payload); return; }
      }
      const next = await api.saveRequiredCopies(caseId, payload);
      if (!alive.current || !current.current.editable || current.current.revision !== payload.expected_revision) return;
      setDirty(false); setUncertain(null); onPendingChange(false); onStatus(next);
    } catch (failure) {
      if (!alive.current) return;
      if (failure instanceof ApiError && failure.status === 409) {
        try {
          const latest = await api.status(caseId);
          if (!alive.current) return;
          if (needsReconciliation(latest, payload)) { reconcile(latest, payload); return; }
          setUncertain(null); setError("The copies could not be saved. Review your selections and try again.");
        } catch {
          if (alive.current) { setUncertain(payload); setError("We couldn’t refresh the saved copies. Check saved copies and retry before drafting."); }
        }
      } else if (failure instanceof ApiError && failure.status > 0 && failure.status < 500) {
        setUncertain(null); setError("The copies could not be saved. Check your selections and try again.");
      } else {
        setUncertain(payload); setError("We couldn’t confirm the save. Check saved copies and retry before drafting.");
      }
    }
    finally { operating.current = false; if (alive.current) setBusy(false); }
  }
  const unconfirmed = Object.values(items).some(item => item.pages.length && !item.confirmed);
  return <div className="card" id="requiredCopies"><h3>Required document copies</h3>
    <p className="small">Review these copies before drafting. They are fixed when drafting begins.</p>
    <p className="small">Upload complete copies in page order: JPEG or PNG, 10 MB per page, 10 pages per copy and 20 pages total. Your confirmation records your own review.</p>
    <p className="small">{PHOTO_UPLOAD_GUIDANCE}</p>
    {view.items.map(item => { const selection = items[item.requirement_id]; return <fieldset key={item.requirement_id} disabled={!editable} style={{ marginTop: 16 }}><legend>{item.title}</legend>
      <blockquote style={{ whiteSpace: "pre-wrap" }}>{item.official_quote}</blockquote>
      <ul className="small">{item.source_urls.map(url => <li key={url}>{/^https?:\/\//.test(url) ? <a href={url} target="_blank" rel="noopener noreferrer">{url}</a> : url}</li>)}</ul>
      {!selection.pages.length && <p>No pages saved for this required copy.</p>}
      <ol>{selection.pages.map((page, index) => <li key={`${page.ref}:${index}`}>
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src={page.url} alt={`${item.title}, page ${index + 1}`} width={150} onError={() => setFailedImages(previous => previous.includes(page.url) ? previous : [...previous, page.url])} onLoad={() => setFailedImages(previous => previous.filter(url => url !== page.url))} />
        {view.editable && <div>{[-1, 1].map(direction => <button type="button" className="btn btn-ghost" key={direction} disabled={!editable || index + direction < 0 || index + direction >= selection.pages.length} aria-label={`Move ${item.title} page ${index + 1} ${direction < 0 ? "up" : "down"}`} onClick={() => { const pages = [...selection.pages]; [pages[index], pages[index + direction]] = [pages[index + direction], pages[index]]; change(item.requirement_id, pages); }}>{direction < 0 ? "Move up" : "Move down"}</button>)}<button type="button" className="btn btn-ghost" onClick={() => change(item.requirement_id, selection.pages.filter((_, i) => i !== index))}>Remove page {index + 1}</button></div>}
      </li>)}</ol>
      {view.editable && <>{[false, true].map(replace => <label key={String(replace)}>{replace ? "Replace all pages of" : "Add pages to"} {item.title}<input type="file" accept="image/jpeg,image/png" multiple onChange={event => { const files = Array.from(event.target.files ?? []); event.target.value = ""; void upload(item.requirement_id, files, replace); }} /></label>)}<label><input type="checkbox" checked={selection.confirmed} disabled={!editable || !selection.pages.length} onChange={e => change(item.requirement_id, selection.pages, e.target.checked)} /> I checked every page. This is the complete, readable {item.title}.</label></>}
    </fieldset>; })}
    {failedImages.length > 0 && <p className="err" role="alert">A copy page could not be displayed. Check saved copies before drafting.</p>}
    {error && <p className="err" role="alert">{error}</p>}
    {view.editable ? <button className="btn btn-primary" type="button" disabled={busy || (!uncertain && (!dirty || unconfirmed))} onClick={() => void save()}>{busy ? "Saving copies…" : uncertain ? "Check saved copies and retry" : "Save required copies"}</button> : <p>These copies are fixed for this letter.</p>}
  </div>;
}
