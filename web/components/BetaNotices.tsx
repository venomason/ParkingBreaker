"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Image from "next/image";
import {
  ApiError,
  api,
  PHOTO_UPLOAD_GUIDANCE,
  type BetaFeedbackNoticeReceipt,
  type BetaFeedbackNoticeRequest,
} from "../lib/api";

const MAX_IMAGES = 20;
const MAX_IMAGE_BYTES = 10_000_000;
const LIST_PAGE_SIZE = 25;
const IMAGE_TYPES = new Set(["image/jpeg", "image/png"]);
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

type NoticeRole = "envelope" | "page";
type SelectedImage = {
  id: string;
  file: File;
  role: NoticeRole | null;
  ref: string | null;
};

function makeId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `notice-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function roleLabel(role: NoticeRole): string {
  return role === "envelope" ? "Envelope" : "Page";
}

function mergeReceipts(
  first: BetaFeedbackNoticeReceipt[],
  second: BetaFeedbackNoticeReceipt[]
): BetaFeedbackNoticeReceipt[] {
  const seen = new Set<string>();
  return [...first, ...second].filter((receipt) => {
    if (seen.has(receipt.notice_id)) return false;
    seen.add(receipt.notice_id);
    return true;
  });
}

/** A lost, timed-out or server-side answer is ambiguous: the request may have committed. */
function isAmbiguous(error: unknown): boolean {
  return (
    !(error instanceof ApiError) ||
    error.status === 0 ||
    error.status === 408 ||
    error.status === 429 ||
    error.status >= 500
  );
}

function noticeError(error: unknown, action: "list" | "save"): string {
  if (!(error instanceof ApiError) || error.status === 0) {
    return action === "list"
      ? "We couldn’t load the saved notices. Your appeal is unchanged; try again."
      : "We couldn’t reach the service. Your selected images and details stay on this page.";
  }
  if (action === "list") {
    if (error.detail === "invalid_beta_feedback_cursor") {
      return "The saved notice list changed. Reload the saved notices to see the current list.";
    }
    if (error.status === 401 || error.status === 404) {
      return "This appeal isn’t available with the current access. Reopen your private appeal link and try again.";
    }
    return "We couldn’t load the saved notices. Your appeal is unchanged; try again.";
  }
  const known: Record<string, string> = {
    beta_feedback_accepted_beta_required:
      "This appeal’s saved beta acceptance is required before a notice can be saved.",
    beta_feedback_photo_missing:
      "One of the selected images is no longer available to this appeal. Add it again.",
    beta_feedback_request_conflict:
      "A different notice was already saved with this request. Reload the saved notices before trying again.",
    invalid_request: "Check the dates, image roles and deadline text, then save again.",
    invalid_beta_feedback_date: "Use an exact YYYY-MM-DD date, or leave the date blank.",
  };
  return known[error.detail] ?? "We couldn’t save this notice. Your selected images and details stay on this page.";
}

function imageProblems(images: SelectedImage[]): string[] {
  const problems: string[] = [];
  if (images.length > MAX_IMAGES) {
    problems.push(
      `You selected ${images.length} images. A notice can have at most ${MAX_IMAGES} images. Remove ${
        images.length - MAX_IMAGES
      } or more to continue. Nothing was uploaded or saved.`
    );
  }
  const unsupported = images.filter((image) => !IMAGE_TYPES.has(image.file.type));
  if (unsupported.length) {
    problems.push(
      `These files are not JPEG or PNG images: ${unsupported
        .map((image) => image.file.name)
        .join(", ")}. Remove or replace them to continue. Nothing was uploaded or saved.`
    );
  }
  const tooLarge = images.filter((image) => image.file.size > MAX_IMAGE_BYTES);
  if (tooLarge.length) {
    problems.push(
      `These images are larger than 10 MB: ${tooLarge
        .map((image) => image.file.name)
        .join(", ")}. Choose smaller JPEG or PNG images to continue. Nothing was uploaded or saved.`
    );
  }
  if (images.some((image) => image.role === null)) {
    problems.push("Choose Envelope or Page for every selected image.");
  }
  return problems;
}

function SavedNotice({ receipt, caseId }: { receipt: BetaFeedbackNoticeReceipt; caseId: string }) {
  const received = new Date(receipt.received_at);
  const receivedText = Number.isNaN(received.getTime())
    ? "Saved by the server"
    : received.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
  const pages = [...receipt.pages].sort((a, b) => a.page_number - b.page_number);
  return (
    <li className="card" data-notice-id={receipt.notice_id} style={{ padding: "14px" }}>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start" }}>
        <div>
          <p className="eyebrow" style={{ marginBottom: "2px" }}>Saved notice</p>
          <h4 style={{ margin: 0 }}>Received by the service</h4>
          <p className="small muted" style={{ marginTop: "2px" }}>
            <time dateTime={receipt.received_at}>{receivedText}</time> · your local time
          </p>
        </div>
        <div className="row" style={{ gap: "4px" }}>
          <span className="bdg s-ok">Saved</span>
          <span className="bdg s-unk">Unreviewed</span>
          {receipt.needs_confirmation && <span className="bdg t-imp">Needs confirmation</span>}
        </div>
      </div>
      <dl className="small" style={{ margin: "12px 0", display: "grid", gap: "8px" }}>
        <div>
          <dt style={{ fontWeight: 750 }}>Date you reported seeing it</dt>
          <dd>{receipt.reported_fields.customer_seen_on ?? "Not provided"}</dd>
        </div>
        <div>
          <dt style={{ fontWeight: 750 }}>Date printed on the notice</dt>
          <dd>{receipt.reported_fields.printed_on ?? "Not provided"}</dd>
        </div>
        <div>
          <dt style={{ fontWeight: 750 }}>Deadline text you reported</dt>
          <dd style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
            {receipt.reported_fields.stated_deadline_text ?? "Not provided"}
          </dd>
        </div>
      </dl>
      <p className="small muted">
        Dates and deadline text are shown exactly as you reported them, without timezone conversion. They are
        not verified deadlines.
      </p>
      <ol className="stack" style={{ listStyle: "none", padding: 0, margin: "10px 0 0" }}>
        {pages.map((page) => {
          const url = api.noticePageUrl(caseId, receipt.notice_id, page.page_number);
          return (
            <li
              key={`${receipt.notice_id}-${page.page_number}`}
              className="row"
              style={{ alignItems: "center", gap: "10px", flexWrap: "nowrap" }}
            >
              <Image
                unoptimized
                width={84}
                height={84}
                src={url}
                alt={`${roleLabel(page.role)} image, page ${page.page_number} of ${pages.length}`}
                style={{ width: "84px", height: "84px", objectFit: "contain", flex: "none" }}
              />
              <div style={{ minWidth: 0 }}>
                <p className="small" style={{ margin: 0 }}>
                  <b>{roleLabel(page.role)}</b> · image {page.page_number} of {pages.length}
                </p>
                <a className="small" href={url} target="_blank" rel="noreferrer">
                  Open private image
                </a>
              </div>
            </li>
          );
        })}
      </ol>
      <p className="small muted">
        This record shows what you reported. It is not agency acceptance, does not confirm that a notice is
        complete, and does not extend any deadline.
      </p>
    </li>
  );
}

/**
 * Accepted-beta notice upload and receipts. This component can never grant or change
 * funding, admission, case, payment or deadline state; the API remains the authority.
 * It is rendered only for a real saved case whose server payment state is NO_CHARGE.
 */
export function BetaNotices({ caseId, workspaceId }: { caseId: string; workspaceId: string }) {
  const [open, setOpen] = useState(false);
  const [receipts, setReceipts] = useState<BetaFeedbackNoticeReceipt[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [listBusy, setListBusy] = useState<"first" | "more" | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  const [images, setImages] = useState<SelectedImage[]>([]);
  const [seenOn, setSeenOn] = useState("");
  const [printedOn, setPrintedOn] = useState("");
  const [deadlineText, setDeadlineText] = useState("");
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string[]>([]);
  const [serverError, setServerError] = useState<string | null>(null);
  const [uncertain, setUncertain] = useState(false);
  const [frozen, setFrozen] = useState<BetaFeedbackNoticeRequest | null>(null);
  const [savedReceipt, setSavedReceipt] = useState<BetaFeedbackNoticeReceipt | null>(null);

  const inputRef = useRef<HTMLInputElement>(null);
  const requestIdRef = useRef<string | null>(null);
  const listToken = useRef(0);
  const uploadInFlight = useRef(false);
  const alive = useRef(true);
  const currentCase = useRef(caseId);

  useEffect(() => {
    currentCase.current = caseId;
  }, [caseId]);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      listToken.current += 1;
    };
  }, []);

  const loadPage = useCallback(
    async (cursor: string | null, append: boolean) => {
      const token = ++listToken.current;
      setListBusy(append ? "more" : "first");
      setListError(null);
      try {
        const page = await api.notices(caseId, LIST_PAGE_SIZE, cursor);
        if (!alive.current || token !== listToken.current || currentCase.current !== caseId) return;
        // Notices are immutable. Keep known saves even when the first page does
        // not yet contain a new receipt, or when more pages were already loaded.
        setReceipts((previous) =>
          append ? mergeReceipts(previous, page.notices) : mergeReceipts(page.notices, previous)
        );
        setNextCursor(page.next_cursor);
        setLoaded(true);
      } catch (error) {
        if (!alive.current || token !== listToken.current || currentCase.current !== caseId) return;
        setListError(noticeError(error, "list"));
      } finally {
        if (alive.current && token === listToken.current) setListBusy(null);
      }
    },
    [caseId]
  );

  // Every open reads the owned server list. A reload therefore shows what the
  // server actually saved, never a client-side claim that nothing was kept.
  useEffect(() => {
    if (!open) return;
    const timer = setTimeout(() => { void loadPage(null, false); }, 0);
    return () => clearTimeout(timer);
  }, [open, loadPage]);

  const busy = saving;
  const problems = [...imageProblems(images)];
  if (deadlineText.length > 1000) {
    problems.push(
      `The deadline text is ${deadlineText.length} characters. Keep it to 1000 characters or fewer. Nothing was uploaded or saved.`
    );
  }
  if (
    (seenOn && !ISO_DATE.test(seenOn)) ||
    (printedOn && !ISO_DATE.test(printedOn))
  ) {
    problems.push("Use an exact YYYY-MM-DD date, or leave the date blank.");
  }

  function addFiles(files: FileList | null) {
    if (!files || busy || uncertain) return;
    // Every selected file is retained, even when it is unsupported, oversized or
    // pushes the count over the limit; validation then blocks upload and save.
    const additions: SelectedImage[] = Array.from(files).map((file) => ({
      id: makeId(),
      file,
      role: null,
      ref: null,
    }));
    setImages((previous) => [...previous, ...additions]);
    setSavedReceipt(null);
    setFormError([]);
    setServerError(null);
  }

  function moveImage(id: string, direction: -1 | 1) {
    setFormError([]);
    setImages((previous) => {
      const index = previous.findIndex((image) => image.id === id);
      const target = index + direction;
      if (index < 0 || target < 0 || target >= previous.length) return previous;
      const next = [...previous];
      [next[index], next[target]] = [next[target], next[index]];
      return next;
    });
  }

  function removeImage(id: string) {
    setImages((previous) => previous.filter((image) => image.id !== id));
    setFormError([]);
    setSavedReceipt(null);
  }

  function clearImages() {
    setImages([]);
    setFormError([]);
    setServerError(null);
    setSavedReceipt(null);
    if (inputRef.current) inputRef.current.value = "";
  }

  const complete = useCallback(
    (receipt: BetaFeedbackNoticeReceipt) => {
      setReceipts((previous) => mergeReceipts([receipt], previous));
      setLoaded(true);
      // Starting a fresh read increments listToken synchronously, invalidating
      // every older response before it can overwrite this confirmed save.
      void loadPage(null, false);
      setImages([]);
      setSeenOn("");
      setPrintedOn("");
      setDeadlineText("");
      setFormError([]);
      setServerError(null);
      setUncertain(false);
      setFrozen(null);
      requestIdRef.current = null;
      setSavedReceipt(receipt);
      if (inputRef.current) inputRef.current.value = "";
    },
    [loadPage]
  );

  async function retryFrozen() {
    const payload = frozen;
    if (!payload || busy) return;
    setSaving(true);
    setServerError(null);
    try {
      const receipt = await api.recordNotice(caseId, payload);
      if (!alive.current || currentCase.current !== caseId) return;
      if (receipt.case_id !== caseId || receipt.request_id !== payload.request_id) {
        setServerError(
          "The service answered with a different appeal or request. Nothing on this page was cleared. Check the saved notices."
        );
        return;
      }
      complete(receipt);
    } catch (error) {
      if (!alive.current || currentCase.current !== caseId) return;
      setServerError(
        isAmbiguous(error)
          ? "The retry answer was also lost. The same request and exact details are still held here; retry again or check the saved notices."
          : noticeError(error, "save")
      );
    } finally {
      if (alive.current && currentCase.current === caseId) setSaving(false);
    }
  }

  async function saveNotice() {
    if (busy || uploadInFlight.current || uncertain) return;
    const currentProblems = imageProblems(images);
    if (!images.length) currentProblems.push("Add at least one JPEG or PNG image of the notice.");
    if (deadlineText.length > 1000) {
      currentProblems.push(
        `The deadline text is ${deadlineText.length} characters. Keep it to 1000 characters or fewer.`
      );
    }
    if ((seenOn && !ISO_DATE.test(seenOn)) || (printedOn && !ISO_DATE.test(printedOn))) {
      currentProblems.push("Use an exact YYYY-MM-DD date, or leave the date blank.");
    }
    if (currentProblems.length) {
      setFormError(currentProblems);
      setServerError(null);
      return;
    }

    uploadInFlight.current = true;
    setSaving(true);
    setFormError([]);
    setServerError(null);
    setSavedReceipt(null);
    let metadataSent = false;
    try {
      let ordered = images;
      // Only this explicit Save uploads. Each successful private ref is cached in
      // place, so a later retry resumes without losing files, order or details.
      for (const entry of ordered) {
        if (entry.ref) continue;
        const uploaded = await api.uploadPhoto(workspaceId, entry.file);
        if (!alive.current || currentCase.current !== caseId) return;
        ordered = ordered.map((image) =>
          image.id === entry.id ? { ...image, ref: uploaded.photo_ref } : image
        );
        setImages(ordered);
      }

      const payload: BetaFeedbackNoticeRequest = {
        request_id: requestIdRef.current ?? (requestIdRef.current = makeId()),
        pages: ordered.map((image) => ({
          role: image.role as NoticeRole,
          photo_ref: image.ref as string,
        })),
        customer_seen_on: seenOn || null,
        printed_on: printedOn || null,
        stated_deadline_text: deadlineText || null,
      };
      // Freeze the exact key and body before the metadata request can be answered.
      setFrozen(payload);
      metadataSent = true;
      const receipt = await api.recordNotice(caseId, payload);
      if (!alive.current || currentCase.current !== caseId) return;
      if (receipt.case_id !== caseId || receipt.request_id !== payload.request_id) {
        setUncertain(true);
        setServerError(
          "The service answered with a different appeal or request, so this save is not confirmed. Nothing on this page was cleared. Retry the same notice or check the saved notices."
        );
        return;
      }
      complete(receipt);
    } catch (error) {
      if (!alive.current || currentCase.current !== caseId) return;
      if (metadataSent && isAmbiguous(error)) {
        setUncertain(true);
        setServerError(
          "The save answer was lost, so it may already be saved. Your exact images, order, dates and request are held here. Retry the same notice; it will not create a new request."
        );
      } else {
        setServerError(noticeError(error, "save"));
      }
    } finally {
      uploadInFlight.current = false;
      if (alive.current && currentCase.current === caseId) setSaving(false);
    }
  }

  return (
    <section className="card" id="betaNoticesCard" aria-labelledby="betaNoticesHeading">
      <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start" }}>
        <div style={{ minWidth: 0 }}>
          <p className="eyebrow" style={{ marginBottom: "2px" }}>Accepted beta</p>
          <h3 id="betaNoticesHeading" style={{ margin: 0 }}>Share an agency notice</h3>
          <p className="small muted" style={{ marginTop: "4px", marginBottom: 0 }}>
            Optionally save photos of a notice you received. The service stores exactly what you report as
            unreviewed. This is not agency acceptance or a verified deadline, and uploading a notice does not
            extend any deadline.
          </p>
        </div>
        <button
          className="btn btn-ghost btn-sm"
          id="betaNoticesToggle"
          type="button"
          aria-expanded={open}
          aria-controls="betaNoticesBody"
          onClick={() => setOpen((value) => !value)}
        >
          {open ? "Hide notice sharing" : "Share an agency notice"}
        </button>
      </div>

      {open && (
        <div className="stack" id="betaNoticesBody" style={{ marginTop: "14px" }}>
          <div className="stack" id="betaNoticesSaved">
            <div className="row" style={{ justifyContent: "space-between" }}>
              <h4 style={{ margin: 0 }}>Saved notices</h4>
              <span className="small muted">{loaded ? `${receipts.length} shown` : ""}</span>
            </div>
            {listBusy === "first" && (
              <p className="small" role="status" id="betaNoticesLoading">Loading saved notices…</p>
            )}
            {listError && (
              <div className="err" id="betaNoticesError" role="alert">
                <p>{listError}</p>
                <button
                  className="btn btn-ghost btn-sm"
                  type="button"
                  onClick={() => { void loadPage(null, false); }}
                >
                  Try loading saved notices again
                </button>
              </div>
            )}
            {!listBusy && !listError && loaded && receipts.length === 0 && (
              <p className="small muted" id="betaNoticesEmpty">
                No notices are saved for this appeal yet.
              </p>
            )}
            {receipts.length > 0 && (
              <ul id="betaNoticeReceipts" className="stack" style={{ listStyle: "none", padding: 0, margin: 0 }}>
                {receipts.map((receipt) => (
                  <SavedNotice key={receipt.notice_id} receipt={receipt} caseId={caseId} />
                ))}
              </ul>
            )}
            {nextCursor && (
              <button
                className="btn btn-ghost"
                id="betaNoticesLoadMore"
                type="button"
                disabled={listBusy !== null}
                onClick={() => { void loadPage(nextCursor, true); }}
              >
                {listBusy === "more" ? "Loading more…" : "Load more saved notices"}
              </button>
            )}
          </div>

          <div className="stack" id="betaNoticeForm">
            <div>
              <label htmlFor="betaNoticeFiles" style={{ fontWeight: 750, display: "block", marginBottom: "4px" }}>
                Notice photos (JPEG or PNG)
              </label>
              <input
                ref={inputRef}
                id="betaNoticeFiles"
                type="file"
                accept="image/jpeg,image/png"
                multiple
                disabled={busy || uncertain}
                aria-describedby="betaNoticeFileHelp"
                onChange={(event) => { addFiles(event.target.files); }}
              />
              <p className="small muted" id="betaNoticeFileHelp">
                {PHOTO_UPLOAD_GUIDANCE}{" "}
                Up to 20 images, each 10 MB or less. Choose Envelope or Page for every image after adding it.
                Images are uploaded only when you press Save notice.
              </p>
            </div>

            {images.length > 0 && (
              <>
                <ol id="betaNoticeFilesList" className="stack" style={{ listStyle: "none", padding: 0, margin: 0 }}>
                  {images.map((image, index) => (
                    <li
                      key={image.id}
                      data-selected-id={image.id}
                      className="card"
                      style={{ padding: "12px" }}
                    >
                      <p className="small" style={{ marginTop: 0 }}>
                        <b>Image {index + 1}</b> · {image.file.name} ·{" "}
                        {(image.file.size / (1024 * 1024)).toFixed(1)} MB
                        {image.ref ? " · uploaded" : " · not uploaded yet"}
                      </p>
                      <label style={{ display: "block", fontWeight: 700 }}>
                        Role for image {index + 1}
                        <select
                          aria-label={`Role for image ${index + 1}`}
                          value={image.role ?? ""}
                          disabled={busy || uncertain}
                          onChange={(event) => {
                            setFormError([]);
                            setImages((previous) =>
                              previous.map((current) =>
                                current.id === image.id
                                  ? { ...current, role: (event.target.value || null) as NoticeRole | null }
                                  : current
                              )
                            );
                          }}
                        >
                          <option value="">Choose Envelope or Page</option>
                          <option value="envelope">Envelope</option>
                          <option value="page">Page</option>
                        </select>
                      </label>
                      <div className="row" style={{ marginTop: "8px" }}>
                        <button
                          className="btn btn-ghost btn-sm"
                          type="button"
                          aria-label={`Move image ${index + 1} up`}
                          disabled={busy || uncertain || index === 0}
                          onClick={() => moveImage(image.id, -1)}
                        >
                          Up
                        </button>
                        <button
                          className="btn btn-ghost btn-sm"
                          type="button"
                          aria-label={`Move image ${index + 1} down`}
                          disabled={busy || uncertain || index === images.length - 1}
                          onClick={() => moveImage(image.id, 1)}
                        >
                          Down
                        </button>
                        <button
                          className="btn btn-ghost btn-sm"
                          type="button"
                          aria-label={`Remove image ${index + 1}`}
                          disabled={busy || uncertain}
                          onClick={() => removeImage(image.id)}
                        >
                          Remove
                        </button>
                      </div>
                    </li>
                  ))}
                </ol>
                <button
                  className="btn btn-ghost btn-sm"
                  id="betaNoticeRemoveAll"
                  type="button"
                  disabled={busy || uncertain}
                  onClick={clearImages}
                >
                  Remove all selected images
                </button>
              </>
            )}

            <div className="local-ids">
              <label>
                Date seen on the notice (optional)
                <input
                  id="betaNoticeSeenOn"
                  type="date"
                  value={seenOn}
                  disabled={busy || uncertain}
                  onChange={(event) => { setFormError([]); setSeenOn(event.target.value); }}
                />
              </label>
              <label>
                Date printed on the notice (optional)
                <input
                  id="betaNoticePrintedOn"
                  type="date"
                  value={printedOn}
                  disabled={busy || uncertain}
                  onChange={(event) => { setFormError([]); setPrintedOn(event.target.value); }}
                />
              </label>
            </div>

            <div>
              <label htmlFor="betaNoticeDeadline" style={{ fontWeight: 750, display: "block", marginBottom: "4px" }}>
                Deadline text exactly as printed (optional)
              </label>
              <textarea
                id="betaNoticeDeadline"
                value={deadlineText}
                disabled={busy || uncertain}
                aria-describedby="betaNoticeDeadlineHelp"
                onChange={(event) => { setFormError([]); setDeadlineText(event.target.value); }}
                style={{ width: "100%", minHeight: "90px", font: "inherit", padding: "10px" }}
              />
              <p className="small muted" id="betaNoticeDeadlineHelp">
                {deadlineText.length}/1000 characters. The text is stored literally and is not a verified
                deadline. Dates use exact YYYY-MM-DD and are shown without timezone conversion.
              </p>
            </div>

            {formError.length > 0 && (
              <div className="err" id="betaNoticeFormError" role="alert">
                <ul style={{ margin: 0, paddingLeft: "18px" }}>
                  {formError.map((problem) => (
                    <li key={problem}>{problem}</li>
                  ))}
                </ul>
              </div>
            )}
            {problems.length > 0 && (
              <div className="err" id="betaNoticeProblems" role="status">
                <ul style={{ margin: 0, paddingLeft: "18px" }}>
                  {problems.map((problem) => (
                    <li key={problem}>{problem}</li>
                  ))}
                </ul>
              </div>
            )}
            {serverError && (
              <p className="err" id="betaNoticeServerError" role="alert">{serverError}</p>
            )}
            {uncertain && (
              <div className="guard" id="betaNoticeUncertain" role="status">
                <div>
                  <b>The save answer was lost.</b>
                  <p>
                    It may already be saved. This page holds the same request, images and details. Retrying
                    uses that exact request and cannot create a second notice.
                  </p>
                  <div className="row">
                    <button
                      className="btn btn-primary btn-sm"
                      id="betaNoticeRetry"
                      type="button"
                      disabled={busy || !frozen}
                      onClick={() => { void retryFrozen(); }}
                    >
                      {busy ? "Retrying…" : "Retry same notice"}
                    </button>
                    <button
                      className="btn btn-ghost btn-sm"
                      type="button"
                      disabled={busy}
                      onClick={() => { void loadPage(null, false); }}
                    >
                      Check saved notices
                    </button>
                  </div>
                </div>
              </div>
            )}
            {savedReceipt && (
              <div className="guard" id="betaNoticeSaved" role="status">
                <div>
                  <b>Notice saved as unreviewed.</b>
                  <p>
                    The service confirmed this appeal and this request. It has not accepted or verified the
                    notice, and saving it does not extend any deadline.
                  </p>
                </div>
              </div>
            )}

            <button
              className="btn btn-primary"
              id="betaNoticeSave"
              type="button"
              disabled={busy || uncertain}
              onClick={() => { void saveNotice(); }}
            >
              {busy ? "Saving notice…" : "Save notice"}
            </button>
            <p className="small muted" style={{ margin: 0 }}>
              Nothing is uploaded until you press Save notice. A saved notice stays unreviewed and is not a
              promise of agency acceptance, completeness or a review time.
            </p>
          </div>
        </div>
      )}
    </section>
  );
}
