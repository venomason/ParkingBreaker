"""Selected provider credentials refuse malformed HTTP-header bytes locally."""

import pytest

from parkingbreaker import llm_config
from parkingbreaker.brave_search import BraveSearcher
from parkingbreaker.provider_credentials import valid_header_credential

BAD_KEYS = [
    "synthetic\nheader",
    "synthetic\rheader",
    "synthetic\theader",
    "synthetic header",
    " synthetic",
    "synthetic ",
    "synthetic\x00header",
    "synthetic\x7fheader",
    "syntheticé",
]


@pytest.mark.parametrize("value", ["synthetic", "sk-test_123:/+=.-~", "!" + "~"])
def test_visible_ascii_credentials_remain_valid(value: str) -> None:
    assert valid_header_credential(value)


@pytest.mark.parametrize("value", ["", *BAD_KEYS])
def test_header_credential_validation_is_pure_and_complete(value: str) -> None:
    assert not valid_header_credential(value)


@pytest.mark.parametrize(
    ("preset", "variable"),
    [
        ("sonnet-5.5", "ANTHROPIC_API_KEY"),
        ("gpt-6-luna", "OPENAI_API_KEY"),
        ("minimax-m3.1", "MINIMAX_API_KEY"),
    ],
)
@pytest.mark.parametrize("value", BAD_KEYS)
def test_malformed_selected_llm_key_is_refused_without_echo(
    preset: str, variable: str, value: str, caplog: pytest.LogCaptureFixture
) -> None:
    with pytest.raises(ValueError, match=variable) as failure:
        llm_config.build_llm_client(preset, {variable: value})
    assert value not in str(failure.value)
    assert value not in caplog.text


@pytest.mark.parametrize("value", BAD_KEYS)
def test_malformed_brave_key_is_refused_without_echo(
    value: str, caplog: pytest.LogCaptureFixture
) -> None:
    with pytest.raises(ValueError, match="BRAVE_SEARCH_API_KEY") as failure:
        BraveSearcher({"BRAVE_SEARCH_API_KEY": value})
    assert value not in str(failure.value)
    assert value not in caplog.text
