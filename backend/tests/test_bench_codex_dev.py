from __future__ import annotations

import ast
import json
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

from parkingbreaker.bench import cli
from parkingbreaker.bench.codex_dev import (
    CODEX_FRAMEWORK_INPUT_TOKENS,
    CODEX_FRAMEWORK_INPUT_TOKENS_OBSERVED,
    CODEX_PRESET_MODELS,
    ENV_GUARD,
    CodexDevDisabledError,
    CodexInvocation,
    CodexProcessResult,
    CodexSubscriptionLlm,
    CodexSubscriptionSearcher,
    CodexUsageReport,
    parse_event_stream,
    subprocess_runner,
)
from parkingbreaker.json_types import JsonValue
from parkingbreaker.llm import (
    LlmBudgetExceeded,
    LlmProviderError,
    Usage,
    conservative_input_tokens,
    schema_json,
)
from parkingbreaker.llm_models import GPT_6_LUNA, usage_cost_cents
from parkingbreaker.research.questions import CRITICAL_KEYS
from parkingbreaker.research.search import SearchError, SearchRequest

ENV = {ENV_GUARD: "1"}
SCHEMA: Mapping[str, JsonValue] = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}
SRC = Path(__file__).resolve().parents[1] / "src" / "parkingbreaker"

# Observed live 2026-10-20, codex-cli 0.159.3, `codex exec --json -m gpt-6-luna` (trivial
# ~50-character prompt; thread id replaced). Note: NO item.started events, and usage includes
# cache_write_input_tokens.
OBSERVED_LIVE_SAMPLE = "\n".join(
    json.dumps(event)
    for event in (
        {"type": "thread.started", "thread_id": "00000000-0000-0000-0000-000000000000"},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {"id": "item_0", "type": "agent_message", "text": '{"answer":"ok"}'},
        },
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 14297,
                "cached_input_tokens": 0,
                "cache_write_input_tokens": 0,
                "output_tokens": 15,
                "reasoning_output_tokens": 0,
            },
        },
    )
)

# Derived from installed Codex docs (codex-cli 0.159.3: `codex exec --help`, event and field
# names in the binary); kept for coverage of the event kinds the live sample lacks (reasoning,
# web_search, error, cached tokens). The usage numbers are invented test values.
EVENT_SAMPLE = "\n".join(
    json.dumps(event)
    for event in (
        {"type": "thread.started", "thread_id": "00000000-0000-0000-0000-000000000000"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "item_0", "type": "reasoning", "text": "x"}},
        {"type": "item.completed", "item": {"id": "item_1", "type": "web_search", "query": "q"}},
        {"type": "error", "message": "Reconnecting... 1/5"},
        {
            "type": "item.completed",
            "item": {"id": "item_2", "type": "agent_message", "text": '{"answer": "ok"}'},
        },
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 20000,
                "cached_input_tokens": 12000,
                "cache_write_input_tokens": 0,
                "output_tokens": 500,
                "reasoning_output_tokens": 300,
            },
        },
    )
)


class FakeRunner:
    def __init__(
        self,
        final: str = '{"answer": "ok"}',
        stdout: str = EVENT_SAMPLE,
        returncode: int = 0,
        error: Exception | None = None,
    ) -> None:
        self.final, self.stdout, self.returncode, self.error = final, stdout, returncode, error
        self.calls: list[CodexInvocation] = []
        self.files: dict[str, str] = {}

    def __call__(self, invocation: CodexInvocation) -> CodexProcessResult:
        self.calls.append(invocation)
        if self.error is not None:
            raise self.error
        argv = invocation.argv
        self.files["schema"] = Path(argv[argv.index("--output-schema") + 1]).read_text()
        if self.returncode == 0:
            Path(argv[argv.index("-o") + 1]).write_text(self.final)
        return CodexProcessResult(self.returncode, self.stdout)


def llm(runner: FakeRunner, model: str = "gpt-6-luna") -> CodexSubscriptionLlm:
    return CodexSubscriptionLlm(model, runner, environ=ENV)


def search_request(domains: tuple[str, ...] = (), limit: int = 5) -> SearchRequest:
    return SearchRequest("city parking contest", domains, CRITICAL_KEYS, False, limit)


