from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, date, datetime

import pytest

from parkingbreaker.agencies import IssuerType
from parkingbreaker.appeal.drafting import (
    Draft,
    DraftSentence,
    LlmDrafter,
    TicketFacts,
    customer_name_sensitive_strings,
)
from parkingbreaker.appeal.letter import LetterInput, Sender, render_letter
from parkingbreaker.appeal.proofread import proofread
from parkingbreaker.appeal.references import Reference
from parkingbreaker.appeal.run import DraftingStatus, run_drafting
from parkingbreaker.json_types import JsonValue
from parkingbreaker.llm import Prompt
from parkingbreaker.oplog.scrubber import identifier_spans, redact_identifiers
from parkingbreaker.research.delegation import PageLink
from parkingbreaker.research.extractor import LlmExtractor
from parkingbreaker.research.issuer_assessment import LlmIssuerAssessor
from parkingbreaker.research.jurisdiction import DotGovRecord, IssuerCandidate, IssuerQuery
from parkingbreaker.research.link_assessment import LlmLinkAssessor
from parkingbreaker.research.questions import FactKey, Ticket, build_questions
from prompt_capture import PromptCapture
from samples import CONTEST_ADDRESS, SAN_FRANCISCO, TEST_BASIS, source

FACTS = TicketFacts("SF-998877", "7ABC123", date(2026, 10, 1), "TC 7.2.22", "Street cleaning")
SENDER = Sender("Jordan Alvarez", ("1450 Valencia Street", "San Francisco, CA 94110"))
EMAIL = "jo.doe+x@example.co.uk"
PHONE = "+1 (415) 555-1234"
PRIVATE = (FACTS.plate, FACTS.citation_number, SENDER.name, *SENDER.address_lines, EMAIL, PHONE)
NOTES = (
    "The sign was covered. "
    f"I am {SENDER.name} at {SENDER.address_lines[0]}. "
    f"My email is {EMAIL} and my phone is {PHONE}. "
    f"My plate is {FACTS.plate} and citation is {FACTS.citation_number}."
)
NOW = datetime(2026, 10, 11, 12, tzinfo=UTC)
SEGMENTS = re.compile(r"^\[(\d+)-(\d+)\] (.+)$", re.MULTILINE)


def echo_notes(prompt: Prompt, attempt: int) -> JsonValue:
    """Copy only the provider's masked view, with its original-coordinate support tags."""
    return {
        "sentences": [
            {"text": text, "support": [f"note:{start}-{end}"], "paragraph": 0}
            for start, end, text in SEGMENTS.findall(prompt.user)
        ]
    }


def test_local_restoration_preserves_letter_bytes_and_proofreads_final_text() -> None:
    def produce(prompt: Prompt, attempt: int) -> JsonValue:
        reply = echo_notes(prompt, attempt)
        assert isinstance(reply, dict)
        assert isinstance(reply["sentences"], list)
        plate = re.search(r"^- plate: (.+)$", prompt.user, re.MULTILINE)
        citation = re.search(r"^- citation_number: (.+)$", prompt.user, re.MULTILINE)
        assert plate
        assert citation
        reply["sentences"].append(
            {
                "text": f"I contest citation {citation[1]} for plate {plate[1]}.",
                "support": ["fact:citation_number", "fact:plate"],
                "paragraph": 1,
            }
        )
        return reply

    client = PromptCapture(PRIVATE, produce)
    result = run_drafting(
        NOTES,
        (),
        FACTS,
        LlmDrafter(client, sensitive_strings=PRIVATE),
        lambda: NOW,
        NOW,
        lambda: SAN_FRANCISCO,
        new_revision=lambda: "privacy",
        get_state_basis=lambda: {"CA": TEST_BASIS},
    )
    assert result.status is DraftingStatus.READY
    assert result.attempts == len(client.prompts) == 1
    actual = Draft(tuple(DraftSentence(s.text, s.support, s.paragraph) for s in result.spans))
    # The public original-offset tags resolve to the customer's exact text, even through
    # email punctuation, variable-length replacements and multiple overlapping detectors.
    expected = Draft(
        (
            *tuple(
                DraftSentence(NOTES[int(start) : int(end)].strip(), (f"note:{start}-{end}",))
                for start, end, _ in SEGMENTS.findall(client.prompts[0].user)
            ),
            DraftSentence(
                f"I contest citation {FACTS.citation_number} for plate {FACTS.plate}.",
                ("fact:citation_number", "fact:plate"),
                1,
            ),
        )
    )
    assert actual == expected
    assert proofread(actual, NOTES, (), FACTS) == ()
    letter = LetterInput(
        SENDER,
        NOW.date(),
        SAN_FRANCISCO,
        CONTEST_ADDRESS,
        FACTS.citation_number,
        FACTS.plate,
        actual,
    )
    rendered = render_letter(letter).text
    assert rendered.encode() == render_letter(replace(letter, draft=expected)).text.encode()
    assert SENDER.name in rendered
    assert SENDER.address_lines[0] in rendered
    assert EMAIL in rendered
    assert PHONE in rendered
    assert "[[PB_PRIVATE_" not in rendered


