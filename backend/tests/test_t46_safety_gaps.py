"""T46: the known safety gaps, each pinned by a test that fails on the old behavior."""

from __future__ import annotations

import io
import logging
import struct
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image, ImageOps, PngImagePlugin

from parkingbreaker.access_email import AccessEmailService
from parkingbreaker.appeal.packet import exif_photo_date
from parkingbreaker.blob_store import FakeBlobStore, LocalBlobStore, PhotoRejected, photo_ref
from parkingbreaker.email_client import EmailOutcome, EmailReason, FakeEmailClient
from parkingbreaker.lob_fakes import FakeLobClient
from parkingbreaker.lob_submission import LetterEvidence, LobResponse
from parkingbreaker.lob_webhook import verify_webhook as verify_lob_webhook
from parkingbreaker.stripe_payment import verify_webhook as verify_stripe_webhook
from parkingbreaker.tracking_email import TrackingEmailService
from parkingbreaker.transaction_store import SqliteTransactionStore
from test_case_service import anyio_backend as anyio_backend
from test_case_service import fake_resolution as fake_resolution
from test_effect_dispatch import WORKER, approved
from test_t20_backlog_bounds import (
    RETENTION,
    WARNING_LOGGER,
    later,
    park,
    warnings,
)
from test_t20_webhook_recovery import app_for

GPS_IFD = 34853
XMP_LOCATION = "<x:xmpmeta><exif:GPSLatitude>37.7749</exif:GPSLatitude></x:xmpmeta>"


