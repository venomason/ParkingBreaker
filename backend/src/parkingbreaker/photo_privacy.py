"""Remove location and other free-form metadata from a customer photo at upload. Pure.

Runs before a photo is stored, so nothing downstream (the mailed packet, any AI reader)
ever receives GPS coordinates, XMP, IPTC, ICC profile text, comments, maker notes,
thumbnails or a second embedded image. Only two facts survive: the orientation, so the
photo prints upright, and a well-formed capture date, which the packet prints as a
clerical label. Only JPEG and PNG are accepted (the upload form allows only these), and
both are rewritten segment by segment, so their pixels are never re-encoded.
"""

from __future__ import annotations

import io
import re
import struct
import zlib

from PIL import Image

_ORIENTATION = 0x0112
_EXIF_IFD = 0x8769
_DATES = (36867, 36868)  # DateTimeOriginal, DateTimeDigitized
_EXIF_DATE = re.compile(r"\d{4}:\d{2}:\d{2} \d{2}:\d{2}:\d{2}")
# PNG chunks kept: image data and colour facts only, each at its specified size so no
# spare bytes can ride along. eXIf is rebuilt; text, time, ICC profiles and unknown
# private chunks are dropped.
_PNG_SIZES: dict[bytes, frozenset[int] | None] = {
    b"IHDR": frozenset({13}), b"IEND": frozenset({0}), b"cHRM": frozenset({32}),
    b"gAMA": frozenset({4}), b"sRGB": frozenset({1}), b"pHYs": frozenset({9}),
    b"sBIT": frozenset({1, 2, 3, 4}), b"bKGD": frozenset({1, 2, 6}),
    b"acTL": frozenset({8}), b"fcTL": frozenset({26}),
    b"PLTE": None, b"tRNS": None, b"IDAT": None, b"fdAT": None,  # pixel data
}  # fmt: skip
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Markers with no length field: SOI, EOI, RST0-7 and TEM.
_STANDALONE = {0xD8, 0xD9, 0x01, *range(0xD0, 0xD8)}


class PhotoMetadataError(ValueError):
    """The photo is not a JPEG or PNG, or its structure could not be rewritten safely."""


def _minimal_exif(source: Image.Exif) -> bytes | None:
    """TIFF-format EXIF holding only orientation and capture date, or None if neither."""
    kept = Image.Exif()
    value = source.get(_ORIENTATION)
    if isinstance(value, int) and 1 <= value <= 8:
        kept[_ORIENTATION] = value
    dates = source.get_ifd(_EXIF_IFD)
    for tag in _DATES:
        date = dates.get(tag)
        if isinstance(date, str) and _EXIF_DATE.fullmatch(date.strip("\x00 ")):
            kept.get_ifd(_EXIF_IFD)[tag] = date.strip("\x00 ")
    if not kept and not kept.get_ifd(_EXIF_IFD):
        return None
    data = kept.tobytes()
    return data[6:] if data.startswith(b"Exif\x00\x00") else data


def _segment(marker: int, body: bytes) -> bytes:
    return bytes((0xFF, marker)) + struct.pack(">H", len(body) + 2) + body


def _kept_jpeg_segment(marker: int, body: bytes) -> bytes | None:
    """The segment to write, rebuilt to its fixed fields where it has any; None drops it."""
    if marker == 0xE0 and body.startswith(b"JFIF\x00") and len(body) >= 12:
        # Version, units and density only; the thumbnail and any surplus bytes go.
        return _segment(marker, body[:12] + b"\x00\x00")
    if marker == 0xEE and body.startswith(b"Adobe") and len(body) >= 12:
        # Adobe's colour-transform record is exactly twelve bytes.
        return _segment(marker, body[:12])
    if 0xE0 <= marker <= 0xEF or marker == 0xFE:
        return None  # every other APPn (EXIF, XMP, ICC, IPTC, MPF) and comments
    return _segment(marker, body)  # tables and frame and scan headers


