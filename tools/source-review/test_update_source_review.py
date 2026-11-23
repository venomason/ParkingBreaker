import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest
import update_source_review as review


class ExportIntegrityTests(unittest.TestCase):
    def test_long_line_splits_into_four_equal_parts(self):
        data = b"a" * 5003
        parts = review.split_parts(data, 4)
        assert [len(part) for part in parts] == [1250, 1251, 1251, 1251]
        assert b"".join(parts) == data

    def test_unicode_parts_decode_individually(self):
        data = "Aé車🚗Z".encode() * 7
        parts = review.split_parts(data, 4)
        assert len(parts) == 4
        assert "".join(part.decode() for part in parts) == data.decode()

    def test_split_seed_recovery_and_corruption(self):
        body = b"fixture\n"
        row = "\t".join(
            [
                "sample",
                "verbatim",
                "7",
                "1",
                str(len(body)),
                hashlib.sha256(body).hexdigest(),
                "a" * 40,
                "-",
            ]
        )
        seed = (
            (
                "~~~tsv\npath\tstatus\tbody_line\tbody_lines\tbody_bytes\tbody_sha256\tgit_blob\tnotes\n"
                + row
                + "\n~~~\n\n~~~text\n"
            ).encode()
            + body
            + b"~~~\n"
        )
        with tempfile.TemporaryDirectory() as temp:
            dest = Path(temp) / "source.md"
            for i, part in enumerate(review.split_parts(seed, 4), 1):
                dest.with_name(f"source part {i}.md").write_bytes(part)
            prior = review.parse_export(dest)
            assert prior["data"] == seed
            review.extract_reuse(prior)
            prior["lines"][6] = b"broken!"
            with pytest.raises(SystemExit, match="digest or size"):
                review.extract_reuse(prior)
            dest.with_name("source part 2.md").unlink()
            with pytest.raises(SystemExit, match="incomplete"):
                review.parse_export(dest)

    def test_failed_publication_restores_previous_files(self):
        with tempfile.TemporaryDirectory() as temp:
            dest = Path(temp) / "source.md"
            part = dest.with_name("source part 1.md")
            dest.write_bytes(b"previous full")
            part.write_bytes(b"previous part")
            state = {"head": "x", "branch": "main", "dirty": ""}
            replace = os.replace

            def fail_second(source, target):
                if target == part:
                    raise OSError("simulated write failure")
                replace(source, target)

            with (
                patch.object(review, "git_state", return_value=state),
                patch.object(review.os, "replace", side_effect=fail_second),
                pytest.raises(OSError, match="simulated write failure"),
            ):
                review.publish(
                    dest, b"next", [b"ne", b"xt"], Path(temp) / "backups", True, temp, state
                )
            assert dest.read_bytes() == b"previous full"
            assert part.read_bytes() == b"previous part"
            assert not dest.with_name("source part 2.md").exists()
