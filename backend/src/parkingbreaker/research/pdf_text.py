"""Pure PDF text extraction; callers isolate untrusted parsing in the I/O adapter."""

from __future__ import annotations

import io

from pypdf import PdfReader

MAX_PDF_PAGES = 40
MAX_PDF_BYTES = 5_000_000
MAX_PDF_TEXT_CHARS = 250_000


def extract_pdf_text(body: bytes) -> str | None:
    if len(body) > MAX_PDF_BYTES:
        return None
    try:
        reader = PdfReader(io.BytesIO(body))
        if reader.is_encrypted or len(reader.pages) > MAX_PDF_PAGES:
            return None
        pages: list[str] = []
        size = 0
        for page in reader.pages:
            text = page.extract_text() or ""
            size += len(text) + 2
            if size > MAX_PDF_TEXT_CHARS:
                return None
            pages.append(text)
        return "\n\n".join(pages) or None
    except Exception:
        return None
