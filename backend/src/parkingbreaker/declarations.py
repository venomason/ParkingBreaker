"""Per-declaration identity and customer affirmation (audit A020-A028, A084).

A required agency declaration is never one opaque blob and never a pre-checked box. Each
one carries a stable id, the exact official wording, where that wording came from, whether
a signature is needed, and whether the customer actually affirmed it for this packet.

Only wording the research actually verified becomes a declaration. Unknown or unverified
wording fails closed: the caller cannot invent a declaration, and an empty requirement list
means "the agency requires none", not "we forgot to ask".
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from parkingbreaker.research.findings import Finding
from parkingbreaker.research.fulfilment import (
    SignatureRequirement,
    declaration_wording,
    signature_requirement,
    verified_findings,
)
from parkingbreaker.research.questions import FactKey, Ticket
from parkingbreaker.research.sources import SourceRecord

DECLARATION_FIELD = "REQUIRED_DECLARATIONS"
SIGNATURE_FIELD = "SIGNATURE_REQUIREMENT"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Declaration:
    declaration_id: str
    field_name: str
    verbatim_text: str
    required: bool
    source_sha256s: tuple[str, ...]
    signature_required: bool

    @property
    def text_sha256(self) -> str:
        return _sha256(self.verbatim_text)

    def affirmed_by(self, text_sha256: str) -> bool:
        """An affirmation matches only the exact wording it was made against."""
        return text_sha256 == self.text_sha256


class DeclarationError(ValueError):
    pass


def declarations_for(
    findings: Sequence[Finding], sources: Sequence[SourceRecord], ticket: Ticket
) -> tuple[Declaration, ...]:
    """Every verified declaration, in a stable order with stable ids."""
    verified = verified_findings(findings, sources, ticket)
    signature_required = (
        signature_requirement(findings, sources, ticket) is SignatureRequirement.REQUIRED
    )
    declarations: list[Declaration] = []
    for f in verified:
        if f.key is not FactKey.REQUIRED_DECLARATIONS or not f.value:
            continue
        wording = declaration_wording(f.verbatim_quote or "")
        if wording is None or not isinstance(f.value, list) or len(f.value) != 1:
            raise DeclarationError("required declaration wording is not established")
        declarations.append(
            Declaration(
                declaration_id=f"declaration-{len(declarations) + 1}",
                field_name=DECLARATION_FIELD,
                verbatim_text=wording,
                required=True,
                source_sha256s=tuple(f.source_sha256s),
                signature_required=signature_required,
            )
        )
    return tuple(declarations)


def unaffirmed_required(
    declarations: Sequence[Declaration], affirmations: Mapping[str, str]
) -> tuple[str, ...]:
    """Ids of required declarations with no matching affirmation, in declaration order.

    Unknown ids are ignored here; `unknown_affirmations` reports those separately so the
    caller can refuse a stale or invented affirmation instead of silently dropping it.
    """
    return tuple(
        declaration.declaration_id
        for declaration in declarations
        if declaration.required
        and not declaration.affirmed_by(affirmations.get(declaration.declaration_id, ""))
    )


def unknown_affirmations(
    declarations: Sequence[Declaration], affirmations: Mapping[str, str]
) -> tuple[str, ...]:
    known = {declaration.declaration_id for declaration in declarations}
    return tuple(sorted(set(affirmations) - known))


def affirmation_map(pairs: Sequence[tuple[str, str]]) -> dict[str, str]:
    """Ordered (id, text hash) pairs to a map, refusing a duplicate id."""
    result: dict[str, str] = {}
    for declaration_id, text_sha256 in pairs:
        if declaration_id in result:
            raise DeclarationError(f"duplicate affirmation: {declaration_id}")
        result[declaration_id] = text_sha256
    return result
