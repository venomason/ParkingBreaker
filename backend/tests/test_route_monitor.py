from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from parkingbreaker.http_fetcher import HttpFetcher
from parkingbreaker.mail_config import MailClass
from parkingbreaker.research.fakes import (
    FakeExtractor,
    FakeFetcher,
    html_response,
    redirect_response,
)
from parkingbreaker.research.findings import Finding
from parkingbreaker.research.questions import FactKey
from parkingbreaker.research.resolution import ResearchAnswers
from parkingbreaker.research.run import ResearchResult, run_research
from parkingbreaker.research.sources import AuthorityClass, Fetcher, FetchResponse, SourceRecord
from parkingbreaker.research_records import agency_json, answers_json, research_json
from parkingbreaker.route_monitor import (
    MonitorConflict,
    case_route_evidence,
    check_routes,
    enroll_route,
    main,
)
from parkingbreaker.route_registry import RouteRevision
from parkingbreaker.transaction_store import SqliteTransactionStore
from parkingbreaker.workspace import Workspace
from samples import RETRIEVED_AT, fixture
from test_research_run import (
    CONTEST_URL,
    SEEDS,
    TEST_AGENCY,
    TICKET,
    TickingClock,
    answers,
    fetcher,
)

CASE_ID = "case_1"
NOW = RETRIEVED_AT + timedelta(minutes=5)
EXPIRY = NOW + timedelta(days=7)
CONTEST = fixture("sfmta_contest.html")


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SqliteTransactionStore]:
    with SqliteTransactionStore(tmp_path / "parkingbreaker.sqlite") as database:
        database.save_workspace("ws_1", Workspace(), NOW)
        database.set_workspace_owner("ws_1", "alice", NOW)
        database.record_intake(CASE_ID, "ws_1", "{}", "{}", NOW, agency_json(TEST_AGENCY))
        yield database


@pytest.fixture
def approved() -> ResearchResult:
    return run_research(
        TICKET, TEST_AGENCY, SEEDS, fetcher(), answers(), TickingClock(), wait=lambda _: None
    )


def _save(store: SqliteTransactionStore, result: ResearchResult) -> None:
    store.save_research(CASE_ID, research_json(result), answers_json(ResearchAnswers()), NOW, None)


def _pages(html: str = CONTEST, status: int = 200) -> FakeFetcher:
    return FakeFetcher({CONTEST_URL: html_response(CONTEST_URL, html, status)})


def _enroll(
    store: SqliteTransactionStore, page_fetcher: Fetcher, *, now: datetime = NOW
) -> RouteRevision:
    (evidence,) = case_route_evidence(store, CASE_ID)
    return enroll_route(
        store,
        page_fetcher,
        lambda: now,
        case_id=CASE_ID,
        evidence_sha256=evidence.evidence_sha256,
        fingerprint=evidence.address_fingerprint,
        valid_until=EXPIRY,
        expected_revision=0,
        command_id="verified-1",
    )


def test_verified_enrollment_and_cosmetic_change_extend_only_the_heartbeat(
    store: SqliteTransactionStore, approved: ResearchResult
) -> None:
    _save(store, approved)
    cosmetic = _pages(CONTEST.replace("<body>", "<body><!-- refreshed layout -->"))
    route = _enroll(store, cosmetic)
    assert route.evidence_sha256 == approved.sources[0].sha256
    assert route.source_quote is not None
    assert "Contests sent by mail" in route.source_quote
    assert "ATTN: Citation Review" in route.source_quote
    assert cosmetic.calls == [CONTEST_URL]

    later = NOW + timedelta(minutes=55)
    after_initial_window = NOW + timedelta(minutes=61)
    assert not store.route_check_current(route, after_initial_window)
    (observed,) = check_routes(store, cosmetic, lambda: later)
    assert observed.result == "unchanged"
    assert store.route_check_current(route, after_initial_window)
    assert store.current_route(route.scope) == route
    assert not store.route_check_current(route, EXPIRY)


