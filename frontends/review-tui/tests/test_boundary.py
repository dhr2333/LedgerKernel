from __future__ import annotations

import re
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
SRC = PROJECT / "src" / "ledger_review_tui"

KERNEL_IMPORT = re.compile(
    r"^\s*(?:import\s+ledger_kernel(?:\.|\s|$)|from\s+ledger_kernel(?:\.|\s|$))",
    re.MULTILINE,
)


class BoundaryTests(unittest.TestCase):
    def test_package_never_imports_the_kernel(self) -> None:
        offenders = [
            path.name
            for path in sorted(SRC.glob("*.py"))
            if KERNEL_IMPORT.search(path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(offenders, [])

    def test_model_does_not_depend_on_curses(self) -> None:
        text = (SRC / "model.py").read_text(encoding="utf-8")
        self.assertNotIn("import curses", text)
        self.assertNotIn("from curses", text)

    def test_pyproject_declares_standalone_install_unit(self) -> None:
        text = (PROJECT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('name = "ledger-kernel-review-tui"', text)
        self.assertIn('version = "0.1.0"', text)
        self.assertIn('requires-python = ">=3.11"', text)
        self.assertIn("dependencies = []", text)
        self.assertIn(
            'ledger-review-tui = "ledger_review_tui.__main__:main"', text
        )
        self.assertIn('where = ["src"]', text)

    def test_package_has_no_network_or_server_imports(self) -> None:
        forbidden = ("http.server", "socket", "asyncio", "ssl")
        for path in sorted(SRC.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            for module in forbidden:
                self.assertNotIn(
                    f"import {module}", text, f"{path.name} imports {module}"
                )


if __name__ == "__main__":
    unittest.main()
