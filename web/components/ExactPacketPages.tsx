"use client";

import { useEffect, useRef, useState } from "react";
import type { PDFDocumentLoadingTask, PDFDocumentProxy, RenderTask } from "pdfjs-dist";

export function ExactPacketPages({ url }: { url: string }) {
  const [pdf, setPdf] = useState<PDFDocumentProxy | null>(null);
  const [page, setPage] = useState(1);
  const [renderedPage, setRenderedPage] = useState(0);
  const [unavailable, setUnavailable] = useState(false);
  const canvas = useRef<HTMLCanvasElement>(null);
  const pageIsReady = renderedPage === page;

  useEffect(() => {
    let active = true;
    let loading: PDFDocumentLoadingTask | undefined;
    void (async () => {
      try {
        const { GlobalWorkerOptions, getDocument } = await import("pdfjs-dist/legacy/build/pdf.mjs");
        if (!active) return;
        GlobalWorkerOptions.workerSrc = new URL("pdfjs-dist/legacy/build/pdf.worker.min.mjs", import.meta.url).href;
        loading = getDocument({ url, useWasm: false });
        const document = await loading.promise;
        if (active) setPdf(document);
      } catch {
        if (active) setUnavailable(true);
      }
    })();
    return () => {
      active = false;
      void loading?.destroy().catch(() => undefined);
    };
  }, [url]);

  useEffect(() => {
    const target = canvas.current;
    if (!pdf || !target) return;
    let active = true;
    let render: RenderTask | undefined;
    void (async () => {
      try {
        const pdfPage = await pdf.getPage(page);
        if (!active) return;
        const width = target.parentElement?.clientWidth || 440;
        const pixels = Math.min(1600, width * Math.max(window.devicePixelRatio || 1, 2));
        const scale = pixels / pdfPage.getViewport({ scale: 1 }).width;
        const viewport = pdfPage.getViewport({ scale });
        target.width = Math.ceil(viewport.width);
        target.height = Math.ceil(viewport.height);
        render = pdfPage.render({ canvas: target, viewport });
        await render.promise;
        if (active) setRenderedPage(page);
      } catch {
        if (active) setUnavailable(true);
      }
    })();
    return () => {
      active = false;
      render?.cancel();
    };
  }, [pdf, page]);

  if (unavailable) {
    return (
      <p className="status-banner is-attention" role="status">
        Download the exact PDF above to review it. The in-page preview couldn’t open.
      </p>
    );
  }

  return (
    <div className="exact-packet-pages">
      <div className="packet-page-controls">
        <button
          type="button"
          className="chip"
          onClick={() => setPage(page - 1)}
          disabled={!pageIsReady || page === 1}
        >
          ← Previous page
        </button>
        <span className="packet-page-indicator mono" role="status">
          {pdf ? `Page ${page} of ${pdf.numPages}` : "Loading pages…"}
        </span>
        <button
          type="button"
          className="chip"
          onClick={() => setPage(page + 1)}
          disabled={!pageIsReady || !pdf || page === pdf.numPages}
        >
          Next page →
        </button>
      </div>
      <div className="packet-page-mat" aria-busy={!pageIsReady}>
        <canvas
          ref={canvas}
          id="livePacketPage"
          hidden={!pageIsReady}
          role="img"
          aria-label={`Exact PDF, page ${page}`}
        />
      </div>
    </div>
  );
}