def test_explicit_first_level_section_controls_a_page_with_a_hearing_header(
    store: SqliteTransactionStore,
) -> None:
    section = "San Francisco parking citation first-level review"
    destination = (
        "Contest by mail SFMTA Customer Service Center ATTN: Citation Review "
        "11 South Van Ness Avenue San Francisco, CA 94103"
    )
    html = CONTEST.replace(
        "<h1>Contest a Citation</h1>",
        f"<h1>Citation reviews and administrative hearings</h1><h2>{section}</h2>",
    ).replace("</main>", "<h2>Administrative hearing</h2><p>Hearing procedures.</p></main>")

    def scoped_findings(sources: Sequence[SourceRecord]) -> list[Finding]:
        return [
            item
            if item.procedure_assessment is None
            else replace(
                item,
                procedure_assessment=replace(
                    item.procedure_assessment, scope_quote=section, where_quote=destination
                ),
            )
            for item in answers().produce(sources)
        ]

    approved = run_research(
        TICKET,
        TEST_AGENCY,
        SEEDS,
        _pages(html),
        FakeExtractor(scoped_findings),
        TickingClock(),
        wait=lambda _: None,
    )
    _save(store, approved)
    route = _enroll(store, _pages(html))
    assert route.scope.stage == "first-level review"
    (checked,) = check_routes(store, _pages(html), lambda: NOW + timedelta(minutes=1))
    assert checked.result == "unchanged"


@pytest.mark.parametrize(
    ("instruction", "ordinary_allowed"),
    [
        ("You may submit your appeal by regular mail.", True),
        ("You may submit your appeal by first-class mail.", True),
        ("You may submit your appeal by regular mail or by certified mail.", True),
        ("You may submit your appeal by certified mail or regular mail.", True),
        ("You may submit your appeal by mail.", False),
        ("You cannot submit your appeal by regular mail.", False),
        ("You may submit your payment by ordinary mail.", False),
        ("You may submit your appeal online and send payment by regular mail.", False),
        ("You may submit your appeal by certified mail or regular mail only if approved.", False),
        ("You may submit your appeal by ordinary mail only if approved.", False),
        ("You may submit your appeal by ordinary mail. Certified mail is required.", False),
        ("You may submit your appeal by ordinary mail. We no longer accept regular mail.", False),
    ],
)
def test_first_class_route_requires_its_own_filing_instruction(
    store: SqliteTransactionStore, instruction: str, ordinary_allowed: bool
) -> None:
    html = CONTEST.replace("</main>", f"<p>{instruction}</p></main>")
    result = run_research(
        TICKET, TEST_AGENCY, SEEDS, _pages(html), answers(), TickingClock(), wait=lambda _: None
    )
    _save(store, result)
    if not ordinary_allowed:
        with pytest.raises(MonitorConflict):
            case_route_evidence(store, CASE_ID, MailClass.FIRST_CLASS)
        return
    assert case_route_evidence(store, CASE_ID)
    (candidate,) = case_route_evidence(store, CASE_ID, MailClass.FIRST_CLASS)
    route = enroll_route(
        store,
        _pages(html),
        lambda: NOW,
        case_id=CASE_ID,
        evidence_sha256=candidate.evidence_sha256,
        fingerprint=candidate.address_fingerprint,
        valid_until=EXPIRY,
        expected_revision=0,
        command_id="ordinary-1",
        mail_class=MailClass.FIRST_CLASS,
    )
    assert route.scope.mail_class == "first_class"
    assert instruction in (route.source_quote or "")
    assert store.current_route(replace(route.scope, mail_class="certified")) is None


def test_enrollment_requires_a_fresh_unchanged_official_page(
    store: SqliteTransactionStore, approved: ResearchResult
) -> None:
    _save(store, approved)
    pages = _pages(CONTEST.replace("11 South Van Ness Avenue", "25 Mission Street"))
    with pytest.raises(MonitorConflict, match="official_contest_passage_changed"):
        _enroll(store, pages)
    assert pages.calls == [CONTEST_URL]
    assert not store.monitored_routes()


