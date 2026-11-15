from dataclasses import FrozenInstanceError, replace
from decimal import Decimal, localcontext

import pytest

from parkingbreaker import llm_config
from parkingbreaker.llm import LlmBudgetExceeded, conservative_input_tokens
from parkingbreaker.llm_models import (
    GPT_6_LUNA,
    MINIMAX_BASE_URL,
    MINIMAX_MODEL_ID,
    MINIMAX_PRICING,
    MINIMAX_PRICING_SOURCE,
    MINIMAX_PRICING_VERSION,
    SONNET_5_5,
    LlmModelConfig,
    ModelPricing,
    minimax_config,
    output_cap_for,
    request_output_tokens,
    usage_cost_cents,
    worst_case_cents,
)
from parkingbreaker.research.fakes import FakeLlmClient


def test_prices_and_presets() -> None:
    assert SONNET_5_5.model_id == "claude-sonnet-5-5"
    assert SONNET_5_5.pricing == ModelPricing(Decimal(200), Decimal(20), Decimal(1000))
    assert GPT_6_LUNA.model_id == "gpt-6-luna"
    assert GPT_6_LUNA.pricing == ModelPricing(Decimal(10), Decimal(1), Decimal(50))
    assert ModelPricing(Decimal(15), Decimal("1.5"), Decimal(60)) == MINIMAX_PRICING
    for obj, name, value in [
        (SONNET_5_5, "model_id", "changed"),
        (SONNET_5_5.pricing, "output_cents_per_mtok", Decimal(1)),
    ]:
        with pytest.raises(FrozenInstanceError):
            setattr(obj, name, value)


def test_minimax_config_default_and_overrides() -> None:
    config = minimax_config()
    assert config.model_id == "MiniMax-M3.1-Flash"
    assert config.model_id == MINIMAX_MODEL_ID
    assert config.base_url == MINIMAX_BASE_URL
    assert config.pricing == MINIMAX_PRICING
    assert config.pricing_source == MINIMAX_PRICING_SOURCE
    assert config.pricing_version == MINIMAX_PRICING_VERSION
    assert not config.structured_output
    assert config.effort == "medium"
    assert config.max_output_tokens == 16000

    override_pricing = ModelPricing(Decimal(200), Decimal(20), Decimal(1000))
    config_override = minimax_config(override_pricing)
    assert config_override.pricing == override_pricing
    assert config_override.model_id == "MiniMax-M3.1-Flash"
    assert config_override.pricing_source == "caller_supplied_unverified"
    assert config_override.pricing_version == "caller_supplied_unverified"

    same_price_override = minimax_config(MINIMAX_PRICING)
    assert same_price_override.pricing == MINIMAX_PRICING
    assert same_price_override.pricing_source == "caller_supplied_unverified"


@pytest.mark.parametrize(
    ("input_tokens", "output_tokens", "cents"),
    [(0, 0, 0), (1, 0, 1), (0, 1, 1), (5000, 0, 1), (5001, 0, 2), (1_000_000, 1_000_000, 1200)],
)
def test_rounds_total_cost_up(input_tokens: int, output_tokens: int, cents: int) -> None:
    assert worst_case_cents(SONNET_5_5, input_tokens, output_tokens) == cents


def test_fractional_prices_ignore_decimal_context() -> None:
    config = replace(
        SONNET_5_5,
        pricing=ModelPricing(Decimal("0.0123456791"), Decimal("0.001"), Decimal("0.987654321")),
    )
    with localcontext() as context:
        context.prec = 2
        assert worst_case_cents(config, 1_000_000, 1_000_000) == 2
        assert output_cap_for(config, 1_000_000, 1) == 999999
        assert worst_case_cents(config, 1_000_000, 999999) == 1
        assert worst_case_cents(config, 1_000_000, 1_000_000) == 2


def test_cache_cost_is_rounded_once() -> None:
    assert usage_cost_cents(SONNET_5_5.pricing, 1, 1, 1) == 1
    assert usage_cost_cents(SONNET_5_5.pricing, 15000, 100000, 2000) == 7
    assert usage_cost_cents(MINIMAX_PRICING, 1_000_000, 1_000_000, 1_000_000) == 77


@pytest.mark.parametrize("config", [SONNET_5_5, GPT_6_LUNA, minimax_config()])
@pytest.mark.parametrize("inputs", [0, 1, 500, 1234567])
@pytest.mark.parametrize("ceiling", [0, 1, 3, 200])
def test_cap_is_largest_affordable_count(config: LlmModelConfig, inputs: int, ceiling: int) -> None:
    cap = output_cap_for(config, inputs, ceiling)
    if cap is None:
        assert ceiling == 0 or worst_case_cents(config, inputs, 1) > ceiling
    else:
        assert worst_case_cents(config, inputs, cap) <= ceiling
        assert worst_case_cents(config, inputs, cap + 1) > ceiling


