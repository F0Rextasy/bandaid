"""Contract tests for bandaid. Run: python -m unittest discover -s tests -v

Every test drives the real CLI against a fixture patch and asserts the exit
code and reported findings a consumer would observe -- nothing internal.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "bandaid.py")
EXAMPLES = os.path.join(ROOT, "examples")


def run_cli(*args):
    proc = subprocess.run(
        [sys.executable, SCRIPT, *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return proc.returncode, proc.stdout, proc.stderr


def fixture(name):
    return ["--patch", os.path.join(EXAMPLES, name), "--no-color"]


class BandaidContract(unittest.TestCase):
    def test_symptom_patch_fails_with_named_rules(self):
        code, out, _ = run_cli(*fixture("bandaid.patch"))
        self.assertEqual(code, 1)
        for rule in ("except-pass", "guard-removed", "bare-except", "empty-catch"):
            self.assertIn(rule, out)
        self.assertIn("6 bandaids", out)

    def test_root_cause_fix_is_clean(self):
        code, out, _ = run_cli(*fixture("real-fix.patch"))
        self.assertEqual(code, 0, out)
        self.assertIn("clean", out)

    def test_justified_escape_passes_and_counts(self):
        code, out, _ = run_cli(*fixture("allowed.patch"))
        self.assertEqual(code, 0, out)
        self.assertIn("clean", out)
        self.assertIn("justified by 'bandaid: allow'", out)

    def test_json_reports_structure(self):
        code, out, _ = run_cli(*fixture("bandaid.patch"), "--format", "json")
        self.assertEqual(code, 1)
        data = json.loads(out)
        self.assertFalse(data["ok"])
        self.assertEqual(data["counts"]["bandaid"], 6)
        self.assertEqual(data["scanned"]["files"], ["src/checkout.py", "web/cart.js"])
        rules = {f["rule"] for f in data["findings"]}
        self.assertIn("guard-removed", rules)

    def test_suspect_passes_by_default_and_fails_strict(self):
        patch = (
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1 +1,2 @@\n"
            " def f():\n"
            "+    x = y  # type: ignore\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False) as fh:
            fh.write(patch)
            path = fh.name
        try:
            code, out, _ = run_cli("--patch", path, "--no-color")
            self.assertEqual(code, 0, out)
            self.assertIn("suspect", out)
            code, out, _ = run_cli("--patch", path, "--no-color", "--strict")
            self.assertEqual(code, 1, out)
        finally:
            os.unlink(path)

    def test_clean_scan_never_crashes(self):
        # regression: clean path raised NameError (bare `strict`) pre-1.0
        code, out, err = run_cli(*fixture("real-fix.patch"))
        self.assertEqual(code, 0, err)
        self.assertNotIn("NameError", err)


if __name__ == "__main__":
    unittest.main()
