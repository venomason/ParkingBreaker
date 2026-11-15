"""Local dated DotGov snapshot I/O; parsing and freshness decisions remain pure."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from parkingbreaker.research.jurisdiction import DotGovSnapshot, parse_dotgov_csv


def load_dotgov_snapshot(path: Path) -> DotGovSnapshot:
    """Load dotgov-YYYY-MM-DD.csv as strict UTF-8, removing one leading BOM.

    Invalid names, calendar dates or UTF-8 raise ValueError. File I/O errors propagate.
    The hash identifies the decoded CSV text after BOM removal, preserving newlines.
    """
    match = re.fullmatch(r"dotgov-([0-9]{4}-[0-9]{2}-[0-9]{2})\.csv", path.name)
    if match is None:
        raise ValueError("DotGov snapshot name must be dotgov-YYYY-MM-DD.csv")
    snapshot_date = date.fromisoformat(match.group(1))
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("DotGov snapshot must be valid UTF-8") from exc
    return parse_dotgov_csv(text.removeprefix("\ufeff"), snapshot_date)
