"""In-memory fake for the Lob seam. For tests only; no network."""

from __future__ import annotations

from dataclasses import dataclass, field

from parkingbreaker.lob_submission import (
    CancelOutcome,
    LetterEvidence,
    LetterRequest,
    LobResponse,
    MetadataLookup,
)


@dataclass
class FakeLobClient:
    """Replays scripted responses in order and honours idempotency like the provider.

    Once a key has produced a letter, later requests with that key return the same letter.
    `letters` maps submission token to letter id; seed it to model a letter created behind
    a lost response. An empty script answers with a timeout.
    """

    script: list[LobResponse] = field(default_factory=list)
    letters: dict[str, str] = field(default_factory=dict)
    by_key: dict[str, str] = field(default_factory=dict)
    calls: list[tuple[str, LetterRequest]] = field(default_factory=list)
    lookups: list[str] = field(default_factory=list)
    evidence_by_token: dict[str, LetterEvidence] = field(default_factory=dict)
    # Scripted answers to cancel requests, in order; an empty script answers UNKNOWN.
    # A CANCELED answer voids the letter for every later lookup, like Lob's deletion.
    cancel_script: list[CancelOutcome] = field(default_factory=list)
    cancels: list[str] = field(default_factory=list)

    def create_letter(self, request: LetterRequest, idempotency_key: str) -> LobResponse:
        self.calls.append((idempotency_key, request))
        if idempotency_key in self.by_key:
            return LobResponse(
                status=200,
                letter_id=self.by_key[idempotency_key],
                evidence=self.evidence_by_token.get(
                    request.submission_token, LetterEvidence.CREATED
                ),
            )
        response = self.script.pop(0) if self.script else LobResponse(None, error="timeout")
        if response.status is not None and 200 <= response.status < 300 and response.letter_id:
            self.by_key[idempotency_key] = response.letter_id
            self.letters[request.submission_token] = response.letter_id
            self.evidence_by_token[request.submission_token] = response.evidence
        return response

    def find_by_metadata(self, submission_token: str) -> str | MetadataLookup | None:
        self.lookups.append(submission_token)
        return MetadataLookup(
            self.letters.get(submission_token),
            authoritative=True,
            evidence=self.evidence_by_token.get(submission_token, LetterEvidence.PRODUCTION),
        )

    def lookup_letter(self, letter_id: str, submission_token: str) -> MetadataLookup:
        if self.letters.get(submission_token) != letter_id:
            raise RuntimeError("unknown letter")
        return MetadataLookup(
            letter_id,
            authoritative=False,
            evidence=self.evidence_by_token.get(submission_token, LetterEvidence.PRODUCTION),
        )

    def cancel_letter(self, letter_id: str) -> CancelOutcome:
        self.cancels.append(letter_id)
        result = self.cancel_script.pop(0) if self.cancel_script else CancelOutcome.UNKNOWN
        if result is CancelOutcome.CANCELED:
            for token, known in self.letters.items():
                if known == letter_id:
                    self.evidence_by_token[token] = LetterEvidence.VOIDED
        return result
