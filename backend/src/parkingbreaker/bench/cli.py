"""Bench I/O, pure resume/cap decisions, and one fresh subprocess per CaseRun."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import cast

from parkingbreaker.bench.cases import KeyRow, parse_answer_key
from parkingbreaker.bench.codex_dev import (
    CODEX_PRESET_MODELS,
    CodexSubscriptionLlm,
    CodexSubscriptionSearcher,
)
from parkingbreaker.bench.fakes import fake_seams
from parkingbreaker.bench.report import render_markdown
from parkingbreaker.bench.runner import CaseRun, RunDeps, run_case
from parkingbreaker.bench.score import aggregate, score_run
from parkingbreaker.brave_search import BraveSearcher
from parkingbreaker.dotgov_source import load_dotgov_snapshot
from parkingbreaker.http_fetcher import HttpFetcher
from parkingbreaker.json_types import JsonValue
from parkingbreaker.llm import LlmClient, Usage
from parkingbreaker.llm_anthropic import AnthropicWebSearcher
from parkingbreaker.llm_config import build_llm_client
from parkingbreaker.llm_models import GPT_6_LUNA, SONNET_5_5, ModelPricing
from parkingbreaker.llm_openai import OpenAIWebSearcher
from parkingbreaker.native_search import SEARCH_MAX_COST_CENTS
from parkingbreaker.research.search import Searcher, SearchError, SearchHit, SearchRequest

# The codex-* presets drive the local Codex CLI for development measurement only (owner
# decision 2026-10-20); they require PB_BENCH_DEV_SUBSCRIPTION=1 and never reach the app.
PRESETS = (
    "gpt-6-luna",
    "minimax-m3.1",
    "sonnet-5.5",
    "deepseek-flash",
    "deepseek-v4-pro",
    "gemini-3.8-flash",
    "gemini-3.5-flash-lite",
    *CODEX_PRESET_MODELS,
)
SEARCHES = ("native", "brave", "codex")
WORST_RUN_CENTS = Decimal(75 + 6 * SEARCH_MAX_COST_CENTS)  # AI phases + bounded native search.
BRAVE_REQUEST_CENTS = Decimal("0.5")
DEFAULT_SNAPSHOT = "dotgov-2026-10-13.csv"


class BenchError(Exception):
    """Fixed, credential-free messages safe to show at the CLI boundary."""


def validate_arguments(arguments: Sequence[str]) -> None:
    """Refuse credentials before argparse can echo an offending value."""
    for argument in arguments:
        value = (
            argument.partition("=")[2] if argument.startswith("-") and "=" in argument else argument
        )
        # Relative paths have nonempty components without base64 padding/markers.
        path_like = value.startswith(("/", "./", "../", "~")) or bool(
            re.fullmatch(r"[^/+=\s]+(?:/[^/+=\s]+)+/?", value)
        )
        if re.search(r"(?<![A-Za-z0-9])sk-", argument, re.IGNORECASE) or (
            not path_like and re.fullmatch(r"[A-Za-z0-9_+=/-]{32,}", value)
        ):
            raise BenchError("key-like command-line argument refused; use environment variables")


def score_bench(*, runs: Path, cases: Path, out: Path, production_use: Sequence[str] = ()) -> None:
    """Project saved runs through the pure scorer and report, without provider calls."""
    by_id = {row.case_id: row for row in parse_answer_key(cases.read_text(encoding="utf-8"))}
    records = tuple(
        CaseRun.from_json(line)
        for line in runs.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    if any(record.case_id not in by_id for record in records):
        raise BenchError("recorded case missing from answer key")
    scores = tuple(score_run(record, by_id[record.case_id]) for record in records)
    models = aggregate(scores, permitted_production_use=dict.fromkeys(production_use, True))
    out.write_text(render_markdown(models, scores), encoding="utf-8")


@dataclass(frozen=True)
class GitState:
    commit: str
    dirty: bool


def git_state() -> GitState:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    root = git("rev-parse", "--show-toplevel")
    commit = git("-C", root, "rev-parse", "HEAD")
    dirty = bool(
        git("-C", root, "status", "--porcelain=v1", "--untracked-files=all", "--", "backend/")
    )
    return GitState(commit, dirty)


def run_id(preset: str, case_id: str, repeat_index: int) -> str:
    return f"{preset}:{case_id}:{repeat_index}"


def record_cost_cents(record: Mapping[str, JsonValue]) -> Decimal:
    costs = record.get("cost_cents")
    requests = record.get("brave_requests")
    if not isinstance(costs, dict) or set(costs) != {"prehold", "research"}:
        raise BenchError("invalid recorded AI cost")
    values = [*costs.values(), requests]
    if any(type(value) is not int or value < 0 for value in values):
        raise BenchError("invalid recorded cost or search count")
    return Decimal(cast(int, costs["prehold"]) + cast(int, costs["research"])) + (
        Decimal(cast(int, requests)) * BRAVE_REQUEST_CENTS
    )


def recorded_runs(
    text: str, fake: bool, *, search: str | None = None, selected_preset: str | None = None
) -> tuple[set[str], Decimal]:
    """Fail closed on damaged history; never ignore a truncated paid record."""
    done: set[str] = set()
    spent = Decimal(0)
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record: JsonValue = json.loads(line)
        except ValueError:
            raise BenchError("invalid runs.jsonl; repair history before resuming") from None
        if not isinstance(record, dict):
            raise BenchError("invalid run record")
        preset, case_id, index = (
            record.get("preset"),
            record.get("case_id"),
            record.get("repeat_index"),
        )
        if (
            not isinstance(preset, str)
            or not isinstance(case_id, str)
            or type(index) is not int
            or index < 1
        ):
            raise BenchError("invalid recorded run identity")
        identity = run_id(preset, case_id, index)
        if record.get("run_id") != identity or identity in done:
            raise BenchError("invalid or duplicate recorded run id")
        if record.get("fake") is not fake:
            raise BenchError("fake and live runs require separate output directories")
        # Legacy records used Brave; never silently resume them as native discovery.
        if (
            not fake
            and search is not None
            and preset == selected_preset
            and record.get("search", "brave") != search
        ):
            raise BenchError(
                "search selection differs from history; use a separate output directory"
            )
        spent += record_cost_cents(record)
        done.add(identity)
    return done, spent


def pending_runs(
    rows: Sequence[KeyRow], preset: str, repeat: int, done: set[str]
) -> tuple[tuple[KeyRow, int], ...]:
    # Complete one sweep before repeats, retaining the caller's case selection order.
    return tuple(
        (row, index)
        for index in range(1, repeat + 1)
        for row in rows
        if run_id(preset, row.case_id, index) not in done
    )


def can_start_run(spent_cents: Decimal, cap_usd: Decimal, *, fake: bool = False) -> bool:
    """Compare exactly, without rounding a near-boundary cap up to an affordable run."""
    reservation = Decimal(78) if fake else WORST_RUN_CENTS
    return Fraction(spent_cents) + Fraction(reservation) <= Fraction(cap_usd) * 100


def _secrets(environ: Mapping[str, str]) -> tuple[str, ...]:
    return tuple(value for name, value in environ.items() if name.endswith("API_KEY") and value)


def redact(value: JsonValue, secrets: Sequence[str]) -> JsonValue:
    if isinstance(value, str):
        for secret in sorted(secrets, key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, dict):
        return {
            cast(str, redact(key, secrets)): redact(item, secrets) for key, item in value.items()
        }
    return value


def save_page(directory: str, sha: str, body: bytes) -> None:
    if hashlib.sha256(body).hexdigest() != sha:
        raise BenchError("page hash mismatch")
    if any(secret.encode() in body for secret in _secrets(os.environ)):
        raise BenchError("credential-bearing page refused")
    path = Path(directory) / sha
    try:
        with path.open("xb") as output:
            output.write(body)
    except FileExistsError:
        if path.read_bytes() != body:
            raise BenchError("conflicting saved page") from None


@dataclass(frozen=True)
class WorkItem:
    row_json: str
    run_date: str
    preset: str
    commit: str
    pages_dir: str
    snapshot_path: str | None
    fake: bool
    search: str = "brave"


class CountingSearcher:
    """Count attempts, including failures, and enforce the cap's six-request bound."""

    def __init__(self, inner: Searcher, *, brave: bool = True) -> None:
        self.inner = inner
        self._brave = brave
        self.requests_made = 0

    @property
    def usage(self) -> Usage:
        value = getattr(self.inner, "usage", Usage())
        return value if isinstance(value, Usage) else Usage()

    @property
    def last_call(self) -> object | None:
        return getattr(self.inner, "last_call", None)

    @property
    def brave_requests(self) -> int:
        return self.requests_made if self._brave else 0

    def search(self, request: SearchRequest) -> Sequence[SearchHit]:
        if self.requests_made >= 6:
            raise SearchError("bench search request ceiling reached")
        self.requests_made += 1
        return self.inner.search(request)


