from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ledger_kernel import __version__
from ledger_kernel.errors import KernelError
from ledger_kernel.kernel import Kernel
from ledger_kernel.locks import FileLock

from .helpers import TARGET, make_kernel, make_ledger, payload


class SimulatedCrash(RuntimeError):
    pass


class CommitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.kernel = make_kernel(self.base)
        self.target = self.base / TARGET

    def tearDown(self) -> None:
        self.kernel.close()
        self.tmp.cleanup()

    def prepared(self, key: str = "k") -> str:
        draft_id = self.kernel.submit_draft(payload(), key, agent_id="a")["draft"]["draft_id"]
        self.kernel.validate_draft(draft_id)
        self.kernel.approve_draft(draft_id)
        return draft_id

    def test_initial_audit_records_package_version(self) -> None:
        event = self.kernel.store.audit_events()[0]
        self.assertEqual(event["event_type"], "KernelInitialized")
        self.assertEqual(event["kernel_version"], __version__)

    def test_batch_approvals_carry_forward_on_empty_write_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = make_ledger(base, target_header="")
            kernel = Kernel.init(root, TARGET)
            try:
                first = kernel.submit_draft(payload(narration="First entry"), "first", agent_id="a")["draft"]["draft_id"]
                second = kernel.submit_draft(payload(narration="Second entry", amount="30.00"), "second", agent_id="a")["draft"]["draft_id"]
                before = kernel.validate_draft(second)
                kernel.validate_draft(first)
                kernel.approve_drafts([first, second])

                kernel.commit_draft(first)
                workflow = kernel.get_draft(second)["workflow"]
                self.assertEqual(workflow["state"], "Approved")

                new_validation = json.loads(
                    (base / ".ledger-kernel" / "validations" / f"{workflow['validation_id']}.json").read_text(encoding="utf-8")
                )
                self.assertEqual(new_validation["review_digest"], before["review_digest"])

                kernel.commit_draft(second)
                self.assertEqual(kernel.get_draft(second)["workflow"]["state"], "Committed")
                text = (base / TARGET).read_text(encoding="utf-8")
                self.assertIn("First entry", text)
                self.assertIn("Second entry", text)
                events = [
                    json.loads(line)["event_type"]
                    for line in (base / ".ledger-kernel" / "audit" / "events.jsonl").read_text(encoding="utf-8").splitlines()
                ]
                self.assertIn("ApprovalCarriedForward", events)
            finally:
                kernel.close()

    def test_identical_batch_entries_keep_their_approvals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = make_ledger(base, target_header="")
            kernel = Kernel.init(root, TARGET)
            try:
                first = kernel.submit_draft(payload(narration="Same entry"), "same-1", agent_id="a")["draft"]["draft_id"]
                second = kernel.submit_draft(payload(narration="Same entry"), "same-2", agent_id="a")["draft"]["draft_id"]
                first_validation = kernel.validate_draft(first)
                second_validation = kernel.validate_draft(second)
                kernel.approve_drafts([first, second])
                kernel.commit_draft(first)
                workflow = kernel.get_draft(second)["workflow"]
                self.assertEqual(workflow["state"], "Approved")
                refreshed = json.loads(
                    (base / ".ledger-kernel" / "validations" / f"{workflow['validation_id']}.json").read_text(encoding="utf-8")
                )
                self.assertEqual(refreshed["review_digest"], second_validation["review_digest"])
                self.assertIn("DUPLICATE_SUSPECTED", {check["code"] for check in refreshed["checks"]})
                kernel.commit_draft(second)
                self.assertEqual(kernel.get_draft(second)["workflow"]["state"], "Committed")
                self.assertEqual((base / TARGET).read_text(encoding="utf-8").count("Same entry"), 2)
                self.assertTrue(first_validation["status"].startswith("pass"))
            finally:
                kernel.close()

    def test_external_change_after_approval_blocks_commit(self) -> None:
        draft_id = self.prepared()
        with open(self.base / "transactions" / "2026.bean", "a", encoding="utf-8") as handle:
            handle.write('\n2026-01-07 * "User" "Manual edit"\n  Assets:Cash  -3.00 CNY\n  Expenses:Food  3.00 CNY\n')
        before = self.target.read_text(encoding="utf-8")
        with self.assertRaises(KernelError):
            self.kernel.commit_draft(draft_id)
        self.assertEqual(self.target.read_text(encoding="utf-8"), before)
        self.assertNotEqual(self.kernel.get_draft(draft_id)["workflow"]["state"], "Committed")

    def test_second_kernel_cannot_hold_lock(self) -> None:
        with FileLock(self.kernel.lock_path):
            with self.assertRaises(KernelError) as caught:
                self.kernel.get_status()
        self.assertEqual(caught.exception.code, "KERNEL_BUSY")

    def test_crash_after_replace_recovers_without_duplicate(self) -> None:
        def crash(name, data):
            if name == "after_replace":
                raise SimulatedCrash("simulated power loss")
            return None

        kernel = Kernel.open(self.base, fault_hook=crash)
        draft_id = kernel.submit_draft(payload(), "crash", agent_id="a")["draft"]["draft_id"]
        kernel.validate_draft(draft_id)
        kernel.approve_draft(draft_id)
        with self.assertRaises(SimulatedCrash):
            kernel.commit_draft(draft_id)
        self.assertIn("Lunch", self.target.read_text(encoding="utf-8"))
        self.assertEqual(kernel.recovery_status()["status"], "RecoveryRequired")
        self.assertEqual(len(kernel.list_observations()), 0)

        restarted = Kernel.open(self.base)
        results = restarted.recover()
        self.assertEqual(results[0]["outcome"], "commit_confirmed")
        self.assertEqual(restarted.get_draft(draft_id)["workflow"]["state"], "Committed")
        self.assertEqual(self.target.read_text(encoding="utf-8").count("Lunch"), 1)
        self.assertEqual(restarted.recovery_status()["status"], "Clean")
        self.assertEqual(restarted.recover(), [])

    def test_commit_only_appends_to_existing_target_content(self) -> None:
        original = self.target.read_text(encoding="utf-8")
        draft_id = self.prepared()
        self.kernel.commit_draft(draft_id)
        updated = self.target.read_text(encoding="utf-8")
        self.assertTrue(updated.startswith(original))
        self.assertEqual(updated.count("2026-01-01"), 0)

    def test_audit_tampering_blocks_commit(self) -> None:
        first = self.prepared("audit-1")
        self.kernel.commit_draft(first)
        second = self.prepared("audit-2")
        audit = self.base / ".ledger-kernel" / "audit" / "events.jsonl"
        lines = audit.read_text(encoding="utf-8").splitlines()
        target = json.loads(lines[-1])
        target["event_hash"] = "sha256:" + "0" * 64
        lines[-1] = json.dumps(target, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        audit.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.assertRaises(KernelError) as caught:
            self.kernel.commit_draft(second)
        self.assertEqual(caught.exception.code, "AUDIT_INTEGRITY")

    def test_reversal_is_a_new_draft_not_a_delete(self) -> None:
        draft_id = self.prepared("reversal-original")
        commit = self.kernel.commit_draft(draft_id)
        proposal = self.kernel.propose_reversal(commit["commit_id"])
        reversal_id = proposal["draft"]["draft_id"]
        validation = self.kernel.validate_draft(reversal_id)
        self.assertNotEqual(validation["status"], "blocked")
        self.kernel.approve_draft(reversal_id)
        self.kernel.commit_draft(reversal_id)
        text = self.target.read_text(encoding="utf-8")
        self.assertIn("Lunch", text)
        self.assertIn("Reversal: Lunch", text)
        self.assertEqual(text.count("Lunch"), 2)
        events = [event["event_type"] for event in self.kernel.audit_tail(200)]
        self.assertIn("CorrectionProposed", events)

    def test_deleted_audit_log_blocks_commit(self) -> None:
        draft_id = self.prepared("audit-deleted")
        (self.base / ".ledger-kernel" / "audit" / "events.jsonl").unlink()
        with self.assertRaises(KernelError) as caught:
            self.kernel.commit_draft(draft_id)
        self.assertEqual(caught.exception.code, "AUDIT_INTEGRITY")
        self.assertNotIn("Lunch", self.target.read_text(encoding="utf-8"))

    def test_crash_before_replace_leaves_approval_usable(self) -> None:
        def crash(name, data):
            if name == "before_replace":
                raise SimulatedCrash("simulated interruption")
            return None

        kernel = Kernel.open(self.base, fault_hook=crash)
        draft_id = kernel.submit_draft(payload(), "abort", agent_id="a")["draft"]["draft_id"]
        kernel.validate_draft(draft_id)
        kernel.approve_draft(draft_id)
        with self.assertRaises(SimulatedCrash):
            kernel.commit_draft(draft_id)
        self.assertNotIn("Lunch", self.target.read_text(encoding="utf-8"))
        journal = kernel.store.list_journals()[0]
        self.assertEqual(journal["status"], "ABORTED")
        self.assertEqual(kernel.recovery_status()["status"], "Clean")

        kernel.fault_hook = None
        result = kernel.commit_draft(draft_id)
        self.assertTrue(result["commit_id"])
        self.assertEqual(self.target.read_text(encoding="utf-8").count("Lunch"), 1)

    def test_crash_after_state_update_is_recoverable(self) -> None:
        def crash(name, data):
            if name == "after_commit":
                raise SimulatedCrash("simulated crash after audit")
            return None

        kernel = Kernel.open(self.base, fault_hook=crash)
        draft_id = kernel.submit_draft(payload(), "late", agent_id="a")["draft"]["draft_id"]
        kernel.validate_draft(draft_id)
        kernel.approve_draft(draft_id)
        with self.assertRaises(SimulatedCrash):
            kernel.commit_draft(draft_id)
        restarted = Kernel.open(self.base)
        restarted.recover()
        self.assertEqual(restarted.get_draft(draft_id)["workflow"]["state"], "Committed")
        self.assertEqual(self.target.read_text(encoding="utf-8").count("Lunch"), 1)


if __name__ == "__main__":
    unittest.main()
