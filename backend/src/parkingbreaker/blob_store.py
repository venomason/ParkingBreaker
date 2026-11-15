"""Customer photos (ticket and evidence), stored by content hash per owner.

One interface, a local-directory implementation for development and a single host, and
an in-memory fake for tests. A cloud bucket implementation sits behind the same
interface at deploy time. Bytes are validated as readable images, and location and other
free-form metadata are removed (photo_privacy), before they are kept; the ref names the
stripped bytes.
"""

from __future__ import annotations

import hashlib
import re
import tempfile
from pathlib import Path
from typing import Protocol

from parkingbreaker.appeal.packet import check_image
from parkingbreaker.photo_privacy import strip_location

MAX_PHOTO_BYTES = 10_000_000
_REF = re.compile(r"photo_[a-f0-9]{64}\Z")


class PhotoRejected(ValueError):
    """Stable reason key only."""


def photo_ref(data: bytes) -> str:
    return f"photo_{hashlib.sha256(data).hexdigest()}"


def validate_photo(data: bytes) -> bytes:
    """The photo as it may be kept: readable, within limits, without location data."""
    if not data or len(data) > MAX_PHOTO_BYTES:
        raise PhotoRejected("photo_size_invalid")
    try:
        check_image(data, "the photo")
        kept = strip_location(data)
        check_image(kept, "the photo")
    except Exception:  # any decoder fault on customer bytes is a refusal, not a 500
        raise PhotoRejected("photo_not_readable") from None
    if len(kept) > MAX_PHOTO_BYTES:
        raise PhotoRejected("photo_size_invalid")
    return kept


def _owner_dir(owner: str) -> str:
    # Owner ids never appear in paths; a hash keeps owners apart.
    return hashlib.sha256(owner.encode()).hexdigest()


class BlobStore(Protocol):
    def put(self, owner: str, data: bytes) -> str: ...

    def get(self, owner: str, ref: str) -> bytes | None: ...


class LocalBlobStore:
    def __init__(self, root: Path) -> None:
        self._root = root

    def put(self, owner: str, data: bytes) -> str:
        data = validate_photo(data)
        ref = photo_ref(data)
        folder = self._root / _owner_dir(owner)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / ref
        if target.is_file() and self.get(owner, ref) == data:
            return ref
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=folder, prefix=f".{ref}.", suffix=".tmp", delete=False
            ) as temp:
                temp_path = Path(temp.name)
                temp.write(data)
            temp_path.replace(target)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
        return ref

    def get(self, owner: str, ref: str) -> bytes | None:
        if not _REF.fullmatch(ref):
            return None
        path = self._root / _owner_dir(owner) / ref
        if not path.is_file():
            return None
        try:
            with path.open("rb") as stored:
                data = stored.read(MAX_PHOTO_BYTES + 1)
        except FileNotFoundError:
            return None
        if len(data) > MAX_PHOTO_BYTES or photo_ref(data) != ref:
            return None
        return data


class FakeBlobStore:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], bytes] = {}

    def put(self, owner: str, data: bytes) -> str:
        data = validate_photo(data)
        ref = photo_ref(data)
        self.items[(owner, ref)] = data
        return ref

    def get(self, owner: str, ref: str) -> bytes | None:
        return self.items.get((owner, ref))
