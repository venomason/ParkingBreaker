"""Explicitly scoped evidence adjudication, with file and CLI I/O at the edge."""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from parkingbreaker.agencies import SourceScope
from parkingbreaker.http_fetcher import HttpFetcher
from parkingbreaker.research.findings import verify_quote
from parkingbreaker.research.sources import (
    AuthorityClass,
    Clock,
    Fetcher,
    FetchFailure,
    SourceRecord,
    fetch_source,
)


@dataclass(frozen=True)
class EvidenceRow:
    url: str
    final_url: str
    retrieved_at: str
    sha256: str
    quote: str
    supports: tuple[str, ...]


@dataclass(frozen=True)
class AdjudicationRefused:
    reason: str


def _evidence_row(
    record: SourceRecord, quote: str, supports: Iterable[str]
) -> EvidenceRow | AdjudicationRefused:
    """Pure decision over the exact text that save_page persists."""
    if not verify_quote(quote, record.text):
        return AdjudicationRefused("quote not verified on the page")
    if record.retrieved_at.utcoffset() is None:
        return AdjudicationRefused("retrieved_at must be timezone-aware")
    return EvidenceRow(
        url=record.url,
        final_url=record.final_url,
        retrieved_at=record.retrieved_at.astimezone(UTC).isoformat(),
        sha256=record.sha256,
        quote=quote,
        supports=tuple(supports),
    )


def _adjudicate(
    url: str,
    scope_hosts_and_paths: Iterable[str],
    quote: str,
    supports: Iterable[str],
    fetcher: Fetcher,
    clock: Clock,
) -> tuple[EvidenceRow | AdjudicationRefused, SourceRecord | None]:
    try:
        scopes = []
        for value in scope_hosts_and_paths:
            host, separator, path = value.partition("/")
            scopes.append(SourceScope(host, "/" + path if separator else None))
    except ValueError as exc:
        return AdjudicationRefused(f"invalid scope: {exc}"), None
    record = fetch_source(url, AuthorityClass.OFFICIAL_PROCEDURE, scopes, fetcher, clock)
    if isinstance(record, FetchFailure):
        return AdjudicationRefused(record.reason), None
    return _evidence_row(record, quote, supports), record


def adjudicate(
    url: str,
    scope_hosts_and_paths: Iterable[str],
    quote: str,
    supports: Iterable[str],
    fetcher: Fetcher,
    clock: Clock,
) -> EvidenceRow | AdjudicationRefused:
    """Fetch only within explicit HOST[/PATH] scopes, then apply the quote guard."""
    result, _record = _adjudicate(url, scope_hosts_and_paths, quote, supports, fetcher, clock)
    return result


def evidence_row_json(row: EvidenceRow) -> str:
    """A deterministic, single-line JSON object, without the JSONL newline."""
    return json.dumps(asdict(row), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def save_page(record: SourceRecord, directory: Path) -> Path:
    """Save exact UTF-8 source text under the original response body's SHA-256.

    The filename preserves fetch_source's identity of the HTTP body, which may be
    HTML or PDF; its digest need not equal the digest of this extracted text file.
    """
    if len(record.sha256) != 64 or any(c not in "0123456789abcdef" for c in record.sha256):
        raise ValueError("invalid source SHA-256")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{record.sha256}.txt"
    content = record.text.encode("utf-8")
    try:
        with path.open("xb") as output:
            output.write(content)
    except FileExistsError:
        if path.read_bytes() != content:
            raise ValueError(f"conflicting saved page: {path.name}") from None
    return path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--scope", action="append", required=True, metavar="HOST[/PATH]")
    parser.add_argument("--quote", required=True)
    parser.add_argument("--supports", action="append", required=True, metavar="FIELD")
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args(argv)
    result, record = _adjudicate(
        args.url, args.scope, args.quote, args.supports, HttpFetcher(), lambda: datetime.now(UTC)
    )
    if isinstance(result, AdjudicationRefused):
        print(result.reason)
        return 1
    assert record is not None
    try:
        save_page(record, args.out_dir)
        row = evidence_row_json(result)
        with args.evidence.open("a", encoding="utf-8") as output:
            output.write(row + "\n")
    except (OSError, ValueError) as exc:
        print(f"evidence save refused: {exc}")
        return 1
    print(row)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
