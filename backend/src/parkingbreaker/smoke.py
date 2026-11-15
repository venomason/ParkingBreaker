"""Owner-invoked TEST-account smoke check: python -m parkingbreaker.smoke.

Only the selected provider is required: --stripe-only neither requires nor constructs
Lob, and --lob-only is symmetric. The shared credential-file loader may still parse
other fields, so selection does not mean an unused provider value was never read. A full
run admits both TEST keys before any factory construction. --check-config proves key
shape only; it is preparation, never authentication evidence. Keys, credential-file
contents and provider exception text are never printed.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import re
import shlex
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from pypdf import PdfWriter

from parkingbreaker import prices
from parkingbreaker.agencies import LobVerification, LobVerificationStatus, MailingAddress
from parkingbreaker.lob_client import LobSdkClient
from parkingbreaker.lob_submission import LetterOptions, LetterRequest, LobClient, MetadataLookup
from parkingbreaker.mail_config import MailClass
from parkingbreaker.mail_request import validate_request
from parkingbreaker.payment_records import PaymentClient
from parkingbreaker.prices import CERTIFIED_CENTS
from parkingbreaker.provider_credentials import valid_header_credential
from parkingbreaker.stripe_payment import STRIPE_API_VERSION, StripePaymentClient

# Lob documents the current API version as 2020-02-11: https://docs.lob.com/
LOB_API_VERSION = "2020-02-11"
FIRST_CLASS = LetterOptions(MailClass.FIRST_CLASS)
# Lob's documented TEST environment renders primary_line="deliverable" with any five-digit
# ZIP (for example "11111") as deliverable: https://docs.lob.com/. This fixture proves a
# TEST response only; it is never evidence that a real street address is deliverable.
TEST_VERIFICATION_ADDRESS = MailingAddress(
    "ParkingBreaker Test", None, "deliverable", None, "San Francisco", "CA", "11111"
)
# Mailed PDF sender/recipient: a real-looking address is deliberately kept separate from
# the verification fixture, because a TEST key does not verify a real street.
TEST_ADDRESS = MailingAddress(
    "ParkingBreaker Test", None, "185 Berry St", "Suite 6600", "San Francisco", "CA", "94107"
)

# One shared, finite readiness budget per Lob smoke run. Creates are never repeated;
# metadata/tracking readiness polls only the same original submission token.
READINESS_BUDGET_SECONDS = 120.0
READINESS_POLL_SECONDS = 2.0
_monotonic: Callable[[], float] = time.monotonic
_sleep: Callable[[float], None] = time.sleep

STRIPE_TEST_PREFIXES = ("sk_test_", "rk_test_", "rkcs_test_")
LOB_TEST_PREFIX = "test_"
# Lob publishable keys are non-secret browser-side tokens; test_pub_ still starts test_.
LOB_PUBLISHABLE_PREFIX = "test_pub_"
KEY_ENVIRONMENT = {"stripe": "STRIPE_SECRET_KEY", "lob": "LOB_API_KEY"}
KEY_SHAPE_NOTE = "key shape only; not authentication"


class SmokeLobClient(LobClient, Protocol):
    def verify_address(self, address: MailingAddress) -> LobVerification: ...


def valid_stripe_test_key(value: str) -> bool:
    """A header-safe Stripe test token; live, publishable, unknown and unsafe shapes fail."""
    if not valid_header_credential(value):
        return False
    return any(
        value.startswith(prefix) and len(value) > len(prefix) for prefix in STRIPE_TEST_PREFIXES
    )


def valid_lob_test_key(value: str) -> bool:
    """A header-safe Lob secret test token; publishable, live and unsafe shapes fail."""
    if not valid_header_credential(value) or value.startswith(LOB_PUBLISHABLE_PREFIX):
        return False
    return value.startswith(LOB_TEST_PREFIX) and len(value) > len(LOB_TEST_PREFIX)


KEY_VALIDATORS: dict[str, Callable[[str], bool]] = {
    "stripe": valid_stripe_test_key,
    "lob": valid_lob_test_key,
}


def load_environment(path: Path, environment: Mapping[str, str]) -> dict[str, str]:
    """Read simple KEY=VALUE lines, quotes and comments; never expand/interpolate."""
    values: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            name, separator, raw = line.partition("=")
            name = name.strip()
            if not separator or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None:
                continue
            try:
                parts = shlex.split(raw, comments=True, posix=True)
            except ValueError:
                continue
            if len(parts) <= 1:
                values[name] = parts[0] if parts else ""
    values.update(environment)
    return values


def smoke_letter(token: str, options: LetterOptions = FIRST_CLASS) -> LetterRequest:
    """Smallest valid one-page US-letter PDF; no customer data or clock."""
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    output = io.BytesIO()
    writer.write(output)
    request = LetterRequest(output.getvalue(), token, TEST_ADDRESS, TEST_ADDRESS, options)
    validate_request(request)
    return request


def _report(
    step: str, passed: bool, object_id: str | None = None, *, note: str | None = None
) -> bool:
    # IDs are provider data too: reject free text, newlines, credentials and addresses.
    safe_id = (
        object_id if object_id and re.fullmatch(r"(?:pi_|ltr_)[A-Za-z0-9]+", object_id) else None
    )
    line = f"{'PASS' if passed else 'FAIL'} {step}" + (f" {safe_id}" if safe_id else "")
    print(line + (f" ({note})" if note else ""))
    return passed


def _stripe_check(client: PaymentClient, token: str) -> bool:
    amount = CERTIFIED_CENTS
    if amount not in prices.ALLOWED_PRICE_CENTS:
        return _report("stripe.authorize", False)
    try:
        held = client.authorize(amount, "pm_card_visa", f"{token}-authorize", {"smoke": token})
    except Exception:
        return _report("stripe.authorize", False)
    authorized = _report(
        "stripe.authorize",
        held.status == "requires_capture" and held.amount_cents == amount,
        held.authorization_ref,
    )
    if not held.authorization_ref:
        return False
    retrieved = False
    try:
        result = client.retrieve(held.authorization_ref)
        retrieved = (
            result.status == "requires_capture"
            and result.authorization_ref == held.authorization_ref
            and result.amount_cents == amount
        )
    except Exception:
        pass
    _report("stripe.retrieve", retrieved, held.authorization_ref)
    released = False
    try:
        result = client.release(held.authorization_ref, f"{token}-release")
        released = (
            result.status == "canceled"
            and result.authorization_ref == held.authorization_ref
            and result.amount_cents == amount
        )
    except Exception:
        pass
    _report("stripe.release", released, held.authorization_ref)
    # The money step: a second hold captured in full, as after Lob accepts a letter.
    captured = False
    capture_ref = None
    try:
        second = client.authorize(amount, "pm_card_visa", f"{token}-authorize-2", {"smoke": token})
        capture_ref = second.authorization_ref
        if second.status == "requires_capture" and second.amount_cents == amount and capture_ref:
            result = client.capture(
                capture_ref, second.amount_cents, f"{token}-capture", final_capture=True
            )
            captured = (
                result.status == "succeeded"
                and result.authorization_ref == capture_ref
                and result.amount_cents == second.amount_cents
                and result.amount_received == second.amount_cents
            )
    except Exception:
        pass
    _report("stripe.capture", captured, capture_ref)
    return authorized and retrieved and released and captured


def _await_letter(
    client: SmokeLobClient,
    submission_token: str,
    expected_id: str,
    deadline: float,
    *,
    require_tracking: bool,
) -> tuple[bool, str | None]:
    """Poll one already-created original until it is ready; never create or adopt another.

    Returns (ready, mismatched_id). An absent, not-yet-rendered or temporary lookup
    failure is pending, never success. Any different nonempty original id fails at once.
    """
    while True:
        # No lookup may start once the shared budget is spent, including a later stage.
        if _monotonic() >= deadline:
            return False, None
        found: str | MetadataLookup | None = None
        try:
            found = client.find_by_metadata(submission_token)
        except Exception:
            found = None
        # A response that consumed or exceeded the remaining budget cannot pass.
        if _monotonic() >= deadline:
            return False, None
        if isinstance(found, MetadataLookup):
            found_id = found.letter_id
            matched_tracking = bool(found.tracking_number)
        else:
            found_id = found
            matched_tracking = False
        if isinstance(found_id, str) and found_id:
            if found_id != expected_id:
                return False, found_id
            if not require_tracking or matched_tracking:
                return True, None
        remaining = deadline - _monotonic()
        if remaining <= 0:
            return False, None
        _sleep(min(READINESS_POLL_SECONDS, remaining))


def _lob_check(client: SmokeLobClient, token: str) -> bool:
    verified = False
    with contextlib.suppress(Exception):
        verified = client.verify_address(TEST_VERIFICATION_ADDRESS).status in {
            LobVerificationStatus.DELIVERABLE,
            LobVerificationStatus.DELIVERABLE_WITH_CORRECTIONS,
        }
    _report("lob.verify_address", verified)
    deadline = _monotonic() + READINESS_BUDGET_SECONDS
    letter_id = None
    try:
        result = client.create_letter(smoke_letter(token), f"{token}-letter")
        if result.status is not None and 200 <= result.status < 300:
            letter_id = result.letter_id
    except Exception:
        pass
    created = _report("lob.create_letter", bool(letter_id), letter_id)
    found_ok = False
    if letter_id:
        ready, mismatched = _await_letter(
            client, token, letter_id, deadline, require_tracking=False
        )
        if ready:
            found_ok = _report("lob.find_by_metadata", True, letter_id)
        elif mismatched is not None:
            found_ok = _report("lob.find_by_metadata", False, mismatched)
        else:
            found_ok = _report("lob.find_by_metadata", False)
    else:
        _report("lob.find_by_metadata", False)
    # Launch mail: certified with USPS tracking, through the same adapter path.
    certified_id = None
    try:
        result = client.create_letter(
            smoke_letter(f"{token}-certified", LetterOptions(MailClass.CERTIFIED)),
            f"{token}-certified-letter",
        )
        if result.status is not None and 200 <= result.status < 300:
            certified_id = result.letter_id
    except Exception:
        pass
    certified = _report("lob.create_certified", bool(certified_id), certified_id)
    tracked = False
    if certified_id:
        ready, _mismatched = _await_letter(
            client, f"{token}-certified", certified_id, deadline, require_tracking=True
        )
        tracked = _report("lob.certified_tracking", ready)
    else:
        _report("lob.certified_tracking", False)
    return verified and created and found_ok and certified and tracked


def _stripe_client(key: str) -> PaymentClient:
    return StripePaymentClient(secret_key=key, api_version=STRIPE_API_VERSION)


def _lob_client(key: str) -> SmokeLobClient:
    return LobSdkClient(key, LOB_API_VERSION, timeout=20)


def _selected_providers(stripe_only: bool, lob_only: bool) -> tuple[str, ...]:
    if stripe_only:
        return ("stripe",)
    if lob_only:
        return ("lob",)
    return ("stripe", "lob")


def _read_explicit_environment(path: Path, environment: Mapping[str, str]) -> dict[str, str]:
    """Read a named credential file; a missing/non-file/unreadable path is a failure."""
    if not path.is_file():
        raise OSError("explicit credential file is missing or not a regular file")
    return load_environment(path, environment)


def main(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    env_path: Path | None = None,
    stripe_factory: Callable[[str], PaymentClient] = _stripe_client,
    lob_factory: Callable[[str], SmokeLobClient] = _lob_client,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    providers = parser.add_mutually_exclusive_group()
    providers.add_argument("--stripe-only", action="store_true")
    providers.add_argument("--lob-only", action="store_true")
    parser.add_argument("--env-file", type=Path, default=None, metavar="PATH")
    parser.add_argument("--check-config", action="store_true")
    args = parser.parse_args(argv)
    source = os.environ if environment is None else environment
    try:
        if args.env_file is not None:
            values = _read_explicit_environment(Path(args.env_file), source)
        else:
            # An injected env_path is the test seam for the default local .env.
            values = load_environment(
                env_path if env_path is not None else Path(__file__).resolve().parents[3] / ".env",
                source,
            )
    except (OSError, UnicodeError):
        _report("configuration", False)
        return 1
    selected = _selected_providers(args.stripe_only, args.lob_only)
    shapes = {
        provider: KEY_VALIDATORS[provider](values.get(KEY_ENVIRONMENT[provider], ""))
        for provider in selected
    }
    if args.check_config:
        passed = True
        for provider in selected:
            passed = (
                _report(f"{provider}.key_shape", shapes[provider], note=KEY_SHAPE_NOTE) and passed
            )
        return 0 if passed else 1
    if not all(shapes.values()):
        # Full runs admit both; provider-only runs admit just the selected key.
        for provider in selected:
            if not shapes[provider]:
                _report(KEY_ENVIRONMENT[provider], False)
        return 1
    token = f"pb-smoke-{uuid4().hex}"
    passed = True
    checks: list[tuple[str, Callable[[], bool]]] = []
    if "stripe" in selected:
        checks.append(
            ("stripe", lambda: _stripe_check(stripe_factory(values["STRIPE_SECRET_KEY"]), token))
        )
    if "lob" in selected:
        checks.append(("lob", lambda: _lob_check(lob_factory(values["LOB_API_KEY"]), token)))
    for name, check in checks:
        try:
            result = check()
        except Exception:
            result = _report(name, False)
        passed = result and passed
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
