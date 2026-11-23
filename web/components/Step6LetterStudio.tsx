"use client";

import { HurtleGuide } from "./HurtleGuide";
import React, { useEffect } from "react";
import { Icon } from "./Icons";
import { SAMPLE_PREVIEW } from "../lib/api";

export type { SentenceItem } from "../lib/api";
import type { SentenceItem } from "../lib/api";

export type { NoteItem } from "../lib/api";

// Standard Helvetica advances in thousandths of an em, in ASCII order from space.
const HELVETICA_WIDTHS = [
  278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
  556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
  1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
  667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
  333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
  556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584,
];
// US Letter minus two 72pt margins and the PDF cell's two 2.835pt insets.
const LINE_WIDTH = (612 - 144 - 5.67) * 1000 / 11;
const TWO_PAGE_LINES = 2 * Math.floor((792 - 144) / 14);
const NOTICE_AT_LINES = TWO_PAGE_LINES - 12;

function estimatedPrintLines(text: string): number {
  let lines = 0;
  for (const paragraph of text.split(/\r\n?|\n/)) {
    lines++;
    let used = 0;
    for (const run of paragraph.match(/ +|[^ ]+/g) ?? []) {
      let width = 0;
      for (const char of run) width += HELVETICA_WIDTHS[char.charCodeAt(0) - 32] ?? 1000;
      if (used > 0 && used + width > LINE_WIDTH && run.trim()) {
        lines++;
        used = 0;
      }
      const overflow = Math.max(0, Math.ceil((used + width) / LINE_WIDTH) - 1);
      lines += overflow;
      used += width - overflow * LINE_WIDTH;
    }
  }
  return lines;
}

export function LetterFitWarning({ text, id }: { text: string; id: string }) {
  const lines = estimatedPrintLines(text);
  return (
    <div id={id} aria-live="polite">
      {lines >= NOTICE_AT_LINES && (
        <p className="small">
          {lines > TWO_PAGE_LINES ? "This letter may exceed two printed pages." : "This letter is close to the two-page print limit."}
          {" "}Shorten the text or remove extra blank lines if needed. The exact PDF checks the final fit.
        </p>
      )}
    </div>
  );
}

interface Step6LetterStudioProps {
  citationNumber: string;
  traceMode: boolean;
  setTraceMode: (value: boolean) => void;
  lapsed: boolean;
  onOpenNotes: () => void;
  onOpenSentenceModal: (sent: SentenceItem) => void;
  onOpenExactPdf: () => void;
  onRestartWindow: () => void;
  setActionBar: (sub: React.ReactNode, buttons: React.ReactNode) => void;
  sents: SentenceItem[];
  adopted: boolean;
  setAdopted: (adopted: boolean) => void;
  senderName: string;
  setSenderName: (name: string) => void;
  senderAddress: string;
  setSenderAddress: (addr: string) => void;
}

