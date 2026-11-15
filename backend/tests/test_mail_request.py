from __future__ import annotations

import json
from dataclasses import replace

import pytest

from mail_samples import make_request
from parkingbreaker.lob_submission import LetterOptions, LetterRequest
from parkingbreaker.mail_config import MailClass
from parkingbreaker.mail_request import (
    decode_request,
    envelope_json,
    request_fingerprint,
    validate_request,
)

REQUEST = make_request()


def test_complete_request_round_trips_without_defaults_or_lost_fields() -> None:
    encoded = envelope_json(REQUEST)
    assert decode_request(REQUEST.pdf, REQUEST.submission_token, encoded) == REQUEST
    assert encoded == envelope_json(REQUEST)
    assert json.loads(encoded)["recipient"]["attention"] == "Appeal Review"
    assert json.loads(encoded)["recipient"]["line2"] == "Suite 2"


@pytest.mark.parametrize(
    "changed",
    [
        replace(REQUEST, pdf=b"changed"),
        replace(REQUEST, submission_token="other-token"),
        replace(REQUEST, sender=replace(REQUEST.sender, recipient="Other Sender")),
        replace(REQUEST, recipient=replace(REQUEST.recipient, attention="Different Review")),
        replace(REQUEST, recipient=replace(REQUEST.recipient, zip_code="94612-1234")),
        replace(REQUEST, options=LetterOptions(MailClass.FIRST_CLASS)),
        replace(REQUEST, options=replace(REQUEST.options, color=True)),
        replace(REQUEST, options=replace(REQUEST.options, double_sided=True)),
        replace(REQUEST, options=replace(REQUEST.options, return_receipt=True)),
    ],
)
def test_fingerprint_covers_all_material_request_fields(changed: LetterRequest) -> None:
    assert request_fingerprint(changed) != request_fingerprint(REQUEST)


@pytest.mark.parametrize(
    "invalid",
    [
        replace(REQUEST, pdf=b""),
        replace(REQUEST, submission_token=" "),
        replace(REQUEST, submission_token="x" * 501),
        replace(REQUEST, submission_token='token"broken'),
        replace(REQUEST, submission_token="token\\broken"),
        replace(REQUEST, submission_token="token\nbroken"),
        replace(REQUEST, recipient=replace(REQUEST.recipient, line1=" ")),
        replace(REQUEST, recipient=replace(REQUEST.recipient, state="California")),
        replace(REQUEST, recipient=replace(REQUEST.recipient, zip_code="9461")),
        replace(REQUEST, sender=replace(REQUEST.sender, country="CA")),
        replace(REQUEST, sender=replace(REQUEST.sender, attention="")),
        replace(REQUEST, sender=replace(REQUEST.sender, recipient="Name\nInjection")),
        replace(REQUEST, options=replace(REQUEST.options, color=1)),  # type: ignore[arg-type]
        replace(REQUEST, options=replace(REQUEST.options, mail_class="certified")),  # type: ignore[arg-type]
        replace(REQUEST, options=LetterOptions(MailClass.FIRST_CLASS, return_receipt=True)),
    ],
)
def test_invalid_frozen_request_is_rejected(invalid: LetterRequest) -> None:
    with pytest.raises(ValueError, match=r".+"):
        validate_request(invalid)


@pytest.mark.parametrize(
    "text",
    [
        "null",
        "{}",
        "malformed",
        envelope_json(REQUEST).replace('"mail_class":"certified"', '"mail_class":"overnight"'),
        envelope_json(REQUEST).replace('"color":false', '"color":1'),
        envelope_json(REQUEST).replace('"line2":"Suite 2",', ""),
        envelope_json(REQUEST).replace('"sender":{', '"sender":{"unrecognized":"field",'),
        envelope_json(REQUEST).replace('"options":{', '"options":{"unrecognized":true,'),
        envelope_json(REQUEST).replace('"color":false', '"color":true,"color":false'),
        envelope_json(REQUEST).replace('"color":false', '"color":NaN'),
    ],
)
def test_decoder_rejects_partial_unknown_or_malformed_envelopes(text: str) -> None:
    with pytest.raises(ValueError, match=r".+"):
        decode_request(REQUEST.pdf, REQUEST.submission_token, text)