def build_searcher(preset: str, environ: Mapping[str, str], search: str = "brave") -> Searcher:
    if search == "brave":
        return BraveSearcher(environ)
    if search not in {"native", "codex"} or preset not in PRESETS:
        raise ValueError("Unknown search selection or preset")
    if preset in CODEX_PRESET_MODELS:
        # "native" means the preset's own provider search, which for Codex is its web search.
        return CodexSubscriptionSearcher(CODEX_PRESET_MODELS[preset], environ=environ)
    if search == "codex":
        raise ValueError("Codex search requires a codex-* preset")
    if preset == "minimax-m3.1":
        raise ValueError("MiniMax coding-plan search entitlement is not verified")
    if preset == "gpt-6-luna":
        return OpenAIWebSearcher(GPT_6_LUNA, environ.get("OPENAI_API_KEY", ""))
    if preset == "sonnet-5.5":
        return AnthropicWebSearcher(SONNET_5_5, environ.get("ANTHROPIC_API_KEY", ""))
    raise ValueError("Unknown search selection or preset")


def build_client(
    preset: str, environ: Mapping[str, str], *, minimax_pricing: ModelPricing
) -> LlmClient:
    if preset in CODEX_PRESET_MODELS:
        return CodexSubscriptionLlm(CODEX_PRESET_MODELS[preset], environ=environ)
    return build_llm_client(preset, environ, minimax_pricing=minimax_pricing)


