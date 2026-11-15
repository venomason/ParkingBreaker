from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from parkingbreaker.bench import cli
from parkingbreaker.bench.__main__ import main, parse_arguments
from parkingbreaker.bench.cases import parse_answer_key
from parkingbreaker.bench.cli import (
    BenchError,
    CountingSearcher,
    GitState,
    WorkItem,
    can_start_run,
    execute_work,
    git_state,
    record_cost_cents,
    recorded_runs,
    run_bench,
    save_page,
    score_bench,
    validate_arguments,
)
from parkingbreaker.bench.fakes import fake_seams
from parkingbreaker.bench.runner import CaseRun
from parkingbreaker.brave_search import BraveSearcher
from parkingbreaker.json_types import JsonValue
from parkingbreaker.llm_models import GPT_6_LUNA, SONNET_5_5, ModelPricing
from parkingbreaker.research.fakes import FakeLlmClient, FakeSearcher
from parkingbreaker.research.search import SearchError, SearchHit, SearchRequest

ROOT = Path(__file__).resolve().parents[2]
KEY = ROOT / "docs/bench/answer-key.jsonl"
GOLDEN = Path(__file__).parent / "fixtures/bench_b16_run.json"


def records(out: Path) -> list[dict[str, JsonValue]]:
    return [json.loads(line) for line in (out / "runs.jsonl").read_text().splitlines()]


def paid_run(item: WorkItem) -> str:
    (row,) = parse_answer_key(item.row_json)
    record: dict[str, JsonValue] = json.loads(GOLDEN.read_text())
    record.update(case_id=row.case_id, preset=item.preset, commit=item.commit)
    record.update(cost_cents={"prehold": 25, "research": 50}, brave_requests=6)
    return json.dumps(record)


@pytest.fixture
def snapshot(tmp_path: Path) -> Path:
    path = tmp_path / "dotgov-2026-10-13.csv"
    path.write_text(
        "Domain name,Domain type,Organization name,Suborganization name,City,State,"
        "Security contact email\n"
    )
    return path