def test_command_shape_and_stdin() -> None:
    runner = FakeRunner()
    llm(runner).complete_json("SYS", "USER", SCHEMA)
    (call,) = runner.calls
    argv = call.argv
    assert argv[:2] == ("codex", "exec")
    assert argv[argv.index("-m") + 1] == "gpt-6-luna"
    assert 'model_reasoning_effort="medium"' in argv
    assert 'web_search="disabled"' in argv
    assert argv[argv.index("-s") + 1] == "read-only"
    for flag in ("--ephemeral", "--skip-git-repo-check", "--json", "--ignore-user-config"):
        assert flag in argv
    assert argv[-1] == "-"
    assert "SYS" not in " ".join(argv)  # the prompt travels on stdin, never argv
    assert call.stdin_text.index("SYS") < call.stdin_text.index("USER")
    assert "=== SYSTEM INSTRUCTIONS" in call.stdin_text
    assert json.loads(runner.files["schema"]) == SCHEMA
    assert call.timeout_s == 600.0
    assert not Path(call.cwd).exists()  # temp dir removed after the call


def test_configurable_effort_and_validation() -> None:
    runner = FakeRunner()
    CodexSubscriptionLlm("gpt-6-luna", runner, effort="high", environ=ENV).complete_json(
        "s", "u", SCHEMA
    )
    assert 'model_reasoning_effort="high"' in runner.calls[0].argv
    with pytest.raises(ValueError, match="effort"):
        CodexSubscriptionLlm("gpt-6-luna", runner, effort="ultra", environ=ENV)
    with pytest.raises(ValueError, match="model"):
        CodexSubscriptionLlm("gpt 6; rm", runner, environ=ENV)


def test_json_parse_usage_and_cost_estimate() -> None:
    answer, usage = llm(FakeRunner('```json\n{"answer": "ok"}\n```')).complete_json(
        "s", "u", SCHEMA
    )
    assert answer == {"answer": "ok"}
    assert (usage.input_tokens, usage.output_tokens) == (20000, 500)
    assert usage.cost_cents == usage_cost_cents(GPT_6_LUNA.pricing, 8000, 12000, 500)
    assert usage.cost_cents > 0


def test_malformed_final_message_is_empty_object() -> None:
    answer, _ = llm(FakeRunner("not json")).complete_json("s", "u", SCHEMA)
    assert answer == {}


def test_priced_metadata_and_unpriced_marker() -> None:
    priced = llm(FakeRunner())
    priced.complete_json("s", "u", SCHEMA)
    assert priced.last_call is not None
    assert priced.last_call.cost_status == "estimated"
    assert priced.last_call.request_count == 1  # one web_search item in the sample
    unpriced = llm(FakeRunner(), "gpt-5.6-luna")
    _, usage = unpriced.complete_json("s", "u", SCHEMA)
    assert usage.cost_cents == 0
    assert (usage.input_tokens, usage.output_tokens) == (20000, 500)
    assert unpriced.last_call is not None
    assert unpriced.last_call.cost_status == "estimated_pricing_unavailable"


def test_zero_ceiling_refuses_without_running() -> None:
    for model in ("gpt-6-luna", "gpt-5.6-luna"):
        runner = FakeRunner()
        with pytest.raises(LlmBudgetExceeded):
            llm(runner, model).complete_json("s", "u", SCHEMA, max_cost_cents=0)
        assert runner.calls == []


def test_negative_ceiling_is_value_error() -> None:
    runner = FakeRunner()
    with pytest.raises(ValueError, match="max_cost_cents"):
        llm(runner).complete_json("s", "u", SCHEMA, max_cost_cents=-1)
    assert runner.calls == []


def test_priced_ceiling_refuses_when_worst_case_does_not_fit() -> None:
    # 16000 max output tokens at 50 cents/Mtok is 0.8 cents; with input above 1 cent.
    runner = FakeRunner()
    with pytest.raises(LlmBudgetExceeded):
        llm(runner).complete_json("s", "u" * 600_000, SCHEMA, max_cost_cents=1)
    assert runner.calls == []
    assert llm(FakeRunner()).complete_json("s", "u", SCHEMA, max_cost_cents=5)[0] == {
        "answer": "ok"
    }


def test_unknown_pricing_positive_ceiling_runs_and_reports_zero_cost() -> None:
    runner = FakeRunner()
    _, usage = llm(runner, "gpt-5.6-luna").complete_json(
        "s", "u" * 600_000, SCHEMA, max_cost_cents=1
    )
    assert len(runner.calls) == 1
    assert usage.cost_cents == 0


