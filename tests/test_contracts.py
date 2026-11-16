from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from ledger_kernel.cli import main as cli_main
from ledger_kernel.contracts import Contract, parse_contract
from ledger_kernel.errors import KernelError
from ledger_kernel.revision import contract_revision
from ledger_kernel.util import sha256_bytes

from .helpers import TARGET, make_kernel, make_ledger, payload

CONTRACT = """\
schema_version: 1
unmanaged_accounts:
  - Assets:Bank
accounts:
  Assets:Cash:
    aliases: [现金]
    policy:
      allowed_currencies: [CNY]
"""

OVERLAP = """\
schema_version: 1
unmanaged_accounts: [Assets:Cash]
accounts:
  Assets:Cash:
    policy:
      allowed_currencies: [CNY]
"""


def sha(text: str) -> str:
    return "sha256:" + sha256_bytes(text.encode("utf-8"))


def contract_events(kernel) -> list[dict]:
    return [event for event in kernel.store.audit_events() if event.get("event_type") == "ContractChanged"]


class ContractParsingTests(unittest.TestCase):
    def test_legacy_contract_without_unmanaged_accounts_loads(self) -> None:
        contract = parse_contract(
            {
                "schema_version": 1,
                "accounts": {"Assets:Cash": {"policy": {"allowed_currencies": ["CNY"]}}},
            }
        )
        self.assertIsInstance(contract, Contract)
        self.assertEqual(contract.unmanaged_accounts, ())
        self.assertIn("Assets:Cash", contract.accounts)

    def test_unmanaged_accounts_are_parsed(self) -> None:
        contract = parse_contract(
            {"schema_version": 1, "unmanaged_accounts": ["Assets:Bank", "Assets:Broker"], "accounts": {}}
        )
        self.assertEqual(contract.unmanaged_accounts, ("Assets:Bank", "Assets:Broker"))

    def test_unmanaged_accounts_cannot_overlap_accounts(self) -> None:
        with self.assertRaises(KernelError) as caught:
            parse_contract(
                {
                    "schema_version": 1,
                    "unmanaged_accounts": ["Assets:Cash"],
                    "accounts": {"Assets:Cash": {"policy": {"allowed_currencies": ["CNY"]}}},
                }
            )
        self.assertEqual(caught.exception.code, "CONTRACT_INVALID")

    def test_unmanaged_accounts_must_be_valid_account_names(self) -> None:
        with self.assertRaises(KernelError) as caught:
            parse_contract({"schema_version": 1, "unmanaged_accounts": ["Not An Account"], "accounts": {}})
        self.assertEqual(caught.exception.code, "CONTRACT_INVALID")

    def test_unmanaged_accounts_reject_duplicates(self) -> None:
        with self.assertRaises(KernelError) as caught:
            parse_contract(
                {"schema_version": 1, "unmanaged_accounts": ["Assets:Bank", "Assets:Bank"], "accounts": {}}
            )
        self.assertEqual(caught.exception.code, "CONTRACT_INVALID")

    def test_unknown_top_level_field_is_still_rejected(self) -> None:
        with self.assertRaises(KernelError) as caught:
            parse_contract({"schema_version": 1, "accounts": {}, "unexpected_field": True})
        self.assertEqual(caught.exception.code, "CONTRACT_INVALID")


class ContractApplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.kernel = make_kernel(self.base)
        self.path = self.kernel.config.contracts_path

    def tearDown(self) -> None:
        self.kernel.close()
        self.tmp.cleanup()

    def test_apply_replaces_file_records_audit_and_keeps_ledger_revision(self) -> None:
        status_before = self.kernel.get_status()
        before_bytes = self.path.read_bytes()
        before_revision = contract_revision(self.path)

        result = self.kernel.apply_contracts(
            CONTRACT,
            expected_current=before_revision,
            confirm=sha(CONTRACT),
            note="manage cash",
        )

        self.assertTrue(result["applied"])
        self.assertTrue(result["changed"])
        self.assertEqual(self.path.read_text(encoding="utf-8"), CONTRACT)
        self.assertEqual(result["previous_revision"], before_revision)
        self.assertEqual(result["contract_revision"], sha(CONTRACT))

        events = contract_events(self.kernel)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["before_sha256"], before_revision)
        self.assertEqual(event["after_sha256"], sha(CONTRACT))
        self.assertEqual(event["actor"], {"type": "human", "id": "local-user"})
        self.assertEqual(event["note"], "manage cash")

        self.assertEqual(
            self.kernel.store.read_blob_text(before_revision.split(":", 1)[1]),
            before_bytes.decode("utf-8"),
        )
        self.assertEqual(self.kernel.store.read_blob_text(sha(CONTRACT).split(":", 1)[1]), CONTRACT)
        self.assertTrue(self.kernel.store.verify_audit()["ok"])
        self.assertEqual(self.kernel.get_status()["ledger_revision"], status_before["ledger_revision"])

        # The applied revision is persisted in state.json, so a later read must
        # not record a second ContractChanged event.
        self.kernel.get_status()
        self.assertEqual(len(contract_events(self.kernel)), 1)

        view = self.kernel.contracts_view()
        self.assertTrue(view["valid"])
        self.assertEqual(view["contract_revision"], sha(CONTRACT))
        self.assertEqual(view["account_count"], 1)
        self.assertEqual(view["unmanaged_accounts"], ["Assets:Bank"])
        self.assertEqual(view["coverage"]["active_account_count"], 6)
        self.assertEqual(view["coverage"]["managed_accounts"], ["Assets:Cash"])
        self.assertEqual(view["coverage"]["unmanaged_accounts"], ["Assets:Bank"])
        self.assertEqual(view["coverage"]["ratio"], round(1 / 6, 4))

    def test_apply_rejects_stale_expected_current_without_writing(self) -> None:
        before_bytes = self.path.read_bytes()
        with self.assertRaises(KernelError) as caught:
            self.kernel.apply_contracts(
                CONTRACT, expected_current=sha("some other content"), confirm=sha(CONTRACT)
            )
        self.assertEqual(caught.exception.code, "CONTRACT_CONFLICT")
        self.assertEqual(self.path.read_bytes(), before_bytes)
        self.assertEqual(contract_events(self.kernel), [])

    def test_apply_requires_expected_current(self) -> None:
        before_bytes = self.path.read_bytes()
        for expected_current in (None, ""):
            with self.subTest(expected_current=expected_current):
                with self.assertRaises(KernelError) as caught:
                    self.kernel.apply_contracts(
                        CONTRACT, expected_current=expected_current, confirm=sha(CONTRACT)
                    )
                self.assertEqual(caught.exception.code, "CONTRACT_CONFLICT")
                self.assertEqual(self.path.read_bytes(), before_bytes)

    def test_apply_requires_matching_confirm_hash(self) -> None:
        before_bytes = self.path.read_bytes()
        for confirm in (None, "", sha("other content")):
            with self.subTest(confirm=confirm):
                with self.assertRaises(KernelError) as caught:
                    self.kernel.apply_contracts(
                        CONTRACT,
                        expected_current=contract_revision(self.path),
                        confirm=confirm,
                    )
                self.assertEqual(caught.exception.code, "CONTRACT_CONFLICT")
                self.assertEqual(self.path.read_bytes(), before_bytes)

    def test_apply_rejects_invalid_contracts_without_writing(self) -> None:
        cases = {
            "unknown top-level field": "schema_version: 1\naccounts: {}\nunexpected_field: true\n",
            "unmanaged overlaps accounts": OVERLAP,
            "invalid unmanaged account": "schema_version: 1\nunmanaged_accounts: ['Not An Account']\naccounts: {}\n",
            "broken yaml": "schema_version: 1\naccounts: [\n",
        }
        for label, text in cases.items():
            with self.subTest(label=label):
                before_bytes = self.path.read_bytes()
                with self.assertRaises(KernelError) as caught:
                    self.kernel.apply_contracts(
                        text,
                        expected_current=contract_revision(self.path),
                        confirm=sha(text),
                    )
                self.assertEqual(caught.exception.code, "CONTRACT_INVALID")
                self.assertEqual(self.path.read_bytes(), before_bytes)
        self.assertEqual(contract_events(self.kernel), [])

    def test_apply_to_absent_file_uses_absent_sentinel(self) -> None:
        self.path.unlink()
        result = self.kernel.apply_contracts(
            CONTRACT, expected_current="absent", confirm=sha(CONTRACT)
        )
        self.assertTrue(result["applied"])
        self.assertEqual(result["previous_revision"], "sha256:absent")
        self.assertEqual(self.path.read_text(encoding="utf-8"), CONTRACT)
        self.assertEqual(contract_revision(self.path), sha(CONTRACT))

    def test_apply_identical_content_is_a_noop(self) -> None:
        current = self.path.read_text(encoding="utf-8")
        before_events = len(contract_events(self.kernel))
        result = self.kernel.apply_contracts(
            current,
            expected_current=contract_revision(self.path),
            confirm=sha(current),
        )
        self.assertFalse(result["applied"])
        self.assertFalse(result["changed"])
        self.assertEqual(len(contract_events(self.kernel)), before_events)

    def test_apply_invalidates_pending_validation_and_approval(self) -> None:
        draft_id = self.kernel.submit_draft(payload(), "contract-apply", agent_id="a")["draft"]["draft_id"]
        self.kernel.validate_draft(draft_id)
        self.kernel.approve_draft(draft_id)
        self.assertEqual(self.kernel.get_draft(draft_id)["workflow"]["state"], "Approved")

        self.kernel.apply_contracts(
            CONTRACT,
            expected_current=contract_revision(self.path),
            confirm=sha(CONTRACT),
        )

        # apply itself must invalidate the approval; a later read may revalidate
        # the draft under the new contract, but never restore the approval.
        invalidated = self.kernel.store.load_draft_state(draft_id)
        self.assertEqual(invalidated["state"], "NeedsHumanApproval")
        self.assertTrue(invalidated["stale"])
        self.assertEqual(invalidated["stale_reason"], "contract")
        self.assertIsNone(invalidated["approval"])

        workflow = self.kernel.get_draft(draft_id)["workflow"]
        self.assertEqual(workflow["state"], "NeedsHumanApproval")
        self.assertIsNone(workflow["approval"])

        validation = self.kernel.validate_draft(draft_id)
        self.assertEqual(validation["status"], "pass")
        self.assertEqual(self.kernel.get_draft(draft_id)["workflow"]["state"], "NeedsHumanApproval")
        self.kernel.approve_draft(draft_id)
        self.assertEqual(self.kernel.get_draft(draft_id)["workflow"]["state"], "Approved")

    def test_contracts_view_reports_invalid_contract_without_raising(self) -> None:
        self.path.write_text("schema_version: 1\naccounts: {}\nnot_a_field: true\n", encoding="utf-8")
        view = self.kernel.contracts_view()
        self.assertFalse(view["valid"])
        self.assertIsNotNone(view["error"])
        self.assertEqual(view["error"]["code"], "CONTRACT_INVALID")

    def test_contracts_view_warns_about_unopened_accounts(self) -> None:
        self.path.write_text(
            "schema_version: 1\naccounts:\n  Assets:Savings:Cash:\n    policy: {}\n",
            encoding="utf-8",
        )
        view = self.kernel.contracts_view()
        self.assertTrue(view["valid"])
        self.assertEqual(view["coverage"]["unopened_contract_accounts"], ["Assets:Savings:Cash"])
        warnings = {warning["code"]: warning for warning in view["warnings"]}
        self.assertIn("CONTRACT_UNKNOWN_ACCOUNT", warnings)
        self.assertEqual(warnings["CONTRACT_UNKNOWN_ACCOUNT"]["account"], "Assets:Savings:Cash")

    def test_preview_contracts_reports_diff_and_affected_accounts(self) -> None:
        preview = self.kernel.preview_contracts(CONTRACT)
        self.assertTrue(preview["valid"])
        self.assertTrue(preview["changed"])
        self.assertEqual(preview["contract_revision"], sha(CONTRACT))
        self.assertEqual(preview["affected"]["accounts"]["added"], ["Assets:Cash"])
        self.assertEqual(preview["affected"]["accounts"]["removed"], [])
        self.assertEqual(preview["affected"]["unmanaged"]["added"], ["Assets:Bank"])
        self.assertIn("+  Assets:Cash:", preview["diff"])
        # Preview must never write the contracts file.
        self.assertNotEqual(self.path.read_text(encoding="utf-8"), CONTRACT)

    def test_preview_contracts_rejects_invalid_text(self) -> None:
        with self.assertRaises(KernelError) as caught:
            self.kernel.preview_contracts("schema_version: 1\naccounts: {}\nunknown_field: true\n")
        self.assertEqual(caught.exception.code, "CONTRACT_INVALID")


class ContractsCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.kernel = make_kernel(self.base)
        self.path = self.kernel.config.contracts_path

    def tearDown(self) -> None:
        self.kernel.close()
        self.tmp.cleanup()

    def run_cli(self, *args: str) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            code = cli_main(list(args))
        return code, output.getvalue()

    def write_candidate(self, text: str) -> Path:
        candidate = self.base / "contracts.new.yaml"
        candidate.write_text(text, encoding="utf-8")
        return candidate

    def test_contracts_show_reports_revision_and_coverage(self) -> None:
        code, output = self.run_cli("--ledger", str(self.base), "--json", "contracts", "show")
        self.assertEqual(code, 0, output)
        data = json.loads(output)
        self.assertTrue(data["valid"])
        self.assertEqual(data["contract_revision"], contract_revision(self.path))
        self.assertEqual(data["coverage"]["active_account_count"], 6)
        self.assertEqual(data["coverage"]["ratio"], 0.0)

    def test_contracts_validate_reports_coverage_without_writing(self) -> None:
        candidate = self.write_candidate(CONTRACT)
        before = self.path.read_bytes()
        code, output = self.run_cli(
            "--ledger", str(self.base), "--json", "contracts", "validate", "--file", str(candidate)
        )
        self.assertEqual(code, 0, output)
        data = json.loads(output)
        self.assertTrue(data["valid"])
        self.assertEqual(data["coverage"]["managed_accounts"], ["Assets:Cash"])
        self.assertEqual(data["affected"]["accounts"]["added"], ["Assets:Cash"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_contracts_validate_rejects_invalid_schema(self) -> None:
        candidate = self.write_candidate("schema_version: 1\naccounts: {}\nbogus: true\n")
        code, output = self.run_cli(
            "--ledger", str(self.base), "--json", "contracts", "validate", "--file", str(candidate)
        )
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["error"]["code"], "CONTRACT_INVALID")

    def test_contracts_diff_shows_unified_diff_and_affected_accounts(self) -> None:
        candidate = self.write_candidate(CONTRACT)
        code, output = self.run_cli(
            "--ledger", str(self.base), "--json", "contracts", "diff", "--file", str(candidate)
        )
        self.assertEqual(code, 0, output)
        data = json.loads(output)
        self.assertIn("-accounts: {}", data["diff"])
        self.assertEqual(data["affected"]["accounts"]["added"], ["Assets:Cash"])

    def test_contracts_apply_requires_expected_current(self) -> None:
        candidate = self.write_candidate(CONTRACT)
        before = self.path.read_bytes()
        code, output = self.run_cli(
            "--ledger", str(self.base), "--json", "contracts", "apply",
            "--file", str(candidate), "--confirm", sha(CONTRACT),
        )
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["error"]["code"], "CONTRACT_CONFLICT")
        self.assertEqual(self.path.read_bytes(), before)

    def test_contracts_apply_requires_confirm(self) -> None:
        candidate = self.write_candidate(CONTRACT)
        before = self.path.read_bytes()
        code, output = self.run_cli(
            "--ledger", str(self.base), "--json", "contracts", "apply",
            "--file", str(candidate), "--expected-current", contract_revision(self.path),
        )
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["error"]["code"], "CONTRACT_CONFLICT")
        self.assertEqual(self.path.read_bytes(), before)

    def test_contracts_apply_success(self) -> None:
        candidate = self.write_candidate(CONTRACT)
        code, output = self.run_cli(
            "--ledger", str(self.base), "--json", "contracts", "apply",
            "--file", str(candidate),
            "--expected-current", contract_revision(self.path),
            "--confirm", sha(CONTRACT),
            "--note", "cli apply",
        )
        self.assertEqual(code, 0, output)
        data = json.loads(output)
        self.assertTrue(data["applied"])
        self.assertEqual(self.path.read_text(encoding="utf-8"), CONTRACT)
        self.assertEqual(contract_revision(self.path), sha(CONTRACT))

    def test_contracts_apply_stale_expected_current_is_rejected(self) -> None:
        candidate = self.write_candidate(CONTRACT)
        before = self.path.read_bytes()
        code, output = self.run_cli(
            "--ledger", str(self.base), "--json", "contracts", "apply",
            "--file", str(candidate),
            "--expected-current", sha("stale"),
            "--confirm", sha(CONTRACT),
        )
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["error"]["code"], "CONTRACT_CONFLICT")
        self.assertEqual(self.path.read_bytes(), before)

    def test_accounts_list_coverage_reports_managed_and_unmanaged(self) -> None:
        candidate = self.write_candidate(CONTRACT)
        code, output = self.run_cli(
            "--ledger", str(self.base), "--json", "contracts", "apply",
            "--file", str(candidate),
            "--expected-current", contract_revision(self.path),
            "--confirm", sha(CONTRACT),
        )
        self.assertEqual(code, 0, output)
        code, output = self.run_cli(
            "--ledger", str(self.base), "--json", "accounts", "list", "--coverage"
        )
        self.assertEqual(code, 0, output)
        data = json.loads(output)
        self.assertEqual(len(data["accounts"]), 6)
        self.assertEqual(data["coverage"]["managed_accounts"], ["Assets:Cash"])
        self.assertEqual(data["unmanaged_accounts"], ["Assets:Bank"])


if __name__ == "__main__":
    unittest.main()
