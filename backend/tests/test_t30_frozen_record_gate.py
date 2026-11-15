"""A current instruction cannot replace responsibility frozen at packet approval."""

from pathlib import Path

import pytest

from parkingbreaker.case_state import CaseState
from parkingbreaker.lob_fakes import FakeLobClient
from parkingbreaker.lob_submission import Accepted, LetterEvidence, LobResponse
from test_case_service import NOW
from test_case_service import fake_resolution as fake_resolution
from test_effect_dispatch import approved


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_new_create_requires_frozen_responsibility_even_with_current_verified_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lob = FakeLobClient([LobResponse(200, "ltr_unbound", evidence=LetterEvidence.PRODUCTION)])
    http, real, case_id = await approved(tmp_path, monkeypatch, lob)
    try:
        token = real._intake(case_id).case_token
        assert token is not None
        frozen = real._store.frozen_destination(token, 1)
        assert frozen is not None
        real._store._require_open().execute(
            "DELETE FROM pb_mail_destination_bindings WHERE case_token=?", (token,)
        )
        record = real._intake(case_id)
        latest = real._store.latest_destination_decision(case_id, record.owner)
        assert latest is not None
        assert latest.decision == frozen
        real.dispatch(case_id)
        assert lob.calls == []
        assert lob.lookups == []
        assert real._store.frozen_destination(token, 1) is None
        case = real._store.get_case(token)
        assert case is not None
        assert case.letter_state is CaseState.STOPPED_NO_CHARGE
    finally:
        await http.aclose()
        real._store.close()


@pytest.mark.anyio
async def test_missing_frozen_record_does_not_release_or_recreate_an_uncertain_letter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lob = FakeLobClient([LobResponse(None)])
    http, real, case_id = await approved(tmp_path, monkeypatch, lob)
    try:
        real.dispatch(case_id)
        token = real._intake(case_id).case_token
        assert token is not None
        cursor = real._store.submission_cursor(token, 1)
        prepared = real._store.get(token, 1)
        assert cursor is not None
        assert prepared is not None
        real._store._require_open().execute(
            "DELETE FROM pb_mail_destination_bindings WHERE case_token=?", (token,)
        )
        lob.letters[prepared.request.submission_token] = "ltr_recovered"
        object.__setattr__(real._deps, "clock", lambda: max(NOW, cursor.next_at))
        real.dispatch(case_id)
        recovered = real._store.submission_cursor(token, 1)
        assert recovered is not None
        assert recovered.outcome == Accepted("ltr_recovered")
        assert len(lob.calls) == 1
        assert lob.lookups == [prepared.request.submission_token]
        assert real._store.frozen_destination(token, 1) is None
        case = real._store.get_case(token)
        assert case is not None
        assert case.letter_state is CaseState.IN_TRANSIT
    finally:
        await http.aclose()
        real._store.close()
