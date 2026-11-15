"""Bounded I/O adapter for untrusted official PDFs; one isolated parser process."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from collections.abc import Sequence

from parkingbreaker.research.pdf_text import MAX_PDF_BYTES, MAX_PDF_TEXT_CHARS

PDF_TIMEOUT_S = 8.0
PDF_CPU_S = 6
PDF_MEMORY_BYTES = 512 * 1024 * 1024
MAX_TEXT_BYTES = MAX_PDF_TEXT_CHARS * 4


def _run_reader(body: bytes, command: Sequence[str], timeout: float) -> str | None:
    try:
        # A regular stdout file is also covered by the child's RLIMIT_FSIZE;
        # communicate never accumulates unlimited child output in parent memory.
        with tempfile.TemporaryFile() as output:
            result = subprocess.run(
                command,
                input=body,
                stdout=output,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
                cwd="/",
                env={"LANG": "C.UTF-8"},
            )
            if result.returncode != 0:
                return None
            output.seek(0)
            data = output.read(MAX_TEXT_BYTES + 1)
            if len(data) > MAX_TEXT_BYTES:
                return None
            return data.decode("utf-8") or None
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        return None


def bounded_pdf_text(body: bytes) -> str | None:
    if len(body) > MAX_PDF_BYTES:
        return None
    return _run_reader(
        body, (sys.executable, "-m", "parkingbreaker.pdf_text_reader"), PDF_TIMEOUT_S
    )


def _main() -> None:
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (PDF_CPU_S, PDF_CPU_S))
    resource.setrlimit(resource.RLIMIT_AS, (PDF_MEMORY_BYTES, PDF_MEMORY_BYTES))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_TEXT_BYTES, MAX_TEXT_BYTES))
    from parkingbreaker.research.pdf_text import extract_pdf_text

    body = sys.stdin.buffer.read(MAX_PDF_BYTES + 1)
    text = extract_pdf_text(body)
    if text is not None:
        sys.stdout.buffer.write(text.encode("utf-8"))


if __name__ == "__main__":
    _main()