def _jpeg(data: bytes, exif: Image.Exif) -> bytes:
    if data[:2] != b"\xff\xd8":
        raise PhotoMetadataError("not a JPEG stream")
    out = bytearray(b"\xff\xd8")
    minimal = _minimal_exif(exif)
    at = 2
    while True:
        if at + 2 > len(data) or data[at] != 0xFF:
            raise PhotoMetadataError("malformed JPEG segment")
        marker = data[at + 1]
        if marker == 0xFF:  # fill byte before a marker
            at += 1
            continue
        if marker == 0xD9:
            # The primary image ends here; trailers and second images (MPO) are dropped.
            return bytes(out + b"\xff\xd9")
        if marker in _STANDALONE or at + 4 > len(data):
            raise PhotoMetadataError("malformed JPEG segment")
        length = struct.unpack(">H", data[at + 2 : at + 4])[0]
        end = at + 2 + length
        if length < 2 or end > len(data):
            raise PhotoMetadataError("malformed JPEG segment")
        body = data[at + 4 : end]
        if minimal is not None and not (marker == 0xE0 and body.startswith(b"JFIF\x00")):
            # The rebuilt EXIF goes right after SOI or JFIF, where readers look for it.
            exif_body = b"Exif\x00\x00" + minimal
            out += b"\xff\xe1" + struct.pack(">H", len(exif_body) + 2) + exif_body
            minimal = None
        kept = _kept_jpeg_segment(marker, body)
        if kept is not None:
            out += kept
        at = end
        if marker == 0xDA:
            # Entropy-coded data runs to the next real marker (not FF00 stuffing or RSTn);
            # progressive files carry more segments after it, all filtered the same way.
            scan = at
            while True:
                at = data.find(b"\xff", at)
                if at < 0 or at + 1 >= len(data):
                    raise PhotoMetadataError("JPEG has no end marker")
                following = data[at + 1]
                if following == 0x00 or 0xD0 <= following <= 0xD7:
                    at += 2
                    continue
                if following == 0xFF:
                    at += 1
                    continue
                break
            out += data[scan:at]


def _png(data: bytes, exif: Image.Exif) -> bytes:
    if not data.startswith(_PNG_SIGNATURE):
        raise PhotoMetadataError("not a PNG stream")
    out = bytearray(_PNG_SIGNATURE)
    minimal = _minimal_exif(exif)
    at = len(_PNG_SIGNATURE)
    while at + 12 <= len(data):
        length = struct.unpack(">I", data[at : at + 4])[0]
        kind = data[at + 4 : at + 8]
        end = at + 12 + length
        if end > len(data):
            raise PhotoMetadataError("malformed PNG chunk")
        if kind in _PNG_SIZES:
            sizes = _PNG_SIZES[kind]
            if sizes is not None and length not in sizes:
                raise PhotoMetadataError("malformed PNG chunk")
            if kind == b"IDAT" and minimal is not None:
                chunk = b"eXIf" + minimal
                out += struct.pack(">I", len(minimal)) + chunk
                out += struct.pack(">I", zlib.crc32(chunk) & 0xFFFFFFFF)
                minimal = None
            out += data[at:end]
        if kind == b"IEND":
            return bytes(out)
        at = end
    raise PhotoMetadataError("PNG has no end chunk")


def strip_location(data: bytes) -> bytes:
    """The same picture with only orientation and capture date left in its metadata."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            kind = image.format
            exif = image.getexif()
            if kind in {"JPEG", "MPO"}:
                return _jpeg(data, exif)
            if kind == "PNG":
                return _png(data, exif)
    except PhotoMetadataError:
        raise
    except Exception as exc:  # any decoder fault on customer bytes is a refusal, not a 500
        raise PhotoMetadataError("photo metadata could not be read") from exc
    raise PhotoMetadataError("only JPEG and PNG photos are accepted")