def test_cap_math_and_request_limit() -> None:
    assert output_cap_for(SONNET_5_5, 1000, 3) == 2800
    assert request_output_tokens(SONNET_5_5, 1000, 3) == 2800
    assert request_output_tokens(SONNET_5_5, 1000, 100) == 16000
    assert request_output_tokens(SONNET_5_5, 1000, None) == 16000
    with pytest.raises(LlmBudgetExceeded):
        request_output_tokens(SONNET_5_5, 1000, 1)
    with pytest.raises(LlmBudgetExceeded):
        request_output_tokens(replace(SONNET_5_5, max_output_tokens=512), 0, 100)


def test_cache_rate_above_regular_rate_is_conservative() -> None:
    config = replace(SONNET_5_5, pricing=ModelPricing(Decimal(1), Decimal(200), Decimal(1000)))
    assert output_cap_for(config, 1000, 3) == 2800
    assert worst_case_cents(config, 1000, 2800) == 3


@pytest.mark.parametrize("ceiling", [-1, True])
def test_invalid_ceiling(ceiling: int) -> None:
    with pytest.raises(ValueError, match="max_cost_cents"):
        output_cap_for(SONNET_5_5, 0, ceiling)


@pytest.mark.parametrize("rate", [Decimal("NaN"), Decimal("Infinity"), Decimal(-1)])
def test_invalid_pricing(rate: Decimal) -> None:
    with pytest.raises(ValueError, match="pricing"):
        ModelPricing(rate, Decimal(0), Decimal(1))


def test_zero_output_rate_rejected() -> None:
    with pytest.raises(ValueError, match="output pricing"):
        ModelPricing(Decimal(0), Decimal(0), Decimal(0))


def test_utf8_estimate_rounds_up() -> None:
    assert conservative_input_tokens("é", "🙂", "x") == 504


@pytest.mark.parametrize(
    ("preset", "variable"),
    [
        ("sonnet-5.5", "ANTHROPIC_API_KEY"),
        ("gpt-6-luna", "OPENAI_API_KEY"),
        ("minimax-m3.1", "MINIMAX_API_KEY"),
    ],
)
@pytest.mark.parametrize("value", [None, "", " "])
def test_missing_key_names_only_variable(preset: str, variable: str, value: str | None) -> None:
    environ = {"UNRELATED_KEY": "PRIVATE"}
    if value is not None:
        environ[variable] = value
    with pytest.raises(ValueError, match=variable) as failure:
        llm_config.build_llm_client(preset, environ, minimax_pricing=SONNET_5_5.pricing)
    assert str(failure.value) == f"Missing {variable}"


@pytest.mark.parametrize(
    ("preset", "variable", "constructor", "expected"),
    [
        ("sonnet-5.5", "ANTHROPIC_API_KEY", "AnthropicLlmClient", SONNET_5_5),
        ("gpt-6-luna", "OPENAI_API_KEY", "OpenAiLlmClient", GPT_6_LUNA),
        (
            "minimax-m3.1",
            "MINIMAX_API_KEY",
            "AnthropicLlmClient",
            minimax_config(SONNET_5_5.pricing),
        ),
        (
            "minimax-m3.1",
            "MINIMAX_API_KEY",
            "AnthropicLlmClient",
            minimax_config(),
        ),
    ],
)
def test_key_loading_builds_named_preset(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    preset: str,
    variable: str,
    constructor: str,
    expected: LlmModelConfig,
) -> None:
    calls: list[tuple[LlmModelConfig, str]] = []
    fake = FakeLlmClient({})

    def build(config: LlmModelConfig, api_key: str) -> FakeLlmClient:
        calls.append((config, api_key))
        return fake

    monkeypatch.setattr(llm_config, constructor, build)
    if expected.pricing != MINIMAX_PRICING or preset != "minimax-m3.1":
        built = llm_config.build_llm_client(
            preset, {variable: "PRIVATE"}, minimax_pricing=expected.pricing
        )
    else:
        built = llm_config.build_llm_client(preset, {variable: "PRIVATE"})
    assert built is fake
    assert calls == [(expected, "PRIVATE")]
    assert "PRIVATE" not in caplog.text


def test_key_loading_rejects_unknown_preset() -> None:
    with pytest.raises(ValueError, match="Unknown LLM preset"):
        llm_config.build_llm_client("invalid", {})


def _mimo_config() -> LlmModelConfig:
    from parkingbreaker.llm_models import mimo_config

    return mimo_config(
        "mimo-v2.6-flash",
        ModelPricing(Decimal("0.14"), Decimal("0.0028"), Decimal("0.28")),
        pricing_source="synthetic-source",
        pricing_version="synthetic-v1",
    )


def test_mimo_config_requires_explicit_documented_current_text_model() -> None:
    config = _mimo_config()
    assert config.provider == "mimo"
    assert config.model_id == "mimo-v2.6-flash"
    assert config.base_url == "https://api.xiaomimimo.com/v1"
    assert config.pricing_source == "synthetic-source"
    assert config.max_output_tokens == 16000
    assert not config.structured_output
    for model_id in ("mimo-v2.5", "mimo-v2.6-unknown", "MiniMax-M3.1-Flash"):
        with pytest.raises(ValueError, match="documented current text model"):
            replace(config, model_id=model_id)
    with pytest.raises(ValueError, match="output limit"):
        replace(config, max_output_tokens=131_073)
    with pytest.raises(ValueError, match="base_url"):
        replace(config, base_url="https://mimo.mi.com/tokenplan")
    with pytest.raises(ValueError, match="pricing source"):
        replace(config, pricing_source="")


