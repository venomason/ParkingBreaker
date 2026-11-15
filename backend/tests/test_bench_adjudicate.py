from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

import parkingbreaker.bench.adjudicate as adjudicate_module
from parkingbreaker.agencies import SourceScope
from parkingbreaker.bench.adjudicate import (
    AdjudicationRefused,
    EvidenceRow,
    adjudicate,
    evidence_row_json,
    main,
    save_page,
)
from parkingbreaker.research.fakes import FakeFetcher, fixed_clock, html_response, redirect_response
from parkingbreaker.research.sources import AuthorityClass, SourceRecord, fetch_source

URL = "https://city.gov/parking/contest"
QUOTE = "Mail your contest within 21 days."
HTML = f"<nav>Ignore this navigation</nav><p>{QUOTE}</p><p>Café office.</p>"
MOMENT = datetime(2026, 10, 14, 10, 30, tzinfo=timezone(timedelta(hours=-7)))
CLOCK = fixed_clock(MOMENT)
SUPPORTS = ("expected.deadline.days", "expected.deadline.trigger")


@pytest.fixture
def fetcher() -> FakeFetcher:
    return FakeFetcher({URL: html_response(URL, HTML)})


@pytest.fixture
def record(fetcher: FakeFetcher) -> SourceRecord:
    result = fetch_source(
        URL, AuthorityClass.OFFICIAL_PROCEDURE, (SourceScope("city.gov"),), fetcher, CLOCK
    )
    assert isinstance(result, SourceRecord)
    return result


def test_quote_on_page_returns_evidence_with_response_hash_and_utc(fetcher: FakeFetcher) -> None:
    result = adjudicate(URL, ("city.gov/parking",), QUOTE, iter(SUPPORTS), fetcher, CLOCK)
    assert result == EvidenceRow(
        url=URL,
        final_url=URL,
        retrieved_at="2026-10-14T17:30:00+00:00",
        sha256=hashlib.sha256(HTML.encode("utf-8")).hexdigest(),
        quote=QUOTE,
        supports=SUPPORTS,
    )
    assert fetcher.calls == [URL]


@pytest.mark.parametrize(
    "quote", ["Mail your contest within 30 days.", "Mail", "", "Ignore this navigation"]
)
def test_unverified_quote_is_refused(quote: str, fetcher: FakeFetcher) -> None:
    result = adjudicate(URL, ("city.gov",), quote, SUPPORTS, fetcher, CLOCK)
    assert isinstance(result, AdjudicationRefused)
    assert "quote" in result.reason


def test_quote_guard_keeps_existing_whitespace_rules(fetcher: FakeFetcher) -> None:
    quote = "Mail your\n contest within\t21 days."
    result = adjudicate(URL, ("city.gov",), quote, SUPPORTS, fetcher, CLOCK)
    assert isinstance(result, EvidenceRow)
    assert result.quote == quote


@pytest.mark.parametrize("scopes", [(), ("other.gov",), ("city.gov/other",), ("city.gov/park",)])
def test_url_outside_explicit_scope_is_refused_without_fetch(
    scopes: tuple[str, ...], fetcher: FakeFetcher
) -> None:
    result = adjudicate(URL, scopes, QUOTE, SUPPORTS, fetcher, CLOCK)
    assert isinstance(result, AdjudicationRefused)
    assert "allowlist" in result.reason
    assert fetcher.calls == []


@pytest.mark.parametrize("scope", ["https://city.gov", "city.gov/", "city.gov/parking/.."])
def test_invalid_scope_is_refused_without_fetch(scope: str, fetcher: FakeFetcher) -> None:
    result = adjudicate(URL, (scope,), QUOTE, SUPPORTS, fetcher, CLOCK)
    assert isinstance(result, AdjudicationRefused)
    assert "invalid scope" in result.reason
    assert fetcher.calls == []


@pytest.mark.parametrize(
    "target",
    ["https://other.gov/parking", "https://city.gov/other", "https://sub.city.gov/parking"],
)
def test_off_scope_redirect_is_refused_before_fetching_target(target: str) -> None:
    fetcher = FakeFetcher(
        {URL: redirect_response(URL, target), target: html_response(target, HTML)}
    )
    result = adjudicate(URL, ("city.gov/parking",), QUOTE, SUPPORTS, fetcher, CLOCK)
    assert isinstance(result, AdjudicationRefused)
    assert "allowlist" in result.reason
    assert fetcher.calls == [URL]


def test_explicit_second_scope_allows_redirect_and_records_final_url() -> None:
    target = "https://portal.example/city/contest"
    fetcher = FakeFetcher(
        {URL: redirect_response(URL, target), target: html_response(target, HTML)}
    )
    result = adjudicate(
        URL, ("city.gov/parking", "portal.example/city"), QUOTE, SUPPORTS, fetcher, CLOCK
    )
    assert isinstance(result, EvidenceRow)
    assert result.url == URL
    assert result.final_url == target
    assert fetcher.calls == [URL, target]


