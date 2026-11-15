from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ledger_kernel.adapter import BeancountAdapter
from ledger_kernel.errors import KernelError

from .helpers import make_ledger


class AdapterTests(unittest.TestCase):
    def test_load_plain_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_ledger(Path(tmp))
            load = BeancountAdapter(Path(tmp)).load(root)
            self.assertTrue(load.ok)
            self.assertGreaterEqual(len(load.entries), 2)
            self.assertIn("Assets:Cash", load.accounts)
            self.assertEqual(load.closure_source, "options.include")
            self.assertEqual(set(load.manifest), {"main.bean", "accounts.bean", "transactions/2026.bean", "transactions/kernel-import.bean"})

    def test_include_files_are_in_revision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_ledger(Path(tmp))
            adapter = BeancountAdapter(Path(tmp))
            before = adapter.load(root).revision
            included = Path(tmp) / "accounts.bean"
            included.write_text(included.read_text() + "\n2026-02-01 open Expenses:Test CNY\n", encoding="utf-8")
            after = adapter.load(root).revision
            self.assertNotEqual(before, after)

    def test_closure_outside_ledger_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            base = Path(tmp)
            (base / "accounts.bean").write_text("2020-01-01 open Assets:Cash CNY\n", encoding="utf-8")
            root = base / "main.bean"
            external = Path(outside) / "external.bean"
            external.write_text("2020-01-01 open Assets:External CNY\n", encoding="utf-8")
            root.write_text(f'include "{external}"\ninclude "accounts.bean"\n', encoding="utf-8")
            with self.assertRaises(KernelError) as caught:
                BeancountAdapter(base).load(root)
            self.assertEqual(caught.exception.code, "CLOSURE_INCONSISTENT")

    def test_beancount_errors_are_reported_with_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_ledger(Path(tmp), invalid=True)
            load = BeancountAdapter(Path(tmp)).load(root)
            self.assertFalse(load.ok)
            records = load.error_records()
            self.assertTrue(records)
            self.assertIsNotNone(records[0]["source_file"])
            self.assertIsNotNone(records[0]["lineno"])

    def test_options_signature_ignores_temp_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_ledger(Path(tmp))
            load = BeancountAdapter(Path(tmp)).load(root)
            signature = load.options_signature()
            self.assertNotIn("filename", signature)
            self.assertNotIn("include", signature)


if __name__ == "__main__":
    unittest.main()