@pytest.mark.parametrize("wrong_field", ["fingerprint", "evidence_sha256"])
def test_enrollment_rejects_synthetic_bindings_before_fetching(
    store: SqliteTransactionStore, approved: ResearchResult, wrong_field: str
) -> None:
    _save(store, approved)
    (evidence,) = case_route_evidence(store, CASE_ID)
    fields = {
        "evidence_sha256": evidence.evidence_sha256,
        "fingerprint": evidence.address_fingerprint,
    }
    fields[wrong_field] = "f" * 64
    pages = _pages()
    with pytest.raises(MonitorConflict, match="one_approved_passage"):
        enroll_route(
            store,
            pages,
            lambda: NOW,
            case_id=CASE_ID,
            evidence_sha256=fields["evidence_sha256"],
            fingerprint=fields["fingerprint"],
            valid_until=EXPIRY,
            expected_revision=0,
            command_id="unapproved",
        )
    assert pages.calls == []


@pytest.mark.parametrize("secondary", [True, False])
def test_approval_cannot_promote_secondary_or_off_scope_source(
    store: SqliteTransactionStore, approved: ResearchResult, secondary: bool
) -> None:
    (source,) = approved.sources
    source = (
        replace(source, authority_class=AuthorityClass.SECONDARY)
        if secondary
        else replace(source, final_url="https://town-parking.example/official")
    )
    _save(store, replace(approved, sources=(source,)))
    with pytest.raises(MonitorConflict, match="official_contest_address_unverified"):
        case_route_evidence(store, CASE_ID)


def test_approval_cannot_omit_the_printed_attention_line(
    store: SqliteTransactionStore, approved: ResearchResult
) -> None:
    address = next(item for item in approved.findings if item.key is FactKey.MAILING_ADDRESS)
    assert isinstance(address.value, dict)
    findings = tuple(
        replace(address, value={**address.value, "attention": None}, evidence=())
        if item == address
        else item
        for item in approved.findings
    )
    _save(store, replace(approved, findings=findings))
    with pytest.raises(MonitorConflict, match="official_contest_passage_unverified"):
        case_route_evidence(store, CASE_ID)


@pytest.mark.parametrize(
    ("html", "status", "reason"),
    [
        (
            CONTEST.replace("11 South Van Ness Avenue", "25 Mission Street"),
            200,
            "official_contest_passage_changed",
        ),
        (
            CONTEST.replace("</main>", "<p>Do not mail contests to the address above.</p></main>"),
            200,
            "official_page_text_changed",
        ),
        ("Gone", 410, "official_page_missing"),
    ],
)
def test_source_drift_suspends_without_replacing_original_evidence(
    store: SqliteTransactionStore, approved: ResearchResult, html: str, status: int, reason: str
) -> None:
    _save(store, approved)
    old = _enroll(store, _pages())
    (observed,) = check_routes(store, _pages(html, status), lambda: NOW + timedelta(minutes=1))
    assert (observed.result, observed.reason) == ("suspended", reason)
    current = store.current_route(old.scope)
    assert current is not None
    assert (current.status, current.revision) == ("suspended", 2)
    assert current.source_quote == old.source_quote
    assert current.evidence_sha256 == old.evidence_sha256
    assert current.address_fingerprint == old.address_fingerprint
    pages = _pages()
    assert list(check_routes(store, pages, lambda: NOW + timedelta(minutes=2))) == []
    assert pages.calls == []


@pytest.mark.parametrize(
    "destination",
    ["/other-tenant/parking", "https://redirect.example/parking", "https://127.0.0.1/parking"],
)
def test_check_never_follows_redirects_and_expires_cleanly(
    store: SqliteTransactionStore, approved: ResearchResult, destination: str
) -> None:
    _save(store, approved)
    old = _enroll(store, _pages())
    redirects = FakeFetcher({CONTEST_URL: redirect_response(CONTEST_URL, destination)})
    (observed,) = check_routes(store, redirects, lambda: NOW + timedelta(minutes=55))
    assert (observed.result, observed.reason) == ("unverified", "official_page_unavailable")
    assert redirects.calls == [CONTEST_URL]
    assert store.current_route(old.scope) == old
    assert not store.route_check_current(old, NOW + timedelta(minutes=61))
    (expired,) = check_routes(store, redirects, lambda: EXPIRY)
    assert expired.result == "expired"
    assert redirects.calls == [CONTEST_URL]


