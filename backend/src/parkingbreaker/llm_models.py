"""Pure model bench configuration and exact, upward-rounded money calculations.

Sonnet/Luna prices are the supplied 2026-10-13 snapshot; MiniMax pricing uses the
2026-10-19 snapshot (https://platform.minimax.io/docs/pricing/overview).
Prices must be re-checked against provider pages before production.
"""

from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Literal

from parkingbreaker.llm import LlmBudgetExceeded

MIN_USEFUL_OUTPUT_TOKENS = 1024
_MTOK = 1_000_000


@dataclass(frozen=True)
class ModelPricing:
    input_cents_per_mtok: Decimal
    cached_input_cents_per_mtok: Decimal
    output_cents_per_mtok: Decimal

    def __post_init__(self) -> None:
        for rate in (
            self.input_cents_per_mtok,
            self.cached_input_cents_per_mtok,
            self.output_cents_per_mtok,
        ):
            if not isinstance(rate, Decimal) or not rate.is_finite() or rate < 0:
                raise ValueError("pricing must use finite non-negative Decimal cents")
        if self.output_cents_per_mtok == 0:
            raise ValueError("output pricing must be positive")


@dataclass(frozen=True)
class LlmModelConfig:
    provider: Literal["anthropic", "openai", "mimo", "deepseek", "gemini"]
    model_id: str
    pricing: ModelPricing
    max_output_tokens: int
    effort: str | None = None
    structured_output: bool = True
    base_url: str | None = None
    pricing_source: str | None = None
    pricing_version: str | None = None

    def __post_init__(self) -> None:
        if (
            self.provider not in {"anthropic", "openai", "mimo", "deepseek", "gemini"}
            or not self.model_id
        ):
            raise ValueError("provider and model_id must identify a model")
        _nonnegative_integer(self.max_output_tokens, "max_output_tokens")
        if self.max_output_tokens == 0:
            raise ValueError("max_output_tokens must be positive")
        if not isinstance(self.pricing, ModelPricing):
            raise ValueError("pricing is required")
        if (self.pricing_source is None) != (self.pricing_version is None):
            raise ValueError("pricing source and version must be set together")
        if self.provider == "mimo":
            if self.model_id not in MIMO_TEXT_MODEL_IDS:
                raise ValueError("mimo requires a documented current text model id")
            if self.base_url != MIMO_BASE_URL:
                raise ValueError("mimo base_url is fixed to the ordinary API endpoint")
            if not self.pricing_source or not self.pricing_version:
                raise ValueError("mimo requires explicit pricing source and version")
            if self.max_output_tokens > MIMO_MAX_OUTPUT_TOKENS:
                raise ValueError("mimo output limit exceeds the provider maximum")
        if self.provider == "deepseek":
            if self.model_id not in DEEPSEEK_MODEL_IDS:
                raise ValueError("deepseek requires a documented model id")
            if self.base_url != DEEPSEEK_BASE_URL:
                raise ValueError("deepseek base_url is fixed to the API endpoint")
            if not self.pricing_source or not self.pricing_version:
                raise ValueError("deepseek requires explicit pricing source and version")
            if self.max_output_tokens > DEEPSEEK_MAX_OUTPUT_TOKENS:
                raise ValueError("deepseek output limit exceeds the provider maximum")
        if self.provider == "gemini":
            if self.model_id not in GEMINI_MODEL_IDS:
                raise ValueError("gemini requires a documented model id")
            if self.base_url != GEMINI_BASE_URL:
                raise ValueError("gemini base_url is fixed to the OpenAI compatibility endpoint")
            if not self.pricing_source or not self.pricing_version:
                raise ValueError("gemini requires explicit pricing source and version")
            if self.max_output_tokens > GEMINI_MAX_OUTPUT_TOKENS:
                raise ValueError("gemini output limit exceeds the provider maximum")


