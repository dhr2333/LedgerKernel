from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from beancount import loader

from ledger_kernel.errors import KernelError
from ledger_kernel.kernel import Kernel

from .helpers import TARGET, make_kernel, make_ledger, payload


class DryRunSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.kernel = make_kernel(self.base)

    def tearDown(self) -> None:
        self.kernel.close()
        self.tmp.cleanup()

    def test_renderer_defect_adding_an_entry_is_delta_mismatch(self) -> None:
        def defect(name, data):
            if name == "render_fragment":
                return data["fragment"] + '2026-01-03 open Assets:Injected CNY\n'
            return None

        kernel = Kernel.open(self.base, fault_hook=defect)
        draft_id = kernel.submit_draft(payload(), "inject", agent_id="a")["draft"]["draft_id"]
        validation = kernel.validate_draft(draft_id)
        self.assertEqual(validation["status"], "blocked")
        self.assertIn("DELTA_MISMATCH", {check["code"] for check in validation["checks"]})

    def test_tampered_fragment_is_rejected_at_commit(self) -> None:
        draft_id = self.kernel.submit_draft(payload(), "tamper", agent_id="a")["draft"]["draft_id"]
        validation = self.kernel.validate_draft(draft_id)
        self.kernel.approve_draft(draft_id)
        path = self.base / ".ledger-kernel" / "validations" / f"{validation['validation_id']}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["fragment"] = data["fragment"].replace("25.00", "250.00")
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(KernelError) as caught:
            self.kernel.commit_draft(draft_id)
        self.assertEqual(caught.exception.code, "FRAGMENT_MISMATCH")
        self.assertNotIn("250.00", (self.base / TARGET).read_text(encoding="utf-8"))

    def test_quotes_and_backslashes_survive_commit(self) -> None:
        body = payload(payee='A "Book" \\ Shop')
        draft_id = self.kernel.submit_draft(body, "quotes", agent_id="a")["draft"]["draft_id"]
        self.kernel.validate_draft(draft_id)
        self.kernel.approve_draft(draft_id)
        self.kernel.commit_draft(draft_id)
        entries, errors, _ = loader.load_file(str(self.base / "main.bean"))
        self.assertEqual(errors, [])
        payees = [entry.payee for entry in entries if getattr(entry, "payee", None)]
        self.assertIn('A "Book" \\ Shop', payees)

    def test_missing_evidence_is_a_warning(self) -> None:
        body = payload()
        body["evidence"] = [{"kind": "file", "locator": "documents/missing.pdf", "sha256": "0" * 64}]
        draft_id = self.kernel.submit_draft(body, "evidence", agent_id="a")["draft"]["draft_id"]
        validation = self.kernel.validate_draft(draft_id)
        self.assertEqual(validation["status"], "pass_with_warnings")
        self.assertIn("EVIDENCE_MISSING", {check["code"] for check in validation["checks"]})

    def test_evidence_hash_mismatch_is_a_warning(self) -> None:
        (self.base / "documents").mkdir()
        (self.base / "documents" / "receipt.txt").write_text("receipt", encoding="utf-8")
        body = payload()
        body["evidence"] = [{"kind": "file", "locator": "documents/receipt.txt", "sha256": "0" * 64}]
        draft_id = self.kernel.submit_draft(body, "hash", agent_id="a")["draft"]["draft_id"]
        validation = self.kernel.validate_draft(draft_id)
        self.assertIn("EVIDENCE_HASH_MISMATCH", {check["code"] for check in validation["checks"]})

    def test_balance_policy_is_only_a_warning(self) -> None:
        (self.base / ".ledger-kernel" / "contracts.yaml").write_text(
            "schema_version: 1\n"
            "accounts:\n"
            "  Assets:Cash:\n"
            "    policy:\n"
            "      balance_policy:\n"
            "        mode: warn_only\n"
            "        min: 900\n",
            encoding="utf-8",
        )
        draft_id = self.kernel.submit_draft(payload(amount="200.00"), "balance", agent_id="a")["draft"]["draft_id"]
        validation = self.kernel.validate_draft(draft_id)
        self.assertEqual(validation["status"], "pass_with_warnings")
        self.assertIn("CONTRACT_BALANCE", {check["code"] for check in validation["checks"]})

    def test_dry_run_does_not_modify_ledger(self) -> None:
        before = (self.base / TARGET).read_bytes()
        draft_id = self.kernel.submit_draft(payload(), "no-touch", agent_id="a")["draft"]["draft_id"]
        self.kernel.validate_draft(draft_id)
        self.assertEqual((self.base / TARGET).read_bytes(), before)
        self.assertEqual(len(list((self.base / ".ledger-kernel" / "snapshots" / "blobs").iterdir())), 4)


if __name__ == "__main__":
    unittest.main()