def test_mimo_factory_requires_explicit_configuration_before_credentials() -> None:
    with pytest.raises(ValueError, match="qualification configuration"):
        llm_config.build_llm_client("mimo", {})
    with pytest.raises(ValueError, match="qualification configuration"):
        llm_config.build_llm_client("mimo", {"MIMO_API_KEY": "PRIVATE"}, mimo_model_config=None)
    with pytest.raises(ValueError, match="Missing MIMO_API_KEY") as failure:
        llm_config.build_llm_client("mimo", {}, mimo_model_config=_mimo_config())
    assert str(failure.value) == "Missing MIMO_API_KEY"


def test_mimo_factory_dispatches_explicit_config_with_dummy_credentials(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    config = _mimo_config()
    calls: list[tuple[LlmModelConfig, str]] = []

    def build(received: LlmModelConfig, key: str) -> object:
        calls.append((received, key))
        return object()

    monkeypatch.setattr(llm_config, "MiMoLlmClient", build)
    assert (
        llm_config.build_llm_client("mimo", {"MIMO_API_KEY": "PRIVATE"}, mimo_model_config=config)
        is not None
    )
    assert calls == [(config, "PRIVATE")]
    assert "PRIVATE" not in caplog.text
    with pytest.raises(ValueError, match="Invalid MIMO_API_KEY"):
        llm_config.build_llm_client(
            "mimo", {"MIMO_API_KEY": "PRIVATE KEY"}, mimo_model_config=config
        )


def test_deepseek_factory_dispatch_and_key_validation() -> None:
    from parkingbreaker.llm_deepseek import DeepSeekLlmClient

    with pytest.raises(ValueError, match="Missing DEEPSEEK_API_KEY"):
        llm_config.build_llm_client("deepseek-flash", {})
    client = llm_config.build_llm_client("deepseek-flash", {"DEEPSEEK_API_KEY": "sk-deepseek-test"})
    assert isinstance(client, DeepSeekLlmClient)
    client_pro = llm_config.build_llm_client(
        "deepseek-v4-pro", {"DEEPSEEK_API_KEY": "sk-deepseek-test"}
    )
    assert isinstance(client_pro, DeepSeekLlmClient)


def test_gemini_factory_dispatch_and_fallback_to_google_api_key() -> None:
    from parkingbreaker.llm_gemini import GeminiLlmClient

    with pytest.raises(ValueError, match="Missing GEMINI_API_KEY"):
        llm_config.build_llm_client("gemini-3.8-flash", {})
    client_google = llm_config.build_llm_client(
        "gemini-3.8-flash", {"GOOGLE_API_KEY": "AIzaSy-test"}
    )
    assert isinstance(client_google, GeminiLlmClient)
    client_gemini = llm_config.build_llm_client(
        "gemini-3.8-flash", {"GEMINI_API_KEY": "AIzaSy-test"}
    )
    assert isinstance(client_gemini, GeminiLlmClient)


def test_resolve_role_preset() -> None:
    from parkingbreaker.oplog.ai_journal import AiWorker

    # When PB_LLM_PRESET is set, it overrides for uniform compatibility:
    env_uniform = {"PB_LLM_PRESET": "minimax-m3.1"}
    assert llm_config.resolve_role_preset(AiWorker.INTAKE_READING, env_uniform) == "minimax-m3.1"
    assert llm_config.resolve_role_preset(AiWorker.RESEARCH, env_uniform) == "minimax-m3.1"
    assert llm_config.resolve_role_preset(AiWorker.DRAFTING, env_uniform) == "minimax-m3.1"

    # Default role routing:
    env_empty: dict[str, str] = {}
    assert llm_config.resolve_role_preset(AiWorker.INTAKE_READING, env_empty) == "gemini-3.8-flash"
    assert llm_config.resolve_role_preset(AiWorker.RESEARCH, env_empty) == "deepseek-v4-pro"
    assert llm_config.resolve_role_preset(AiWorker.DRAFTING, env_empty) == "deepseek-flash"

    # Role-specific overrides:
    env_override = {
        "PB_LLM_INTAKE_PRESET": "gemini-3.5-flash-lite",
        "PB_LLM_RESEARCH_PRESET": "deepseek-flash",
    }
    assert (
        llm_config.resolve_role_preset(AiWorker.INTAKE_READING, env_override)
        == "gemini-3.5-flash-lite"
    )
    assert llm_config.resolve_role_preset(AiWorker.RESEARCH, env_override) == "deepseek-flash"
    assert llm_config.resolve_role_preset(AiWorker.DRAFTING, env_override) == "deepseek-flash"
