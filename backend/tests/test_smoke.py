"""Offline smoke CLI proofs: injected SDK transport and Lob client, never sockets."""

import io
import os
from collections.abc import Callable
from pathlib import Path

import pytest
from pypdf import PdfReader

from parkingbreaker import config
from parkingbreaker.agencies import LobVerification, LobVerificationStatus, MailingAddress
from parkingbreaker.lob_client import MetadataLookupFailure
from parkingbreaker.lob_submission import LetterEvidence, LetterRequest, LobResponse, MetadataLookup
from parkingbreaker.mail_config import MailClass
from parkingbreaker.payment_records import PaymentClient
from parkingbreaker.smoke import (
    LOB_API_VERSION,
    READINESS_BUDGET_SECONDS,
    TEST_ADDRESS,
    TEST_VERIFICATION_ADDRESS,
    SmokeLobClient,
    _lob_client,
    load_environment,
    main,
    smoke_letter,
)
from parkingbreaker.stripe_payment import STRIPE_API_VERSION, StripePaymentClient
from test_stripe_payment import REF, Transport, fixture

TEST_ENV = {"STRIPE_SECRET_KEY": "sk_test_offline", "LOB_API_KEY": "test_offline"}


class FakeClock:
    """Deterministic readiness clock: sleeping advances time without waiting."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture(autouse=True)
def fake_readiness_clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    # Negative readiness tests poll to the 120-second deadline; the fake clock keeps
    # that deterministic and instantaneous without weakening the production budget.
    clock = FakeClock()
    monkeypatch.setattr("parkingbreaker.smoke._monotonic", clock.monotonic)
    monkeypatch.setattr("parkingbreaker.smoke._sleep", clock.sleep)
    return clock


class Lob:
    def __init__(self, fail: str = "") -> None:
        self.fail = fail
        self.calls: list[str] = []
        self.tokens: list[str] = []
        self.certified_tokens: list[str] = []
        self.keys: list[str] = []

    def verify_address(self, address: MailingAddress) -> LobVerification:
        self.calls.append("verify")
        assert address == TEST_VERIFICATION_ADDRESS
        return LobVerification(
            LobVerificationStatus.UNVERIFIED
            if self.fail == "verify"
            else LobVerificationStatus.DELIVERABLE
        )

    def create_letter(self, request: LetterRequest, idempotency_key: str) -> LobResponse:
        self.calls.append("create")
        assert request.sender == request.recipient == TEST_ADDRESS
        reader = PdfReader(io.BytesIO(request.pdf))
        assert len(reader.pages) == 1
        assert tuple(reader.pages[0].mediabox) == (0, 0, 612, 792)
        if request.options.mail_class is MailClass.CERTIFIED:
            assert request.submission_token == f"{self.tokens[-1]}-certified"
            if self.fail == "certified":
                return LobResponse(422, error="provider validation rejected")
            self.certified_tokens.append(request.submission_token)
            return LobResponse(200, "ltr_Certified1", evidence=LetterEvidence.CREATED)
        self.tokens.append(request.submission_token)
        self.keys.append(idempotency_key)
        if self.fail == "create":
            raise RuntimeError("test_secret_that_must_not_be_printed")
        return LobResponse(200, "ltr_Offline123", evidence=LetterEvidence.PRODUCTION)

    def find_by_metadata(self, submission_token: str) -> MetadataLookup:
        self.calls.append("find")
        if self.fail == "find":
            return MetadataLookup(None)
        if self.certified_tokens and submission_token == self.certified_tokens[-1]:
            return MetadataLookup(
                "ltr_Certified1",
                evidence=LetterEvidence.RENDERED,
                tracking_number=(None if self.fail == "tracking" else "9407111899223197428490"),
            )
        assert submission_token == self.tokens[-1]
        return MetadataLookup("ltr_Offline123", evidence=LetterEvidence.PRODUCTION)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("network forbidden")

    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr("socket.create_connection", forbidden)


def never_stripe(_key: str) -> PaymentClient:
    pytest.fail("Stripe must not be constructed")


def never_lob(_key: str) -> SmokeLobClient:
    pytest.fail("Lob must not be constructed")


@pytest.mark.parametrize("name", ["STRIPE_SECRET_KEY", "LOB_API_KEY"])
@pytest.mark.parametrize(
    "value", [None, "", "sk_live_PRIVATE", "live_PRIVATE", "sk_test_", "test_"]
)
def test_refuses_missing_or_non_test_keys_without_echoing_values(
    name: str, value: str | None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    environment = dict(TEST_ENV)
    if value is None:
        del environment[name]
    else:
        environment[name] = value
    assert (
        main(
            [],
            environment=environment,
            env_path=tmp_path / "missing",
            stripe_factory=never_stripe,
            lob_factory=never_lob,
        )
        == 1
    )
    output = capsys.readouterr().out
    assert f"FAIL {name}" in output
    if value:
        assert value not in output


def test_env_comments_quotes_and_environment_wins(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        '# comment\n STRIPE_SECRET_KEY = "sk_test_file" # comment\n'
        "LOB_API_KEY='test_quoted#value'\nEMPTY=\nBAD LINE\n"
        "BROKEN='unterminated\nexport X=ignored\nOTHER=${NO_EXPANSION}\n",
        encoding="utf-8",
    )
    assert load_environment(path, {"STRIPE_SECRET_KEY": "", "EXTRA": "env"}) == {
        "STRIPE_SECRET_KEY": "",
        "LOB_API_KEY": "test_quoted#value",
        "EMPTY": "",
        "OTHER": "${NO_EXPANSION}",
        "EXTRA": "env",
    }


def test_full_run_injected_stripe_transport_and_lob_prints_six_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    transport = Transport(
        fixture(), fixture(), fixture("canceled"), fixture(), fixture("succeeded")
    )
    lob = Lob()

    def stripe_factory(_key: str) -> PaymentClient:
        return StripePaymentClient(
            secret_key="sk_test_offline", api_version=STRIPE_API_VERSION, transport=transport
        )

    def lob_factory(_key: str) -> SmokeLobClient:
        return lob

    assert (
        main(
            [],
            environment=TEST_ENV,
            env_path=tmp_path / "missing",
            stripe_factory=stripe_factory,
            lob_factory=lob_factory,
        )
        == 0
    )
    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        f"PASS stripe.authorize {REF}",
        f"PASS stripe.retrieve {REF}",
        f"PASS stripe.release {REF}",
        f"PASS stripe.capture {REF}",
        "PASS lob.verify_address",
        "PASS lob.create_letter ltr_Offline123",
        "PASS lob.find_by_metadata ltr_Offline123",
        "PASS lob.create_certified ltr_Certified1",
        "PASS lob.certified_tracking",
    ]
    assert len(transport.calls) == 5
    assert transport.calls[4][1].endswith("/capture")
    assert all(call[4] == STRIPE_API_VERSION for call in transport.calls)
    assert lob.calls == ["verify", "create", "find", "create", "find"]
    assert not any(value in "\n".join(lines) for value in TEST_ENV.values())


@pytest.mark.parametrize("step", ["verify", "create", "find", "certified", "tracking"])
def test_lob_failing_step_exits_nonzero_without_provider_details(
    step: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def lob_factory(_key: str) -> SmokeLobClient:
        return Lob(step)

    assert (
        main(
            ["--lob-only"],
            environment=TEST_ENV,
            env_path=tmp_path / "missing",
            stripe_factory=never_stripe,
            lob_factory=lob_factory,
        )
        == 1
    )
    output = capsys.readouterr().out
    assert "FAIL lob." in output
    assert "test_secret" not in output
    assert "stripe." not in output


def test_stripe_retrieval_failure_still_releases_hold_and_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    transport = Transport(
        fixture(), RuntimeError("secret"), fixture("canceled"), fixture(), fixture("succeeded")
    )

    def stripe_factory(_key: str) -> PaymentClient:
        return StripePaymentClient(
            secret_key="sk_test_offline", api_version=STRIPE_API_VERSION, transport=transport
        )

    assert (
        main(
            ["--stripe-only"],
            environment=TEST_ENV,
            env_path=tmp_path / "missing",
            stripe_factory=stripe_factory,
            lob_factory=never_lob,
        )
        == 1
    )
    assert capsys.readouterr().out.splitlines() == [
        f"PASS stripe.authorize {REF}",
        f"FAIL stripe.retrieve {REF}",
        f"PASS stripe.release {REF}",
        f"PASS stripe.capture {REF}",
    ]
    assert len(transport.calls) == 5


def test_fresh_submission_metadata_and_idempotency_keys(
    tmp_path: Path,
) -> None:
    lob = Lob()

    def lob_factory(_key: str) -> SmokeLobClient:
        return lob

    for _ in range(2):
        assert (
            main(
                ["--lob-only"],
                environment=TEST_ENV,
                env_path=tmp_path / "missing",
                stripe_factory=never_stripe,
                lob_factory=lob_factory,
            )
            == 0
        )
    assert len(set(lob.tokens)) == len(set(lob.keys)) == 2
    assert smoke_letter("fixed") == smoke_letter("fixed")


def test_failed_capture_exits_nonzero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    transport = Transport(
        fixture(), fixture(), fixture("canceled"), fixture(), fixture("requires_capture")
    )

    def stripe_factory(_key: str) -> PaymentClient:
        return StripePaymentClient(
            secret_key="sk_test_offline", api_version=STRIPE_API_VERSION, transport=transport
        )

    assert (
        main(
            ["--stripe-only"],
            environment=TEST_ENV,
            env_path=tmp_path / "missing",
            stripe_factory=stripe_factory,
            lob_factory=never_lob,
        )
        == 1
    )
    assert capsys.readouterr().out.splitlines()[-1] == f"FAIL stripe.capture {REF}"


SHAPE_NOTE = "key shape only; not authentication"
STRIPE_ONLY_ENV = {"STRIPE_SECRET_KEY": "sk_test_offline"}
LOB_ONLY_ENV = {"LOB_API_KEY": "test_offline"}


def stripe_factory_for(transport: Transport) -> Callable[[str], PaymentClient]:
    def factory(key: str) -> PaymentClient:
        return StripePaymentClient(
            secret_key=key, api_version=STRIPE_API_VERSION, transport=transport
        )

    return factory


def test_lob_only_ignores_missing_stripe_and_never_constructs_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lob = Lob()

    def lob_factory(_key: str) -> SmokeLobClient:
        return lob

    assert (
        main(
            ["--lob-only"],
            environment=LOB_ONLY_ENV,
            env_path=tmp_path / "missing",
            stripe_factory=never_stripe,
            lob_factory=lob_factory,
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "stripe" not in output
    assert "FAIL" not in output


def test_stripe_only_ignores_missing_lob_and_never_constructs_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    transport = Transport(
        fixture(), fixture(), fixture("canceled"), fixture(), fixture("succeeded")
    )
    assert (
        main(
            ["--stripe-only"],
            environment=STRIPE_ONLY_ENV,
            env_path=tmp_path / "missing",
            stripe_factory=stripe_factory_for(transport),
            lob_factory=never_lob,
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "lob." not in output
    assert "FAIL" not in output


@pytest.mark.parametrize(
    ("environment", "missing"),
    [
        ({"LOB_API_KEY": "test_offline"}, "STRIPE_SECRET_KEY"),
        ({"STRIPE_SECRET_KEY": "", "LOB_API_KEY": "test_offline"}, "STRIPE_SECRET_KEY"),
        (
            {"STRIPE_SECRET_KEY": "sk_live_PRIVATE", "LOB_API_KEY": "test_offline"},
            "STRIPE_SECRET_KEY",
        ),
        ({"STRIPE_SECRET_KEY": "sk_test_offline"}, "LOB_API_KEY"),
        (
            {"STRIPE_SECRET_KEY": "sk_test_offline", "LOB_API_KEY": "test_pub_PRIVATE"},
            "LOB_API_KEY",
        ),
        ({"STRIPE_SECRET_KEY": "sk_test_offline", "LOB_API_KEY": ""}, "LOB_API_KEY"),
        (
            {"STRIPE_SECRET_KEY": "rk_live_PRIVATE", "LOB_API_KEY": "live_PRIVATE"},
            "STRIPE_SECRET_KEY and LOB_API_KEY",
        ),
    ],
)
def test_full_run_admits_both_keys_before_any_factory(
    environment: dict[str, str],
    missing: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        main(
            [],
            environment=environment,
            env_path=tmp_path / "missing",
            stripe_factory=never_stripe,
            lob_factory=never_lob,
        )
        == 1
    )
    output = capsys.readouterr().out
    assert output.splitlines() == [f"FAIL {name}" for name in missing.split(" and ")]


@pytest.mark.parametrize(
    "key", ["sk_test_a", "rk_test_a", "rkcs_test_a", "sk_test_ABC-_.~123", "rk_test_0"]
)
def test_stripe_accepts_only_named_test_shapes(
    key: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(
            ["--stripe-only", "--check-config"],
            environment={"STRIPE_SECRET_KEY": key},
            env_path=tmp_path / "missing",
            stripe_factory=never_stripe,
            lob_factory=never_lob,
        )
        == 0
    )
    assert capsys.readouterr().out.splitlines() == [f"PASS stripe.key_shape ({SHAPE_NOTE})"]


@pytest.mark.parametrize(
    "key",
    [
        "",
        "sk_test_",
        "rk_test_",
        "rkcs_test_",
        "sk_live_PRIVATE",
        "rk_live_PRIVATE",
        "rkcs_live_PRIVATE",
        "pk_test_PRIVATE",
        "pk_live_PRIVATE",
        "sk_pub_PRIVATE",
        "test_PRIVATE",
        "sk_test_ with space",
        "sk_test_tab\there",
        "sk_test_line\nbreak",
        "sk_test_null\x00byte",
        "sk_test_del\x7fbyte",
        "sk_test_caf\u00e9",
        "sk_test_leading ",
        " sk_test_leading",
        "sk_test_carriage\r",
        "sk_test_ok\u00a0",
    ],
)
def test_stripe_refuses_live_publishable_malformed_and_unsafe_shapes_without_echoing(
    key: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(
            ["--stripe-only", "--check-config"],
            environment={"STRIPE_SECRET_KEY": key},
            env_path=tmp_path / "missing",
            stripe_factory=never_stripe,
            lob_factory=never_lob,
        )
        == 1
    )
    output = capsys.readouterr().out
    assert output.splitlines() == [f"FAIL stripe.key_shape ({SHAPE_NOTE})"]
    if key:
        assert key not in output


@pytest.mark.parametrize("key", ["test_a", "test_ABC-_.~123", "test_0"])
def test_lob_accepts_only_header_safe_secret_test_shapes(
    key: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(
            ["--lob-only", "--check-config"],
            environment={"LOB_API_KEY": key},
            env_path=tmp_path / "missing",
            stripe_factory=never_stripe,
            lob_factory=never_lob,
        )
        == 0
    )
    assert capsys.readouterr().out.splitlines() == [f"PASS lob.key_shape ({SHAPE_NOTE})"]


@pytest.mark.parametrize(
    "key",
    [
        "",
        "test_",
        "test_pub_PRIVATE",
        "test_pub_",
        "live_PRIVATE",
        "live_pub_PRIVATE",
        "pub_test_PRIVATE",
        "test_ with space",
        "test_tab\there",
        "test_line\nbreak",
        "test_null\x00byte",
        "test_del\x7fbyte",
        "test_caf\u00e9",
        "test_leading ",
        " test_leading",
        "test_carriage\r",
        "TEST_PRIVATE",
        "tset_test_PRIVATE",
        "sk_test_PRIVATE",
    ],
)
def test_lob_refuses_publishable_live_malformed_and_unsafe_shapes_without_echoing(
    key: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(
            ["--lob-only", "--check-config"],
            environment={"LOB_API_KEY": key},
            env_path=tmp_path / "missing",
            stripe_factory=never_stripe,
            lob_factory=never_lob,
        )
        == 1
    )
    output = capsys.readouterr().out
    assert output.splitlines() == [f"FAIL lob.key_shape ({SHAPE_NOTE})"]
    if key:
        assert key not in output


def test_check_config_both_providers_is_shape_only_without_factories(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(
            ["--check-config"],
            environment=TEST_ENV,
            env_path=tmp_path / "missing",
            stripe_factory=never_stripe,
            lob_factory=never_lob,
        )
        == 0
    )
    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        f"PASS stripe.key_shape ({SHAPE_NOTE})",
        f"PASS lob.key_shape ({SHAPE_NOTE})",
    ]
    assert all("not authentication" in line for line in lines)
    assert not any("authenticated" in line for line in lines)
    assert not any(value in "\n".join(lines) for value in TEST_ENV.values())


def test_check_config_selects_only_the_requested_provider(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(
            ["--lob-only", "--check-config"],
            environment=LOB_ONLY_ENV,
            env_path=tmp_path / "missing",
            stripe_factory=never_stripe,
            lob_factory=never_lob,
        )
        == 0
    )
    assert capsys.readouterr().out.splitlines() == [f"PASS lob.key_shape ({SHAPE_NOTE})"]
    assert (
        main(
            ["--lob-only", "--check-config"],
            environment={"LOB_API_KEY": "test_pub_PRIVATE"},
            env_path=tmp_path / "missing",
            stripe_factory=never_stripe,
            lob_factory=never_lob,
        )
        == 1
    )
    assert capsys.readouterr().out.splitlines() == [f"FAIL lob.key_shape ({SHAPE_NOTE})"]


def test_explicit_env_file_loads_with_process_environment_precedence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "credentials.env"
    path.write_text(
        "STRIPE_SECRET_KEY=sk_test_from_file\nLOB_API_KEY=test_from_file\n", encoding="utf-8"
    )
    assert (
        main(
            ["--check-config", "--env-file", str(path)],
            environment={},
            env_path=tmp_path / "missing",
            stripe_factory=never_stripe,
            lob_factory=never_lob,
        )
        == 0
    )
    first = capsys.readouterr().out
    assert first.splitlines() == [
        f"PASS stripe.key_shape ({SHAPE_NOTE})",
        f"PASS lob.key_shape ({SHAPE_NOTE})",
    ]
    assert "sk_test_from_file" not in first
    assert "test_from_file" not in first
    assert (
        main(
            ["--check-config", "--env-file", str(path)],
            environment={"STRIPE_SECRET_KEY": "sk_live_override"},
            env_path=tmp_path / "missing",
            stripe_factory=never_stripe,
            lob_factory=never_lob,
        )
        == 1
    )
    second = capsys.readouterr().out
    assert second.splitlines() == [
        f"FAIL stripe.key_shape ({SHAPE_NOTE})",
        f"PASS lob.key_shape ({SHAPE_NOTE})",
    ]
    assert "sk_live_override" not in second
    assert "sk_test_from_file" not in second


def test_explicit_env_file_supplies_only_the_selected_provider(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "credentials.env"
    path.write_text("LOB_API_KEY=test_from_file\n", encoding="utf-8")
    assert (
        main(
            ["--lob-only", "--check-config", "--env-file", str(path)],
            environment={},
            env_path=tmp_path / "missing",
            stripe_factory=never_stripe,
            lob_factory=never_lob,
        )
        == 0
    )
    assert capsys.readouterr().out.splitlines() == [f"PASS lob.key_shape ({SHAPE_NOTE})"]


@pytest.mark.parametrize("kind", ["missing", "directory", "unreadable"])
def test_explicit_env_file_failures_are_sanitized_before_any_factory(
    kind: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "credentials.env"
    if kind == "directory":
        path.mkdir()
    elif kind == "unreadable":
        path.write_text("STRIPE_SECRET_KEY=sk_live_FILE_SUPERSECRET\n", encoding="utf-8")
        path.chmod(0o000)
        if os.geteuid() == 0:
            pytest.skip("root bypasses file permissions")
    for argv in (
        ["--check-config", "--env-file", str(path)],
        ["--stripe-only", "--env-file", str(path)],
        ["--env-file", str(path)],
    ):
        assert (
            main(
                argv,
                environment={},
                env_path=tmp_path / "missing",
                stripe_factory=never_stripe,
                lob_factory=never_lob,
            )
            == 1
        )
        output = capsys.readouterr().out
        assert output.splitlines() == ["FAIL configuration"]
        assert "FILE_SUPERSECRET" not in output
    if kind == "unreadable":
        path.chmod(0o600)


def test_selected_mode_ignores_invalid_unselected_provider(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lob = Lob()

    def lob_factory(_key: str) -> SmokeLobClient:
        return lob

    assert (
        main(
            ["--lob-only"],
            environment={"LOB_API_KEY": "test_offline", "STRIPE_SECRET_KEY": "sk_live_PRIVATE"},
            env_path=tmp_path / "missing",
            stripe_factory=never_stripe,
            lob_factory=lob_factory,
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "stripe" not in output
    assert "sk_live_PRIVATE" not in output


def test_check_config_full_mode_reports_both_shapes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(
            ["--check-config"],
            environment={"STRIPE_SECRET_KEY": "sk_test_offline", "LOB_API_KEY": "test_pub_PRIVATE"},
            env_path=tmp_path / "missing",
            stripe_factory=never_stripe,
            lob_factory=never_lob,
        )
        == 1
    )
    assert capsys.readouterr().out.splitlines() == [
        f"PASS stripe.key_shape ({SHAPE_NOTE})",
        f"FAIL lob.key_shape ({SHAPE_NOTE})",
    ]


DOCUMENTED_LOB_API_VERSION = "2020-02-11"


def test_shared_lob_api_version_is_the_documented_date() -> None:
    assert LOB_API_VERSION == DOCUMENTED_LOB_API_VERSION


def test_lob_client_factory_forwards_the_documented_pin_and_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, float]] = []

    class RecordingLobSdkClient:
        def __init__(self, key: str, api_version: str, timeout: float) -> None:
            calls.append((key, api_version, timeout))

    monkeypatch.setattr("parkingbreaker.smoke.LobSdkClient", RecordingLobSdkClient)
    client = _lob_client("test_offline")
    assert isinstance(client, RecordingLobSdkClient)
    assert calls == [("test_offline", LOB_API_VERSION, 20)]


def test_config_consumes_the_same_documented_lob_pin() -> None:
    # config.py imports this exact shared constant; no constructor or socket is touched.
    assert vars(config)["LOB_API_VERSION"] is LOB_API_VERSION
    assert vars(config)["LOB_API_VERSION"] == DOCUMENTED_LOB_API_VERSION
    runtime_names = config.build_services.__code__.co_names
    assert "LOB_API_VERSION" in runtime_names
    assert "LobSdkClient" in runtime_names


class PollingLob:
    """Create each original once, then report readiness after a configured delay."""

    def __init__(
        self,
        *,
        pending_ordinary: int = 0,
        pending_certified: int = 0,
        tracking: str | None = "9407111899223197428490",
        wrong_id: str | None = None,
        lookup_fails: bool = False,
    ) -> None:
        self.pending_ordinary = pending_ordinary
        self.pending_certified = pending_certified
        self.tracking = tracking
        self.wrong_id = wrong_id
        self.lookup_fails = lookup_fails
        self.creates: list[str] = []
        self.lookups: list[str] = []
        self.verified: list[MailingAddress] = []
        self.ordinary_token: str | None = None
        self.certified_token: str | None = None

    def verify_address(self, address: MailingAddress) -> LobVerification:
        self.verified.append(address)
        assert address == TEST_VERIFICATION_ADDRESS
        return LobVerification(LobVerificationStatus.DELIVERABLE)

    def create_letter(self, request: LetterRequest, idempotency_key: str) -> LobResponse:
        assert request.sender == request.recipient == TEST_ADDRESS
        self.creates.append(request.submission_token)
        if request.options.mail_class is MailClass.CERTIFIED:
            self.certified_token = request.submission_token
            return LobResponse(200, "ltr_Certified1", evidence=LetterEvidence.CREATED)
        self.ordinary_token = request.submission_token
        return LobResponse(200, "ltr_Offline123", evidence=LetterEvidence.CREATED)

    def find_by_metadata(self, submission_token: str) -> MetadataLookup:
        self.lookups.append(submission_token)
        if self.lookup_fails:
            raise MetadataLookupFailure
        if self.certified_token is not None and submission_token == self.certified_token:
            if self.pending_certified > 0:
                self.pending_certified -= 1
                return MetadataLookup(None)
            return MetadataLookup(
                "ltr_Certified1",
                evidence=LetterEvidence.RENDERED,
                tracking_number=self.tracking,
            )
        if self.wrong_id is not None:
            return MetadataLookup(self.wrong_id)
        if self.pending_ordinary > 0:
            self.pending_ordinary -= 1
            return MetadataLookup(None)
        return MetadataLookup("ltr_Offline123", evidence=LetterEvidence.RENDERED)


def run_lob_only(lob: PollingLob, tmp_path: Path) -> int:
    def lob_factory(_key: str) -> SmokeLobClient:
        return lob

    return main(
        ["--lob-only"],
        environment=LOB_ONLY_ENV,
        env_path=tmp_path / "missing",
        stripe_factory=never_stripe,
        lob_factory=lob_factory,
    )


def test_create_before_render_then_readiness_for_the_same_originals(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], fake_readiness_clock: FakeClock
) -> None:
    lob = PollingLob(pending_ordinary=3, pending_certified=2)
    assert run_lob_only(lob, tmp_path) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[-4:] == [
        "PASS lob.create_letter ltr_Offline123",
        "PASS lob.find_by_metadata ltr_Offline123",
        "PASS lob.create_certified ltr_Certified1",
        "PASS lob.certified_tracking",
    ]
    # Exactly the two intentional creates; every poll reused of the same original token.
    assert len(lob.creates) == 2
    assert set(lob.creates) == {lob.ordinary_token, lob.certified_token}
    assert lob.lookups[:4] == [lob.ordinary_token] * 4
    assert lob.lookups[4:] == [lob.certified_token] * 3
    assert fake_readiness_clock.now == 10.0
    assert fake_readiness_clock.sleeps == [2.0] * 5


def test_mismatched_original_id_fails_immediately_without_retrying(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lob = PollingLob(wrong_id="ltr_Wrong999")
    assert run_lob_only(lob, tmp_path) == 1
    output = capsys.readouterr().out
    assert "FAIL lob.find_by_metadata ltr_Wrong999" in output.splitlines()
    assert lob.ordinary_token is not None
    assert lob.lookups.count(lob.ordinary_token) == 1
    assert len(lob.creates) == 2


def test_never_ready_refusal_is_bounded_and_creates_only_twice(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], fake_readiness_clock: FakeClock
) -> None:
    lob = PollingLob(pending_ordinary=10**9)
    assert run_lob_only(lob, tmp_path) == 1
    lines = capsys.readouterr().out.splitlines()
    assert "FAIL lob.find_by_metadata" in lines
    # The ordinary stage spent the one shared budget, so certified may not lookup or pass.
    assert "FAIL lob.certified_tracking" in lines
    assert len(lob.creates) == 2
    assert lob.ordinary_token is not None
    assert lob.lookups.count(lob.ordinary_token) == 60
    assert lob.certified_token is not None
    assert lob.lookups.count(lob.certified_token) == 0
    assert fake_readiness_clock.now == READINESS_BUDGET_SECONDS


def test_matched_original_without_tracking_still_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], fake_readiness_clock: FakeClock
) -> None:
    lob = PollingLob(tracking=None)
    assert run_lob_only(lob, tmp_path) == 1
    lines = capsys.readouterr().out.splitlines()
    assert "PASS lob.find_by_metadata ltr_Offline123" in lines
    assert lines[-1] == "FAIL lob.certified_tracking"
    assert lob.certified_token is not None
    assert lob.lookups.count(lob.certified_token) == 60
    assert fake_readiness_clock.now == READINESS_BUDGET_SECONDS


def test_lookup_exceptions_remain_pending_and_bounded(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], fake_readiness_clock: FakeClock
) -> None:
    lob = PollingLob(lookup_fails=True)
    assert run_lob_only(lob, tmp_path) == 1
    assert capsys.readouterr().out.splitlines() == [
        "PASS lob.verify_address",
        "PASS lob.create_letter ltr_Offline123",
        "FAIL lob.find_by_metadata",
        "PASS lob.create_certified ltr_Certified1",
        "FAIL lob.certified_tracking",
    ]
    assert len(lob.creates) == 2
    assert fake_readiness_clock.now == READINESS_BUDGET_SECONDS


def test_verification_fixture_is_separate_from_the_mailed_address(
    tmp_path: Path,
) -> None:
    # A TEST key proves a test response only; the documented fixture is not a real street.
    assert TEST_VERIFICATION_ADDRESS.line1 == "deliverable"
    assert TEST_VERIFICATION_ADDRESS.zip_code == "11111"
    assert TEST_VERIFICATION_ADDRESS != TEST_ADDRESS
    assert TEST_ADDRESS.line1 == "185 Berry St"
    lob = PollingLob()
    assert run_lob_only(lob, tmp_path) == 0
    assert lob.verified == [TEST_VERIFICATION_ADDRESS]
    assert lob.creates == [lob.ordinary_token, lob.certified_token]


def test_stripe_only_never_waits_or_touches_lob(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_readiness_clock: FakeClock,
) -> None:
    slept: list[float] = []
    monkeypatch.setattr("parkingbreaker.smoke._sleep", slept.append)
    transport = Transport(
        fixture(), fixture(), fixture("canceled"), fixture(), fixture("succeeded")
    )
    assert (
        main(
            ["--stripe-only"],
            environment=STRIPE_ONLY_ENV,
            env_path=tmp_path / "missing",
            stripe_factory=stripe_factory_for(transport),
            lob_factory=never_lob,
        )
        == 0
    )
    assert slept == []
    assert fake_readiness_clock.now == 0.0
    assert "lob." not in capsys.readouterr().out


def test_lookup_that_crosses_the_deadline_cannot_pass(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    fake_readiness_clock: FakeClock,
) -> None:
    monkeypatch.setattr("parkingbreaker.smoke.READINESS_BUDGET_SECONDS", 4.0)
    started: list[float] = []

    class SlowLookupLob(PollingLob):
        def find_by_metadata(self, submission_token: str) -> MetadataLookup:
            started.append(fake_readiness_clock.now)
            found = super().find_by_metadata(submission_token)
            # The lookup itself consumed the remaining shared budget.
            fake_readiness_clock.now = 4.0
            return found

    lob = SlowLookupLob()
    assert run_lob_only(lob, tmp_path) == 1
    lines = capsys.readouterr().out.splitlines()
    assert "FAIL lob.find_by_metadata" in lines
    assert "FAIL lob.certified_tracking" in lines
    assert len(lob.creates) == 2
    assert started == [0.0]
    assert fake_readiness_clock.now == 4.0
