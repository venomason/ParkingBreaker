"""Photo storage: validated images, content refs, owners kept apart."""

from __future__ import annotations

import io
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from PIL import Image

from parkingbreaker.blob_store import (
    MAX_PHOTO_BYTES,
    FakeBlobStore,
    LocalBlobStore,
    PhotoRejected,
    photo_ref,
)


def png(color: str = "white") -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (4, 4), color).save(out, "PNG")
    return out.getvalue()


@pytest.mark.parametrize("make", [lambda p: LocalBlobStore(p), lambda _p: FakeBlobStore()])
def test_put_get_and_owner_isolation(tmp_path: Path, make: object) -> None:
    store = make(tmp_path)  # type: ignore[operator]
    ref = store.put("alice", png())
    assert ref == photo_ref(png())
    assert store.get("alice", ref) == png()
    assert store.get("bob", ref) is None
    assert store.put("alice", png()) == ref


@pytest.mark.parametrize(
    ("data", "reason"),
    [(b"", "photo_size_invalid"), (b"not an image", "photo_not_readable")],
)
def test_rejects_unreadable_or_empty(tmp_path: Path, data: bytes, reason: str) -> None:
    with pytest.raises(PhotoRejected, match=reason):
        LocalBlobStore(tmp_path).put("alice", data)


def test_rejects_oversize(tmp_path: Path) -> None:
    with pytest.raises(PhotoRejected, match="photo_size_invalid"):
        LocalBlobStore(tmp_path).put("alice", b"x" * (MAX_PHOTO_BYTES + 1))


def test_refuses_path_like_refs(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path)
    assert store.get("alice", "../../etc/passwd") is None


def test_concurrent_identical_puts_use_independent_atomic_temps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalBlobStore(tmp_path)
    payload = png()
    barrier = threading.Barrier(2)
    original_replace = Path.replace

    def synchronized_replace(path: Path, target: str | Path) -> Path:
        if path.name.endswith(".tmp"):
            barrier.wait(timeout=5)
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", synchronized_replace)
    with ThreadPoolExecutor(max_workers=2) as pool:
        refs = list(pool.map(lambda _: store.put("alice", payload), range(2)))
    assert refs[0] == refs[1] == photo_ref(payload)
    assert store.get("alice", refs[0]) == payload


def test_local_read_refuses_tampered_content(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path)
    original = png()
    ref = store.put("alice", original)
    owner_folder = next(tmp_path.iterdir())
    (owner_folder / ref).write_bytes(png("black"))
    assert store.get("alice", ref) is None