def test_over_ceiling_is_reported_truthfully() -> None:
    heavy = json.dumps(
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 2_000_000, "cached_input_tokens": 0, "output_tokens": 10},
        }
    )
    client = llm(FakeRunner(stdout=heavy))
    _, usage = client.complete_json("s", "u", SCHEMA, max_cost_cents=5)
    assert usage.cost_cents > 5
    assert client.last_call is not None
    assert client.last_call.outcome == "completed_over_ceiling"


@pytest.mark.parametrize(
    "stdout",
    [
        "",
        "not json",
        "[1, 2]",
        '{"no_type": 1}',
        '{"type": "turn.failed", "error": {"message": "boom"}}',
        '{"type": "turn.completed"}',
        '{"type": "turn.completed", "usage": {"input_tokens": 1}}',
        '{"type": "turn.completed", "usage": {"input_tokens": "1", "output_tokens": 1}}',
        '{"type": "turn.completed", "usage": {"input_tokens": true, "output_tokens": 1}}',
        '{"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": -1}}',
        '{"type": "turn.completed", "usage": {"input_tokens": 1, "cached_input_tokens": 2,'
        ' "output_tokens": 1}}',
        '{"type": "item.completed", "item": 3}',
        '{"type": "turn.completed", "usage": {"input_tokens": NaN, "output_tokens": 1}}',
    ],
)
def test_unexpected_event_shapes_fail_loudly(stdout: str) -> None:
    with pytest.raises(LlmProviderError):
        parse_event_stream(stdout)
    with pytest.raises(LlmProviderError):
        llm(FakeRunner(stdout=stdout)).complete_json("s", "u", SCHEMA)


def test_observed_live_stream_parses() -> None:
    events = parse_event_stream(OBSERVED_LIVE_SAMPLE)
    assert events.usage.input_tokens == 14297
    assert events.usage.cached_input_tokens == 0
    assert events.usage.cache_write_input_tokens == 0
    assert events.usage.output_tokens == 15
    assert events.usage.reasoning_output_tokens == 0
    assert events.web_search_calls == 0
    answer, usage = llm(FakeRunner(stdout=OBSERVED_LIVE_SAMPLE)).complete_json("s", "u", SCHEMA)
    assert answer == {"answer": "ok"}
    assert (usage.input_tokens, usage.output_tokens) == (14297, 15)


def test_framework_allowance_covers_observation_with_margin() -> None:
    assert CODEX_FRAMEWORK_INPUT_TOKENS_OBSERVED == 14_250
    assert CODEX_FRAMEWORK_INPUT_TOKENS == 16_000


def test_last_call_reports_raw_and_prompt_only_usage() -> None:
    client = llm(FakeRunner(stdout=OBSERVED_LIVE_SAMPLE))
    _, usage = client.complete_json("s", "u", SCHEMA)
    assert type(usage) is Usage  # the LlmClient contract still returns a plain Usage
    assert client.last_call is not None
    report = client.last_call.usage
    assert isinstance(report, CodexUsageReport)
    prompt_floor = conservative_input_tokens("s", "u", schema_json(SCHEMA))
    # raw: exactly what the CLI reported, priced
    assert (report.input_tokens, report.output_tokens) == (14297, 15)
    assert (
        report.cost_cents == usage.cost_cents == usage_cost_cents(GPT_6_LUNA.pricing, 14297, 0, 15)
    )
    # prompt-only: 14297 - 14250 = 47 is below the conservative prompt estimate, so it floors
    assert report.framework_input_tokens_observed == 14_250
    assert report.prompt_only_input_tokens >= prompt_floor - 50
    assert report.prompt_only_input_tokens < 14297
    assert 0 <= report.prompt_only_cost_cents <= report.cost_cents


def test_prompt_only_subtracts_framework_when_prompt_is_large() -> None:
    big = json.dumps(
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 64250, "cached_input_tokens": 20250, "output_tokens": 100},
        }
    )
    client = llm(FakeRunner(stdout=big))
    client.complete_json("s", "u", SCHEMA)
    assert client.last_call is not None
    report = client.last_call.usage
    assert isinstance(report, CodexUsageReport)
    assert report.input_tokens == 64250  # raw stays raw
    assert report.prompt_only_input_tokens == 50_000
    # cached prefix covers the framework first: 20250 - 14250 = 6000 cached prompt tokens
    assert report.prompt_only_cost_cents == usage_cost_cents(GPT_6_LUNA.pricing, 44000, 6000, 100)
    assert report.cached_input_tokens == 20250