def test_allowlisted_host_cannot_resolve_to_a_private_address(
    store: SqliteTransactionStore, approved: ResearchResult
) -> None:
    _save(store, approved)
    route = _enroll(store, _pages())
    resolved: list[str] = []

    def resolver(host: str) -> tuple[str, ...]:
        resolved.append(host)
        return ("127.0.0.1",)

    (observed,) = check_routes(store, HttpFetcher(resolver=resolver), lambda: NOW)
    assert (observed.result, observed.reason) == ("unverified", "official_page_unavailable")
    assert resolved == ["www.sfmta.com"]
    assert store.current_route(route.scope) == route


def test_watcher_cannot_overwrite_an_operator_decision_committed_during_fetch(
    store: SqliteTransactionStore, approved: ResearchResult
) -> None:
    _save(store, approved)
    old = _enroll(store, _pages())

    class ConcurrentDecision:
        def get(self, url: str) -> FetchResponse:
            store.change_route(
                scope=old.scope,
                expected_revision=old.revision,
                command_id="revoke-after-official-conflict",
                status="revoked",
                address_fingerprint=old.address_fingerprint,
                source_url=old.source_url,
                source_quote=old.source_quote,
                evidence_sha256=old.evidence_sha256,
                valid_until=old.valid_until,
                now=NOW,
            )
            return html_response(url, "No contest mailing address")

    (observed,) = check_routes(store, ConcurrentDecision(), lambda: NOW)
    assert observed.result == "superseded"
    current = store.current_route(old.scope)
    assert current is not None
    assert (current.status, current.revision) == ("revoked", 2)


def test_watch_write_contention_remains_visible_and_retries(
    store: SqliteTransactionStore, approved: ResearchResult
) -> None:
    _save(store, approved)
    old = _enroll(store, _pages())
    pages = _pages("New destination instructions")
    with store.submission_ownership():
        (blocked,) = check_routes(store, pages, lambda: NOW)
        assert not store.route_check_current(old, NOW)
    assert (blocked.result, blocked.reason) == ("unverified", "submission_ownership_unavailable")
    assert store.current_route(old.scope) == old
    restored_page = _pages()
    (retried,) = check_routes(store, restored_page, lambda: NOW)
    assert retried.result == "suspended"
    assert not restored_page.calls


@pytest.mark.parametrize("marker", ["RESTORE_QUARANTINE.json", "FAKE_RECOVERY_ONLY"])
def test_cli_checks_restore_markers_before_opening_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], marker: str
) -> None:
    path = tmp_path / "parkingbreaker.sqlite"
    path.write_bytes(b"restored data")
    (tmp_path / marker).touch()
    assert main(["--database", str(path), "check"]) == 1
    assert json.loads(capsys.readouterr().err) == {
        "error": "route_management_disabled_on_restored_data"
    }
    assert path.read_bytes() == b"restored data"


def test_cli_can_revoke_exact_scope_immediately(
    store: SqliteTransactionStore,
    approved: ResearchResult,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _save(store, approved)
    now = min(NOW, datetime.now(UTC) - timedelta(minutes=1))
    old = _enroll(store, _pages(), now=now)
    args = [
        "--database",
        str(tmp_path / "parkingbreaker.sqlite"),
        "revoke",
        "--issuer",
        old.scope.issuer,
        "--stage",
        old.scope.stage,
        "--expected-revision",
        str(old.revision),
        "--command-id",
        "operator-revoke",
    ]
    assert main(args) == 0
    outcome = json.loads(capsys.readouterr().out)
    assert outcome["status"] == "revoked"
    assert outcome["official_check_current"] is False
    assert outcome["revision"] == 2
    assert outcome["address_fingerprint"] == old.address_fingerprint


def test_cli_can_suspend_a_known_scope_without_enrollment(
    store: SqliteTransactionStore, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = [
        "--database",
        str(tmp_path / "parkingbreaker.sqlite"),
        "suspend",
        "--issuer",
        TEST_AGENCY.id,
        "--stage",
        "initial review",
        "--expected-revision",
        "0",
        "--command-id",
        "operator-hold-before-enrollment",
    ]
    assert main(args) == 0
    outcome = json.loads(capsys.readouterr().out)
    assert outcome["status"] == "suspended"
    assert outcome["revision"] == 1
    (route,) = store.monitored_routes()
    assert route.scope.stage == "first-level review"
    assert route.address_fingerprint is None
    assert route.source_url is None