def test_all_17_cases_offline_and_resume(tmp_path: Path) -> None:
    # sitecustomize applies to both the CLI and every fresh child process.
    marker = tmp_path / "network-attempt"
    (tmp_path / "sitecustomize.py").write_text(
        "import pathlib, socket\n"
        "def refused(*args, **kwargs):\n"
        f"    pathlib.Path({str(marker)!r}).touch()\n"
        "    raise AssertionError('network forbidden')\n"
        "socket.socket.connect = refused\n"
        "socket.create_connection = refused\n"
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join((str(tmp_path), str(ROOT / "backend/src")))
    for name in ("OPENAI_API_KEY", "BRAVE_SEARCH_API_KEY", "ANTHROPIC_API_KEY", "MINIMAX_API_KEY"):
        environment.pop(name, None)
    out = tmp_path / "output"
    command = [
        sys.executable,
        "-B",
        "-m",
        "parkingbreaker.bench",
        "run",
        "--preset",
        "gpt-6-luna",
        "--cases",
        str(KEY),
        "--repeat",
        "1",
        "--out",
        str(out),
        "--cap-usd",
        "75",
        "--fake",
    ]
    first = subprocess.run(command, env=environment, capture_output=True, text=True, check=False)
    assert first.returncode == 0, first.stderr
    assert "completed=17" in first.stdout
    assert "recorded_usd=0" in first.stdout
    data = records(out)
    assert {row["case_id"] for row in data} == {f"B{i:02}" for i in range(1, 18)}
    assert len({row["run_id"] for row in data}) == 17
    assert all(row["commit"] == git_state().commit for row in data)
    assert all(row["fake"] is True and row["brave_requests"] == 0 for row in data)
    assert all(cast(dict[str, JsonValue], row["research_outcome"])["ran"] for row in data)
    for row in data:
        for fetch in cast(list[dict[str, JsonValue]], row["fetches"]):
            sha = cast(str, fetch["sha256"])
            assert hashlib.sha256((out / "pages" / sha).read_bytes()).hexdigest() == sha
    before = (out / "runs.jsonl").read_bytes()
    second = subprocess.run(command, env=environment, capture_output=True, text=True, check=False)
    assert second.returncode == 0, second.stderr
    assert "completed=0 skipped=17" in second.stdout
    assert (out / "runs.jsonl").read_bytes() == before
    report = tmp_path / "report.md"
    score_command = [
        sys.executable,
        "-B",
        "-m",
        "parkingbreaker.bench",
        "score",
        "--runs",
        str(out / "runs.jsonl"),
        "--cases",
        str(KEY),
        "--out",
        str(report),
    ]
    scored = subprocess.run(
        score_command, env=environment, capture_output=True, text=True, check=False
    )
    assert scored.returncode == 0, scored.stderr
    rendered = report.read_bytes()
    assert rendered.startswith(b"# Phase 0 qualification bench\n")
    assert b"Production use confirmed: unscored" in rendered
    assert all(f"| B{i:02} |".encode() in rendered for i in range(1, 18))
    # Input order and blank lines cannot change the Markdown projection.
    (out / "runs.jsonl").write_text("\n\n".join(reversed(before.decode().splitlines())))
    rescored = subprocess.run(
        score_command, env=environment, capture_output=True, text=True, check=False
    )
    assert rescored.returncode == 0, rescored.stderr
    assert report.read_bytes() == rendered
    assert not marker.exists()


@pytest.mark.parametrize(
    "arguments",
    [
        ["--out", "sk-example"],
        ["--out=sk-example"],
        ["--unknown-key", "A" * 40],
        ["--cases", "Ab3/" * 12 + "=="],
        ["--out", "B" * 44 + "="],
        ["--out=" + "A" * 32],
        ["--out", "A" * 32],
        ["--out", "Ab3_+=-" * 6],
        ["--out", "/tmp/" + "A" * 40 + "/sk-token"],
        ["--cases=./data/SK-token.jsonl"],
    ],
)
def test_key_like_arguments_are_refused_without_echo(
    arguments: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["run", *arguments]) == 2
    captured = capsys.readouterr()
    assert "key-like command-line argument refused" in captured.err
    assert all(
        value not in captured.err + captured.out
        for value in arguments
        if not value.startswith("--")
    )


@pytest.mark.parametrize("inline", [False, True])
@pytest.mark.parametrize(
    "path",
    [
        "/tmp/claude-1000/-home-ghostmonday-Projects-parkingbreaker-fresh/"
        "44d6700d-46c5-4de6-acf8-715b59d56c9b/scratchpad/x",
        "/tmp/" + "A" * 40,
        "./" + "A" * 40,
        "../" + "A" * 40,
        "~/" + "A" * 40,
        "artifacts/" + "A" * 40 + "/runs.jsonl",
        "A" * 40 + "/report.md",
        "/tmp/task-output",
    ],
)
def test_path_arguments_allow_long_components(path: str, inline: bool) -> None:
    validate_arguments([f"--out={path}"] if inline else ["--out", path])


def test_score_wiring_and_explicit_production_use(tmp_path: Path) -> None:
    runs = tmp_path / "runs.jsonl"
    record = json.loads(GOLDEN.read_text())
    record["preset"] = "gpt-6-luna"
    other = {**record, "preset": "sonnet-5.5"}
    runs.write_text(json.dumps(record) + "\n" + json.dumps(other))
    report = tmp_path / "report.md"
    arguments = ["score", "--runs", str(runs), "--cases", str(KEY), "--out", str(report)]
    assert main([*arguments, "--production-use", "gpt-6-luna"]) == 0
    text = report.read_text()
    assert "## gpt-6-luna" in text
    assert "## sonnet-5.5" in text
    assert text.count("Production use confirmed: yes") == 1
    assert text.count("Production use confirmed: unscored") == 1
    assert main([*arguments, "--production-use", "gpt-6-luna", "sonnet-5.5"]) == 0
    assert report.read_text().count("Production use confirmed: yes") == 2
    assert (
        main([*arguments, "--production-use", "gpt-6-luna", "--production-use", "sonnet-5.5"]) == 0
    )
    assert report.read_text().count("Production use confirmed: yes") == 2


@pytest.mark.parametrize("content", ["{", "[]", "{}", '{"case_id":"unknown"}'])
def test_score_invalid_runs_do_not_overwrite_report(
    tmp_path: Path, content: str, capsys: pytest.CaptureFixture[str]
) -> None:
    runs = tmp_path / "runs.jsonl"
    runs.write_text(content)
    report = tmp_path / "report.md"
    report.write_text("previous report")
    assert main(["score", "--runs", str(runs), "--cases", str(KEY), "--out", str(report)]) == 2
    assert report.read_text() == "previous report"
    assert "Traceback" not in capsys.readouterr().err


def test_score_requires_matching_answer_key(tmp_path: Path) -> None:
    runs = tmp_path / "runs.jsonl"
    record = json.loads(GOLDEN.read_text())
    record["case_id"] = "unknown"
    runs.write_text(json.dumps(record))
    report = tmp_path / "report.md"
    with pytest.raises(BenchError, match="case missing from answer key"):
        score_bench(runs=runs, cases=KEY, out=report)
    assert not report.exists()


def test_score_reports_verdict_and_aggregated_friction(tmp_path: Path) -> None:
    key = json.loads((GOLDEN.parent / "bench_b16.jsonl").read_text())
    key.update(status="certain", uncertain_fields=[])
    cases = tmp_path / "key.jsonl"
    cases.write_text(json.dumps(key))
    record = json.loads(GOLDEN.read_text())
    record["research_outcome"] = {
        "outcome": "Stopped",
        "family": "NOT_YET_VERIFIED",
        "hard_stop": "S2",
        "ran": True,
    }
    record["critical_problems"] = [{"reason": "synthetic guard failure"}]
    runs = tmp_path / "runs.jsonl"
    runs.write_text(json.dumps(record))
    report = tmp_path / "report.md"
    score_bench(runs=runs, cases=cases, out=report)
    text = report.read_text()
    assert "Scored runs: 1; excluded runs: 0." in text
    assert "| safe-wrong-family |" in text
    assert "| research | synthetic guard failure | 1 |" in text


def test_parse_selection_preserves_requested_order() -> None:
    args = parse_arguments(
        [
            "run",
            "--preset",
            "gpt-6-luna",
            "--cases",
            str(KEY),
            "--repeat",
            "2",
            "--out",
            "/tmp/task-output",
            "--cap-usd",
            "75",
            "--case",
            "B13",
            "B16",
            "--case",
            "B03",
        ]
    )
    assert args.case == [["B13", "B16"], ["B03"]]


def test_cap_counts_recorded_ai_and_half_cent_searches_and_resumes(
    tmp_path: Path, snapshot: Path
) -> None:
    out = tmp_path / "runs"
    calls: list[str] = []

    def execute(item: WorkItem) -> str:
        (row,) = parse_answer_key(item.row_json)
        calls.append(row.case_id)
        assert item.commit == "pinned-commit"
        return paid_run(item)

    def run(cap: str) -> None:
        result = run_bench(
            preset="gpt-6-luna",
            cases=KEY,
            repeat=2,
            out=out,
            cap_usd=Decimal(cap),
            case_ids=("B13", "B16", "B03"),
            snapshot=snapshot,
            inspect_git=lambda: GitState("pinned-commit", False),
            execute=execute,
        )
        assert result.cap_stopped

    run("3.749999")
    assert not calls
    run("3.75")
    assert calls == ["B13"]
    run("4.53")
    assert calls == ["B13", "B16"]
    run("6.09")
    assert calls == ["B13", "B16", "B03", "B13"]
    data = records(out)
    assert [row["repeat_index"] for row in data] == [1, 1, 1, 2]
    assert sum(record_cost_cents(row) for row in data) == Decimal(312)
    assert (out / snapshot.name).read_bytes() == snapshot.read_bytes()


def test_resume_counts_other_presets_and_fractional_search_cost(
    tmp_path: Path, snapshot: Path
) -> None:
    out = tmp_path / "runs"
    out.mkdir()
    record: dict[str, JsonValue] = json.loads(GOLDEN.read_text())
    record.update(
        case_id="B16",
        preset="sonnet-5.5",
        repeat_index=1,
        run_id="sonnet-5.5:B16:1",
        fake=False,
        cost_cents={"prehold": 0, "research": 0},
        brave_requests=1,
    )
    (out / "runs.jsonl").write_text(json.dumps(record))  # Valid final line without newline.
    result = run_bench(
        preset="gpt-6-luna",
        cases=KEY,
        repeat=1,
        out=out,
        cap_usd=Decimal("3.7549"),
        case_ids=("B16",),
        snapshot=snapshot,
        inspect_git=lambda: GitState("commit", False),
        execute=paid_run,
    )
    assert result.cap_stopped
    assert result.completed == 0
    assert result.spent_cents == Decimal("0.5")
    result = run_bench(
        preset="gpt-6-luna",
        cases=KEY,
        repeat=1,
        out=out,
        cap_usd=Decimal("3.755"),
        case_ids=("B16",),
        snapshot=snapshot,
        inspect_git=lambda: GitState("commit", False),
        execute=paid_run,
    )
    assert result.completed == 1
    assert len(records(out)) == 2


def test_dirty_backend_refuses_before_any_work(tmp_path: Path) -> None:
    out = tmp_path / "output"
    with pytest.raises(BenchError, match="uncommitted changes under backend/"):
        run_bench(
            preset="gpt-6-luna",
            cases=KEY,
            repeat=1,
            out=out,
            cap_usd=Decimal(75),
            inspect_git=lambda: GitState("commit", True),
        )
    assert not out.exists()
    result = run_bench(
        preset="gpt-6-luna",
        cases=KEY,
        repeat=1,
        out=out,
        cap_usd=Decimal(75),
        fake=True,
        case_ids=("B16",),
        inspect_git=lambda: GitState("commit", True),
        execute=execute_work,
    )
    assert result.completed == 1


def test_search_ceiling_counts_failed_attempts() -> None:
    def fail(_: SearchRequest) -> Sequence[SearchHit]:
        raise SearchError("failed")

    inner = FakeSearcher(fail)
    searcher = CountingSearcher(inner)
    request = SearchRequest("query", (), frozenset(), False)
    for _ in range(6):
        with pytest.raises(SearchError, match=r"^failed$"):
            searcher.search(request)
    with pytest.raises(SearchError, match="ceiling reached"):
        searcher.search(request)
    assert searcher.requests_made == len(inner.calls) == 6


def test_credentials_never_reach_records_or_saved_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = 'secret-with-quote-"-and-newline-\n'
    monkeypatch.setenv("OPENAI_API_KEY", secret)

    def execute(item: WorkItem) -> str:
        record: dict[str, JsonValue] = json.loads(paid_run(item))
        record["digest_error"] = secret
        return json.dumps(record)

    result = run_bench(
        preset="gpt-6-luna",
        cases=KEY,
        repeat=1,
        out=tmp_path,
        cap_usd=Decimal(1),
        fake=True,
        case_ids=("B16",),
        inspect_git=lambda: GitState("commit", False),
        execute=execute,
    )
    assert result.completed == 1
    assert records(tmp_path)[0]["digest_error"] == "[REDACTED]"
    body = secret.encode()
    with pytest.raises(BenchError, match="credential-bearing page refused"):
        save_page(str(tmp_path / "pages"), hashlib.sha256(body).hexdigest(), body)
    assert not list((tmp_path / "pages").iterdir())


@pytest.mark.parametrize(
    ("preset", "search_mode"),
    [
        ("gpt-6-luna", "native"),
        ("gpt-6-luna", "brave"),
        ("sonnet-5.5", "native"),
        ("sonnet-5.5", "brave"),
        ("minimax-m3.1", "brave"),
    ],
)
def test_live_wiring_uses_environment_and_plan_pricing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, preset: str, search_mode: str
) -> None:
    row = next(row for row in parse_answer_key(KEY.read_text()) if row.case_id == "B16")
    fetcher, searcher, client, snapshot = fake_seams(row, date(2026, 10, 14))
    calls: list[str] = []

    def build(
        name: str, environ: Mapping[str, str], *, minimax_pricing: ModelPricing | None = None
    ) -> FakeLlmClient:
        assert environ is os.environ
        assert name == preset
        assert minimax_pricing == ModelPricing(Decimal(30), Decimal(3), Decimal(120))
        calls.append("llm")
        return client

    def native(config: object, api_key: str) -> FakeSearcher:
        assert api_key == "PRIVATE"
        assert config == (GPT_6_LUNA if preset == "gpt-6-luna" else SONNET_5_5)
        calls.append("native")
        return searcher

    def brave(environ: Mapping[str, str]) -> FakeSearcher:
        assert environ is os.environ
        calls.append("brave")
        return searcher

    for variable in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "MINIMAX_API_KEY"):
        monkeypatch.setenv(variable, "PRIVATE")
    monkeypatch.setattr(cli, "OpenAIWebSearcher", native)
    monkeypatch.setattr(cli, "AnthropicWebSearcher", native)
    monkeypatch.setattr(cli, "build_llm_client", build)
    monkeypatch.setattr(cli, "BraveSearcher", brave)
    monkeypatch.setattr(cli, "HttpFetcher", lambda: fetcher)
    monkeypatch.setattr(cli, "load_dotgov_snapshot", lambda _: snapshot)
    (tmp_path / "pages").mkdir()
    item = WorkItem(
        row_json=json.dumps(asdict(row)),
        run_date="2026-10-14",
        preset=preset,
        commit="pinned",
        pages_dir=str(tmp_path / "pages"),
        snapshot_path=str(tmp_path / "dotgov-2026-10-13.csv"),
        fake=False,
        search=search_mode,
    )
    record = json.loads(execute_work(item))
    assert calls == [search_mode, "llm"]
    assert record["commit"] == "pinned"
    assert record["brave_requests"] == (len(searcher.calls) if search_mode == "brave" else 0)
    assert client.ceilings[0] == 25
    assert client.ceilings[-1] == 50