def test_one_capture_helper_checks_every_ai_path_including_redraft_feedback() -> None:
    def produce(prompt: Prompt, attempt: int) -> JsonValue:
        if attempt == 1:
            plate = re.search(r"^- plate: (.+)$", prompt.user, re.MULTILINE)
            assert plate
            # Incorrect support: proofreading runs after restoration and quotes the real
            # plate in its rejection. The next provider request must mask that feedback.
            return {
                "sentences": [
                    {
                        "text": f"I saw {plate[1]}.",
                        "support": ["note:0-21"],
                        "paragraph": 0,
                    }
                ]
            }
        return echo_notes(prompt, attempt)

    client = PromptCapture(PRIVATE, produce)
    result = run_drafting(
        NOTES,
        (),
        FACTS,
        LlmDrafter(client, sensitive_strings=PRIVATE),
        lambda: NOW,
        NOW,
        lambda: SAN_FRANCISCO,
        new_revision=lambda: "privacy",
        get_state_basis=lambda: {"CA": TEST_BASIS},
    )
    assert result.status is DraftingStatus.READY
    assert result.attempts == len(client.prompts) == 2
    assert "unsupported identifier" in client.prompts[1].user
    assert "[[PB_PRIVATE_" in client.prompts[1].user

    # Research has no notes or sender input. Exercise every existing LlmClient path with
    # case identifiers also embedded in free text, URLs, saved pages and link context.
    client.produce = lambda *_: {}
    private_text = " | ".join(PRIVATE)
    page = source(
        f"San Francisco accepts contests at 11 South Van Ness Avenue. {private_text}",
        url=f"https://sf.gov/contest?plate={FACTS.plate}",
    )
    ticket = Ticket("sf", "TC 7.2.22", private_text, FACTS.issue_date, stage=private_text)
    LlmExtractor(client, sensitive_strings=PRIVATE).extract(build_questions(ticket), (page,))
    candidate = IssuerCandidate(
        DotGovRecord("sf.gov", "City", "San Francisco", None, "San Francisco", "CA"),
        IssuerType.MUNICIPAL,
        30,
        (),
        ("sf.gov",),
    )
    LlmIssuerAssessor(client, sensitive_strings=PRIVATE).assess(
        IssuerQuery(f"San Francisco {private_text}", "San Francisco", "CA"),
        private_text,
        (candidate,),
        (page,),
    )
    LlmLinkAssessor(client, sensitive_strings=PRIVATE).assess(
        page,
        (PageLink(page.url, private_text, private_text),),
        "San Francisco",
        private_text,
    )
    assert len(client.prompts) == len(client.calls) == 5
    for prompt in client.prompts[2:]:
        # Privacy must not erase official destination evidence or source provenance.
        assert "11 South Van Ness Avenue" in prompt.user
        assert page.sha256 in prompt.user


@pytest.mark.parametrize(
    "identifier",
    [EMAIL, PHONE, "9876543210", "AB12CD", "1450 Valencia Street"],
)
def test_notes_use_scrubber_detection_without_case_identifier_list(identifier: str) -> None:
    notes = f"I wrote {identifier} in my notes."
    client = PromptCapture((identifier,), echo_notes)
    result = LlmDrafter(client).draft(notes, (), FACTS)
    assert len(client.prompts) == 1
    assert result.draft == Draft((DraftSentence(notes, (f"note:0-{len(notes)}",)),))


def test_original_offsets_survive_unicode_whitespace_repeats_and_token_collisions() -> None:
    notes = f"I am Jordan\t  ALVAREZ. My email is {EMAIL}, again {EMAIL}. [[PB_PRIVATE_fact_plate]]"
    client = PromptCapture((SENDER.name, EMAIL), echo_notes)
    result = LlmDrafter(client, sensitive_strings=(SENDER.name,)).draft(notes, (), FACTS)
    assert result.draft.sentences
    for sentence in result.draft.sentences:
        tag = re.fullmatch(r"note:(\d+)-(\d+)", sentence.support[0])
        assert tag
        assert sentence.text == notes[int(tag[1]) : int(tag[2])].strip()


