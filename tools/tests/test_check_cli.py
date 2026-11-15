"""Check execution must work without the archived operational task ledger."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()