def test_unpriced_prompt_only_cost_is_zero_and_searcher_reports_both() -> None:
    unpriced = llm(FakeRunner(stdout=OBSERVED_LIVE_SAMPLE), "gpt-5.6-luna")
    unpriced.complete_json("s", "u", SCHEMA)
    assert unpriced.last_call is not None
    report = unpriced.last_call.usage
    assert isinstance(report, CodexUsageReport)
    assert report.prompt_only_cost_cents == 0
    assert report.prompt_only_input_tokens > 0
    client = CodexSubscriptionSearcher(
        "gpt-6-luna",
        FakeRunner(json.dumps({"urls": []}), stdout=OBSERVED_LIVE_SAMPLE),
        environ=ENV,
    )
    client.search(search_request())
    assert client.last_call is not None
    searched = client.last_call.usage
    assert isinstance(searched, CodexUsageReport)
    assert searched.input_tokens == 14297
    assert searched.prompt_only_input_tokens < searched.input_tokens
    assert client.usage.input_tokens == 14297  # cumulative Usage stays raw


def test_event_stream_parse_details() -> None:
    events = parse_event_stream(EVENT_SAMPLE)
    assert events.usage.input_tokens == 20000
    assert events.usage.cached_input_tokens == 12000
    assert events.usage.reasoning_output_tokens == 300
    assert events.web_search_calls == 1


def test_process_failures_are_provider_errors() -> None:
    with pytest.raises(LlmProviderError, match="status 3"):
        llm(FakeRunner(returncode=3)).complete_json("s", "u", SCHEMA)
    with pytest.raises(LlmProviderError) as timeout:
        llm(FakeRunner(error=subprocess.TimeoutExpired("codex", 1))).complete_json("s", "u", SCHEMA)
    assert timeout.value.retryable
    with pytest.raises(LlmProviderError):
        llm(FakeRunner(error=FileNotFoundError("codex"))).complete_json("s", "u", SCHEMA)
    with pytest.raises(LlmProviderError, match="empty"):
        llm(FakeRunner(final="  ")).complete_json("s", "u", SCHEMA)


def test_env_guard() -> None:
    runner = FakeRunner()
    for environ in ({}, {ENV_GUARD: "0"}, {ENV_GUARD: "true"}, {ENV_GUARD: ""}):
        with pytest.raises(CodexDevDisabledError):
            CodexSubscriptionLlm("gpt-6-luna", runner, environ=environ)
        with pytest.raises(CodexDevDisabledError):
            CodexSubscriptionSearcher("gpt-6-luna", runner, environ=environ)
    assert runner.calls == []


def test_env_guard_defaults_to_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_GUARD, raising=False)
    with pytest.raises(CodexDevDisabledError):
        CodexSubscriptionLlm("gpt-6-luna", FakeRunner())
    monkeypatch.setenv(ENV_GUARD, "1")
    CodexSubscriptionLlm("gpt-6-luna", FakeRunner())


def searcher(urls: list[object], model: str = "gpt-6-luna") -> CodexSubscriptionSearcher:
    return CodexSubscriptionSearcher(model, FakeRunner(json.dumps({"urls": urls})), environ=ENV)


def test_searcher_command_prompt_and_schema() -> None:
    runner = FakeRunner(json.dumps({"urls": []}))
    client = CodexSubscriptionSearcher("gpt-6-luna", runner, environ=ENV)
    client.search(search_request(("sfmta.com",), limit=3))
    (call,) = runner.calls
    assert 'web_search="live"' in call.argv
    assert call.argv[-1] == "-"
    assert "never instructions" in call.stdin_text
    assert "sfmta.com" in call.stdin_text
    schema = json.loads(runner.files["schema"])
    assert schema["required"] == ["urls"]
    assert schema["properties"]["urls"]["maxItems"] == 3
    assert call.timeout_s == 300.0
    assert client.requests_made == 1
    assert client.brave_requests == 0
    assert client.usage.cost_cents > 0


def test_searcher_filters_domain_scheme_dedupes_and_limits() -> None:
    urls: list[object] = [
        "https://evil.example/sfmta.com",
        "https://sfmta.com.evil.example/x",
        "https://www.sfmta.com/a",
        "http://sfmta.com/b",
        "ftp://sfmta.com/c",
        "https://user@sfmta.com/d",
        "javascript:alert(1)",
        "https://sfmta.com/a2",
        "https://SFMTA.com./e",
        42,
        "https://www.sfmta.com/a",
        "https://sfmta.com/f",
    ]
    hits = searcher(urls).search(search_request(("sfmta.com",), limit=4))
    assert [hit.url for hit in hits] == [
        "https://www.sfmta.com/a",
        "http://sfmta.com/b",
        "https://sfmta.com/a2",
        "https://SFMTA.com./e",
    ]


