from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ledger_kernel.errors import KernelError
from ledger_kernel.kernel import Kernel

from .helpers import TARGET, make_kernel, payload


class ObservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.kernel = make_kernel(self.base)
        self.target = self.base / TARGET

    def tearDown(self) -> None:
        self.kernel.close()
        self.tmp.cleanup()

    def prepare_approved(self, *, key: str, narration: str, amount: str, date: str) -> str:
        draft_id = self.kernel.submit_draft(
            payload(narration=narration, amount=amount, date=date), key, agent_id="a"
        )["draft"]["draft_id"]
        self.kernel.validate_draft(draft_id)
        self.kernel.approve_draft(draft_id)
        return draft_id

    def test_external_edit_while_kernel_closed_is_observed_not_overwritten(self) -> None:
        accounts = self.base / "accounts.bean"
        original = accounts.read_text(encoding="utf-8")
        accounts.write_text(original + "\n2026-02-01 open Expenses:New CNY\n", encoding="utf-8")
        restarted = Kernel.open(self.base)
        status = restarted.get_status()
        self.assertEqual(status["status"], "Clean")
        self.assertEqual(status["observations"], 1)
        self.assertEqual(accounts.read_text(encoding="utf-8"), original + "\n2026-02-01 open Expenses:New CNY\n")
        observation = restarted.list_observations()[0]
        self.assertEqual(observation["source_kind"], "external_unknown")
        self.assertEqual(observation["attribution_confidence"], "unknown")
        self.assertEqual(observation["changed_files"], ["accounts.bean"])
        self.assertFalse(observation["diff_unavailable"])
        self.assertEqual(observation["ledger_load_status"], "valid")

    def test_invalid_external_edit_blocks_controlled_commits(self) -> None:
        draft_id = self.prepare_approved(key="invalid", narration="Lunch", amount="1.00", date="2026-01-02")
        accounts = self.base / "accounts.bean"
        accounts.write_text(accounts.read_text(encoding="utf-8") + "\nThis is not beancount\n", encoding="utf-8")
        status = self.kernel.get_status()
        self.assertEqual(status["status"], "LedgerInvalid")
        self.assertGreaterEqual(status["observations"], 1)
        observation = self.kernel.list_observations()[0]
        self.assertEqual(observation["ledger_load_status"], "invalid")
        self.assertTrue(observation["action_required"])
        with self.assertRaises(KernelError):
            self.kernel.commit_draft(draft_id)
        self.assertNotIn("Lunch", self.target.read_text(encoding="utf-8"))

    def test_external_append_after_commit_is_not_a_known_transition(self) -> None:
        draft_id = self.prepare_approved(key="first", narration="Kernel", amount="1.00", date="2026-01-02")
        self.kernel.commit_draft(draft_id)
        with open(self.target, "a", encoding="utf-8") as handle:
            handle.write('\n2026-01-09 * "User" "After commit"\n  Assets:Cash  -4.00 CNY\n  Expenses:Food  4.00 CNY\n')
        status = self.kernel.get_status()
        self.assertEqual(status["status"], "Clean")
        self.assertGreaterEqual(status["observations"], 1)
        observation = self.kernel.list_observations()[0]
        self.assertEqual(observation["source_kind"], "external_unknown")
        self.assertIn("After commit", self.target.read_text(encoding="utf-8"))

    def test_deleting_derived_state_rebuilds_from_ledger(self) -> None:
        (self.base / ".ledger-kernel" / "state.json").unlink()
        status = Kernel.open(self.base).get_status()
        self.assertEqual(status["status"], "Clean")
        self.assertGreaterEqual(len(Kernel.open(self.base).list_accounts()), 6)

    def test_kernel_commit_is_not_reported_as_external_change(self) -> None:
        draft_id = self.prepare_approved(key="quiet", narration="Quiet", amount="1.00", date="2026-01-02")
        self.kernel.commit_draft(draft_id)
        restarted = Kernel.open(self.base)
        self.assertEqual(restarted.get_status()["observations"], 0)

    def test_batch_approvals_carry_forward(self) -> None:
        ids = [
            self.prepare_approved(key=f"b{index}", narration=f"Batch {index}", amount=f"{index + 1}.00", date=f"2026-01-0{index + 2}")
            for index in range(3)
        ]
        for draft_id in ids:
            self.kernel.commit_draft(draft_id)
        for draft_id in ids:
            self.assertEqual(self.kernel.get_draft(draft_id)["workflow"]["state"], "Committed")
        text = self.target.read_text(encoding="utf-8")
        for index in range(3):
            self.assertEqual(text.count(f"Batch {index}"), 1)
        events = [event for event in self.kernel.audit_tail(200) if event["event_type"] == "ApprovalCarriedForward"]
        self.assertGreaterEqual(len(events), 2)

    def test_digest_change_breaks_carry_forward(self) -> None:
        (self.base / ".ledger-kernel" / "contracts.yaml").write_text(
            "schema_version: 1\n"
            "accounts:\n"
            "  Assets:Cash:\n"
            "    policy:\n"
            "      balance_policy:\n"
            "        mode: warn_only\n"
            "        max: 980\n",
            encoding="utf-8",
        )
        first = self.prepare_approved(key="d1", narration="First", amount="25.00", date="2026-01-02")
        second = self.prepare_approved(key="d2", narration="Second", amount="10.00", date="2026-01-03")
        self.kernel.commit_draft(first)
        with self.assertRaises(KernelError):
            self.kernel.commit_draft(second)
        self.assertEqual(self.kernel.get_draft(second)["workflow"]["state"], "NeedsHumanApproval")
        self.assertNotIn("Second", self.target.read_text(encoding="utf-8"))

    def test_external_change_mid_batch_invalidates_remaining_approvals(self) -> None:
        first = self.prepare_approved(key="m1", narration="First", amount="1.00", date="2026-01-02")
        second = self.prepare_approved(key="m2", narration="Second", amount="2.00", date="2026-01-03")
        self.kernel.commit_draft(first)
        with open(self.base / "transactions" / "2026.bean", "a", encoding="utf-8") as handle:
            handle.write('\n2026-01-10 * "User" "Mid batch"\n  Assets:Cash  -5.00 CNY\n  Expenses:Food  5.00 CNY\n')
        with self.assertRaises(KernelError):
            self.kernel.commit_draft(second)
        self.assertNotEqual(self.kernel.get_draft(second)["workflow"]["state"], "Committed")
        self.assertIn("Mid batch", (self.base / "transactions" / "2026.bean").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