def _nonnegative_integer(value: int, name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _ceil(value: Fraction) -> int:
    return -(-value.numerator // value.denominator)


def usage_cost_cents(
    pricing: ModelPricing, input_tokens: int, cached_input_tokens: int, output_tokens: int
) -> int:
    """Input excludes cache reads; cache creation is charged at regular input price.

    No cache writes are requested by these adapters. Fraction conversion preserves
    Decimal rates exactly, independently of the caller's Decimal context precision.
    """
    for value in (input_tokens, cached_input_tokens, output_tokens):
        _nonnegative_integer(value, "tokens")
    return _ceil(
        (
            input_tokens * Fraction(pricing.input_cents_per_mtok)
            + cached_input_tokens * Fraction(pricing.cached_input_cents_per_mtok)
            + output_tokens * Fraction(pricing.output_cents_per_mtok)
        )
        / _MTOK
    )


def worst_case_cents(config: LlmModelConfig, input_tokens: int, output_tokens: int) -> int:
    """Assume no cache discount (or the cache rate if it is more expensive)."""
    _nonnegative_integer(input_tokens, "input_tokens")
    _nonnegative_integer(output_tokens, "output_tokens")
    return _ceil(
        (
            input_tokens
            * max(
                Fraction(config.pricing.input_cents_per_mtok),
                Fraction(config.pricing.cached_input_cents_per_mtok),
            )
            + output_tokens * Fraction(config.pricing.output_cents_per_mtok)
        )
        / _MTOK
    )


def output_cap_for(config: LlmModelConfig, input_tokens: int, max_cost_cents: int) -> int | None:
    """Largest affordable output count, before applying the model's output limit."""
    _nonnegative_integer(input_tokens, "input_tokens")
    validate_ceiling(max_cost_cents)
    remaining = max_cost_cents * _MTOK - input_tokens * max(
        Fraction(config.pricing.input_cents_per_mtok),
        Fraction(config.pricing.cached_input_cents_per_mtok),
    )
    cap = remaining // Fraction(config.pricing.output_cents_per_mtok)
    return cap if max_cost_cents > 0 and cap >= 1 else None


def validate_ceiling(max_cost_cents: int | None) -> None:
    if max_cost_cents is not None:
        _nonnegative_integer(max_cost_cents, "max_cost_cents")


def request_output_tokens(
    config: LlmModelConfig, input_tokens: int, max_cost_cents: int | None
) -> int:
    """A capped call needs at least 1024 tokens; uncapped calls use the model limit."""
    validate_ceiling(max_cost_cents)
    if max_cost_cents is None:
        return config.max_output_tokens
    cap = output_cap_for(config, input_tokens, max_cost_cents)
    if cap is None or min(cap, config.max_output_tokens) < MIN_USEFUL_OUTPUT_TOKENS:
        raise LlmBudgetExceeded("Ceiling cannot fund 1024 useful output tokens")
    return min(cap, config.max_output_tokens)


SONNET_5_5 = LlmModelConfig(
    "anthropic",
    "claude-sonnet-5-5",
    ModelPricing(Decimal(200), Decimal(20), Decimal(1000)),
    max_output_tokens=16000,
    effort="medium",
)
GPT_6_LUNA = LlmModelConfig(
    "openai",
    "gpt-6-luna",
    ModelPricing(Decimal(10), Decimal(1), Decimal(50)),
    max_output_tokens=16000,
    effort="medium",
)
MINIMAX_PRICING = ModelPricing(Decimal(15), Decimal("1.5"), Decimal(60))
MINIMAX_PRICING_SOURCE = "https://platform.minimax.io/docs/pricing/overview"
MINIMAX_PRICING_VERSION = "2026-10-19"
MINIMAX_MODEL_ID = "MiniMax-M3.1-Flash"
MINIMAX_BASE_URL = "https://api.minimax.io/anthropic"

# Xiaomi MiMo ordinary Chat Completions API. Token Plan and custom endpoints are
# deliberately not representable here.
MIMO_BASE_URL = "https://api.xiaomimimo.com/v1"
MIMO_TEXT_MODEL_IDS = frozenset({"mimo-v2.6-flash", "mimo-v2.6-pro", "mimo-v2.6-pro-ultraspeed"})
MIMO_MAX_OUTPUT_TOKENS = 131_072
MIMO_CONTEXT_TOKENS = 1_048_576
MIMO_DEFAULT_OUTPUT_CAP = 16_000


def minimax_config(pricing: ModelPricing | None = None) -> LlmModelConfig:
    """The MiniMax preset uses the 2026-10-19 snapshot unless explicit pricing is supplied."""
    using_published_snapshot = pricing is None
    return LlmModelConfig(
        "anthropic",
        MINIMAX_MODEL_ID,
        pricing if pricing is not None else MINIMAX_PRICING,
        max_output_tokens=16000,
        effort="medium",
        structured_output=False,
        base_url=MINIMAX_BASE_URL,
        pricing_source=(
            MINIMAX_PRICING_SOURCE if using_published_snapshot else "caller_supplied_unverified"
        ),
        pricing_version=(
            MINIMAX_PRICING_VERSION if using_published_snapshot else "caller_supplied_unverified"
        ),
    )


def mimo_config(
    model_id: str,
    pricing: ModelPricing,
    *,
    pricing_source: str,
    pricing_version: str,
    max_output_tokens: int = MIMO_DEFAULT_OUTPUT_CAP,
) -> LlmModelConfig:
    """Explicit offline MiMo configuration; no implicit model, prices or qualification.

    The caller owns the pinned model choice. ``max_output_tokens`` is an operational
    request cap within the documented 131072 provider maximum; it never remaps the model.
    """
    return LlmModelConfig(
        "mimo",
        model_id,
        pricing,
        max_output_tokens=max_output_tokens,
        effort=None,
        structured_output=False,
        base_url=MIMO_BASE_URL,
        pricing_source=pricing_source,
        pricing_version=pricing_version,
    )


# DeepSeek API configuration (OpenAI-compatible Chat Completions endpoint).
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL_IDS = frozenset({"deepseek-flash", "deepseek-v4-pro"})
DEEPSEEK_MAX_OUTPUT_TOKENS = 16_000
DEEPSEEK_CONTEXT_TOKENS = 1_048_576
DEEPSEEK_PRICING_SOURCE = "https://api-docs.deepseek.com/quick_start/pricing/"
DEEPSEEK_PRICING_VERSION = "2026-11-08"

# Peak rate snapshots in cents per Mtok used for conservative spending ceilings:
# deepseek-flash: $0.30 input / $0.006 cache hit / $1.20 output
DEEPSEEK_FLASH_PRICING = ModelPricing(Decimal("30"), Decimal("0.6"), Decimal("120"))
# deepseek-v4-pro: $1.32 input / $0.044 cache hit / $3.96 output
DEEPSEEK_V4_PRO_PRICING = ModelPricing(Decimal("132"), Decimal("4.4"), Decimal("396"))


def deepseek_flash_config(pricing: ModelPricing | None = None) -> LlmModelConfig:
    return LlmModelConfig(
        "deepseek",
        "deepseek-flash",
        pricing if pricing is not None else DEEPSEEK_FLASH_PRICING,
        max_output_tokens=8192,
        structured_output=True,
        base_url=DEEPSEEK_BASE_URL,
        pricing_source=DEEPSEEK_PRICING_SOURCE,
        pricing_version=DEEPSEEK_PRICING_VERSION,
    )


def deepseek_v4_pro_config(pricing: ModelPricing | None = None) -> LlmModelConfig:
    return LlmModelConfig(
        "deepseek",
        "deepseek-v4-pro",
        pricing if pricing is not None else DEEPSEEK_V4_PRO_PRICING,
        max_output_tokens=8192,
        structured_output=True,
        base_url=DEEPSEEK_BASE_URL,
        pricing_source=DEEPSEEK_PRICING_SOURCE,
        pricing_version=DEEPSEEK_PRICING_VERSION,
    )


# Google Gemini Paid Standard API (OpenAI-compatible Chat Completions endpoint).
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
GEMINI_MODEL_IDS = frozenset({"gemini-3.8-flash", "gemini-3.5-flash-lite"})
GEMINI_MAX_OUTPUT_TOKENS = 16_000
GEMINI_CONTEXT_TOKENS = 1_048_576
GEMINI_PRICING_SOURCE = "https://ai.google.dev/gemini-api/docs/pricing"
GEMINI_PRICING_VERSION = "2026-11-08"

# Paid Standard rates in cents per Mtok:
# gemini-3.8-flash: $0.75 input / $0.1875 cache / $3.75 output
GEMINI_3_8_FLASH_PRICING = ModelPricing(Decimal("75"), Decimal("18.75"), Decimal("375"))
# gemini-3.5-flash-lite: $0.30 input / $0.075 cache / $2.50 output
GEMINI_3_5_FLASH_LITE_PRICING = ModelPricing(Decimal("30"), Decimal("7.5"), Decimal("250"))


def gemini_3_8_flash_config(pricing: ModelPricing | None = None) -> LlmModelConfig:
    return LlmModelConfig(
        "gemini",
        "gemini-3.8-flash",
        pricing if pricing is not None else GEMINI_3_8_FLASH_PRICING,
        max_output_tokens=8192,
        structured_output=True,
        base_url=GEMINI_BASE_URL,
        pricing_source=GEMINI_PRICING_SOURCE,
        pricing_version=GEMINI_PRICING_VERSION,
    )


def gemini_3_5_flash_lite_config(pricing: ModelPricing | None = None) -> LlmModelConfig:
    return LlmModelConfig(
        "gemini",
        "gemini-3.5-flash-lite",
        pricing if pricing is not None else GEMINI_3_5_FLASH_LITE_PRICING,
        max_output_tokens=8192,
        structured_output=True,
        base_url=GEMINI_BASE_URL,
        pricing_source=GEMINI_PRICING_SOURCE,
        pricing_version=GEMINI_PRICING_VERSION,
    )
