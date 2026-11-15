from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

import pytest

from parkingbreaker.dotgov_source import load_dotgov_snapshot
from parkingbreaker.research.jurisdiction import parse_dotgov_csv

CSV_TEXT = (
    "Domain name,Domain type,Organization name,Suborganization name,City,State,"
    "Security contact email\r\n"
    "orange.gov,City,City of Orange,,Orange,CA,\r\n"
)


@pytest.mark.parametrize("bom", ["", "\ufeff"])
def test_load_dated_snapshot_preserves_text_and_hash(tmp_path: Path, bom: str) -> None:
    path = tmp_path / "dotgov-2026-10-13.csv"
    path.write_bytes((bom + CSV_TEXT).encode("utf-8"))
    result = load_dotgov_snapshot(path)
    assert result == parse_dotgov_csv(CSV_TEXT, date(2026, 10, 13))
    assert result.content_sha256 == hashlib.sha256(CSV_TEXT.encode("utf-8")).hexdigest()


@pytest.mark.parametrize(
    "name",
    [
        "snapshot-2026-10-13.csv",
        "dotgov-2026-1-13.csv",
        "dotgov-20261013.csv",
        "dotgov-2026-10-13.CSV",
        "dotgov-2026-10-13.csv.bak",
        "dotgov-2026-10-13.csv\n",
        "dotgov-2026-02-30.csv",
        "dotgov-0000-10-13.csv",
    ],
)
def test_invalid_filename_or_calendar_date_is_rejected(tmp_path: Path, name: str) -> None:
    # Name validation must happen before attempting to read the file.
    with pytest.raises(ValueError, match=r"DotGov snapshot name|day is out of range|year 0"):
        load_dotgov_snapshot(tmp_path / name)


def test_invalid_utf8_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "dotgov-2026-10-13.csv"
    path.write_bytes(CSV_TEXT.encode("utf-8") + b"\xff")
    with pytest.raises(ValueError, match="UTF-8"):
        load_dotgov_snapshot(path)


def test_invalid_csv_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "dotgov-2026-10-13.csv"
    path.write_bytes(b"Wrong header\n")
    with pytest.raises(ValueError, match="DotGov header"):
        load_dotgov_snapshot(path)
