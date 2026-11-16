from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ledger_kernel.cli import main as cli_main
from ledger_kernel.revision import contract_revision
from ledger_kernel.util import sha256_bytes

from .helpers import make_kernel, payload

CONTRACT = """\
schema_version: 1
accounts:
  Assets:Cash:
    policy:
      reconciliation:
        cadence: monthly
"""

AMOUNT_FIELDS = {"amount", "units", "before", "after", "delta", "min", "max"}


class CliJsonContractTests(unittest.TestCase):
    """Freeze the read-only JSON surface used by external frontends (v0.3)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.kernel = make_kernel(self.base)
        path = self.kernel.config.contracts_path
        self.kernel.apply_contracts(
            CONTRACT,
            expected_current=contract_revision(path),
            confirm="sha256:" + sha256_bytes(CONTRACT.encode("utf-8")),
        )
        self.kernel.reconcile_account("Assets:Cash", as_of=dt.date.today(), declared={"CNY": "1000.00"})
        self.draft_id = self.kernel.submit_draft(payload(), "contract-1", agent_id="agent")["draft"][
            "draft_id"
        ]
        self.kernel.validate_draft(self.draft_id)
        self.approved_id = self.kernel.submit_draft(
            payload(narration="Books", date="2026-01-03", account="Expenses:Books"),
            "contract-2",
            agent_id="agent",
        )["draft"]["draft_id"]
        self.kernel.validate_draft(self.approved_id)
        self.kernel.approve_draft(self.approved_id)

    def tearDown(self) -> None:
        self.kernel.close()
        self.tmp.cleanup()

    def json_cli(self, *args: str) -> dict:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            code = cli_main(["--ledger", str(self.base), "--json", *args])
        self.assertEqual(code, 0, output.getvalue())
        return json.loads(output.getvalue())

    def json_cli_error(self, *args: str) -> tuple[int, dict]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            code = cli_main(["--ledger", str(self.base), "--json", *args])
        self.assertEqual(code, 2, output.getvalue())
        return code, json.loads(output.getvalue())

    def assert_amount_string(self, value: object) -> None:
        self.assertIsInstance(value, str, f"amount must be a decimal string, got {value!r}")

    def test_review_queue_envelope_and_row_types(self) -> None:
        data = self.json_cli("review")
        self.assertEqual(data["mode"], "queue")
        rows = {row["draft_id"]: row for row in data["drafts"]}
        self.assertEqual(set(rows), {self.draft_id, self.approved_id})
        row = rows[self.draft_id]
        required = {
            "draft_id",
            "state",
            "stale",
            "validation_id",
            "agent",
            "transactions",
            "accounts",
            "narration",
            "created_at",
            "date",
            "payee",
            "amount",
            "currency",
            "validation_status",
            "checks",
        }
        self.assertTrue(required <= set(row), required - set(row))
        self.assertIsInstance(row["draft_id"], str)
        self.assertIsInstance(row["state"], str)
        self.assertIsInstance(row["stale"], bool)
        self.assertIsInstance(row["transactions"], int)
        self.assertIsInstance(row["accounts"], list)
        self.assertTrue(all(isinstance(item, str) for item in row["accounts"]))
        self.assert_amount_string(row["amount"])
        self.assertIsInstance(row["currency"], str)
        self.assertIsInstance(row["date"], str)
        self.assertIsInstance(row["checks"], list)
        self.assertIsInstance(row["payee"], (str, type(None)))
        self.assertIsInstance(row["validation_status"], (str, type(None)))

    def test_review_detail_exposes_diff_checks_and_raw_draft(self) -> None:
        data = self.json_cli("review", self.draft_id)
        self.assertEqual(data["mode"], "detail")
        self.assertEqual(len(data["drafts"]), 1)
        entry = data["drafts"][0]
        self.assertEqual(entry["draft_id"], self.draft_id)
        self.assertIsInstance(entry["state"], str)
        self.assertIsInstance(entry["fragment"], str)
        self.assertIsInstance(entry["checks"], list)
        diff = entry["diff"]
        for key in ("changed_files", "deltas", "balances_before", "balances_after", "postings", "file_diff"):
            self.assertIn(key, diff)
        self.assertIsInstance(diff["changed_files"], list)
        for delta in diff["deltas"]:
            self.assert_amount_string(delta["before"])
            self.assert_amount_string(delta["after"])
            self.assert_amount_string(delta["delta"])
        for posting in diff["postings"]:
            self.assert_amount_string(posting["units"])
        draft = entry["draft"]
        self.assertEqual(draft["draft_id"], self.draft_id)
        self.assertIsInstance(draft["transactions"], list)
        self.assertIsInstance(draft["confidence"], float)

    def test_review_by_account_groups_and_unknown_account_note(self) -> None:
        data = self.json_cli("review", "--by-account")
        self.assertEqual(data["mode"], "by_account")
        self.assertIsNone(data["account"])
        self.assertIsNone(data["note"])
        self.assertGreaterEqual(len(data["groups"]), 1)
        group = data["groups"][0]
        for key in ("account", "lifecycle", "balances", "contract", "reconciliation", "pending_drafts", "warnings", "draft_count"):
            self.assertIn(key, group)
        self.assertIsInstance(group["account"], str)
        self.assertIsInstance(group["lifecycle"], str)
        for currency, amount in group["balances"].items():
            self.assertIsInstance(currency, str)
            self.assert_amount_string(amount)
        self.assertIn(group["reconciliation"]["status"], {"not_configured", "never", "current", "overdue"})
        self.assertIn(group["reconciliation"]["difference_status"], {"none", "zero", "nonzero"})
        self.assertIsInstance(group["pending_drafts"], list)
        self.assertIsInstance(group["draft_count"], int)
        self.assertIsInstance(group["warnings"], list)

        unknown = self.json_cli("review", "--by-account", "--account", "Expenses:Missing")
        self.assertEqual(unknown["mode"], "by_account")
        self.assertEqual(unknown["account"], "Expenses:Missing")
        self.assertEqual(unknown["groups"], [])
        self.assertTrue(unknown["note"].startswith("unknown account:"))

    def test_accounts_list_rows_include_account_layer_views(self) -> None:
        rows = self.json_cli("accounts", "list")
        self.assertIsInstance(rows, list)
        row = next(item for item in rows if item["account"] == "Assets:Cash")
        required = {
            "account",
            "lifecycle",
            "open_date",
            "close_date",
            "currencies",
            "booking",
            "balances",
            "aliases",
            "contract",
            "pending_drafts",
            "assertion_status",
            "last_assertion",
            "reconciliation",
        }
        self.assertTrue(required <= set(row), required - set(row))
        self.assertEqual(row["assertion_status"], "unset")
        self.assertIsNone(row["last_assertion"])
        self.assertEqual(row["reconciliation"]["status"], "current")
        self.assertIsInstance(row["pending_drafts"], int)
        for amount in row["balances"].values():
            self.assert_amount_string(amount)

    def test_accounts_list_coverage_wrapper(self) -> None:
        data = self.json_cli("accounts", "list", "--coverage")
        self.assertIn("accounts", data)
        self.assertIn("coverage", data)
        self.assertIn("unmanaged_accounts", data)
        coverage = data["coverage"]
        for key in (
            "active_account_count",
            "managed_accounts",
            "unmanaged_accounts",
            "uncovered_accounts",
            "unopened_contract_accounts",
            "inactive_contract_accounts",
            "ratio",
        ):
            self.assertIn(key, coverage)
        self.assertIsInstance(coverage["active_account_count"], int)
        self.assertIsInstance(coverage["ratio"], float)
        self.assertIn("Assets:Cash", coverage["managed_accounts"])

    def test_accounts_show_fields(self) -> None:
        data = self.json_cli("accounts", "show", "Assets:Cash")
        self.assertEqual(data["account"], "Assets:Cash")
        self.assertIn("definition", data)
        for key in ("open_date", "close_date", "currencies", "booking", "source_file", "source_line"):
            self.assertIn(key, data["definition"])
        self.assert_amount_string(data["balances"]["CNY"])
        self.assertEqual(data["reconciliation"]["status"], "current")
        self.assertIn("last_posting_date", data)
        self.assertIsInstance(data["note"], str)

    def test_contracts_show_fields(self) -> None:
        data = self.json_cli("contracts", "show")
        self.assertTrue(data["valid"])
        self.assertIsNone(data["error"])
        self.assertTrue(data["contract_revision"].startswith("sha256:"))
        self.assertEqual(data["schema_version"], 1)
        self.assertIn("Assets:Cash", data["accounts"])
        policy = data["accounts"]["Assets:Cash"]
        self.assertEqual(policy["reconciliation"]["cadence"], "monthly")
        self.assertIn("coverage", data)
        self.assertIsInstance(data["coverage"]["ratio"], float)
        self.assertGreaterEqual(data["coverage"]["ratio"], 0.0)
        self.assertLessEqual(data["coverage"]["ratio"], 1.0)
        self.assertIn("Assets:Cash", data["coverage"]["managed_accounts"])
        self.assertIsInstance(data["warnings"], list)

    def test_error_envelope_is_stable(self) -> None:
        _code, data = self.json_cli_error("accounts", "show", "Assets:Missing")
        self.assertFalse(data["ok"])
        error = data["error"]
        self.assertEqual(error["code"], "ACCOUNT_NOT_FOUND")
        self.assertIsInstance(error["message"], str)
        self.assertIsInstance(error["details"], dict)

    def test_approve_requires_explicit_confirmation(self) -> None:
        output = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO("")), contextlib.redirect_stdout(
            output
        ), contextlib.redirect_stderr(io.StringIO()):
            code = cli_main(["--ledger", str(self.base), "--json", "drafts", "approve", self.draft_id])
        self.assertEqual(code, 2)
        error = json.loads(output.getvalue())["error"]
        self.assertEqual(error["code"], "INVALID_STATE")
        self.assertEqual(self.kernel.get_draft(self.draft_id)["workflow"]["state"], "NeedsHumanApproval")

    def test_contract_has_no_floating_point_amounts(self) -> None:
        detail = self.json_cli("review", self.draft_id)["drafts"][0]
        for delta in detail["diff"]["deltas"]:
            for key in ("before", "after", "delta"):
                self.assert_amount_string(delta[key])
        rows = self.json_cli("accounts", "list")
        for row in rows:
            for amount in row["balances"].values():
                self.assert_amount_string(amount)


if __name__ == "__main__":
    unittest.main()
