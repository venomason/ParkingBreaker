"""Lob Python SDK adapter (v6 section 6.3).

The Lob SDK owns HTTP, authentication, multipart encoding and response decoding. Lob's
high-level LetterEditable.file accepts only a URL/string, so create uses the SDK's generic
ApiClient multipart boundary to upload the frozen bytes without converting them to a URL.
"""

from __future__ import annotations

import io
import json
import logging
import re
from datetime import datetime
from typing import cast
from urllib.parse import SplitResult, parse_qs, urlsplit

from lob_python.api.letters_api import LettersApi  # type: ignore[import-untyped]
from lob_python.api.us_verifications_api import UsVerificationsApi  # type: ignore[import-untyped]
from lob_python.api_client import ApiClient  # type: ignore[import-untyped]
from lob_python.configuration import Configuration  # type: ignore[import-untyped]
from lob_python.exceptions import ApiException  # type: ignore[import-untyped]
from lob_python.model.address_editable import AddressEditable  # type: ignore[import-untyped]
from lob_python.model.country_extended import CountryExtended  # type: ignore[import-untyped]
from lob_python.model.letter import Letter  # type: ignore[import-untyped]
from lob_python.model.letter_deletion import LetterDeletion  # type: ignore[import-untyped]
from lob_python.model.letter_editable import LetterEditable  # type: ignore[import-untyped]
from lob_python.model.letter_list import LetterList  # type: ignore[import-untyped]
from lob_python.model.ltr_use_type import LtrUseType  # type: ignore[import-untyped]
from lob_python.model.mail_type import MailType  # type: ignore[import-untyped]
from lob_python.model.metadata_model import MetadataModel  # type: ignore[import-untyped]
from lob_python.model.us_verification import UsVerification  # type: ignore[import-untyped]
from lob_python.model.us_verifications_writable import UsVerificationsWritable  # type: ignore[import-untyped]

from parkingbreaker.agencies import (
    LobVerification,
    LobVerificationStatus,
    MailingAddress,
)
from parkingbreaker.lob_submission import (
    CancelOutcome,
    LetterEvidence,
    LetterRequest,
    LobResponse,
    MetadataLookup,
)
from parkingbreaker.lob_webhook import has_usps_scan, scan_proof
from parkingbreaker.mail_config import MailClass
from parkingbreaker.mail_request import validate_request

_LETTER_ID = re.compile(r"ltr_[A-Za-z0-9]+\Z", re.ASCII)
_TRACKING_NUMBER = re.compile(r"[A-Za-z0-9]{10,40}\Z", re.ASCII)
_VERSION = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z", re.ASCII)
_PAGE_SIZE = 100
_MAX_METADATA_PAGES = 10
_DEFINITIVE_VALIDATION_CODES = frozenset(
    {
        "address_length_exceeds_limit",
        "bad_request",
        "failed_deliverability_strictness",
        "file_pages_below_min",
        "file_pages_exceed_max",
        "file_size_exceeds_limit",
        "foreign_return_address",
        "inconsistent_page_dimensions",
        "invalid",
        "invalid_file",
        "invalid_file_dimensions",
        "invalid_image_dpi",
        "pdf_encrypted",
        "special_characters_restricted",
        "unembedded_fonts",
    }
)


class MetadataLookupFailure(RuntimeError):
    """The metadata search was incomplete or returned conflicting letter IDs."""

    def __init__(self) -> None:
        super().__init__("Lob metadata lookup was incomplete or ambiguous")


