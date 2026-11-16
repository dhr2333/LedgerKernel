"""Run the fixture end-to-end check the way CI and contributors run it."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "e2e_ledger.py"


class FixtureEndToEndTest(unittest.TestCase):
    def test_fixture_loop_passes(self) -> None:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT / "src")
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), "--json"],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        report = json.loads(completed.stdout)
        self.assertEqual(report["failed"], [])
        self.assertTrue(report["fixture"])
        self.assertEqual(report["init"]["status"], "Clean")
        self.assertTrue(report["init"]["target_in_closure"])
        self.assertGreaterEqual(report["init"]["closure_files"], 6)
        names = {item["check"] for item in report["checks"]}
        for expected in (
            "committed transaction appears once",
            "external change attributed unknown",
            "draft invalidated by external change",
            "commit blocked by invalid ledger",
        ):
            self.assertIn(expected, names)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
