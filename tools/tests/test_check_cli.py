"""Check execution must work without the archived operational task ledger."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("kit", ROOT / "build-kit/scripts/kit.py")
KIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(KIT)


class CheckCliTests(unittest.TestCase):
    def invoke(self, arguments):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = KIT.main(arguments)
        return code, json.loads(output.getvalue())

    def test_checks_preserve_exit_and_receipt_without_archival_state(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(KIT, "KIT", Path(directory)):
            receipt = Path(directory) / "receipt.json"
            for exit_code in (0, 1):
                with self.subTest(exit_code=exit_code):
                    receipt.write_text(json.dumps({"exit_code": exit_code}))
                    with patch.object(KIT, "run_check", return_value=receipt) as run:
                        code, result = self.invoke(["run-check", "web-types"])
                    self.assertEqual(code, exit_code)
                    self.assertEqual(result, {"receipt": "receipt.json", "exit_code": exit_code})
                    run.assert_called_once_with("web-types", None)

    def test_unknown_check_refuses_before_reading_state(self):
        with patch.object(KIT, "read", side_effect=AssertionError("archival state accessed")):
            code, result = self.invoke(["run-check", "unknown"])
        self.assertEqual(code, 2)
        self.assertEqual(result, {"error": "Choose a defined check ID"})

    def test_governance_commands_still_require_state(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(KIT, "KIT", Path(directory)):
            for arguments in (["status"], ["next"], ["accept", "receipt.json"]):
                with self.subTest(arguments=arguments):
                    code, result = self.invoke(arguments)
                    self.assertEqual(code, 2)
                    self.assertIn("tasks.json", result["error"])

    def test_doctor_checks_current_checkout_without_archival_state(self):
        with (
            patch.object(KIT, "verify", side_effect=AssertionError("archival state accessed")),
            patch.object(KIT.shutil, "which", return_value="/bin/command"),
        ):
            code, result = self.invoke(["doctor"])
        self.assertEqual(code, 0)
        self.assertTrue(result["workspace_ready"])
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["living_status"], "docs/ROADMAP.md")

    def test_doctor_rejects_invalid_guard_file(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(KIT, "KIT", Path(directory)):
            guard = Path(directory) / "state/guard-freeze.json"
            guard.parent.mkdir()
            guard.write_text("[]")
            code, result = self.invoke(["doctor"])
        self.assertEqual(code, 1)
        self.assertFalse(result["workspace_ready"])
        self.assertIn("Guard freeze must be a JSON object", result["errors"])

    def test_kit_check_runs_current_suite_and_recognizes_archived_command(self):
        discovery = [sys.executable, "-m", "unittest", "discover", "-s"]
        self.assertEqual(
            KIT.browser_check_command("kit-check"), [*discovery, "tools/tests", "-v"]
        )
        archived = [*discovery, "build-kit/tests", "-v"]
        self.assertEqual(
            KIT.expected_check_command({"check_id": "kit-check", "command": archived}), archived
        )


if __name__ == "__main__":
    unittest.main()