def execute_work(item: WorkItem) -> str:
    (row,) = parse_answer_key(item.row_json)
    today = date.fromisoformat(item.run_date)
    if item.fake:
        fetcher, searcher, client, snapshot = fake_seams(row, today)
        deps = RunDeps(
            fetcher,
            searcher,
            client,
            lambda: datetime.now(UTC),
            time.monotonic,
            lambda _: None,
            snapshot,
            item.preset,
            item.commit,
            row.key_version,
            evidence_dir=item.pages_dir,
            evidence_writer=save_page,
        )
    else:
        if item.snapshot_path is None:
            raise BenchError("live runs require a dated DotGov snapshot")
        pricing = ModelPricing(Decimal(30), Decimal(3), Decimal(120))
        deps = RunDeps(
            HttpFetcher(),
            CountingSearcher(
                build_searcher(item.preset, os.environ, item.search), brave=item.search == "brave"
            ),
            build_client(item.preset, os.environ, minimax_pricing=pricing),
            lambda: datetime.now(UTC),
            time.monotonic,
            time.sleep,
            load_dotgov_snapshot(Path(item.snapshot_path)),
            item.preset,
            item.commit,
            row.key_version,
            evidence_dir=item.pages_dir,
            evidence_writer=save_page,
        )
    record: JsonValue = json.loads(run_case(row, today, deps).to_json())
    return json.dumps(redact(record, _secrets(os.environ)), allow_nan=False)