@pytest.mark.parametrize("history", ['{"truncated":', "{}", "[]"])
def test_malformed_history_fails_closed(history: str) -> None:
    with pytest.raises(BenchError):
        recorded_runs(history, False)


@pytest.mark.parametrize("cap", ["NaN", "Infinity", "-1"])
def test_invalid_cap_is_refused_without_outputs(tmp_path: Path, cap: str) -> None:
    with pytest.raises(BenchError, match="spending cap"):
        run_bench(
            preset="gpt-6-luna",
            cases=KEY,
            repeat=1,
            out=tmp_path / "output",
            cap_usd=Decimal(cap),
            fake=True,
        )
    assert not (tmp_path / "output").exists()


def test_cap_comparison_never_rounds_up() -> None:
    assert not can_start_run(Decimal(0), Decimal("3.749999999999999999999999999999999999"))
    assert can_start_run(Decimal("0.5"), Decimal("3.755"))


def test_invalid_decimal_argument_is_sanitized(capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        main(
            [
                "run",
                "--preset",
                "gpt-6-luna",
                "--cases",
                str(KEY),
                "--repeat",
                "1",
                "--out",
                "/tmp/unused",
                "--cap-usd",
                "not-a-number",
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert "not-a-number" not in captured.err
    assert "Traceback" not in captured.err


def test_search_cli_defaults_to_brave_and_allows_explicit_native() -> None:
    arguments = [
        "run",
        "--preset",
        "gpt-6-luna",
        "--cases",
        str(KEY),
        "--repeat",
        "1",
        "--out",
        "/tmp/task-output",
        "--cap-usd",
        "75",
    ]
    assert parse_arguments(arguments).search == "brave"
    assert parse_arguments([*arguments, "--search", "native"]).search == "native"
    assert parse_arguments([*arguments, "--search", "codex"]).search == "codex"
    assert set(cli.SEARCHES) == {"native", "brave", "codex"}
    with pytest.raises(BenchError):
        parse_arguments([*arguments, "--search", "bing"])


def test_bench_default_uses_brave_without_minimax_search_entitlement() -> None:
    searcher = cli.build_searcher("minimax-m3.1", {"BRAVE_SEARCH_API_KEY": "placeholder"})
    assert isinstance(searcher, BraveSearcher)
    assert searcher.requests_made == 0

    with pytest.raises(ValueError, match="coding-plan search entitlement is not verified"):
        cli.build_searcher("minimax-m3.1", {"MINIMAX_API_KEY": "placeholder"}, search="native")


def test_native_search_cost_included_once_in_cap() -> None:
    assert record_cost_cents(
        {"cost_cents": {"prehold": 20, "research": 30}, "brave_requests": 0}
    ) == Decimal(50)
    assert Decimal(375) == cli.WORST_RUN_CENTS


def test_resume_refuses_changed_discovery_for_same_preset() -> None:
    record: dict[str, JsonValue] = json.loads(GOLDEN.read_text())
    record.update(preset="gpt-6-luna", run_id="gpt-6-luna:B16:1", repeat_index=1, fake=False)
    legacy = json.dumps(record)
    with pytest.raises(BenchError, match="search selection differs"):
        recorded_runs(legacy, False, search="native", selected_preset="gpt-6-luna")
    assert recorded_runs(legacy, False, search="brave", selected_preset="gpt-6-luna")[0]
    record["search"] = "native"
    with pytest.raises(BenchError, match="search selection differs"):
        recorded_runs(json.dumps(record), False, search="brave", selected_preset="gpt-6-luna")
    assert CaseRun.from_json(json.dumps(record)).preset == "gpt-6-luna"


@pytest.mark.parametrize(
    "preset", ["deepseek-flash", "deepseek-v4-pro", "gemini-3.8-flash", "gemini-3.5-flash-lite"]
)
def test_selected_runtime_presets_keep_offline_and_zero_cap_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, preset: str
) -> None:
    import socket

    def no_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("offline bench attempted network access")

    def no_work(item: WorkItem) -> str:
        raise AssertionError("zero-cap bench attempted a provider work item")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    arguments = [
        "run",
        "--preset",
        preset,
        "--cases",
        str(KEY),
        "--repeat",
        "1",
        "--out",
        str(tmp_path / "offline"),
        "--cap-usd",
        "1",
        "--case",
        "B16",
        "--fake",
    ]
    assert parse_arguments(arguments).preset == preset
    result = run_bench(
        preset=preset,
        cases=KEY,
        repeat=1,
        out=tmp_path / "offline",
        cap_usd=Decimal(1),
        fake=True,
        case_ids=("B16",),
        execute=execute_work,
    )
    assert result.completed == 1
    assert result.spent_cents <= 100
    saved = records(tmp_path / "offline")
    assert len(saved) == 1
    assert saved[0]["preset"] == preset
    assert saved[0]["fake"] is True
    stopped = run_bench(
        preset=preset,
        cases=KEY,
        repeat=1,
        out=tmp_path / "zero-cap",
        cap_usd=Decimal(0),
        fake=False,
        case_ids=("B16",),
        inspect_git=lambda: GitState("synthetic-commit", False),
        execute=no_work,
    )
    assert stopped.completed == 0
    assert stopped.cap_stopped
    with pytest.raises(ValueError, match="Unknown search selection"):
        cli.build_searcher(preset, {}, search="native")