def test_customer_name_masking_includes_distinct_name_parts() -> None:
    assert customer_name_sensitive_strings("Jordan Alvarez") == (
        "Jordan Alvarez",
        "Jordan",
        "Alvarez",
    )
    assert customer_name_sensitive_strings("Li Q") == ("Li Q", "Li", "Q")


@pytest.mark.parametrize(
    "kind", ["unsupported_span", "unknown_token", "altered_token", "lowercase_token"]
)
def test_unrestorable_placeholders_fail_closed(kind: str) -> None:
    def produce(prompt: Prompt, attempt: int) -> JsonValue:
        token = re.search(r"\[\[PB_PRIVATE_\d+_\d+\]\]", prompt.user)
        assert token
        text = token[0]
        if kind == "unknown_token":
            text = "[[PB_PRIVATE_999_1000]]"
        elif kind == "altered_token":
            text = text[:-1]
        elif kind == "lowercase_token":
            text = text.lower()
        return {
            "sentences": [
                {
                    "text": text,
                    "support": ["note:0-1"],
                    "paragraph": 0,
                }
            ]
        }

    result = LlmDrafter(PromptCapture((EMAIL,), produce)).draft(f"Email {EMAIL}.", (), FACTS)
    assert result.draft == Draft(())


def test_context_identifiers_and_reference_tags_restore_without_token_collisions() -> None:
    facts = replace(FACTS, location=SENDER.address_lines[0], violation_description=SENDER.name)
    reference = Reference(
        f"exception-{FACTS.plate}", FactKey.CODE_EXCEPTIONS, EMAIL, PHONE, "https://sf.gov"
    )

    def produce(prompt: Prompt, attempt: int) -> JsonValue:
        location = re.search(r"^- location: (.+)$", prompt.user, re.MULTILINE)
        description = re.search(r"^- violation_description: (.+)$", prompt.user, re.MULTILINE)
        ref = re.search(r"<<<REFERENCE id=(.+?)>>>\n(.+) \| quoted: (.+)", prompt.user)
        assert location
        assert description
        assert ref
        return {
            "sentences": [
                {"text": location[1], "support": ["fact:location"], "paragraph": 0},
                {
                    "text": description[1],
                    "support": ["fact:violation_description"],
                    "paragraph": 0,
                },
                {"text": f"{ref[2]} | {ref[3]}", "support": [ref[1]], "paragraph": 1},
            ]
        }

    client = PromptCapture(PRIVATE, produce)
    result = LlmDrafter(client, sensitive_strings=PRIVATE).draft(
        "The sign was covered.", (reference,), facts, (" | ".join(PRIVATE),)
    )
    assert result.draft == Draft(
        (
            DraftSentence(SENDER.address_lines[0], ("fact:location",)),
            DraftSentence(SENDER.name, ("fact:violation_description",)),
            DraftSentence(f"{EMAIL} | {PHONE}", (reference.id,), 1),
        )
    )
    assert (
        proofread(
            result.draft, "The sign was covered.", (reference,), facts, require_all_notes=False
        )
        == ()
    )


def test_restored_customer_name_is_not_mistaken_for_service_advice() -> None:
    # A known private string can contain a banned phrase; the provider sees only its tag.
    notes = "My name is Legal Advice."
    client = PromptCapture(("Legal Advice",), echo_notes)
    result = run_drafting(
        notes,
        (),
        FACTS,
        LlmDrafter(client, sensitive_strings=("Legal Advice",)),
        lambda: NOW,
        NOW,
        lambda: SAN_FRANCISCO,
        new_revision=lambda: "restored-proofread",
        get_state_basis=lambda: {"CA": TEST_BASIS},
    )
    assert result.status is DraftingStatus.READY
    assert result.attempts == len(client.prompts) == 1
    assert result.spans[0].text == notes
    assert not result.flags


def test_redaction_reuses_log_matches_and_merges_overlaps() -> None:
    notes = f"Name: Jordan\n  Alvarez. Call {PHONE}. Plate 7ABC123."
    spans = identifier_spans(notes, (SENDER.name,))
    masked = redact_identifiers(notes, (SENDER.name,))
    assert {s.reason for s in spans} >= {"case_sensitive_string", "phone", "plate"}
    assert len(masked.spans) == 3
    for span in masked.spans:
        start = masked.text.index(span.placeholder)
        assert masked.original_offset(start) == span.start
        assert masked.original_offset(start + len(span.placeholder)) == span.end
    assert masked.original_offset(len(masked.text)) == len(notes)
    with pytest.raises(ValueError, match="inside an identifier placeholder"):
        masked.original_offset(masked.text.index(masked.spans[0].placeholder) + 1)