class LobSdkClient:
    """One synchronous Lob SDK boundary with caller-supplied credentials and API version."""

    def __init__(
        self,
        api_key: str,
        api_version: str,
        timeout: float,
        *,
        api_client: ApiClient | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key:
            raise ValueError("api_key must be non-empty")
        if not isinstance(api_version, str) or not _VERSION.fullmatch(api_version):
            raise ValueError("api_version must use YYYY-MM-DD")
        if isinstance(timeout, bool) or not isinstance(timeout, int | float) or timeout <= 0:
            raise ValueError("timeout must be positive")

        if api_client is None:
            configuration = Configuration(username=api_key, password="")
            configuration.retries = 0
            api_client = ApiClient(configuration)
        else:
            # ApiClient is the SDK's supported REST injection point used by offline tests.
            api_client.configuration.username = api_key
            api_client.configuration.password = ""
            api_client.configuration.retries = 0
        # The SDK copies retries when building its pool manager. Update that copy
        # too; urllib3 keys pools by retry policy, including already-warmed clients.
        api_client.rest_client.pool_manager.connection_pool_kw["retries"] = 0
        # lob-python 5.2.1 logs raw response bodies at DEBUG, which may include addresses.
        logging.getLogger("lob_python.rest").setLevel(logging.WARNING)
        api_client.set_default_header("Lob-Version", api_version)
        self._api_client = api_client
        self._timeout = float(timeout)
        self._letters = LettersApi(api_client)
        self._verifications = UsVerificationsApi(api_client)

    def create_letter(self, request: LetterRequest, idempotency_key: str) -> LobResponse:
        """Submit exactly one frozen request; SDK and transport retries are disabled."""
        validate_request(request)
        if (
            not isinstance(idempotency_key, str)
            or not idempotency_key
            or len(idempotency_key) > 256
        ):
            raise ValueError("idempotency_key must contain 1 to 256 characters")

        try:
            # Everything up to the HTTP call is local: a failure here (for example the SDK
            # model's own limits) happened before any provider request, so it is a definite
            # refusal rather than an uncertain outcome.
            editable = LetterEditable(
                color=request.options.color,
                to=_address_model(request.recipient),
                _from=_address_model(request.sender),
                # The SDK model only accepts URL strings here. Its supported generic
                # multipart call below replaces this placeholder with the actual file part.
                file="frozen-letter.pdf",
                use_type=LtrUseType("operational"),
                metadata=MetadataModel(submission_token=request.submission_token),
                mail_type=MailType("usps_first_class"),
                double_sided=request.options.double_sided,
                address_placement="insert_blank_page",
                extra_service=_extra_service(
                    request.options.mail_class, request.options.return_receipt
                ),
            )
            fields = self._api_client.sanitize_for_serialization(editable)
            fields.pop("file", None)
            fields = {name: value for name, value in fields.items() if value is not None}
            pdf = io.BytesIO(request.pdf)
            pdf.name = "letter.pdf"
        except Exception:
            return LobResponse(None, error="local request validation rejected", local_refusal=True)
        try:
            response = self._api_client.call_api(
                "/letters",
                "POST",
                query_params=[],
                header_params={
                    "Content-Type": "multipart/form-data",
                    "Idempotency-Key": idempotency_key,
                    "Lob-Version": self._api_client.default_headers["Lob-Version"],
                },
                post_params=list(fields.items()),
                files={"file": [pdf]},
                response_type=(Letter,),
                auth_settings=["basicAuth"],
                _return_http_data_only=True,
                _request_timeout=self._timeout,
                _check_type=True,
            )
        except ApiException as exc:
            return _response_for_api_exception(exc)
        except Exception:
            return LobResponse(None, error="provider request uncertain")

        response_status = getattr(self._api_client.last_response, "status", 200)
        if not isinstance(response_status, int) or not 200 <= response_status < 300:
            return LobResponse(None, error="provider response uncertain")
        letter_id = getattr(response, "id", None)
        if not isinstance(letter_id, str) or _LETTER_ID.fullmatch(letter_id) is None:
            return LobResponse(None, error="provider response uncertain")

        # Creation/rendering anchors the original object but proves no mailing. Only
        # explicit USPS scan events on this same token-bound object permit PRODUCTION.
        if not _is_render_candidate(response, letter_id, request.submission_token):
            return LobResponse(None, error="provider response uncertain")
        status = _value(response, "status")
        current = response
        if status is None or status == "processed":
            current = self._get_letter(letter_id)
        elif status != "rendered":
            return LobResponse(None, error="provider response uncertain")
        rendered = _is_rendered_letter(current, letter_id, request.submission_token)
        tracking = _value(current, "tracking_number") if rendered else None
        return LobResponse(
            response_status,
            letter_id,
            tracking_number=(
                tracking
                if isinstance(tracking, str) and _TRACKING_NUMBER.fullmatch(tracking)
                else None
            ),
            evidence=(
                LetterEvidence.PRODUCTION
                if _is_render_candidate(current, letter_id, request.submission_token)
                and has_usps_scan(_value(current, "tracking_events"))
                else LetterEvidence.RENDERED
                if rendered
                else LetterEvidence.CREATED
            ),
            scans=scan_proof(_value(current, "tracking_events")),
        )

    def find_by_metadata(self, submission_token: str) -> MetadataLookup:
        """Search bounded pages and report only an exact, unambiguous token match."""
        if not isinstance(submission_token, str) or not submission_token.strip():
            raise ValueError("submission_token must be non-blank")

        matches: set[str] = set()
        cursor: str | None = None
        seen_cursors: set[str] = set()
        seen_ids: set[str] = set()
        try:
            for _ in range(_MAX_METADATA_PAGES):
                # The SDK's generated list method serializes MetadataModel as a
                # JSON query string. Lob's published contract requires deepObject.
                query: list[tuple[str, object]] = [
                    ("limit", _PAGE_SIZE),
                    ("metadata[submission_token]", submission_token),
                ]
                if cursor is not None:
                    query.append(("after", cursor))
                page = self._api_client.call_api(
                    "/letters",
                    "GET",
                    query_params=query,
                    header_params={"Accept": "application/json"},
                    response_type=(LetterList,),
                    auth_settings=["basicAuth"],
                    _return_http_data_only=True,
                    _request_timeout=self._timeout,
                    _check_type=True,
                )
                if not isinstance(page, LetterList) or not isinstance(page.data, list):
                    raise MetadataLookupFailure

                page_ids: set[str] = set()
                for item in page.data:
                    item_id = _value(item, "id")
                    metadata = _value(item, "metadata")
                    token = _value(metadata, "submission_token")
                    if (
                        not isinstance(item_id, str)
                        or _LETTER_ID.fullmatch(item_id) is None
                        or not isinstance(token, str)
                    ):
                        raise MetadataLookupFailure
                    if token == submission_token:
                        if not _is_render_candidate(item, item_id, submission_token):
                            raise MetadataLookupFailure
                        matches.add(item_id)
                    page_ids.add(item_id)

                if page.next_url is None:
                    if len(matches) > 1:
                        raise MetadataLookupFailure
                    if not matches:
                        return MetadataLookup(None, authoritative=False)
                    letter_id = next(iter(matches))
                    current = self._get_letter(letter_id)
                    if not _is_render_candidate(current, letter_id, submission_token):
                        raise MetadataLookupFailure
                    rendered = _is_rendered_letter(current, letter_id, submission_token)
                    scanned = has_usps_scan(_value(current, "tracking_events"))
                    if not rendered and not scanned:
                        raise MetadataLookupFailure
                    send_date = _value(current, "send_date")
                    tracking = _value(current, "tracking_number")
                    return MetadataLookup(
                        letter_id,
                        authoritative=False,
                        evidence=(
                            LetterEvidence.PRODUCTION
                            if scanned
                            else LetterEvidence.RENDERED
                            if rendered
                            else LetterEvidence.CREATED
                        ),
                        send_date=(
                            send_date if rendered and isinstance(send_date, datetime) else None
                        ),
                        tracking_number=(
                            tracking
                            if isinstance(tracking, str) and _TRACKING_NUMBER.fullmatch(tracking)
                            else None
                        ),
                        scans=scan_proof(_value(current, "tracking_events")),
                    )
                if not page.data or not page_ids.difference(seen_ids):
                    raise MetadataLookupFailure
                next_cursor = _next_cursor(page.next_url, self._api_client.configuration.host)
                if next_cursor in seen_cursors:
                    raise MetadataLookupFailure
                seen_cursors.add(next_cursor)
                seen_ids.update(page_ids)
                cursor = next_cursor
        except ApiException:
            raise MetadataLookupFailure from None
        except MetadataLookupFailure:
            raise
        except Exception:
            raise MetadataLookupFailure from None
        raise MetadataLookupFailure

    def lookup_letter(self, letter_id: str, submission_token: str) -> MetadataLookup:
        """Retrieve the one known original letter by id and classify what Lob shows.

        Unlike the metadata search this also reports a failed or deleted letter, as VOIDED.
        An unreachable, mismatched or contradictory object raises MetadataLookupFailure.
        """
        if not isinstance(letter_id, str) or _LETTER_ID.fullmatch(letter_id) is None:
            raise ValueError("letter_id must be a Lob letter id")
        current = self._get_letter(letter_id)
        if not _matches_letter_identity(current, letter_id, submission_token):
            raise MetadataLookupFailure
        assert current is not None
        scanned = has_usps_scan(_value(current, "tracking_events"))
        tracking = _value(current, "tracking_number")
        tracking_number = (
            tracking if isinstance(tracking, str) and _TRACKING_NUMBER.fullmatch(tracking) else None
        )
        if _is_render_candidate(current, letter_id, submission_token):
            send_date = _value(current, "send_date")
            rendered = _is_rendered_letter(current, letter_id, submission_token)
            return MetadataLookup(
                letter_id,
                authoritative=False,
                evidence=(
                    LetterEvidence.PRODUCTION
                    if scanned
                    else LetterEvidence.RENDERED
                    if rendered
                    else LetterEvidence.CREATED
                ),
                send_date=send_date if rendered and isinstance(send_date, datetime) else None,
                tracking_number=tracking_number,
                scans=scan_proof(_value(current, "tracking_events")),
            )
        if not scanned and _is_voided(current):
            return MetadataLookup(letter_id, authoritative=False, evidence=LetterEvidence.VOIDED)
        raise MetadataLookupFailure

    def cancel_letter(self, letter_id: str) -> CancelOutcome:
        """Request cancellation of exactly one letter (LettersApi.cancel, DELETE /letters/{id}).

        Only Lob's own confirmation that this id was deleted is CANCELED. A 422
        not_deletable is a refusal, never proof of production entry. The caller
        requires independent exact-letter evidence before capture (H3).
        """
        if not isinstance(letter_id, str) or _LETTER_ID.fullmatch(letter_id) is None:
            raise ValueError("letter_id must be a Lob letter id")
        try:
            result = self._letters.cancel(letter_id, _request_timeout=self._timeout)
        except ApiException as exc:
            if exc.status == 422 and _error_code(exc.body) == "not_deletable":
                return CancelOutcome.NOT_CANCELABLE
            return CancelOutcome.UNKNOWN
        except Exception:
            return CancelOutcome.UNKNOWN
        response_status = getattr(self._api_client.last_response, "status", None)
        if (
            isinstance(response_status, int)
            and 200 <= response_status < 300
            and isinstance(result, LetterDeletion)
            and _value(result, "id") == letter_id
            and _value(result, "deleted") is True
        ):
            return CancelOutcome.CANCELED
        return CancelOutcome.UNKNOWN

    def _get_letter(self, letter_id: str) -> object | None:
        """Fetch one existing object by ID; never create or recover by another key."""
        try:
            letter = cast(object, self._letters.get(letter_id, _request_timeout=self._timeout))
            response_status = getattr(self._api_client.last_response, "status", None)
            if not isinstance(response_status, int) or not 200 <= response_status < 300:
                return None
            return letter
        except Exception:
            # Provider exception text can contain customer data; callers receive only
            # an uncertain outcome and must not capture or release the hold.
            return None

    def verify_address(self, address: MailingAddress) -> LobVerification:
        """Assess deliverability without returning or adopting Lob's standardized address."""
        try:
            request = UsVerificationsWritable(
                recipient=address.recipient,
                primary_line=address.line1,
                secondary_line=address.line2,
                city=address.city,
                state=address.state,
                zip_code=address.zip_code,
            )
            result = self._verifications.verifySingle(
                request,
                _request_timeout=self._timeout,
            )
        except Exception:
            return LobVerification(
                LobVerificationStatus.UNVERIFIED, note="Lob verification unavailable"
            )
        if not isinstance(result, UsVerification):
            return LobVerification(
                LobVerificationStatus.UNVERIFIED, note="Lob returned an unknown verification"
            )

        status = {
            "deliverable": LobVerificationStatus.DELIVERABLE,
            "deliverable_unnecessary_unit": LobVerificationStatus.DELIVERABLE_WITH_CORRECTIONS,
            "undeliverable": LobVerificationStatus.UNDELIVERABLE,
        }.get(result.deliverability, LobVerificationStatus.UNVERIFIED)
        note = (
            None
            if status is not LobVerificationStatus.UNVERIFIED
            else "Lob did not establish deliverability"
        )
        return LobVerification(status, note=note)


def _address_model(address: MailingAddress) -> AddressEditable:
    return AddressEditable(
        name=address.recipient,
        company=address.attention,
        address_line1=address.line1,
        address_line2=address.line2,
        address_city=address.city,
        address_state=address.state,
        address_zip=address.zip_code,
        address_country=CountryExtended("US"),
    )


def _value(record: object, key: str) -> object:
    if isinstance(record, dict):
        return record.get(key)
    return getattr(record, key, None)


def _is_rendered_letter(letter: object | None, expected_id: str, token: str) -> bool:
    """Validate only explicit, current render evidence from the same provider object."""
    if letter is None or not _matches_letter_identity(letter, expected_id, token):
        return False
    return _value(letter, "status") == "rendered" and not _has_render_failure(letter)


def _matches_letter_identity(letter: object | None, expected_id: str, token: str) -> bool:
    if letter is None:
        return False
    letter_id = _value(letter, "id")
    metadata = _value(letter, "metadata")
    return (
        isinstance(letter_id, str)
        and _LETTER_ID.fullmatch(letter_id) is not None
        and letter_id == expected_id
        and _value(metadata, "submission_token") == token
    )


def _is_render_candidate(letter: object | None, expected_id: str, token: str) -> bool:
    if (
        letter is None
        or not _matches_letter_identity(letter, expected_id, token)
        or _has_render_failure(letter)
    ):
        return False
    return _value(letter, "status") in (None, "processed", "rendered")


def _is_voided(letter: object) -> bool:
    """Lob shows this letter deleted, canceled or failed, so it will not be mailed."""
    deleted = _value(letter, "deleted")
    if deleted is True:
        return True
    status = _value(letter, "status")
    return status in ("failed", "canceled") or _value(letter, "failure_reason") is not None


def _has_render_failure(letter: object) -> bool:
    deleted = _value(letter, "deleted")
    if deleted is not None and (type(deleted) is not bool or deleted):
        return True
    failure_reason = _value(letter, "failure_reason")
    return failure_reason is not None


def _next_cursor(next_url: str, configured_host: str) -> str:
    try:
        base = urlsplit(configured_host)
        next_page = urlsplit(next_url)
        if (
            base.scheme not in {"http", "https"}
            or next_page.scheme.lower() != base.scheme.lower()
            or next_page.hostname is None
            or base.hostname is None
            or next_page.hostname.lower() != base.hostname.lower()
            or _effective_port(next_page) != _effective_port(base)
            or next_page.username is not None
            or next_page.password is not None
            or next_page.fragment
            or next_page.path != f"{base.path.rstrip('/')}/letters"
        ):
            raise ValueError("invalid next page URL")
        query = parse_qs(next_page.query, keep_blank_values=True, strict_parsing=True)
        cursors = query.get("after", [])
        if len(cursors) != 1 or not cursors[0].strip():
            raise ValueError("invalid pagination cursor")
        return cursors[0]
    except (TypeError, UnicodeDecodeError, ValueError):
        raise MetadataLookupFailure from None


def _effective_port(parsed: SplitResult) -> int:
    port = parsed.port
    if port is not None:
        return port
    scheme = parsed.scheme.lower()
    if scheme == "https":
        return 443
    if scheme == "http":
        return 80
    raise ValueError("invalid URL scheme")


def _extra_service(mail_class: MailClass, return_receipt: bool) -> str | None:
    if mail_class is MailClass.FIRST_CLASS:
        return None
    if return_receipt:
        return "certified_return_receipt"
    return "certified"


def _response_for_api_exception(exc: ApiException) -> LobResponse:
    status = exc.status
    if status in {400, 413, 422} and _is_definitive_validation_error(exc.body):
        return LobResponse(status, error="provider validation rejected")
    return LobResponse(None, error="provider response uncertain")


def _is_definitive_validation_error(body: object) -> bool:
    code = _error_code(body)
    return code is not None and code in _DEFINITIVE_VALIDATION_CODES


def _error_code(body: object) -> str | None:
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    if not isinstance(body, str):
        return None
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    return code if isinstance(code, str) else None
