"""Isolated parsing retains legitimate PDFs and reaps a stalled parser process."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from parkingbreaker.pdf_text_reader import _run_reader, bounded_pdf_text
from parkingbreaker.research.pdf_text import MAX_PDF_BYTES, extract_pdf_text
from test_research_sources import make_pdf


def test_isolated_reader_matches_the_one_pure_parser() -> None:
    body = make_pdf("Mail requests to the citation review office.", "Include the citation number.")
    assert bounded_pdf_text(body) == extract_pdf_text(body)
    assert bounded_pdf_text(body) is not None


@pytest.mark.parametrize(
    "body",
    [b"%PDF-broken", b"x" * (MAX_PDF_BYTES + 1), make_pdf("")],
    ids=["malformed", "oversized", "blank-page"],
)
def test_unusable_pdf_returns_no_evidence(body: bytes) -> None:
    assert bounded_pdf_text(body) is None


def test_wall_deadline_kills_and_reaps_stalled_child(tmp_path: Path) -> None:
    marker = tmp_path / "synthetic-parser.pid"
    command = (
        sys.executable,
        "-c",
        "import os,pathlib,sys,time; "
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(10)",
        str(marker),
    )
    started = time.monotonic()
    assert _run_reader(b"", command, 0.3) is None
    assert time.monotonic() - started < 2
    pid = int(marker.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
