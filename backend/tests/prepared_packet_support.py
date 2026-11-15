"""Valid synthetic re-mail bytes for tests that enter at the durable attempt seam."""

from dataclasses import replace
from datetime import datetime

from parkingbreaker.agencies import MailingAddress
from parkingbreaker.appeal.letter import redirect_letter_address
from parkingbreaker.appeal.packet import frozen_signature_artwork
from parkingbreaker.case_service import CaseService
from parkingbreaker.research.run import ResearchResult


def redirected_pdf(real: CaseService, case_id: str, destination: MailingAddress) -> bytes:
    record = real._intake(case_id)
    assert record.case_token is not None
    original = real._store.get(record.case_token, 1)
    saved = real._store.get_draft(case_id)
    assert original is not None
    assert saved is not None
    packet, _, _, _ = real._frozen_packet(
        record,
        saved,
        frozen_signature_artwork(original.request.pdf),
        letter_text=redirect_letter_address(
            saved.letter_text, original.request.recipient, destination
        ),
    )
    return packet.pdf


def fetched_at(result: ResearchResult, now: datetime) -> ResearchResult:
    """Model a fresh synthetic fetch at the service clock; never alter saved evidence."""
    delta = now - result.finished_at
    return replace(
        result,
        started_at=result.started_at + delta,
        finished_at=now,
        sources=tuple(
            replace(source, retrieved_at=source.retrieved_at + delta) for source in result.sources
        ),
    )
