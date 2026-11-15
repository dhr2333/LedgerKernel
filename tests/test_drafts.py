from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ledger_kernel.errors import KernelError

from .helpers import TARGET, make_kernel, payload


class DraftFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.kernel = make_kernel(self.base)
        self.target = self.base / TARGET

    def tearDown(self) -> None:
        self.kernel.close()
        self.tmp.cleanup()

    def submit(self, key: str = "key-1") -> dict:
        return self.kernel.submit_draft(payload(), key, agent_id="test-agent")

    def test_submit_does_not_touch_ledger(self) -> None:
        before = self.target.read_bytes()
        self.submit()
        self.assertEqual(self.target.read_bytes(), before)
        self.assertEqual(self.kernel.get_status()["draft_counts"].get("Submitted"), 1)

    def test_idempotency_returns_same_draft(self) -> None:
        first = self.submit("same-key")
        second = self.submit("same-key")
        self.assertEqual(first["draft"]["draft_id"], second["draft"]["draft_id"])
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(len(self.kernel.list_drafts()), 1)

    def test_validate_approve_commit_once(self) -> None:
        draft_id = self.submit()["draft"]["draft_id"]
        validation = self.kernel.validate_draft(draft_id)
        self.assertEqual(validation["status"], "pass")
        self.assertEqual(validation["diff"]["changed_files"], [TARGET])
        self.kernel.approve_draft(draft_id)
        result = self.kernel.commit_draft(draft_id)
        self.assertIn("2026-01-02 * \"Shop\" \"Lunch\"", self.target.read_text(encoding="utf-8"))
        self.assertEqual(self.kernel.get_draft(draft_id)["workflow"]["state"], "Committed")
        with self.assertRaises(KernelError):
            self.kernel.commit_draft(draft_id)
        self.assertEqual(self.target.read_text(encoding="utf-8").count("Lunch"), 1)
        self.assertIn(result["commit_id"], (self.base / ".ledger-kernel" / "journal" / f"{result['commit_id']}.json").read_text())

    def test_multi_transaction_draft_commits_both(self) -> None:
        import copy

        body = payload()
        second = copy.deepcopy(body["transactions"][0])
        second["date"] = "2026-01-03"
        second["narration"] = "Dinner"
        body["transactions"].append(second)
        draft_id = self.kernel.submit_draft(body, "multi", agent_id="a")["draft"]["draft_id"]
        validation = self.kernel.validate_draft(draft_id)
        self.assertEqual(validation["status"], "pass")
        self.assertEqual(len(validation["diff"]["postings"]), 4)
        self.kernel.approve_draft(draft_id)
        self.kernel.commit_draft(draft_id)
        text = self.target.read_text(encoding="utf-8")
        self.assertEqual(text.count("Lunch"), 1)
        self.assertEqual(text.count("Dinner"), 1)

    def test_unbalanced_transaction_is_blocked_by_beancount(self) -> None:
        body = payload()
        body["transactions"][0]["postings"][1]["units"] = "-20.00"
        draft_id = self.kernel.submit_draft(body, "unbalanced", agent_id="a")["draft"]["draft_id"]
        validation = self.kernel.validate_draft(draft_id)
        self.assertEqual(validation["status"], "blocked")
        codes = {check["code"] for check in validation["checks"]}
        self.assertIn("BEANCOUNT_ERROR", codes)
        with self.assertRaises(KernelError):
            self.kernel.approve_draft(draft_id)

    def test_hard_contract_is_blocked(self) -> None:
        (self.base / ".ledger-kernel" / "contracts.yaml").write_text(
            "schema_version: 1\n"
            "accounts:\n"
            "  Assets:Cash:\n"
            "    policy:\n"
            "      allowed_currencies: [USD]\n",
            encoding="utf-8",
        )
        draft_id = self.kernel.submit_draft(payload(), "contract", agent_id="a")["draft"]["draft_id"]
        validation = self.kernel.validate_draft(draft_id)
        self.assertEqual(validation["status"], "blocked")
        self.assertIn("CONTRACT_CURRENCY", {check["code"] for check in validation["checks"]})

    def test_low_confidence_is_warning_not_block(self) -> None:
        draft_id = self.kernel.submit_draft(payload(confidence=0.1), "low", agent_id="a")["draft"]["draft_id"]
        validation = self.kernel.validate_draft(draft_id)
        self.assertEqual(validation["status"], "pass_with_warnings")
        self.assertIn("LOW_CONFIDENCE", {check["code"] for check in validation["checks"]})

    def test_external_change_invalidates_validation(self) -> None:
        draft_id = self.submit()["draft"]["draft_id"]
        self.kernel.validate_draft(draft_id)
        with open(self.base / "transactions" / "2026.bean", "a", encoding="utf-8") as handle:
            handle.write('\n2026-01-05 * "External" "Edit"\n  Assets:Cash  -1.00 CNY\n  Expenses:Food  1.00 CNY\n')
        with self.assertRaises(KernelError) as caught:
            self.kernel.approve_draft(draft_id)
        self.assertIn(caught.exception.code, {"STALE_REVISION", "INVALID_STATE"})
        self.assertTrue(self.kernel.get_draft(draft_id)["workflow"]["stale"])

    def test_batch_approve_api(self) -> None:
        first = self.kernel.submit_draft(payload(narration="One"), "batch-1", agent_id="a")["draft"]["draft_id"]
        second = self.kernel.submit_draft(payload(narration="Two"), "batch-2", agent_id="a")["draft"]["draft_id"]
        self.kernel.validate_draft(first)
        self.kernel.validate_draft(second)
        results = self.kernel.approve_drafts([first, second])
        self.assertEqual(len(results), 2)
        self.assertEqual(self.kernel.get_draft(first)["workflow"]["state"], "Approved")
        self.assertEqual(self.kernel.get_draft(second)["workflow"]["state"], "Approved")

    def test_reject_and_withdraw(self) -> None:
        draft_id = self.submit()["draft"]["draft_id"]
        self.kernel.validate_draft(draft_id)
        self.kernel.reject_draft(draft_id, "wrong account")
        self.assertEqual(self.kernel.get_draft(draft_id)["workflow"]["state"], "Rejected")

        other = self.kernel.submit_draft(payload(narration="Other"), "key-2", agent_id="a")["draft"]["draft_id"]
        self.kernel.withdraw_draft(other, agent_id="a")
        self.assertEqual(self.kernel.get_draft(other)["workflow"]["state"], "Withdrawn")

    def test_expired_draft_cannot_be_approved(self) -> None:
        body = payload()
        draft_id = self.kernel.submit_draft(body, "expires", agent_id="a")["draft"]["draft_id"]
        self.kernel.validate_draft(draft_id)
        path = self.base / ".ledger-kernel" / "drafts" / f"{draft_id}.json"
        data = path.read_text(encoding="utf-8").replace('"expires_at": null', '"expires_at": "2020-01-01T00:00:00+00:00"')
        path.write_text(data, encoding="utf-8")
        with self.assertRaises(KernelError):
            self.kernel.approve_draft(draft_id)
        self.assertEqual(self.kernel.get_draft(draft_id)["workflow"]["state"], "Expired")

    def test_detached_write_target_blocks_validation(self) -> None:
        main = self.base / "main.bean"
        text = main.read_text(encoding="utf-8")
        text = "\n".join(line for line in text.splitlines() if "kernel-import.bean" not in line) + "\n"
        main.write_text(text, encoding="utf-8")
        status = self.kernel.get_status()
        self.assertFalse(status["write_target_in_closure"])
        draft_id = self.submit("detached")["draft"]["draft_id"]
        with self.assertRaises(KernelError) as caught:
            self.kernel.validate_draft(draft_id)
        self.assertEqual(caught.exception.code, "WRITE_TARGET_INVALID")

    def test_non_target_include_change_changes_revision(self) -> None:
        before = self.kernel.get_status()["ledger_revision"]
        with open(self.base / "transactions" / "2026.bean", "a", encoding="utf-8") as handle:
            handle.write('\n2026-01-06 * "External" "Second"\n  Assets:Cash  -2.00 CNY\n  Expenses:Food  2.00 CNY\n')
        after = self.kernel.get_status()["ledger_revision"]
        self.assertNotEqual(before, after)
        self.assertEqual(self.kernel.get_status()["status"], "Clean")


if __name__ == "__main__":
    unittest.main()