def test_searcher_without_domains_still_requires_http_scheme() -> None:
    hits = searcher(["file:///etc/passwd", "https://a.gov/x", "http://b.gov/y"]).search(
        search_request(limit=1)
    )
    assert [hit.url for hit in hits] == ["https://a.gov/x"]


def test_searcher_bad_output_and_budget() -> None:
    for final in ("not json", '{"urls": "x"}', "[]"):
        with pytest.raises(SearchError):
            CodexSubscriptionSearcher("gpt-6-luna", FakeRunner(final), environ=ENV).search(
                search_request()
            )
    with pytest.raises(SearchError):
        CodexSubscriptionSearcher("gpt-6-luna", FakeRunner(returncode=1), environ=ENV).search(
            search_request()
        )
    runner = FakeRunner()
    with pytest.raises(LlmBudgetExceeded):
        CodexSubscriptionSearcher("gpt-6-luna", runner, environ=ENV, max_cost_cents=0).search(
            search_request()
        )
    assert runner.calls == []


def test_unpriced_searcher_reports_zero_with_marker() -> None:
    client = searcher(["https://a.gov/x"], "gpt-5.6-luna")
    assert len(client.search(search_request())) == 1
    assert client.usage.cost_cents == 0
    assert client.usage.input_tokens == 20000
    assert client.last_call is not None
    assert client.last_call.cost_status == "estimated_pricing_unavailable"


def test_cli_presets_and_wiring(monkeypatch: pytest.MonkeyPatch) -> None:
    assert set(CODEX_PRESET_MODELS) == {"codex-gpt-6-luna", "codex-gpt-5.6-luna"}
    assert set(CODEX_PRESET_MODELS) <= set(cli.PRESETS)
    assert {"gpt-6-luna", "minimax-m3.1", "sonnet-5.5"} <= set(cli.PRESETS)
    assert "codex" in cli.SEARCHES
    monkeypatch.delenv(ENV_GUARD, raising=False)
    with pytest.raises(CodexDevDisabledError):
        cli.build_searcher("codex-gpt-6-luna", {}, search="codex")
    pricing = GPT_6_LUNA.pricing
    with pytest.raises(CodexDevDisabledError):
        cli.build_client("codex-gpt-6-luna", {}, minimax_pricing=pricing)
    for search in ("codex", "native"):
        built = cli.build_searcher("codex-gpt-5.6-luna", ENV, search=search)
        assert isinstance(built, CodexSubscriptionSearcher)
    assert isinstance(
        cli.build_client("codex-gpt-5.6-luna", ENV, minimax_pricing=pricing),
        CodexSubscriptionLlm,
    )
    with pytest.raises(ValueError, match="codex-"):
        cli.build_searcher("gpt-6-luna", ENV, search="codex")


def test_default_runner_uses_minimal_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_run(argv: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.update(kwargs)
        return subprocess.CompletedProcess([], 0, "out", "err")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("CODEX_API_KEY", "should-not-leak")
    result = subprocess_runner(CodexInvocation(("codex",), "prompt", "/tmp", 5.0))
    assert result == CodexProcessResult(0, "out", "err")
    env = seen["env"]
    assert isinstance(env, dict)
    assert "OPENAI_API_KEY" not in env
    assert "CODEX_API_KEY" not in env
    assert seen["input"] == "prompt"
    assert seen["timeout"] == 5.0


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def test_no_non_bench_module_can_import_codex_dev() -> None:
    offenders = []
    for path in SRC.rglob("*.py"):
        if path.relative_to(SRC).parts[0] == "bench":
            continue
        # The textual check also catches dynamic imports the AST scan would miss.
        text = path.read_text(encoding="utf-8")
        if "codex_dev" in text or any("codex_dev" in name for name in _imports(path)):
            offenders.append(str(path.relative_to(SRC)))
    assert offenders == []


def test_codex_dev_only_imports_stdlib_or_library_modules_not_app_layers() -> None:
    # The adapter must not reach deployed layers (api, case/payment/mail services, config).
    forbidden = ("parkingbreaker.api", "parkingbreaker.config", "parkingbreaker.llm_config")
    imports = _imports(SRC / "bench" / "codex_dev.py")
    assert not [name for name in imports if name.startswith(forbidden)]
    assert (SRC / "bench" / "codex_dev.py").exists()