def run_process(item: WorkItem) -> str:
    result = subprocess.run(
        [sys.executable, "-B", "-m", "parkingbreaker.bench.worker"],
        input=json.dumps(asdict(item)),
        text=True,
        capture_output=True,
    )
    if result.returncode:
        raise BenchError("case worker failed; stopped without retrying")
    return result.stdout


@dataclass(frozen=True)
class BenchResult:
    completed: int
    skipped: int
    cap_stopped: bool
    spent_cents: Decimal


def run_bench(
    *,
    preset: str,
    cases: Path,
    repeat: int,
    out: Path,
    cap_usd: Decimal,
    fake: bool = False,
    search: str = "brave",
    case_ids: Sequence[str] = (),
    snapshot: Path | None = None,
    inspect_git: Callable[[], GitState] = git_state,
    execute: Callable[[WorkItem], str] = run_process,
) -> BenchResult:
    if (
        preset not in PRESETS
        or search not in SEARCHES
        or repeat < 1
        or not cap_usd.is_finite()
        or cap_usd < 0
    ):
        raise BenchError("invalid preset, repeat count, or spending cap")
    state = inspect_git()
    if state.dirty and not fake:
        raise BenchError("uncommitted changes under backend/; live bench refused")
    rows = parse_answer_key(cases.read_text(encoding="utf-8"))
    by_id = {row.case_id: row for row in rows}
    if case_ids:
        if any(identity not in by_id for identity in case_ids):
            raise BenchError("unknown case selection")
        rows = tuple(by_id[identity] for identity in dict.fromkeys(case_ids))
    out.mkdir(parents=True, exist_ok=True)
    pages = out / "pages"
    pages.mkdir(exist_ok=True)
    with (out / "runs.jsonl").open("a+", encoding="utf-8") as output:
        try:
            fcntl.flock(output, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BenchError("output directory is already in use") from None
        output.seek(0)
        history = output.read()
        done, spent = recorded_runs(history, fake, search=search, selected_preset=preset)
        pending = pending_runs(rows, preset, repeat, done)
        skipped = len(rows) * repeat - len(pending)
        completed = 0
        today = date.today().isoformat()
        snapshot_path = snapshot or out / DEFAULT_SNAPSHOT
        if pending and can_start_run(spent, cap_usd, fake=fake) and not fake:
            load_dotgov_snapshot(snapshot_path)
            saved = out / snapshot_path.name
            content = snapshot_path.read_bytes()
            if saved.exists() and saved.read_bytes() != content:
                raise BenchError("conflicting saved snapshot")
            saved.write_bytes(content)
            snapshot_path = saved
        for row, index in pending:
            if not can_start_run(spent, cap_usd, fake=fake):
                return BenchResult(completed, skipped, True, spent)
            item = WorkItem(
                json.dumps(asdict(row)),
                today,
                preset,
                state.commit,
                str(pages.resolve()),
                None if fake else str(snapshot_path.resolve()),
                fake,
                search,
            )
            record: JsonValue = json.loads(execute(item))
            if (
                not isinstance(record, dict)
                or record.get("case_id") != row.case_id
                or record.get("preset") != preset
            ):
                raise BenchError("invalid worker result")
            record.update(run_id=run_id(preset, row.case_id, index), repeat_index=index, fake=fake)
            if not fake:
                record["search"] = search
            cost = record_cost_cents(record)
            text = json.dumps(redact(record, _secrets(os.environ)), allow_nan=False)
            # A valid final JSON object without a newline is still a completed run.
            output.write(("\n" if history and not history.endswith("\n") else "") + text + "\n")
            output.flush()
            os.fsync(output.fileno())
            history = ""
            spent += cost
            completed += 1
        return BenchResult(completed, skipped, False, spent)