def test_unmatched_webhook_warning_never_logs_an_unexpected_identifier(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        park(store, "evt_shaped1", "7ABC123 Pat Customer", "tok_a")
        park(store, "Citation 9988776655", "ltr_shaped2", "tok_b")
        # Prefix-shaped private text is not a Lob id either.
        park(store, "evt_7ABC123_Pat_Customer", "ltr_Citation_9988776655", "tok_c")
    real = app_for(path)
    caplog.set_level(logging.WARNING, logger=WARNING_LOGGER)
    try:
        later(real, RETENTION + timedelta(seconds=1))
        real._replay_webhooks()
        text = "\n".join(warnings(caplog))
        assert len(warnings(caplog)) == 3
        for private in ("7ABC123", "Pat", "9988776655", "Citation"):
            assert private not in text
        # Well-formed Lob ids stay readable so an operator can find the row.
        assert "evt_shaped1" in text
        assert "ltr_shaped2" in text
        assert text.count("sha256:") == 4
    finally:
        real._store.close()


def _photo_with_location(fmt: str) -> bytes:
    exif = Image.Exif()
    exif[GPS_IFD] = {1: "N", 2: (37.0, 46.0, 30.0), 3: "W", 4: (122.0, 25.0, 0.0)}
    exif[0x8769] = {36867: "2026:10:01 08:15:00"}
    exif[0x0112] = 6
    out = io.BytesIO()
    pixels = Image.new("RGB", (40, 30), "red")
    pixels.putpixel((0, 0), (0, 0, 255))
    # A colour profile is free-form too: its text can name a place.
    extra: dict[str, object] = {"exif": exif, "icc_profile": b"desc 37.7749 Market St"}
    if fmt == "PNG":
        info = PngImagePlugin.PngInfo()
        info.add_itxt("XML:com.adobe.xmp", XMP_LOCATION)
        extra["pnginfo"] = info
    pixels.save(
        out, "JPEG" if fmt == "PROGRESSIVE" else fmt, progressive=fmt == "PROGRESSIVE", **extra
    )
    data = out.getvalue()
    if fmt != "PNG":
        # XMP and comments (also between progressive scans) and a trailing second image,
        # as phones write them, all carry location.
        xmp = b"http://ns.adobe.com/xap/1.0/\x00" + XMP_LOCATION.encode()
        segment = b"\xff\xe1" + struct.pack(">H", len(xmp) + 2) + xmp
        segment += b"\xff\xfe" + struct.pack(">H", 2 + 7) + b"37.7749"
        # Allowlisted segments with room to spare: a JFIF thumbnail and a long Adobe record.
        jfif = b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x02\x01" + b"37.7749"
        adobe = b"Adobe\x00\x64\x00\x00\x00\x00\x01" + b"37.7749"
        segment += b"\xff\xe0" + struct.pack(">H", len(jfif) + 2) + jfif
        segment += b"\xff\xee" + struct.pack(">H", len(adobe) + 2) + adobe
        trailer = io.BytesIO()
        Image.new("RGB", (8, 8)).save(trailer, "JPEG", exif=exif)
        later_scan = data.rfind(b"\xff\xda")
        data = data[:later_scan] + segment + data[later_scan:] + trailer.getvalue()
        data = data[:2] + segment + data[2:]
    return data


@pytest.mark.parametrize("fmt", ["JPEG", "PROGRESSIVE", "PNG"])
@pytest.mark.parametrize("make", [lambda p: LocalBlobStore(p), lambda _p: FakeBlobStore()])
def test_stored_photos_carry_no_location_but_keep_pixels_and_capture_date(
    tmp_path: Path, fmt: str, make: object
) -> None:
    uploaded = _photo_with_location(fmt)
    assert GPS_IFD in Image.open(io.BytesIO(uploaded)).getexif()
    store = make(tmp_path)  # type: ignore[operator]
    ref = store.put("alice", uploaded)
    kept = store.get("alice", ref)
    assert kept is not None
    assert ref == photo_ref(kept)
    image = Image.open(io.BytesIO(kept))
    assert GPS_IFD not in image.getexif()
    for marker in (b"GPSLatitude", b"37.7749", b"xap/1.0", b"Market St", b"ICC_PROFILE"):
        assert marker not in kept
    assert kept.count(b"\xff\xd8") <= 1
    assert exif_photo_date(kept) == "2026-10-01 08:15:00"
    before = ImageOps.exif_transpose(Image.open(io.BytesIO(uploaded))).convert("RGB")
    after = ImageOps.exif_transpose(image).convert("RGB")
    assert after.tobytes() == before.tobytes()

    # Formats the upload form never offers are refused, not re-encoded.
    webp = io.BytesIO()
    Image.new("RGB", (4, 4)).save(webp, "WEBP", exif=Image.Exif())
    with pytest.raises(PhotoRejected, match="photo_not_readable"):
        store.put("alice", webp.getvalue())
    # A fixed-size PNG chunk stretched to carry text, or a broken animation, is refused.
    plain = io.BytesIO()
    Image.new("RGB", (4, 4)).save(plain, "PNG")
    body = b"pHYs" + b"\x00\x00\x0b\x13\x00\x00\x0b\x13\x01" + b"37.7749"
    padded = struct.pack(">I", len(body) - 4) + body + struct.pack(">I", zlib.crc32(body))
    frames = b"acTL" + struct.pack(">II", 1, 0)
    control = b"fcTL" + struct.pack(">IIIIIHHBB", 7, 4, 4, 0, 0, 1, 1, 0, 0)  # bad sequence
    broken = b"".join(
        struct.pack(">I", len(c) - 4) + c + struct.pack(">I", zlib.crc32(c))
        for c in (frames, control)
    )
    for extra in (padded, broken):
        with pytest.raises(PhotoRejected, match="photo_not_readable"):
            store.put("alice", plain.getvalue()[:33] + extra + plain.getvalue()[33:])


ACCEPTED = EmailOutcome("accepted", provider_id="0b8f8f9e-2a43-4c1e-9a55-3c1d2f6e7a10")


@pytest.mark.anyio
@pytest.mark.usefixtures("fake_resolution")
async def test_customer_gets_one_tracking_email_once_the_letter_is_mailed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lob = FakeLobClient(
        [
            LobResponse(
                200, "ltr_original", tracking_number="9400111", evidence=LetterEvidence.PRODUCTION
            )
        ]
    )
    api, real, case = await approved(tmp_path, monkeypatch, lob)
    store = real._store
    email = FakeEmailClient(
        [EmailOutcome("uncertain", reason=EmailReason.TRANSPORT_ERROR), ACCEPTED]
    )
    clock = lambda: real._deps.clock()  # noqa: E731 - follows the test's moving clock
    # The production path: the worker's access-email dispatch also sends tracking emails.
    delivery = AccessEmailService(
        store,
        email,
        key="A" * 43,
        origin="https://parkingbreaker.example",
        sender_identity="ParkingBreaker <mail@parkingbreaker.example>",
        clock=clock,
        tracking=TrackingEmailService(store, email, clock=clock),
    )
    try:
        assert delivery.dispatch() == 0  # nothing mailed yet: no email
        step = await api.post(
            f"/internal/cases/{case}/step", headers=WORKER, json={"step_id": "dispatch"}
        )
        assert step.json()["state"] == "IN_TRANSIT"
        assert delivery.dispatch() == 1  # uncertain: retried later, same email
        assert delivery.dispatch() == 0  # backoff not yet due
        later = real._deps.clock() + timedelta(minutes=5)
        object.__setattr__(real._deps, "clock", lambda: later)
        assert delivery.dispatch() == 1
        assert delivery.dispatch() == 0  # accepted: never sent again
        assert email.call_count == 2
        ((key, message),) = email._messages.items()
        assert key.startswith("pb-tracking-")
        assert message.subject == "Your ParkingBreaker letter was accepted for mailing"
        assert "certified mail" in message.text
        assert "9400111" in message.text
        token = real._intake(case).case_token
        assert token is not None
        assert token not in message.text
        assert case not in message.text
        row = (
            store._require_open()
            .execute("SELECT outcome, tries FROM pb_tracking_emails")
            .fetchone()
        )
        assert row == ("accepted", 2)
    finally:
        await api.aclose()


@pytest.mark.anyio
@pytest.mark.usefixtures("fake_resolution")
async def test_tracking_email_waits_for_a_number_and_never_resends_after_the_key_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lob = FakeLobClient([LobResponse(200, "ltr_original", evidence=LetterEvidence.PRODUCTION)])
    api, real, case = await approved(tmp_path, monkeypatch, lob)
    store = real._store
    email = FakeEmailClient()  # scripted with no answer: every send is uncertain
    start = real._deps.clock()

    def service_at(delta: timedelta) -> TrackingEmailService:
        return TrackingEmailService(store, email, clock=lambda: start + delta)

    try:
        step = await api.post(
            f"/internal/cases/{case}/step", headers=WORKER, json={"step_id": "dispatch"}
        )
        assert step.json()["state"] == "IN_TRANSIT"
        with store.submission_ownership():
            # No tracking number yet: nothing is sent for up to a week.
            assert service_at(timedelta(hours=1)).dispatch() == 0
            assert service_at(timedelta(days=6)).dispatch() == 0
            assert email.call_count == 0
            # A crash after the attempt is recorded and before the answer is saved.
            (due,) = store.due_tracking_emails(start + timedelta(days=7, hours=1))
            assert store.prepare_tracking_email(due, now=start + timedelta(days=7, hours=1))
            # The restart retries within the provider's key window, with the same key...
            assert service_at(timedelta(days=7, hours=2)).dispatch() == 1
            # ...but never once that window may have lapsed.
            assert service_at(timedelta(days=8, hours=2)).dispatch() == 0
        assert email.call_count == 1
        ((message),) = email._messages.values()
        assert "No USPS tracking number" in message.text
        row = (
            store._require_open()
            .execute("SELECT outcome, tries, in_flight FROM pb_tracking_emails")
            .fetchone()
        )
        assert row == ("exhausted", 2, 0)
    finally:
        await api.aclose()


@pytest.mark.parametrize("secret", ["", "   "])
def test_webhook_verification_refuses_a_blank_secret(secret: str) -> None:
    now = datetime(2026, 10, 31, tzinfo=UTC)
    with pytest.raises(ValueError, match="webhook_not_configured"):
        verify_lob_webhook(b"{}", "sig", str(int(now.timestamp())), secret, now)
    with pytest.raises(ValueError, match="webhook_not_configured"):
        verify_stripe_webhook(b"{}", "t=1,v1=sig", secret, now)