export function Step6LetterStudio({
  citationNumber,
  traceMode,
  setTraceMode,
  lapsed,
  onOpenNotes,
  onOpenSentenceModal,
  onOpenExactPdf,
  onRestartWindow,
  setActionBar,
  sents,
  adopted,
  setAdopted,
  senderName,
  setSenderName,
  senderAddress,
  setSenderAddress,
}: Step6LetterStudioProps) {
  const liveSents = sents.filter((s) => !s.removed);
  const flagged = liveSents.filter((s) => s.flag);

  useEffect(() => {
    if (lapsed) {
      setActionBar(
        "Your window closed · nothing was charged",
        <button
          className="btn btn-primary"
          type="button"
          id="actRestartBar"
          onClick={onRestartWindow}
        >
          Start a new 30-minute window
        </button>
      );
      return;
    }

    if (!adopted) {
      setActionBar(
        flagged.length ? "Resolve the proofreader note first" : "You can still change anything until you adopt",
        <button
          className="btn btn-primary"
          type="button"
          id="actAdoptBtn"
          disabled={flagged.length > 0}
          onClick={() => {
            setAdopted(true);
          }}
        >
          Adopt this exact text →
        </button>
      );
    } else {
      const ok = senderName.trim().length > 0 && senderAddress.trim().length > 0;
      setActionBar(
        "Text adopted · This exact wording will be printed",
        <button
          className="btn btn-primary"
          type="button"
          id="actPreviewPdf"
          disabled={!ok}
          onClick={onOpenExactPdf}
        >
          Preview the exact PDF →
        </button>
      );
    }
  }, [lapsed, adopted, flagged.length, senderName, senderAddress, onRestartWindow, onOpenExactPdf, setAdopted, setActionBar]);

  // Group sentences by paragraph (p: 0 header/greeting, p: 1 body, p: 2 closing)
  const p0 = liveSents.filter((s) => s.p === 0);
  const p1 = liveSents.filter((s) => s.p === 1);
  const p2 = liveSents.filter((s) => s.p === 2);

  return (
    <section className="screen on" id="s6" aria-labelledby="h-s6">
      <div className="voice">
        <HurtleGuide pose="letter" />
        <div>
          <p className="eyebrow">Step 5 · Your letter</p>
          <h2 id="h-s6" tabIndex={-1}>
            Your letter, in your words.
          </h2>
          <p className="lead">
            Tap any sentence to see where it came from or edit it. Nothing is mailed until you
            approve the exact PDF.
          </p>
        </div>
      </div>

      <div
        className={`flagbar ${flagged.length === 0 ? "clear" : ""}`}
        id="flagbar"
        role="status"
      >
        {flagged.length > 0 ? (
          <>
            <Icon id="dot" size={20} style={{ flex: "none" }} />
            <div>
              <b>Proofreader · 1 thing to check</b>
              <p>
                “{flagged[0].text}” This sentence adds a date you didn’t give. Edit or remove it.
              </p>
              <button
                className="chip"
                type="button"
                data-showflag={flagged[0].id}
                style={{ marginTop: "8px" }}
                onClick={() => onOpenSentenceModal(flagged[0])}
              >
                Show me
              </button>
            </div>
          </>
        ) : (
          <>
            <Icon id="check" size={20} style={{ flex: "none" }} />
            <div>
              <b>Proofreader · all clear</b>
              <p>Every sentence traces back to your notes or the official pages.</p>
            </div>
          </>
        )}
      </div>

      <div className="studio-bar">
        <div className="seg-pill" role="group" aria-label="Letter view mode">
          <button
            type="button"
            id="modeClean"
            aria-pressed={!traceMode}
            onClick={() => setTraceMode(false)}
          >
            Letter view
          </button>
          <button
            type="button"
            id="modeTrace"
            aria-pressed={traceMode}
            onClick={() => setTraceMode(true)}
          >
            Source tags
          </button>
        </div>
        <div className="row">
          <button className="chip" type="button" onClick={onOpenNotes}>
            Your notes
          </button>
          {adopted && (
            <button
              className="chip"
              type="button"
              id="unlockBtn"
              onClick={() => setAdopted(false)}
            >
              Unlock
            </button>
          )}
        </div>
      </div>

      {!lapsed && <LetterFitWarning id="demo-letter-fit" text={
        [p0, p1, p2].map(paragraph => paragraph.map(sentence => sentence.text).join(" ")).join("\n\n")
      } />}
      <div className="paper" id="paper">
        <p className="meta">
          {adopted ? (
            <>
              {senderName}
              <br />
              {senderAddress}
            </>
          ) : (
            "[Your name and address are added at the final PDF step]"
          )}
          <br />
          <br />
          To: {SAMPLE_PREVIEW.recipient.split("\n").slice(0, 2).join(", ")}
          <br />
          {SAMPLE_PREVIEW.recipient.split("\n").slice(2).join(", ")}
          <br />
          Re: Citation {citationNumber} · {SAMPLE_PREVIEW.letterDate}
        </p>

        {/* Fix 4: Real Letter Paragraph Flow in Letter View */}
        {!traceMode ? (
          <div className="paper-paras">
            {p0.length > 0 && (
              <p className="letter-p">
                {p0.map((s) => (
                  <button
                    key={s.id}
                    type="button"
                    className={`sent-inline ${s.flag ? "flag" : ""} ${s.edited ? "edited" : ""}`}
                    onClick={() => onOpenSentenceModal(s)}
                    aria-label={`${s.text}${s.flag ? " (flagged by proofreader)" : ""}`}
                  >
                    {s.text}
                  </button>
                ))}
              </p>
            )}
            {p1.length > 0 && (
              <p className="letter-p">
                {p1.map((s) => (
                  <button
                    key={s.id}
                    type="button"
                    className={`sent-inline ${s.flag ? "flag" : ""} ${s.edited ? "edited" : ""}`}
                    onClick={() => onOpenSentenceModal(s)}
                    aria-label={`${s.text}${s.flag ? " (flagged by proofreader)" : ""}`}
                  >
                    {s.text}{" "}
                  </button>
                ))}
              </p>
            )}
            {p2.length > 0 && (
              <p className="letter-p">
                {p2.map((s) => (
                  <button
                    key={s.id}
                    type="button"
                    className={`sent-inline ${s.flag ? "flag" : ""} ${s.edited ? "edited" : ""}`}
                    onClick={() => onOpenSentenceModal(s)}
                    aria-label={`${s.text}${s.flag ? " (flagged by proofreader)" : ""}`}
                  >
                    {s.text}
                  </button>
                ))}
              </p>
            )}
          </div>
        ) : (
          <div className="sent-list-trace" role="list">
            {liveSents.map((s) => (
              <button
                key={s.id}
                type="button"
                className={`sent-block ${s.flag ? "flag" : ""} ${s.edited ? "edited" : ""}`}
                onClick={() => onOpenSentenceModal(s)}
                aria-label={`${s.text}${s.flag ? " (flagged by proofreader)" : ""}`}
              >
                <span className="sent-tag">{s.edited ? "Edited + your note" : s.tag}</span>
                <span>{s.text}</span>
              </button>
            ))}
          </div>
        )}

        <p style={{ marginTop: "14px" }}>
          Sincerely,
          <br />
          {adopted ? senderName : "[Your name]"}
        </p>
      </div>

      {adopted && (
        <div className="card signbox" id="senderBox">
          <h3>Final sender details</h3>
          <p className="muted small" style={{ margin: "4px 0 10px" }}>
            Added locally after drafting. This sample does not show a verified signature
            requirement, so no signature is requested.
          </p>
          <div className="local-ids single">
            <label>
              Your name{" "}
              <input
                id="senderName"
                autoComplete="name"
                value={senderName}
                onChange={(e) => setSenderName(e.target.value)}
              />
            </label>
            <label>
              Your return address{" "}
              <input
                id="senderAddress"
                autoComplete="street-address"
                value={senderAddress}
                onChange={(e) => setSenderAddress(e.target.value)}
              />
            </label>
          </div>
        </div>
      )}
    </section>
  );
}
