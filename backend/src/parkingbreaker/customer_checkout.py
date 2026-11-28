"""Customer checkout on the durable site, including filing research before payment."""

from collections.abc import Sequence
from datetime import date

from parkingbreaker.api import schemas as s
from parkingbreaker.api.service import ServiceConflict
from parkingbreaker.case_service import (
    _PAYMENT_QUESTIONS,
    _VISIBLE_QUESTIONS,
    VEHICLE_QUESTION,
    CaseService,
    intake_answers,
    research_view,
)
from parkingbreaker.case_state import (
    CaseState,
    CustomerNeeded,
    HardStop,
    StopFamily,
    Stopped,
    StopReason,
    stop_family,
)
from parkingbreaker.lob_run import production_date
from parkingbreaker.mail_config import MailClass
from parkingbreaker.prehold import PROVIDER_ERRORS
from parkingbreaker.research.ladder import CustomerQuestion
from parkingbreaker.research.resolution import (
    ResearchAnswers,
    ResearchResolution,
    answer_question,
    resolution_event,
)
from parkingbreaker.research.run import ResearchResult
from parkingbreaker.research.sources import AuthorityClass, SourceRecord
from parkingbreaker.research_records import (
    answers_json,
    decode_agency,
    decode_answers,
    decode_intake_agency,
    decode_research,
    research_json,
)
from parkingbreaker.service_phase import ServicePhase
from parkingbreaker.stop_reasons import stop_message


def _advance_unasked_questions(result: ResearchResult, answers: ResearchAnswers) -> ResearchAnswers:
    """Advance internal non-visible questions without fabricating visible customer answers."""
    for _ in CustomerQuestion:
        event = resolution_event(result.resolve(answers))
        if not (
            isinstance(event, CustomerNeeded)
            and event.question not in _VISIBLE_QUESTIONS
            and event.question in CustomerQuestion.__members__
        ):
            break
        answers = answer_question(answers, CustomerQuestion(event.question), None)
    return answers


def _question_view(question_id: str) -> s.CaseQuestion | None:
    if question_id == "VEHICLE_RELATIONSHIP":
        return VEHICLE_QUESTION
    return _PAYMENT_QUESTIONS.get(question_id)


def _official_source_url(sources: Sequence[SourceRecord]) -> str | None:
    for source in sources:
        if (
            source.http_status == 200
            and source.authority_class is not AuthorityClass.SECONDARY
            and source.final_url.startswith("https://")
        ):
            return source.final_url
    return None


