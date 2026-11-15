"""Test-only seam for the retired paid-draft provider.

Production authorship is deterministic (`CustomerTextDrafter`), so the budget, ownership,
refusal and replay machinery that guarded paid drafting has no production caller. These
suites still exercise that machinery with an explicit provider so a future opt-in cannot
reintroduce the old behaviour untested. The application default is unchanged.
"""

from __future__ import annotations

from parkingbreaker import config
from parkingbreaker.appeal.drafting import LlmDrafter
from parkingbreaker.case_service import CaseService


def install_paid_drafter(service: CaseService) -> None:
    """Use the fixture LLM as a paid drafter for this service instance only."""
    html = config.FIXTURE.read_text(encoding="utf-8")
    object.__setattr__(service._deps, "drafter_for_subject", None)
    # The fixture class is monkeypatched onto the config module by these suites.
    fixture_llm = getattr(config, "FixtureLlm")  # noqa: B009
    object.__setattr__(service._deps, "drafter", LlmDrafter(fixture_llm(html)))
