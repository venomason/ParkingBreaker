"""Credential I/O edge. Callers supply their environment mapping; keys are never logged."""

from collections.abc import Mapping

from parkingbreaker.llm import LlmClient
from parkingbreaker.llm_anthropic import AnthropicLlmClient
from parkingbreaker.llm_deepseek import DeepSeekLlmClient
from parkingbreaker.llm_gemini import GeminiLlmClient
from parkingbreaker.llm_mimo import MiMoLlmClient
from parkingbreaker.llm_models import (
    GPT_6_LUNA,
    SONNET_5_5,
    LlmModelConfig,
    ModelPricing,
    deepseek_flash_config,
    deepseek_v4_pro_config,
    gemini_3_5_flash_lite_config,
    gemini_3_8_flash_config,
    minimax_config,
)
from parkingbreaker.llm_openai import OpenAiLlmClient
from parkingbreaker.oplog.ai_journal import AiWorker
from parkingbreaker.provider_credentials import valid_header_credential

DEFAULT_INTAKE_PRESET = "deepseek-flash"
DEFAULT_PACKET_CHECK_PRESET = "gemini-3.8-flash"
DEFAULT_RESEARCH_PRESET = "deepseek-v4-pro"
DEFAULT_DRAFTING_PRESET = "deepseek-flash"


def resolve_role_preset(worker: AiWorker, environ: Mapping[str, str]) -> str:
    """Resolve preset for an AiWorker role.

    If PB_LLM_PRESET is set, it serves as the uniform preset for backward compatibility.
    Otherwise, role-specific environment variables or defaults apply:
    - INTAKE_READING: PB_LLM_INTAKE_PRESET or deepseek-flash (V4.1 Flash)
    - RESEARCH: PB_LLM_RESEARCH_PRESET or deepseek-v4-pro
    - DRAFTING: PB_LLM_DRAFTING_PRESET or deepseek-flash
    - PACKET_CHECK: PB_LLM_PACKET_CHECK_PRESET or gemini-3.8-flash
    """
    if "PB_LLM_PRESET" in environ:
        return environ["PB_LLM_PRESET"]
    if worker is AiWorker.INTAKE_READING:
        return environ.get("PB_LLM_INTAKE_PRESET", DEFAULT_INTAKE_PRESET)
    if worker is AiWorker.RESEARCH:
        return environ.get("PB_LLM_RESEARCH_PRESET", DEFAULT_RESEARCH_PRESET)
    if worker is AiWorker.DRAFTING:
        return environ.get("PB_LLM_DRAFTING_PRESET", DEFAULT_DRAFTING_PRESET)
    if worker is AiWorker.PACKET_CHECK:
        return environ.get("PB_LLM_PACKET_CHECK_PRESET", DEFAULT_PACKET_CHECK_PRESET)
    return environ.get("PB_LLM_PRESET", "minimax-m3.1")


def build_llm_client(
    preset: str,
    environ: Mapping[str, str],
    *,
    minimax_pricing: ModelPricing | None = None,
    mimo_model_config: LlmModelConfig | None = None,
) -> LlmClient:
    """Named presets: sonnet-5.5, gpt-6-luna, minimax-m3.1, mimo,
    deepseek-flash, deepseek-v4-pro, gemini-3.8-flash, gemini-3.5-flash-lite.

    MiMo has no implicit model or prices: it requires an explicit, qualified
    ``mimo_model_config`` and otherwise fails before any SDK or network use.
    """
    variables = {
        "sonnet-5.5": "ANTHROPIC_API_KEY",
        "gpt-6-luna": "OPENAI_API_KEY",
        "minimax-m3.1": "MINIMAX_API_KEY",
        "mimo": "MIMO_API_KEY",
        "deepseek-flash": "DEEPSEEK_API_KEY",
        "deepseek-v4-pro": "DEEPSEEK_API_KEY",
        "gemini-3.8-flash": "GEMINI_API_KEY",
        "gemini-3.5-flash-lite": "GEMINI_API_KEY",
    }
    if preset not in variables:
        raise ValueError("Unknown LLM preset")
    if preset == "mimo" and (mimo_model_config is None or mimo_model_config.provider != "mimo"):
        raise ValueError("MiMo requires an explicit qualification configuration")
    variable = variables[preset]
    key = environ.get(variable, "")
    if not key.strip() and preset in {"gemini-3.8-flash", "gemini-3.5-flash-lite"}:
        key = environ.get("GOOGLE_API_KEY", "")
    if not key.strip():
        raise ValueError(f"Missing {variable}")
    if not valid_header_credential(key):
        raise ValueError(f"Invalid {variable}")
    if preset == "sonnet-5.5":
        return AnthropicLlmClient(SONNET_5_5, key)
    if preset == "gpt-6-luna":
        return OpenAiLlmClient(GPT_6_LUNA, key)
    if preset == "mimo":
        assert mimo_model_config is not None  # validated above
        return MiMoLlmClient(mimo_model_config, key)
    if preset == "deepseek-flash":
        return DeepSeekLlmClient(deepseek_flash_config(), key)
    if preset == "deepseek-v4-pro":
        return DeepSeekLlmClient(deepseek_v4_pro_config(), key)
    if preset == "gemini-3.8-flash":
        return GeminiLlmClient(gemini_3_8_flash_config(), key)
    if preset == "gemini-3.5-flash-lite":
        return GeminiLlmClient(gemini_3_5_flash_lite_config(), key)
    return AnthropicLlmClient(minimax_config(minimax_pricing), key)
