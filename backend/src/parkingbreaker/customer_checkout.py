"""Customer checkout on the durable site, including filing research before payment."""

from datetime import date

from parkingbreaker.api import schemas as s
from parkingbreaker.api.service import ServiceConflict
from parkingbreaker.case_service import CaseService, intake_answers
from parkingbreaker.case_state import HardStop, StopFamily
from parkingbreaker.lob_run import production_date
from parkingbreaker.mail_config import MailClass
from parkingbreaker.prehold import PROVIDER_ERRORS
from parkingbreaker.research_records import (
    answers_json,
    decode_agency,
    decode_research,
    research_json,
)
from parkingbreaker.service_phase import ServicePhase


class CustomerCheckout(CaseService):
    def _receipt(self, workspace_id: str, case_id: str) -> s.IntakeReceipt:
        # Only intake submission (and its idempotent replay) can commission research.
        # Status reads and attempted card authorizations only inspect its saved result.
        if self.service_phase is ServicePhase.PAID and isinstance(
            super().pre_hold(case_id), s.Offer
        ):
            self._prepare_filing(case_id)
        return super()._receipt(workspace_id, case_id)

    def _prepare_filing(self, case_id: str) -> None:
        record = self._intake(case_id)
        if (
            record.case_token
            or not record.agency_json
            or self._store.has_active_authorization_attempt(case_id)
            or self._store.get_research(case_id)
        ):
            return
        with self._store.draft_ownership(case_id) as owned:
            if not owned:
                raise ServiceConflict("filing_research_in_progress")
            if self._store.get_research(case_id) is not None:
                return
            deps = (
                self._deps.research_for_subject(case_id)
                if self._deps.research_for_subject
                else self._deps.research
            )
            if deps is None:
                return
            agency = decode_agency(record.agency_json)
            try:
                result, reused = self._collect_research(record, agency, deps, record.created_at)
            except PROVIDER_ERRORS:
                self._log_research_call(case_id, deps.model_label, None, reused=False)
                return
            intake = s.SubmitIntake.model_validate_json(record.intake_json)
            self._store.save_research(
                case_id,
                research_json(result),
                answers_json(intake_answers(intake)),
                self._deps.clock(),
                None,
            )
            self._log_research_call(case_id, deps.model_label, result, reused=reused)
            self._record_tricks(agency.id, result, deps.tricks)

    def _filing_verified_for_reservation(self, case_id: str) -> bool:
        saved = self._store.get_research(case_id)
        if saved is None:
            return False
        research = decode_research(saved.research_json)
        now = self._deps.clock()
        intake = s.SubmitIntake.model_validate_json(self._intake(case_id).intake_json)
        resolution = research.resolve(intake_answers(intake))
        return (
            research.evidence_current_at(now)
            and resolution.research_complete
            and resolution.deadline is not None
            and resolution.deadline.official is not None
            and not resolution.notice_conflicts
            and resolution.deadline_feasible(production_date(now), MailClass.CERTIFIED)
        )

    def pre_hold(self, case_id: str, *, today: date | None = None) -> s.PreHoldResult:
        offer = super().pre_hold(case_id, today=today)
        if (
            isinstance(offer, s.Offer)
            and self.service_phase is ServicePhase.PAID
            and self._intake(case_id).case_token is None
            and not self._store.has_active_authorization_attempt(case_id)
            and not self._filing_verified_for_reservation(case_id)
        ):
            return s.Stop(
                case_id=case_id,
                family=StopFamily.NOT_YET_VERIFIED,
                hard_stop=HardStop.S2,
                reason_key="filing_evidence_unverified",
                message="We could not verify on-time filing at the correct office. "
                "Check your ticket for the issuer's filing instructions.",
            )
        return offer