def test_fetch_error_returns_refusal() -> None:
    result = adjudicate(URL, ("city.gov",), QUOTE, SUPPORTS, FakeFetcher({}), CLOCK)
    assert isinstance(result, AdjudicationRefused)
    assert "fetch error" in result.reason


def test_http_error_returns_refusal() -> None:
    fetcher = FakeFetcher({URL: html_response(URL, HTML, status=503)})
    result = adjudicate(URL, ("city.gov",), QUOTE, SUPPORTS, fetcher, CLOCK)
    assert isinstance(result, AdjudicationRefused)
    assert "HTTP 503" in result.reason


def test_naive_clock_is_refused(fetcher: FakeFetcher) -> None:
    result = adjudicate(
        URL, ("city.gov",), QUOTE, SUPPORTS, fetcher, fixed_clock(MOMENT.replace(tzinfo=None))
    )
    assert isinstance(result, AdjudicationRefused)
    assert "timezone-aware" in result.reason


def test_save_page_is_idempotent_and_preserves_exact_text(
    record: SourceRecord, tmp_path: Path
) -> None:
    directory = tmp_path / "pages"
    path = save_page(record, directory)
    assert path == directory / f"{record.sha256}.txt"
    assert path.read_bytes() == record.text.encode("utf-8")
    before = path.stat()
    assert save_page(record, directory) == path
    assert path.stat().st_mtime_ns == before.st_mtime_ns
    assert list(directory.iterdir()) == [path]


def test_save_page_refuses_conflicting_content(record: SourceRecord, tmp_path: Path) -> None:
    path = save_page(record, tmp_path)
    with pytest.raises(ValueError, match="conflicting saved page"):
        save_page(replace(record, text="different content"), tmp_path)
    assert path.read_bytes() == record.text.encode("utf-8")


def test_save_page_refuses_invalid_hash(record: SourceRecord, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="invalid source SHA-256"):
        save_page(replace(record, sha256="../escape"), tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_json_is_canonical_and_stays_on_one_line(record: SourceRecord) -> None:
    row = EvidenceRow(URL, URL, MOMENT.astimezone(UTC).isoformat(), record.sha256, "a\nb", SUPPORTS)
    encoded = evidence_row_json(row)
    assert "\n" not in encoded
    assert encoded == json.dumps(json.loads(encoded), sort_keys=True, separators=(",", ":"))
    assert list(json.loads(encoded)) == [
        "final_url",
        "quote",
        "retrieved_at",
        "sha256",
        "supports",
        "url",
    ]
    assert json.loads(encoded)["supports"] == list(SUPPORTS)


def cli_args(tmp_path: Path, quote: str = QUOTE) -> list[str]:
    return [
        "--url",
        URL,
        "--scope",
        "city.gov/parking",
        "--scope",
        "other.gov/appeals",
        "--quote",
        quote,
        "--supports",
        SUPPORTS[0],
        "--supports",
        SUPPORTS[1],
        "--out-dir",
        str(tmp_path / "pages"),
        "--evidence",
        str(tmp_path / "evidence.jsonl"),
    ]


def test_cli_saves_and_appends_rows_offline(
    fetcher: FakeFetcher,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(adjudicate_module, "HttpFetcher", lambda: fetcher)
    evidence = tmp_path / "evidence.jsonl"
    evidence.write_text('{"prior":true}\n', encoding="utf-8")
    assert main(cli_args(tmp_path)) == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert evidence.read_text(encoding="utf-8") == '{"prior":true}\n' + output.out
    row = json.loads(output.out)
    assert row["supports"] == list(SUPPORTS)
    assert row["sha256"] == hashlib.sha256(HTML.encode("utf-8")).hexdigest()
    assert (tmp_path / "pages" / f"{row['sha256']}.txt").read_text(encoding="utf-8") == (
        QUOTE + "\n\nCafé office."
    )
    assert fetcher.calls == [URL]


def test_cli_refusal_writes_no_evidence(
    fetcher: FakeFetcher,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(adjudicate_module, "HttpFetcher", lambda: fetcher)
    assert main(cli_args(tmp_path, "Mail your contest within 30 days.")) == 1
    assert capsys.readouterr().out == "quote not verified on the page\n"
    assert list(tmp_path.iterdir()) == []


def test_cli_conflicting_page_does_not_append_row(
    record: SourceRecord,
    fetcher: FakeFetcher,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(adjudicate_module, "HttpFetcher", lambda: fetcher)
    path = save_page(replace(record, text="pre-existing conflicting content"), tmp_path / "pages")
    assert main(cli_args(tmp_path)) == 1
    assert "conflicting saved page" in capsys.readouterr().out
    assert not (tmp_path / "evidence.jsonl").exists()
    assert path.read_bytes() == b"pre-existing conflicting content"
