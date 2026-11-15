"""T40 follow-up: a crash while drafting a case with required copies pays for no second call."""

from __future__ import annotations

from pathlib import Path

import pytest

from parkingbreaker.api import schemas as s
from parkingbreaker.appeal.fakes import FakeDrafter
from parkingbreaker.case_state import CaseState
from test_appeal_packet import image
from test_case_required_copies import anyio_backend as anyio_backend
from test_case_required_copies import require_copy, selection
from test_case_service import ALICE, drafting_case
from test_case_service import fake_resolution as fake_resolution


class Crash(Exception):
    """The process dies after the call returned, before the draft is saved."""


@pytest.mark.anyio
async def test_recovery_with_required_copies_replays_the_paid_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, real, case = await drafting_case(tmp_path, monkeypatch)
    try:
        require_copy(real, case)
        record = real._intake(case)
        refs = [real._deps.photos.put(record.owner, image(color=(20, 100, 80)))]
        saved = await client.put(
            f"/cases/{case}/required-copies",
            headers=ALICE,
            json=selection(real, case, refs).model_dump(),
        )
        assert saved.status_code == 200, saved.text
        before = real.status(case).required_copies
        assert before is not None
        assert before.ready
        assert before.editable
        real_save = real._save_draft

        def crash_once(*args: object, **kwargs: object) -> object:
            monkeypatch.setattr(real, "_save_draft", real_save)
            raise Crash

        monkeypatch.setattr(real, "_save_draft", crash_once)
        with pytest.raises(Crash):
            real.step(case, s.WorkerStep(step_id="draft"))
        assert isinstance(real._deps.drafter, FakeDrafter)
        assert len(real._deps.drafter.calls) == 1
        # The paid run took the destination lock, so the copies are no longer editable.
        locked = real.status(case).required_copies
        assert locked is not None
        assert not locked.editable
        status = real.step(case, s.WorkerStep(step_id="draft"))
        assert len(real._deps.drafter.calls) == 1
        assert status.state is CaseState.AWAITING_ADOPTION
    finally:
        await client.aclose()