class CustomerCheckout(CaseService):
    def _receipt(self, workspace_id: str, case_id: str) -> s.IntakeReceipt:
        # Only intake submission (and its idempotent replay) can commission research.
        # Status reads and attempted card authorizations only inspect its saved result.
        if self.service_phase is ServicePhase.PAID and isinstance(
            super().pre_hold(case_id), s.Offer
        ):
            self._prepare_filing(case_id)
        receipt = super()._receipt(workspace_id, case_id)
        if self._prehold_question(case_id) is not None:
            return receipt.model_copy(update={"state": CaseState.NEEDS_CUSTOMER})
        return receipt

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
            agency, lead = decode_intake_agency(record.agency_json)
            try:
                result, reused = self._collect_research(
                    record, agency, deps, record.created_at, discovered_procedure_url=lead
                )
            except PROVIDER_ERRORS:
                self._log_research_call(case_id, deps.model_label, None, reused=False)
                return
            intake = s.SubmitIntake.model_validate_json(record.intake_json)
            initial_answers = _advance_unasked_questions(result, intake_answers(intake))
            self._store.save_research(
                case_id,
                research_json(result),
                answers_json(initial_answers),
                self._deps.clock(),
                None,
            )
            self._log_research_call(case_id, deps.model_label, result, reused=reused)
            self._record_tricks(agency.id, result, deps.tricks)

    def _prehold_resolution(
        self, case_id: str
    ) -> tuple[ResearchResult, ResearchAnswers, ResearchResolution] | None:
        saved = self._store.get_research(case_id)
        if saved is None:
            return None
        research = decode_research(saved.research_json)
        answers = _advance_unasked_questions(research, decode_answers(saved.answers_json))
        return research, answers, research.resolve(answers)

    def _official_url_for_stop(
        self, case_id: str, sources: Sequence[SourceRecord] = ()
    ) -> str | None:
        found = _official_source_url(sources)
        if found is not None:
            return found
        record = self._store.get_intake(case_id)
        if record is not None and record.agency_json is not None:
            agency = decode_agency(record.agency_json)
            if agency.official_domains:
                return f"https://{agency.official_domains[0]}/"
        return None

    def _prehold_question(self, case_id: str) -> s.CaseQuestion | None:
        if self.service_phase is not ServicePhase.PAID:
            return None
        record = self._store.get_intake(case_id)
        if (
            record is None
            or record.case_token is not None
            or self._store.beta_entitlement(case_id) is not None
            or self._store.has_active_authorization_attempt(case_id)
            or not isinstance(super().pre_hold(case_id), s.Offer)
        ):
            return None
        evaluated = self._prehold_resolution(case_id)
        if evaluated is None:
            return None
        research, _answers, resolution = evaluated
        now = self._deps.clock()
        if not (
            research.evidence_current_at(now)
            and resolution.mailing_address is not None
            and resolution.deadline is not None
            and resolution.deadline.official is not None
            and not resolution.notice_conflicts
            and resolution.deadline_feasible(production_date(now), MailClass.CERTIFIED)
            and not resolution.stops
        ):
            return None
        event = resolution_event(resolution)
        if isinstance(event, CustomerNeeded) and event.question in _VISIBLE_QUESTIONS:
            return _question_view(event.question)
        return None

    def _filing_verified_for_reservation(self, case_id: str) -> bool:
        evaluated = self._prehold_resolution(case_id)
        if evaluated is None:
            return False
        research, _answers, resolution = evaluated
        now = self._deps.clock()
        return (
            research.evidence_current_at(now)
            and resolution.research_complete
            and resolution.deadline is not None
            and resolution.deadline.official is not None
            and not resolution.notice_conflicts
            and resolution.deadline_feasible(production_date(now), MailClass.CERTIFIED)
        )

    def _finished_transaction(self, workspace_id: str) -> bool:
        if super()._finished_transaction(workspace_id):
            return True
        intake = self._store.intake_for_workspace(workspace_id)
        if (
            intake is None
            or intake.case_token is not None
            or self._store.beta_entitlement(intake.intake_id) is not None
            or self._store.has_active_authorization_attempt(intake.intake_id)
            or self._prehold_question(intake.intake_id) is not None
        ):
            return False
        view = self.pre_hold(intake.intake_id)
        return isinstance(view, s.Stop) and view.reason_key != "paid_cohort_full"

    def status(self, case_id: str) -> s.CaseStatus:
        record = self._intake(case_id)
        if record.case_token is None:
            question = self._prehold_question(case_id)
            if question is not None:
                saved = self._store.get_research(case_id)
                tier = None if record.selected_tier is None else s.MailTier(record.selected_tier)
                return s.CaseStatus(
                    case_id=case_id,
                    workspace_id=record.workspace_token,
                    state=CaseState.NEEDS_CUSTOMER,
                    payment_state=None,
                    authorization_pending=None,
                    selected_tier=tier,
                    window_started_at=None,
                    window_expires_at=None,
                    seconds_remaining=None,
                    window_status=None,
                    question=question,
                    stop=None,
                    research=(
                        None
                        if saved is None
                        else research_view(decode_research(saved.research_json))
                    ),
                    fake=False,
                )
        return super().status(case_id)

    def answer_question(self, case_id: str, body: s.CaseAnswer) -> s.CaseStatus:
        record = self._intake(case_id)
        if record.case_token is not None:
            return super().answer_question(case_id, body)
        saved = self._store.get_research(case_id)
        current_question = self._prehold_question(case_id)
        if saved is None or current_question is None:
            raise ServiceConflict("no_open_question")
        if current_question.question_id != body.question_id:
            raise ServiceConflict("question_mismatch")
        result = decode_research(saved.research_json)
        previous_answers = _advance_unasked_questions(result, decode_answers(saved.answers_json))
        question = CustomerQuestion(body.question_id)
        answers = answer_question(previous_answers, question, body.answer)
        if question in previous_answers.answered and answers != previous_answers:
            raise ServiceConflict("question_mismatch")
        answers = _advance_unasked_questions(result, answers)
        if answers != decode_answers(saved.answers_json):
            self._store.save_research(
                case_id,
                saved.research_json,
                answers_json(answers),
                self._deps.clock(),
                saved.revision,
            )
        return self.status(case_id)

    def pre_hold(self, case_id: str, *, today: date | None = None) -> s.PreHoldResult:
        offer = super().pre_hold(case_id, today=today)
        if (
            isinstance(offer, s.Offer)
            and self.service_phase is ServicePhase.PAID
            and self._intake(case_id).case_token is None
            and not self._store.has_active_authorization_attempt(case_id)
            and not self._filing_verified_for_reservation(case_id)
        ):
            evaluated = self._prehold_resolution(case_id)
            sources = evaluated[0].sources if evaluated is not None else ()
            official_url = self._official_url_for_stop(case_id, sources)
            if evaluated is not None and self._prehold_question(case_id) is None:
                event = resolution_event(evaluated[2])
                if isinstance(event, Stopped) and event.reason is StopReason.PROCESS_INCOMPATIBLE:
                    detail = event.detail or None
                    msg = stop_message(event.reason, event.hard_stop, official_quote=detail)
                    return s.Stop(
                        case_id=case_id,
                        family=stop_family(event.reason),
                        hard_stop=event.hard_stop,
                        reason_key=msg.key,
                        message=msg.text,
                        official_quote=detail,
                        official_url=official_url,
                    )
            return s.Stop(
                case_id=case_id,
                family=StopFamily.NOT_YET_VERIFIED,
                hard_stop=HardStop.S2,
                reason_key="filing_evidence_unverified",
                message="We could not verify on-time filing at the correct office. "
                "Check your ticket for the issuer's filing instructions.",
                official_url=official_url,
            )
        return offer
